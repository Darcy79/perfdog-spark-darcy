# -*- coding: utf-8 -*-
"""只启动 Web 看板（不采集），用于离线查看历史报告。

用法：uv run --no-project python dashboard.py [--port 8080]
"""

import argparse
import sys
import time

from web import WebServer


def main():
    ap = argparse.ArgumentParser(description="PerfDog-CN 历史看板（无需手机）")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--output", default="output")
    args = ap.parse_args()

    web = WebServer(port=args.port, output_dir=args.output)
    port = web.start()
    web.set_status(running=False, device="(离线)", pid=None)
    print(f"[+] 看板已启动: http://localhost:{port}")
    print(f"[+] 历史报告: http://localhost:{port}/report.html")
    print("[*] Ctrl+C 退出")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
