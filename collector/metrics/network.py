# -*- coding: utf-8 -*-
"""网络流量采集（V0.2 新增）。

目标进程的网络收发速率：`/proc/<pid>/net/dev` 双采样点差值。
- rx_bytes / tx_bytes 为接口累计字节，差值 ÷ 时间间隔 = 速率
- 聚合所有非回环(lo)接口（wlan0 / rmnet0 / eth0 等）
- ⚠️ 口径（2026-08-21 修正）：Android 上普通 App 与 shell 共享 root netns，
  该文件实际是**整机流量**（排除 lo），不是进程级流量；前台小游戏占绝大部分时
  近似可用，但微信主进程心跳/系统后台同步也会被计入。免 root 无严格进程级方案
  （PerfDog 官方免 root 模式同理），报告口径须标注"整机（除 lo）"。

容错：读取失败返回 None 字段，不中断采集；进程网络统计可能受限，视机型。
"""


class NetworkCollector:
    def __init__(self, adb, pid_resolver):
        self.adb = adb
        self.pid_resolver = pid_resolver
        self._last = None  # (ts, rx, tx)

    def _read(self, pid):
        """读取进程累计 rx/tx 字节，失败返回 None。"""
        try:
            out = self.adb.shell(["cat", f"/proc/{pid}/net/dev"])
        except Exception:
            return None
        rx = tx = 0
        for line in out.splitlines():
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            if name.strip() == "lo":
                continue
            fields = rest.split()
            if len(fields) >= 9:
                try:
                    rx += int(fields[0])   # rx_bytes
                    tx += int(fields[8])   # tx_bytes
                except ValueError:
                    return None
        return rx, tx

    def sample(self, ts):
        pid = self.pid_resolver.current_pid(ts)
        result = {"pid": pid, "rx_kbps": None, "tx_kbps": None}
        if not pid:
            return result
        cur = self._read(pid)
        if cur is None:
            return result
        if self._last is not None:
            last_ts, last_rx, last_tx = self._last
            dt = ts - last_ts
            if dt > 0:
                drx = cur[0] - last_rx
                dtx = cur[1] - last_tx
                result["rx_kbps"] = round(max(drx, 0) / dt / 1024, 2)   # KB/s
                result["tx_kbps"] = round(max(dtx, 0) / dt / 1024, 2)
        self._last = (ts, cur[0], cur[1])
        return result
