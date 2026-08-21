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
import random
import re
import threading
import time
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
                       "target": None, "process_pattern": ""}
        self.adb = adb              # 采集器注入的 adb 实例（离线看板为 None）
        self.switch_cb = switch_cb  # 采集器注入的热切换回调 apply_target(package, pattern)
        self._resolving = False     # 后台应用名(label)解析进行中
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
