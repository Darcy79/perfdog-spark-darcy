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
  4) FPS = 按大 gap 切段后取帧数最多的主段，(帧数-1) ÷ (主段首末时间戳跨度)
     （2026-09-01）：OPPO 等机型 SF 缓冲 128 帧时间戳稀疏分布在约 35 分钟里，
     全缓冲首尾跨度会算出病态低值（实测显示 0.01）；连续缓冲（如荣耀）全缓冲
     即单段，结果不变。对滚动缓冲/满槽稳定（增量法在缓冲满时会恒 0）
  5) 新鲜度：比较缓冲最新帧时间戳是否推进——持续渲染则单调前移，静止/暂停不变
     → FPS 归零（不需要 --latency-clear，避免 clear 后窗口内无帧导致的间歇性 0）
  6) Jank 率 = 相邻帧间隔 > 阈值 的帧占比。阈值实际口径（2026-08-27 节奏校准后，
     2026-09-11 注释校正）：新帧 ≥8 时 阈值 = 2×节奏×1.1（节奏 = 新帧间隔中位数
     吸附最近标准 vsync 档，容差 10%）；新帧不足 8 回退 2×刷新周期。
     并非纯"2×刷新周期"口径，详见 _jank_threshold_ns 与常量区注释
  7) 百分位/Jank 只算本次新增帧（2026-08-21）：128 槽缓冲 ~2.13s 数据窗 > 0.5s 采样
     间隔，若对全缓冲 gaps 计算，一次 2–3.6s 的卡死帧会留缓冲 30–60s，期间每个采样点
     的 P95/Max/Jank 被重复污染 → 只对 ts > 上次缓冲最大时间戳的新增帧算 gaps

容错冗余（2026-08-12 自查优化）：
  - 游戏不在前台 → 渲染层销毁 → 报 no_layer，5s 节流重试 --list
  - --latency 读取失败 → 层已失效，立即重匹配
  - 0 帧/静止 = 游戏确实没在渲染，是合法结果，不误换层
  - --list 本身读失败/输出为空 → 报 probe_fail（2026-09-11 新增）：此前与
    no_layer 混用，主机 adb 链路瞬时失败被误报成"游戏不在前台"，
    事后完全不可判读（run 20260911_162353：51/63 点误报 no_layer，
    实际层与进程全程都在）
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

# ---- FPS 切段阈值（2026-09-01 OPPO 修复，供真机调参）----
# OPPO ded7a388 实测：SF --latency 缓冲的 128 帧时间戳**稀疏分布在约 35 分钟**里
# （相邻帧间隔中位仍 16.7ms，帧时间统计正常），全跨度 span 法算出
# 127/2108s ≈ 0.06 → FPS 显示病态低值 0.01。
# 修复：相邻帧间隔超过此阈值的处切段，取**帧数最多的主段**算 FPS：
#   - 荣耀等连续缓冲（128 帧 ≈ 2.13s）→ 单段，结果与修复前完全一致
#   - 真实卡顿（100–500ms gap）低于阈值不切段，主段含卡顿帧 → FPS 略降（合理）
#   - 暂停（>0.5s gap）切段，恢复后主段是新段 → FPS 反映当前节奏
#     （顺带修复"暂停恢复后 span 跨暂停间隙导致 FPS 偏低"）
# 0.5s ≈ 60fps 下缺 30 帧，远超"渲染节奏"尺度，不会误切正常抖动。
FPS_SEGMENT_GAP_NS = 500_000_000

# ---- FPS 物理上限钳制与低帧数告警（2026-09-11）----
# 主段仅少数帧时 span 极短，(帧数-1)/span 可输出非物理值（如 2 帧相隔 0.5ms
# → 2000 FPS）。上限 = 已知刷新率 ×FPS_CAP_REFRESH_FACTOR（帧率物理上界是
# 刷新率，留 50% 余量给 refresh_ns 上报偏低的机型）；刷新率未知/超出物理窗口
# （1..1000Hz）时兜底 FPS_CAP_FALLBACK_HZ。钳制发生时结果带 fps_clamped=True。
FPS_CAP_REFRESH_FACTOR = 1.5
FPS_CAP_FALLBACK_HZ = 240.0
# 主段帧数下限：低于此值 FPS 读数统计上不可靠（单帧抖动即可大幅改变结果），
# 结果带 fps_warn="low_frames"——数值仍输出（已受上限钳制保护），供前端/
# 健全性规则标注"低置信"。正常连续缓冲（≥8 帧）不受影响。
FPS_MIN_SEGMENT_FRAMES = 8
# 刷新周期物理窗口（ns）：正常面板在 1ms(1000Hz)..1s(1Hz) 之间。--latency
# 首行超出此窗口视为解析异常（哨兵/坏首行数字），不采用、回退默认 60Hz，
# 保证落盘 refresh_hz 恒为正数（下游 1000/hz 不会除零）。
FPS_REFRESH_NS_MIN = 1_000_000
FPS_REFRESH_NS_MAX = 1_000_000_000


def _main_segment(timestamps):
    """按相邻间隔 > FPS_SEGMENT_GAP_NS 切段，返回帧数最多的段（时间戳连续切片）。

    帧数并列时取靠后的段（更接近当前渲染节奏）。空输入返回 []。
    """
    if not timestamps:
        return []
    segments = []
    cur = [timestamps[0]]
    for t in timestamps[1:]:
        if t - cur[-1] > FPS_SEGMENT_GAP_NS:
            segments.append(cur)
            cur = [t]
        else:
            cur.append(t)
    segments.append(cur)
    best = segments[0]
    for seg in segments[1:]:
        if len(seg) >= len(best):      # >=：并列取靠后段
            best = seg
    return best


# ---- Jank 阈值节奏校准（2026-08-27 kimi 归因修复）----
# 现象：荣耀面板支持 60/90/120/144Hz，微信小游戏被平台锁 60fps；若 --latency
# 首行按面板当前 vsync 报 120Hz（8.33ms），阈值 = 2×8.33 = 16.67ms，而 60fps
# 帧间隔正好 ~16.7ms → 亚毫秒抖动就跨线 → 假 Jank 70-100%（实测 hz=120 段
# Jank 78.6% vs hz=60 段 3.9%）。
# 修复：新帧 ≥MIN 时用 gaps 中位数作"实际呈现节奏"，吸附最近标准 vsync 档
# （10% 容差），阈值 = 2×节奏×1.1；新帧不足回退 refresh_ns 口径。
# 口径声明（2026-09-11 校正，替代早先"PerfDog 同口径 2×"的表述）：
#   实际阈值 = 2×节奏×1.1（即 2.2×节奏）。1.1 是亚毫秒抖动容差（60fps 帧间隔
#   在 16.64~16.70ms 间抖动，硬取 2.0×16.67=33.33ms 会误伤 33.4ms 的正常帧）。
#   已知局限：节奏取新帧 gaps 中位数——若 0.5s 新帧窗口内卡顿帧占比 >50%，
#   中位数被卡顿间隔占据 → 阈值随卡顿自我抬升，Jank 率被系统性低估（此时
#   FPS 曲线会同步跳水，可交叉判读）；data_health 的 fake_jank 规则只覆盖
#   "Jank 虚高"反方向，不覆盖此低估方向。
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
        # 最近一次层探测的错误码（None=成功/未探测，"no_layer"/"probe_fail"），
        # 供 sample() 在无层时上报正确错误类型（2026-09-11 事故修复）
        self._last_probe_err = None

    def resolve_layer(self):
        """旧签名兼容：只返回层名（可能 None）。错误码区分见 resolve_layer_ex()。"""
        return self.resolve_layer_ex()[0]

    def resolve_layer_ex(self):
        """匹配目标应用渲染层，返回 (layer, err)。err 三值（2026-09-11 事故修复）：
          None         匹配成功（layer 非 None）
          "no_layer"   --list 执行成功但没有匹配层（应用不在前台/渲染层未创建）
          "probe_fail" --list 执行失败（异常）或输出为空（adb 链路抖动/设备半死）

        优先级：
          1) SurfaceView[...](BLAST)  —— SurfaceView 渲染（小游戏/视频类，帧统计最准）
          2) SurfaceView[...]         —— 普通 SurfaceView
          3) 应用窗口层 com.pkg/...Activity#id —— 普通 View 渲染的应用（无 SurfaceView）
        按包名/进程模式匹配；支持任意 App（2026-08-13 扩展）。
        """
        try:
            out = self.adb.shell(["dumpsys", "SurfaceFlinger", "--list"])
        except Exception:
            return None, "probe_fail"
        if not out or not out.strip():
            # --list 正常时永远有系统层输出；全空 = 链路异常而非"无渲染层"
            return None, "probe_fail"

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
            # 窗口层兜底：跳过容器/装饰层/输入层，只留应用窗口层
            skip_prefix = ("ActivityRecord{", "Input ", "Dim layer", "Wallpaper",
                           "Background for ", "Bounds for ", "Ime", "StatusBar",
                           "NavigationBar", "Gesture", "Display Overlays", "RoundCorner")
            # OPPO 特有输入事件层（hex id 前缀，如 "6f89759 ActivityRecordInputSink ..."），
            # 无帧统计，绝不能作为窗口层兜底（2026-08-27 OPPO 适配）
            skip_kw = ("ActivityRecordInputSink",)
            if any(raw.startswith(p) for p in skip_prefix) or any(k in raw for k in skip_kw):
                continue
            window.append(raw)
        # 优先 BLAST，其次普通 SurfaceView，最后窗口层（普通 View 应用）
        layer = (blast or normal or window or [None])[0]
        return layer, (None if layer else "no_layer")

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
        """节流重匹配渲染层。返回是否真正执行了探测。

        探测错误码记入 _last_probe_err（层找到时清 None），供 sample() 在
        无层时区分 no_layer / probe_fail 上报（2026-09-11 事故修复）。
        """
        if ts >= self._next_resolve:
            layer, err = self.resolve_layer_ex()
            self._last_probe_err = None if layer else (err or "no_layer")
            self._next_resolve = ts + self.retry_interval
            if layer:
                self._set_layer(layer)
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

        返回 (refresh_ns, frame_timestamps)。刷新周期取**首个非空行**（旧实现按
        "物理第 0 行"判定，输出带前导空行时刷新周期会被误当帧时间戳）；该行须为
        纯数字且落在物理窗口 [FPS_REFRESH_NS_MIN, FPS_REFRESH_NS_MAX] 才采用，
        保证落盘 refresh_hz 恒为正数。帧时间戳取第二列（actualPresentTime），
        单列格式直接取值；0 与哨兵值（>3 年）丢弃。
        """
        lines = out.splitlines()
        refresh = DEFAULT_REFRESH_NS
        timestamps = []
        first_seen = False
        for line in lines:
            s = line.strip()
            if not s:
                continue
            if not first_seen:
                first_seen = True
                m = re.match(r"^(\d+)$", s)
                if m:
                    # 首个非空行为纯数字 → 刷新周期行，不再当帧数据
                    v = int(m.group(1))
                    if FPS_REFRESH_NS_MIN <= v <= FPS_REFRESH_NS_MAX:
                        refresh = v
                    continue
                # 非纯数字（坏首行）→ 保持旧行为：按帧数据解析（通常被 isdigit 过滤）
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

    def _fps_cap(self):
        """FPS 物理上限（2026-09-11）：已知刷新率时 = 刷新率×FPS_CAP_REFRESH_FACTOR。

        帧率物理上界是刷新率；×1.5 的余量是给 refresh_ns 上报偏低的机型
        （如面板报 120Hz 档但实际逐帧节奏 60fps，此时 FPS 读数不会超过
        节奏值本身，不会触碰 180 上限）。刷新率超出物理窗口（1..1000Hz）
        时用 FPS_CAP_FALLBACK_HZ 兜底。
        """
        if self.refresh_ns:
            hz = 1e9 / self.refresh_ns
            if 1.0 <= hz <= 1000.0:
                return hz * FPS_CAP_REFRESH_FACTOR
        return FPS_CAP_FALLBACK_HZ

    def sample(self, ts):
        # gfx 通道（普通 View 应用）优先走增量
        if self.mode == "gfx":
            return self._sample_gfx(ts)

        if not self.layer:
            self._try_resolve(ts)
            if not self.layer:
                # 上报"读失败"与"真的没有层"的正确错误码（2026-09-11 事故修复）：
                # 主机 adb 链路抖动是 probe_fail，不得再误报 no_layer（应用不在前台）
                err = self._last_probe_err or "no_layer"
                hint = ("渲染层读取失败(链路抖动)" if err == "probe_fail" else None)
                # 之前匹配到过 SurfaceView 层（微信小游戏等）→ 层只是暂时丢失（重建/切场），
                # 保留 sf 通道等 5s 重匹配找回；此时切 gfxinfo 会因 WebGL 恒 0 帧让 FPS 永久归零
                if self._ever_surfaceview:
                    return {"layer": None, "total_frames": None, "fps": None,
                            "jank_rate": None, "error": err,
                            "hint": hint or "渲染层暂失,重匹配中"}
                # 从未有 SurfaceView 层：可能是普通 View 应用 → 试 gfxinfo
                if self._gfx_read() is not None:
                    self._switch_to_gfx(ts)
                    return self._sample_gfx(ts)
                return {"layer": None, "total_frames": None, "fps": None,
                        "jank_rate": None, "error": err,
                        "hint": hint or "应用未在前台或无渲染层"}

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
        # OPPO 适配（2026-08-27）：窗口层 total_frames 可能为 1（无统计意义），
        # 且游戏启动初期 SurfaceView 渲染层尚未创建时不能死等——先主动重匹配
        # （5s 节流内不动作），SurfaceView 出现即换层；仍无帧再试 gfxinfo。
        if n <= 1 and not self.layer_is_surfaceview:
            self._try_resolve(ts)
            if self.layer and self.layer_is_surfaceview:
                try:
                    out = self.adb.shell(["dumpsys", "SurfaceFlinger", "--latency", self.layer])
                    refresh, timestamps = self._parse_latency(out)
                    self.refresh_ns = refresh
                    n = len(timestamps)
                except Exception:
                    n = 0
            if n <= 1 and self._gfx_read() is not None:
                self._switch_to_gfx(ts)
                return self._sample_gfx(ts)

        result = {"layer": self.layer, "total_frames": n, "fps": None, "jank_rate": None,
                  "refresh_hz": round(1e9 / self.refresh_ns, 1) if self.refresh_ns else None}

        # 新鲜度：以"缓冲最新帧时间戳是否推进"判断（帧时间戳与 uptime 基准不同，
        # 不能直接比较；持续渲染则最新帧单调前移，静止/暂停则不变）
        cur_max = timestamps[-1] if timestamps else None
        advancing = bool(cur_max is not None and self._last_max_ts is not None
                         and cur_max > self._last_max_ts)

        # FPS：按大 gap 切段，取帧数最多的主段用跨度算（2026-09-01 OPPO 修复）。
        # 背景：OPPO 的 SF 缓冲 128 帧时间戳稀疏分布在约 35 分钟里，旧版全缓冲
        # 首尾跨度算出病态低值（127/2108s ≈ 0.06 → 显示 0.01）；荣耀等连续缓冲
        # 全缓冲即单段，行为与旧版一致。静止判断（advancing/stale）语义不变。
        #
        # 【口径说明 / 2026-09-11】FPS 与 Jank/P95 的时间窗不同，是有意保留的设计，
        # 勿"顺手统一"：
        #   - FPS 用全缓冲主段窗口（荣耀连续缓冲约 2.13s；OPPO 稀疏缓冲为主段
        #     跨度）——滚动缓冲下每个采样点都是一段平均节奏，读数稳定；
        #   - Jank/P50/P95 只用 0.5s 新增帧窗口——防止一次卡死帧在滚动缓冲里
        #     滞留 30-60s、期间重复污染每个采样点（2026-08-21 修复）。
        #   因此同一点位可能出现"FPS 正常但 Jank 偏高"：FPS 是均值（被非卡顿帧
        #   稀释），Jank 是短窗瞬时值。解读以持续段为准，勿用单点互相对质。
        if n >= 2 and (self._last_max_ts is None or advancing):
            main_seg = _main_segment(timestamps)
            if len(main_seg) >= 2:
                span_s = (main_seg[-1] - main_seg[0]) / 1e9
                if span_s > 0:
                    fps_raw = (len(main_seg) - 1) / span_s
                    cap = self._fps_cap()
                    if fps_raw > cap:
                        # 物理上限钳制（2026-09-11）：主段帧数极少时 span 极短，
                        # 裸算可输出非物理值（2 帧相隔 0.5ms → 2000 FPS）
                        result["fps"] = round(cap, 2)
                        result["fps_clamped"] = True
                    else:
                        result["fps"] = round(fps_raw, 2)
                    if len(main_seg) < FPS_MIN_SEGMENT_FRAMES:
                        # 主段帧数过少 → 读数统计上不可靠（低置信），数值仍落盘
                        result["fps_warn"] = "low_frames"
            else:
                result["fps"] = 0.0   # 缓冲内仅孤立帧，无可测渲染节奏
        else:
            result["fps"] = 0.0
            if n >= 2 and cur_max is not None and self._last_max_ts is not None \
                    and cur_max <= self._last_max_ts:
                result["stale"] = True
        self._last_max_ts = cur_max

        # Jank 率 / 帧时间百分位：只对本次新增帧计算（2026-08-21 修复缓冲残留污染）。
        # 新增帧 = ts > 上次缓冲最大时间戳 的条目；首轮（_last_seen_ts=None）取全缓冲。
        # 注意与上面 FPS 的口径差异（见上方"口径说明"注释）：这是有意的时间窗分工。
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
