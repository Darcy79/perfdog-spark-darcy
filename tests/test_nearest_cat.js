/**
 * web/assets/app.js 纯函数最小验证（2026-08-25，二次评估 #3）
 *
 * 只验证 nearestCat（事件标注线的二分查找）——它决定 logcat 事件竖线落在哪个
 * 类目上，越界/相等/未命中都容易写错，且浏览器里出错是"静默不渲染"，很难发现。
 *
 * 不引任何测试框架：把 app.js 当普通脚本执行（它是 IIFE，顶层只挂 window.PerfCharts，
 * document/echarts 仅在函数体内使用 → 无 DOM 也能加载），再对导出的纯函数断言。
 *
 * 运行（项目根目录，二选一）：
 *     bun tests/test_nearest_cat.js
 *     node tests/test_nearest_cat.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

// 无 DOM 环境：造一个 window 壳给 app.js 挂载导出
globalThis.window = globalThis;
const APP_JS = path.join(__dirname, '..', 'web', 'assets', 'app.js');
new Function(fs.readFileSync(APP_JS, 'utf8'))();

const nearestCat = window.PerfCharts && window.PerfCharts.nearestCat;
if (typeof nearestCat !== 'function') {
  console.error('[x] app.js 未导出 window.PerfCharts.nearestCat');
  process.exit(1);
}

let passed = 0;
const failures = [];
function eq(actual, expected, name) {
  if (actual === expected) { passed++; return; }
  failures.push(`${name}: 期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)}`);
}

// 采样点：0s / 1s / 2s / 3.5s（t_ms），类目轴取值 = ms/1000 保留 1 位小数
const T = [0, 1000, 2000, 3500];

// 空/无效输入
eq(nearestCat([], 500), null, '空数组返回 null');
eq(nearestCat(null, 500), null, 'times 为 null 返回 null');
eq(nearestCat(undefined, 500), null, 'times 为 undefined 返回 null');
eq(nearestCat(T, null), null, 'ms 为 null 返回 null');
eq(nearestCat(T, undefined), null, 'ms 为 undefined 返回 null');

// 单点数组
eq(nearestCat([1000], 0), 1, '单点：左越界夹到唯一点');
eq(nearestCat([1000], 9999), 1, '单点：右越界夹到唯一点');
eq(nearestCat([1000], 1000), 1, '单点：精确命中');

// 左右边界（事件早于首个采样点 / 晚于最后一个采样点 → 夹到端点，不返回 null）
eq(nearestCat(T, -1), 0, '左越界夹到首点');
eq(nearestCat(T, 0), 0, '命中首点');
eq(nearestCat(T, 99999), 3.5, '右越界夹到末点');
eq(nearestCat(T, 3500), 3.5, '命中末点');

// 精确命中中间采样点
eq(nearestCat(T, 1000), 1, '精确命中中间点');
eq(nearestCat(T, 2000), 2, '精确命中中间点2');

// 未命中：取最近的采样点
eq(nearestCat(T, 1200), 1, '偏左 → 取左邻');
eq(nearestCat(T, 1800), 2, '偏右 → 取右邻');
eq(nearestCat(T, 1500), 1, '正中间等距 → 取左邻（口径固定）');
eq(nearestCat(T, 2900), 3.5, '不等距区间内偏右 → 取右邻');
eq(nearestCat(T, 2700), 2, '不等距区间内偏左 → 取左邻');

// 类目取值精度：四舍五入到 0.1s（与 renderAll 的 x 轴生成口径一致）
eq(nearestCat([10440], 10440), 10.4, '类目值保留 1 位小数');
eq(nearestCat([10460], 10460), 10.5, '类目值四舍五入');

// 长序列：二分应稳定命中（顺带防死循环）
const LONG = Array.from({ length: 1000 }, (_, i) => i * 500);   // 0 ~ 499.5s，步长 0.5s
eq(nearestCat(LONG, 250 * 500 + 10), 125, '长序列：偏左取左邻');
eq(nearestCat(LONG, 250 * 500 - 10), 125, '长序列：偏右取右邻');
eq(nearestCat(LONG, 999 * 500), 499.5, '长序列：命中末点');

// ---------------- prepareRows：event 行过滤 + 核数抽取（v41 任务 3） ----------------
const prepareRows = window.PerfCharts && window.PerfCharts.prepareRows;
const setCores = window.PerfCharts && window.PerfCharts.setCores;
if (typeof prepareRows !== 'function' || typeof setCores !== 'function') {
  console.error('[x] app.js 未导出 window.PerfCharts.prepareRows/setCores');
  process.exit(1);
}
{
  const pr = prepareRows([
    { ts: 1.0, event: 'meta', cores: 8 },
    { t_ms: 500, cpu: { cpu_proc_pct: 50 } },
    { ts: 2.0, event: 'target_switch', to: 'com.x' },
  ]);
  eq(pr.rows.length, 1, 'prepareRows：event 行被过滤');
  eq(pr.rows[0].t_ms, 500, 'prepareRows：保留真实采样点');
  eq(pr.cores, 8, 'prepareRows：抽出核数 8');
}
{
  const pr2 = prepareRows([{ t_ms: 0, cpu: { cpu_proc_pct: 10 } }]);
  eq(pr2.rows.length, 1, 'prepareRows：无 meta 时保留采样点');
  eq(pr2.cores, null, 'prepareRows：无 meta 时核数为 null');
}
{
  setCores(12);
  const pr3 = prepareRows([{ t_ms: 0, cpu: {} }]);   // 无 meta → 重置为 null，不沿用上次 12
  eq(pr3.cores, null, 'prepareRows：无 meta 时重置核数（不沿用旧报告）');
}

// ---------------- prepareRows 抽取设备信息 + formatDeviceInfo（v46 优化 2） ----------------
const formatDeviceInfo = window.PerfCharts && window.PerfCharts.formatDeviceInfo;
if (typeof formatDeviceInfo !== 'function') {
  console.error('[x] app.js 未导出 window.PerfCharts.formatDeviceInfo');
  process.exit(1);
}
{
  const pr = prepareRows([
    { ts: 1.0, event: 'meta', cores: 8, device: { model: 'ADT-AN00', market_name: 'Magic3 Pro', cpu_hardware: 'SM8350', cpu_max_freq_mhz: '1804.8', screen_resolution: '1080×2388' } },
    { t_ms: 500, cpu: { cpu_proc_pct: 50 } },
  ]);
  eq(pr.device && pr.device.model, 'ADT-AN00', 'prepareRows：抽出设备信息 model');
  eq(pr.device.market_name, 'Magic3 Pro', 'prepareRows：抽出设备信息市场名');
}
{
  const dev = { model: 'ADT-AN00', market_name: 'Magic3 Pro', cpu_hardware: 'SM8350', cpu_max_freq_mhz: '1804.8', screen_resolution: '1080×2388' };
  const text = formatDeviceInfo(dev, 8);
  eq(text.indexOf('Magic3 Pro') >= 0, true, 'formatDeviceInfo：含市场名');
  eq(text.indexOf('ADT-AN00') >= 0, true, 'formatDeviceInfo：含型号代码');
  eq(text.indexOf('SM8350') >= 0, true, 'formatDeviceInfo：含 CPU 型号');
  eq(text.indexOf('1080×2388') >= 0, true, 'formatDeviceInfo：含分辨率');
  eq(text.indexOf('8 核') >= 0, true, 'formatDeviceInfo：含核数');
}
{
  eq(formatDeviceInfo(null, 8), '', 'formatDeviceInfo：null → 空串');
  eq(formatDeviceInfo({}, 8), '', 'formatDeviceInfo：空对象 → 空串');
  eq(formatDeviceInfo({ model: 'ADT-AN00' }, null), 'ADT-AN00', 'formatDeviceInfo：仅型号，无核数');
}

// ---------------- deviceInfoLines（v47：历史看板设备信息分行分字段） ----------------
const deviceInfoLines = window.PerfCharts && window.PerfCharts.deviceInfoLines;
if (typeof deviceInfoLines !== 'function') {
  console.error('[x] app.js 未导出 window.PerfCharts.deviceInfoLines');
  process.exit(1);
}
{
  // 全字段：设备(市场名+型号) / 芯片(硬件·主频·核数) / 分辨率 三行，顺序固定
  const dev = { model: 'ADT-AN00', market_name: 'Magic3 Pro', cpu_hardware: 'SM8350', cpu_max_freq_mhz: '1804.8', screen_resolution: '1080×2388' };
  const lines = deviceInfoLines(dev, 8);
  eq(lines.length, 3, 'deviceInfoLines：全字段 → 3 行');
  eq(lines[0].label, '设备', 'deviceInfoLines：第 1 行标签=设备');
  eq(lines[0].value, 'Magic3 Pro (ADT-AN00)', 'deviceInfoLines：市场名+型号组合');
  eq(lines[1].label, '芯片', 'deviceInfoLines：第 2 行标签=芯片');
  eq(lines[1].value, 'SM8350 · 1804.8MHz · 8 核', 'deviceInfoLines：芯片行硬件·主频·核数');
  eq(lines[2].label, '分辨率', 'deviceInfoLines：第 3 行标签=分辨率');
  eq(lines[2].value, '1080×2388', 'deviceInfoLines：分辨率值');
}
{
  // 市场名与型号代码相同 → 不重复附 "(代码)"
  const lines = deviceInfoLines({ model: 'ADT-AN00', market_name: 'ADT-AN00' }, null);
  eq(lines.length, 1, 'deviceInfoLines：同名不附型号 → 仅设备行');
  eq(lines[0].value, 'ADT-AN00', 'deviceInfoLines：市场名==型号时不重复');
}
{
  // 无市场名 → 设备行只显示型号代码
  const lines = deviceInfoLines({ model: 'PKC110' }, null);
  eq(lines.length, 1, 'deviceInfoLines：无市场名仅 1 行');
  eq(lines[0].value, 'PKC110', 'deviceInfoLines：无市场名 → 用型号');
}
{
  // 缺哪个跳哪个：无分辨率 → 无第 3 行；无主频 → 芯片行不含 MHz
  const lines = deviceInfoLines({ model: 'ADT-AN00', cpu_hardware: 'SM8350' }, 8);
  eq(lines.length, 2, 'deviceInfoLines：缺分辨率 → 2 行');
  eq(lines[1].value, 'SM8350 · 8 核', 'deviceInfoLines：芯片行缺主频跳主频');
}
{
  // 核数仅随芯片信息出现：无芯片信息时不孤零零出 "8 核" 行
  const lines = deviceInfoLines({ model: 'ADT-AN00', screen_resolution: '1080×2388' }, 8);
  eq(lines.length, 2, 'deviceInfoLines：无芯片信息 → 设备+分辨率 2 行');
  eq(lines[1].label, '分辨率', 'deviceInfoLines：无芯片行，第 2 行=分辨率');
}
{
  // device 无效 → 空数组（老数据无 device 时 report.html 不渲染设备块）
  eq(deviceInfoLines(null, 8).length, 0, 'deviceInfoLines：null → []');
  eq(deviceInfoLines({}, 8).length, 0, 'deviceInfoLines：空对象 → []');
  eq(deviceInfoLines(undefined, null).length, 0, 'deviceInfoLines：undefined → []');
}
{
  // formatDeviceInfo 基于 deviceInfoLines 重组，一行版语义一致（index.html 状态栏不变）
  const dev = { model: 'ADT-AN00', market_name: 'Magic3 Pro', cpu_hardware: 'SM8350', cpu_max_freq_mhz: '1804.8', screen_resolution: '1080×2388' };
  eq(formatDeviceInfo(dev, 8),
     'Magic3 Pro (ADT-AN00) · SM8350 · 1804.8MHz · 8 核 · 1080×2388',
     'formatDeviceInfo：基于 lines 重组为一行');
}

if (failures.length) {
  console.error(`[x] 断言失败 ${failures.length} 条（通过 ${passed}）：`);
  failures.forEach((f) => console.error('    - ' + f));
  process.exit(1);
}
console.log(`[+] 全部断言通过（${passed} 条）`);
