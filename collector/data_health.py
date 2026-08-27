# -*- coding: utf-8 -*-
"""数据健全性自检（2026-08-27）。

把"靠人眼发现异常"升级成"工具自动发现并标注"。纯标准库、纯函数、可单测。

设计原则：
  - 只标"疑似"，不标"确定"；规则宁严勿松，首要目标是正常数据零误报。
  - 两类入口：
      check_row_health(row)   单采样点校验（物理约束等，可实时逐点跑）
      scan_rows(rows)         整份数据扫描（段级规则：采错进程/假 Jank/量级突变/连续缺失）
  - 阈值经 4 份真实数据调校（见 tests）：
      output/20260827_090606、20260825_134148   → 正常样本（不应误报）
      output/20260827_083117                   → 采错进程异常样本（应检出）
      output/20260824_084252                   → 同样采错进程（应检出，验证规则泛化）
"""

# ---------------- 阈值常量（集中管理，便于调参） ----------------
# 采错进程：渲染中（fps 中位 ≥ 阈值）但进程 CPU 几乎为 0 的样本占比超过此值
WRONG_PROC_MIN_FPS = 30.0      # fps 中位数下限：低于它可能真的不渲染，不算"采错"
WRONG_PROC_CPU_NEAR_ZERO = 2.0  # 进程 CPU% < 此值视为"几乎为 0"（闲置进程特征）
WRONG_PROC_FRACTION = 0.30     # 近零 CPU 样本占比阈值（异常 37.7%/47.2%，正常 0%）
WRONG_PROC_MIN_POINTS = 30     # 最少样本数，避免小窗口误报

# 假 Jank：jank 中位高 + fps 中位高 + 帧时间并未明显升高 → 阈值口径问题
FAKE_JANK_RATE = 0.9           # jank_rate 中位（90%）——注意 jank_rate 是 0~1 比例
FAKE_JANK_MIN_FPS = 50.0       # fps 中位下限
FAKE_JANK_P50_ABS_MS = 30.0    # frame_p50 中位 < 此值视为"帧时间未明显升高"（60Hz 满帧 16.7ms）

# 内存量级突变：相对滚动中位数的倍数，超出上/下界视为突变
PSS_JUMP_UP = 5.0              # >5× 滚动中位
PSS_JUMP_DOWN = 0.2            # <0.2× 滚动中位
PSS_ROLLING_WINDOW = 20        # 滚动中位窗口（点数）
PSS_MIN_RATIO_POINTS = 3       # 连续多少点同向突变才上报（防单点毛刺）

# 连续缺失：某关键指标连续 None 的点数阈值
MISSING_RUN = 5

# RSS<PSS：仅当缺口超过 PSS 的此比例才判"解析异常"（否则是 smaps_rollup 舍入噪声）
RSS_LT_PSS_MIN_RATIO = 0.01    # 1%（实测异常样本缺口中位仅 0.41%，阈值要能避开舍入噪声）

# 关键指标（用于"连续缺失"检测）
KEY_METRICS = {
    "fps": lambda r: (r.get("fps") or {}).get("fps"),
    "cpu_proc": lambda r: (r.get("cpu") or {}).get("cpu_proc_pct"),
    "pss": lambda r: (r.get("mem") or {}).get("pss_kb"),
    "temp": lambda r: (r.get("therm") or {}).get("temp_c"),
}


def _median(values):
    """返回中位数（空/全 None 返回 None）。"""
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _rolling_median_window(values, window):
    """精确滚动中位：用 deque 维护窗口顺序 + 排序副本算中位（数据量小，够用）。"""
    from collections import deque
    out = []
    dq = deque()
    for v in values:
        if isinstance(v, (int, float)):
            dq.append(v)
            if len(dq) > window:
                dq.popleft()
        out.append(_median(list(dq)) if dq else None)
    return out


def _is_data_row(r):
    """判断是否为采样点（排除 meta/target_switch 等 event 行）。"""
    return isinstance(r, dict) and not r.get("event")


def check_row_health(row):
    """单采样点校验，返回该点的问题列表（["内存解析异常（RSS<PSS）", ...]）。

    只做物理约束这类"单点即可判定"的规则；段级规则在 scan_rows 里做。
    """
    issues = []
    if not _is_data_row(row):
        return issues
    m = row.get("mem") or {}
    pss = m.get("pss_kb")
    rss = m.get("vmrss_kb")
    if pss is not None and rss is not None and pss > 0:
        # RSS 应 ≥ PSS（共享内存 RSS 全计、PSS 按比例摊）。缺口超过 1% 才判异常，
        # 否则是 smaps_rollup 舍入/时序噪声（实测异常样本缺口中位仅 0.41%）。
        if rss < pss and (pss - rss) > pss * RSS_LT_PSS_MIN_RATIO:
            issues.append("内存解析异常（RSS<PSS）")
    return issues


def scan_rows(rows):
    """整份数据扫描，返回异常清单（list[dict]）。

    每项：{type, level, message, start_t_ms, end_t_ms, count}。
    只对段级规则做（采错进程/假 Jank/量级突变/连续缺失）；单点物理约束按
    "违反占比"聚合成一个 issue（避免每个点一条刷屏）。
    """
    data = [r for r in (rows or []) if _is_data_row(r)]
    if not data:
        return []

    issues = []

    # ---- 1) 采错进程：渲染中但进程 CPU 几乎为 0 ----
    fps_vals = [(r.get("fps") or {}).get("fps") for r in data]
    cpu_vals = [(r.get("cpu") or {}).get("cpu_proc_pct") for r in data]
    med_fps = _median(fps_vals)
    n_cpu = sum(1 for v in cpu_vals if isinstance(v, (int, float)))
    n_near_zero = sum(1 for v in cpu_vals if isinstance(v, (int, float)) and v < WRONG_PROC_CPU_NEAR_ZERO)
    if (med_fps is not None and med_fps >= WRONG_PROC_MIN_FPS
            and n_cpu >= WRONG_PROC_MIN_POINTS
            and n_cpu > 0 and n_near_zero / n_cpu >= WRONG_PROC_FRACTION):
        issues.append({
            "type": "wrong_process",
            "level": "high",
            "message": "疑似采错进程（渲染中 FPS 正常，但进程 CPU 近乎为 0）",
            "start_t_ms": data[0].get("t_ms"),
            "end_t_ms": data[-1].get("t_ms"),
            "count": n_cpu,
        })

    # ---- 2) 假 Jank：jank 中位高 + fps 中位高 + 帧时间未明显升高 ----
    jank_vals = [(r.get("fps") or {}).get("jank_rate") for r in data]
    p50_vals = [(r.get("fps") or {}).get("frame_p50_ms") for r in data]
    med_jank = _median(jank_vals)
    med_p50 = _median(p50_vals)
    if (med_jank is not None and med_jank > FAKE_JANK_RATE
            and med_fps is not None and med_fps >= FAKE_JANK_MIN_FPS
            and med_p50 is not None and 0 < med_p50 < FAKE_JANK_P50_ABS_MS):
        # Jank 中位高但 FPS 中位高且帧时间中位仍在正常范围 → 更像阈值口径问题而非真卡顿
        # （真卡顿时 frame_p50 会明显变大）。
        issues.append({
            "type": "fake_jank",
            "level": "medium",
            "message": "疑似 Jank 阈值口径问题（Jank 高但帧率正常且帧时间未升高，核对刷新率/节奏）",
            "start_t_ms": data[0].get("t_ms"),
            "end_t_ms": data[-1].get("t_ms"),
            "count": len(data),
        })

    # ---- 3) 内存量级突变：PSS 相对滚动中位偏离 >5× 或 <0.2×，连续多点上报告 ----
    pss_vals = [(r.get("mem") or {}).get("pss_kb") for r in data]
    roll = _rolling_median_window(pss_vals, PSS_ROLLING_WINDOW)
    streak = 0
    seg_start = None
    for i, (r, v, med) in enumerate(zip(data, pss_vals, roll)):
        if isinstance(v, (int, float)) and isinstance(med, (int, float)) and med > 0:
            ratio = v / med
            bad = ratio > PSS_JUMP_UP or ratio < PSS_JUMP_DOWN
        else:
            bad = False
        if bad:
            if streak == 0:
                seg_start = r.get("t_ms")
            streak += 1
        else:
            if streak >= PSS_MIN_RATIO_POINTS:
                issues.append({
                    "type": "pss_jump",
                    "level": "medium",
                    "message": "内存量级突变（PSS 相对滚动中位偏离 >5× 或 <0.2×）",
                    "start_t_ms": seg_start,
                    "end_t_ms": data[i - 1].get("t_ms"),
                    "count": streak,
                })
            streak = 0
            seg_start = None
    if streak >= PSS_MIN_RATIO_POINTS:
        issues.append({
            "type": "pss_jump",
            "level": "medium",
            "message": "内存量级突变（PSS 相对滚动中位偏离 >5× 或 <0.2×）",
            "start_t_ms": seg_start,
            "end_t_ms": data[-1].get("t_ms"),
            "count": streak,
        })

    # ---- 4) 连续缺失：某关键指标连续 N 点 None ----
    for metric, getter in KEY_METRICS.items():
        run = 0
        run_start = None
        for r in data:
            if getter(r) is None:
                if run == 0:
                    run_start = r.get("t_ms")
                run += 1
            else:
                if run >= MISSING_RUN:
                    issues.append({
                        "type": "missing_metric",
                        "level": "medium",
                        "message": f"{metric} 指标连续缺失（{run} 个采样点）",
                        "start_t_ms": run_start,
                        "end_t_ms": r.get("t_ms"),
                        "count": run,
                    })
                run = 0
                run_start = None
        if run >= MISSING_RUN:
            issues.append({
                "type": "missing_metric",
                "level": "medium",
                "message": f"{metric} 指标连续缺失（{run} 个采样点）",
                "start_t_ms": run_start,
                "end_t_ms": data[-1].get("t_ms"),
                "count": run,
            })

    # ---- 5) 单点物理约束聚合：RSS<PSS 违反占比 ----
    viol = 0
    n_mem = 0
    for r in data:
        m = r.get("mem") or {}
        pss = m.get("pss_kb")
        rss = m.get("vmrss_kb")
        if pss is not None and rss is not None:
            n_mem += 1
            if rss < pss and (pss - rss) > pss * RSS_LT_PSS_MIN_RATIO:
                viol += 1
    if n_mem > 0 and viol / n_mem >= 0.1:
        issues.append({
            "type": "rss_lt_pss",
            "level": "medium",
            "message": f"内存解析异常（RSS<PSS 占比 {viol}/{n_mem}）",
            "start_t_ms": data[0].get("t_ms"),
            "end_t_ms": data[-1].get("t_ms"),
            "count": viol,
        })

    return issues


def health_summary(issues):
    """生成可读中文摘要（供报告标注/控制台告警）。

    无异常返回空字符串；有异常返回如"⚠️ 疑似采错进程（渲染中但进程 CPU 近乎为 0）"。
    """
    if not issues:
        return ""
    parts = []
    for it in issues:
        start = it.get("start_t_ms")
        end = it.get("end_t_ms")
        seg = ""
        if start is not None and end is not None:
            seg = f"（{round(start / 1000, 1)}s~{round(end / 1000, 1)}s）"
        parts.append(f"{it['message']}{seg}")
    return "；".join(parts)


def check_rows_live(row, streak_state):
    """实时轻量自检：对单个采样点跑关键规则，维护连续命中计数。

    streak_state: dict，调用方持有（跨轮累积）。返回 (alerts, streak_state)。
    只跑 a（RSS<PSS）与 b（采错进程线索），单点开销极小。
    """
    alerts = []
    row_issues = check_row_health(row)
    if row_issues:
        streak_state["rss"] = streak_state.get("rss", 0) + 1
        if streak_state["rss"] >= 5:
            alerts.append(row_issues[0])
    else:
        streak_state["rss"] = 0

    fps_v = row.get("fps") or {}
    cpu_v = row.get("cpu") or {}
    fps = fps_v.get("fps")
    cpu = cpu_v.get("cpu_proc_pct")
    if (fps is not None and fps >= WRONG_PROC_MIN_FPS
            and cpu is not None and cpu < WRONG_PROC_CPU_NEAR_ZERO):
        streak_state["cpu_near_zero"] = streak_state.get("cpu_near_zero", 0) + 1
    else:
        streak_state["cpu_near_zero"] = 0
    if streak_state["cpu_near_zero"] >= 10:
        alerts.append("疑似采错进程（渲染中 FPS 正常但进程 CPU 近乎为 0）")
        streak_state["cpu_near_zero"] = 0   # 已告警，重置防刷屏

    return alerts, streak_state
