# -*- coding: utf-8 -*-
"""解析器 golden test（2026-08-21 P2-11）。

覆盖高危解析器：fps _parse_latency（哨兵过滤/单列格式）、mem smaps_rollup/meminfo
（同源 PSS/RSS）、cpu 合并命令解析、thermal 温度单位物理校验。

运行（项目根目录）：
    uv run --no-project python -m unittest discover -s tests -v
或：
    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

# 注入 collector 目录到 sys.path（main.py 以 collector 为运行根）
_COLLECTOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "collector")
if _COLLECTOR not in sys.path:
    sys.path.insert(0, _COLLECTOR)

from metrics.fps import FpsCollector, MAX_VALID_TS, _main_segment, FPS_SEGMENT_GAP_NS
from metrics.mem import parse_smaps_rollup, parse_meminfo
from metrics.cpu import CpuCollector
from metrics.thermal import ThermalCollector
from pidresolver import PidResolver
from export_report import COLUMNS, flatten, extract_cores, data_rows


class MockAdb:
    """最小 adb 替身：按关键字匹配返回预设输出，记录调用。"""

    def __init__(self, responses):
        self.responses = responses          # {包含关键字: 返回文本}
        self.calls = []

    def shell(self, args):
        self.calls.append(list(args))
        joined = " ".join(args)
        for key, val in self.responses.items():
            if key in joined:
                return val
        raise AssertionError(f"未预期的 adb 调用: {args}")


class MockResolver:
    def __init__(self, pid):
        self.pid = pid

    def current_pid(self, ts=0.0):
        return self.pid


class TestFpsLatency(unittest.TestCase):
    """dumpsys SurfaceFlinger --latency 解析：哨兵过滤、单列格式、刷新周期。"""

    def test_parse_60hz_with_sentinel(self):
        out = "16666666\n"
        # 60Hz 帧序列（第二列 actualPresentTime 递增 16.67ms）+ 末尾 INT64_MAX 哨兵
        for i in range(5):
            base = 1_000_000_000 + i * 16_666_666
            out += f"{base}\t{base + 10}\t{base - 5}\n"
        out += f"0\t{MAX_VALID_TS + 1}\t0\n"      # 哨兵必须被过滤
        refresh, ts = FpsCollector._parse_latency(out)
        self.assertEqual(refresh, 16_666_666)
        self.assertEqual(len(ts), 5)
        self.assertTrue(all(t <= MAX_VALID_TS for t in ts))

    def test_parse_single_column(self):
        # 单列格式（部分设备无制表符三列）
        out = "16666666\n"
        for i in range(4):
            out += f"{1_000_000_000 + i * 16_666_666}\n"
        refresh, ts = FpsCollector._parse_latency(out)
        self.assertEqual(refresh, 16_666_666)
        self.assertEqual(len(ts), 4)

    def test_parse_filters_zero_and_bad_first_line(self):
        out = "not-a-number\n0\n100\n200\n"
        refresh, ts = FpsCollector._parse_latency(out)
        self.assertEqual(refresh, 16_666_666)   # 首行解析失败 → 兜底 60Hz
        self.assertEqual(ts, [100, 200])        # 0 被丢弃


class SfMockAdb:
    """SurfaceFlinger 替身：可切换"当前存在的层"与该层的 --latency 输出。

    旧层名再查 --latency 会抛错（真机上层重建后 #id 变化即读取失败），
    用于驱动 FpsCollector 的层重匹配路径。
    """

    def __init__(self, layer, latency=""):
        self.layer = layer          # 设备上当前存在的层（None = 层已销毁）
        self.latency = latency      # 该层 --latency 输出
        self.calls = []

    def shell(self, args):
        self.calls.append(list(args))
        joined = " ".join(args)
        if "--list" in joined:
            return (self.layer or "") + "\n"
        if "--latency" in joined:
            if args[-1] != self.layer:
                raise RuntimeError("layer not found")   # 层已重建/销毁
            return self.latency
        raise AssertionError(f"未预期的 adb 调用: {args}")


def sf_latency(base_ns, count, step_ns):
    """构造 --latency 输出（首行刷新周期 + 三列帧时间戳，60Hz）。"""
    out = "16666666\n"
    for i in range(count):
        t = base_ns + i * step_ns
        out += f"{t}\t{t}\t{t}\n"
    return out


class TestFpsLayerSwitch(unittest.TestCase):
    """渲染层重建后帧统计不跨层残留（2026-08-25，二次评估 #1）。

    场景：切场景/游戏重启 → SurfaceView 层重建（#id 变化）→ 新层缓冲里新帧不足 2 个
    时，旧实现会沿用 _last_frame_stats（上一层的 P50/P95/Max）继续上报。
    """

    L1 = "SurfaceView[com.tencent.mm:appbrand0/com.tencent.mm.appbrand.AppUI]#123(BLAST)"
    L2 = "SurfaceView[com.tencent.mm:appbrand0/com.tencent.mm.appbrand.AppUI]#456(BLAST)"

    def _collector(self, adb):
        return FpsCollector(adb, "com.tencent.mm", "appbrand", retry_interval=0.0)

    def test_layer_switch_resets_frame_stats(self):
        # 旧层：8 帧、帧间隔 100ms（很卡）→ P50/P95/Max 全 100
        adb = SfMockAdb(self.L1, sf_latency(1_000_000_000_000, 8, 100_000_000))
        c = self._collector(adb)
        r1 = c.sample(1.0)
        self.assertEqual(r1["layer"], self.L1)
        self.assertEqual(r1["frame_p50_ms"], 100.0)

        # 层重建：#id 变化，新层缓冲此刻只有 1 帧（不足以算帧间隔）
        adb.layer = self.L2
        adb.latency = sf_latency(2_000_000_000_000, 1, 16_666_666)
        r2 = c.sample(2.0)                       # 旧层名读取失败 → 置空 + 重匹配到新层
        self.assertEqual(r2.get("error"), "layer_read_fail")
        self.assertEqual(c.layer, self.L2)
        self.assertIsNone(c._last_frame_stats)   # 基准已随层切换重置
        self.assertIsNone(c._last_seen_ts)
        self.assertIsNone(c._last_max_ts)

        r3 = c.sample(3.0)
        self.assertEqual(r3["total_frames"], 1)
        # 核心：新层新帧不足 2 个 → 不上报帧时间（修复前会残留旧层的 100.0）
        self.assertNotIn("frame_p50_ms", r3)
        self.assertNotIn("frame_p95_ms", r3)
        self.assertNotIn("frame_max_ms", r3)

        # 新层攒够帧后，统计来自新层自己的帧间隔（16.67ms），与旧层无关
        adb.latency = sf_latency(2_000_000_000_000, 6, 16_666_666)
        r4 = c.sample(4.0)
        self.assertEqual(r4["frame_p50_ms"], 16.67)
        self.assertEqual(r4["frame_max_ms"], 16.67)
        self.assertGreater(r4["fps"], 0)
        self.assertEqual(c.mode, "sf")           # 全程留在 sf 通道

    def test_layer_lost_keeps_sf_channel(self):
        """回归保护：层暂失时仍保留 sf 通道重匹配，绝不切 gfxinfo。

        （gfxinfo 对 WebGL 恒 0 帧，误切后 FPS 会永久归零）
        """
        adb = SfMockAdb(self.L1, sf_latency(1_000_000_000_000, 4, 16_666_666))
        c = self._collector(adb)
        c.sample(1.0)
        self.assertTrue(c._ever_surfaceview)

        adb.layer = None                          # 层销毁（切后台/重建中）
        r2 = c.sample(2.0)
        self.assertEqual(r2.get("error"), "layer_read_fail")
        r3 = c.sample(3.0)
        self.assertEqual(r3.get("error"), "no_layer")
        self.assertEqual(r3.get("hint"), "渲染层暂失,重匹配中")
        self.assertEqual(c.mode, "sf")
        self.assertFalse([a for a in adb.calls if "gfxinfo" in " ".join(a)])

        adb.layer = self.L1                       # 层找回：正常出数
        r4 = c.sample(4.0)
        self.assertEqual(r4["layer"], self.L1)
        self.assertEqual(r4["frame_p50_ms"], 16.67)


class TestFpsSparseSegment(unittest.TestCase):
    """OPPO 稀疏缓冲修复（2026-09-01）：按大 gap 切段取主段算 FPS。

    现象：OPPO ded7a388 的 SF --latency 缓冲 128 帧时间戳稀疏分布在约 35 分钟里
    （相邻帧间隔中位 16.7ms），旧版全缓冲首尾跨度算出 127/2108s ≈ 0.06 → 显示 0.01；
    修复后取帧数最多的密集主段，FPS 反映当前真实渲染节奏。
    """

    L = "SurfaceView[com.tencent.mm:appbrand0/AppUI]#776(BLAST)"

    @staticmethod
    def _latency_from_ts(tss, refresh_ns=16666666):
        out = f"{refresh_ns}\n"
        for t in tss:
            out += f"{t}\t{t}\t{t}\n"
        return out

    def _collector(self, adb):
        return FpsCollector(adb, "com.tencent.mm", "appbrand", retry_interval=0.0)

    # ---------- _main_segment 纯函数 ----------

    def test_segment_empty_and_single(self):
        self.assertEqual(_main_segment([]), [])
        self.assertEqual(_main_segment([100]), [100])

    def test_segment_contiguous_is_single(self):
        tss = [i * 16_666_666 for i in range(128)]
        self.assertEqual(_main_segment(tss), tss)

    def test_segment_picks_densest(self):
        # 段 1：2 帧；大 gap；段 2：5 帧（密集）→ 取段 2
        tss = [0, 16_000_000,
               10_000_000_000,
               20_000_000_000, 20_016_000_000, 20_032_000_000,
               20_048_000_000, 20_064_000_000]
        seg = _main_segment(tss)
        self.assertEqual(seg, tss[3:])

    def test_segment_tie_takes_later(self):
        # 两段各 2 帧并列 → 取靠后段（更接近当前节奏）
        tss = [0, 16_000_000, 5_000_000_000, 5_016_000_000]
        seg = _main_segment(tss)
        self.assertEqual(seg, tss[2:])

    def test_segment_jank_gap_does_not_split(self):
        # 300ms 真实卡顿 < 0.5s 阈值 → 不切段，主段含全部帧
        step = 16_666_666
        tss = [0, step, step * 2, step * 2 + 300_000_000, step * 2 + 300_000_000 + step]
        seg = _main_segment(tss)
        self.assertEqual(seg, tss)

    # ---------- sample() 端到端 ----------

    def test_sparse_buffer_fps_uses_main_segment(self):
        """OPPO 场景：孤立帧 + 2000s 大 gap + 127 帧密集段。

        修复后 FPS = 126 帧 ÷ (126×16.67ms) = 60.0；
        旧版全跨度 = 127 ÷ 2002.1s ≈ 0.06（病态低值）。
        """
        step = 16_666_666
        t0 = 1_000_000_000_000
        gap_ns = 2_000 * 1_000_000_000            # 2000s，远超 0.5s 切段阈值
        tss = [t0] + [t0 + gap_ns + i * step for i in range(127)]
        adb = SfMockAdb(self.L, self._latency_from_ts(tss))
        c = self._collector(adb)
        r = c.sample(1.0)
        self.assertEqual(r["total_frames"], 128)
        # 主段 = 后 127 帧，首尾跨度 = 126×16.67ms ≈ 2.1s → FPS ≈ 60.0
        self.assertEqual(r["fps"], 60.0)
        self.assertEqual(r["frame_p50_ms"], 16.67)   # 帧时间统计不受影响

    def test_contiguous_buffer_unchanged(self):
        """荣耀回归保护：128 帧连续缓冲 → 单段，行为与修复前完全一致。"""
        adb = SfMockAdb(self.L, sf_latency(1_000_000_000_000, 128, 16_666_666))
        c = self._collector(adb)
        r = c.sample(1.0)
        self.assertEqual(r["total_frames"], 128)
        self.assertEqual(r["fps"], 60.0)   # 127 ÷ (127×16.67ms)

    def test_main_segment_only_isolated_frame(self):
        """缓冲内每帧间隔都 >0.5s → 每段仅 1 帧 → 无可测节奏，FPS=0。"""
        tss = [0, 1_000_000_000, 2_000_000_000]   # 间隔 1s
        adb = SfMockAdb(self.L, self._latency_from_ts(tss))
        c = self._collector(adb)
        r = c.sample(1.0)
        self.assertEqual(r["fps"], 0.0)

    def test_stale_semantics_preserved(self):
        """静止判断不受切段影响：缓冲无新帧推进 → fps=0 + stale=True。"""
        adb = SfMockAdb(self.L, sf_latency(1_000_000_000_000, 8, 16_666_666))
        c = self._collector(adb)
        r1 = c.sample(1.0)
        self.assertGreater(r1["fps"], 0)
        r2 = c.sample(2.0)                          # 同一缓冲，无推进
        self.assertEqual(r2["fps"], 0.0)
        self.assertTrue(r2.get("stale"))

    def test_pause_resume_reflects_new_rhythm(self):
        """暂停恢复：恢复后新段是当前节奏，FPS 不被暂停间隙拉低。"""
        step = 16_666_666
        # 首轮：正常 8 帧
        adb = SfMockAdb(self.L, sf_latency(1_000_000_000_000, 8, step))
        c = self._collector(adb)
        r1 = c.sample(1.0)
        self.assertEqual(r1["fps"], 60.0)
        # 暂停 30s 后恢复：缓冲里旧段 8 帧 + 新段 8 帧（30s gap 切开）
        base2 = 1_000_000_000_000 + 7 * step + 30_000_000_000
        tss = [1_000_000_000_000 + i * step for i in range(8)] + \
              [base2 + i * step for i in range(8)]
        adb.latency = self._latency_from_ts(tss)
        r2 = c.sample(2.0)
        self.assertEqual(r2["fps"], 60.0)   # 主段 = 恢复后的 8 帧（并列取靠后段）


class TestExportFpsSource(unittest.TestCase):
    """导出扁平化透出 FPS 通道标记（2026-08-25，二次评估 kimi）。"""

    def test_columns_and_headers_aligned(self):
        keys = [k for k, _ in COLUMNS]
        self.assertIn("fps_source", keys)
        self.assertEqual(len(keys), len(set(keys)))          # 无重复列 key
        self.assertTrue(all(label for _, label in COLUMNS))  # 每列都有表头

    def test_sf_row_marked_sf(self):
        row = {"t_ms": 500, "fps": {"layer": "SurfaceView[x]#1", "total_frames": 120,
                                    "fps": 59.9, "jank_rate": 0.0, "refresh_hz": 60.0}}
        self.assertEqual(flatten(row)["fps_source"], "sf")

    def test_gfx_row_marked_gfxinfo(self):
        row = {"t_ms": 500, "fps": {"layer": "com.x/Act#1", "total_frames": 100, "fps": 60.0,
                                    "jank_rate": 0.01, "refresh_hz": None, "source": "gfxinfo"}}
        self.assertEqual(flatten(row)["fps_source"], "gfxinfo")

    def test_error_and_missing_rows_blank(self):
        err = {"t_ms": 500, "fps": {"layer": None, "total_frames": None, "fps": None,
                                    "jank_rate": None, "error": "no_layer"}}
        self.assertEqual(flatten(err)["fps_source"], "")
        self.assertEqual(flatten({"t_ms": 0})["fps_source"], "")


class TestCoresProbeAndMeta(unittest.TestCase):
    """核数探测（nproc）与 jsonl meta 行兼容（2026-08-26，任务 3）。"""

    def test_probe_cores_parses_nproc(self):
        adb = MockAdb({"nproc": "8\n"})
        self.assertEqual(CpuCollector.probe_cores(adb), 8)

    def test_probe_cores_falls_back_on_bad_output(self):
        adb = MockAdb({"nproc": "not-a-number\n"})
        self.assertEqual(CpuCollector.probe_cores(adb), 8)   # 解析失败 → 兜底 8
        adb2 = MockAdb({"nproc": "0\n"})
        self.assertEqual(CpuCollector.probe_cores(adb2), 8)  # 非法核数 → 兜底 8

    def test_probe_cores_default_override(self):
        adb = MockAdb({"nproc": "not-a-number\n"})
        self.assertEqual(CpuCollector.probe_cores(adb, default=12), 12)

    def test_extract_cores_and_data_rows_filter_meta(self):
        rows = [
            {"ts": 1.0, "event": "meta", "cores": 8},
            {"ts": 1.0, "t_ms": 0, "cpu": {"cpu_proc_pct": 50.0}},
            {"ts": 2.0, "t_ms": 1000, "cpu": {"cpu_proc_pct": 60.0}},
            {"ts": 2.0, "event": "target_switch", "to": "com.x"},
        ]
        self.assertEqual(extract_cores(rows), 8)
        d = data_rows(rows)
        self.assertEqual(len(d), 2)                     # meta / target_switch 被过滤
        self.assertTrue(all("event" not in r for r in d))
        self.assertEqual([r["t_ms"] for r in d], [0, 1000])

    def test_extract_cores_absent_returns_none(self):
        self.assertIsNone(extract_cores([{"t_ms": 0, "cpu": {}}]))
        self.assertIsNone(extract_cores([]))


class TestReportCacheLru(unittest.TestCase):
    """/api/report 解析缓存改 LRU（2026-08-25，二次评估 #2）。

    旧实现超上限整体 clear()：报告数 >50 时，常看的几份会被"连坐"清掉，
    每次点开都要重新全文解析（1MB jsonl ~50ms）。
    直接调真实的 Handler._load_report（该方法只用闭包里的 server，不碰 self/socket）。
    """

    def _server_with_reports(self, count, cache_max):
        import json as _json
        import shutil
        import tempfile
        from web import WebServer

        tmp = tempfile.mkdtemp(prefix="perfdog_test_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        names = []
        for i in range(count):
            name = f"r{i}.jsonl"
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                f.write(_json.dumps({"t_ms": i * 500, "fps": {"fps": 60.0}}) + "\n")
            names.append(name)
        server = WebServer(port=0, output_dir=tmp)
        server._report_cache_max = cache_max
        handler_cls = server._make_handler()
        dummy = handler_cls.__new__(handler_cls)          # 不走 socket 初始化

        def load(name):
            return handler_cls._load_report(dummy, name)

        return server, names, load

    def test_lru_evicts_least_recently_used_only(self):
        server, names, load = self._server_with_reports(4, cache_max=3)
        rows = load(names[0])
        self.assertEqual(rows[0]["t_ms"], 0)
        load(names[1])
        load(names[2])
        self.assertEqual(list(server._report_cache), names[:3])

        load(names[0])                                     # 命中 → 提升为最近使用
        self.assertEqual(list(server._report_cache)[-1], names[0])

        load(names[3])                                     # 超上限 → 只淘汰最久未使用的
        self.assertEqual(len(server._report_cache), 3)
        self.assertNotIn(names[1], server._report_cache)   # r1 最久未用，被淘汰
        self.assertIn(names[0], server._report_cache)      # 热点保留（旧实现此处已被清空）
        self.assertIn(names[2], server._report_cache)
        self.assertIn(names[3], server._report_cache)

    def test_cache_hit_returns_same_object_and_invalidates_on_change(self):
        server, names, load = self._server_with_reports(1, cache_max=3)
        first = load(names[0])
        self.assertIs(load(names[0]), first)               # 命中缓存，不重复解析

        path = os.path.join(server.output_dir, names[0])   # 文件变化（size/mtime）→ 失效重读
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"t_ms": 0}\n{"t_ms": 500}\n')
        again = load(names[0])
        self.assertEqual(len(again), 2)
        self.assertEqual(len(server._report_cache), 1)     # 同名只占一条

    def test_bad_name_and_traversal_rejected(self):
        server, names, load = self._server_with_reports(1, cache_max=3)
        self.assertEqual(load("")["error"], "bad name")
        self.assertEqual(load("x.txt")["error"], "bad name")
        self.assertEqual(load("../x.jsonl")["error"], "bad path")
        self.assertEqual(load("nope.jsonl")["error"], "no such file")
        self.assertEqual(len(server._report_cache), 0)     # 非法/失败请求不污染缓存


class TestRunsMetaCount(unittest.TestCase):
    """/api/runs 行数缓存对 meta 首行的处理（2026-08-26，v41 问题 2）。

    v41 起 jsonl 首行是 {"event":"meta","cores":N}，旧逻辑把它计入 points，
    历史列表"采样点数"比真实多 1。修复后 points = 行数 - (首行为 meta ? 1 : 0)。
    """

    def _list_runs_for(self, files):
        import json as _json
        import shutil
        import tempfile
        from web import WebServer
        tmp = tempfile.mkdtemp(prefix="perfdog_runs_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for rel, lines in files.items():
            fp = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                for line in lines:
                    f.write(_json.dumps(line, ensure_ascii=False) + "\n")
        server = WebServer(port=0, output_dir=tmp)
        handler_cls = server._make_handler()
        dummy = handler_cls.__new__(handler_cls)
        runs = handler_cls._list_runs(dummy)
        return {r["name"]: r["points"] for r in runs}

    def test_meta_line_not_counted_as_point(self):
        got = self._list_runs_for({
            "a/with_meta.jsonl": [
                {"ts": 1.0, "event": "meta", "cores": 8},
                {"ts": 1.0, "t_ms": 0, "cpu": {}},
                {"ts": 2.0, "t_ms": 1000, "cpu": {}},
            ],
            "b/without_meta.jsonl": [
                {"ts": 1.0, "t_ms": 0, "cpu": {}},
                {"ts": 2.0, "t_ms": 1000, "cpu": {}},
            ],
        })
        self.assertEqual(got["a/with_meta.jsonl"], 2)      # 3 行 - 1 meta
        self.assertEqual(got["b/without_meta.jsonl"], 2)   # 老数据无 meta，不受影响

    def test_meta_detection_tolerates_space_and_field_order(self):
        # 首行 JSON 含空格/字段序不同，也应正确识别为 meta
        got = self._list_runs_for({
            "c/x.jsonl": [
                {"cores": 4, "event": "meta", "ts": 1.0},
                {"ts": 1.0, "t_ms": 0, "cpu": {}},
            ],
        })
        self.assertEqual(got["c/x.jsonl"], 1)


class TestMemParsers(unittest.TestCase):
    """内存 PSS/RSS 同源解析（P0-1）。"""

    SMAPS_ROLLUP = """\
55b0a2b5d000-7f80000000 rw-p 00000000 00:00 0 [anon:libc_malloc]
Rss:              512000 kB
Pss:              463000 kB
Pss_Anon:         400000 kB
Pss_File:         61000 kB
Pss_Shmem:        2000 kB
Shared_Clean:     3000 kB
Shared_Dirty:     49000 kB
Private_Clean:    1000 kB
Private_Dirty:    459000 kB
Referenced:       500000 kB
Anonymous:        459000 kB
"""

    MEMINFO = """\
Applications Memory Usage (in Kilobytes):
Uptime: 1000000 Realtime: 1000000

** MEMINFO in pid 1697 [com.tencent.mm] **
                   Pss  Private  Private  Swap      Heap     Heap     Heap
                 Total    Dirty    Clean    Dirty     Size    Alloc     Free
                ------   ------   ------   ------   ------   ------   ------
  Native Heap     2000     2000        0        0    10000     8000     2000
  Dalvik Heap     3000     3000        0        0     5000     4000     1000

App Summary
                       Pss(KB)                        Rss(KB)
                       ------                        ------
           Java Heap:     3000                         9000
         Native Heap:     2000                         8000
                TOTAL PSS:    25000              TOTAL RSS:    40000     TOTAL DIRTY:   30000
"""

    def test_smaps_rollup_parses_pss_rss(self):
        d = parse_smaps_rollup(self.SMAPS_ROLLUP)
        self.assertIsNotNone(d)
        self.assertEqual(d["pss_kb"], 463000)
        self.assertEqual(d["rss_kb"], 512000)
        self.assertGreaterEqual(d["rss_kb"], d["pss_kb"])   # 物理约束

    def test_smaps_rollup_rejects_pss_anon(self):
        # 行首精确匹配：Pss_Anon 等不能误判为 Pss
        d = parse_smaps_rollup("Rss: 1 kB\nPss_Anon: 999 kB\n")
        self.assertIsNone(d)

    def test_meminfo_app_summary_same_source(self):
        d = parse_meminfo(self.MEMINFO)
        self.assertEqual(d.get("pss_kb"), 25000)
        self.assertEqual(d.get("rss_kb"), 40000)
        self.assertGreaterEqual(d["rss_kb"], d["pss_kb"])

    def test_meminfo_case_insensitive(self):
        d = parse_meminfo("App Summary\n\ntotal pss: 1234\ntotal rss: 5678\n")
        self.assertEqual(d.get("pss_kb"), 1234)
        self.assertEqual(d.get("rss_kb"), 5678)

    def test_meminfo_real_honor_format_with_inner_blank_line(self):
        # 荣耀 ADT-AN00 (Android 14) 真实输出（2026-08-24 抓取）：
        # App Summary 段内 "Unknown:" 与 "TOTAL PSS:" 之间有空行——
        # 旧版按"段内第一个空行截断"会切掉 TOTAL 行导致 PSS 全空（v36 回归 bug）
        out = """\
App Summary
                       Pss(KB)                        Rss(KB)
                        ------                         ------
           Java Heap:   103968                         128400
         Native Heap:    88760                          93584
                Code:    54972                         227108
               Stack:     5088                           5244
            Graphics:     9224                           9228
       Private Other:    96764
              System:   165259
             Unknown:                                  106748
 
           TOTAL PSS:   524035            TOTAL RSS:   570312       TOTAL SWAP PSS:   130050
 
 Objects
"""
        d = parse_meminfo(out)
        self.assertEqual(d.get("pss_kb"), 524035)
        self.assertEqual(d.get("rss_kb"), 570312)
        self.assertGreaterEqual(d["rss_kb"], d["pss_kb"])


class TestCpuMergedCommand(unittest.TestCase):
    """cpu 合并 adb 往返后的解析（P2-7）。"""

    def _out(self, stat, pstat):
        return stat + "__PDSEP__" + pstat

    def test_merged_cpu_math(self):
        stat1 = "cpu  100 0 50 850 0 0 0 0 0 0\n"          # total=1000 idle=850 busy=150
        proc1 = "1697 (com.tencent.mm) S 1 2 3 4 5 6 7 8 9 10 11 100 200 0 0\n"
        stat2 = "cpu  120 0 70 910 0 0 0 0 0 0\n"          # total=1100 idle=910 busy=190
        proc2 = "1697 (com.tencent.mm) S 1 2 3 4 5 6 7 8 9 10 11 200 200 0 0\n"
        adb = MockAdb({
            "cat /proc/stat": self._out(stat1, proc1),
            "echo __PDSEP__": self._out(stat1, proc1),
        })
        # 顺序响应：第一次 sample 拿 stat1+proc1，第二次拿 stat2+proc2
        adb.responses = {}
        adb.responses["__PDSEP__"] = [self._out(stat1, proc1), self._out(stat2, proc2)]

        def _shell(args):
            adb.calls.append(list(args))
            joined = " ".join(args)
            assert "__PDSEP__" in joined
            idx = len([c for c in adb.calls if "__PDSEP__" in " ".join(c)]) - 1
            return adb.responses["__PDSEP__"][idx]

        adb.shell = _shell
        c = CpuCollector(adb, MockResolver(1697), clk_tck=100)
        r1 = c.sample(100.0)
        r2 = c.sample(101.0)
        self.assertEqual(r1["cpu_total_pct"], None)        # 首轮无差值
        self.assertEqual(r2["cpu_total_pct"], 40.0)        # dbusy 40 / dcpu 100
        self.assertEqual(r2["cpu_proc_pct"], 100.0)        # dproc 100 jiffies / 100 / 1s
        # 合并后每轮只发一次 adb shell（含 __PDSEP__）
        self.assertEqual(len([c for c in adb.calls if "__PDSEP__" in " ".join(c)]), 2)


    def test_process_restart_resets_proc_baseline(self):
        """进程重启（pid 变化）后进程%基线重置：首个样本为 None，不跨进程相减。

        评估报告低优先级项（2026-08-24 补齐）：旧实现用旧进程 jiffies 减新进程，
        且 dt 横跨进程死亡期，重启后首个进程%严重失真。
        """
        adb = MockAdb({})
        # 三次采样：/proc/stat 递增（整机%可算），进程在第二次采样时重启换 pid
        seq = [
            "cpu  100 0 50 850 0 0 0 0 0 0\n"  # total=1000 busy=150
            "__PDSEP__" + "100 (oldproc) S 1 2 3 4 5 6 7 8 9 10 11 500 500 0 0\n",
            "cpu  120 0 70 910 0 0 0 0 0 0\n"  # total=1100 busy=190
            "__PDSEP__" + "200 (newproc) S 1 2 3 4 5 6 7 8 9 10 11 30 30 0 0\n",
            "cpu  140 0 90 970 0 0 0 0 0 0\n"  # total=1200 busy=230
            "__PDSEP__" + "200 (newproc) S 1 2 3 4 5 6 7 8 9 10 11 30 30 0 0\n",
        ]
        calls = {"n": 0}

        def _shell(args):
            out = seq[min(calls["n"], len(seq) - 1)]
            calls["n"] += 1
            return out

        adb.shell = _shell
        resolver = MockResolver(100)
        c = CpuCollector(adb, resolver, clk_tck=100)
        r1 = c.sample(100.0)
        self.assertIsNone(r1["cpu_proc_pct"])      # 首轮基线
        resolver.pid = 200                          # 进程重启，pid 变化
        r2 = c.sample(101.0)
        self.assertIsNone(r2["cpu_proc_pct"])      # 基线重置，不跨进程相减
        self.assertIsNotNone(r2["cpu_total_pct"])  # 整机基线保留（与进程无关）
        r3 = c.sample(102.0)
        self.assertEqual(r3["cpu_proc_pct"], 0.0)  # 新基线后 jiffies 未变 → 增量 0
        self.assertEqual(c._last_pid, 200)


class TestThermalValidation(unittest.TestCase):
    """温度物理范围校验（P0-2）：0.01°C 口径重算 / 异常置 None。"""

    def test_dumpsys_3700_recomputed_as_37c(self):
        # 3700 无条件 /10 = 370°C（荒谬）→ 按 0.01°C 口径 /100 = 37°C
        adb = MockAdb({
            "/sys/class/power_supply/battery/temp": "not-a-number\n",
            "/sys/class/power_supply/battery/current_now": "not-a-number\n",
            "dumpsys battery": "temperature: 3700\nvoltage: 4100\n",
        })
        t = ThermalCollector(adb)
        r = t.sample(0)
        self.assertEqual(r["temp_c"], 37.0)
        self.assertIsNone(r.get("error"))

    def test_absurd_temp_becomes_none(self):
        adb = MockAdb({
            "/sys/class/power_supply/battery/temp": "not-a-number\n",
            "/sys/class/power_supply/battery/current_now": "not-a-number\n",
            "dumpsys battery": "temperature: 50000\nvoltage: 4100\n",   # 5000°C 两口径都荒谬
        })
        t = ThermalCollector(adb)
        r = t.sample(0)
        self.assertIsNone(r["temp_c"])
        self.assertEqual(r.get("error"), "temperature_out_of_range")

    def test_normal_temp_unchanged(self):
        adb = MockAdb({
            "/sys/class/power_supply/battery/temp": "not-a-number\n",
            "/sys/class/power_supply/battery/current_now": "not-a-number\n",
            "dumpsys battery": "temperature: 430\nvoltage: 4100\n",    # 43.0°C 正常
        })
        t = ThermalCollector(adb)
        r = t.sample(0)
        self.assertEqual(r["temp_c"], 43.0)


class TestPidResolverParsing(unittest.TestCase):
    """ps -A -o PID,ARGS 解析（P2-9）。"""

    def test_resolve_with_ps_args(self):
        out = ("PID ARGS\n"
               "1417 /system/bin/surfaceflinger\n"
               "1697 com.tencent.mm\n"
               "5838 com.tencent.mm:appbrand0\n")
        adb = MockAdb({"ps -A -o PID,ARGS": out})
        r = PidResolver(adb, "com.tencent.mm", "appbrand")
        pid = r.resolve()
        self.assertEqual(pid, 5838)     # 优先匹配 process_pattern 子进程

    def test_resolve_fallback_to_main(self):
        out = "PID ARGS\n1417 /system/bin/surfaceflinger\n1697 com.tencent.mm\n"
        adb = MockAdb({"ps -A -o PID,ARGS": out})
        r = PidResolver(adb, "com.tencent.mm", "appbrand")
        pid = r.resolve()
        self.assertEqual(pid, 1697)     # 无子进程 → 回退主进程

    def test_resolve_comm_mismatch_triggers_recheck(self):
        # pid 被系统复用：comm 不匹配期望 → current_pid 重新解析
        adb = MockAdb({
            "ps -A -o PID,ARGS": "PID ARGS\n1697 com.tencent.mm\n5838 com.tencent.mm:appbrand0\n",
            "cat /proc/1697/comm": "surfaceflinger\n",    # 身份已变
        })
        r = PidResolver(adb, "com.tencent.mm", "appbrand")
        r.pid = 1697
        r._next_check = 0
        got = r.current_pid(ts=1.0)
        self.assertEqual(got, 5838)     # comm 不符 → 重新解析到 appbrand 子进程


class TestPidResolverActivePick(unittest.TestCase):
    """多 appbrand 候选选最活跃进程（2026-08-27 P0 修复）。

    微信多开/驻留时 appbrand0/1/2 并存，旧逻辑取 ps 第一个（pid 最小）会采到
    闲置驻留进程；修复后按 /proc/<pid>/stat 的 utime+stime 选累计 CPU 最大者。
    """

    def test_multiple_candidates_picks_most_active(self):
        adb = MockAdb({
            "ps -A -o PID,ARGS":
                "PID ARGS\n"
                "100 /system/bin/surfaceflinger\n"
                "11715 com.tencent.mm:appbrand2\n"
                "18186 com.tencent.mm:appbrand1\n"
                "18243 com.tencent.mm:appbrand0\n",
            # cat 三个候选 stat：appbrand2 闲置（38s），appbrand1 活跃（1476s）
            "cat /proc/11715/stat /proc/18186/stat /proc/18243/stat":
                "11715 (com.tencent.mm:appbrand2) S 1 2 3 4 5 6 7 8 9 10 11 2000 1800 0 0\n"
                "18186 (com.tencent.mm:appbrand1) S 1 2 3 4 5 6 7 8 9 10 11 80000 67600 0 0\n"
                "18243 (com.tencent.mm:appbrand0) S 1 2 3 4 5 6 7 8 9 10 11 5000 4000 0 0\n",
        })
        r = PidResolver(adb, "com.tencent.mm", "appbrand")
        pid = r.resolve()
        self.assertEqual(pid, 18186)                    # 选 CPU 时间最大的 appbrand1
        self.assertIn("appbrand1", r.proc_name or "")

    def test_single_candidate_no_extra_read(self):
        # 只有一个候选时不额外读 stat（len(cands)<=1 直接返回）
        adb = MockAdb({
            "ps -A -o PID,ARGS":
                "PID ARGS\n100 /system/bin/surfaceflinger\n1697 com.tencent.mm:appbrand0\n",
        })
        r = PidResolver(adb, "com.tencent.mm", "appbrand")
        pid = r.resolve()
        self.assertEqual(pid, 1697)
        # MockAdb 未配置 "cat /proc/..." 响应，若被调用会抛 AssertionError → 到不了这行


class TestProbeCores(unittest.TestCase):
    """核数探测：/sys/devices/system/cpu/online 优先（2026-08-27 P1 修复）。

    旧实现只 nproc，被 cpuset 限制时会少报（8 核报 6）；online 反映物理核数。
    """

    def test_parse_cpu_online_variants(self):
        from metrics.cpu import CpuCollector
        cases = {
            "0-7": 8, "-7": 8, "0,2-7": 8, "3": 4,
            "0-3\n": 4, " 0-5 ": 6, "0-1,3-5": 6, "": None, "abc": None,
        }
        for raw, expect in cases.items():
            got = CpuCollector._parse_cpu_online(raw)
            self.assertEqual(got, expect, f"online={raw!r} → {got}（期望 {expect}）")

    def test_probe_cores_prefers_sysfs(self):
        # online 可读 → 用物理核数 8，即使 nproc 只报 6（cpuset 限制）
        adb = MockAdb({
            "cat /sys/devices/system/cpu/online": "0-7\n",
            "nproc": "6\n",
        })
        from metrics.cpu import CpuCollector
        self.assertEqual(CpuCollector.probe_cores(adb), 8)

    def test_probe_cores_falls_back_to_nproc_then_default(self):
        from metrics.cpu import CpuCollector
        # online 读失败 → nproc
        adb1 = MockAdb({"nproc": "4\n"})
        self.assertEqual(CpuCollector.probe_cores(adb1), 4)
        # online 与 nproc 都失败 → default
        adb2 = MockAdb({})
        self.assertEqual(CpuCollector.probe_cores(adb2), 8)


class TestJankRhythmThreshold(unittest.TestCase):
    """Jank 阈值节奏校准（2026-08-27 kimi 归因修复）。

    面板 120Hz 但游戏锁 60fps 时，refresh_ns 口径阈值 16.67ms 与帧间隔 16.7ms
    压线误判假 Jank；改为按实际帧间隔中位数吸附标准 vsync 档（60/90/120/144Hz）。
    """

    def _make(self, refresh_ns=8_333_333):
        from metrics.fps import FpsCollector
        c = FpsCollector.__new__(FpsCollector)   # 跳过 __init__（无 adb）
        c.refresh_ns = refresh_ns
        return c

    def _ts(self, gap_ns, count):
        # 生成 count+1 个时间戳，间隔 gap_ns（带 ±2% 抖动）
        import random
        out = []
        t = 1_000_000_000
        for _ in range(count):
            out.append(t)
            t += int(gap_ns * random.uniform(0.98, 1.02))
        return out

    def test_60fps_on_120hz_panel_no_false_jank(self):
        # 60fps 帧间隔 ~16.7ms，面板 refresh=120Hz：旧口径阈值 16.67ms 全误判
        c = self._make(refresh_ns=8_333_333)
        new_ts = self._ts(16_666_666, 30)
        thr = c._jank_threshold_ns(new_ts)
        self.assertGreater(thr, 33_000_000)          # 阈值≈36.7ms（16.67×2×1.1）
        over = sum(1 for i in range(1, len(new_ts))
                   if new_ts[i] - new_ts[i - 1] > thr)
        self.assertEqual(over, 0)                     # 正常帧不再被误判为 Jank

    def test_real_120hz_game_threshold_basically_unchanged(self):
        c = self._make(refresh_ns=8_333_333)
        new_ts = self._ts(8_333_333, 30)
        thr = c._jank_threshold_ns(new_ts)
        self.assertAlmostEqual(thr / 1e6, 8.3333 * 2 * 1.1, delta=1.0)  # ≈18.3ms

    def test_60fps_on_144hz_panel(self):
        # 144Hz 面板锁 60fps：间隔 16.7ms 吸附 60Hz 档，不误判
        c = self._make(refresh_ns=6_944_444)
        new_ts = self._ts(16_666_666, 30)
        thr = c._jank_threshold_ns(new_ts)
        self.assertGreater(thr, 33_000_000)

    def test_few_frames_falls_back_to_refresh(self):
        # 新帧 <8 → 回退 refresh_ns×2
        c = self._make(refresh_ns=8_333_333)
        new_ts = self._ts(16_666_666, 5)
        thr = c._jank_threshold_ns(new_ts)
        self.assertAlmostEqual(thr / 1e6, 8.3333 * 2, delta=0.1)   # 16.67ms

    def test_locked_30fps_not_snapped(self):
        # 锁 30fps：间隔 ~33.3ms 不吸附任何标准档（容差 10%），阈值放宽到 73ms
        c = self._make(refresh_ns=8_333_333)
        new_ts = self._ts(33_333_333, 30)
        thr = c._jank_threshold_ns(new_ts)
        self.assertGreater(thr / 1e6, 60)            # >60ms


class TestOppoLayerResolve(unittest.TestCase):
    """OPPO 机型层匹配（2026-08-27 适配）：跳过 ActivityRecordInputSink 输入层，
    优先 SurfaceView；窗口层兜底不被输入层污染。"""

    LIST_OPPO = (
        "dumpsys SurfaceFlinger --list:\n"
        "c2601e6 ActivityRecordInputSink com.tencent.mm/.ui.LauncherUI#147\n"
        "ActivityRecord{e6a8841 u0 com.tencent.mm/.ui.LauncherUI t738}#143\n"
        "6f89759 ActivityRecordInputSink com.tencent.mm/.plugin.appbrand.ui.AppBrandUI#628\n"
        "ActivityRecord{ccb0fa0 u0 com.tencent.mm/.plugin.appbrand.ui.AppBrandUI t740}#617\n"
        "e98d43b com.tencent.mm/com.tencent.mm.plugin.appbrand.ui.AppBrandUI#622\n"
        "Background for SurfaceView[com.tencent.mm/com.tencent.mm.plugin.appbrand.ui.AppBrandUI]#777\n"
        "SurfaceView[com.tencent.mm/com.tencent.mm.plugin.appbrand.ui.AppBrandUI](BLAST)#776\n"
    )

    def _resolve(self, out):
        from metrics.fps import FpsCollector
        adb = MockAdb({"dumpsys SurfaceFlinger --list": out})
        c = FpsCollector(adb, "com.tencent.mm", "appbrand")
        return c.resolve_layer()

    def test_prefers_surfaceview_blast_over_input_sink(self):
        layer = self._resolve(self.LIST_OPPO)
        self.assertIn("SurfaceView[", layer)
        self.assertIn("(BLAST)", layer)

    def test_window_fallback_skips_input_sink(self):
        # 无 SurfaceView 层（游戏启动初期）：窗口层兜底必须跳过 ActivityRecordInputSink
        out = ("dumpsys SurfaceFlinger --list:\n"
               "6f89759 ActivityRecordInputSink com.tencent.mm/.plugin.appbrand.ui.AppBrandUI#628\n"
               "e98d43b com.tencent.mm/com.tencent.mm.plugin.appbrand.ui.AppBrandUI#622\n")
        layer = self._resolve(out)
        self.assertIn("AppBrandUI#622", layer)
        self.assertNotIn("ActivityRecordInputSink", layer)


if __name__ == "__main__":
    unittest.main()
