# -*- coding: utf-8 -*-
"""启动期"轻量探测"（2026-09-11）：列出微信候选实例、找游戏渲染层、给出推荐。

用户需求（2026-09-11）：
  - 双击启动后先做**轻微检测并展示**，用户**选定进程、点开始采集之前不记录任何数据**；
  - 探测到的数据只用于"选谁采"的选择与推荐；其他 App 的信息获取流程不动。

背景（真机实测 2026-09-11）：
  微信可同时存在 appbrand0/1/2 三个实例；"累计 CPU 时间最大"判据会偏向存在时间
  久的进程，而游戏实例可能切换 → 自动选进程会静默产出错数据（PSS 231MB vs 1035MB）。
  因此把选择权交给用户，探测只负责"给足信息 + 给一个推荐"。

设计：纯解析函数（可单测）+ 少量 adb 往返（探测不写任何采集输出）。
"""

import re

# "com.tencent.mm:appbrand1" → appbrand1
_APPBRAND_RE = re.compile(r":(appbrand\d+)\b")
_APPBRAND_IDX_RE = re.compile(r"(\d+)$")
# 游戏层名形如：SurfaceView[com.tencent.mm/...AppBrandUI1](BLAST)#18655
_LAYER_APPBRAND_RE = re.compile(r"AppBrandUI(\d+)", re.I)
# 输入事件层/系统手势层：必须排除（选了会导致 FPS 恒 0）
_LAYER_SKIP_KEYWORDS = ("ActivityRecordInputSink", "GestureNav", "Input ", "InputMethod",
                        "Bounds for", "Background for", "Dim Layer", "StatusBar",
                        "NavigationBar", "ScreenDecorOverlay", "Sprite")


def appbrand_index(name):
    """"com.tencent.mm:appbrand1" → 1；无 appbrand 段或末尾非数字 → None。"""
    m = _APPBRAND_RE.search(name or "")
    if not m:
        return None
    m2 = _APPBRAND_IDX_RE.search(m.group(1))
    return int(m2.group(1)) if m2 else None


def layer_appbrand_index(layer_name):
    """层名里的 AppBrandUI(n) → n；无 → None。"""
    m = _LAYER_APPBRAND_RE.search(layer_name or "")
    return int(m.group(1)) if m else None


def parse_ps_candidates(ps_output, pattern="appbrand"):
    """解析 `ps -A -o PID,ARGS` 输出 → [(pid, name, index)]，按 index 升序（None 排最后）。

    toybox ps 输出行形如：` 11295 com.tencent.mm:appbrand0`；
    表头（PID/ARGS 或 USER/PID…）与解析失败行自动跳过。
    """
    out, seen = [], set()
    for line in (ps_output or "").splitlines():
        s = line.strip()
        if not s:
            continue
        head = s.split(None, 1)
        if len(head) < 2 or not head[0].isdigit():
            continue          # 表头或异常行
        pid, name = int(head[0]), head[1].strip()
        if pid in seen:
            continue
        if pattern and pattern not in name:
            continue
        seen.add(pid)
        out.append((pid, name, appbrand_index(name)))
    out.sort(key=lambda c: (c[2] is None, c[2] if c[2] is not None else 0))
    return out


def pick_game_layer(list_output):
    """从 `dumpsys SurfaceFlinger --list` 输出挑游戏渲染层。

    规则（真机验证过顺序）：
      1) 含 AppBrandUI 的 SurfaceView（BLAST 优先）——这是小游戏游戏的渲染层；
      2) 退一步：任意含 AppBrandUI 的 SurfaceView；
      3) 再退：任意非跳过关键字的 SurfaceView（(BLAST) 优先）。
    返回 (layer_name, appbrand_index|None)；找不到返回 (None, None)。
    """
    cands = []
    for line in (list_output or "").splitlines():
        s = line.strip()
        if not s or "SurfaceView[" not in s:
            continue
        if any(k in s for k in _LAYER_SKIP_KEYWORDS):
            continue
        cands.append(s)

    def rank(name):
        idx = layer_appbrand_index(name)
        return (0 if idx is not None else 1,
                0 if "(BLAST)" in name else 1,
                0 if "AppBrandUI" in name else 1)

    if not cands:
        return None, None
    best = sorted(cands, key=rank)[0]
    return best, layer_appbrand_index(best)


def parse_stat_ticks(stat_line):
    """`/proc/<pid>/stat` 单行 → utime+stime（ticks）；解析失败 None。

    注意 comm 可能含空格/括号（如 "(nt.mm:appbrand0)"），故以**最后一个 ")"**
    之后开始数字段；utime/stime 是剩余字段的第 12/13 个（1-based：14/15）。
    """
    if not stat_line or ")" not in stat_line:
        return None
    try:
        after = stat_line[stat_line.rfind(")") + 1:].split()
        return int(after[11]) + int(after[12])
    except (ValueError, IndexError):
        return None


def parse_vmrss_kb(grep_output, pid):
    """从 `grep VmRSS /proc/<pid>/status`（可能多文件）输出取指定 pid 的 VmRSS(KB)。

    多文件 grep 输出形如：`/proc/11295/status:VmRSS:\t 211380 kB`；
    单文件（无前缀）也可解析。解析失败 None。
    """
    for line in (grep_output or "").splitlines():
        s = line.strip()
        if not s:
            continue
        target = s
        if ":" in s and s.startswith("/proc/"):
            path, _, rest = s.partition(":")
            if str(pid) not in path:
                continue
            target = rest
        m = re.search(r"VmRSS:\s*(\d+)\s*kB", target)
        if m:
            return int(m.group(1))
    return None


def recommend(candidates, delta_ticks, layer_index=None):
    """给出推荐 (pid, reason, detail)。

    优先级：
      1) 与游戏层 AppBrandUI(n) 索引匹配的候选（最可靠：层就是游戏在渲染的证据）；
      2) 探测窗口内**增量 CPU 最大**（比累计更能反映"此刻谁在跑"）；
      3) 都没有时取累计 CPU 最大；再不行取第一个候选。
    candidates: [(pid, name, index)]；delta_ticks: {pid: 增量ticks}；layer_index: int|None
    返回 (pid, reason, detail_text)；无候选返回 (None, None, "")。
    """
    if not candidates:
        return None, None, ""
    if layer_index is not None:
        hit = [c for c in candidates if c[2] == layer_index]
        if hit:
            c = hit[0]
            return c[0], "layer_match", f"与游戏渲染层 AppBrandUI{layer_index} 匹配"
    if delta_ticks:
        ranked = sorted(candidates, key=lambda c: -delta_ticks.get(c[0], -1))
        best = ranked[0]
        if delta_ticks.get(best[0], 0) > 0:
            return best[0], "busy_cpu", f"探测窗口内 CPU 增量最大（{delta_ticks[best[0]]} ticks）"
        return best[0], "busy_cpu", "各候选探测窗口内均无 CPU 增量，取第一候选"
    c = candidates[0]
    return c[0], "first", "无增量数据，取第一候选"


def probe_once(adb, pattern="appbrand", sample_gap=0.8):
    """执行一次轻量探测（供 web /api/candidates 与启动向导调用）。

    返回 dict：{ok, error, candidates:[{pid,name,index,cpu_delta_ticks,cpu_delta_pct,
    rss_mb,recommended,reason,detail}], layer:{name,appbrand_index}, recommended_pid}
    —— 探测**不创建任何采集输出文件**，仅只读 adb。
    """
    try:
        ps_out = adb.shell(["ps", "-A", "-o", "PID,ARGS"])
    except Exception:
        try:
            ps_out = adb.shell(["ps", "-A"])
        except Exception as e:
            return {"ok": False, "error": f"读取进程列表失败: {e}", "candidates": []}
    cands = parse_ps_candidates(ps_out, pattern)
    if not cands:
        return {"ok": False,
                "error": "未找到匹配进程（请先在手机上打开被测小游戏并保持前台）",
                "candidates": [], "layer": {"name": None, "appbrand_index": None}}

    # 层探测（用于推荐与展示）
    layer_name, layer_idx = None, None
    try:
        layer_name, layer_idx = pick_game_layer(
            adb.shell(["dumpsys", "SurfaceFlinger", "--list"]))
    except Exception:
        pass

    pids = [c[0] for c in cands]

    def _ticks_snapshot():
        try:
            out = adb.shell(["cat"] + [f"/proc/{p}/stat" for p in pids])
        except Exception:
            return {}
        got = {}
        for line in (out or "").splitlines():
            t = parse_stat_ticks(line)
            if t is None:
                continue
            head = line.split("(", 1)[0].strip()
            if head.isdigit():
                got[int(head)] = t
        return got

    first = _ticks_snapshot()
    if sample_gap:
        import time as _t
        _t.sleep(sample_gap)
    second = _ticks_snapshot()
    delta = {}
    for p in pids:
        if p in first and p in second and second[p] >= first[p]:
            delta[p] = second[p] - first[p]

    # 内存（轻量：VmRSS；smaps_rollup 在部分 ROM 被 SELinux 拒）
    rss = {}
    try:
        out = adb.shell(["grep", "VmRSS"] + [f"/proc/{p}/status" for p in pids])
        for p in pids:
            v = parse_vmrss_kb(out, p)
            if v is not None:
                rss[p] = v
    except Exception:
        pass

    rec_pid, reason, detail = recommend(cands, delta, layer_idx)
    items = []
    for pid, name, idx in cands:
        items.append({
            "pid": pid,
            "name": name,
            "index": idx,
            "cpu_delta_ticks": delta.get(pid),
            "cpu_delta_pct": round(delta[pid] / 100.0 / sample_gap * 100.0, 1) if delta.get(pid) is not None and sample_gap else None,
            "rss_mb": round(rss[pid] / 1024.0, 1) if pid in rss else None,
            "recommended": pid == rec_pid,
            "reason": reason if pid == rec_pid else None,
            "detail": detail if pid == rec_pid else None,
        })
    return {"ok": True, "error": None, "candidates": items,
            "layer": {"name": layer_name, "appbrand_index": layer_idx},
            "recommended_pid": rec_pid,
            "recommend_reason": reason,
            "recommend_detail": detail,
            "pattern": pattern}
