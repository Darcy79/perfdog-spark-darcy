# -*- coding: utf-8 -*-
"""目标进程解析。

微信小游戏运行在微信 App 的子进程 com.tencent.mm:appbrandN 中。
按配置的 process_pattern 在微信各进程里匹配；匹配失败回退主进程。
进程可能因小游戏重启而变化，采样时通过 current_pid() 每次校验。

性能（2026-08-21 优化）：
  - resolve() 单次 `ps -A -o PID,ARGS` 拿全进程列表（替代 pidof + 逐 pid cat cmdline 的多次往返）
  - current_pid() 校验 pid 身份（2026-09-11 修正：cmdline 完整进程名比对优先，
    comm 包含匹配仅作回退——comm 截断 15 字符，对 appbrand 进程恒不匹配）
  - resolve() 失败加 5s 节流缓存（此前 pid 为 None 时 3 个采集线程 ~3次/s 打 pidof）
"""

import time

# pid 身份校验"未知"容错阈值：连续 N 次读不到 cmdline/comm 才判 pid 失效。
# 读不到 ≠ 进程消失——主机 adb 链路抖动时逐次误判会让 cpu/mem/net 整点缺数
# （2026-09-11 事故 run 20260911_162353：63 点中 32 点 pid=None，实际进程与
# 渲染层全程都在）；但真进程死亡后也不能无限沿用旧 pid 采错数据，连续 3 次
# （校验周期 5s，约 10-15s）即判失效，由 data_health 连续缺失规则兜底标注。
IDENTITY_FAIL_STREAK = 3


class PidResolver:
    def __init__(self, adb, package, process_pattern="appbrand",
                 fixed_pid=None, fixed_name=None):
        self.adb = adb
        self.package = package
        self.process_pattern = process_pattern
        self.pid = None
        self.proc_name = None   # 匹配到的进程名（jsonl meta 记录用，事后可核对采集对象）
        self._next_check = 0.0   # 下一次允许校验的时间点（性能优化 2026-08-12）
        self._check_interval = 5.0  # 校验周期：避免每轮采样都 cat comm 多一次往返
        # 期望进程名（身份校验用）：优先 process_pattern；无则用包名最后一段。
        # 校验优先走 /proc/<pid>/cmdline 完整进程名比对（不受 comm 15 字符截断
        # 影响，见 _identity_ok），comm 仅作 cmdline 读取失败时的回退。
        self._expect = process_pattern or package.rsplit(".", 1)[-1]
        # "用户指定进程"模式（2026-09-11 启动向导）：用户在探测列表里选定 pid，
        # 之后**不再自动改选**——进程消失就返回 None（指标缺数、页面告警），
        # 而不是静默切到另一个 appbrand 造成"同一份数据前后不同进程"的脏数据。
        self._fixed_pid = fixed_pid
        self._fixed_name = fixed_name
        if fixed_pid:
            self.pid = fixed_pid
            self.proc_name = fixed_name
        # 身份校验"未知"连续计数（读不到 cmdline/comm 的次数），跨校验周期累积；
        # 校验结果为确认/明确不匹配时清零（观测入口：resolver._id_unknown_streak）
        self._id_unknown_streak = 0

    def resolve(self):
        """重新解析目标进程 pid，找不到返回 None（带 5s 失败节流）。

        多候选（2026-08-27 真机发现：微信多开/驻留导致多个 appbrand 进程并存，
        如 appbrand0/1/2 同时存在）：取**累计 CPU 时间最大**（utime+stime）的进程。
        闲置驻留进程 CPU 极小（实测 18.6h 仅 38.8s），真正渲染小游戏的进程 CPU 大
        （实测 1476s）；旧逻辑取 ps 列表第一个（pid 最小）会采到闲置进程，
        导致 cpu/mem/net 全部采错进程（8/27 数据作废事故）。
        """
        # 用户指定进程模式（2026-09-11）：只确认该 pid 仍存在、身份未变，
        # **绝不自动改选其他 appbrand**——自动改选会在切换前先采一段错进程数据。
        if self._fixed_pid:
            try:
                pids = self._list_pids()
            except Exception:
                pids = []
            for p, name in pids:
                if p == self._fixed_pid:
                    self.pid = p
                    self.proc_name = name or self._fixed_name
                    return p
            self.pid = None
            self.proc_name = None
            return None
        # 失败节流：上次解析失败后 5s 内不再重复打命令（3 个采集线程共享实例，
        # 不加节流会以 ~3次/s 高频轰炸 adb）
        if self.pid is None and time.time() < self._next_check:
            return None
        pids = self._list_pids()
        if not pids:
            self.pid = None
            self.proc_name = None
            self._next_check = time.time() + 5.0
            return None
        if self.process_pattern:
            cands = [c for c in pids if self.process_pattern in c[1]]
            if cands:
                chosen = self._pick_active(cands)
                self.pid, self.proc_name = chosen
                return self.pid
            # 未匹配到子进程 → 回退主进程（包名匹配）
        for pid, name in pids:
            if self.package in name:
                self.pid = pid
                self.proc_name = name
                return self.pid
        self.pid = None
        self.proc_name = None
        self._next_check = time.time() + 5.0
        return None

    def _pick_active(self, cands):
        """多候选中选累计 CPU 时间最大的（utime+stime），返回 (pid, name)。

        候选通常 1~3 个；一次 `cat /proc/PID/stat ...` 拿全部（一次 adb 往返）。
        全部解析失败时回退第一个候选（兼容旧行为）。
        """
        if len(cands) <= 1:
            return cands[0]
        pids = [str(c[0]) for c in cands]
        try:
            out = self.adb.shell(["cat"] + [f"/proc/{p}/stat" for p in pids])
        except Exception:
            return cands[0]
        best, best_ticks = cands[0], -1
        for line in out.splitlines():
            if ")" not in line:
                continue
            try:
                after = line[line.rfind(")") + 1:].split()
                ticks = int(after[11]) + int(after[12])   # 原字段 14=utime 15=stime
                pid = int(line.split("(", 1)[0].strip())
            except (ValueError, IndexError):
                continue
            if ticks > best_ticks:
                best = next((c for c in cands if c[0] == pid), best)
                best_ticks = ticks
        return best

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

    def _identity_ok(self, pid):
        """校验 pid 是否仍属于期望进程，三态返回（2026-09-11 事故修复）：

          True  = 身份确认（cmdline 或 comm 匹配）
          False = cmdline 可读且明确不匹配 → pid 一定被复用（调用方立即判失效）
          None  = 读不到（异常 / 输出为空）→ "未知"，调用方沿用旧 pid 容错

        区分 False 与 None 是本修复的核心：读不到多半是主机 adb 链路抖动，
        误判成"进程消失"会让 cpu/mem/net 整点缺数（run 20260911_162353：
        63 点中 32 点 pid=None，事后证实进程与渲染层全程都在）；而 cmdline
        明确不匹配是真复用，必须立即处理不能拖。
        两级比对（2026-09-11 comm 矛盾修正的延续）：
          1) cmdline（首选）：Android 应用进程 cmdline[0] 即完整进程名，与
             resolve() 记录的 proc_name（ps ARGS 列）或 _expect 做包含匹配；
             可读且不匹配 → 结论可靠，返回 False；
          2) comm（回退）：comm 截断 15 字符且截断方向因 ROM 而异（荣耀为
             末 15、标准 Linux 为首 15），匹配成功可确认（True）；**不匹配
             不作为否定结论**——可能只是关键字被截掉 → 返回 None（未知）。
        """
        # 1) cmdline 完整进程名比对
        try:
            out = self.adb.shell(["cat", f"/proc/{pid}/cmdline"])
            cmdline = out.replace("\0", " ").strip()
            if cmdline:
                expect_full = self.proc_name or ""
                if (expect_full and expect_full in cmdline) \
                        or (self._expect and self._expect in cmdline):
                    return True
                return False     # cmdline 可读但不匹配 → pid 已被复用
            # 读到了但为空（进程刚 fork 等瞬态）：不足以下结论 → 走 comm
        except Exception:
            pass
        # 2) comm 回退（只用于确认，不用于否定——截断方向因 ROM 而异）
        try:
            comm = self.adb.shell(["cat", f"/proc/{pid}/comm"]).strip()
            if comm and self._expect and self._expect in comm:
                return True
        except Exception:
            pass
        return None

    def current_pid(self, ts=0.0):
        """校验进程仍存在且身份未变，进程消失/被复用则重新解析。

        三态容错（2026-09-11）：
          - 身份确认 → 沿用，未知计数清零；
          - cmdline 明确不匹配（被复用）→ 立即判失效（保持原语义，不能拖）；
          - 读不到（未知）→ 沿用旧 pid 并累计 _id_unknown_streak，连续
            IDENTITY_FAIL_STREAK 次才判失效（链路抖动不丢点；真进程死亡
            最多多沿用 (N-1)×5s ≈ 10s）。
        ts 为当前时间；默认未传则立即校验（兼容旧调用）。
        按 _check_interval 节流：在校验窗口内直接返回缓存的 pid。
        """
        if self.pid:
            if ts and ts < self._next_check:
                return self.pid        # 校验窗口内，直接用缓存
            self._next_check = ts + self._check_interval
            verdict = self._identity_ok(self.pid)
            if verdict is True:
                self._id_unknown_streak = 0
                return self.pid
            if verdict is False:
                # cmdline 可读且明确不匹配 → pid 一定被复用，立即失效
                self._id_unknown_streak = 0
                self.pid = None
            else:
                # 读不到 = 未知：沿用旧 pid，连续 N 次才判失效
                self._id_unknown_streak += 1
                if self._id_unknown_streak < IDENTITY_FAIL_STREAK:
                    return self.pid
                self._id_unknown_streak = 0
                self.pid = None
            if self._fixed_pid:
                # 用户指定进程模式：失效即停采该目标（指标缺数、页面可告警），
                # 不自动改选——避免"同一份数据前后不同进程"的静默脏数据
                return None
        return self.resolve()
