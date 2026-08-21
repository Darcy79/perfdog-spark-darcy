# 自研 PerfDog 采集器（FPS / CPU / 内存 / 网络 / 温度）

微信小游戏真机性能采集工具。Windows + USB 安卓真机，ADB 采集，数据本地 JSONL 落盘，**不传云端**。

> 📖 **指标详解与使用说明**见同目录 `指标说明.md`（FPS/Jank/帧时间/CPU/PSS 与 PerfDog Memory 区别/网络/温度等逐项说明 + 异常排查 + 测试场景建议）。

## 获取

**方式 A：下载打包版（推荐，免装 Python）**
1. 打开 GitHub Release 页：https://github.com/Darcy79/perfdog-spark-darcy/releases
2. 下载最新版 `perfdog.exe`（GitHub Actions 自动构建的单文件，Windows 直接双击运行）
3. exe 首次运行自动把配置模板复制到 exe 所在目录（可改 `config.json`），数据保存在 exe 旁 `output/` 下

**方式 B：源码运行**
```bash
git clone https://github.com/Darcy79/perfdog-spark-darcy.git
cd perfdog-spark-darcy
# 双击 start_perfdog.bat（自动用 uv 跑，无需装 Python）；或：
cd collector && uv run --no-project python main.py --web
```

> 两种方式行为一致：等价 `cd collector && python main.py --web`。打包版与源码版每次 Release 同步更新。

## 一键启动（不用记命令）

| 文件 | 作用 |
|---|---|
| `start_perfdog.bat` | **双击启动**采集 + Web 看板（自动定位目录、自动开浏览器） |
| `start_dashboard.bat` | **双击查看历史报告**（无需手机，数据在本地 jsonl） |

> 首次运行 bat 会自动用 uv 下载 Python（1 分钟左右），之后即用即开。
> bat 输出为英文（避免中文在 cmd 乱码），功能与命令启动完全一致。

## 输出结构（每次采集一个独立文件夹）

**每次采集自动新建按时间命名的文件夹**，内含数据文件与报告，历史数据互不覆盖：

```
collector/output/
├── 20260812_164653/                  ← 按时间命名的采集文件夹
│   ├── perfdog_20260812_164653.jsonl ← 原始数据（每行一个采样点）
│   └── perfdog_20260812_164653.html  ← 自包含 HTML 报告（双击即看，自动生成）
├── 20260812_170001/
│   ├── perfdog_20260812_170001.jsonl
│   └── perfdog_20260812_170001.html
└── ...                                ← 每次采集都新增一个文件夹，不覆盖旧数据
```

- **HTML 报告**：采集结束**自动生成**，ECharts 已内联，双击即看，可任意拷贝分享
- **看板历史**：实时看板的历史报告页会自动列出所有文件夹里的报告（最新在前）
- 想找某次数据：按采集时间找到对应文件夹即可，不会丢

## 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Windows |
| 手机 | 安卓真机，开启 **USB 调试**（设置 → 开发者选项 → USB 调试），USB 数据线连接 |
| Python | 装了 Python 3.8+ 用 `python main.py`；**没装 Python 也能跑**（用 `uv`，见下） |
| adb | Android SDK Platform-Tools，需让命令行能认到 `adb` |

### 让命令行认到 adb（选一种，做一次就行）

**方法 A（最省事）：把 adb 三个文件复制到系统目录**
1. 打开文件夹 `C:\platform-tools`（资源管理器地址栏输入 `C:\platform-tools` 回车）
2. 选中 `adb.exe`、`AdbWinApi.dll`、`AdbWinUsbApi.dll` 三个文件 → 复制
3. 粘贴到 `C:\Windows\System32`（地址栏输入 `C:\Windows\System32` 回车；弹"需要管理员权限"点"继续"）
4. 验证：新开命令行输入 `adb version`，有版本号即成功

> 没有 `C:\platform-tools` 的话：下载 https://dl.google.com/android/repository/platform-tools-latest-windows.zip 解压后得到该文件夹，再按上面做。

**方法 B（标准做法）：配置 PATH 环境变量**
1. Win 键搜索"环境变量" → 编辑系统环境变量 → 右下角"环境变量"
2. 用户变量 Path → 编辑 → 新建 → 填 `C:\platform-tools` → 一路确定
3. 重开命令行，`adb version` 验证

### 没有 Python？用 uv 零安装运行

命令行输入 `uv --version`，有版本号就说明可以用 uv 直接跑（uv 会自动下载 Python，无需安装）：

```bash
uv run --no-project python main.py
```

第一次运行会显示 Downloading cpython…（下载约 1 分钟），之后就正常了。

## 快速开始（保姆级）

1. **手机连接电脑**：USB 数据线连上，手机解锁后弹窗选"允许 USB 调试"并勾选"始终允许"。命令行验证：`adb devices` → 应看到 `XXXXXXX  device`（结尾是 `device` 才成功）。
2. **手机打开游戏**：微信 → 进入被测小游戏 → 停在游戏主界面。⚠️ 保持前台，别退回桌面、别锁屏。
3. **运行采集器**：打开 `collector` 文件夹，在**地址栏**输入 `cmd` 回车（会直接弹出定位到该目录的命令行），然后：
   - 装了 Python：`python main.py`
   - 没装 Python：`uv run --no-project python main.py`
   
   看到类似下面就是成功了（让它跑 1~2 分钟，游戏里做点操作）：
   ```
   [+] 已连接设备: A3GD6R2A09011153
   [+] 目标进程: com.tencent.mm（匹配 appbrand） pid=xxxx
   [*] 开始采集...
   [12:00:01] FPS=60 Jank%=0 CPU总%=12.3 CPU进程%=8.5 PSS=234567kB
   ```
4. **停止**：按 **Ctrl + C**。
5. **结果文件**：`collector\output\perfdog_<时间戳>.jsonl`，记事本打开可看。

- 默认每 **1 秒**采一个点，输出到 `collector/output/perfdog_<时间戳>.jsonl`。

## 导出报告（JSONL → HTML / Excel / CSV）

采集数据可导出为多种格式，**HTML 为自包含单文件**（ECharts 已内联，双击即看，可任意拷贝分享）：

```bash
# 在 collector 目录下
uv run --no-project python export_report.py --input output/20260812_164653/perfdog_20260812_164653.jsonl --format html   # 自包含 HTML 报告
uv run --no-project python export_report.py --input output/20260812_164653/perfdog_20260812_164653.jsonl --format csv    # CSV（Excel 直接打开）
uv run --with openpyxl python export_report.py --input output/20260812_164653/perfdog_20260812_164653.jsonl --format xlsx # Excel
```

> xlsx 需要 openpyxl，用 `uv run --with openpyxl` 即可（零安装）。导出列：时间/FPS/Jank/帧时间P50·P95·Max/刷新率/CPU整机·进程/PSS·RSS/上下行速率/温度/功率/电流/电压。

## 常用参数

```bash
python main.py --interval 0.5     # 采样间隔 0.5 秒
python main.py --duration 120     # 只采 120 秒自动停
python main.py --output ../test1  # 输出到其他目录
python main.py --serial 设备序列号  # 多设备时指定
python main.py --web              # 启动实时 Web 看板（浏览器查看）
python main.py --web --port 9000  # 指定看板端口（默认 8080）
```

## Web 看板（实时曲线 + 历史报告）

加 `--web` 参数即可获得 PerfDog 式实时看板（浏览器打开，数据全在本地、不传云端）：

```bash
python main.py --web
```

启动后按提示用浏览器打开：

| 地址 | 内容 |
|---|---|
| `http://localhost:8080` | **实时看板**：FPS / CPU / 内存 / 帧时间 / 网络 / 温度实时曲线 |
| `http://localhost:8080/report.html` | **历史报告**：列出 `output/*.jsonl`，点开看完整曲线 + 均值/最高/最低/P95 统计 |

- **实时性**：实时看板走 **SSE 推送**（采集到即推，毫秒级响应），浏览器断线自动降级为 1s 轮询
- 采集过程中：看板实时更新；停止采集（Ctrl+C）后 Web 服务**继续运行**，可继续查看本次与历史报告
- 再按一次 Ctrl+C 彻底退出（关闭 Web 服务）
- 手机不在也能看历史报告（数据来自本地 jsonl）

## 配置（config.json）

```jsonc
{
  "package": "com.tencent.mm",        // 微信包名，一般不用改
  "process_pattern": "appbrand",      // 匹配小游戏子进程（微信:appbrandN）
  "serial": "",                       // 指定设备序列号，留空自动选第一台
  "interval_ms": 1000,                // 采样间隔（毫秒）
  "duration_s": 0                     // 采集时长（秒），0 = 直到手动停止
}
```

> `process_pattern` 决定采哪个进程：小游戏逻辑跑在微信的 `com.tencent.mm:appbrandN` 子进程，默认按 `appbrand` 匹配；找不到时回退微信主进程。

## 测任意 App（通用扩展）

采集器支持**任意安卓应用/游戏**——不限于微信小游戏（2026-08-13 扩展：层匹配支持普通 View 应用的窗口层）。两种选应用方式：

```bash
# 方式 A：先看当前前台是哪个 App（把目标 App 切到前台后运行）
python main.py --show-foreground
# → 输出形如  mCurrentFocus=Window{... u0 com.example.game/com.example.game.MainActivity}

# 方式 B：直接指定包名采集（不用改配置）
python main.py --package com.example.game --process-pattern "" --web
#   --package <包名>        目标应用包名（从 --show-foreground 的输出里拿）
#   --process-pattern ""    原生 App 直接采主进程（微信小游戏才用 appbrand）
```

也可以复制 `config.app.json` 改 `package` 后用 `--config config.app.json`。

**原生 App 与微信小游戏的差异**：
- FPS：SurfaceFlinger 采集对普通 View 应用同样适用（走应用窗口层），**无 profileable 限制**
- CPU / 内存 / 网络 / 温度：完全兼容，无差异
- 微信小游戏锁 60Hz 是平台行为（未调 `wx.setPreferredFramesPerSecond`）；测原生 App 可验证高帧率（若该 App 支持高刷）

## 埋点 SDK（小游戏引擎级指标，骨架）

`..\sdk\perfdog-sdk.js` 为小游戏内嵌埋点 SDK 骨架（Laya 版）：
- 挂钩 `Laya.Stat` 读引擎级指标：DrawCall / 三角面 / GPU 内存 / 节点数（多级降级读取）
- rAF 包裹采样：帧间隔分布、JS 主线程忙占比、卡顿帧计数
- `scene()` / `mark()` 场景与关键节点打点，时间戳 `t_ms` 与采集器对齐
- 数据写本地文件（`wx.env.USER_DATA_PATH/perfdog_sdk.jsonl`）

接入与数据回传方式见 `..\sdk\perfdog-sdk.js` 文件头注释与架构文档 §5。

## 输出文件

采集数据位于 `output/<时间戳>/perfdog_<时间戳>.jsonl`，每行一个采样点（JSON）：

```json
{"ts": 1723412345.678, "t_ms": 1234.5, "fps": {"total_frames": 126, "fps": 59.86, "jank_rate": 0.0, "frame_p50_ms": 16.7, "frame_p95_ms": 16.8, "frame_max_ms": 17.0, "refresh_hz": 60.0}, "cpu": {"pid": 12345, "cpu_total_pct": 12.3, "cpu_proc_pct": 8.5}, "mem": {"pid": 12345, "pss_kb": 234567, "vmrss_kb": 345678}, "net": {"rx_kbps": 7.8, "tx_kbps": 1.7}, "therm": {"temp_c": 36.0, "current_ma": null, "voltage_v": 4.12, "power_w": null}}
```

| 键 | 含义 |
|---|---|
| `ts` / `t_ms` | Unix 秒 / 相对启动毫秒（对齐埋点时间轴） |
| `fps.fps` | 帧率；`jank_rate` = 卡顿帧占比；`frame_p50/p95/max_ms` = 帧时间分布（卡顿定位关键）；`refresh_hz` = 屏幕刷新率 |
| `cpu.cpu_total_pct` | 整机 CPU 占用%；`cpu_proc_pct` = 目标进程 CPU（**单核折算，多核满载可 >100%**） |
| `mem.pss_kb` / `vmrss_kb` | PSS 内存（与 PerfDog 同口径）/ RSS 驻留内存（同源解析，恒满足 RSS ≥ PSS） |
| `net.rx_kbps` / `tx_kbps` | **整机网络流量（除 lo）**，非进程级（免 root 免 profileable 方案的固有口径，PerfDog 官方免 root 同理） |
| `therm.temp_c` / `current_ma` / `voltage_v` / `power_w` | 电池温度 / 电流 / 电压 / 功率（**温度是电池温度 ≠ SoC 温度**；机型无权限时为 null） |

> 完整指标口径详解见 `指标说明.md`。

## 容错行为（采集器内置）

| 情况 | 表现 | 自动处理 |
|---|---|---|
| 小游戏不在前台 / 没跑起来（无渲染层） | FPS 显示「无渲染层(游戏请在微信前台)」 | 每 5 秒重试匹配一次渲染层（不空转刷屏），回前台自动恢复 |
| 渲染层失效（读帧失败） | FPS 显示「渲染层失效,重匹配中」 | 立即标记失效并重匹配 |
| 渲染层存在但连续 3 轮无帧提交（僵尸层） | FPS=0 | 自动放弃该层重新匹配，防止一直报 0 |
| 内存采样节流 | PSS 显示「(节流)」 | `dumpsys meminfo` 较慢（~0.5s），每 2 秒真采一次避免拖慢采样间隔 |
| CPU 时钟频率差异 | 无感 | 自动 `getconf CLK_TCK` 校准（个别机型不是 100Hz），避免进程 CPU% 算错 |

## 常见问题

- **`未检测到已连接的设备`**：换 USB 口 / 换数据线 / 确认手机弹出"允许 USB 调试"并勾选始终允许。
- **`未找到 com.tencent.mm 的进程`**：确认小游戏已打开且在**前台**（回到桌面或锁屏会掉）。
- **FPS 显示"无渲染层(游戏请在微信前台)"**：小游戏没在前台。微信小游戏的 WebGL 渲染层（SurfaceView）在退后台时会被系统销毁，回游戏前台即自动恢复。
- **FPS 一直为 `-`/0**：微信小游戏的 WebGL 渲染走 SurfaceView，`dumpsys gfxinfo` 采不到帧（已实测确认），采集器改用 **SurfaceFlinger 帧统计**（`--latency` 按渲染层读取，PerfDog 同思路）。**静止画面 FPS=0 是正常的**（画面没变化就不提交新帧）；游戏操作/战斗时才有帧率。若一直 0，确认游戏真的在渲染画面。
- **FPS 上限 / 高刷新率**：采集器**没有帧率上限**（90/120/144Hz 都能采），看板 FPS 图会自动放缩。游戏帧率上限 = 手机当前屏幕刷新率（如 60Hz 屏游戏最多 60fps）；想看高帧率，在手机系统设置里把屏幕刷新率调到 90/120/144Hz，游戏会随之解锁。
- **Jank 率口径**：帧间隔超过 **2×屏幕刷新周期** 算卡顿帧（60Hz 屏 → 33.3ms，120Hz 屏 → 16.7ms，随刷新率自适应）。
- **温度/功耗显示为空**：部分机型电池节点需 root（荣耀 `/sys` 节点即如此），工具已自动降级 `dumpsys battery` 读温度/电压；电流节点多数机型读不到，属正常。看板会隐藏无数据的图表。
- **CPU 进程占用为 0**：首次采样为基准点（差值算法需两个点），下一轮即有值。
- **命令行 `adb` 报"找不到 adbwinapi.dll"**：多半是 adb 文件被复制进 `C:\Windows\System32` 后无法加载（System32 为 64 位目录，DLL 加载不匹配）。**采集器已自动绕过**（优先用 `C:\platform-tools\adb.exe`），不影响使用；想彻底修好可手动清掉 System32 里的坏文件（以管理员身份开命令提示符）：
  ```bat
  del C:\Windows\System32\adb.exe C:\Windows\System32\AdbWinApi.dll C:\Windows\System32\AdbWinUsbApi.dll
  ```

## 第一阶段验收清单（真机）

- [ ] 连接真机后 `python main.py` 能启动并打印设备信息、目标 pid
- [ ] 小游戏前台运行时，每间隔稳定输出一行采样，FPS 接近游戏帧率（如 30/60）
- [ ] CPU 整机% + 进程% 有数值，且随场景负载变化
- [ ] PSS 内存有数值，且随游戏内操作波动
- [ ] Ctrl+C 停止后生成 JSONL 文件，内容可读、时间戳连续
- [ ] 切后台（小游戏退到微信主页）后 FPS 归零/无输出，切回恢复 —— 验证进程识别正确

## 下一步

1. ~~确认小游戏引擎~~ ✅ 已确认：**LayaAir 3.4 引擎（WebGL 渲染）**，见架构文档 §5
2. 真机验证：
   - 第一阶段采集器跑通（上方验收清单），把控制台摘要/JSONL 反馈给我核对口径
   - `Laya.Stat.show()` 看游戏内面板，确认 release 版统计项可用性（决定 SDK 引擎级字段取舍）
3. 第二阶段：本地 ECharts HTML 报告生成器 + 埋点 SDK 真机联调（数据时间轴合并）
