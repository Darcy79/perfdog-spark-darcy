# -*- coding: utf-8 -*-
"""内存采集。

主口径 PSS：`dumpsys meminfo <pid>` 解析 TOTAL PSS（与 PerfDog 内存口径一致）。
兜底 RSS：`/proc/<pid>/status` 的 VmRSS。
dumpsys meminfo 执行较慢（~0.5s），内存采样建议节流（间隔 ≥2s，调用方控制）。
"""

import re

_TOTAL_PSS_RE = re.compile(r"TOTAL\s+PSS:\s+([\d,]+)")
_TOTAL_RE = re.compile(r"TOTAL\s+(\d+)\s+kB")
_VMRSS_RE = re.compile(r"VmRSS:\s+(\d+)\s+kB")


# dumpsys meminfo 执行较慢（~0.5s），两次真实采样的最小间隔（秒）
# 节流窗口内的 sample 返回空值（不采、不产生重复点），避免采样间隔被拖长
MIN_INTERVAL = 2.0


class MemCollector:
    def __init__(self, adb, pid_resolver, package="", min_interval=MIN_INTERVAL):
        self.adb = adb
        self.pid_resolver = pid_resolver
        self.package = package
        self.min_interval = min_interval
        self._last_real_ts = None

    def sample(self, ts):
        # 节流：距上次真实采样不足 min_interval 秒时，跳过本轮（返回空值）
        if self._last_real_ts is not None and (ts - self._last_real_ts) < self.min_interval:
            return {"pid": None, "pss_kb": None, "vmrss_kb": None, "throttled": True}
        self._last_real_ts = ts
        pid = self.pid_resolver.current_pid(ts)
        result = {"pid": pid, "pss_kb": None, "vmrss_kb": None}
        target = str(pid) if pid else self.package
        try:
            out = self.adb.shell(["dumpsys", "meminfo", target])
        except Exception:
            return result
        m = _TOTAL_PSS_RE.search(out)
        if m:
            result["pss_kb"] = int(m.group(1).replace(",", ""))
        else:
            m2 = _TOTAL_RE.search(out)
            if m2:
                result["pss_kb"] = int(m2.group(1).replace(",", ""))
        if pid:
            try:
                s = self.adb.shell(["cat", f"/proc/{pid}/status"])
                mr = _VMRSS_RE.search(s)
                if mr:
                    result["vmrss_kb"] = int(mr.group(1))
            except Exception:
                pass
        return result
