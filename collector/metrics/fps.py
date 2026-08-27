# -*- coding: utf-8 -*-
"""FPS 采集（SurfaceFlinger 方案，2026-08-12 真机确认）

⚠️ 背景：微信小游戏 WebGL 渲染走独立渲染线程 + SurfaceView，
`dumpsys gfxinfo` 采不到帧数据（实测 total_frames 恒 0）。
改用 SurfaceFlinger 帧统计：

  1) `dumpsys SurfaceFlinger --list` 匹配小游戏的 SurfaceView layer
     注意：荣耀 ADT-AN00 (Android 14) 实测必须带 `#id` 完整层名查询
     （不带 #id 返回 0 帧），因此 layer 名保留 #id；#id 随层重建变化，
     读取失败时自动重匹配（见容错逻辑）
  2) `dumpsys SurfaceFlinger --latency <layer>` 读帧时间戳（滚动缓冲，约 128 槽）
     格式：首行刷新周期(ns)；后续每行三列
       desiredPresentTime  actualPresentTime  frameReadyTime
     （部分设备单列，兼容处理）；帧时间戳取第 2 列
  3) 数据清洗：0 = 空槽位；实测此设备缓冲末位常驻哨兵值
     INT64_MAX (9223372036854775807)，必须过滤，否则 span 会算出天文数字
  4) FPS = (帧数-1) ÷ (首末帧时间戳跨度) —— 基于缓冲内时间戳直接算，
     对滚动缓冲/满槽稳定（增量法在缓冲满时会恒 0）
  5) 新鲜度：比较缓冲最新帧时间戳是否推进——持续渲染则单调前移，静止/暂停不变
     → FPS 归零（不需要 --latency-clear，避免 clear 后窗口内无帧导致的间歇性 0）
  6) Jank 率 = 相邻帧间隔 > 2×刷新周期 的占比（PerfDog 同口径，
     随刷新率自适应：60Hz→33.3ms、90Hz→22.2ms、120Hz→16.7ms）
  7) 百分位/Jank 只算本次新增帧（2026-08-21）：128 槽缓冲 ~2.13s 数据窗 > 0.5s 采样
     间隔，若对全缓冲 gaps 计算，一次 2–3.6s 的卡死帧会留缓冲 30–60s，期间每个采样点
     的 P95/Max/Jank 被重复污染 → 只对 ts > 上次缓冲最大时间戳的新增帧算 gaps

容错冗余（2026-08-12 自查优化）：
  - 游戏不在前台 → 渲染层销毁 → 报 no_layer，5s 节流重试 --list
  - --latency 读取失败 → 层已失效，立即重匹配
  - 0 帧/静止 = 游戏确实没在渲染，是合法结果，不误换层
"""

import math
import re

# 层不存在时，两次 --list 重试的最小间隔（秒）
DEFAULT_RETRY_INTERVAL = 5.0
# Jank 阈值 = 刷新周期的倍数（2×，PerfDog 口径）
JANK_MULTIPLIER = 2.0
# 单列格式的兜底刷新周期（ns，60Hz）——当首行解析失败时用
DEFAULT_REFRESH_NS = 16_666_666
# 缓冲内有效时间戳上限（ns，>3 年视为哨兵/异常值）：
# 实测荣耀设备缓冲末位常驻 INT64_MAX 哨兵
MAX_VALID_TS = 10 ** 17

# ---- Jank 阈值节奏校准（2026-08-27 kimi 归因修复）----
# 现象：荣耀面板支持 60/90/120/144Hz，微信小游戏被平台锁 60fps；若 --latency
# 首行按面板当前 vsync 报 120Hz（8.33ms），阈值 = 2×8.33 = 16.67ms，而 60fps
# 帧间隔正好 ~16.7ms → 亚毫秒抖动就跨线 → 假 Jank 70-100%（实测 hz=120 段
# Jank 78.6% vs hz=60 段 3.9%）。
# 修复：新帧 ≥MIN 时用 gaps 中位数作"实际呈现节奏"，吸附最近标准 vsync 档
# （10% 容差），阈值 = 2×节奏×1.1；新帧不足回退 refresh_ns 口径。
_VSYNC_STANDARDS_MS = (16.6667, 11.1111, 8.3333, 6.9444)  # 60/90/120/144Hz
JANK_RHYTHM_TOLERANCE = 1.1      # 阈值 = 2×节奏×1.1（亚毫秒抖动容差）
JANK_RHYTHM_SNAP = 0.10          # 吸附标准档的容差（10%）
MIN_FRAMES_FOR_RHYTHM = 8        # 新帧数下限，不足回退 refresh_ns 口径


class FpsCollector:
    def __init__(self, adb, package, process_pattern="appbrand",
                 retry_interval=DEFAULT_RETRY_INTERVAL):
        self.adb = adb
        self.package = package
        self.process_pattern = process_pattern
        self.retry_interval = retry_interval
        self.layer = None
        self.refresh_ns = DEFAULT_REFRESH_NS
        self._next_resolve = 0.0
        self._last_max_ts = None   # 上次缓冲最新帧时间戳（用于静止判断）
        self._last_seen_ts = None  # 上次缓冲最大有效帧时间戳（用于"只算新帧"）
        # 最近一次由新帧算出的帧时间分布（无新帧时沿用；层切换时随基准一并重置）
        self._last_frame_stats = None
        # FPS 双通道（2026-08-13 扩展支持任意 App）：
        #   sf  = SurfaceFlinger --latency（微信小游戏 SurfaceView 层）
        #   gfx = dumpsys gfxinfo 增量（普通 View 应用——SF 窗口层无帧统计）
        self.mode = "sf"
        self.layer_is_surfaceview = False
        self._gfx_inited = False
        self._gfx_last = None   # (ts, total, janky)
        # 是否匹配到过 SurfaceView 层（小游戏/视频类）。若之前有过，说明这是 SurfaceView
        # 应用，渲染层只是暂时丢失（重建/弹窗）→ 保留 sf 通道重匹配，绝不切 gfxinfo
        # （gfxinfo 对 WebGL 恒 0 帧，切过去 FPS 会永久归零，2026-08-21 真机 673s 后暴露）
        self._ever_surfaceview = False
        self._gfx_zero_streak = 0   # gfxinfo 连续 0 帧计数（兜底回退 sf）

    def resolve_layer(self):
        """匹配目标应用渲染层，返回含 #id 的完整层名（荣耀 Android14 必须带 #id）。

        优先级：
          1) SurfaceView[...](BLAST)  —— SurfaceView 渲染（小游戏/视频类，帧统计最准）
          2) SurfaceView[...]         —— 普通 SurfaceView
          3) 应用窗口层 com.pkg/...Activity#id —— 普通 View 渲染的应用（无 SurfaceView）
        按包名/进程模式匹配；找不到返回 None。支持任意 App（2026-08-13 扩展）。
        """
        try:
            out = self.adb.shell(["dumpsys", "SurfaceFlinger", "--list"])
        except Exception:
            return None

        def _hit(raw):
            if self.package and self.package not in raw:
                return False
            if self.process_pattern and self.process_pattern not in raw:
                return False
            return bool(self.package or self.process_pattern)

        blast, normal, window = [], [], []
        for line in out.splitlines():
            raw = line.strip()
            if not raw:
                continue
            if not _hit(raw):
                continue
            if raw.startswith("SurfaceView["):
                if "(BLAST)" in raw:
                    blast.append(raw)
                else:
                    normal.append(raw)
                continue
            # 窗口层兜底：跳过容器/装饰层，只留应用窗口层
            skip = ("ActivityRecord{", "Input ", "Dim layer", "Wallpaper",
                    "Background for ", "Bounds for ", "Ime", "StatusBar",
                    "NavigationBar", "Gesture", "Display Overlays", "RoundCorner")
            if any(raw.startswith(p) for p in skip):
                continue
            window.append(raw)
        # 优先 BLAST，其次普通 SurfaceView，最后窗口层（普通 View 应用）
        return (blast or normal or window or [None])[0]

    def _reset_frame_baseline(self):
        """清空与"当前层缓冲"绑定的帧统计基准（层重建后旧基准全部失效）。

        只动帧统计三件套，不碰 mode / _ever_surfaceview / _gfx_* ——
        "SF 层短暂丢失时保留 sf 通道重匹配"的记忆逻辑依赖后者。
        """
        self._last_max_ts = None
        self._last_seen_ts = None
        self._last_frame_stats = None

    def _set_layer(self, layer):
        """记录当前层及其类型（SurfaceView 层才有 SF 帧统计）。

        层名变化（切场景/游戏重启导致渲染层重建，#id 随之改变；或读取失败置空）
        时重置帧统计基准（2026-08-25 修复跨层残留）：
          - _last_frame_stats：新层前两个采样点若新帧不足 2 个，会沿用**旧层**的
            P50/P95/Max 读数，曲线上表现为切场景后仍显示上一场景的帧时间
          - _last_seen_ts / _last_max_ts：新层时间戳与旧层不连续，续用旧基准会把
            新层的帧误判为"非新增帧"（漏算 Jank）或"未推进"（FPS 误判为 0/stale）
        重置后首个采样点按"首轮"处理：整个缓冲都算新帧，直接出该层自己的统计。
        """
        if layer != self.layer:
            self._reset_frame_baseline()
        self.layer = layer
        self.layer_is_surfaceview = bool(layer and layer.startswith("SurfaceView["))
        if self.layer_is_surfaceview:
            self._ever_surfaceview = True   # 记住：这是 SurfaceView 应用
        return layer

    def _try_resolve(self, ts):
        if ts >= self._next_resolve:
            self._set_layer(self.resolve_layer())
            self._next_resolve = ts + self.retry_interval
            return True
        return False

    # ---------------- gfxinfo 通道（普通 View 应用） ----------------
    @staticmethod
    def _parse_gfxinfo(out):
        """解析 gfxinfo 汇总帧数。返回 (total_frames, janky_frames) 或 None。"""
        total = janky = None
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Total frames rendered"):
                m = re.search(r"(\d+)", s)
                if m:
                    total = int(m.group(1))
            elif s.startswith("Janky frames"):
                m = re.search(r"(\d+)", s)
                if m:
                    janky = int(m.group(1))
        return (total, (janky or 0)) if total is not None else None

    def _gfx_read(self):
        """读目标包 gfxinfo 汇总。失败/无数据返回 None。"""
        try:
            out = self.adb.shell(["dumpsys", "gfxinfo", self.package])
        except Exception:
            return None
        return self._parse_gfxinfo(out)

    def _switch_to_gfx(self, ts):
        """切到 gfx 通道（首轮执行 reset）。返回 True 表示切换成功。"""
        self.mode = "gfx"
        self._gfx_inited = False
        self._gfx_last = None
        self._sample_gfx(ts)
        return True

    def _sample_gfx(self, ts):
        if not self._gfx_inited:
            try:
                self.adb.shell(["dumpsys", "gfxinfo", self.package, "reset"])
            except Exception:
                pass
            self._gfx_inited = True
            self._gfx_last = None

        cur = self._gfx_read()
        if cur is None:
            # gfxinfo 无数据（如微信 WebGL）→ 回退 SurfaceFlinger 通道
            self.mode = "sf"
            self._gfx_inited = False
            self._gfx_last = None
            return {"layer": None, "total_frames": None, "fps": None,
                    "jank_rate": None, "error": "gfx_unavailable",
                    "hint": "gfxinfo 无数据，回退 SurfaceFlinger"}

        total, janky = cur
        # 兜底：gfxinfo 连续 3 次 0 帧（WebGL 应用 reset 后永远无帧统计）→ 回退 sf，
        # 防止任何路径误切到 gfx 后 FPS 永久归零（2026-08-21 真机 673s 后 FPS=0 修复）
        if total == 0:
            self._gfx_zero_streak += 1
            if self._gfx_zero_streak >= 3:
                self.mode = "sf"
                self._gfx_inited = False
                self._gfx_last = None
                self._gfx_zero_streak = 0
                return {"layer": None, "total_frames": None, "fps": None,
                        "jank_rate": None, "error": "gfx_unavailable",
                        "hint": "gfxinfo 不统计该应用，回退 SurfaceFlinger"}
        else:
            self._gfx_zero_streak = 0
        result = {"layer": self.layer, "total_frames": total, "fps": None,
                  "jank_rate": None, "refresh_hz": None, "source": "gfxinfo"}
        if self._gfx_last is not None:
            lt, ltotal, ljanky = self._gfx_last
            dt = ts - lt
            if dt > 0:
                df = total - ltotal
                if df > 0:
                    result["fps"] = round(df / dt, 2)
                    result["jank_rate"] = round(max(janky - ljanky, 0) / df, 4)
                elif df == 0:
                    result["fps"] = 0.0   # 静止：无新帧
        self._gfx_last = (ts, total, janky)
        return result

    @staticmethod
    def _parse_latency(out):
        """解析 --latency 输出。

        返回 (refresh_ns, frame_timestamps)。帧时间戳取第二列（actualPresentTime），
        单列格式直接取值；0 与哨兵值（>3 年）丢弃。
        """
        lines = out.splitlines()
        refresh = DEFAULT_REFRESH_NS
        timestamps = []
        for i, line in enumerate(lines):
            s = line.strip()
            if not s:
                continue
            if i == 0:
                m = re.match(r"^(\d+)$", s)
                if m:
                    v = int(m.group(1))
                    if v > 0:
                        refresh = v
                continue
            if "\t" in s:
                cols = s.split("\t")
                t = cols[1] if len(cols) >= 2 else cols[0]
            elif " " in s:
                cols = s.split()
                t = cols[1] if len(cols) >= 2 else cols[0]
            else:
                t = s
            if t.isdigit():
                v = int(t)
                if 0 < v <= MAX_VALID_TS:
                    timestamps.append(v)
        return refresh, timestamps

    def _jank_threshold_ns(self, new_ts):
        """按实际呈现节奏校准 Jank 阈值（2026-08-27 修复假 Jank）。

        新帧 ≥MIN_FRAMES_FOR_RHYTHM 时：gaps 中位数 = 实际渲染节奏，吸附最近
        标准 vsync 档（60/90/120/144Hz，10% 容差内才吸附，防真高刷被错吸到
        低档），阈值 = 2×节奏×1.1。新帧不足回退 self.refresh_ns×2（现有口径）。
        返回阈值（ns）。
        """
        if len(new_ts) < MIN_FRAMES_FOR_RHYTHM:
            return self.refresh_ns * JANK_MULTIPLIER
        gaps_ms = sorted((new_ts[i] - new_ts[i - 1]) / 1e6
                         for i in range(1, len(new_ts)))
        med = gaps_ms[len(gaps_ms) // 2]
        rhythm = med
        for std in _VSYNC_STANDARDS_MS:
            if abs(med - std) / std <= JANK_RHYTHM_SNAP:
                rhythm = std
                break
        return rhythm * JANK_MULTIPLIER * JANK_RHYTHM_TOLERANCE * 1e6  # ns

    def sample(self, ts):
        # gfx 通道（普通 View 应用）优先走增量
        if self.mode == "gfx":
            return self._sample_gfx(ts)

        if not self.layer:
            self._try_resolve(ts)
            if not self.layer:
                # 之前匹配到过 SurfaceView 层（微信小游戏等）→ 层只是暂时丢失（重建/切场），
                # 保留 sf 通道等 5s 重匹配找回；此时切 gfxinfo 会因 WebGL 恒 0 帧让 FPS 永久归零
                if self._ever_surfaceview:
                    return {"layer": None, "total_frames": None, "fps": None,
                            "jank_rate": None, "error": "no_layer",
                            "hint": "渲染层暂失,重匹配中"}
                # 从未有 SurfaceView 层：可能是普通 View 应用 → 试 gfxinfo
                if self._gfx_read() is not None:
                    self._switch_to_gfx(ts)
                    return self._sample_gfx(ts)
                return {"layer": None, "total_frames": None, "fps": None,
                        "jank_rate": None, "error": "no_layer",
                        "hint": "应用未在前台或无渲染层"}

        try:
            out = self.adb.shell(["dumpsys", "SurfaceFlinger", "--latency", self.layer])
        except Exception as e:
            self._set_layer(None)
            self._try_resolve(ts)
            return {"layer": None, "total_frames": None, "fps": None,
                    "jank_rate": None, "error": "layer_read_fail", "detail": str(e)}

        refresh, timestamps = self._parse_latency(out)
        self.refresh_ns = refresh
        n = len(timestamps)

        # 关键：窗口层（非 SurfaceView）通常不提供 SF 帧统计 → 自动切 gfxinfo 通道
        # （微信小游戏是 SurfaceView 层，不走此分支，静止画面正常显示 0）
        if n == 0 and not self.layer_is_surfaceview:
            if self._gfx_read() is not None:
                self._switch_to_gfx(ts)
                return self._sample_gfx(ts)

        result = {"layer": self.layer, "total_frames": n, "fps": None, "jank_rate": None,
                  "refresh_hz": round(1e9 / self.refresh_ns, 1) if self.refresh_ns else None}

        # 新鲜度：以"缓冲最新帧时间戳是否推进"判断（帧时间戳与 uptime 基准不同，
        # 不能直接比较；持续渲染则最新帧单调前移，静止/暂停则不变）
        cur_max = timestamps[-1] if timestamps else None
        advancing = bool(cur_max is not None and self._last_max_ts is not None
                         and cur_max > self._last_max_ts)

        # FPS：基于缓冲内时间戳跨度（对滚动/满槽稳定）；静止时归零
        if n >= 2 and (self._last_max_ts is None or advancing):
            span_s = (timestamps[-1] - timestamps[0]) / 1e9
            if span_s > 0:
                result["fps"] = round((n - 1) / span_s, 2)
        else:
            result["fps"] = 0.0
            if n >= 2 and cur_max is not None and self._last_max_ts is not None \
                    and cur_max <= self._last_max_ts:
                result["stale"] = True
        self._last_max_ts = cur_max

        # Jank 率 / 帧时间百分位：只对本次新增帧计算（2026-08-21 修复缓冲残留污染）。
        # 新增帧 = ts > 上次缓冲最大时间戳 的条目；首轮（_last_seen_ts=None）取全缓冲。
        # FPS 值仍用全缓冲 span（保持滚动缓冲下的稳定性，不受此影响）。
        new_ts = timestamps if self._last_seen_ts is None \
            else [t for t in timestamps if t > self._last_seen_ts]
        if timestamps:
            self._last_seen_ts = timestamps[-1]
        jank_threshold = self._jank_threshold_ns(new_ts)   # 节奏校准阈值（2026-08-27）
        if len(new_ts) >= 2:
            gaps = [new_ts[i] - new_ts[i - 1] for i in range(1, len(new_ts))]
            over = sum(1 for g in gaps if g > jank_threshold)
            result["jank_rate"] = round(over / (len(new_ts) - 1), 4)
            # 帧时间分布（ms）：P50 / P95 / Max。P95 用 ceil(n*0.95)-1 取排序值，
            # 与前端统计栏口径统一（此前后端 int(n*0.95) 差一位次）
            sorted_gaps = sorted(gaps)
            g_ms = lambda v: round(v / 1e6, 2)
            idx95 = min(max(int(math.ceil(len(sorted_gaps) * 0.95)) - 1, 0), len(sorted_gaps) - 1)
            self._last_frame_stats = {
                "frame_p50_ms": g_ms(sorted_gaps[(len(sorted_gaps) - 1) // 2]),
                "frame_p95_ms": g_ms(sorted_gaps[idx95]),
                "frame_max_ms": g_ms(sorted_gaps[-1]),
            }
        if self._last_frame_stats is not None:
            # 无新帧（静止）或新帧不足 2 个 → 沿用最近一次新帧统计，曲线连续不跳变
            result.update(self._last_frame_stats)

        return result
