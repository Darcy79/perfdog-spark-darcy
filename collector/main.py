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

    # 可选：实时 Web 看板
    web = None
    if args.web:
        from web import WebServer
        web = WebServer(port=args.port, output_dir=outdir, adb=adb)
        port = web.start()
        # 启动指引（exe 版没有 bat 的说明文字，关键信息必须在这里讲清楚）：
        # 看板地址 / 历史报告地址 / 数据目录绝对路径 / 如何停止
        print("")
        print("=" * 60)
        print("  PerfDog-CN 实时看板已启动")
        open_hint = "（即将自动打开浏览器）" if not args.no_browser else ""
        print(f"  实时看板  : http://localhost:{port}  {open_hint}".rstrip())
        print(f"  历史报告  : http://localhost:{port}/report.html")
        print(f"  数据目录  : {os.path.abspath(outdir)}")
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
        web.set_status(running=True, device=adb.serial, pid=pid, run_id=run_id,
                       target=package, process_pattern=process_pattern,
                       started_at=datetime.now().strftime("%H:%M:%S"))

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

    stop = {"flag": False}

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

    start = time.time()
    n = 0
    with open(out_file, "w", encoding="utf-8") as f:
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
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            n += 1
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
            time.sleep(max(0.05, args.interval))

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
