# -*- coding: utf-8 -*-
"""数据健全性自检单测（2026-08-27）。

覆盖 data_health 各规则：采错进程 / 假 Jank / RSS<PSS / 量级突变 / 连续缺失，
以及 health_summary 摘要。阈值经 4 份真实数据调校（见模块 docstring）。
"""

import os
import sys
import unittest

_COLLECTOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "collector")
if _COLLECTOR not in sys.path:
    sys.path.insert(0, _COLLECTOR)

from data_health import (check_row_health, scan_rows, health_summary, check_rows_live,
                         WRONG_PROC_MIN_FPS, WRONG_PROC_CPU_NEAR_ZERO, WRONG_PROC_FRACTION)


def _row(t_ms, fps=None, cpu=None, pss=None, rss=None, jank=None, p50=None):
    r = {"t_ms": t_ms}
    if fps is not None or jank is not None or p50 is not None:
        f = {}
        if fps is not None:
            f["fps"] = fps
        if jank is not None:
            f["jank_rate"] = jank
        if p50 is not None:
            f["frame_p50_ms"] = p50
        r["fps"] = f
    if cpu is not None:
        r["cpu"] = {"cpu_proc_pct": cpu}
    if pss is not None or rss is not None:
        m = {}
        if pss is not None:
            m["pss_kb"] = pss
        if rss is not None:
            m["vmrss_kb"] = rss
        r["mem"] = m
    return r


class TestCheckRowHealth(unittest.TestCase):
    def test_rss_lt_pss_flagged_only_when_meaningful(self):
        # 缺口 5%（500/10000）→ 判异常
        self.assertIn("内存解析异常（RSS<PSS）", check_row_health(
            {"t_ms": 0, "mem": {"pss_kb": 10000, "vmrss_kb": 9500}}))
        # 缺口 0.5%（50/10000）→ 舍入噪声，不判
        self.assertEqual(check_row_health(
            {"t_ms": 0, "mem": {"pss_kb": 10000, "vmrss_kb": 9950}}), [])
        # 正常 RSS>=PSS 不判
        self.assertEqual(check_row_health(
            {"t_ms": 0, "mem": {"pss_kb": 10000, "vmrss_kb": 12000}}), [])

    def test_event_row_skipped(self):
        self.assertEqual(check_row_health({"event": "meta", "cores": 8}), [])


class TestScanRowsWrongProcess(unittest.TestCase):
    def test_wrong_process_detected(self):
        # 渲染中（fps 高）但进程 CPU 大量近 0 → 采错进程
        rows = []
        for i in range(100):
            cpu = 0.5 if i % 3 else 0.0   # 1/3 近零，符合阈值
            rows.append(_row(i * 1000, fps=58.0, cpu=cpu, pss=100000, rss=120000))
        issues = scan_rows(rows)
        self.assertTrue(any(it["type"] == "wrong_process" for it in issues))

    def test_normal_high_cpu_not_flagged(self):
        rows = [_row(i * 1000, fps=58.0, cpu=120.0 + i % 10, pss=100000, rss=120000)
                for i in range(100)]
        issues = scan_rows(rows)
        self.assertFalse(any(it["type"] == "wrong_process" for it in issues))

    def test_low_fps_not_flagged(self):
        # fps 低（可能真不渲染）→ 不判采错进程
        rows = [_row(i * 1000, fps=5.0, cpu=0.0) for i in range(100)]
        issues = scan_rows(rows)
        self.assertFalse(any(it["type"] == "wrong_process" for it in issues))


class TestScanRowsFakeJank(unittest.TestCase):
    def test_fake_jank_detected(self):
        # 面板高刷+游戏锁帧的假 Jank：jank 中位极高、fps 高、帧时间未升高
        rows = [_row(i * 1000, fps=58.0, jank=0.95, p50=16.7) for i in range(50)]
        issues = scan_rows(rows)
        self.assertTrue(any(it["type"] == "fake_jank" for it in issues))

    def test_real_jank_elevated_p50_not_flagged(self):
        # 真卡顿：jank 高但帧时间也明显升高（p50 很大）→ 不判假 Jank
        rows = [_row(i * 1000, fps=30.0, jank=0.95, p50=45.0) for i in range(50)]
        issues = scan_rows(rows)
        self.assertFalse(any(it["type"] == "fake_jank" for it in issues))


class TestScanRowsPssJump(unittest.TestCase):
    def test_pss_jump_detected(self):
        # 前 20 点 PSS 稳定在 100000，之后跳到 800000（8×）持续 → 突变
        rows = [_row(i * 1000, pss=100000, rss=120000) for i in range(20)]
        rows += [_row((20 + i) * 1000, pss=800000, rss=900000) for i in range(5)]
        issues = scan_rows(rows)
        self.assertTrue(any(it["type"] == "pss_jump" for it in issues))

    def test_stable_pss_not_flagged(self):
        rows = [_row(i * 1000, pss=100000 + i * 10, rss=120000) for i in range(50)]
        issues = scan_rows(rows)
        self.assertFalse(any(it["type"] == "pss_jump" for it in issues))


class TestScanRowsMissingMetric(unittest.TestCase):
    def test_missing_metric_detected(self):
        # fps 连续 10 点缺失
        rows = [_row(i * 1000, cpu=50.0, pss=100000, rss=120000) for i in range(10)]
        rows += [_row(i * 1000, fps=58.0, cpu=50.0, pss=100000, rss=120000) for i in range(10, 20)]
        issues = scan_rows(rows)
        missing = [it for it in issues if it["type"] == "missing_metric" and it["message"].startswith("fps")]
        self.assertTrue(missing)
        self.assertEqual(missing[0]["count"], 10)

    def test_short_gap_not_flagged(self):
        # 仅 2 点缺失 → 不判
        rows = [_row(0, cpu=50.0), _row(1000, cpu=50.0), _row(2000, fps=58.0, cpu=50.0)]
        issues = scan_rows(rows)
        self.assertFalse(any(it["type"] == "missing_metric" for it in issues))


class TestHealthSummary(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(health_summary([]), "")

    def test_summary_text(self):
        issues = [{"type": "wrong_process", "level": "high", "message": "疑似采错进程",
                   "start_t_ms": 0, "end_t_ms": 5000, "count": 100}]
        self.assertIn("疑似采错进程", health_summary(issues))
        self.assertIn("0.0s~5.0s", health_summary(issues))


class TestCheckRowsLive(unittest.TestCase):
    def test_rss_alert_after_streak(self):
        state = {}
        row_bad = {"t_ms": 0, "mem": {"pss_kb": 10000, "vmrss_kb": 9000}}
        for _ in range(5):
            alerts, state = check_rows_live(row_bad, state)
        self.assertTrue(any("RSS<PSS" in a for a in alerts))

    def test_cpu_near_zero_alert_after_streak(self):
        state = {}
        row = {"t_ms": 0, "fps": {"fps": 58.0}, "cpu": {"cpu_proc_pct": 0.0}}
        for _ in range(10):
            alerts, state = check_rows_live(row, state)
        self.assertTrue(any("采错进程" in a for a in alerts))


if __name__ == "__main__":
    unittest.main()
