# -*- coding: utf-8 -*-
"""APK 应用名（label）提取 —— 无 aapt 时的纯 Python 方案。

荣耀等 ROM 不带 aapt，`dumpsys package` 也没有 label 字段。改用设备自带 unzip
提取 APK 内的两个二进制文件，在本机解析出应用名：

  1) AndroidManifest.xml（AXML，Android binary XML）→ 解析出 <application
     android:label>：label 是直接字符串则立刻可用；是资源引用(@7fxxxxxx)
     则拿到资源 ID。
  2) resources.arsc → 按资源 ID 定位 string 类型的 entry，取全局字符串池里的值。

2026-08-21 荣耀 ADT-AN00（Android 14）真机验证。仅标准库，无外部依赖。
"""

import struct
import subprocess

# ---------------- 基础二进制工具 ----------------


def _chunks(data, start=0, end=None):
    """递归遍历 chunk：yield (type, headerSize, size, off)。

    AXML 根（0x0003）/ ARSC 根（0x0002）/ package（0x0200）的 size 是整块
    总大小（AXML 根 = 整个文件），会包住内部子 chunk（字符串池、XML 元素、
    type 块等）。因此对容器 chunk 递归进入其内部，否则会跳过全部内容。
    """
    if end is None:
        end = len(data)
    off = start
    while off + 8 <= end:
        ctype, hsize = struct.unpack_from("<HH", data, off)
        size = struct.unpack_from("<I", data, off + 4)[0]
        if size < 8 or off + size > end:
            break
        yield ctype, hsize, size, off
        if ctype in (0x0003, 0x0002, 0x0200):
            for sub in _chunks(data, off + hsize, off + size):
                yield sub
        off += size


def _read_uleb(data, off):
    """Android UTF-8 字符串池的 uleb128 长度编码。"""
    result = 0
    shift = 0
    while True:
        b = data[off]
        off += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, off


def _parse_string_pool(data, off):
    """解析 ResStringPool（chunk 0x0001），返回字符串列表。"""
    count, _sc, flags, s_start, _ss = struct.unpack_from("<IIIII", data, off + 8)
    if count == 0:
        return []
    # AOSP ResStringPool：UTF8_FLAG = 0x00000100（实测：ARSC 全局池 flags=0x100 是
    # UTF-8 数据，AXML 池 flags=0x0 是 UTF-16 数据）。0x1/0x2 不是编码标志。
    utf8 = bool(flags & 0x100)
    offsets = struct.unpack_from("<%dI" % count, data, off + 28)
    base = off + s_start
    out = []
    for o in offsets:
        p = base + o
        if p >= len(data):
            out.append("")
            continue
        if utf8:
            try:
                _, p = _read_uleb(data, p)      # utf16 长度（跳过）
                blen, p = _read_uleb(data, p)   # 字节长度
                raw = data[p:p + blen]
                out.append(raw.decode("utf-8", errors="replace"))
            except Exception:
                out.append("")
        else:
            try:
                clen = struct.unpack_from("<H", data, p)[0]   # 字符数（含结尾 null）
                raw = data[p + 2:p + 2 + clen * 2]
                out.append(raw.decode("utf-16le", errors="replace").rstrip("\x00"))
            except Exception:
                out.append("")
    return out


# ---------------- AXML 解析（AndroidManifest.xml） ----------------

def parse_axml_label(data):
    """解析 Android binary XML，返回 (label_text, label_res)。

    label 可能是直接字符串（AXML 字符串池索引），也可能是资源引用（@7fxxxxxx）。
    优先取 <application> 节点的 android:label，找不到再扫任意节点。
    """
    strings = []
    for ctype, _hs, _sz, off in _chunks(data):
        if ctype == 0x0001:
            strings = _parse_string_pool(data, off)
            break

    ANDROID_NS = "http://schemas.android.com/apk/res/android"

    def get_str(idx):
        return strings[idx] if 0 <= idx < len(strings) else None

    def node_label(off, hsize):
        """返回该 start element 的 android:label (str) 或 (res)，没有则 None。"""
        node_ext = off + hsize
        ns, name = struct.unpack_from("<II", data, node_ext)
        a_start, a_size, a_count = struct.unpack_from("<HHI", data, node_ext + 8)
        attrs = node_ext + a_start
        for i in range(a_count):
            ao = attrs + i * a_size
            ans, aname, _raw, _vs, _r0, v_type, v_data = struct.unpack_from("<IIIBHBI", data, ao)
            if get_str(aname) != "label":
                continue
            if get_str(ans) != ANDROID_NS:
                continue
            if v_type == 0x03:        # TYPE_STRING → 直接文本
                return get_str(v_data), None
            if v_type == 0x01:        # TYPE_REFERENCE → 资源 ID
                return None, v_data
            return None, None
        return None, None

    # 第一遍：<application> 节点
    for ctype, hsize, _sz, off in _chunks(data):
        if ctype != 0x0102:      # start element
            continue
        try:
            node_ext = off + hsize
            name = struct.unpack_from("<I", data, node_ext + 4)[0]
            if get_str(name) == "application":
                lbl, rid = node_label(off, hsize)
                if lbl is not None or rid is not None:
                    return lbl, rid
        except Exception:
            continue
    # 第二遍：任意带 label 的节点（部分应用仅 activity 声明 label）
    for ctype, hsize, _sz, off in _chunks(data):
        if ctype != 0x0102:
            continue
        try:
            lbl, rid = node_label(off, hsize)
            if lbl is not None or rid is not None:
                return lbl, rid
        except Exception:
            continue
    return None, None


# ---------------- ARSC 解析（resources.arsc） ----------------

def parse_arsc_label(data, res_id):
    """按资源 ID 在 resources.arsc 中取字符串值。找不到返回 None。"""
    pkg_id = (res_id >> 24) & 0xFF
    type_id = (res_id >> 16) & 0xFF
    entry_idx = res_id & 0xFFFF

    global_strings = None
    type_info = None   # (type_off, entry_count, entries_start, thsize)
    pkg_off = None

    for ctype, hsize, _sz, off in _chunks(data):
        if ctype == 0x0001:
            if global_strings is None:
                global_strings = _parse_string_pool(data, off)
        elif ctype == 0x0200:      # ResTable_package
            pid = struct.unpack_from("<I", data, off + 8)[0]
            if pid == pkg_id:
                pkg_off = off
        elif ctype == 0x0201 and pkg_off is not None:   # ResTable_type
            tid = data[off + 8]
            if tid == type_id:
                entry_count = struct.unpack_from("<I", data, off + 12)[0]
                entries_start = struct.unpack_from("<I", data, off + 16)[0]
                type_info = (off, entry_count, entries_start, hsize)
                break

    if not global_strings or not type_info:
        return None
    type_off, entry_count, entries_start, thsize = type_info
    if entry_idx >= entry_count:
        return None
    # entry offsets 数组在 type 块 headerSize 处（含 config）
    eo = struct.unpack_from("<I", data, type_off + thsize + entry_idx * 4)[0]
    if eo == 0:
        return None
    entry_off = type_off + entries_start + eo
    esize, eflags = struct.unpack_from("<HH", data, entry_off)
    if eflags & 0x0001:      # FLAG_COMPLEX → 非简单值，跳过
        return None
    # Res_value 紧随 entry 之后
    val_off = entry_off + esize
    v_size, v_res0, v_type, v_data = struct.unpack_from("<HBBI", data, val_off)
    if v_type == 0x03 and 0 <= v_data < len(global_strings):   # TYPE_STRING
        return global_strings[v_data]
    return None


# ---------------- 对外入口 ----------------

def _extract(adb, apk_path, entry, timeout=20):
    """用设备自带 unzip 提取 APK 内条目（exec-out 裸参数，保留二进制）。

    ⚠️ 用 exec-out 直传 unzip 参数（无管道、无引号）：Windows adb 客户端对
    参数内部嵌套单引号（unzip -p '/path'）或管道（| base64）处理不可靠，
    实测会把整条命令当单个程序名执行而失败。设备 apk 路径
    （/data/app/~~xx==/pkg-xx==/base.apk）不含空格/glob 字符，裸传安全。
    timeout 超时（大 APK）抛异常，由上层捕获返回 None，不阻塞整体解析。
    """
    proc = subprocess.run(
        adb._base + ["exec-out", "unzip", "-p", apk_path, entry],
        capture_output=True,
        timeout=timeout,
    )
    return proc.stdout


def _apk_label_once(adb, apk_path):
    """对单个 apk 文件解析应用名。成功返回字符串，失败返回 None。"""
    try:
        # 大 APK（微信/QQ/浏览器等）unzip 提取慢，timeout 到点即跳过（显示包名），
        # 避免单个大包拖死整体解析；中小包（游戏/工具）秒级完成。
        axml = _extract(adb, apk_path, "AndroidManifest.xml", timeout=8)
        if not axml or len(axml) < 8:
            return None
        label, res_id = parse_axml_label(axml)
        if label:
            return label.strip() or None
        if res_id:
            arsc = _extract(adb, apk_path, "resources.arsc", timeout=12)
            if arsc and len(arsc) > 8:
                v = parse_arsc_label(arsc, res_id)
                if v:
                    return v.strip() or None
    except Exception:
        return None
    return None


def get_apk_label(adb, pkg, apk_path):
    """提取应用名（仅解析 base.apk）。

    多 APK 应用的 label 资源可能在 split apk 里，但 split 同样是大文件、
    解析成本高，且实测价值有限（qmqswd 无 split 也解析不出），
    为控制整体解析耗时，本版不做 split 补查。
    """
    return _apk_label_once(adb, apk_path)
