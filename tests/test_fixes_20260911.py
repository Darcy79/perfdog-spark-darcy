# -*- coding: utf-8 -*-
"""v61 三项加固的单元测试：POST 同源校验 / 断连退避 / 导出新增质量列。

背景（2026-09-11，智谱评估报告遗留项 + 用户长测前收尾）：
  1. web.py 的破坏性 POST（/api/stop、/api/shutdown、/api/switch-target）此前
     无 Origin/Host 校验，浏览器内任意网页可 no-cors 静默触发；
  2. main.py 断连告警需连续 10 轮失败，adb 半死时最坏 3 分钟才提示，且期间
     主循环仍按 1s 空转猛撞 20s 超时；
  3. fps.py v59 起落盘的 fps_clamped / fps_warn 未进入 CSV/XLSX 导出列。

运行（项目根目录）：
    uv run --no-project python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from collections import OrderedDict

# 注入 collector 目录到 sys.path（main.py 以 collector 为运行根）
_COLLECTOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "collector")
if _COLLECTOR not in sys.path:
    sys.path.insert(0, _COLLECTOR)

from web import same_origin_ok, trim_report_cache
from main import backoff_sleep, FAIL_ALERT_STREAK, BACKOFF_MAX_S
from export_report import COLUMNS, flatten, export_csv


class TestSameOriginOk(unittest.TestCase):
    """POST 同源校验：回环 Host + 非跨站 Origin 放行；无 Origin 视为非浏览器调用。"""

    def test_loopback_hosts_allowed(self):
        for host in ("localhost", "localhost:8080", "127.0.0.1", "127.0.0.1:8080",
                     "[::1]:8080", "::1"):
            self.assertTrue(same_origin_ok(host, None), host)

    def test_remote_host_rejected(self):
        for host in ("evil.com", "192.168.1.10:8080", "localhost.evil.com",
                     "[2001:db8::1]:80"):
            self.assertFalse(same_origin_ok(host, None), host)

    def test_same_site_origin_allowed(self):
        for origin in ("http://localhost:8080", "http://127.0.0.1",
                       "https://localhost", "http://[::1]:8080"):
            self.assertTrue(same_origin_ok("127.0.0.1:8080", origin), origin)

    def test_cross_site_origin_rejected(self):
        # 浏览器跨站请求会带 Origin，"null"（sandbox/跨源重定向）同样拒绝
        for origin in ("http://evil.com", "https://attacker.example", "null"):
            self.assertFalse(same_origin_ok("127.0.0.1:8080", origin), origin)

    def test_malformed_origin_rejected(self):
        self.assertFalse(same_origin_ok("127.0.0.1", "http://[::1"))

    def test_missing_host_allowed(self):
        # 极简客户端（HTTP/1.0、脚本）可能不带 Host；无 Origin 说明非浏览器跨站场景
        self.assertTrue(same_origin_ok("", None))
        self.assertTrue(same_origin_ok(None, None))


class TestBackoffSleep(unittest.TestCase):
    """断连退避：阈值内保持正常节奏，超阈值线性退避并封顶。"""

    def test_normal_interval(self):
        self.assertAlmostEqual(backoff_sleep(1.0, 0), 1.0)
        self.assertAlmostEqual(backoff_sleep(1.0, FAIL_ALERT_STREAK - 1), 1.0)

    def test_backoff_scales_with_streak(self):
        self.assertAlmostEqual(backoff_sleep(1.0, 3), 3.0)
        self.assertAlmostEqual(backoff_sleep(1.0, 4), 4.0)
        self.assertAlmostEqual(backoff_sleep(0.5, 3), 1.5)

    def test_backoff_capped(self):
        self.assertAlmostEqual(backoff_sleep(1.0, 50), BACKOFF_MAX_S)
        self.assertAlmostEqual(backoff_sleep(2.0, 10), BACKOFF_MAX_S)

    def test_base_floor(self):
        self.assertAlmostEqual(backoff_sleep(0.0, 0), 0.05)
        self.assertAlmostEqual(backoff_sleep(None, 0), 0.05)

    def test_alert_threshold_is_three(self):
        # 防回退：阈值必须是 3（此前 10 轮 × 20s 超时 → 最坏 3 分钟才告警）
        self.assertEqual(FAIL_ALERT_STREAK, 3)


class TestExportQualityColumns(unittest.TestCase):
    """导出质量列：fps_clamped / fps_warn 追加在列尾，原有列序不变。"""

    def test_columns_appended_at_end(self):
        keys = [k for k, _ in COLUMNS]
        self.assertEqual(keys[-2:], ["fps_clamped", "fps_warn"])
        self.assertIn("fps_source", keys)          # 原有列仍在
        self.assertEqual(keys[0], "t_ms")

    def test_flatten_marks_present(self):
        row = {"t_ms": 1000, "fps": {"fps": 60.0, "refresh_hz": 60, "layer": "L#1",
                                     "fps_clamped": True, "fps_warn": "low_frames"}}
        out = flatten(row)
        self.assertEqual(out["fps_clamped"], "是")
        self.assertEqual(out["fps_warn"], "low_frames")

    def test_flatten_normal_point_blank(self):
        row = {"t_ms": 2000, "fps": {"fps": 59.9, "refresh_hz": 60, "layer": "L#1"}}
        out = flatten(row)
        self.assertEqual(out["fps_clamped"], "")
        self.assertEqual(out["fps_warn"], "")
        self.assertEqual(out["fps_source"], "sf")   # 原判定逻辑未受影响

    def test_csv_header_and_row_include_quality_columns(self):
        rows = [{"t_ms": 0, "fps": {"fps": 60.0, "refresh_hz": 60, "layer": "L#1",
                                    "fps_clamped": True}}]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "out.csv")
            n = export_csv(rows, p)
            self.assertEqual(n, 1)
            with open(p, encoding="utf-8-sig") as f:
                header = f.readline().strip()
                data = f.readline().strip()
        self.assertIn("FPS已钳制", header)
        self.assertIn("FPS低置信", header)
        self.assertIn("是", data)


class TestReportCacheBudget(unittest.TestCase):
    """报告缓存双预算：份数上限 + 采样点总数上限（v62，长测内存防护）。"""

    @staticmethod
    def _entry(n_points):
        return (1, 2, [{"t_ms": i} for i in range(n_points)])

    def test_evict_by_entry_count(self):
        cache = OrderedDict()
        for i in range(5):
            cache["run%d" % i] = self._entry(10)
        points = trim_report_cache(cache, 50, max_entries=3, max_points=100000)
        self.assertEqual(len(cache), 3)
        self.assertEqual(points, 30)
        self.assertNotIn("run0", cache)      # LRU：最旧的先淘汰
        self.assertIn("run4", cache)

    def test_evict_by_points_budget(self):
        cache = OrderedDict()
        for i in range(3):
            cache["run%d" % i] = self._entry(3000)     # 共 9000 点
        points = trim_report_cache(cache, 9000, max_entries=50, max_points=5000)
        self.assertLessEqual(points, 5000)
        self.assertEqual(len(cache), 1)      # 淘汰到只剩最新那份
        self.assertIn("run2", cache)

    def test_single_oversized_entry_kept(self):
        # 正在看的这份即使单份超预算也不淘汰自己（否则会"打开→立刻淘汰→再解析"死循环）
        cache = OrderedDict()
        cache["big"] = self._entry(60000)
        points = trim_report_cache(cache, 60000, max_entries=50, max_points=50000)
        self.assertEqual(len(cache), 1)
        self.assertEqual(points, 60000)

    def test_no_eviction_when_within_budget(self):
        cache = OrderedDict()
        cache["a"] = self._entry(100)
        points = trim_report_cache(cache, 100, max_entries=50, max_points=50000)
        self.assertEqual(len(cache), 1)
        self.assertEqual(points, 100)

    def test_empty_cache_noop(self):
        cache = OrderedDict()
        self.assertEqual(trim_report_cache(cache, 0, 3, 100), 0)


if __name__ == "__main__":
    unittest.main()
