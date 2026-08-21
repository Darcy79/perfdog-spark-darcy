# -*- coding: utf-8 -*-
"""ADB 桥：设备发现、命令执行、环境自检。

仅使用 Python 标准库。
adb 定位策略：依次探测候选路径，用 `adb version` 验证可用性——
  1) C:/platform-tools/adb.exe（常见解压位置）
  2) 环境变量 ANDROID_HOME 下的 platform-tools/adb.exe
  3) 当前用户目录下的 platform-tools/adb.exe
  4) PATH 中的 adb
这样即使 PATH 里的 adb 损坏（如误复制进 System32 导致 DLL 加载失败），
也能自动切换到可用的那份。
"""

import os
import shutil
import subprocess


class AdbError(Exception):
    """ADB 相关错误，带用户可读的中文信息。"""


def _find_adb():
    """返回第一个可用的 adb 绝对路径；全失败返回 None。"""
    candidates = [
        r"C:\platform-tools\adb.exe",
        (os.environ.get("ANDROID_HOME", "") or "") + r"\platform-tools\adb.exe",
        os.path.expanduser(r"~\platform-tools\adb.exe"),
        shutil.which("adb") or "",
    ]
    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if not os.path.isfile(path):
            continue
        # 用 `adb version` 验证可执行性，过滤掉"文件在但 DLL 缺失"的坏安装
        try:
            r = subprocess.run([path, "version"], capture_output=True, timeout=10)
            if r.returncode == 0:
                return path
        except Exception:
            continue
    return None


class Adb:
    """封装 adb 命令。构造时自动探测设备并锁定 serial。"""

    def __init__(self, serial=""):
        self.adb = _find_adb()
        if not self.adb:
            raise AdbError(
                "未找到可用的 adb。\n"
                "请下载 Android SDK Platform-Tools 并解压到 C:\\platform-tools：\n"
                "https://dl.google.com/android/repository/platform-tools-latest-windows.zip\n"
                "（或把解压出的 adb.exe 所在目录加入 PATH）"
            )
        self.serial = ""
        self._base = [self.adb]
        if serial:
            self.serial = serial
            self._base = [self.adb, "-s", serial]
        # 自检：确认有设备在线
        self._probe()

    def _probe(self):
        out = self._run(["devices"])
        lines = [l for l in out.splitlines() if l.strip() and not l.strip().startswith("List")]
        if not lines:
            raise AdbError("未检测到已连接的设备，请确认手机已开启 USB 调试并连接。")
        if self.serial:
            ok = any(
                l.split()[0] == self.serial and l.split()[1] == "device"
                for l in lines
                if len(l.split()) >= 2
            )
            if not ok:
                raise AdbError(f"设备 {self.serial} 不在线（当前连接: {[l.split()[0] for l in lines]}）")
            return
        for l in lines:
            parts = l.split()
            if len(parts) >= 2 and parts[1] == "device":
                self.serial = parts[0]
                self._base = [self.adb, "-s", self.serial]
                return
        raise AdbError("未找到处于 device 状态的设备（有 device 前缀设备但未授权，请在手机上允许 USB 调试）。")

    def is_device_alive(self):
        """探活：设备是否仍在线（连续采样失败时的断连诊断用，2026-08-21）。"""
        try:
            out = self._run(["devices"])
            for l in out.splitlines():
                parts = l.split()
                if len(parts) >= 2 and parts[0] == self.serial and parts[1] == "device":
                    return True
        except Exception:
            pass
        return False

    def shell(self, args):
        """执行 adb shell 命令，返回 stdout 文本。

        参数统一用单引号包裹再发给设备端 shell——
        layer 名含 [ ] ( ) 等 shell 特殊字符（如 SurfaceView[com.tencent.mm/...]），
        不包裹会被 glob/子shell 展开导致命令失败。
        """
        quoted = ["'" + a.replace("'", "'\\''") + "'" for a in args]
        return self._run(["shell"] + quoted)

    def exec_out(self, args, timeout=30):
        """执行 adb exec-out，返回原始 stdout（bytes，保留二进制）。

        用于读取 APK 内部二进制文件（unzip -p 提取的 AXML / resources.arsc）——
        shell() 的 text 模式会破坏二进制数据，必须走 exec-out。
        """
        quoted = ["'" + a.replace("'", "'\\''") + "'" for a in args]
        proc = subprocess.run(
            self._base + ["exec-out"] + quoted,
            capture_output=True,
            timeout=timeout,
        )
        return proc.stdout

    def _run(self, args, timeout=20):
        proc = subprocess.run(
            self._base + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise AdbError(f"adb 命令失败: {' '.join(self._base + args)}\n{proc.stderr.strip()[:500]}")
        return proc.stdout
