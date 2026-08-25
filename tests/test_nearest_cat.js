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

if (failures.length) {
  console.error(`[x] nearestCat 断言失败 ${failures.length} 条（通过 ${passed}）：`);
  failures.forEach((f) => console.error('    - ' + f));
  process.exit(1);
}
console.log(`[+] nearestCat 全部断言通过（${passed} 条）`);
