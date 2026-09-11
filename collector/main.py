# -*- coding: utf-8 -*-
"""自研 PerfDog 采集器 — 第一阶段骨架（FPS/CPU/内存）。

用法:
    python main.py                          # 默认 config.json，1s 间隔，Ctrl+C 停止
    python main.py --duration 120           # 采集 120 秒
    python main.py --interval 0.5           # 0.5s 间隔
    python main.py --output ../test1        # 指定输出目录

输出: <output>/perfdog_<YYYYmmdd_HHMMSS>.jsonl（每行一个采样点）
"""

import argparse
import json
import os
import queue
import signal
import sys
import threading
import time
import webbrowser
from datetime import datetime

from adb import Adb, AdbError
from pidresolver import PidResolver
from metrics.fps import FpsCollector
from metrics.cpu import CpuCollector
from metrics.mem import MemCollector
from metrics.network import NetworkCollector
from metrics.thermal import ThermalCollector
from logcat import LogcatMonitor
from data_health import check_rows_live
from device_info import probe_device_info

# 各指标独立采样间隔（秒）。FPS 高频（0.5s）让 Jank 及时出现；
# 内存/温度低频（2s）避免 dumpsys 拖慢整体。并行后互不阻塞。
SAMPLER_INTERVALS = {
    "fps": 0.5,
    "cpu": 1.0,
    "mem": 2.0,
    "net": 1.0,
    "therm": 2.0,
}


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# 断连/半死退避参数（v61）：连续失败 streak 达到 FAIL_ALERT_STREAK 即告警；
# 此后主循环 sleep 按 streak 递增（上限 BACKOFF_MAX_S），避免 adb 每次 shell
# 阻塞到 timeout（20s）时仍按 1s 节奏空转猛撞超时、把告警拖到最坏 3 分钟。
FAIL_ALERT_STREAK = 3
BACKOFF_MAX_S = 5.0
# 目标一致性自检间隔（秒，独立线程跑；2026-09-11）——不能放主采样循环：
# 设备半死时 dumpsys 会阻塞到 adb 超时（20s），把采样点间隔拉到 21s。
MISMATCH_CHECK_INTERVAL = 10.0


def backoff_sleep(base_iv, fail_streak):
    """按连续失败轮数计算本轮 sleep 秒数（纯函数便于单测）。

    fail_streak < FAIL_ALERT_STREAK：正常节奏 base_iv（下限 0.05s）；
    达到阈值后线性退避 base_iv × streak，封顶 BACKOFF_MAX_S。
    设备恢复（streak 清零）后立即回到正常节奏。
    """
    base = max(0.05, float(base_iv or 0.05))
    streak = int(fail_streak or 0)
    if streak < FAIL_ALERT_STREAK:
        return base
    return min(base * streak, BACKOFF_MAX_S)


def row_has_any_value(row):
    """判断采样行是否包含至少一个有效指标值（首点落盘门槛用，纯函数）。

    "有效" = 任一指标 dict 有非 None 的关键值，**或任一指标带 error**——
    失败本身也是信息（链路抖动时的首批 probe_fail 行必须留痕，否则事后
    更无法判读），只有"各指标线程尚未产出首份快照"的全空行才该跳过
    （2026-09-11 实测：首点 t≈0.4s 全指标 None）。throttled 不是有效值。
    """
    for k in ("fps", "cpu", "mem", "net", "therm"):
        v = row.get(k)
        if not isinstance(v, dict):
            continue
        if v.get("error"):
            return True
        for kk in ("fps", "cpu_total_pct", "cpu_proc_pct", "pss_kb", "vmrss_kb",
                   "rx_kbps", "tx_kbps", "temp_c", "power_w", "voltage_v"):
            if v.get(kk) is not None:
                return True
    return False


class ChannelAlertTracker:
    """缺数/断连事件状态机（纯逻辑，可单测；2026-09-11 事故复盘新增）。

    背景：断连告警此前只 print + set_status，不写 jsonl——run 20260911_162353
    出现大面积空洞后无法从数据判断"当时链路是否故障"，事后不可判读。
    两个独立维度的进入/恢复沿各发一次事件（状态沿去重 = 天然节流，不刷屏）：
      missing_metric：≥MISSING_ERR_COUNT 个指标带 error 且未达断连阈值
                      → 事件带错误码分布 detail；
      disconnect    ：≥DISCONNECT_ERR_COUNT（多数指标 error，与原断连告警
                      阈值一致）→ 更严重，只发 disconnect（不发 missing）；
      recovered     ：任一维度从"在状态"回到阈值以下。
    update(err_codes) 传入本轮带 error 的指标错误码列表，返回事件 dict 列表
    （不含 ts，由落盘方补），可为空。事件行带 event 字段，前端 prepareRows /
    导出 data_rows / data_health 均按该字段跳过，不参与采样点统计。
    """

    DISCONNECT_ERR_COUNT = 4   # 5 个指标中 ≥4 带 error（沿用原断连告警阈值）
    MISSING_ERR_COUNT = 3

    def __init__(self):
        self._in_disconnect = False
        self._in_missing = False

    def update(self, err_codes):
        events = []
        n = len(err_codes)
        dist = {}
        for c in err_codes:
            dist[c] = dist.get(c, 0) + 1
        # --- 缺数维度（3 ≤ n < 4）：进入沿只发一次，带错误码分布 ---
        if self.MISSING_ERR_COUNT <= n < self.DISCONNECT_ERR_COUNT \
                and not self._in_missing:
            self._in_missing = True
            events.append({"event": "channel_alert", "kind": "missing_metric",
                           "detail": {"err_count": n, "err_codes": dist}})
        elif n < self.MISSING_ERR_COUNT and self._in_missing:
            self._in_missing = False
            events.append({"event": "channel_alert", "kind": "recovered",
                           "detail": {"err_count": n, "scope": "missing"}})
        # --- 断连维度（n ≥ 4）：进入沿发 disconnect，恢复发 recovered ---
        if n >= self.DISCONNECT_ERR_COUNT and not self._in_disconnect:
            self._in_disconnect = True
            events.append({"event": "channel_alert", "kind": "disconnect",
                           "detail": {"err_count": n, "err_codes": dist}})
        elif n < self.DISCONNECT_ERR_COUNT and self._in_disconnect:
            self._in_disconnect = False
            events.append({"event": "channel_alert", "kind": "recovered",
                           "detail": {"err_count": n, "scope": "disconnect"}})
        return events


def main():
    ap = argparse.ArgumentParser(description="自研 PerfDog 采集器（第一阶段：FPS/CPU/内存）")
    ap.add_argument("--config", default="config.json", help="配置文件路径")
    ap.add_argument("--package", default=None, help="覆盖目标包名（测其他 App，如 --package com.example.game）")
    ap.add_argument("--process-pattern", default=None,
                    help="覆盖进程匹配模式；测原生 App 时传空字符串 --process-pattern \"\"")
    ap.add_argument("--show-foreground", action="store_true",
                    help="仅打印当前前台应用的包名/窗口，然后退出（用于找要测的 App）")
    ap.add_argument("--serial", default="", help="ADB 设备序列号，默认自动选第一台")
    ap.add_argument("--duration", type=float, default=0, help="采集时长(秒)，0=手动停止")
    ap.add_argument("--interval", type=float, default=1.0, help="采样间隔(秒)")
    ap.add_argument("--output", default="output", help="输出目录")
    ap.add_argument("--web", action="store_true", help="启动实时 Web 看板")
    ap.add_argument("--port", type=int, default=8080, help="Web 看板端口（默认 8080）")
    ap.add_argument("--no-browser", action="store_true",
                    help="启动看板后不自动打开浏览器（无头/CI 场景用）")
    ap.add_argument("--auto", action="store_true",
                    help="跳过启动向导：自动解析目标进程并立即开始采集（旧行为/脚本用）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    package = args.package if args.package is not None else cfg.get("package", "com.tencent.mm")
    process_pattern = args.process_pattern if args.process_pattern is not None \
        else cfg.get("process_pattern", "appbrand")
    serial = args.serial or cfg.get("serial", "")
    target_cur = package   # 当前被测目标（写进每个采样点，供历史报告显示；热切换时更新）

    outdir = args.output          # 输出根目录（output/）
    os.makedirs(outdir, exist_ok=True)

    try:
        adb = Adb(serial)
    except AdbError as e:
        print(f"[-] {e}")
        sys.exit(1)
    print(f"[+] 已连接设备: {adb.serial}")

    # 仅打印当前前台应用（方便确定要测哪个 App），然后退出
    if args.show_foreground:
        try:
            out = adb.shell(["dumpsys", "window"])
            for line in out.splitlines():
                if "mCurrentFocus" in line:
                    print(f"[+] 当前前台窗口: {line.strip()}")
                    break
            else:
                print("[-] 未取到前台窗口")
        except Exception as e:
            print(f"[-] 获取前台窗口失败: {e}")
        sys.exit(0)

    # 停止标志：Ctrl+C / 看板"停止采集"按钮（POST /api/stop）共用同一路径
    # （复用首次 Ctrl+C 的完整停止逻辑：退采样循环 → 停 logcat → 生成报告 → running=False）
    stop = {"flag": False}
    # 看板"退出程序"（POST /api/shutdown）：停止采集后跳过"看板仍在运行"等待，直接结束进程
    shutdown_req = {"flag": False}

    # ---------------- 启动向导（2026-09-11）：先探测、用户确认后才开始记录 ----------------
    # 背景：微信可同时存在 appbrand0/1/2 三个实例，自动选进程曾两次采到闲置实例
    # （2026-09-11 实测：采到 appbrand0 PSS 231MB / CPU 增量近 0，而游戏实际在
    # appbrand1：PSS 1035MB；FPS 层名 AppBrandUI1 与进程 appbrand0 不匹配）。
    # 新流程：探测（只读，**不创建任何采集输出**）→ 网页列出候选与推荐 → 用户选定
    # → 才创建 jsonl 并开始采样。命令行 --auto（或不带 --web）保持旧的自动解析行为。
    wizard = bool(args.web and not args.auto)

    web = None
    if args.web:
        from web import WebServer
        web = WebServer(port=args.port, output_dir=outdir, adb=adb,
                        process_pattern=process_pattern)
        port = web.start()
        # 启动指引（exe 版没有 bat 的说明文字，关键信息必须在这里讲清楚）：
        # 看板地址 / 历史报告地址 / 数据目录绝对路径 / 如何开始与停止
        print("")
        print("=" * 60)
        print("  PerfDog-CN 看板已启动")
        open_hint = "（即将自动打开浏览器）" if not args.no_browser else ""
        print(f"  实时看板  : http://localhost:{port}  {open_hint}".rstrip())
        print(f"  历史报告  : http://localhost:{port}/report.html")
        print(f"  数据目录  : {os.path.abspath(outdir)}")
        if wizard:
            print("  采集流程  : ① 网页上选择目标进程 ② 点「开始采集」→ 才开始记录数据")
        else:
            print("  停止方式  : 本窗口按 Ctrl+C 一次停采集，再按一次退出")
        print("=" * 60)
        print("")
        if not args.no_browser:
            # 延迟打开：等服务线程就绪（start() 已绑定端口，稍等更稳妥）
            def _open_browser():
                time.sleep(1.5)
                try:
                    webbrowser.open(f"http://localhost:{port}")
                except Exception:
                    pass
            threading.Thread(target=_open_browser, daemon=True,
                             name="open-browser").start()

    chosen_pid, chosen_name = None, None
    if wizard:
        from probe import probe_once
        print("[*] 正在探测微信候选进程（只读，不记录任何采集数据）…", flush=True)
        probe_info = probe_once(adb, process_pattern)
        if probe_info.get("ok"):
            layer = probe_info.get("layer") or {}
            print("[+] 候选进程（网页上可选择，★为推荐）：")
            for c in probe_info["candidates"]:
                mark = " ★推荐" if c.get("recommended") else ""
                print(f"    pid={c['pid']:<7} {c['name']:<30} 内存 {c['rss_mb']}MB  "
                      f"CPU增量 {c['cpu_delta_pct']}%{mark}")
            if layer.get("name"):
                print(f"[+] 游戏渲染层: {layer['name']}"
                      f"（AppBrandUI{layer.get('appbrand_index')}）")
            print(f"[+] 推荐: pid={probe_info.get('recommended_pid')}"
                  f"（{probe_info.get('recommend_detail')}）")
        else:
            print(f"[!] 探测未成功: {probe_info.get('error')}")
        if web:
            web.set_status(phase="waiting", candidates=probe_info,
                           device=adb.serial, target=package,
                           process_pattern=process_pattern)
        print("[*] 网页上选择目标进程 → 点「开始采集」；也可直接在本窗口按回车（用推荐项）"
              "或输入 pid 后回车；命令行 --auto 可跳过向导")
        # 终端兜底输入（网页界面做好之前的可用路径）：后台线程读一行，主循环轮询
        _in_q = queue.Queue()

        def _stdin_reader():
            try:
                _in_q.put(input())
            except Exception:
                pass

        threading.Thread(target=_stdin_reader, daemon=True, name="stdin-reader").start()
        _last_wait_log = 0.0
        while not stop["flag"]:
            req = web.take_start_request() if web else None
            if req:
                chosen_pid = req.get("pid")
                chosen_name = req.get("name")
                print(f"[>] 收到开始指令（网页）: pid={chosen_pid} {chosen_name}", flush=True)
                break
            try:
                line = (_in_q.get_nowait() or "").strip()
            except queue.Empty:
                line = None
            if line is not None:
                if line == "":
                    rec = (probe_info or {}).get("recommended_pid")
                    if rec:
                        chosen_pid = rec
                        for c in (probe_info or {}).get("candidates", []):
                            if c["pid"] == rec:
                                chosen_name = c["name"]
                                break
                        print(f"[>] 回车确认 → 使用推荐 pid={chosen_pid} {chosen_name}", flush=True)
                        break
                    print("[!] 无推荐项可用，请在网页上选择（或输入 pid）", flush=True)
                elif line.isdigit():
                    chosen_pid = int(line)
                    for c in (probe_info or {}).get("candidates", []):
                        if c["pid"] == chosen_pid:
                            chosen_name = c["name"]
                            break
                    print(f"[>] 已选定 pid={chosen_pid} {chosen_name}", flush=True)
                    break
                elif line:
                    print(f"[!] 无法识别输入 {line!r}：回车用推荐项，或输入候选 pid", flush=True)
                threading.Thread(target=_stdin_reader, daemon=True,
                                 name="stdin-reader").start()
            if time.time() - _last_wait_log > 30:
                _last_wait_log = time.time()
                print("[*] 仍在等待「开始采集」…（网页点按钮 / 本窗口回车 / Ctrl+C 退出）",
                      flush=True)
            time.sleep(0.3)
        if stop["flag"]:
            print("[=] 已取消，未创建任何采集数据。")
            if web:
                web.stop()
            return
        if not chosen_pid:
            # 理论上 /api/start 会带 pid；这里兜底用推荐项，避免"开始了却没有目标"
            rec = (probe_info or {}).get("recommended_pid")
            if rec:
                chosen_pid = rec
                for c in (probe_info or {}).get("candidates", []):
                    if c["pid"] == rec:
                        chosen_name = c["name"]
                        break
                print(f"[!] 未指定进程，回退到推荐 pid={chosen_pid}")

    # 目标进程：向导模式用用户选定的 pid（固定，不再自动改选）；否则旧行为自动解析
    if chosen_pid:
        resolver = PidResolver(adb, package, process_pattern,
                               fixed_pid=chosen_pid, fixed_name=chosen_name)
        pid = resolver.resolve()
        print(f"[+] 目标进程（用户指定）: {chosen_name or package} pid={pid}")
    else:
        resolver = PidResolver(adb, package, process_pattern)
        pid = resolver.resolve()
        if pid:
            print(f"[+] 目标进程: {package}（匹配 {process_pattern or '主进程'}） pid={pid}")
        else:
            print(f"[!] 未找到 {package} 的进程，请确认小游戏已打开且在前台。")

    fps = FpsCollector(adb, package, process_pattern)
    cpu = CpuCollector(adb, resolver)
    mem = MemCollector(adb, resolver, package, min_interval=0)  # 由线程间隔(2s)控制，不再内部节流
    net = NetworkCollector(adb, resolver)
    therm = ThermalCollector(adb)
    collectors = {"fps": fps, "cpu": cpu, "mem": mem, "net": net, "therm": therm}

    # 探测核数（2026-08-26）：供前端 CPU 图"进程占整机%"派生曲线（cpu_proc_pct ÷ 核数）。
    # 写入 status（实时看板）+ jsonl meta 行（历史报告），让历史报告不依赖当前是否连设备。
    cores = CpuCollector.probe_cores(adb)
    print(f"[+] CPU 核数: {cores}")

    # 探测设备信息（2026-08-27）：型号/市场名/平台/CPU/分辨率，一次 shell 往返。
    # 失败项静默 None，不影响采集；写入 status + meta 行供报告展示。
    device_info = probe_device_info(adb)
    if device_info.get("model"):
        print(f"[+] 设备: {device_info.get('market_name') or device_info.get('model')}"
              f"（{device_info.get('model')}）")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 每次采集新建一个按时间命名的文件夹，内含 jsonl 与 html 报告，避免历史数据混淆
    run_dir = os.path.join(outdir, run_id)
    os.makedirs(run_dir, exist_ok=True)
    out_file = os.path.join(run_dir, f"perfdog_{run_id}.jsonl")
    print(f"[*] 开始采集（间隔 {args.interval}s，{'Ctrl+C 停止' if not args.duration else f'{args.duration}s'}）")
    print(f"[*] 数据目录: {run_dir}")

    # 模式1：logcat 事件监听（零侵入捞小游戏 console.log，叠加看板标注层）
    # 目标为微信小游戏时启用；原生 App 采集无需日志标注
    monitor = None
    events_file = None
    if package.lower() == "com.tencent.mm":
        try:
            monitor = LogcatMonitor(adb, serial)
            monitor.start()
            events_file = os.path.join(run_dir, f"perfdog_{run_id}.events.jsonl")
            print(f"[+] logcat 事件监听已启动（模式1：捞 console.log，tag/关键词过滤，限流 {monitor._min_gap}s）")
        except Exception as e:
            monitor = None
            print(f"[!] logcat 监听启动失败（不影响性能采集）: {e}")

    # 实时 Web 看板：服务启动与"打开浏览器"已在启动向导阶段提前完成，
    # 这里只在**用户确认、采集真正开始后**更新状态——向导模式下此前不产生任何输出文件。
    if web:
        web.set_status(running=True, device=adb.serial, pid=pid, run_id=run_id,
                       target=package, process_pattern=process_pattern,
                       cores=cores, device_info=device_info,
                       started_at=datetime.now().strftime("%H:%M:%S"),
                       phase="running",
                       target_source=("user" if chosen_pid else "auto"))

        # ---- 看板下拉"切换被测应用"回调（方案 A 2026-08-20） ----
        # 热切换：不重启进程，重建绑定目标进程的采集器即可；下一次采样自动走新目标。
        def _apply_target(new_package, new_pattern=""):
            """按看板请求切换被测目标（重建采集器 + 更新进程/状态 + 持久化配置）。"""
            nonlocal pid, target_cur
            new_package = (new_package or "").strip()
            if not new_package:
                return False, "包名为空"
            # 1) 持久化到当前 config：下次双击 bat 启动默认用上次选的目标
            try:
                cfg["package"] = new_package
                cfg["process_pattern"] = new_pattern or ""
                with open(args.config, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[!] 目标持久化失败（不影响本次切换）: {e}")
            # 2) 重建采集器：fps/cpu/mem/net 都绑定 resolver 或 package，必须重建；
            #    therm（电池温度）不依赖目标，复用。worker 每轮从 collectors 取新实例。
            try:
                new_resolver = PidResolver(adb, new_package, new_pattern)
                new_pid = new_resolver.resolve()
                with lock:
                    collectors["fps"] = FpsCollector(adb, new_package, new_pattern)
                    collectors["cpu"] = CpuCollector(adb, new_resolver)
                    collectors["mem"] = MemCollector(adb, new_resolver, new_package, min_interval=0)
                    collectors["net"] = NetworkCollector(adb, new_resolver)
                    pid = new_pid
                    target_cur = new_package
                    web.clear_latest()   # 清空实时缓冲：新目标从零开始显示
                    web.set_status(pid=pid, target=new_package,
                                   process_pattern=new_pattern or "")
                # 3) jsonl 写一行目标切换标记（历史报告可识别两次目标的分界）
                try:
                    with open(out_file, "a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "ts": round(time.time(), 3),
                            "event": "target_switch",
                            "to": new_package,
                            "process_pattern": new_pattern or "",
                        }, ensure_ascii=False) + "\n")
                except Exception as e:
                    print(f"[!] 目标切换标记写入失败: {e}")
                msg = f"目标已切换为 {new_package}"
                if not new_pid:
                    msg += "（未找到进程，请确认应用已在前台打开）"
                print(f"[>] {msg}")
                return True, msg
            except Exception as e:
                return False, f"切换失败: {e}"

        web.set_switch_callback(_apply_target)

        # ---- 看板停止采集 / 退出程序 回调（2026-08-21 打包后改动 P0） ----
        def _stop_capture():
            """看板 POST /api/stop：复用首次 Ctrl+C 的停止路径（stop.flag=True）。"""
            stop["flag"] = True
            # 联动中止后台 label 解析（避免解析线程空转）
            web.abort_label_resolve()
            print("[>] 已从看板收到停止采集请求", flush=True)

        def _shutdown_all():
            """看板 POST /api/shutdown：停止采集并让程序走完停止流程后自然退出。"""
            _stop_capture()
            shutdown_req["flag"] = True

        web.set_stop_callback(_stop_capture)
        web.set_shutdown_callback(_shutdown_all)

    def _handler(sig, frame):
        if stop["flag"]:
            # 第二次 Ctrl+C：彻底退出（含 Web 服务）
            if web:
                web.stop()
            sys.exit(0)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handler)

    # ---------------- 并行采集（性能优化 2026-08-12） ----------------
    # 每个指标独立线程按各自间隔采样，写入 latest（加锁）；
    # 主线程每 args.interval 秒取最新快照落盘。FPS 高频 → Jank 实时性提升，
    # 慢指标（mem/therm）不再拖累快指标。
    latest = {}
    lock = threading.Lock()

    def _worker(key, interval):
        while not stop["flag"]:
            t = time.time()
            try:
                # 每轮从 collectors dict 取采样器：看板热切换目标时替换 dict 即自动切新目标
                with lock:
                    sampler = collectors[key]
                v = sampler.sample(t)
            except Exception as e:
                v = {f"{key}_error": str(e)}
            with lock:
                latest[key] = v
            time.sleep(max(0.05, interval - (time.time() - t)))

    threads = []
    for key, interval in SAMPLER_INTERVALS.items():
        th = threading.Thread(target=_worker, args=(key, interval),
                              daemon=True, name=f"sampler-{key}")
        th.start()
        threads.append(th)

    # 目标一致性自检（2026-09-11）：层里的 AppBrandUI(n) 应与正在采集的 appbrand(n)
    # 一致；不一致说明微信把渲染切到了别的实例（cpu/mem/net 采错进程）。
    # ⚠️ 必须独立线程：设备半死时 dumpsys 会阻塞到 adb 超时（20s），若放在主采样
    # 循环内会把采样点间隔拉长到 ~21s（2026-09-11 真机实测到该现象）。
    # **不自动切换**——切换前必然先采一段错数据；只写事件行 + 页面告警。
    def _mismatch_watch():
        state = {"msg": None}
        while not stop["flag"]:
            time.sleep(MISMATCH_CHECK_INTERVAL)
            if stop["flag"] or not chosen_pid:
                continue
            try:
                from probe import pick_game_layer, appbrand_index
                lname, lidx = pick_game_layer(
                    adb.shell(["dumpsys", "SurfaceFlinger", "--list"]))
                pidx = appbrand_index(resolver.proc_name or "")
                if lidx is not None and pidx is not None and lidx != pidx:
                    msg = (f"渲染层 AppBrandUI{lidx} 与采集进程 appbrand{pidx} 不一致"
                           f"（pid={resolver.pid}）——cpu/内存可能采错进程")
                    if msg != state["msg"]:
                        state["msg"] = msg
                        print(f"[!] 目标错配: {msg}", flush=True)
                        try:
                            with open(out_file, "a", encoding="utf-8") as mf:
                                mf.write(json.dumps({
                                    "ts": round(time.time(), 3),
                                    "event": "target_mismatch",
                                    "layer": lname,
                                    "layer_index": lidx,
                                    "pid_index": pidx,
                                    "pid": resolver.pid,
                                }, ensure_ascii=False) + "\n")
                        except Exception:
                            pass
                        if web:
                            web.set_status(mismatch={"layer": lname, "layer_index": lidx,
                                                     "pid_index": pidx, "pid": resolver.pid,
                                                     "message": msg})
                elif state["msg"]:
                    state["msg"] = None
                    if web:
                        web.set_status(mismatch=None)
            except Exception:
                pass

    if chosen_pid:
        threading.Thread(target=_mismatch_watch, daemon=True,
                         name="mismatch-watch").start()

    start = time.time()
    n = 0
    wrote_any = False   # 首点门槛：写出第一行有效数据前跳过全空行（任务⑤）
    # 设备断连诊断（2026-08-21）：连续 N 轮多数指标报错 → 探活 adb devices 醒目告警
    fail_streak = 0
    diag_shown = False
    # 缺数/断连事件状态机（2026-09-11 事故复盘）：告警同时落盘事件行，事后可判读
    channel_alerts = ChannelAlertTracker()
    # 数据健全性实时自检（2026-08-27）：每轮对当前快照跑轻量规则，连续命中才告警，
    # 复用断连告警的"连续 N 轮才提醒"思路，避免单点噪声刷屏
    health_streak = {}
    with open(out_file, "w", encoding="utf-8") as f:
        # meta 行（2026-08-26）：首行写入核数等采集元信息，供历史报告读取核数，
        # 不依赖当前是否连接设备。event 行不参与采样点统计（前端 prepareRows 过滤）。
        # 2026-08-27：加 proc_name（本次匹配到的进程名），事后可核对采集对象
        # （多 appbrand 进程并存时确认采到的是活跃进程）。
        f.write(json.dumps({
            "ts": round(time.time(), 3),
            "event": "meta",
            "cores": cores,
            "proc_name": getattr(resolver, "proc_name", None) or package,
            "device": device_info,
        }, ensure_ascii=False) + "\n")
        while not stop["flag"]:
            ts = time.time()
            if args.duration and (ts - start) >= args.duration:
                break
            with lock:
                row = {"ts": round(ts, 3), "t_ms": round((ts - start) * 1000, 1),
                       "target": target_cur}
                for k in SAMPLER_INTERVALS:
                    if k in latest:
                        row[k] = latest[k]

            # 断连监测：本轮多数指标带 error → 累计；恢复后清零
            err_codes = [row[k].get("error") for k in SAMPLER_INTERVALS
                         if isinstance(row.get(k), dict) and row[k].get("error")]
            err_count = len(err_codes)
            # 缺数/断连事件落盘（2026-09-11 事故复盘）：状态沿触发，去重不刷屏。
            # 事件行带 event 字段，前端 prepareRows / 导出 data_rows /
            # data_health 均按该字段跳过，不参与采样点统计。
            for ev in channel_alerts.update(err_codes):
                f.write(json.dumps({"ts": round(ts, 3), **ev},
                                   ensure_ascii=False) + "\n")
            if err_count >= len(SAMPLER_INTERVALS) - 1:
                fail_streak += 1
                if fail_streak >= FAIL_ALERT_STREAK and not diag_shown:
                    diag_shown = True
                    if adb.is_device_alive():
                        print("[!] 连续采样失败但设备在线：请确认目标应用在前台/渲染层存在", flush=True)
                        if web:
                            web.set_status(running=False)
                    else:
                        print("[!] 检测到设备断连！请检查 USB 连接（采集线程持续重试，恢复后自动继续）", flush=True)
                        if web:
                            web.set_status(running=False, device="断连")
            else:
                fail_streak = 0
                if diag_shown:
                    diag_shown = False
                    if web:
                        web.set_status(running=True, device=adb.serial)

            # 数据健全性实时自检：只跑关键规则（RSS<PSS / 采错进程线索），连续命中才告警
            health_alerts, health_streak = check_rows_live(row, health_streak)
            for msg in health_alerts:
                print(f"[!] 数据健全性告警: {msg}", flush=True)

            # 首点门槛（2026-09-11）：采集启动后各指标线程尚未产出首份快照时，
            # 首行全空（实测首点 t≈0.4s 全指标 None）。跳过"任何指标都无值"的行
            # 直到写出第一行有效数据；之后即使某行暂时全空也照写（中断/恢复形态
            # 要留痕）。主循环节奏未变，不影响 t_ms 起点语义与 --duration 计时。
            if wrote_any or row_has_any_value(row):
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                n += 1
                wrote_any = True
                if web:
                    web.add_sample(row)

            # logcat 事件轮询落盘（与采样点同目录，供看板叠加标注层）
            if monitor and events_file:
                try:
                    for ev in monitor.get_events():
                        with open(events_file, "a", encoding="utf-8") as ef:
                            ef.write(json.dumps(ev, ensure_ascii=False) + "\n")
                except Exception as e:
                    print(f"[!] 事件落盘失败: {e}")

            fps_v = row.get("fps") or {}
            cpu_v = row.get("cpu") or {}
            mem_v = row.get("mem") or {}
            fps_txt = "-"
            err = fps_v.get("error")
            if err == "no_layer":
                fps_txt = "无渲染层(游戏请在微信前台)"
            elif err == "probe_fail":
                fps_txt = "渲染层读取失败(链路抖动)"
            elif err == "layer_read_fail":
                fps_txt = "渲染层失效,重匹配中"
            elif fps_v.get("fps") is not None:
                fps_txt = fps_v["fps"]
            mem_txt = mem_v.get("pss_kb", "-")
            if mem_v.get("throttled"):
                mem_txt = "(节流)"
            net_v = row.get("net") or {}
            therm_v = row.get("therm") or {}
            temp_txt = therm_v.get("temp_c")
            net_txt = f"↓{net_v.get('rx_kbps', '-')}/↑{net_v.get('tx_kbps', '-')}KB/s"
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"FPS={fps_txt} Jank%={fps_v.get('jank_rate', '-')} "
                f"CPU总%={cpu_v.get('cpu_total_pct', '-')} CPU进程%={cpu_v.get('cpu_proc_pct', '-')} "
                f"PSS={mem_txt}kB {net_txt} 温度={temp_txt if temp_txt is not None else '-'}°C"
            )
            # v61：连续失败退避——设备半死（每次 adb shell 阻塞至 timeout）时
            # 降低主循环空转频率，避免把断连告警拖到最坏 3 分钟；恢复即回正常节奏。
            time.sleep(backoff_sleep(args.interval, fail_streak))

    print(f"[=] 采集结束，共 {n} 个采样点。已保存: {out_file}")

    if monitor:
        monitor.stop()
        if events_file and os.path.exists(events_file):
            n_ev = sum(1 for _ in open(events_file, encoding="utf-8"))
            print(f"[+] logcat 事件已保存: {events_file}（{n_ev} 条）")
        else:
            print(f"[!] 本次未捕获到 logcat 事件（游戏内无 console.log 输出，或 tag 未命中过滤规则）")

    # 自动生成 HTML 报告（自包含，双击即看），与 jsonl 同目录
    try:
        from export_report import load_rows, export_html
        rows = load_rows(out_file)
        if rows:
            html_path = os.path.join(run_dir, f"perfdog_{run_id}.html")
            export_html(rows, html_path)
            print(f"[+] 已生成 HTML 报告: {html_path}（双击打开即可查看）")
    except Exception as e:
        print(f"[!] HTML 报告生成失败（不影响数据）: {e}")

    if web:
        web.set_status(running=False)
        if shutdown_req["flag"]:
            # 看板"退出程序"：停止流程已走完（含 HTML 报告生成），直接结束进程
            print("[*] 已收到看板退出请求，程序退出。")
            web.stop()
            return
        print(f"[*] Web 看板仍在运行（可查看刚采集的数据与历史报告）:")
        print(f"[*]   实时看板/历史: http://localhost:{web.port}")
        print(f"[*]   历史报告页: http://localhost:{web.port}/report.html")
        print(f"[*] 再次按 Ctrl+C 退出")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
