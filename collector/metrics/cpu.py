# -*- coding: utf-8 -*-
"""CPU 采集。

- 整机占用：`/proc/stat` cpu 行（user/nice/system/idle/iowait/irq/softirq/steal），
  双采样点差值 → busy/total。
- 进程占用：`/proc/<pid>/stat` 的 utime+stime（字段 14/15），
  CPU% = (delta / CLK_TCK) / 墙钟间隔 × 100（单核折算）。

两采样点成对读取；采样间隔太短（<0.3s）抖动大，调用方控制。
"""

# Android 系统时钟频率一般为 100Hz（构造时用 `getconf CLK_TCK` 动态校准，失败兜底 100）
DEFAULT_CLK_TCK = 100


class CpuCollector:
    def __init__(self, adb, pid_resolver, clk_tck=None):
        self.adb = adb
        self.pid_resolver = pid_resolver
        # 动态校准 CLK_TCK：个别机型/内核可能不是 100Hz，写死会算错进程 CPU%
        if clk_tck is None:
            clk_tck = self._probe_clk_tck()
        self.clk_tck = clk_tck
        self._last_busy = None
        self._last_total = None
        self._last_proc = None
        self._last_ts = None

    def _probe_clk_tck(self):
        try:
            out = self.adb.shell(["getconf", "CLK_TCK"])
            v = int(out.strip())
            return v if 50 <= v <= 1000 else DEFAULT_CLK_TCK
        except Exception:
            return DEFAULT_CLK_TCK

    def sample(self, ts):
        pid = self.pid_resolver.current_pid(ts)
        if not pid:
            return {"pid": None, "cpu_total_pct": None, "cpu_proc_pct": None, "error": "no_pid"}
        try:
            stat = self.adb.shell(["cat", "/proc/stat"]).splitlines()[0]
            fields = [int(x) for x in stat.split()[1:]]
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
            total = sum(fields)
            busy = total - idle

            pstat = self.adb.shell(["cat", f"/proc/{pid}/stat"])
            # 进程名可能含空格/括号，用最后一个 ')' 切分，其后字段从 state 开始
            after = pstat[pstat.rfind(")") + 1:].split()
            utime = int(after[11])  # 原字段 14
            stime = int(after[12])  # 原字段 15
            proc_jiffies = utime + stime
        except Exception:
            return {"pid": pid, "cpu_total_pct": None, "cpu_proc_pct": None, "error": "read_fail"}

        result = {"pid": pid, "cpu_total_pct": None, "cpu_proc_pct": None}
        if self._last_busy is not None and self._last_ts is not None:
            dt = ts - self._last_ts
            if dt > 0:
                dcpu = total - self._last_total
                dbusy = busy - self._last_busy
                dproc = proc_jiffies - self._last_proc
                if dcpu > 0:
                    result["cpu_total_pct"] = round(dbusy / dcpu * 100, 2)
                if dproc >= 0:
                    result["cpu_proc_pct"] = round(dproc / self.clk_tck / dt * 100, 2)
        self._last_busy, self._last_total, self._last_proc, self._last_ts = busy, total, proc_jiffies, ts
        return result
