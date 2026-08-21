# -*- coding: utf-8 -*-
"""自研 PerfDog — Web 看板服务端（纯 Python 标准库，本地运行不传云端）

与 main.py 集成：main.py 传入 --web 后，采集循环每采一点调用
web.add_sample(row)，页面通过轮询 /api/latest 实时刷新。

端点：
    GET /                    实时看板页
    GET /report.html         历史报告页
    GET /assets/*            静态文件（echarts.min.js / app.js / style.css）
    GET /api/status          采集状态 {running, device, pid, run_id, outdir}
    GET /api/latest          最近采样点（环形缓冲，最多 300 点）
    GET /api/runs            output 目录历史 jsonl 列表
    GET /api/report?name=xx  指定历史报告完整数据（JSON 数组）
"""

import json
import os
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
RING_SIZE = 300  # 实时看板保留最近 300 个采样点
# 已装应用 label 缓存（避免每次下拉都逐包 aapt 解析）：按设备序列号分 key
APP_LABEL_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".app_labels.json")


class WebServer:
    def __init__(self, port=8080, output_dir="output", adb=None, switch_cb=None):
        self.port = port
        self.output_dir = os.path.abspath(output_dir)
        self.latest = []            # 环形缓冲
        self._seq = 0               # 采样序号：SSE 用它作增量游标（替代 count）
        self.status = {"running": False, "device": "", "pid": None,
                       "run_id": "", "started_at": None,
                       "target": None, "process_pattern": ""}
        self.adb = adb              # 采集器注入的 adb 实例（离线看板为 None）
        self.switch_cb = switch_cb  # 采集器注入的热切换回调 apply_target(package, pattern)
        self._lock = threading.Lock()
        self._httpd = None
        self._thread = None

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

    def clear_latest(self):
        """清空实时缓冲（切换被测目标后新目标从零开始显示）。"""
        with self._lock:
            self.latest = []

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
                    self._send(200, json.dumps(self._list_device_apps(), ensure_ascii=False))
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
                    self._send(200, json.dumps(self._list_runs(), ensure_ascii=False))
                elif path == "/api/report":
                    qs = parse_qs(parsed.query)
                    name = (qs.get("name") or [""])[0]
                    self._send(200, json.dumps(self._load_report(name), ensure_ascii=False))
                elif path == "/api/events":
                    # logcat 事件标注（模式1：与报告同目录的 perfdog_xxx.events.jsonl）
                    qs = parse_qs(parsed.query)
                    name = (qs.get("name") or [""])[0]
                    self._send(200, json.dumps(self._load_events(name), ensure_ascii=False))
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
            def _list_device_apps(self):
                """adb 列出已装第三方应用（pm list packages -3 -f），尝试解析应用名（label）。

                label 来源：设备自带 aapt（部分 ROM 有 /system/bin/aapt）→ `aapt dump badging`
                解析 application-label；解析结果缓存到本地 .app_labels.json（按 serial 分 key）。
                无 aapt / 解析失败 → label 回退为包名（不影响点选切换）。
                """
                if not server.adb:
                    return {"ok": False, "apps": [], "error": "离线看板无设备连接，请用 start_perfdog.bat 启动采集"}
                try:
                    out = server.adb.shell(["pm", "list", "packages", "-3", "-f"])
                except Exception as e:
                    return {"ok": False, "apps": [], "error": f"列出应用失败: {e}"}
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
                # 解析/复用 label（有缓存先读缓存，只对缺失项跑 aapt）
                labels = self._load_app_labels(server.adb.serial)
                aapt = self._find_aapt() if server.adb else None
                changed = False
                for a in apps:
                    lbl = labels.get(a["pkg"])
                    if lbl is None and aapt and a["apk"]:
                        lbl = self._aapt_label(aapt, a["apk"]) or ""
                        labels[a["pkg"]] = lbl
                        changed = True
                    a["label"] = (lbl or a["pkg"])
                    a.pop("apk", None)
                if changed:
                    self._save_app_labels(server.adb.serial, labels)
                return {"ok": True, "apps": apps}

            def _find_aapt(self):
                """探测设备上可用的 aapt/aapt2 路径；找不到返回 None（降级显示包名）。"""
                try:
                    out = server.adb.shell(["sh", "-c",
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
                    out = server.adb.shell([aapt, "dump", "badging", apk_path])
                except Exception:
                    return None
                for line in out.splitlines():
                    s = line.strip()
                    # 常见两种格式：
                    #   application-label:'中文名'
                    #   application: label='中文名' icon='...'
                    m = re.search(r"application-label:['\"](.*?)['\"]", s)
                    if m:
                        return m.group(1)
                    m2 = re.search(r"application:\s+label='(.*?)'", s)
                    if m2:
                        return m2.group(1)
                return None

            def _load_app_labels(self, serial):
                try:
                    with open(APP_LABEL_CACHE, encoding="utf-8") as f:
                        data = json.load(f)
                    return data.get(serial, {})
                except Exception:
                    return {}

            def _save_app_labels(self, serial, labels):
                try:
                    data = {}
                    if os.path.isfile(APP_LABEL_CACHE):
                        with open(APP_LABEL_CACHE, encoding="utf-8") as f:
                            data = json.load(f)
                    data[serial] = labels
                    with open(APP_LABEL_CACHE, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

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
                """递归扫描 output 下所有 jsonl（含按时间命名的子文件夹），最新在前。"""
                out = []
                base = os.path.realpath(server.output_dir)
                if os.path.isdir(base):
                    for root, _, files in os.walk(base):
                        for fn in files:
                            if not fn.endswith(".jsonl"):
                                continue
                            fp = os.path.join(root, fn)
                            rel = os.path.relpath(fp, base).replace("\\", "/")
                            try:
                                st = os.stat(fp)
                                n = sum(1 for _ in open(fp, encoding="utf-8"))
                            except Exception:
                                continue
                            remark = ""
                            rp = fp + ".remark.txt"
                            if os.path.isfile(rp):
                                try:
                                    remark = open(rp, encoding="utf-8").read().strip()
                                except Exception:
                                    pass
                            out.append({
                                "name": rel,
                                "size_kb": round(st.st_size / 1024, 1),
                                "points": n,
                                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
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
                base = os.path.realpath(server.output_dir)
                fp = os.path.realpath(os.path.join(base, name))
                if not fp.startswith(base + os.sep):
                    return {"error": "bad path"}
                try:
                    rows = []
                    with open(fp, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                rows.append(json.loads(line))
                    return rows
                except Exception as e:
                    return {"error": str(e)}

        return Handler
