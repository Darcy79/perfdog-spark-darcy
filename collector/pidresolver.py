# -*- coding: utf-8 -*-
"""目标进程解析。

微信小游戏运行在微信 App 的子进程 com.tencent.mm:appbrandN 中。
按配置的 process_pattern 在微信各进程里匹配；匹配失败回退主进程。
进程可能因小游戏重启而变化，采样时通过 current_pid() 每次校验。

性能（2026-08-21 优化）：
  - resolve() 单次 `ps -A -o PID,ARGS` 拿全进程列表（替代 pidof + 逐 pid cat cmdline 的多次往返）
  - current_pid() 校验 comm 是否仍匹配（防止 pid 被系统复用给别的进程后采错数据）
  - resolve() 失败加 5s 节流缓存（此前 pid 为 None 时 3 个采集线程 ~3次/s 打 pidof）
"""

import time


class PidResolver:
    def __init__(self, adb, package, process_pattern="appbrand"):
        self.adb = adb
        self.package = package
        self.process_pattern = process_pattern
        self.pid = None
        self._next_check = 0.0   # 下一次允许校验的时间点（性能优化 2026-08-12）
        self._check_interval = 5.0  # 校验周期：避免每轮采样都 cat comm 多一次往返
        # 期望进程名（comm 校验用）：优先 process_pattern；无则用包名最后一段
        # （Android comm 截断 15 字符，如 "com.tencent.mm:appbrand" → "com.tencent.mm:"，
        #  故只做包含匹配）
        self._expect = process_pattern or package.rsplit(".", 1)[-1]

    def resolve(self):
        """重新解析目标进程 pid，找不到返回 None（带 5s 失败节流）。"""
        # 失败节流：上次解析失败后 5s 内不再重复打命令（3 个采集线程共享实例，
        # 不加节流会以 ~3次/s 高频轰炸 adb）
        if self.pid is None and time.time() < self._next_check:
            return None
        pids = self._list_pids()
        if not pids:
            self.pid = None
            self._next_check = time.time() + 5.0
            return None
        if self.process_pattern:
            for pid, name in pids:
                if self.process_pattern in name:
                    self.pid = pid
                    return self.pid
            # 未匹配到子进程 → 回退主进程（包名匹配）
        for pid, name in pids:
            if self.package in name:
                self.pid = pid
                return self.pid
        self.pid = None
        self._next_check = time.time() + 5.0
        return None

    def _list_pids(self):
        """单次 ps 拿全部进程 (pid, name)。优先 `ps -A -o PID,ARGS`（完整命令行），
        不支持 -o 的旧 toybox 回退 `ps -A` 按列取 NAME。失败返回 []。
        """
        for args in (["ps", "-A", "-o", "PID,ARGS"], ["ps", "-A"]):
            try:
                out = self.adb.shell(args)
            except Exception:
                continue
            pids = []
            for line in out.splitlines():
                s = line.strip()
                if not s:
                    continue
                if s.startswith("PID") or s.startswith("USER"):
                    continue
                if args[2] == "-o":
                    # "  PID ARGS..."：PID 为第一列，其余为完整命令行
                    parts = s.split(None, 1)
                    if len(parts) < 2:
                        continue
                    try:
                        pid = int(parts[0])
                    except ValueError:
                        continue
                    pids.append((pid, parts[1]))
                else:
                    # toybox 默认列：USER PID PPID VSZ RSS WCHAN ADDR S NAME
                    parts = s.split()
                    if len(parts) < 9:
                        continue
                    try:
                        pid = int(parts[1])
                    except ValueError:
                        continue
                    pids.append((pid, parts[-1]))
            if pids:
                return pids
        return []

    def current_pid(self, ts=0.0):
        """校验进程仍存在且身份未变（comm 匹配），进程消失/被复用则重新解析。

        ts 为当前时间；默认未传则立即校验（兼容旧调用）。
        按 _check_interval 节流：在校验窗口内直接返回缓存的 pid。
        """
        if self.pid:
            if ts and ts < self._next_check:
                return self.pid        # 校验窗口内，直接用缓存
            self._next_check = ts + self._check_interval
            try:
                out = self.adb.shell(["cat", f"/proc/{self.pid}/comm"])
                comm = out.strip()
                # comm 校验（2026-08-21）：进程被杀后 pid 可能被系统复用给别的进程，
                # 只检查非空会继续采错误进程的数据且无感知 → 必须比对期望进程名
                if comm and self._expect and self._expect in comm:
                    return self.pid
            except Exception:
                pass
            # comm 不匹配 / 读取失败：pid 已失效或被复用，重新解析
            self.pid = None
        return self.resolve()
