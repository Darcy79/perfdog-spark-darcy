# -*- coding: utf-8 -*-
"""v64：内存 swap PSS 相关修复的单元测试。

背景（2026-09-11 真机实测）：dumpsys meminfo 的 **TOTAL PSS 含 swap 部分**，
进程被换出时会出现 PSS > RSS（appbrand0：PSS 231242 / RSS 211380 / SWAP PSS 141456），
被 data_health 的 rss_lt_pss 规则误判为"内存解析异常"（当时 172/172 点全命中）。
修复：meminfo 解析并落盘 swap_pss_kb，规则改用"非 swap PSS = pss - swap"与 RSS 比较；
swap 缺失时保持原口径（老数据仍能复检出真正的双源倒挂）。
"""

import os
import sys
import unittest

_COLLECTOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "collector")
if _COLLECTOR not in sys.path:
    sys.path.insert(0, _COLLECTOR)

from metrics.mem import parse_meminfo
from data_health import check_row_health

# 荣耀 ADT-AN00 / Android 14 实测行（App Summary 段）
MEMINFO_WITH_SWAP = (
    "App Summary\n"
    "                       Pss(KB)           Rss(KB)\n"
    "           TOTAL PSS:   231242           TOTAL RSS:   211380"
    "       TOTAL SWAP PSS:   141456\n"
)


class TestMeminfoSwapParsing(unittest.TestCase):
    def test_parse_swap_pss(self):
        d = parse_meminfo(MEMINFO_WITH_SWAP)
        self.assertEqual(d["pss_kb"], 231242)
        self.assertEqual(d["rss_kb"], 211380)
        self.assertEqual(d["swap_pss_kb"], 141456)

    def test_old_format_without_swap(self):
        d = parse_meminfo("TOTAL PSS: 1000   TOTAL RSS: 2000")
        self.assertEqual(d["pss_kb"], 1000)
        self.assertEqual(d["rss_kb"], 2000)
        self.assertNotIn("swap_pss_kb", d)

    def test_thousand_separators(self):
        d = parse_meminfo("TOTAL PSS: 1,035,724  TOTAL RSS: 1,162,752  TOTAL SWAP PSS: 65,738")
        self.assertEqual(d["swap_pss_kb"], 65738)


class TestRssLtPssRule(unittest.TestCase):
    @staticmethod
    def _row(pss, rss, swap=None):
        mem = {"pss_kb": pss, "vmrss_kb": rss}
        if swap is not None:
            mem["swap_pss_kb"] = swap
        return {"t_ms": 1000, "mem": mem}

    def test_swapped_out_process_not_flagged(self):
        # 真机形态：PSS 231242 > RSS 211380，但含 swap 141456 → 非 swap PSS 89806 < RSS → 正常
        self.assertEqual(check_row_health(self._row(231242, 211380, 141456)), [])

    def test_real_inversion_still_flagged(self):
        # 无 swap 的真倒挂（旧双源解析错位形态）→ 必须仍然报
        self.assertIn("内存解析异常（RSS<PSS）", check_row_health(self._row(200000, 150000, 0)))

    def test_missing_swap_field_backward_compatible(self):
        # 老数据无 swap 字段 → 按原口径判定（仍报），保证历史数据复检能力
        self.assertIn("内存解析异常（RSS<PSS）", check_row_health(self._row(200000, 150000)))

    def test_inversion_beyond_swap_still_flagged(self):
        # 扣掉 swap 后仍倒挂（300000-100000=200000 > RSS 150000）→ 报
        self.assertIn("内存解析异常（RSS<PSS）", check_row_health(self._row(300000, 150000, 100000)))

    def test_normal_case_untouched(self):
        # 非 swap PSS ≤ RSS 的常规样本不受影响
        self.assertEqual(check_row_health(self._row(800000, 900000)), [])


if __name__ == "__main__":
    unittest.main()
