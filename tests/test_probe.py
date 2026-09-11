# -*- coding: utf-8 -*-
"""启动期轻量探测（probe.py）单元测试（2026-09-11）。

覆盖：appbrand 索引提取、层名解析与挑选（排除输入层/手势层）、ps 候选解析、
/proc 字段解析（含荣耀真机 comm 形态）、VmRSS 多文件 grep 解析、推荐优先级、
以及 probe_once 的端到端（假 adb）。

样例数据取自 2026-09-11 荣耀 ADT-AN00 真机实测（appbrand0/1/2 并存那一次）。
"""

import os
import sys
import unittest

_COLLECTOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "collector")
if _COLLECTOR not in sys.path:
    sys.path.insert(0, _COLLECTOR)

from probe import (appbrand_index, layer_appbrand_index, parse_ps_candidates,
                   pick_game_layer, parse_stat_ticks, parse_vmrss_kb, recommend,
                   probe_once)

PS_SAMPLE = """  PID ARGS
    1 init second_stage
 11295 com.tencent.mm:appbrand0
 20621 com.tencent.mm:appbrand1
 22275 com.tencent.mm:appbrand2
 25047 com.tencent.mm
 25241 com.tencent.mm:push
 20998 com.tencent.mm:xweb_privileged_process_0
"""

STAT_SAMPLE_0 = ("11295 (nt.mm:appbrand0) S 1039 1039 0 0 -1 1077936448 690412 642 2279 0 "
                 "8108 5273 1 1 20 0 142 0 199406559 255781146624 48989 "
                 "18446744073709551615 1 1 0 0 0 0 4608 4097 1073775868 0 0 0 17 2 0 0 12 0 0 0 0 0 0 0 0 0 0 0")
STAT_SAMPLE_1 = ("20621 (nt.mm:appbrand1) S 1039 1039 0 0 -1 1077936448 690412 642 2279 0 "
                 "53985 7025 1 1 20 0 142 0 199406559 255781146624 48989 "
                 "18446744073709551615 1 1 0 0 0 0 4608 4097 1073775868 0 0 0 17 2 0 0 12 0 0 0 0 0 0 0 0 0 0 0")

LAYERS_SAMPLE = """1956308 ActivityRecordInputSink com.tencent.mm/.plugin.appbrand.ui.AppBrandUI1#18626
ActivityRecord{f1810ab u0 com.tencent.mm/.plugin.appbrand.ui.AppBrandUI1 t243}#18618
Background for SurfaceView[com.tencent.mm/com.tencent.mm.plugin.appbrand.ui.AppBrandUI1]#18656
SurfaceView[com.tencent.mm/com.tencent.mm.plugin.appbrand.ui.AppBrandUI1]#18654
SurfaceView[com.tencent.mm/com.tencent.mm.plugin.appbrand.ui.AppBrandUI1](BLAST)#18655
SurfaceView[GestureNavLeft](BLAST)#18488
SurfaceView[GestureNavBottom](BLAST)#18457
"""


class TestIndexExtraction(unittest.TestCase):
    def test_appbrand_index(self):
        self.assertEqual(appbrand_index("com.tencent.mm:appbrand0"), 0)
        self.assertEqual(appbrand_index("com.tencent.mm:appbrand1"), 1)
        self.assertEqual(appbrand_index("com.tencent.mm:appbrand12"), 12)
        self.assertIsNone(appbrand_index("com.tencent.mm"))
        self.assertIsNone(appbrand_index("com.tencent.mm:push"))
        self.assertIsNone(appbrand_index(None))

    def test_layer_appbrand_index(self):
        self.assertEqual(layer_appbrand_index(
            "SurfaceView[com.tencent.mm/com.tencent.mm.plugin.appbrand.ui.AppBrandUI1](BLAST)#18655"), 1)
        self.assertIsNone(layer_appbrand_index("SurfaceView[GestureNavLeft](BLAST)#18488"))


class TestParsePs(unittest.TestCase):
    def test_only_appbrand_matched_and_sorted(self):
        cands = parse_ps_candidates(PS_SAMPLE, "appbrand")
        self.assertEqual([c[0] for c in cands], [11295, 20621, 22275])
        self.assertEqual([c[2] for c in cands], [0, 1, 2])

    def test_main_process_not_matched_when_pattern_appbrand(self):
        cands = parse_ps_candidates(PS_SAMPLE, "appbrand")
        names = [c[1] for c in cands]
        self.assertNotIn("com.tencent.mm", names)
        self.assertNotIn("com.tencent.mm:push", names)

    def test_header_and_junk_skipped(self):
        cands = parse_ps_candidates("  PID ARGS\njunk line\n 11295 com.tencent.mm:appbrand0\n",
                                    "appbrand")
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0][0], 11295)


class TestPickGameLayer(unittest.TestCase):
    def test_picks_blast_appbrand_surfaceview(self):
        name, idx = pick_game_layer(LAYERS_SAMPLE)
        self.assertIn("SurfaceView[com.tencent.mm", name)
        self.assertIn("(BLAST)", name)
        self.assertEqual(idx, 1)

    def test_input_sink_and_gesture_excluded(self):
        name, _ = pick_game_layer(LAYERS_SAMPLE)
        self.assertNotIn("ActivityRecordInputSink", name)
        self.assertNotIn("GestureNav", name)
        self.assertNotIn("Background for", name)

    def test_no_layer(self):
        self.assertEqual(pick_game_layer(""), (None, None))
        self.assertEqual(pick_game_layer("SurfaceView[GestureNavLeft](BLAST)#1"), (None, None))


class TestParseStatAndRss(unittest.TestCase):
    def test_ticks_parsed_from_real_sample(self):
        # 荣耀真机：comm 为 "nt.mm:appbrand0"（末 15 字符形态）；utime=8108 stime=5273
        self.assertEqual(parse_stat_ticks(STAT_SAMPLE_0), 13381)
        self.assertEqual(parse_stat_ticks(STAT_SAMPLE_1), 61010)

    def test_ticks_bad_input(self):
        self.assertIsNone(parse_stat_ticks(""))
        self.assertIsNone(parse_stat_ticks("11295 no-paren-here"))
        self.assertIsNone(parse_stat_ticks("11295 (x) S 1 2"))

    def test_vmrss_multi_and_single_file(self):
        out = ("/proc/11295/status:VmRSS:\t 211380 kB\n"
               "/proc/20621/status:VmRSS:\t1162752 kB\n")
        self.assertEqual(parse_vmrss_kb(out, 11295), 211380)
        self.assertEqual(parse_vmrss_kb(out, 20621), 1162752)
        self.assertEqual(parse_vmrss_kb("VmRSS:\t 500 kB", 9), 500)
        self.assertIsNone(parse_vmrss_kb(out, 99999))


class TestRecommend(unittest.TestCase):
    CANDS = [(11295, "com.tencent.mm:appbrand0", 0),
             (20621, "com.tencent.mm:appbrand1", 1),
             (22275, "com.tencent.mm:appbrand2", 2)]

    def test_layer_match_wins_over_busy_cpu(self):
        # 层指向 appbrand1；即便 appbrand0 增量更大，也应推荐与层匹配的 appbrand1
        pid, reason, _ = recommend(self.CANDS, {11295: 500, 20621: 10}, layer_index=1)
        self.assertEqual(pid, 20621)
        self.assertEqual(reason, "layer_match")

    def test_busy_cpu_when_no_layer(self):
        pid, reason, _ = recommend(self.CANDS, {11295: 5, 20621: 300, 22275: 0}, layer_index=None)
        self.assertEqual(pid, 20621)
        self.assertEqual(reason, "busy_cpu")

    def test_fallback_first_when_no_delta_data(self):
        # 完全没有增量数据（探测失败）→ 取第一候选，reason=first
        pid, reason, _ = recommend(self.CANDS, {}, layer_index=None)
        self.assertEqual(pid, 11295)
        self.assertEqual(reason, "first")

    def test_zero_delta_uses_busy_cpu_branch(self):
        # 有增量数据但全为 0 → 走 busy_cpu 分支（取第一候选）
        pid, reason, _ = recommend(self.CANDS, {11295: 0, 20621: 0, 22275: 0}, layer_index=None)
        self.assertEqual(pid, 11295)
        self.assertEqual(reason, "busy_cpu")

    def test_empty(self):
        self.assertEqual(recommend([], {}, None), (None, None, ""))


class _FakeAdb:
    """假 adb：按命令返回预设输出，并记录调用（验证探测不写文件、只读）。"""

    def __init__(self):
        self.calls = []

    def shell(self, args):
        self.calls.append(list(args))
        if args[:3] == ["ps", "-A", "-o"]:
            return PS_SAMPLE
        if args[:1] == ["cat"]:
            if len(args) == 3 and args[1].endswith("11295/stat"):
                return STAT_SAMPLE_0
            return "\n".join([STAT_SAMPLE_0, STAT_SAMPLE_1])
        if args[:1] == ["grep"]:
            return "/proc/11295/status:VmRSS:\t 211380 kB\n/proc/20621/status:VmRSS:\t1162752 kB\n"
        if "SurfaceFlinger" in args:
            return LAYERS_SAMPLE
        return ""


class TestProbeOnce(unittest.TestCase):
    def test_probe_recommends_layer_matched_instance(self):
        adb = _FakeAdb()
        res = probe_once(adb, "appbrand", sample_gap=0)
        self.assertTrue(res["ok"])
        self.assertEqual(res["layer"]["appbrand_index"], 1)
        self.assertEqual(res["recommended_pid"], 20621)          # 层匹配 appbrand1
        rec = [c for c in res["candidates"] if c["recommended"]]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["index"], 1)
        self.assertEqual(rec[0]["rss_mb"], round(1162752 / 1024.0, 1))

    def test_probe_no_candidates_message(self):
        class _Empty(_FakeAdb):
            def shell(self, args):
                self.calls.append(list(args))
                return "  PID ARGS\n    1 init\n"
        res = probe_once(_Empty(), "appbrand", sample_gap=0)
        self.assertFalse(res["ok"])
        self.assertIn("未找到匹配进程", res["error"])


if __name__ == "__main__":
    unittest.main()
