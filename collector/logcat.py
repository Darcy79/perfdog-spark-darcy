# -*- coding: utf-8 -*-
"""logcat 事件采集 — 模式1（零侵入：直接捞小游戏 console.log 输出）。

背景：埋点 SDK 每次测试都要改代码/重打包，落地成本高。
改用 adb logcat 流式读取微信小游戏运行时的 JS 日志（console.log / chromium / 微信 tag），
零侵入、零改包，一次打通后测试端从此零操作。

原理：
    adb logcat -v time  持续流式输出系统日志；
    后台线程逐行解析，按 tag 白名单 + 关键词过滤出小游戏相关事件；
    用"设备 epoch 秒"锚点 + logcat 行自带设备时间戳做时间对齐 → 事件 t_ms 与采集数据同基准；
    供看板在曲线上叠加"场景/事件标注层"。

用法（main.py 集成）:
    from logcat import LogcatMonitor
    mon = LogcatMonitor(adb, serial)
    mon.start()
    ...
    for ev in mon.get_events():
        write_to(events_file, ev)
    mon.stop()
"""

import re
import subprocess
import threading
import time
from datetime import datetime

# logcat 行: 08-17 10:30:00.123  1234  5678 I chromium: [INFO:CONSOLE(12)] "hello"
_LINE_RE = re.compile(
    r"^(\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"\d+\s+\d+\s+([VDIWEF])\s+([^:]+): (.*)$"
)

# tag 白名单：小游戏 JS 日志常出现在这些 tag
DEFAULT_TAGS = (
    "chromium", "WeChat", "MicroMsg", "console", "XWeb",
    "JsCore", "JSCore", "JSBridge", "WebView", "mm",
)

# 文本命中词：场景切换 / 打点 / 错误（命中即收）
DEFAULT_TEXT_HITS = (
    "console", "PERF", "perf", "scene", "Scene", "onShow", "onHide",
    "进入", "场景", "对局", "商城", "卡顿", "error", "exception",
    "crash", "Error", "Exception",
)


class LogcatMonitor:
    """后台线程持续读 adb logcat，解析并缓存小游戏相关事件。"""

    def __init__(self, adb, serial="", tags=None, text_hits=None,
                 min_interval=1.0):
        self._adb = adb                 # Adb 实例（复用其 adb 路径）
        self._serial = serial or getattr(adb, "serial", "")
        self._tags = tuple(tags) if tags else DEFAULT_TAGS
        self._hits = tuple(text_hits) if text_hits else DEFAULT_TEXT_HITS
        self._min_gap = min_interval    # 同 tag+text 限流间隔（秒）
        self._proc = None
        self._thread = None
        self._stop = False
        self._events = []               # 待消费事件（加锁）
        self._lock = threading.Lock()
        self._anchor = None             # 采集启动时设备 epoch（秒）
        self._anchor_year = None        # 锚点对应年份（logcat 时间戳无年份，补锚点年）
        self._last_emit = {}            # (tag, text) -> 最近发送时间，限流
        self.started = False

    # ---------------- 生命周期 ----------------
    def start(self):
        """启动 logcat 监听。取设备时间锚点 + 拉流。"""
        # 设备 epoch 秒锚点（与 logcat 行时间戳同基准，保证 t_ms 对齐）
        try:
            out = self._adb.shell(["date", "+%s"])
            self._anchor = float(out.strip())
        except Exception:
            self._anchor = time.time()
        # logcat 行时间戳无年份字段，取锚点年（跨 12/31 采集不会错一年，2026-08-21 修复）
        self._anchor_year = datetime.fromtimestamp(self._anchor).year
        cmd = [self._adb.adb]
        if self._serial:
            cmd += ["-s", self._serial]
        # -T 1：只读新日志，不 dump 历史 buffer；-v time 输出设备时间戳
        cmd += ["logcat", "-v", "time", "-T", "1"]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=1, text=True, encoding="utf-8", errors="replace")
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="logcat-monitor")
        self._stop = False
        self._thread.start()
        self.started = True

    def stop(self):
        self._stop = True
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass

    # ---------------- 事件消费 ----------------
    def get_events(self):
        """取出自上次调用以来的新事件列表（清空缓存）。"""
        with self._lock:
            evs = self._events
            self._events = []
            return evs

    # ---------------- 内部 ----------------
    def _run(self):
        """读流主循环。USB 抖动导致进程退出时自动重连。"""
        while not self._stop:
            try:
                line = self._proc.stdout.readline()
            except Exception:
                line = ""
            if line:
                ev = self._parse(line)
                if ev:
                    with self._lock:
                        self._events.append(ev)
                continue
            # EOF：进程退出（USB 断/被杀）。未停止则 2s 后重启拉流
            if self._stop:
                break
            self._proc.terminate()
            time.sleep(2)
            if not self._stop:
                try:
                    cmd = [self._adb.adb]
                    if self._serial:
                        cmd += ["-s", self._serial]
                    cmd += ["logcat", "-v", "time", "-T", "1"]
                    self._proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        bufsize=1, text=True, encoding="utf-8",
                        errors="replace")
                except Exception:
                    time.sleep(2)

    def _parse(self, line):
        """解析一行 logcat → 事件 dict（不符合过滤规则的返回 None）。"""
        m = _LINE_RE.match(line)
        if not m:
            return None
        date_s, hmss, level, tag, text = m.groups()
        text = text.strip()
        tag_l = tag.lower()

        # 级别：只收 I/W/E/F；V/D 多为系统噪音
        if level in ("V", "D"):
            return None
        # tag 白名单
        if not any(t in tag_l for t in self._tags):
            return None
        # 文本命中：chromium 的 [INFO:CONSOLE 行（JS console.log）无条件收；
        # 其余需命中关键词；E/F 错误无条件收
        is_console = "console" in text.lower() or "info:console" in text.lower()
        if not (is_console or level in ("E", "F")
                or any(h in text for h in self._hits)):
            return None

        # 时间对齐：logcat 时间戳 → 设备 epoch → 相对锚点的 t_ms
        # 年份取锚点年（设备时钟与电脑可能不同年，datetime.now().year 会错一年）
        t_ms = None
        try:
            year = self._anchor_year if self._anchor_year is not None \
                else datetime.now().year
            dt = datetime.strptime(
                f"{year}-{date_s} {hmss}",
                "%Y-%m-%d %H:%M:%S.%f")
            t_ms = (dt.timestamp() - self._anchor) * 1000
            if t_ms < 0:
                t_ms = 0
        except Exception:
            pass

        # 限流：同 tag+text 至少间隔 min_gap 秒，防 console.log 高频刷屏
        now = time.time()
        key = (tag, text[:80])
        last = self._last_emit.get(key, 0)
        if now - last < self._min_gap:
            return None
        self._last_emit[key] = now
        # 限流字典定期裁剪：长采集大量唯一文本会缓慢膨胀（2026-08-21 修复）
        if len(self._last_emit) > 500:
            self._last_emit.clear()

        return {"t_ms": t_ms, "tag": tag, "level": level, "text": text}


if __name__ == "__main__":
    # 独立调试：python logcat.py [serial]
    import sys
    from adb import Adb
    serial = sys.argv[1] if len(sys.argv) > 1 else ""
    adb = Adb(serial)
    mon = LogcatMonitor(adb, serial)
    mon.start()
    print("[+] logcat 监听中（Ctrl+C 停止），10 秒窗口…")
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            for ev in mon.get_events():
                t = ev["t_ms"] / 1000 if ev["t_ms"] is not None else -1
                print(f"[{t:8.1f}s] {ev['level']} {ev['tag']}: {ev['text']}")
            time.sleep(0.2)
    finally:
        mon.stop()
    print("[-] 监听结束")
