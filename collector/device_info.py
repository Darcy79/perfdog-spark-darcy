# -*- coding: utf-8 -*-
"""设备信息探测（2026-08-27）。

启动时一次 `adb shell` 拿全设备信息（getprop 型号/平台 + wm size + cpuinfo_max_freq），
失败项静默置 None。写进 status（实时看板）+ jsonl meta 行（历史报告），
让报告打开即知被测设备，不依赖当前是否连接设备。

实测（荣耀 Magic3 Pro / ADT-AN00）：
  ro.product.model   = ADT-AN00
  ro.product.name    = ADT-AN00
  ro.board.platform  = lahaina          （骁龙 888 平台代号）
  /proc/cpuinfo Hardware = SM8350
  wm size            = Physical size: 1080x2388
  cpu0 cpuinfo_max_freq = 1804800 (kHz)
"""

import re

# 型号代码 → 市场名映射（部分厂商不写 ro.product.marketname，靠内置表兜底）。
# 便于扩充；未知型号显示"未知"。
MODEL_MARKET_MAP = {
    # 荣耀
    "ADT-AN00": "Magic3 Pro",
    "ADT-AL00": "Magic3",
    "MAA-AN00": "Magic7 Pro",
    "FNE-AN00": "Magic6 Pro",
    "BVL-AN00": "Magic5 Pro",
    "PGT-AN00": "Magic5",
    "LGE-AN00": "Magic4 Pro",
    # 华为
    "PLA-AL10": "Mate 70 Pro+",
    "PLR-AL00": "Mate 70 Pro",
    "CLS-AL00": "Mate 70",
    "LNA-AL00": "Mate 50 Pro",
    "RTE-AL00": "Mate 50",
    "BLA-AL00": "Mate 10 Pro",
    "LIO-AL00": "Mate 30 Pro",
    "TAH-AN00": "Mate 30",
    "NOH-AN00": "Mate 40 Pro",
    "OCE-AN00": "Mate 40",
    "ELS-AN00": "P40 Pro",
    "ANA-AN00": "P40",
    "VOG-AL00": "P30 Pro",
    # OPPO
    "PFGM00": "Find X8 Pro",
    "PKC110": "Find X7",
    "PHZ110": "Find X6 Pro",
    "PGFM10": "Find X5 Pro",
    "PEGM10": "Reno10 Pro+",
    # vivo
    "V2364A": "X100 Pro",
    "V2324A": "X100",
    "V2242A": "X90 Pro+",
    # 小米
    "2210132C": "Xiaomi 12",
    "2201122C": "Xiaomi 11 Ultra",
    "M2012K11C": "Redmi K40",
    "24069RA21C": "Redmi Note 13 Pro",
}


def _mhz_from_khz(raw):
    """kHz 数值 → MHz 字符串（如 '1804.8'）；解析失败返回 None。"""
    try:
        v = int(str(raw).strip())
        if v <= 0:
            return None
        return f"{v / 1000:.1f}"
    except (ValueError, TypeError):
        return None


def _market_name(model, marketname):
    """市场名：优先厂商写好的 marketname，其次内置映射表，否则 None（前端显示"未知"）。"""
    if marketname:
        return marketname
    return MODEL_MARKET_MAP.get(model)


def parse_wm_size(out):
    """解析 `wm size` 输出 'Physical size: 1080x2388' → '1080×2388'。"""
    m = re.search(r"Physical size:\s*(\d+)\s*x\s*(\d+)", out or "")
    if m:
        return f"{m.group(1)}×{m.group(2)}"
    return None


def _parse_hardware(raw):
    """'Hardware\t: SM8350' → 'SM8350'（/proc/cpuinfo Hardware 行）。"""
    if not raw:
        return None
    m = re.search(r"Hardware\s*[:：]\s*(\S+)", raw)
    return m.group(1) if m else raw.strip().split()[-1] if raw.strip() else None


def parse_device_info(out):
    """解析 probe_device_info 的合并 shell 输出（标记分隔），返回 device dict。

    各段顺序：model / marketname / name / board / hardware / wm_size / cpu_freq。
    字段缺失/解析失败置 None。
    """
    seg = (out or "").split("__PDDEV__")
    seg = seg if len(seg) >= 7 else seg + [""] * (7 - len(seg))

    def _s(i):
        return seg[i].strip() or None

    model = _s(0)
    marketname = _s(1)
    name = _s(2)
    board = _s(3)
    hardware = _parse_hardware(_s(4))
    resolution = parse_wm_size(_s(5))
    cpu_freq_mhz = _mhz_from_khz(_s(6))

    return {
        "model": model,
        "model_code": name or model,        # ro.product.name 作型号代码兜底
        "market_name": _market_name(model, marketname),
        "board_platform": board,            # 平台代号（lahaina 等）
        "cpu_hardware": hardware,           # SM8350 等
        "cpu_max_freq_mhz": cpu_freq_mhz,
        "screen_resolution": resolution,
    }


def probe_device_info(adb):
    """一次 shell 往返拿全设备信息；任何失败静默返回全 None dict。

    合并命令（`adb shell sh -c '...'`）一次往返取：getprop 四项 + wm size +
    cpuinfo_max_freq，避免多次 adb 往返拖慢启动。
    """
    cmd = (
        "getprop ro.product.model; echo __PDDEV__; "
        "getprop ro.product.marketname; echo __PDDEV__; "
        "getprop ro.product.name; echo __PDDEV__; "
        "getprop ro.board.platform; echo __PDDEV__; "
        "cat /proc/cpuinfo | grep -i hardware | head -1; echo __PDDEV__; "
        "wm size; echo __PDDEV__; "
        "cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq"
    )
    try:
        out = adb.shell(["sh", "-c", cmd])
        return parse_device_info(out)
    except Exception:
        return {
            "model": None, "model_code": None, "market_name": None,
            "board_platform": None, "cpu_hardware": None,
            "cpu_max_freq_mhz": None, "screen_resolution": None,
        }
