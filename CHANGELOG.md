# CHANGELOG — 自研 PerfDog 变更日志

> **接手项目先读本文件最新 1~2 条 + `AGENTS.md`（协作规约）**，不必通读代码。
> 每条格式：`版本 | 日期 | 负责 | 改动文件 | 为什么 | 影响面`。
> 维护人：主会话（每次提交同步追加；历史条目由版本记录回溯整理）。

---

## 当前状态（2026-09-11，v63）

| 项 | 值 |
|---|---|
| 前端资源版本 | **v59**（`web/index.html` + `web/report.html` 各 3 处 `?v=`） |
| 测试 | Python **131** 条 + JS **59** 断言（全绿） |
| 采集环境 | Windows + adb 真机（荣耀 ADT-AN00 Magic3 Pro / OPPO；详见 `devices.md`） |
| 工具链 | `uv`（Python）+ `bun`（JS 测试）+ `adb`；路径见 `AGENTS.md` §4 |

**未完成 / 待验证**

- 代码（非阻塞）：report.html 内联 JS 抽离、main.py 可测试化、web.py 18 端点端到端测试、apk_label 二进制解析模糊测试；前端尚未展示 `fps_clamped` / `fps_warn`（当前只在 jsonl 与 CSV/XLSX 里）
- 待真人参与：真机矩阵（微信多开 / 分屏、高刷切换、30min+ 长测）；官方 PerfDog 对拍（**先出《口径差异白皮书》**，口径素材见 `指标说明.md`「九」）；tag 打包验证 CI
- 待真机确认：开小游戏时 appbrand 进程的 `comm`/`cmdline` 实测值；OPPO 及其他品牌 `comm` 截断方向（首 15 / 末 15）；微信多开时 appbrand1/2 能否区分

---

## v63（2026-09-11 · 主会话）

**背景**：`20260911_133010` 报告再次采错进程（第 2 次同类事故）——采到 appbrand0
（PSS 231MB、CPU 增量≈0），而游戏实际在 appbrand1（PSS 1035MB）；铁证是 FPS 层名
`SurfaceView[...AppBrandUI1](BLAST)` 指向 appbrand1 而进程是 appbrand0（微信同时存在
appbrand0/1/2 三实例）。v44 的"按累计 CPU 选最活跃"会偏向存在时间久的进程，且锁定后
不再重选。**用户要求：双击启动后先轻量检测展示，用户选定进程、点开始采集之前不记录
任何数据。**

- **后端**（commit `0a88b2b`）：
  · 新增 `collector/probe.py`——启动期只读探测（`parse_ps_candidates` /
    `pick_game_layer`（挑 SurfaceView 游戏层并解析 `AppBrandUI(n)`，排除
    InputSink/GestureNav/Input/Background）/ `parse_stat_ticks` / `parse_vmrss_kb` /
    `probe_once`）；推荐优先级：**层索引匹配 > 探测窗增量 CPU 最大 > 第一候选**
  · `main.py` 新增 `--auto`（跳过向导）；带 `--web` 默认走向导：探测（**不建任何采集
    输出**）→ 终端打印候选/推荐 → 等待确认（网页 `/api/start` 或本窗口回车/输入 pid）
    → **确认后才创建 jsonl 并采样**；采集期每 5s 做"层 vs 进程索引"错配自检，不一致写
    `target_mismatch` 事件行 + 页面告警，**不自动切换**（切换前必先采错数据）
  · `pidresolver.py` 新增 `fixed_pid` 模式：用户选定后固定采集该 pid，进程消失返回
    None（缺数可见），不再自动改选
  · `web.py` 新增 GET `/api/candidates`（TTL 8s）、POST `/api/start?pid=`（校验 pid
    在候选中）；`status` 增 `phase` / `target_source` / `mismatch`
- **前端**（本次提交）：`web/index.html` 启动向导卡片（候选列表 + 推荐标记 + 内存/CPU
  增量 + 「开始采集」/「重新探测」）+ 采集期错配横幅（含「停止并重新选择」/「忽略本次」）；
  `web/assets/style.css` 向导样式；`?v=` → 59
- **测试**：新增 `tests/test_probe.py` 18 用例 → Python 131 条全绿
- **真机验证**（荣耀 ADT-AN00）：探测识别层 `AppBrandUI1` → 推荐 **appbrand1**
  （RSS 1266MB、CPU 增量 84%），采错的 appbrand0（196MB、0%）被标不推荐；接口端到端
  验证：`/api/candidates` 200、非法 pid 被拒、`/api/start` 合法 pid 后
  `take_start_request()` 恰取走一次、跨站 Origin 403

## v62（2026-09-11 · 主会话）

- **改动**：`collector/web.py`（报告缓存新增"采样点总数"预算：`trim_report_cache` 纯函数 + `_report_cache_points/pints_max`）；`tests/test_fixes_20260911.py`（+5 用例）；**新建 `AGENTS.md`、`CHANGELOG.md`**
- **为什么**：① `_report_cache` 原策略只按"份数 ≤50"淘汰，每条是完整 rows（1 万点约 10~30MB），长测后连开多份长报告可能吃 GB 级内存；② 项目组多 AI 成员协作，需要单一同步入口，避免每次接手都通读项目
- **影响面**：采集端 web 服务（无接口/口径变化）；测试 108 → 113

## v61（2026-09-11 · 主会话）

- **改动**：`collector/web.py`（破坏性 POST 增同源校验 `same_origin_ok`）；`collector/main.py`（断连告警阈值 10→3 轮 + `backoff_sleep` 线性退避，封顶 5s）；`collector/export_report.py`（CSV/XLSX 追加"FPS已钳制/FPS低置信"列）；`tests/test_fixes_20260911.py` 新建（15 用例）
- **为什么**：长测前加固——防浏览器跨站 no-cors 静默触发 `/api/stop|shutdown|switch-target`；缩短设备半死时告警延迟（原最坏 3 分钟）；导出可筛选不可信采样点
- **影响面**：**导出多两列（列尾，原列序不变）**；测试 93 → 108

## v60（2026-09-11 · kimi k3 交付 + 主会话补正）

- **改动**：`devices.md`（**新建 V1.0**：适配矩阵 / 机型行为差异 / 「五、换机验收步骤」/ 待验证场景 / 阈值过拟合提醒）；`架构设计.md`（**重写 V1.0**）；`指标说明.md`（**V0.4**：Jank 节奏校准口径 + 自掩蔽局限，新增「九、统计口径说明」「十、数据健全性自检」）；`README.md`（「5 分钟上手」+「文档索引」）；6 份文档头部状态标注
- **为什么**：文档收敛与交接准备；把口径与自查规则写成可查文档（此前 data_health 规则零文档）
- **影响面**：仅文档，无代码

## v59（2026-09-11 · 智谱GLM5.3flash）

- **改动**：`collector/metrics/fps.py`（FPS 物理上限钳制 + `fps_clamped`/`fps_warn` 落盘 + FPS/Jank 口径说明 + `_parse_latency` 改"首个非空行"+刷新周期物理窗口 + Jank 注释校正）；`collector/pidresolver.py`（`_identity_ok`：cmdline 完整名优先 + comm 回退）；`collector/metrics/mem.py`（pid=None 不回退包名）；`collector/export_report.py`（`script_safe_json` 转义 `</script>`）；`tests/test_parsers.py`（+17）
- **为什么**：独立评估发现的高风险项——FPS 主段无上限可输出非物理值、comm 校验与自身文档假设矛盾（真机实测：荣耀 Android 14 的 comm 取"末 15 字符"）、mem 回退包名会造成假突跳、导出内联 JSON 有注入面
- **影响面**：**jsonl 新增字段 `fps_clamped` / `fps_warn`**；测试 76 → 93

## v58（2026-09-11 · 重活大鲸鱼DSV4P）

- **改动**：`web/assets/app.js`（锁定蓝线随缩放/平移重定位；长报告等距降采样 3000 显示点、统计仍全量；削减 `getOption()` 深拷贝；KPI 口径标注）；`web/report.html`（"?" 卡口径说明）；`web/assets/style.css`；`web/index.html`（版本号）
- **为什么**：锁定后拖时间条"所见≠所报"；长报告（数千点）打开与交互卡顿；统计口径未标注会影响与官方 PerfDog 对拍的可信度
- **影响面**：**前端资源 `?v=` → 58**；界面文案（"卡顿率（均值）""帧时间 P95（均值）"）

## v57（2026-09-11 · 智谱GLM5.3flash）

- **改动**：`.gitignore`（`!/perfdog.spec`）、`perfdog.spec`（**首次入库**）、`web/assets/app.js`（三处 `Math.min/max.apply` → 循环归约）、`web/report.html`（事件层 fetch 竞态守卫）、`web/index.html`（版本号）
- **为什么**：`perfdog.spec` 被 `*.spec` 忽略从未入库，而 CI 执行 `pyinstaller perfdog.spec` → **构建必失败**；长报告 `apply` 参数展开抛 `RangeError` 致统计栏崩溃；快速切报告时旧事件线会画到新报告上
- **影响面**：CI 打包链路恢复可用；`?v=` → 57

---

## 历史摘要（v36 ~ v56）

| 版本 / 时间 | 要点 | 负责 |
|---|---|---|
| v56（9-08） | 顶部时间条出入动效（与快照条观感一致） | pro |
| v52~v55（9-07） | rename 内联化；锁定时刻全指标快照条（引入 → 淡入淡出动效 → 按模块分行 → Excel 冻结窗格） | pro |
| v50/v51（9-03） | 左侧栏去卡片化（透明 + 右缘极淡分隔线）；全局深色细滚动条 | pro |
| v49（9-03） | 报告标题行收纳时长/采样点；左侧栏 sticky 常驻；双报告对比轻量版（两列 KPI + Δ 方向着色） | pro + 主会话补漏（注入转义/占位恢复） |
| v47/v48（9-02/03） | 看板大优化：KPI 阈值着色、"?" 操作卡、全局单时间条、📄 打开自包含报告、URL hash 深链、toast、窄屏修复 | Qwen + pro |
| v46（9-02） | 锁定浮层/白线数值降序；设备信息探测（型号/市场名/芯片/频率/分辨率） | pro |
| OPPO 适配（9-01） | 输入事件层 skip + 窗口层重匹配（FPS 0 修复）；FPS 主段法（修 0.01 病态值）；9-07 真人确认恢复（中位 ~57.6） | 主会话 + Qwen |
| v44/v45（8-27） | 采错进程修复（多 appbrand 按 utime+stime 选活跃）；Jank 节奏吸附阈值（修"面板 120Hz + 游戏锁 60fps"假 Jank） | 主会话（kimi 归因） |
| 数据健全性自检（8-27） | `data_health.py` 5 规则 + 实时告警 + 报告横幅；借此发现 2 份历史采错数据 | pro |
| v36~v43（8-22~8-26） | mem PSS/RSS 同源修复；加载态"加载完再显示"；Failed to fetch 修复；legend 点击拦截；runs/report 缓存 + 并发锁；15 golden test | 主会话 / Qwen / pro |

---

## 已知作废数据（历史，勿用于结论）

| 采集记录 | 问题 | 作废范围 |
|---|---|---|
| `20260824_082744` | PSS 全空（meminfo 段内空行解析 bug） | 内存指标 |
| `20260824_084252`、`20260827_083117` | 采错进程（采到闲置 appbrand） | CPU / 内存 / 网络 |
| `20260901_155646/155733/160412` 等 | OPPO FPS 病态 0.01（稀疏缓冲算法，已修） | 仅 FPS |
