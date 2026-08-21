# -*- coding: utf-8 -*-
"""内存采集。

主口径 PSS（与 PerfDog 一致）：
  1) 优先 `/proc/<pid>/smaps_rollup`（Android 8+，一次 cat 毫秒级，Pss/Rss 同源双值）
  2) 失败回退 `dumpsys meminfo <pid>`，解析 App Summary 段的 TOTAL PSS / TOTAL RSS（同源）

同源保证：PSS 与 RSS 出自同一份数据，物理约束 RSS >= PSS 恒成立。
此前 PSS 来自 dumpsys、RSS 来自 /proc/status 双源不同步，实测 72.6% 样本出现
RSS < PSS 倒挂（2026-08-21 修复）。

dumpsys meminfo 执行较慢（~0.5s），内存采样建议节流（间隔 ≥2s，调用方控制）。
"""

import re

# smaps_rollup：行首精确匹配（Pss_Anon/Pss_File/Pss_Shmem 以 Pss_ 前缀，不能误匹配）
_ROLLUP_RSS_RE = re.compile(r"^Rss:\s+(\d+)\s+kB", re.MULTILINE)
_ROLLUP_PSS_RE = re.compile(r"^Pss:\s+(\d+)\s+kB", re.MULTILINE)
# dumpsys meminfo App Summary 段：TOTAL PSS / TOTAL RSS（大小写不敏感）
_TOTAL_PSS_RE = re.compile(r"TOTAL\s+PSS:\s*([\d,]+)", re.IGNORECASE)
_TOTAL_RSS_RE = re.compile(r"TOTAL\s+RSS:\s*([\d,]+)", re.IGNORECASE)
# 最末兜底：旧版主表 TOTAL 行（带 kB 后缀，Android 14 主表 TOTAL 行不带 kB，
# 仅在 App Summary 段解析失败时兜底，误匹配风险已尽力压低）
_TOTAL_RE = re.compile(r"TOTAL\s+(\d+)\s+kB")
# 兜底：/proc/<pid>/status 的 VmRSS（仅当 dumpsys 也拿不到 RSS 时）
_VMRSS_RE = re.compile(r"VmRSS:\s+(\d+)\s+kB")

# dumpsys meminfo 执行较慢（~0.5s），两次真实采样的最小间隔（秒）
# 节流窗口内的 sample 返回空值（不采、不产生重复点），避免采样间隔被拖长
MIN_INTERVAL = 2.0


def _kb(v):
    """去掉千分位逗号转 int。"""
    return int(v.replace(",", ""))


def parse_smaps_rollup(out):
    """解析 /proc/<pid>/smaps_rollup 输出。返回 {"pss_kb":.., "rss_kb":..} 或 None。"""
    m = _ROLLUP_PSS_RE.search(out)
    r = _ROLLUP_RSS_RE.search(out)
    if not m or not r:
        return None
    return {"pss_kb": _kb(m.group(1)), "rss_kb": _kb(r.group(1))}


def parse_meminfo(out):
    """解析 dumpsys meminfo 输出（App Summary 段同源 PSS/RSS）。

    返回 {"pss_kb":.., "rss_kb":..}（能拿到的字段），失败返回空 dict。
    """
    res = {}
    # 优先在 App Summary 段内匹配（锚定段起点，避免命中主表/其它段落）
    seg = out
    mseg = re.search(r"App Summary", out, re.IGNORECASE)
    if mseg:
        seg = out[mseg.end():]
        # 跳过段标题后的空行/空白，从内容行开始
        body = re.search(r"\S", seg)
        if body:
            seg = seg[body.start():]
            mend = re.search(r"\n\s*\n", seg)   # 数字区结束：下一个空行
            if mend:
                seg = seg[:mend.start()]
    mp = _TOTAL_PSS_RE.search(seg)
    mr = _TOTAL_RSS_RE.search(seg)
    if mp:
        res["pss_kb"] = _kb(mp.group(1))
    if mr:
        res["rss_kb"] = _kb(mr.group(1))
    return res


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

        # 1) 优先 smaps_rollup：毫秒级 + Pss/Rss 同源（Android 8+；部分 ROM SELinux 限制
        #    读他进程 smaps，失败自动回退 dumpsys）
        if pid:
            try:
                out = self.adb.shell(["cat", f"/proc/{pid}/smaps_rollup"])
                d = parse_smaps_rollup(out)
                if d:
                    result["pss_kb"] = d["pss_kb"]
                    result["vmrss_kb"] = d["rss_kb"]
                    return result
            except Exception:
                pass

        # 2) 回退 dumpsys meminfo（App Summary 同源解析）
        target = str(pid) if pid else self.package
        try:
            out = self.adb.shell(["dumpsys", "meminfo", target])
        except Exception:
            return result
        d = parse_meminfo(out)
        if d.get("pss_kb") is not None:
            result["pss_kb"] = d["pss_kb"]
        else:
            # 最末兜底：旧格式 TOTAL 行
            m2 = _TOTAL_RE.search(out)
            if m2:
                result["pss_kb"] = _kb(m2.group(1))
        if d.get("rss_kb") is not None:
            result["vmrss_kb"] = d["rss_kb"]
        elif pid:
            # dumpsys 拿不到 RSS（旧版输出无 TOTAL RSS 列）→ 兜底 /proc/status VmRSS
            try:
                s = self.adb.shell(["cat", f"/proc/{pid}/status"])
                mr = _VMRSS_RE.search(s)
                if mr:
                    result["vmrss_kb"] = int(mr.group(1))
            except Exception:
                pass
        return result
