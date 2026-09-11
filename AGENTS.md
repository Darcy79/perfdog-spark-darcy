# AGENTS.md — 自研 PerfDog 项目协作规约（面向 AI 协作者）

> 本文件面向进入本项目的 AI 协作者（Qwen / kimi / pro / 智谱 GLM 等）。
> **先读完本文件（约 3 分钟），再按「动手前先读」清单取资料——不要通读全部代码**，
> 那既浪费 token 也没有必要；最近改了什么看 `CHANGELOG.md` 最新 1~2 条即可。

---

## 1. 项目一句话

微信小游戏 / 安卓 App 的**真机性能采集工具**（自研 PerfDog 替代）：
Windows + adb 采集 FPS / CPU / 内存 / 网络 / 温度 → 本地 `jsonl` → 本地自包含 HTML 报告；**数据不出本机**。

---

## 2. 当前状态（截至 2026-09-11，v62）

| 项 | 值 |
|---|---|
| 仓库 / 分支 | https://github.com/Darcy79/perfdog-spark-darcy · `main` |
| 前端资源版本 | **v58**（改前端必须升 `?v=`，`web/index.html` + `web/report.html` 各 3 处） |
| 测试 | Python **113** 条（`tests/`）+ JS **59** 断言（`tests/test_nearest_cat.js`） |
| 最近变更 | `CHANGELOG.md`（**接手前必读最新 1~2 条**） |

---

## 3. 硬约束（违反 = 返工）

1. **不要 `git commit` / `git push`**：所有改动留在工作区，由主会话统一核实、提交、同步 GitHub；
2. **文件域隔离**：多成员并行时按域动手——采集端 `collector/**` + `tests/**`、前端 `web/**`、文档 `*.md`；不越界改别人的域；
3. **改前端必升 `?v=`**（两页各 3 处），并确认无旧版本号残留；
4. **不覆盖用户数据**：`collector/output/**`（采集数据）、`collector/.app_labels.json`（本机缓存）只读；
5. **语义等价优先**：修 bug 不顺手重构、不夹带风格改写；不确定就在交付里说明，不要臆测；
6. 只改需要的文件；发现别处有问题**只报告**，不要"顺手修"。

---

## 4. 常用命令（Windows / 本机路径）

```powershell
# 采集（在 collector/ 下执行）
uv run --no-project python main.py --web            # --duration 60 定时长；--interval 1 采样间隔
# 只看历史报告（无需手机）
双击 start_dashboard.bat

# 测试（在项目根执行）
uv run --no-project python -m unittest discover -s tests -p "test_*.py"   # 应 113 条全绿
C:\Users\SparkGame\.cherrystudio\bin\bun.exe tests/test_nearest_cat.js    # 应 59 断言全过

# JS 语法检查（沙箱内 bun/cmd 不能走管道 → 必须 Start-Process 重定向）
C:\Users\SparkGame\.cherrystudio\bin\bun.exe build web/assets/app.js --outfile <临时文件>
```

- 工具路径：`uv` = `C:\Users\SparkGame\.cherrystudio\bin\uv.exe`；`bun` = `C:\Users\SparkGame\.cherrystudio\bin\bun.exe`；`adb` = `C:\platform-tools\adb.exe`
- 沙箱内跑外部程序（adb / git / bun）用 `Start-Process -RedirectStandardOutput/-RedirectStandardError`，直接管道会失败。

---

## 5. 口径速查（权威文档：`指标说明.md`，改指标算法前必读）

| 指标 | 口径 |
|---|---|
| **FPS** | `dumpsys SurfaceFlinger --latency`（layer 必须带 `#id`）→ 按相邻帧间隔 >0.5s 切段，取**帧数最多的主段**，(主段帧数−1)/主段时长；超物理上限被钳制（落盘 `fps_clamped`）、主段 <8 帧标低置信（`fps_warn="low_frames"`） |
| **Jank** | 阈值 = **2×节奏×1.1**；节奏 = 新增帧间隔中位数吸附标准档 60/90/120/144Hz（10% 容差）；新增帧 <8 回退 2×refresh_ns。**局限**：卡顿占比 >50% 时中位数自掩蔽、可能低估 |
| **帧时间 P50/P95/Max** | 只统计**新增帧**（0.5s 采样窗内新出现的帧） |
| **汇总卡口径** | 帧时间 P95 = **各采样点 frame_p95_ms 的算术平均**；卡顿率 = **各点 jank_rate 的算术平均**（都**不是**全体帧口径） |
| **网络** | **整机**流量（`/proc/pid/net/dev` 除 lo），非进程级 |
| **内存** | `smaps_rollup` 同源优先 → 失败回退 `dumpsys meminfo`；**pid 解析失败时不回退包名维度**（宁可缺数） |
| **pid 身份** | `/proc/<pid>/cmdline` 完整名优先 + `comm` 回退——Android `comm` 截断 15 字符，**荣耀 Android 14 取的是"末 15 字符"**（非标准首截断） |

FPS 与 Jank/帧时间的时间窗**故意不同**（FPS=滚动缓冲主段均值窗，Jank=0.5s 瞬时窗）：单点出现"FPS 正常但 Jank 偏高"属正常，**不要用单点互相对质**。

---

## 6. 动手前先读（按任务类型，别通读）

| 你的任务 | 先读 |
|---|---|
| 采集端 / 指标算法 | `指标说明.md`、`devices.md`、对应的 `collector/metrics/*.py` |
| 前端看板 | `web/assets/app.js`、`web/report.html`、`指标说明.md`「九、统计口径说明」 |
| 文档 | `架构设计.md`、`指标说明.md`、`devices.md` |
| 打包 / CI | `.github/workflows/build.yml`、`perfdog.spec`、`README.md` |
| 真机适配 | `devices.md`（含「五、换机验收步骤」） |

---

## 7. 交付格式（主会话据此核实）

1. 改动文件清单；
2. 每处 **diff 摘要**（前后关键代码，而不是"我改了 X"这种概述）；
3. **自检结果**（跑了什么命令、结果如何）；
4. 疑问 / 取舍说明（为什么这样改、有没有折中）。
   **不要 commit/push。** 主会话会逐处核实、跑回归、再提交同步。

---

## 8. 提交前自检清单

- [ ] Python 测试全绿（`unittest discover`，当前应为 113 条）
- [ ] JS 测试全绿（`bun tests/test_nearest_cat.js`，59 断言）
- [ ] JS 语法检查通过（`bun build` app.js / 页面内联脚本）
- [ ] 改了前端 → `?v=` 已升、无旧版本号残留
- [ ] 没有越界改其他成员的文件域
- [ ] 没有 `git commit` / `git push`
