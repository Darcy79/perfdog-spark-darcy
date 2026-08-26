# -*- coding: utf-8 -*-
"""JSONL 采集数据导出工具。

用法（在 collector 目录下）：
  # 导出自包含 HTML 报告（ECharts 内联，双击即看，可任意拷贝）
  uv run --no-project python export_report.py --input output/xxx.jsonl --format html

  # 导出 CSV（Excel 可直接打开）
  uv run --no-project python export_report.py --input output/xxx.jsonl --format csv

  # 导出 Excel .xlsx（需要 openpyxl，uv 零安装：--with）
  uv run --with openpyxl python export_report.py --input output/xxx.jsonl --format xlsx

  # 指定输出路径
  ... --out 自定义路径.html/csv/xlsx
"""

import argparse
import csv
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(SCRIPT_DIR, "..", "web")
ASSETS_DIR = os.path.join(WEB_DIR, "assets")

# 指标列（CSV/XLSX 通用，扁平化）
COLUMNS = [
    ("t_ms", "相对时间ms"), ("fps", "FPS"), ("jank_rate", "Jank率"),
    ("frame_p50_ms", "帧时间P50ms"), ("frame_p95_ms", "帧时间P95ms"),
    ("frame_max_ms", "帧时间Maxms"), ("refresh_hz", "刷新率Hz"),
    ("fps_source", "FPS通道"),
    ("cpu_total_pct", "CPU整机%"), ("cpu_proc_pct", "CPU进程%"),
    ("pss_mb", "PSS内存MB"), ("rss_mb", "RSS内存MB"),
    ("rx_kbps", "下行KB/s"), ("tx_kbps", "上行KB/s"),
    ("temp_c", "电池温度C"), ("power_w", "功率W"),
    ("current_ma", "电流mA"), ("voltage_v", "电压V"),
]


def load_rows(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def extract_cores(rows, default=None):
    """从 meta 行（{"event":"meta","cores":N}）读核数；无则返回 default。"""
    for r in rows or []:
        if isinstance(r, dict) and r.get("event") == "meta" and r.get("cores"):
            try:
                v = int(r["cores"])
                if 1 <= v <= 256:
                    return v
            except (TypeError, ValueError):
                pass
    return default


def data_rows(rows):
    """过滤 event 行（meta / target_switch），只保留真正的采样点。

    event 行没有 t_ms / 指标字段，若当采样点导出会产生空行，且 x 轴类目被塞进 NaN。
    核数单独经 extract_cores 读取，不在此处丢失。
    """
    return [r for r in (rows or []) if isinstance(r, dict) and not r.get("event")]


def fps_source(f):
    """判定该采样点的 FPS 采集通道："sf" / "gfxinfo" / ""（无数据或错误）。

    两通道 Jank/帧时间口径不同（sf 按帧间隔 >2×刷新周期，gfxinfo 用系统 Janky 计数），
    同一次采集中途可能切换 → 导出必须能区分，否则整段数据被当成同一口径解读。
    gfxinfo 结果自带 source 字段；SF 通道结果没有（不改 jsonl schema，此处按
    "有层名 + 带 refresh_hz 键" 反推），错误样本（no_layer/read_fail）留空。
    """
    if not f:
        return ""
    src = f.get("source")
    if src:
        return str(src)
    return "sf" if f.get("layer") and "refresh_hz" in f else ""


def flatten(row):
    """将嵌套 jsonl 扁平化为指标字典。"""
    out = {"t_ms": row.get("t_ms")}
    f = row.get("fps") or {}
    out.update({k: f.get(k) for k in ("fps", "jank_rate", "frame_p50_ms", "frame_p95_ms",
                                      "frame_max_ms", "refresh_hz")})
    out["fps_source"] = fps_source(f)
    c = row.get("cpu") or {}
    out.update({"cpu_total_pct": c.get("cpu_total_pct"), "cpu_proc_pct": c.get("cpu_proc_pct")})
    m = row.get("mem") or {}
    out["pss_mb"] = round(m["pss_kb"] / 1024, 2) if m.get("pss_kb") is not None else None
    out["rss_mb"] = round(m["vmrss_kb"] / 1024, 2) if m.get("vmrss_kb") is not None else None
    n = row.get("net") or {}
    out.update({"rx_kbps": n.get("rx_kbps"), "tx_kbps": n.get("tx_kbps")})
    t = row.get("therm") or {}
    out.update({"temp_c": t.get("temp_c"), "power_w": t.get("power_w"),
                "current_ma": t.get("current_ma"), "voltage_v": t.get("voltage_v")})
    return out


def export_csv(rows, out_path):
    rows = data_rows(rows)   # 过滤 meta/target_switch 事件行，避免空行
    flat = [flatten(r) for r in rows]
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([label for _, label in COLUMNS])
        for r in flat:
            w.writerow([r.get(k, "") for k, _ in COLUMNS])
    return len(flat)


def export_xlsx(rows, out_path):
    try:
        from openpyxl import Workbook
    except ImportError:
        print("[!] 需要 openpyxl。请用以下命令运行（uv 零安装）：")
        print('    uv run --with openpyxl python export_report.py --input ... --format xlsx')
        sys.exit(1)
    rows = data_rows(rows)
    flat = [flatten(r) for r in rows]
    wb = Workbook()
    ws = wb.active
    ws.title = "perfdog"
    ws.append([label for _, label in COLUMNS])
    for r in flat:
        ws.append([r.get(k, "") for k, _ in COLUMNS])
    wb.save(out_path)
    return len(flat)


def export_html(rows, out_path, events=None):
    """自包含 HTML：内联 echarts + app.js + 样式 + 数据（+ 可选 logcat 事件标注）。"""
    def read(p):
        with open(p, encoding="utf-8") as f:
            return f.read()

    cores = extract_cores(rows)      # 从 meta 行读核数（供 CPU 图"进程占整机%"）
    rows = data_rows(rows)           # 过滤 meta/target_switch 事件行
    echarts = read(os.path.join(ASSETS_DIR, "echarts.min.js"))
    appjs = read(os.path.join(ASSETS_DIR, "app.js"))
    style = read(os.path.join(ASSETS_DIR, "style.css"))
    data = json.dumps(rows, ensure_ascii=False)
    # 自动探测同目录事件文件 <name>.events.jsonl（logcat 标注层）
    if events is None:
        events = []
        try:
            # 用 removesuffix 语义拼接：out_path 中若含其他 ".html" 段（如目录名）
            # replace(".html", ...) 会错替换，只剥末尾 5 字符（2026-08-21 修复）
            ev_path = (out_path[:-len(".html")] + ".events.jsonl") \
                if out_path.endswith(".html") else out_path + ".events.jsonl"
            with open(ev_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            pass
    events_json = json.dumps(events, ensure_ascii=False)
    cores_json = json.dumps(cores)   # None → "null"，JS 侧 falsy

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PerfDog 自研工具 · 报告</title>
<style>{style}</style>
</head>
<body>
<header>
  <h1>性能采集报告</h1>
  <div id="status-bar"><span id="report-meta">{os.path.basename(out_path)} · {len(rows)} 个采样点 · 本地生成</span></div>
</header>
<main>
  <div class="summary" id="report-summary"></div>
  <section class="chart-card"><div class="chart-head"><h2>FPS / Jank</h2><div id="stat-fps" class="stat-line"></div></div><div id="chart-fps" class="chart"></div></section>
  <section class="chart-card"><div class="chart-head"><h2>帧时间 (ms)</h2><div id="stat-frametime" class="stat-line"></div></div><div id="chart-frametime" class="chart"></div></section>
  <section class="chart-card"><div class="chart-head"><h2>CPU 占用</h2><div id="stat-cpu" class="stat-line"></div></div><div id="chart-cpu" class="chart"></div></section>
  <section class="chart-card"><div class="chart-head"><h2>内存 (PSS/RSS)</h2><div id="stat-mem" class="stat-line"></div></div><div id="chart-mem" class="chart"></div></section>
  <section class="chart-card"><div class="chart-head"><h2>网络流量</h2><div id="stat-net" class="stat-line"></div></div><div id="chart-net" class="chart"></div></section>
  <section class="chart-card"><div class="chart-head"><h2>电池温度 / 功率</h2><div id="stat-temp" class="stat-line"></div></div><div id="chart-temp" class="chart"></div></section>
</main>
<footer><span>自研 PerfDog · 数据仅存本地，不传云端</span></footer>
<script>{echarts}</script>
<script>{appjs}</script>
<script>
(function () {{
  var charts = {{ fps: null, frametime: null, cpu: null, mem: null, net: null, temp: null }};
  charts.fps = window.PerfCharts.makeChart('chart-fps');
  charts.frametime = window.PerfCharts.makeChart('chart-frametime');
  charts.cpu = window.PerfCharts.makeChart('chart-cpu');
  charts.mem = window.PerfCharts.makeChart('chart-mem');
  charts.net = window.PerfCharts.makeChart('chart-net');
  charts.temp = window.PerfCharts.makeChart('chart-temp');
  var ROWS = {data};
  var CORES = {cores_json};
  if (CORES) window.PerfCharts.setCores(CORES);
  window.PerfCharts.renderAll(charts, ROWS, {{ zoom: true }});
  window.PerfCharts.updateStats(charts, ROWS);
  window.PerfCharts.renderSummary('report-summary', window.PerfCharts.computeStats(ROWS));
  window.PerfCharts.initSortable('main');
  window.PerfCharts.setPinData(ROWS);
  window.PerfCharts.enableClickPin('main');
  window.PerfCharts.createTimeSliders(charts, ROWS);
  var EVENTS = {events_json};
  if (EVENTS && EVENTS.length) window.PerfCharts.renderEvents(charts, ROWS, EVENTS);
}})();
</script>
</body>
</html>
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description="PerfDog-CN JSONL 导出工具")
    ap.add_argument("--input", required=True, help="输入 jsonl 路径")
    ap.add_argument("--format", choices=["html", "csv", "xlsx"], default="html")
    ap.add_argument("--out", default="", help="输出路径（默认与输入同名，扩展名按格式）")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        print(f"[!] 输入文件不存在: {args.input}")
        sys.exit(1)

    rows = load_rows(args.input)
    if not rows:
        print("[!] 数据为空")
        sys.exit(1)

    out = args.out or os.path.splitext(args.input)[0] + "." + args.format
    if args.format == "csv":
        n = export_csv(rows, out)
    elif args.format == "xlsx":
        n = export_xlsx(rows, out)
    else:
        # 事件文件跟随输入 jsonl 所在目录（<name>.events.jsonl），与输出路径无关
        events = []
        ev_path = args.input.replace(".jsonl", ".events.jsonl")
        if os.path.isfile(ev_path):
            try:
                with open(ev_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                events.append(json.loads(line))
                            except Exception:
                                pass
            except Exception:
                pass
        n = export_html(rows, out, events)
    print(f"[+] 已导出 {n} 个采样点 -> {os.path.abspath(out)}")
    if args.format == "html":
        print("[+] 双击该 HTML 即可在浏览器查看（自包含，可任意拷贝分享）")


if __name__ == "__main__":
    main()
