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
