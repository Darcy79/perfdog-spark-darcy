# -*- coding: utf-8 -*-
"""APK 应用名（label）提取 —— 无 aapt 时的纯 Python 方案。

荣耀等 ROM 不带 aapt，`dumpsys package` 也没有 label 字段。改用设备自带 unzip
提取 APK 内的两个二进制文件，在本机解析出应用名：

  1) AndroidManifest.xml（AXML，Android binary XML）→ 解析出 <application
     android:label>：label 是直接字符串则立刻可用；是资源引用(@7fxxxxxx)
     则拿到资源 ID。
  2) resources.arsc → 按资源 ID 定位 string 类型的 entry，取全局字符串池里的值。

2026-08-21 荣耀 ADT-AN00（Android 14）真机验证。仅标准库，无外部依赖。

内存安全（2026-08-21 加固）：解析的是不可信的二进制内容（异常/恶意/损坏 APK），
所有长度、数量、偏移字段都按"数据区实际容纳能力"做上限校验，防止超大值一次性
分配海量内存（曾导致 Python 进程吃光电脑内存）：
  - _chunks：递归改显式栈迭代，带深度上限，异常 chunk 只跳过不中断
  - _parse_string_pool：count 超过数据区可容纳的偏移量上限直接返回空，
    不做巨型 unpack
  - _extract：AndroidManifest.xml > 10MB / resources.arsc > 100MB 直接放弃，
    超大文件不进内存
"""

import struct
import subprocess
import threading

# ---------------- 内存安全上限 ----------------

# 字符串池偏移表每个 4 字节 → 数据区能容纳的最大字符串数 = 数据长度/4。
# 合法 APK 的 manifest/arsc 远达不到这个量级；超限说明 count 字段异常。
# 单条字符串截断上限：真实应用名最长几十字符（UTF-8 UTF-16 都远小于此），
# 超长只可能来自损坏数据，截断防止切片/解码放大内存。
_MAX_STRING_BYTES = 1 << 20    # 1MB
# _chunks 容器嵌套深度上限：AXML/ARSC 真实层级 ≤ 2~3 层，超限视为异常文件
_MAX_CHUNK_DEPTH = 8
# _extract 提取大小上限：正常 manifest 几百 KB，arsc 几十 MB；超限放弃该包
_MAX_AXML_SIZE = 10 * 1024 * 1024      # 10MB
_MAX_ARSC_SIZE = 100 * 1024 * 1024     # 100MB


# ---------------- 基础二进制工具 ----------------


def _chunks(data, start=0, end=None):
    """前序遍历 chunk：yield (type, headerSize, size, off)。

    AXML 根（0x0003）/ ARSC 根（0x0002）/ package（0x0200）的 size 是整块
    总大小（AXML 根 = 整个文件），会包住内部子 chunk（字符串池、XML 元素、
    type 块等）。因此对容器 chunk 下钻进入其内部，否则会跳过全部内容。

    原递归实现遇到异常文件（深层嵌套/自引用 chunk）会无限递归、栈和内存
    爆炸。改为显式栈迭代 + 深度上限；遍历顺序与原递归一致（前序、从左到
    右）——ARSC 解析依赖"全局字符串池在 package 块之前"这一顺序。
    异常 chunk（headerSize 非法/越界）只终止当前层、不中断整体遍历。
    """
    if end is None:
        end = len(data)

    def _level(s, e):
        """生成 [s, e) 内的同级 chunk，只读头不递归。"""
        off = s
        while off + 8 <= e:
            try:
                ctype, hsize = struct.unpack_from("<HH", data, off)
                size = struct.unpack_from("<I", data, off + 4)[0]
            except Exception:
                return
            # size/hsize 非法 → 当前层数据损坏，停止本层遍历
            if size < 8 or off + size > e or hsize < 8 or hsize > size:
                return
            yield ctype, hsize, size, off
            off += size

    # 栈元素 (迭代器, 深度)；容器入栈即"前序下钻"，与原递归顺序一致
    stack = [(_level(start, end), 0)]
    while stack:
        it, depth = stack[-1]
        try:
            ctype, hsize, size, off = next(it)
        except StopIteration:
            stack.pop()
            continue
        yield ctype, hsize, size, off
        if ctype in (0x0003, 0x0002, 0x0200) and depth < _MAX_CHUNK_DEPTH:
            stack.append((_level(off + hsize, off + size), depth + 1))


def _read_uleb(data, off):
    """Android UTF-8 字符串池的 uleb128 长度编码。带边界与异常值防护。"""
    result = 0
    shift = 0
    for _ in range(6):   # 32 位长度最多 5 个 7-bit 组，第 6 个兜底
        if off >= len(data):
            raise ValueError("uleb 越界")
        b = data[off]
        off += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    else:
        raise ValueError("uleb 长度异常")
    return result, off


def _parse_string_pool(data, off):
    """解析 ResStringPool（chunk 0x0001），返回字符串列表。

    count 等字段直接来自文件内容、不可信：先用数据区实际容纳能力校验，
    超限返回空列表，绝不做 struct.unpack_from("<%dI" % count) 的巨型分配。
    """
    try:
        chunk_size = struct.unpack_from("<I", data, off + 4)[0]
        count, _sc, flags, s_start, _ss = struct.unpack_from("<IIIII", data, off + 8)
    except Exception:
        return []
    if count == 0:
        return []
    # 合理性校验：偏移表（count*4 字节）+ 字符串数据区必须落在 chunk 内。
    # 这是硬上限——合法文件远达不到，超限即异常/恶意 count，直接放弃。
    chunk_end = min(off + chunk_size, len(data))
    if chunk_size < 28 or s_start < 28 or off + s_start > chunk_end:
        return []
    data_len = chunk_end - (off + s_start)
    if count > data_len // 4:
        return []
    # AOSP ResStringPool：UTF8_FLAG = 0x00000100（实测：ARSC 全局池 flags=0x100 是
    # UTF-8 数据，AXML 池 flags=0x0 是 UTF-16 数据）。0x1/0x2 不是编码标志。
    utf8 = bool(flags & 0x100)
    try:
        offsets = struct.unpack_from("<%dI" % count, data, off + 28)
    except Exception:
        return []
    base = off + s_start
    out = []
    for o in offsets:
        p = base + o
        if p < 0 or p >= len(data):
            out.append("")
            continue
        if utf8:
            try:
                _, p = _read_uleb(data, p)      # utf16 长度（跳过）
                blen, p = _read_uleb(data, p)   # 字节长度
                if blen > _MAX_STRING_BYTES:    # 长度异常，防切片放大内存
                    out.append("")
                    continue
                raw = data[p:p + blen]
                out.append(raw.decode("utf-8", errors="replace"))
            except Exception:
                out.append("")
        else:
            try:
                clen = struct.unpack_from("<H", data, p)[0]   # 字符数（含结尾 null）
                if clen * 2 > _MAX_STRING_BYTES:
                    out.append("")
                    continue
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
    不可信数据全程 try/except：任何字段越界/损坏都当作"该节点无 label"跳过。
    """
    if len(data) > _MAX_AXML_SIZE:   # 双保险：入口再校验一次
        return None, None

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
        # a_count 来自文件：按剩余数据区可容纳上限校验，防异常值放大循环
        if a_size == 0 or a_count > (len(data) - node_ext - a_start) // a_size:
            return None, None
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
    """按资源 ID 在 resources.arsc 中取字符串值。找不到/数据异常返回 None。"""
    if len(data) > _MAX_ARSC_SIZE:   # 双保险：入口再校验一次
        return None
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
            try:
                pid = struct.unpack_from("<I", data, off + 8)[0]
            except Exception:
                continue
            if pid == pkg_id:
                pkg_off = off
        elif ctype == 0x0201 and pkg_off is not None:   # ResTable_type
            try:
                tid = data[off + 8]
                if tid == type_id:
                    entry_count = struct.unpack_from("<I", data, off + 12)[0]
                    entries_start = struct.unpack_from("<I", data, off + 16)[0]
                    type_info = (off, entry_count, entries_start, hsize)
                    break
            except Exception:
                continue

    if not global_strings or not type_info:
        return None
    type_off, entry_count, entries_start, thsize = type_info
    if entry_idx >= entry_count:
        return None
    # entry offsets 数组在 type 块 headerSize 处（含 config）
    # entry_idx*4 越界校验：偏移表不可能超出数据区可容纳范围
    if entry_idx * 4 + 4 > len(data) - (type_off + thsize):
        return None
    try:
        eo = struct.unpack_from("<I", data, type_off + thsize + entry_idx * 4)[0]
    except Exception:
        return None
    # NO_ENTRY 哨兵是 0xFFFFFFFF；偏移 0 是合法值（第一个 entry）
    if eo == 0xFFFFFFFF:
        return None
    entry_off = type_off + entries_start + eo
    if entry_off < 0 or entry_off + 4 > len(data):
        return None
    try:
        esize, eflags = struct.unpack_from("<HH", data, entry_off)
    except Exception:
        return None
    if eflags & 0x0001:      # FLAG_COMPLEX → 非简单值，跳过
        return None
    # Res_value 紧随 entry 之后
    val_off = entry_off + esize
    if val_off + 8 > len(data):
        return None
    try:
        v_size, v_res0, v_type, v_data = struct.unpack_from("<HBBI", data, val_off)
    except Exception:
        return None
    if v_type == 0x03 and 0 <= v_data < len(global_strings):   # TYPE_STRING
        return global_strings[v_data]
    return None


# ---------------- 对外入口 ----------------

def _extract(adb, apk_path, entry, timeout=20, max_size=10 * 1024 * 1024):
    """用设备自带 unzip 提取 APK 内条目（exec-out 裸参数，保留二进制）。

    ⚠️ 用 exec-out 直传 unzip 参数（无管道、无引号）：Windows adb 客户端对
    参数内部嵌套单引号（unzip -p '/path'）或管道（| base64）处理不可靠，
    实测会把整条命令当单个程序名执行而失败。设备 apk 路径
    （/data/app/~~xx==/pkg-xx==/base.apk）不含空格/glob 字符，裸传安全。

    流式读取 + 大小截断：累计超过 max_size 立即 kill 进程并返回 None，
    异常/zip-bomb 式的超大条目不会整块进内存（capture_output 会先读完整
    输出再判断，防不住）；看门狗线程负责 timeout 到点 kill（大 APK），
    超时/超界均返回 None，由上层当解析失败处理，不阻塞整体。
    """
    proc = subprocess.Popen(
        adb._base + ["exec-out", "unzip", "-p", apk_path, entry],
        stdout=subprocess.PIPE,
    )
    state = {"timeout": False}

    def _watchdog():
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            state["timeout"] = True
            proc.kill()

    threading.Thread(target=_watchdog, daemon=True).start()

    buf = []
    total = 0
    try:
        while True:
            chunk = proc.stdout.read(1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > max_size:
                proc.kill()
                return None
            buf.append(chunk)
    except Exception:
        proc.kill()
        return None
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    if state["timeout"]:
        return None   # 超时得到的是截断数据，不能当有效内容解析
    return b"".join(buf)


def _apk_label_once(adb, apk_path):
    """对单个 apk 文件解析应用名。成功返回字符串，失败返回 None。"""
    try:
        # 大 APK（微信/QQ/浏览器等）unzip 提取慢，timeout 到点即跳过（显示包名），
        # 避免单个大包拖死整体解析；中小包（游戏/工具）秒级完成。
        # manifest 超过 10MB 属异常，直接放弃该文件不进内存。
        axml = _extract(adb, apk_path, "AndroidManifest.xml",
                        timeout=8, max_size=_MAX_AXML_SIZE)
        if not axml or len(axml) < 8:
            return None
        label, res_id = parse_axml_label(axml)
        if label:
            return label.strip() or None
        if res_id:
            arsc = _extract(adb, apk_path, "resources.arsc",
                            timeout=12, max_size=_MAX_ARSC_SIZE)
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
