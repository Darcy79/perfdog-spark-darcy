# -*- coding: utf-8 -*-
"""自研 PerfDog — Web 看板服务端（纯 Python 标准库，本地运行不传云端）

与 main.py 集成：main.py 传入 --web 后，采集循环每采一点调用
web.add_sample(row)，页面通过轮询 /api/latest 实时刷新。

端点：
    GET /                    实时看板页
    GET /report.html         历史报告页
    GET /assets/*            静态文件（echarts.min.js / app.js / style.css）
    GET /api/status          采集状态 {running, device, pid, run_id, outdir, ...}
    GET /api/latest          最近采样点（环形缓冲，最多 300 点）
    GET /api/runs            output 目录历史 jsonl 列表（行数按 mtime/size 缓存）
    GET /api/report?name=xx  指定历史报告完整数据（JSON 数组，按 mtime 缓存）
    GET /api/events?name=xx  logcat 事件标注列表
    POST /api/stop           停止采集（复用首次 Ctrl+C 的完整停止路径）
    POST /api/shutdown       彻底退出程序（停止采集 + 结束进程）
    POST /api/switch-target  热切换被测应用
    POST /api/rename         记录备注/重命名
"""

import json
import os
import random
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from apk_label import get_apk_label

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
RING_SIZE = 300  # 实时看板保留最近 300 个采样点
# 已装应用 label 缓存（避免每次下拉都逐包 aapt 解析）：按设备序列号分 key
APP_LABEL_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".app_labels.json")
# 包列表短期缓存（60s TTL）：避免每次打开下拉/刷新都全量 pm list packages
APPS_CACHE_TTL = 60.0
# label 解析失败后的重试间隔（秒）：失败也写缓存（fail 标记），
# 期间不再重复昂贵的 unzip 解析（此前失败不缓存导致每次下拉都重解析）
LABEL_RETRY_INTERVAL = 24 * 3600.0


class WebServer:
    def __init__(self, port=8080, output_dir="output", adb=None, switch_cb=None):
        self.port = port
        self.output_dir = os.path.abspath(output_dir)
        self.latest = []            # 环形缓冲
        self._seq = 0               # 采样序号：SSE 用它作增量游标（替代 count）
        self.status = {"running": False, "device": "", "pid": None,
                       "run_id": "", "started_at": None,
                       "target": None, "process_pattern": "",
                       "outdir": os.path.abspath(output_dir)}
        self.adb = adb              # 采集器注入的 adb 实例（离线看板为 None）
        self.switch_cb = switch_cb  # 采集器注入的热切换回调 apply_target(package, pattern)
        self._stop_cb = None        # 采集器注入的停止采集回调（复用 Ctrl+C 停止路径）
        self._shutdown_cb = None    # 采集器注入的彻底退出回调（停止采集 + 结束进程）
        self._resolving = False     # 后台应用名(label)解析进行中
        self._abort_label = threading.Event()  # 中止后台 label 解析（停止采集时置位）
        self._lock = threading.Lock()
        self._httpd = None
        self._thread = None
        # 包列表短期缓存（TTL）：{serial: (expire_ts, apps)}
        self._apps_cache = {}
        # 扫描锁：缓存过期瞬间并发请求只放行一个 pm list，其余等待复用结果
        self._apps_scan_lock = threading.Lock()
        # label 缓存文件读写锁：串行化 HTTP 线程读、后台线程写，
        # 避免"读-改-写"竞态覆盖其他设备记录或产生半截 JSON
        self._labels_file_lock = threading.Lock()
        # 失败缓存的随机抖动（0~1h）：防止大量包在同一时刻到期引发解析风暴
        self._label_retry_jitter = random.uniform(0, 3600)
        # /api/runs 行数缓存：{rel: (mtime_ns, size, n)}，历史文件不可变，
        # 只对 (mtime,size) 变化的文件重算行数（2026-08-21，output 27MB 不再每次全读）
        self._runs_lines = {}
        # runs 行数缓存互斥锁（2026-08-24）：/api/runs 每 5s 轮询 + 并发打开报告时，
        # _runs_lines 的读改写若交叉可能触发 "dictionary changed size during iteration"，
        # 该异常若不兜底会让 handler 线程崩溃 → 连接被重置 → 前端 Failed to fetch
        self._runs_lock = threading.Lock()
        # /api/report 解析缓存：OrderedDict{name: (mtime_ns, size, rows)}，历史 jsonl 不可变。
        # 用 LRU 淘汰（2026-08-25）：旧实现超上限整体 clear()，报告数 >50 时命中率断崖式
        # 归零（每次清空后所有报告都要重新全文解析）；改为只淘汰最久未使用的那条
        self._report_cache = OrderedDict()
        # report 缓存条数上限：超过则按 LRU 逐条淘汰最久未使用项（语义不变，仍是防膨胀）
        self._report_cache_max = 50
        # report 缓存互斥锁（2026-08-24）：ThreadingHTTPServer 下 /api/report 并发
        # 时缓存的 get/set/clear 可能交叉——GIL 只保证单条字节码原子，读改写序列
        # 不加锁仍可能读到不一致状态；加锁同时让同一报告的并发请求串行命中缓存，
        # 避免重复全文解析
        self._report_lock = threading.Lock()

    # ---------------- 采集器调用 ----------------
    def add_sample(self, row):
        with self._lock:
            self._seq += 1
            row["_seq"] = self._seq
            self.latest.append(row)
            if len(self.latest) > RING_SIZE:
                self.latest = self.latest[-RING_SIZE:]

    def set_status(self, **kw):
        with self._lock:
            self.status.update(kw)

    def set_switch_callback(self, cb):
        """注入热切换回调（main.py 传入 apply_target）。"""
        with self._lock:
            self.switch_cb = cb

    def set_stop_callback(self, cb):
        """注入停止采集回调（main.py 传入，复用首次 Ctrl+C 的完整停止路径）。"""
        with self._lock:
            self._stop_cb = cb

    def set_shutdown_callback(self, cb):
        """注入彻底退出回调（停止采集 + 结束进程）。"""
        with self._lock:
            self._shutdown_cb = cb

    def abort_label_resolve(self):
        """请求中止后台 label 解析（停止采集时调用，避免解析线程空转）。"""
        self._abort_label.set()

    def clear_latest(self):
        """清空实时缓冲（切换被测目标后新目标从零开始显示）。"""
        with self._lock:
            self.latest = []

    # ---------------- 已装应用名（label）解析 ----------------
    # label 解析较慢（逐包 unzip + Python 解析，adb 并发被 server 串行化、并行无效），
    # 采用懒解析：下拉秒回缓存/包名，后台串行补齐，前端轮询刷新。
    def _label_expired(self, ts):
        """失败 label 是否到达重试时间（24h 基础间隔 + 随机抖动）。"""
        return (ts or 0) + LABEL_RETRY_INTERVAL + self._label_retry_jitter <= time.time()

    def _normalize_label(self, v):
        """把缓存条目归一化为 {label, fail, ts} dict；非法条目返回 None。

        兼容旧格式（裸 label 字符串）。
        """
        if isinstance(v, str):                      # 旧格式：裸 label 字符串
            return {"label": v}
        if not isinstance(v, dict):
            return None
        return v

    def _need_resolve(self, entry):
        """该包是否需要（重新）解析：无缓存，或失败条目已过重试期。"""
        e = self._normalize_label(entry)
        if e is None:
            return True
        if e.get("fail"):
            return self._label_expired(e.get("ts"))
        return not e.get("label")

    def _label_text(self, entry):
        """取条目可显示的 label；失败条目/无 label 返回 None（回退显示包名）。"""
        e = self._normalize_label(entry)
        if e and not e.get("fail") and e.get("label"):
            return e["label"]
        return None

    def _load_app_labels(self, serial):
        """读 label 缓存（锁保护；文件损坏/不存在返回空）。"""
        with self._labels_file_lock:
            try:
                with open(APP_LABEL_CACHE, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                return {}
        raw = data.get(serial, {})
        if not isinstance(raw, dict):
            return {}
        out = {}
        for k, v in raw.items():
            e = self._normalize_label(v)
            if e is not None:
                out[k] = e
        return out

    def _save_app_label(self, serial, pkg, entry):
        """持久化单个包的 label 条目（锁内读-改-写 + 临时文件 + os.replace）。

        原子替换保证并发/轮询下不产生半截 JSON；失败保留旧文件不丢数据。
        """
        try:
            with self._labels_file_lock:
                data = {}
                try:
                    with open(APP_LABEL_CACHE, encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    pass
                if not isinstance(data, dict):
                    data = {}
                cur = data.get(serial)
                cur = cur if isinstance(cur, dict) else {}
                cur[pkg] = entry
                data[serial] = cur
                tmp = APP_LABEL_CACHE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, APP_LABEL_CACHE)
        except Exception as e:
            print(f"[!] label 缓存写入失败 {pkg}: {e}", flush=True)

    def _find_aapt(self):
        """探测设备上可用的 aapt/aapt2 路径；找不到返回 None（降级 apk 解析）。"""
        try:
            out = self.adb.shell(["sh", "-c",
                "ls /system/bin/aapt /system/bin/aapt2 /system/xbin/aapt "
                "/system/xbin/aapt2 2>/dev/null"])
        except Exception:
            return None
        for line in out.splitlines():
            if line.strip():
                return line.strip()
        return None

    def _aapt_label(self, aapt, apk_path):
        """aapt dump badging 解析 application-label；失败返回 None。"""
        try:
            out = self.adb.shell([aapt, "dump", "badging", apk_path])
        except Exception:
            return None
        for line in out.splitlines():
            s = line.strip()
            m = re.search(r"application-label:['\"](.*?)['\"]", s)
            if m:
                return m.group(1)
            m2 = re.search(r"application:\s+label='(.*?)'", s)
            if m2:
                return m2.group(1)
        return None

    def _resolve_labels_async(self, serial, todo):
        """后台串行解析未缓存的应用名，每解析完一个立即持久化。

        成功写 {label, ts}，失败也写 {fail, ts}（带重试时间），避免每次
        打开下拉都对失败包重复昂贵的 unzip 解析。
        """
        labels = self._load_app_labels(serial)
        aapt = self._find_aapt()
        try:
            for pkg, apk in todo:
                # 中止标志：看板停止采集时终止剩余解析（2026-08-21）
                if self._abort_label.is_set():
                    print("[-] label 解析已中止（采集停止）", flush=True)
                    break
                # 已解析成功 / 失败未到重试期 → 跳过（防止重复昂贵解析）
                if not self._need_resolve(labels.get(pkg)):
                    continue
                try:
                    if aapt:
                        lbl = self._aapt_label(aapt, apk) or ""
                    else:
                        lbl = get_apk_label(self.adb, pkg, apk) or ""
                except Exception as e:
                    print(f"[!] label解析 {pkg} 异常: {e}", flush=True)
                    lbl = ""
                if lbl:
                    entry = {"label": lbl, "ts": time.time()}
                    print(f"[+] 应用名解析: {pkg} = {lbl}", flush=True)
                else:
                    entry = {"fail": True, "ts": time.time()}
                    print(f"[-] 应用名解析失败，24 小时后重试: {pkg}", flush=True)
                labels[pkg] = entry
                self._save_app_label(serial, pkg, entry)
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            with self._lock:
                self._resolving = False

    # ---------------- HTTP 服务 ----------------
    def start(self):
        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self._httpd.server_address[1]

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # 静默访问日志
                pass

            def _send(self, code, body, ctype="application/json; charset=utf-8"):
                data = body if isinstance(body, bytes) else body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _send_file(self, path, ctype):
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                except OSError:
                    self._send(404, "not found", "text/plain; charset=utf-8")
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                # 不缓存：HTML/JS/CSS 每次从本地读最新版本，避免浏览器用旧文件
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _send_json_api(self, payload_fn):
                """JSON API 端点统一兜底（2026-08-24）。

                ThreadingHTTPServer 下 handler 线程内任何未捕获异常都会冒泡到
                socketserver，导致当前连接被直接重置——浏览器 fetch 收到的是
                `TypeError: Failed to fetch`，用户误以为数据损坏。这里把异常收敛为
                200 + {"error": ...}（与既有 /api 错误契约一致），保证线程不崩、
                前端能拿到可读错误。客户端主动断开（BrokenPipe/ConnectionReset）
                属正常，原样抛给上层默认处理。
                """
                try:
                    payload = payload_fn()
                    self._send(200, json.dumps(payload, ensure_ascii=False))
                except (BrokenPipeError, ConnectionResetError):
                    raise
                except Exception as e:
                    try:
                        self._send(200, json.dumps(
                            {"error": f"internal error: {e}"}, ensure_ascii=False))
                    except Exception:
                        pass  # 响应头/正文可能已部分发出，无法补救，仅保证不崩线程

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path

                if path == "/":
                    self._send_file(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")
                elif path == "/report.html":
                    self._send_file(os.path.join(WEB_DIR, "report.html"), "text/html; charset=utf-8")
                elif path.startswith("/assets/"):
                    # 防路径穿越：只允许 assets 目录内的文件
                    name = os.path.basename(parsed.path)
                    fp = os.path.join(WEB_DIR, "assets", name)
                    ctype = "application/javascript; charset=utf-8" if name.endswith(".js") else \
                            "text/css; charset=utf-8" if name.endswith(".css") else \
                            "application/octet-stream"
                    self._send_file(fp, ctype)
                elif path == "/api/status":
                    with server._lock:
                        self._send(200, json.dumps(server.status, ensure_ascii=False))
                elif path == "/api/target":
                    # 当前被测目标（供看板下拉回显）
                    with server._lock:
                        self._send(200, json.dumps({
                            "running": server.status.get("running", False),
                            "package": server.status.get("target"),
                            "process_pattern": server.status.get("process_pattern", ""),
                            "device": server.status.get("device", ""),
                        }, ensure_ascii=False))
                elif path == "/api/device-apps":
                    # 列出手机已装第三方应用（测 APK 时下拉直接点选，无需敲命令）
                    # ?force=1 绕过 60s 包列表缓存强制重新扫描（刷新按钮用）
                    qs = parse_qs(parsed.query)
                    force = (qs.get("force") or [""])[0] == "1"
                    self._send(200, json.dumps(self._list_device_apps(force), ensure_ascii=False))
                elif path == "/api/latest":
                    with server._lock:
                        self._send(200, json.dumps(server.latest, ensure_ascii=False))
                elif path == "/favicon.ico":
                    # 浏览器自动请求网站图标：返回一个内联 SVG，避免 404 噪音
                    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                           '<rect width="32" height="32" rx="6" fill="#4fc3f7"/>'
                           '<text x="16" y="23" font-size="18" text-anchor="middle" '
                           'fill="#fff" font-family="Arial">P</text></svg>')
                    self._send(200, svg, "image/svg+xml")
                elif path == "/api/stream":
                    # SSE 实时推送：采集到新样本即推，毫秒级响应（二期增强 2026-08-14）
                    self._sse(server)
                elif path == "/api/runs":
                    # 统一兜底：内部异常 → 200+{"error"}，不让线程崩溃重置连接
                    self._send_json_api(self._list_runs)
                elif path == "/api/report":
                    qs = parse_qs(parsed.query)
                    name = (qs.get("name") or [""])[0]
                    self._send_json_api(lambda: self._load_report(name))
                elif path == "/api/events":
                    # logcat 事件标注（模式1：与报告同目录的 perfdog_xxx.events.jsonl）
                    qs = parse_qs(parsed.query)
                    name = (qs.get("name") or [""])[0]
                    self._send_json_api(lambda: self._load_events(name))
                else:
                    self._send(404, json.dumps({"error": "not found"}))

            def do_POST(self):
                """POST /api/rename 或 POST /api/switch-target

                /api/rename?name=xxx.jsonl&newname=场景备注
                    备注写入 sidecar 文件 <jsonl路径>.remark.txt（不改动原始 jsonl）。
                    newname 为空表示清除备注。
                /api/switch-target?package=xx&process_pattern=yy
                    看板下拉切换被测应用：调用采集器 apply_target 热切换目标。
                """
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                if parsed.path == "/api/rename":
                    name = (qs.get("name") or [""])[0]
                    newname = (qs.get("newname") or [""])[0]
                    ok, err = self._rename_run(name, newname)
                    self._send(200, json.dumps({"ok": ok, "error": err}, ensure_ascii=False))
                elif parsed.path == "/api/switch-target":
                    package = (qs.get("package") or [""])[0].strip()
                    pattern = (qs.get("process_pattern") or [""])[0].strip()
                    ok, msg = self._switch_target(package, pattern)
                    self._send(200, json.dumps({
                        "ok": ok, "message": msg if ok else None,
                        "error": msg if not ok else None,
                    }, ensure_ascii=False))
                elif parsed.path == "/api/stop":
                    # 看板"停止采集"按钮：复用首次 Ctrl+C 的完整停止路径
                    # （退采样循环 → 停 logcat → 生成 HTML 报告 → running=False）
                    cb = server._stop_cb
                    if cb:
                        try:
                            cb()
                            self._send(200, json.dumps(
                                {"ok": True, "message": "停止采集请求已发出"},
                                ensure_ascii=False))
                        except Exception as e:
                            self._send(200, json.dumps(
                                {"ok": False, "error": str(e)}, ensure_ascii=False))
                    else:
                        self._send(200, json.dumps(
                            {"ok": False, "error": "未运行采集器"},
                            ensure_ascii=False))
                elif parsed.path == "/api/shutdown":
                    # 看板彻底退出：停止采集 + 结束进程（停止流程走完后程序自然退出）
                    cb = server._shutdown_cb
                    if cb:
                        try:
                            cb()
                            self._send(200, json.dumps(
                                {"ok": True, "message": "程序即将退出"},
                                ensure_ascii=False))
                        except Exception as e:
                            self._send(200, json.dumps(
                                {"ok": False, "error": str(e)}, ensure_ascii=False))
                    else:
                        self._send(200, json.dumps(
                            {"ok": False, "error": "未运行采集器"},
                            ensure_ascii=False))
                else:
                    self._send(404, json.dumps({"error": "not found"}))

            def _sse(self, server):
                """SSE 流：增量推送新采样点；无新数据发 keepalive 心跳保持连接。"""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                last_seq = -1
                try:
                    while True:
                        with server._lock:
                            latest = list(server.latest)
                        # 用 _seq 游标推增量：实时缓冲满 300 裁剪后 count 恒为 300，
                        # 按 count 判断会永久停发（2026-08-21 实时看板 299s 冻结修复）
                        new = [r for r in latest if r.get("_seq", 0) > last_seq]
                        if new:
                            max_seq = last_seq
                            for row in new:
                                payload = json.dumps(row, ensure_ascii=False)
                                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                                if row.get("_seq", 0) > max_seq:
                                    max_seq = row.get("_seq", 0)
                            last_seq = max_seq
                        else:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        time.sleep(0.2)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # 客户端断开，正常结束

            # ---------------- 内部 ----------------
            def _scan_device_apps(self, serial):
                """执行一次 pm list packages -3 -f 并缓存结果（TTL）。

                并发请求只放行一个扫描，其余等待并复用结果；扫描失败时
                返回 (apps, error)，apps 为空列表。
                """
                now = time.time()
                cached = server._apps_cache.get(serial)
                if cached and cached[0] > now:
                    return cached[1], None
                with server._apps_scan_lock:
                    # 拿到锁后再查一次：可能有并发请求刚扫描完
                    cached = server._apps_cache.get(serial)
                    if cached and cached[0] > now:
                        return cached[1], None
                    try:
                        out = server.adb.shell(["pm", "list", "packages", "-3", "-f"])
                    except Exception as e:
                        return [], f"列出应用失败: {e}"
                    apps, seen = [], set()
                    for line in out.splitlines():
                        s = line.strip()
                        if s.startswith("package:"):
                            rest = s[len("package:"):].strip()
                            path, _, pkg = rest.rpartition("=")
                            if not pkg:          # 个别设备不带 =包名，仅路径
                                pkg, path = path, ""
                            if pkg and pkg not in seen:
                                seen.add(pkg)
                                apps.append({"pkg": pkg, "apk": path})
                    apps.sort(key=lambda a: a["pkg"])
                    server._apps_cache[serial] = (time.time() + APPS_CACHE_TTL, apps)
                    return apps, None

            def _list_device_apps(self, force=False):
                """adb 列出已装第三方应用（pm list packages -3 -f），尝试解析应用名（label）。

                label 来源：设备自带 aapt（部分 ROM 有 /system/bin/aapt）→ `aapt dump badging`
                解析 application-label；解析结果缓存到本地 .app_labels.json（按 serial 分 key）。
                无 aapt / 解析失败 → label 回退为包名（不影响点选切换）。

                包列表结果带 60s TTL 缓存，避免每次打开下拉/刷新都全量 pm list；
                force=True（?force=1，刷新按钮用）绕过缓存强制重新扫描。
                """
                if not server.adb:
                    return {"ok": False, "apps": [], "error": "离线看板无设备连接，请用 start_perfdog.bat 启动采集"}
                serial = server.adb.serial
                if force:
                    server._apps_cache.pop(serial, None)
                scanned, err = self._scan_device_apps(serial)
                if err:
                    return {"ok": False, "apps": [], "error": err}
                # 浅拷贝条目：缓存里的 dict 是共享引用，本次响应会 pop 掉 apk，
                # 直接改缓存对象会导致下次请求 KeyError
                apps = [dict(a) for a in scanned]
                labels = server._load_app_labels(serial)
                # 需要解析的包：无缓存，或失败条目已过 24h 重试期。
                # 后台串行解析（adb 并发被 server 串行化，并行无效），
                # 本次下拉先显示包名，前端轮询自动刷新。
                todo = [(a["pkg"], a["apk"]) for a in apps
                        if a["apk"] and server._need_resolve(labels.get(a["pkg"]))]
                # resolving = 后台是否正在解析（含上次请求已启动的线程）；
                # 不能只返回"本次是否启动"，否则后台在跑时前端会停止轮询。
                resolving = False
                with server._lock:
                    if todo and not server._resolving:
                        server._resolving = True
                        threading.Thread(
                            target=server._resolve_labels_async,
                            args=(serial, todo),
                            daemon=True,
                        ).start()
                    resolving = server._resolving
                for a in apps:
                    a["label"] = server._label_text(labels.get(a["pkg"])) or a["pkg"]
                    a.pop("apk", None)
                return {"ok": True, "apps": apps, "resolving": resolving}

            def _switch_target(self, package, pattern):
                """调用采集器注入的 apply_target 回调执行热切换。"""
                if not package:
                    return False, "包名为空"
                if not re.match(r"^[A-Za-z0-9._:]+$", package):
                    return False, "包名不合法（仅允许字母、数字、点、下划线、冒号）"
                if not server.switch_cb:
                    return False, "未运行采集器，无法切换目标（请用 start_perfdog.bat 启动后再切换）"
                try:
                    ok, msg = server.switch_cb(package, pattern)
                    return ok, msg
                except Exception as e:
                    return False, f"切换失败: {e}"

            def _rename_run(self, name, newname):
                """为记录写备注（sidecar .remark.txt）。返回 (ok, err)。"""
                if not name or not name.endswith(".jsonl"):
                    return False, "bad name"
                base = os.path.realpath(server.output_dir)
                fp = os.path.realpath(os.path.join(base, name))
                if not fp.startswith(base + os.sep):
                    return False, "bad path"
                if not os.path.isfile(fp):
                    return False, "no such file"
                try:
                    remark = newname.strip().replace("\r", "").replace("\n", "")
                    remark = re.sub(r"[\\/:*?\"<>|]", "", remark)   # 去掉路径分隔等危险字符
                    remark = remark[:100]
                    rp = fp + ".remark.txt"
                    if remark:
                        with open(rp, "w", encoding="utf-8") as f:
                            f.write(remark)
                    else:
                        # 空备注 = 清除
                        if os.path.isfile(rp):
                            os.remove(rp)
                    return True, None
                except Exception as e:
                    return False, str(e)

            def _list_runs(self):
                """递归扫描 output 下所有 jsonl（含按时间命名的子文件夹），最新在前。

                行数按 (mtime_ns, size) 缓存（2026-08-21）：历史 jsonl 采集结束后不可变，
                只对变化的文件重算行数——此前每次请求全量 open 计数，output 积累
                37 文件 27MB 时 report.html 每 5s 轮询就要全读一遍 27MB。
                """
                base = os.path.realpath(server.output_dir)
                # 第一遍：收集文件集合与 stat（不读内容）
                files = []
                if os.path.isdir(base):
                    for root, _, names in os.walk(base):
                        for fn in names:
                            if not fn.endswith(".jsonl"):
                                continue
                            fp = os.path.join(root, fn)
                            rel = os.path.relpath(fp, base).replace("\\", "/")
                            try:
                                st = os.stat(fp)
                            except Exception:
                                continue
                            files.append((rel, st.st_mtime_ns, st.st_size))
                # 清理已不存在文件的缓存 + 重算行数：全程持 _runs_lock（2026-08-24），
                # 并发 /api/runs 请求不再交叉修改 _runs_lines（此前读改写交叉可能触发
                # "dictionary changed size during iteration" → 线程崩溃 → 连接重置）
                with server._runs_lock:
                    alive = {rel for rel, _, _ in files}
                    for rel in [r for r in server._runs_lines if r not in alive]:
                        server._runs_lines.pop(rel, None)
                    # 只对 (mtime,size) 变化的文件重算行数
                    for rel, mtime_ns, size in files:
                        entry = server._runs_lines.get(rel)
                        if entry and entry[0] == mtime_ns and entry[1] == size:
                            continue
                        fp = os.path.join(base, rel.replace("/", os.sep))
                        try:
                            n = sum(1 for _ in open(fp, encoding="utf-8"))
                        except Exception:
                            continue
                        try:
                            st = os.stat(fp)
                            server._runs_lines[rel] = (st.st_mtime_ns, st.st_size, n)
                        except Exception:
                            server._runs_lines[rel] = (mtime_ns, size, n)
                    counts = {rel: server._runs_lines.get(rel) for rel, _, _ in files}
                # 组响应（用锁外快照，remark 读取等文件 IO 不持锁）
                out = []
                for rel, mtime_ns, size in files:
                    entry = counts.get(rel)
                    if not entry:
                        continue
                    _, _, n = entry
                    fp = os.path.join(base, rel.replace("/", os.sep))
                    remark = ""
                    rp = fp + ".remark.txt"
                    if os.path.isfile(rp):
                        try:
                            remark = open(rp, encoding="utf-8").read().strip()
                        except Exception:
                            pass
                    out.append({
                        "name": rel,
                        "size_kb": round(size / 1024, 1),
                        "points": n,
                        "mtime": datetime.fromtimestamp(mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M:%S"),
                        "remark": remark,
                    })
                out.sort(key=lambda x: x["mtime"], reverse=True)
                return out

            def _load_events(self, name):
                """读与报告同目录的 <name>.events.jsonl（logcat 事件标注）。"""
                if not name or not name.endswith(".jsonl"):
                    return []
                base = os.path.realpath(server.output_dir)
                fp = os.path.realpath(os.path.join(base, name.replace(".jsonl", ".events.jsonl")))
                if not fp.startswith(base + os.sep):
                    return []
                events = []
                try:
                    with open(fp, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    events.append(json.loads(line))
                                except Exception:
                                    pass
                except FileNotFoundError:
                    return []
                except Exception:
                    return []
                return events

            def _load_report(self, name):
                # 允许子目录路径，但做防穿越校验：规范化后必须仍在 output 目录内
                if not name or not name.endswith(".jsonl"):
                    return {"error": "bad name"}
                # name 来自 URL query（parse_qs 已解码）：超长/畸形值直接拒绝，
                # 避免 join/realpath 对极端输入做无意义处理
                if len(name) > 1024:
                    return {"error": "bad name"}
                base = os.path.realpath(server.output_dir)
                fp = os.path.realpath(os.path.join(base, name))
                if not fp.startswith(base + os.sep):
                    return {"error": "bad path"}
                try:
                    st = os.stat(fp)
                except Exception:
                    return {"error": "no such file"}
                # 按 (mtime_ns, size) 缓存解析结果（2026-08-21）：历史 jsonl 不可变，
                # 反复点开同一报告不再全文读 + JSON 解析（1MB jsonl ~50ms → 0）。
                # 缓存读写在 _report_lock 内（2026-08-24）：ThreadingHTTPServer 并发时
                # 避免 get/set/clear 交叉；同一报告的并发请求串行命中缓存不重复解析。
                # 淘汰策略为 LRU（2026-08-25）：命中 move_to_end 提升为最近使用，超限
                # 只 popitem(last=False) 淘汰最久未使用项——旧的整体 clear() 在报告数
                # 超过上限时会让命中率断崖归零（常看的几份报告每次都被连坐清掉）。
                with server._report_lock:
                    entry = server._report_cache.get(name)
                    if entry and entry[0] == st.st_mtime_ns and entry[1] == st.st_size:
                        server._report_cache.move_to_end(name)
                        return entry[2]
                    try:
                        rows = []
                        with open(fp, encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line:
                                    rows.append(json.loads(line))
                    except Exception as e:
                        return {"error": str(e)}
                    server._report_cache[name] = (st.st_mtime_ns, st.st_size, rows)
                    server._report_cache.move_to_end(name)   # 覆盖旧条目时也要提到队尾
                    while len(server._report_cache) > server._report_cache_max:
                        server._report_cache.popitem(last=False)
                    return rows

        return Handler
