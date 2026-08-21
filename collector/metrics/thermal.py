# -*- coding: utf-8 -*-
"""功耗温度采集（V0.2 新增）。

双通道读取（覆盖不同机型权限差异）：
  1) 优先 /sys/class/power_supply/battery/*（部分机型可读，含电流 current_now）
  2) 失败降级 `dumpsys battery`（免 root 标准接口，含温度/电压，无电流）

单位统一：
  temp   -> °C（sys 为 0.1°C/单位；dumpsys 为 0.1°C）
  current-> mA（sys 为 uA）
  voltage-> V （sys 为 uV；dumpsys 为 mV）
  power  -> W （= 电压×电流，电流缺失时为 None）

容错：任一指标读不到返回 None，不中断采集。
"""

BATTERY = "/sys/class/power_supply/battery"


class ThermalCollector:
    def __init__(self, adb, base=BATTERY):
        self.adb = adb
        self.base = base
        # 降级模式缓存：sys 节点若确认不可读（如荣耀需 root），直接走 dumpsys，
        # 避免每轮都做 3 次无效 cat 往返（性能优化 2026-08-12）
        self.sys_unavailable = None   # None=未探测, True=已确认不可读

    def _probe_sys(self):
        """探测 sys 节点是否可读；连续两个节点失败则判为不可读，锁定降级。"""
        if self.sys_unavailable is not None:
            return not self.sys_unavailable
        ok = 0
        for name in ("temp", "current_now"):
            if self._read_sys(name) is not None:
                ok += 1
        # 只要有一个节点可读就保留 sys 通道；全部失败则降级
        self.sys_unavailable = ok == 0
        return not self.sys_unavailable

    def _read_sys(self, name):
        """读 sys 节点原始值（float）。失败返回 None。"""
        try:
            out = self.adb.shell(["cat", f"{self.base}/{name}"])
            return float(out.strip())
        except Exception:
            return None

    def _read_dumpsys(self):
        """降级源：dumpsys battery 的温度(0.1°C)与电压(mV)。返回 (temp, volt_mv) 或 None。"""
        try:
            out = self.adb.shell(["dumpsys", "battery"])
        except Exception:
            return None
        temp = volt = None
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("temperature:"):
                try:
                    temp = float(s.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif s.startswith("voltage:"):
                try:
                    volt = float(s.split(":", 1)[1].strip())
                except ValueError:
                    pass
        if temp is None and volt is None:
            return None
        return temp, volt

    def sample(self, ts):
        result = {"temp_c": None, "current_ma": None, "voltage_v": None, "power_w": None}

        temp = cur = vol = None
        if self._probe_sys():
            temp = self._read_sys("temp")          # 0.1°C
            cur = self._read_sys("current_now")    # uA
            vol = self._read_sys("voltage_now")    # uV

        # 降级：sys 读不到温度/电压时用 dumpsys battery（含已锁定降级模式）
        if temp is None or vol is None:
            d = self._read_dumpsys()
            if d:
                dt, dv = d
                if temp is None and dt is not None:
                    temp = dt               # dumpsys 与 sys 同为 0.1°C 口径，直接沿用
                if vol is None and dv is not None:
                    vol = dv * 1000         # mV -> uV

        if temp is not None:
            result["temp_c"] = round(temp / 10, 1)        # -> °C
        if cur is not None:
            result["current_ma"] = round(abs(cur) / 1000, 1)
        if vol is not None:
            result["voltage_v"] = round(vol / 1e6, 2)
        if cur is not None and vol is not None:
            result["power_w"] = round(abs(cur) * vol / 1e12, 3)
        return result
