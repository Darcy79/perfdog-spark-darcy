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
import json
import subprocess
import unittest

# 注入 collector 目录到 sys.path（main.py 以 collector 为运行根）
_COLLECTOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "collector")
if _COLLECTOR not in sys.path:
    sys.path.insert(0, _COLLECTOR)

from metrics.fps import FpsCollector, MAX_VALID_TS, _main_segment, FPS_SEGMENT_GAP_NS
from metrics.mem import parse_smaps_rollup, parse_meminfo, MemCollector
from metrics.cpu import CpuCollector
from metrics.thermal import ThermalCollector
from pidresolver import PidResolver
from adb import Adb, AdbError
from main import ChannelAlertTracker, row_has_any_value
from export_report import COLUMNS, flatten, extract_cores, data_rows, script_safe_json


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

        adb.layer = "com.android.launcher/RecentsActivity#5"
        # ↑ v66 语义：--list 输出为空 = 链路读失败（probe_fail），所以"层销毁但
        #   链路正常"要用"返回不匹配的系统层列表"模拟；SfMockAdb(None) 的空输出
        #   现在归为 probe_fail（见 TestFpsProbeFail）
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
        # pid 被系统复用：cmdline 可读且明确不匹配 → current_pid 立即重新解析
        # （v66 三态化：comm-only 不匹配属"未知"不再触发失效，见
        #   TestPidResolverIdentity.test_comm_mismatch_only_is_unknown_three_strikes）
        adb = MockAdb({
            "ps -A -o PID,ARGS": "PID ARGS\n1697 com.tencent.mm\n5838 com.tencent.mm:appbrand0\n",
            "cat /proc/1697/cmdline": "surfaceflinger\n",    # 身份已变（结论可靠）
        })
        r = PidResolver(adb, "com.tencent.mm", "appbrand")
        r.pid = 1697
        r._next_check = 0
        got = r.current_pid(ts=1.0)
        self.assertEqual(got, 5838)     # cmdline 不符 → 重新解析到 appbrand 子进程


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


class TestFpsPhysicalClamp(unittest.TestCase):
    """FPS 物理上限钳制与低帧数告警（2026-09-11）。

    主段仅 2 帧、间隔极近时 (帧数-1)/span 输出非物理值（2 帧相隔 0.5ms → 2000
    FPS）；修复后钳制到刷新率×1.5（60Hz → 90），并带 fps_clamped / fps_warn
    标记。正常连续/稀疏缓冲的既有正确值必须保持不变。
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

    def test_two_close_frames_clamped(self):
        # 主段 2 帧、相隔 0.5ms → 裸算 2000 FPS → 钳制到 60Hz×1.5=90 + 双标记
        t0 = 1_000_000_000_000
        adb = SfMockAdb(self.L, self._latency_from_ts([t0, t0 + 500_000]))
        c = self._collector(adb)
        r = c.sample(1.0)
        self.assertEqual(r["fps"], 90.0)
        self.assertTrue(r.get("fps_clamped"))
        self.assertEqual(r.get("fps_warn"), "low_frames")

    def test_bad_refresh_header_uses_default_cap(self):
        # 刷新周期行缺失（坏首行）→ 回退默认 60Hz，上限仍是 90
        t0 = 1_000_000_000_000
        out = "bad-header\n"
        for i in range(2):
            out += f"{t0 + i * 100_000}\t{t0 + i * 100_000}\t{t0 + i * 100_000}\n"
        adb = SfMockAdb(self.L, out)
        c = self._collector(adb)
        r = c.sample(1.0)
        self.assertEqual(r["fps"], 90.0)      # 裸算 1/0.0001 = 10000 → 钳到 90
        self.assertTrue(r.get("fps_clamped"))
        self.assertEqual(r.get("refresh_hz"), 60.0)   # 坏首行 → 默认刷新率，恒为正

    def test_fps_cap_fallback_for_out_of_window_refresh(self):
        # refresh_ns 超出物理窗口（0.5Hz）→ 兜底上限 240
        c = FpsCollector.__new__(FpsCollector)
        c.refresh_ns = 2_000_000_000
        self.assertEqual(c._fps_cap(), 240.0)
        c.refresh_ns = 8_333_333
        self.assertAlmostEqual(c._fps_cap(), 180.0, places=4)   # 120Hz×1.5

    def test_normal_contiguous_no_clamp_no_warn(self):
        # 荣耀连续缓冲回归保护：60.0 正常值，无钳制无告警
        adb = SfMockAdb(self.L, sf_latency(1_000_000_000_000, 128, 16_666_666))
        c = self._collector(adb)
        r = c.sample(1.0)
        self.assertEqual(r["fps"], 60.0)
        self.assertNotIn("fps_clamped", r)
        self.assertNotIn("fps_warn", r)

    def test_sparse_buffer_main_segment_value_unchanged(self):
        # OPPO 稀疏缓冲回归保护：主段法 60.0 保持不变
        step = 16_666_666
        t0 = 1_000_000_000_000
        gap_ns = 2_000 * 1_000_000_000
        tss = [t0] + [t0 + gap_ns + i * step for i in range(127)]
        adb = SfMockAdb(self.L, self._latency_from_ts(tss))
        c = self._collector(adb)
        r = c.sample(1.0)
        self.assertEqual(r["fps"], 60.0)
        self.assertNotIn("fps_clamped", r)
        self.assertNotIn("fps_warn", r)

    def test_real_120fps_stream_not_clamped(self):
        # 真实 120fps 流：上限 180，正常读数不被误钳
        tss = [1_000_000_000_000 + i * 8_333_333 for i in range(32)]
        adb = SfMockAdb(self.L, self._latency_from_ts(tss, refresh_ns=8_333_333))
        c = self._collector(adb)
        r = c.sample(1.0)
        self.assertEqual(r["fps"], 120.0)
        self.assertNotIn("fps_clamped", r)
        self.assertNotIn("fps_warn", r)


class TestParseLatencyFirstLine(unittest.TestCase):
    """--latency 刷新周期行改按"首个非空行"判定（2026-09-11）。

    旧实现按物理第 0 行（i==0）判定：输出带前导空行时，刷新周期行落到了
    数据区 → 16666666 被当成一个"帧时间戳"混入，污染 gaps/FPS。
    同时刷新周期带物理窗口校验（1ms..1s），异常值不采用（refresh_hz 恒为正）。
    """

    def test_leading_blank_line_refresh_still_parsed(self):
        out = "\n16666666\n"
        for i in range(3):
            t = 1_000_000_000 + i * 16_666_666
            out += f"{t}\t{t}\t{t}\n"
        refresh, ts = FpsCollector._parse_latency(out)
        self.assertEqual(refresh, 16_666_666)
        self.assertEqual(len(ts), 3)   # 旧实现会混入 16666666 这个"伪帧"

    def test_refresh_out_of_physical_window_falls_back(self):
        # 首行数字异常（如 500ns）→ 不采用也不当帧数据，回退默认 60Hz
        out = "500\n"
        for i in range(3):
            t = 1_000_000_000 + i * 16_666_666
            out += f"{t}\t{t}\t{t}\n"
        refresh, ts = FpsCollector._parse_latency(out)
        self.assertEqual(refresh, 16_666_666)
        self.assertEqual(ts, [1_000_000_000, 1_016_666_666, 1_033_333_332])

    def test_normal_and_bad_first_line_unchanged(self):
        # 正常输出与坏首行（非数字）行为与旧版完全一致（回归保护）
        refresh, ts = FpsCollector._parse_latency("16666666\n0\n100\n200\n")
        self.assertEqual(refresh, 16_666_666)
        self.assertEqual(ts, [100, 200])
        refresh, ts = FpsCollector._parse_latency("not-a-number\n0\n100\n200\n")
        self.assertEqual(refresh, 16_666_666)
        self.assertEqual(ts, [100, 200])


class TestPidResolverIdentity(unittest.TestCase):
    """pid 身份校验（2026-09-11）：cmdline 优先，comm 截断形态不再误杀。

    真机实测（荣耀 ADT-AN00 / Android 14）：comm = 进程名**末 15 字符**
    （com.android.systemui → "ndroid.systemui"），appbrand 进程 comm 碰巧含
    "appbrand"；但标准 Linux 是首 15 截断（→ "com.tencent.mm:"，不含关键字）
    → 旧校验在这类 ROM 上对正确进程恒失败，每 5s 触发 pid=None + 节流窗口。
    改为 cmdline（完整进程名）优先、comm 回退的双级校验，两类 ROM 都正确。
    """

    PS = ("PID ARGS\n"
          "1417 /system/bin/surfaceflinger\n"
          "5838 com.tencent.mm:appbrand0\n")

    def _resolver(self, responses):
        adb = MockAdb(dict(responses, **{"ps -A -o PID,ARGS": self.PS}))
        r = PidResolver(adb, "com.tencent.mm", "appbrand")
        self.assertEqual(r.resolve(), 5838)
        r._next_check = 0
        return r, adb

    def _ps_calls(self, adb):
        return [c for c in adb.calls if "ps -A" in " ".join(c)]

    def test_correct_process_truncated_comm_keeps_pid(self):
        # 进程正确但 comm 不含关键字（首 15 截断 ROM）：cmdline 比对命中 → 不重解析
        r, adb = self._resolver({
            "cat /proc/5838/cmdline": "com.tencent.mm:appbrand0\x00",
            "cat /proc/5838/comm": "com.tencent.mm:\n",   # 标准截断形态
        })
        got = r.current_pid(ts=1.0)
        self.assertEqual(got, 5838)
        self.assertEqual(len(self._ps_calls(adb)), 1)   # 未触发 re-resolve

    def test_reused_pid_cmdline_mismatch_reresolves(self):
        # pid 被复用给别的进程：cmdline 可读且不匹配 → 立即 re-resolve
        # （comm 给"含 appbrand"的干扰值，证明优先走 cmdline、不被 comm 误导）
        r, adb = self._resolver({
            "cat /proc/5838/cmdline": "com.android.chrome\x00",
            "cat /proc/5838/comm": "com.tencent.mm:appbrand0\n",
        })
        adb.responses["ps -A -o PID,ARGS"] = \
            "PID ARGS\n1417 /system/bin/surfaceflinger\n7000 com.tencent.mm:appbrand1\n"
        got = r.current_pid(ts=1.0)
        self.assertEqual(got, 7000)

    def test_cmdline_unreadable_falls_back_to_comm(self):
        # cmdline 读取失败（SELinux/旧内核）→ 回退 comm 包含匹配，不误杀正确进程
        r, adb = self._resolver({
            "cat /proc/5838/comm": "com.tencent.mm:appbrand0\n",   # 未截断 ROM
        })
        got = r.current_pid(ts=1.0)
        self.assertEqual(got, 5838)
        self.assertEqual(len(self._ps_calls(adb)), 1)

    def test_comm_mismatch_only_is_unknown_three_strikes(self):
        # 2026-09-11 v66 三态化：cmdline 读失败 + comm 不匹配 ≠ 被复用
        # （comm 截断方向因 ROM 而异，不匹配可能只是关键字被截掉）→ 属"未知"，
        # 沿用旧 pid；连续 IDENTITY_FAIL_STREAK(3) 次未知才判失效 re-resolve
        r, adb = self._resolver({
            "cat /proc/5838/comm": "surfaceflinger\n",
        })
        adb.responses["ps -A -o PID,ARGS"] = \
            "PID ARGS\n1417 /system/bin/surfaceflinger\n7000 com.tencent.mm:appbrand1\n"
        self.assertEqual(r.current_pid(ts=1.0), 5838)    # 第 1 次未知 → 沿用
        self.assertEqual(r.current_pid(ts=10.0), 5838)   # 第 2 次未知 → 沿用
        self.assertEqual(r.current_pid(ts=20.0), 7000)   # 第 3 次未知 → 判失效
        self.assertEqual(len(self._ps_calls(adb)), 2)    # 仅初始 resolve + 失效后

    def test_unknown_streak_resets_on_confirm(self):
        # 未知累计被"确认"打断 → 计数清零，不会跨周期累积误判失效
        r, adb = self._resolver({})
        # 未知 1、2 → 确认（清零）→ 未知 1、2：五次校验都应沿用旧 pid
        seq = [
            {"cat /proc/5838/comm": "surfaceflinger\n"},
            {"cat /proc/5838/comm": "surfaceflinger\n"},
            {"cat /proc/5838/cmdline": "com.tencent.mm:appbrand0\x00"},
            {"cat /proc/5838/comm": "surfaceflinger\n"},
            {"cat /proc/5838/comm": "surfaceflinger\n"},
        ]
        for i, extra in enumerate(seq):
            adb.responses.update(extra)
            self.assertEqual(r.current_pid(ts=1.0 + i * 10.0), 5838,
                             f"第 {i + 1} 次校验应沿用旧 pid")
        self.assertEqual(len(self._ps_calls(adb)), 1)    # 全程未失效


class TestMemNoPackageFallback(unittest.TestCase):
    """pid 为 None 时不得回退包名维度（2026-09-11）。

    dumpsys meminfo <package> 对多进程应用返回全部进程合计（微信可差一个
    量级），曲线上表现为假突跳；pid 缺失时宁可缺数，不采错数。
    """

    def test_pid_none_returns_empty_without_package_fallback(self):
        adb = MockAdb({"dumpsys meminfo": "TOTAL PSS: 999999 kB"})   # 若回退包名会被采到
        c = MemCollector(adb, MockResolver(None), package="com.tencent.mm")
        r = c.sample(1.0)
        self.assertIsNone(r["pss_kb"])
        self.assertIsNone(r["vmrss_kb"])
        self.assertEqual([x for x in adb.calls if "meminfo" in " ".join(x)], [])

    def test_pid_present_still_uses_smaps_rollup(self):
        # 有 pid 时行为不变：smaps_rollup 优先，同源双值
        adb = MockAdb({"cat /proc/5838/smaps_rollup": "Rss:  100000 kB\nPss:  90000 kB\n"})
        c = MemCollector(adb, MockResolver(5838), package="com.tencent.mm")
        r = c.sample(1.0)
        self.assertEqual(r["pss_kb"], 90000)
        self.assertEqual(r["vmrss_kb"], 100000)


class TestScriptSafeJson(unittest.TestCase):
    """自包含报告内联 JSON 的 </script> 注入防护（2026-09-11）。"""

    def test_closing_script_tag_escaped_and_roundtrip(self):
        rows = [{"t_ms": 0, "log": "</script><script>alert(1)</script>"}]
        s = script_safe_json(rows, ensure_ascii=False)
        self.assertNotIn("</script>", s)
        self.assertIn("<\\/script>", s)
        self.assertEqual(json.loads(s)[0]["log"], rows[0]["log"])   # JSON 无损

    def test_normal_data_untouched(self):
        rows = [{"t_ms": 0, "fps": {"fps": 59.9}, "note": "a<b>c</b>"}]
        self.assertEqual(json.loads(script_safe_json(rows, ensure_ascii=False)), rows)


class RaisingAdb:
    """--list 必抛的 adb 替身：模拟主机 adb 链路瞬断（2026-09-11 事故形态）。"""

    def shell(self, args):
        raise RuntimeError("adb: device 'X' not found")


class TestFpsProbeFail(unittest.TestCase):
    """区分"读失败"与"真的没有层"（2026-09-11 事故最高优先修复）。

    事故：run 20260911_162353 主机 adb 通道瞬时失败，resolve_layer 的
    `except: return None` 与"无匹配层"混用，51/63 点误报 no_layer
    （"游戏不在前台"），层与进程实际全程都在，事后不可判读。
    """

    L = "SurfaceView[com.tencent.mm:appbrand0/AppUI]#776(BLAST)"

    def _collector(self, adb):
        return FpsCollector(adb, "com.tencent.mm", "appbrand", retry_interval=0.0)

    def test_list_raise_reports_probe_fail(self):
        # --list 抛异常（链路瞬断）→ probe_fail，不得报 no_layer
        c = self._collector(RaisingAdb())
        r = c.sample(1.0)
        self.assertEqual(r["error"], "probe_fail")
        self.assertIn("链路抖动", r["hint"])

    def test_list_empty_output_reports_probe_fail(self):
        # --list 执行成功但输出全空（正常时永远有系统层）→ probe_fail
        adb = SfMockAdb(None, "")          # layer=None → --list 返回空
        c = self._collector(adb)
        r = c.sample(1.0)
        self.assertEqual(r["error"], "probe_fail")

    def test_list_ok_but_no_match_reports_no_layer(self):
        # --list 正常返回、层名不含目标包/模式 → 保持 no_layer（语义兼容不变）
        adb = SfMockAdb("com.other.app/MainActivity#9", "")
        c = self._collector(adb)
        r = c.sample(1.0)
        self.assertEqual(r["error"], "no_layer")

    def test_resolve_layer_old_signature_compat(self):
        # 旧签名 resolve_layer() 仍只返回层名：异常 → None；正常 → 层名
        self.assertIsNone(self._collector(RaisingAdb()).resolve_layer())
        adb = SfMockAdb(self.L, "")
        self.assertEqual(self._collector(adb).resolve_layer(), self.L)


class TestAdbTransientRetry(unittest.TestCase):
    """adb shell 瞬时通道错误轻量重试（2026-09-11 事故修复）。

    默认重试 1 次（间隔 ~0.15s）：shell() 被多线程高频共用，多次重试会放大
    设备负载。命令本身失败与超时不重试（超时重试单点最坏耗时翻倍到 40s）。
    """

    def _adb(self):
        c = Adb.__new__(Adb)           # 跳过 __init__（无需真设备）
        c._base = ["adb"]
        c.serial = "TEST"
        return c

    def test_transient_error_retried_once_then_ok(self):
        adb = self._adb()
        calls = {"n": 0}

        def fake_run(args, timeout=20):
            calls["n"] += 1
            if calls["n"] == 1:
                raise AdbError("adb 命令失败: adb shell error: closed")
            return "ok"

        adb._run = fake_run
        self.assertEqual(adb.shell(["echo", "hi"]), "ok")
        self.assertEqual(calls["n"], 2)          # 失败 1 次 + 重试成功

    def test_transient_always_raises_after_exhausting_retry(self):
        adb = self._adb()
        calls = {"n": 0}

        def fake_run(args, timeout=20):
            calls["n"] += 1
            raise AdbError("adb 命令失败: device offline")

        adb._run = fake_run
        with self.assertRaises(AdbError):
            adb.shell(["echo", "hi"])
        self.assertEqual(calls["n"], 2)          # 1 + 默认重试 1 次

    def test_command_failure_not_retried(self):
        # 命令本身失败（Permission denied）→ 重试无意义，立即抛
        adb = self._adb()
        calls = {"n": 0}

        def fake_run(args, timeout=20):
            calls["n"] += 1
            raise AdbError("adb 命令失败: Permission denied")

        adb._run = fake_run
        with self.assertRaises(AdbError):
            adb.shell(["cat", "/proc/1/smaps"])
        self.assertEqual(calls["n"], 1)

    def test_timeout_not_retried(self):
        # 超时（20s）重试会让单点耗时翻倍 → 不重试，直接抛
        adb = self._adb()
        calls = {"n": 0}

        def fake_run(args, timeout=20):
            calls["n"] += 1
            raise subprocess.TimeoutExpired(cmd="adb", timeout=20)

        adb._run = fake_run
        with self.assertRaises(subprocess.TimeoutExpired):
            adb.shell(["dumpsys", "SurfaceFlinger", "--list"])
        self.assertEqual(calls["n"], 1)

    def test_retries_zero_disables_retry(self):
        adb = self._adb()
        calls = {"n": 0}

        def fake_run(args, timeout=20):
            calls["n"] += 1
            raise AdbError("adb 命令失败: error: closed")

        adb._run = fake_run
        with self.assertRaises(AdbError):
            adb.shell(["echo", "hi"], retries=0)
        self.assertEqual(calls["n"], 1)


class TestChannelAlertTracker(unittest.TestCase):
    """缺数/断连事件状态机（2026-09-11 事故复盘）：状态沿触发、去重不刷屏。"""

    def test_below_threshold_no_events(self):
        t = ChannelAlertTracker()
        self.assertEqual(t.update(["no_layer", "no_layer"]), [])
        self.assertEqual(t.update([]), [])

    def test_missing_metric_enter_dedup_recover(self):
        t = ChannelAlertTracker()
        ev = t.update(["no_layer", "no_layer", "read_fail"])
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["kind"], "missing_metric")
        self.assertEqual(ev[0]["detail"]["err_codes"], {"no_layer": 2, "read_fail": 1})
        # 状态沿去重：持续缺数不重复发事件
        self.assertEqual(t.update(["no_layer", "no_layer", "read_fail"]), [])
        rec = t.update(["no_layer"])
        self.assertEqual([e["kind"] for e in rec], ["recovered"])
        self.assertEqual(rec[0]["detail"]["scope"], "missing")

    def test_disconnect_suppresses_missing_and_recovers(self):
        t = ChannelAlertTracker()
        ev = t.update(["no_layer"] * 4)
        self.assertEqual([e["kind"] for e in ev], ["disconnect"])  # ≥4 不发 missing
        self.assertEqual(t.update(["no_layer"] * 4), [])           # 沿不重发
        rec = t.update([])
        self.assertEqual([e["kind"] for e in rec], ["recovered"])

    def test_escalation_missing_to_disconnect(self):
        t = ChannelAlertTracker()
        self.assertEqual([e["kind"] for e in t.update(["probe_fail"] * 3)],
                         ["missing_metric"])
        self.assertEqual([e["kind"] for e in t.update(["probe_fail"] * 4)],
                         ["disconnect"])
        rec = t.update([])
        self.assertEqual(sorted(e["detail"]["scope"] for e in rec),
                         ["disconnect", "missing"])


class TestRowHasAnyValue(unittest.TestCase):
    """首点落盘门槛（2026-09-11）：全空行跳过，error 行必须留痕。"""

    def test_empty_bootstrap_row_skipped(self):
        # 各指标线程尚未产出首份快照的全空行 → False（首点门槛跳过它）
        self.assertFalse(row_has_any_value(
            {"ts": 1.0, "t_ms": 400.0, "target": "com.tencent.mm"}))

    def test_error_row_counts_as_value(self):
        # error 也是信息（链路抖动首批 probe_fail 必须留痕，不能丢）
        self.assertTrue(row_has_any_value({"fps": {"error": "probe_fail", "fps": None}}))

    def test_value_and_throttled_rows(self):
        self.assertTrue(row_has_any_value({"fps": {"fps": 60.0}}))
        self.assertTrue(row_has_any_value({"mem": {"pss_kb": 100, "pid": 1}}))
        # throttled（未到采样间隔的空点）不是有效值
        self.assertFalse(row_has_any_value(
            {"mem": {"pid": None, "pss_kb": None, "vmrss_kb": None, "throttled": True}}))


if __name__ == "__main__":
    unittest.main()
