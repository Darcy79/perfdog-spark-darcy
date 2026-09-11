# 真机适配矩阵与已知行为差异（devices.md）

> 版本：V1.0（2026-09-11）· 状态：**现行文档**
> 用途：换新手机/新机型时先查本文；采集异常时按「换机验收步骤」定位。
> 原则：本文只记录**真机实测确认**的行为，未验证的一律标注"待验证"，不臆测。

---

## 一、适配矩阵总览

| 机型 | 序列号 | 平台 | 屏幕 | 采集链状态 | 主要适配点 |
|---|---|---|---|---|---|
| 荣耀 ADT-AN00（Magic3 Pro） | A3GD6R2A09011153 | SM8350 / lahaina（骁龙 888），1804.8MHz | 1080×2388，60Hz 屏（支持 60/90/120/144Hz 档） | ✅ 全指标可用 | SF 层名带 `#id`；smaps_rollup 被 SELinux 拒 → dumpsys 回退；meminfo 段内空行；logcat 捞不到 JS 日志 |
| OPPO（型号待补全） | ded7a388 | 待补全 | 面板跑 120Hz 档 | ✅ 全指标可用（FPS 主段法修复后） | 输入层需 skip；窗口层偶发 total_frames=1；**SF 缓冲稀疏**（0.5s 切段取主段） |
| 红米（型号待补全） | — | — | — | ⚠️ 基础采集链跑通（2026-08-17 早期验证） | 未做完整回归，细节待补 |

> 其他品牌（华为/vivo/小米/三星等）：**未覆盖/待验证**，接入时按「换机验收步骤」执行并回填本文。

---

## 二、荣耀 ADT-AN00（Magic3 Pro）— 主力验证机

**设备信息**（`device_info.py` 实测）：model=ADT-AN00，market_name=Magic3 Pro，board=lahaina，cpuinfo Hardware=SM8350，cpu0 最大频率 1804.8MHz，分辨率 1080×2388。

| 项 | 实测行为 | 工具处理 |
|---|---|---|
| SurfaceFlinger 层名 | `SurfaceView[com.tencent.mm/...](BLAST)#4964` —— **`#id` 必须保留**，去掉读不到帧（本机实测，多数工具默认去 #id 的做法在此机失效） | 层名原样使用；#id 随层重建变化，读帧失败自动重匹配 |
| 内存采集 | `/proc/<pid>/smaps_rollup` 被 **SELinux 拒绝** | 自动回退 `dumpsys meminfo`（App Summary 段 TOTAL PSS/RSS 同源解析） |
| meminfo 格式 | App Summary 段内 `Unknown:` 与 `TOTAL PSS:` 之间**存在真实空行**（曾导致按"段内空行截断"时 PSS 全空） | v36 起改为**全输出搜索** TOTAL PSS/RSS，该真机格式已固化为 golden test |
| 面板刷新率 | 支持 60/90/120/144Hz 档，且采集中会动态切换（单次采集 60↔120 翻转可达数十次）；`--latency` 首行跟随当前 vsync 档 | Jank 阈值**不跟面板 vsync**，按帧间隔中位数节奏校准（见 指标说明.md「一、2」），避免"面板 120Hz + 游戏锁 60fps"假 Jank |
| CPU 核数 | `nproc` 报 **6**（shell 被 cpuset 限制），`/sys/devices/system/cpu/online` 为 `0-7`（物理 8 核） | 核数探测以 cpu/online 为准，nproc 仅作回退 |
| 电池节点 | `/sys/class/power_supply/battery/*` 需 root（读不到电流） | 锁定降级 `dumpsys battery`（温度/电压可得，电流/功率为 null） |
| logcat 事件层 | **捞不到小游戏 JS console.log**（三轮验证均失败） | logcat 事件层默认不启用、静默旁路；`*.events.jsonl` 不产生属预期 |
| 典型读数 | 微信小游戏采集中 FPS 中位约 56.9~57.8（60Hz 档） | — |

---

## 三、OPPO（ded7a388）— 第二验证机

| 项 | 实测行为 | 工具处理 |
|---|---|---|
| SurfaceFlinger 层名 | 与荣耀同格式（`SurfaceView[...](BLAST)#id`） | 同一匹配逻辑 |
| 多余输入层 | `--list` 输出多出 `hexid ActivityRecordInputSink ...` 输入事件层（hex id 前缀），**无帧统计**，误选会导致 FPS 永远为 0 | 层匹配显式 skip `ActivityRecordInputSink` |
| 窗口层计数异常 | 窗口层偶发 `total_frames=1`（无统计意义），旧逻辑因此不换层、死等 | 计数 ≤1 时**主动重匹配**（SurfaceView 出现即换层），仍无帧再试 gfxinfo |
| **SF 缓冲稀疏** | 128 帧时间戳**稀疏分布在约 35 分钟**里（相邻帧间隔中位仍 16.7ms），旧"全缓冲首尾跨度"算法算出 127/2108s ≈ **0.01 FPS 病态值** | 2026-09-01 起：相邻帧间隔 >0.5s 切段，取**帧数最多的主段**算 FPS；连续缓冲（如荣耀）全缓冲即单段，行为不变。修复后 FPS 中位约 57.6 |
| 面板 | 跑 120Hz 档 | 同荣耀：Jank 阈值按节奏校准，不跟 vsync |

---

## 四、红米（2026-08-17 早期验证）

基础采集链（FPS/CPU/内存）曾跑通，**未做完整指标回归与机型差异排查**。再次使用时按「换机验收步骤」重新验收并回填本文。

---

## 五、换机验收步骤

新机型接入时按顺序执行，**全程约 2 分钟**：

1. **识别前台**：手机打开被测应用保持前台，执行
   `python main.py --show-foreground`
   → 输出 `mCurrentFocus=Window{... 包名/Activity}`，确认包名与进程模式（原生 App 用 `--process-pattern ""`）。
2. **30 秒试采**：`python main.py --duration 30 --web`
   → **FPS 有值（30~满帧区间）即适配成功**；同时确认 CPU/PSS/温度有数、进程 pid 稳定。
3. **FPS 病态/为 0 时按此顺序排查**：
   1. `adb shell dumpsys SurfaceFlinger --list` → 找该机型**特殊层**（是否有类似 OPPO `ActivityRecordInputSink` 的输入层/装饰层混入窗口层兜底，需加入 skip 列表）；
   2. `adb shell dumpsys SurfaceFlinger --latency <层名>` 看**原始时间戳跨度**——128 帧是否稀疏分布在几十分钟里（是 → 依赖主段法，确认版本 ≥2026-09-01）；
   3. 确认**面板刷新率档位**（`dumpsys display | grep -i refresh`，60/90/120/144）——Jank 判定已按节奏校准，档位本身不影响适配，但解读报告时要知道游戏锁帧上限。

验收通过后：把机型的设备信息（getprop/分辨率）、层名格式、特异行为**回填到本文对应章节**。

---

## 六、待验证场景（未覆盖，勿当已支持）

| 场景 | 状态 | 说明 |
|---|---|---|
| 微信多开 / 分屏多开 | ⬜ 未验证 | 多开会产生更多 appbrand 进程与渲染层，活跃进程选择逻辑（累计 CPU 最大）未在该场景回归 |
| 高刷新率切档中采集（60↔120↔144） | ⬜ 未验证 | 荣耀实测切档会发生且 Jank 节奏校准可应对，但切档瞬间的帧统计连续性未专门验证 |
| 30min+ 长时采集 | ⬜ 未验证 | 现有真机数据最长约 21 分钟；长测下的内存增长、温度封顶、jsonl 体积与看板性能未回归 |
| 更多品牌机型（华为/vivo/小米/三星等） | ⬜ 未覆盖 | 接入后按「换机验收步骤」执行并回填 |

---

## 七、阈值过拟合提醒

`data_health.py` 的 5 条自检规则阈值是在 **4 份真实数据**（荣耀/微信小游戏场景）上调校的：
正常样本零误报、已知异常样本准确命中。**每接入一个新机型/新被测应用，应跑一次回归**
（用新机型的正常数据过一遍 `scan_rows`，确认不误报；构造/复用异常样本确认能检出），
避免阈值过拟合导致新机型上误报刷屏或异常漏报。调校记录见 `tests/test_data_health.py`。

---

*本文档只记录真机实测行为；新增机型/场景验证后请同步更新（本地 + GitHub 双向同步）。*
