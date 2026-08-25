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

from metrics.fps import FpsCollector, MAX_VALID_TS
from metrics.mem import parse_smaps_rollup, parse_meminfo
from metrics.cpu import CpuCollector
from metrics.thermal import ThermalCollector
from pidresolver import PidResolver
from export_report import COLUMNS, flatten


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


if __name__ == "__main__":
    unittest.main()
