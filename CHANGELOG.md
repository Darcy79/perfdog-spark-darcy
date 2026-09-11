# CHANGELOG — 自研 PerfDog 变更日志

> **接手项目先读本文件最新 1~2 条 + `AGENTS.md`（协作规约）**，不必通读代码。
> 每条格式：`版本 | 日期 | 负责 | 改动文件 | 为什么 | 影响面`。
> 维护人：主会话（每次提交同步追加；历史条目由版本记录回溯整理）。

---

## 当前状态（2026-09-11，v66）

| 项 | 值 |
|---|---|
| 前端资源版本 | **v60**（`web/index.html` + `web/report.html` 各 3 处 `?v=`） |
| 测试 | Python **156** 条 + JS **59** 断言（全绿） |
| 采集环境 | Windows + adb 真机（荣耀 ADT-AN00 Magic3 Pro / OPPO；详见 `devices.md`） |
| 工具链 | `uv`（Python）+ `bun`（JS 测试）+ `adb`；路径见 `AGENTS.md` §4 |

**未完成 / 待验证**

- 代码（非阻塞）：report.html 内联 JS 抽离、main.py 可测试化、web.py 18 端点端到端测试、apk_label 二进制解析模糊测试；前端尚未展示 `fps_clamped` / `fps_warn`（当前只在 jsonl 与 CSV/XLSX 里）
- 待真人参与：真机矩阵（微信多开 / 分屏、高刷切换、30min+ 长测）；官方 PerfDog 对拍（**先出《口径差异白皮书》**，口径素材见 `指标说明.md`「九」）；tag 打包验证 CI
- 待真机确认：开小游戏时 appbrand 进程的 `comm`/`cmdline` 实测值；OPPO 及其他品牌 `comm` 截断方向（首 15 / 末 15）；微信多开时 appbrand1/2 能否区分

---

## v66（2026-09-11 · 智谱GLM5.3flash 编码 + 主会话核实）

- **改动**：`collector/metrics/fps.py`（`resolve_layer_ex()` 返回 `(layer, err)`，把「读失败」
  与「真的没有层」分开：`--list` 异常/输出空 → 新错误码 **`probe_fail`**；`--list` 成功但
  无匹配层 → 保持 `no_layer`；旧签名 `resolve_layer()` 保留兼容）；
  `collector/pidresolver.py`（身份校验三态：`True` 确认 / `False` cmdline 可读且明确不匹配
  → 立即失效 / **`None` 读不到 = 未知** → 沿用旧 pid，连续 `IDENTITY_FAIL_STREAK = 3` 次
  才判失效；comm 不匹配只算未知不算否定——截断方向因 ROM 而异）；
  `collector/adb.py`（`shell(args, retries=1)`：瞬时通道错误 `error: closed` / `device
  offline` / `device not found` / `connection reset` 重试 1 次、间隔 0.15s；命令本身失败
  与**超时不重试**）；`collector/main.py`（新增 `ChannelAlertTracker` 状态机 → 断连/缺数
  写 jsonl 事件行 `channel_alert`（kind = `disconnect` / `missing_metric` / `recovered`，
  状态沿触发天然去重）；新增 `row_has_any_value()` 首点门槛修复首点全空；控制台文案补
  `probe_fail`）；`tests/test_parsers.py`（+17 用例，139 → **156**）
- **为什么**：run `20260911_162353`（16:23:53–16:24:55）63 点中 **51 点误报 `no_layer`**、
  32 点 `pid=None`，事后核对**层（`#18944`）与进程（13694）全程都在**，且同代码同设备
  事后三轮实测全绿（组件级 75s、9 流并发压测 45s、真实采集器 70s 均零缺数）、Windows
  无 USB 事件、设备 logcat 无异常 ⇒ 实为**主机 adb 通道瞬时失败**；但代码把「读不到」
  归因为「层不存在 / 进程消失」，且告警只打印不落盘，导致数据大面积空洞**且事后不可判读**
- **影响面**：新增错误码 `probe_fail`（历史数据的 `no_layer` 语义不变）；jsonl 新增
  `channel_alert` 事件行（前端 `prepareRows`、导出 `data_rows`、`data_health` 均按 `event`
  字段跳过——已核实）；采集首点不再全空；pid 判失效最多延后 ~10s（真死亡）
- **验证**：① **156 测试全绿**（`uv run --no-project python -m unittest discover -s tests -p "test_*.py"`）；
  ② 真机 60s 抽检（主会话独立跑）：59/59 点有 FPS，`no_layer=0 / probe_fail=0 / mem.pid=None=0 /
  cpu.pid=None=0`，首点 `t_ms=1000.8` 即含 FPS；③ 主会话独立降级验证（stub `--list` 抛异常）：
  连续 6 点均为 `probe_fail`、通道保持 `sf` 且零 `gfxinfo` 调用、含 `channel_alert` 的 jsonl
  导出正常且事件行不进入内联数据
- **未决**：`channel_alert` 的真机端到端形态未实测（60s 正常运行零故障）；报告页对
  `probe_fail` / `no_layer` 的差异化标注与「缺数率」展示待做（web 域，主会话）

## v65（2026-09-11 · 主会话）

- **改动**：`collector/main.py`（目标错配自检从采样主循环 **移出**，改为独立守护线程
  `_mismatch_watch()`，新增常量 `MISMATCH_CHECK_INTERVAL = 10.0`）；
  `指标说明.md` 升 **V0.5**（新增「一、1」FPS 口径对比小节：上屏帧率 vs 引擎渲染循环帧率；
  「一、3」补充帧时间 P50≈P95≈Max 的正常性说明）；
  `web/index.html` + `web/report.html`（FPS 卡标题加 `.h2-q` 口径提示徽标；报告页 hint-card
  补 FPS 口径与帧时间说明）；`web/assets/style.css`（`.h2-q` 样式）；资源版本 **v59 → v60**
- **为什么**：① 真机 run `20260911_140605` 出现 **7 次 21.0s 采样间隔跳变**（t_ms
  198669→219683→240698…）——v63 的错配自检在采样主循环里每 5s 同步执行一次
  `dumpsys SurfaceFlinger --list`，设备/链路卡顿时 adb 阻塞约 20s，把采样循环一起拖住，
  造成可见数据空洞；② 用户明确要求：SDK 埋点实装前，先把"本工具 FPS（上屏）"与"引擎/
  开发者工具 FPS（渲染循环）"的口径差异在文档和界面标注清楚
- **影响面**：错配自检变为 **10s 一次异步检测**，采样间隔恢复正常（检测延迟 ≤10s，可接受）；
  界面/文档新增口径提示（**无数据格式变化**，jsonl 结构不变）
- **验证**：`compileall` 通过 + **139 测试全绿**；前端资源版本核查无 `?v=59` 残留

## v64（2026-09-11 · 主会话）

- **改动**：`collector/metrics/mem.py`（parse_meminfo 增解析 `TOTAL SWAP PSS` → 落盘
  `swap_pss_kb`；MemCollector 结果新增该字段）；`collector/data_health.py`（`rss_lt_pss`
  规则改用"非 swap PSS = pss − swap_pss"与 RSS 比较；swap 缺失时保持原口径）；
  `tests/test_mem_swap.py` 新建（8 用例）
- **为什么**：`dumpsys meminfo` 的 **TOTAL PSS 含 swap 部分**，进程被换出时会出现
  PSS > RSS（真机 appbrand0：PSS 231MB / RSS 211MB / SWAP PSS 141MB），被规则误判为
  "内存解析异常"（老报告 172/172 点全命中）
- **影响面**：jsonl 的 `mem` 新增 `swap_pss_kb` 字段；**新采集不再产生该误报**；
  历史数据（无 swap 字段）维持原判定口径，其 `rss_lt_pss` 告警可能为 swap 误报
- **验证**：真机（fixed_pid=20621）采样得 `pss 1316615 / rss 1406756 / swap 101449`，
  `check_row_health` 返回空（不误报）；测试 131 → 139 全绿

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
