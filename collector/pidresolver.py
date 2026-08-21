# -*- coding: utf-8 -*-
"""目标进程解析。

微信小游戏运行在微信 App 的子进程 com.tencent.mm:appbrandN 中。
按配置的 process_pattern 在微信各进程里匹配；匹配失败回退主进程。
进程可能因小游戏重启而变化，采样时通过 current_pid() 每次校验。
"""


class PidResolver:
    def __init__(self, adb, package, process_pattern="appbrand"):
        self.adb = adb
        self.package = package
        self.process_pattern = process_pattern
        self.pid = None
        self._next_check = 0.0   # 下一次允许校验的时间点（性能优化 2026-08-12）
        self._check_interval = 5.0  # 校验周期：避免每轮采样都 cat comm 多一次往返

    def resolve(self):
        """重新解析目标进程 pid，找不到返回 None。"""
        try:
            out = self.adb.shell(["pidof", self.package])
        except Exception:
            return None
        pids = out.split()
        if not pids:
            return None
        if self.process_pattern:
            for pid in pids:
                try:
                    cmdline = self.adb.shell(["cat", f"/proc/{pid}/cmdline"])
                    # cmdline 以 \x00 分隔各参数，转为空格连接后匹配
                    name = cmdline.replace("\x00", " ")
                    if self.process_pattern in name:
                        self.pid = int(pid)
                        return self.pid
                except Exception:
                    continue
        try:
            self.pid = int(pids[0])
        except ValueError:
            self.pid = None
        return self.pid

    def current_pid(self, ts=0.0):
        """校验进程仍存在，进程消失则重试解析。

        ts 为当前时间；默认未传则立即校验（兼容旧调用）。
        按 _check_interval 节流：在校验窗口内直接返回缓存的 pid，
        不重复 cat /proc/pid/comm，减少 adb 往返（性能优化）。
        """
        if self.pid:
            if ts and ts < self._next_check:
                return self.pid        # 校验窗口内，直接用缓存
            self._next_check = ts + self._check_interval
            try:
                out = self.adb.shell(["cat", f"/proc/{self.pid}/comm"])
                if out.strip():
                    return self.pid
            except Exception:
                pass
        return self.resolve()
