# -*- coding: utf-8 -*-
"""设备信息探测/解析单测（2026-08-27）。

覆盖 parse_device_info（合并 shell 输出解析）、parse_wm_size、市场名映射兜底、
_mhz_from_khz。probe_device_info 需真机，仅验证解析纯函数。
"""

import os
import sys
import unittest

_COLLECTOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "collector")
if _COLLECTOR not in sys.path:
    sys.path.insert(0, _COLLECTOR)

from device_info import parse_device_info, parse_wm_size, _mhz_from_khz, _market_name, MODEL_MARKET_MAP


class TestParseDeviceInfo(unittest.TestCase):
    def test_full_output(self):
        # 模拟荣耀 Magic3 Pro 实测输出
        out = (
            "ADT-AN00\n__PDDEV__\n\n__PDDEV__\nADT-AN00\n__PDDEV__\nlahaina\n__PDDEV__\n"
            "Hardware\t: SM8350\n__PDDEV__\nPhysical size: 1080x2388\n__PDDEV__\n1804800"
        )
        d = parse_device_info(out)
        self.assertEqual(d["model"], "ADT-AN00")
        self.assertEqual(d["market_name"], "Magic3 Pro")   # 映射表兜底
        self.assertEqual(d["board_platform"], "lahaina")
        self.assertEqual(d["cpu_hardware"], "SM8350")
        self.assertEqual(d["screen_resolution"], "1080×2388")
        self.assertEqual(d["cpu_max_freq_mhz"], "1804.8")

    def test_marketname_preferred_over_map(self):
        out = (
            "XYZ-001\n__PDDEV__\nMyPhone 1\n__PDDEV__\nXYZ-001\n__PDDEV__\nplatform\n"
            "__PDDEV__\nHardware : ABC\n__PDDEV__\nPhysical size: 720x1280\n__PDDEV__\n1000000"
        )
        d = parse_device_info(out)
        self.assertEqual(d["market_name"], "MyPhone 1")   # 厂商写了 marketname 优先

    def test_missing_fields_are_none(self):
        d = parse_device_info("")
        self.assertIsNone(d["model"])
        self.assertIsNone(d["market_name"])
        self.assertIsNone(d["screen_resolution"])
        self.assertIsNone(d["cpu_max_freq_mhz"])

    def test_short_output_padded(self):
        # 只有 model，其余段缺失 → 不应崩，后面字段 None
        d = parse_device_info("ADT-AN00")
        self.assertEqual(d["model"], "ADT-AN00")
        self.assertIsNone(d["cpu_max_freq_mhz"])


class TestHelpers(unittest.TestCase):
    def test_wm_size(self):
        self.assertEqual(parse_wm_size("Physical size: 1080x2388"), "1080×2388")
        self.assertIsNone(parse_wm_size("Override size: 720x1280"))
        self.assertIsNone(parse_wm_size(""))

    def test_mhz_from_khz(self):
        self.assertEqual(_mhz_from_khz("1804800"), "1804.8")
        self.assertEqual(_mhz_from_khz("1000000"), "1000.0")
        self.assertIsNone(_mhz_from_khz("0"))
        self.assertIsNone(_mhz_from_khz("abc"))

    def test_market_name_map(self):
        self.assertEqual(_market_name("ADT-AN00", None), "Magic3 Pro")
        self.assertEqual(_market_name("ADT-AN00", "Honor Magic3 Pro"), "Honor Magic3 Pro")
        self.assertIsNone(_market_name("UNKNOWN-999", None))
        self.assertIn("PLA-AL10", MODEL_MARKET_MAP)   # 华为 Mate 70 Pro+ 在表


if __name__ == "__main__":
    unittest.main()
