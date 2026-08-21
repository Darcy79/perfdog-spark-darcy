/**
 * perfdog-sdk.js — 自研 PerfDog 小游戏埋点 SDK（LayaAir 版骨架）
 * =====================================================================
 * 配套工具：C:\Users\SparkGame\Desktop\deepseekv4-flash产出\自研perfdog\collector\
 * 版本：V0.1 骨架（2026-08-12）
 * 适配：LayaAir 3.4（已按 layabox/LayaAir LayaAir_3.4 分支核对 StatElement
 *       枚举与索引，与 3.3 一致；3.x 系列通用。索引表见下方 STAT_IDX）
 *
 * 功能：
 *   1) 挂钩 Laya.Stat —— 读取引擎级指标（FPS / DrawCall / 三角面 / GPU 内存等）
 *   2) rAF 包裹采样 —— 帧间隔分布、帧回调耗时（JS 主线程忙判断）
 *   3) 场景打点 scene() / 关键节点 mark()
 *   4) 本地 JSONL 落盘（wx 文件系统），时间戳 t_ms 与采集器对齐
 *
 * 接入方式（测试包专用，不改线上逻辑）：
 *   在 Laya 项目入口（如 Game.ts / Main.ts 的启动流程最前面）：
 *     import * as PerfDogSDK from "./perfdog-sdk";
 *     PerfDogSDK.start({ sampleIntervalMs: 1000 });
 *     // 场景切换处：
 *     PerfDogSDK.scene("战斗-甲修");
 *     // 关键节点：
 *     PerfDogSDK.mark("修甲开始");
 *
 * 数据格式（每行一个 JSON，t_ms 与采集器 JSONL 同时间轴）：
 *   {"t_ms":1234,"fps":60,"frameTimeMs":16.7,"raf":{"fps":60,"avgGapMs":16.6,"maxGapMs":80},
 *    "engine":{"drawCall":42,"triangles":3000,"gpuMemMB":12.5,"sprite2D":123,"sprite3D":456},
 *    "thread":{"busyPct":12.3,"overrun":2},"scene":"战斗-甲修"}
 *
 * ⚠️ 真机验证点（release 发布版统计项可能被裁剪，见架构文档 §5.1）：
 *   - SDK 启动时自动探测引擎统计可用性并打印，请把日志回传以确认哪些字段可用
 *   - 若 Laya.Stat 相关路径全部不可用，会自动降级为 rAF 计数（fps/raf 字段仍有效）
 * =====================================================================
 */
(function (global) {
  'use strict';

  // ------------------------------------------------------------------
  // 配置
  // ------------------------------------------------------------------
  var CONFIG = {
    sampleIntervalMs: 1000,   // 采样周期（毫秒）
    engineStats: true,        // 读取 Laya.Stat 引擎指标
    rafStats: true,           // rAF 帧间隔采样
    threadStats: true,        // JS 线程忙采样
    sceneTracking: true,      // 场景追踪
    writeToFile: true,        // 写本地文件（wx 环境）
    logFilename: 'perfdog_sdk.jsonl', // 输出文件名（wx.env.USER_DATA_PATH 下）
  };

  // ------------------------------------------------------------------
  // 内部状态
  // ------------------------------------------------------------------
  var state = {
    running: false,
    timer: null,          // 采样定时器
    startTs: 0,           // 启动时 Date.now()
    samples: [],          // 采样缓存
    sceneStack: ['_boot'],// 场景栈（当前场景 = 栈顶）
    marks: [],
  };

  // rAF 统计
  var raf = {
    api: null,
    count: 0,             // 包裹后累计帧数
    gapSum: 0,            // 帧间隔累计
    gapMax: 0,
    lastTs: 0,            // 上一帧时间戳（性能现在）
    busySum: 0,           // 回调耗时累计（主线程忙）
    overrun: 0,           // 帧间隔 > 2×平均的帧数（卡顿帧）
    lastSample: null,
  };

  // ------------------------------------------------------------------
  // 引擎统计读取（多级降级）
  // ------------------------------------------------------------------
  // LayaAir 3.x StatElement 枚举为顺序数字，下表基于官方 StatElement 声明顺序维护，
  // 优先用枚举对象（Laya.StatElement 等）按名字访问，失败才用索引兜底。
  // 若真机上报的字段名/索引失效，以当前引擎实际为准修订。
  var STAT_IDX = {
    CT_FPS: 0, T_Frame_Time: 1,
    CT_OpaqueDrawCall: 20, CT_TransDrawCall: 21, CT_DepthCastDrawCall: 22,
    CT_ShadowDrawCall: 23, CT_2DDrawCall: 24, CT_3DDrawCall: 25,
    CT_DrawCall: 26, CT_IndirectDrawCall: 27, CT_Instancing_DrawCall: 28,
    M_GPUMemory: 51, CT_Triangle: 53,
    C_Sprite2DCount: 58, C_Sprite3DCount: 59,
    C_BaseRenderCount: 60, C_MeshRenderCount: 61,
    C_SkinnedMeshRenderCount: 62, C_ShurikenParticleRenderCount: 63,
  };

  /** 找到 Stat 类（Laya.Stat 或 window.Stat） */
  function getStat() {
    if (typeof Laya !== 'undefined' && Laya.Stat) return Laya.Stat;
    if (global.Stat) return global.Stat;
    if (global.Laya && global.Laya.Stat) return global.Laya.Stat;
    return null;
  }

  /** 找到 statAgent（提供 getElementData） */
  function findStatAgent() {
    // Laya.Stat 未挂 LayaGL 时，依次探测常见挂载点
    var stat = getStat();
    var candidates = [];
    if (stat && stat.LayaGL && stat.LayaGL.statAgent) candidates.push(stat.LayaGL.statAgent);
    if (global.LayaGL && global.LayaGL.statAgent) candidates.push(global.LayaGL.statAgent);
    if (global.Laya && global.Laya.LayaGL && global.Laya.LayaGL.statAgent) candidates.push(global.Laya.LayaGL.statAgent);
    for (var i = 0; i < candidates.length; i++) {
      if (candidates[i] && typeof candidates[i].getElementData === 'function') {
        return candidates[i];
      }
    }
    return null;
  }

  /** 通过枚举对象或索引表取 element 数字 */
  function statElementOf(stat, agent, name) {
    // 1) 优先枚举对象
    var enumObj = (stat && stat.StatElement) || global.StatElement || null;
    if (enumObj && typeof enumObj[name] === 'number') return enumObj[name];
    // 2) 索引表兜底
    return STAT_IDX[name];
  }

  /** 读取引擎指标（返回对象，探测不到的字段为 null） */
  function readEngineStats() {
    var out = { fps: null, frameTimeMs: null, drawCall: null, triangles: null,
                gpuMemMB: null, sprite2D: null, sprite3D: null };
    var stat = getStat();
    if (!stat) return out;

    try {
      out.fps = stat.FPS != null ? Math.round(stat.FPS) : null;
    } catch (e) { /* 忽略 */ }

    var agent = findStatAgent();
    if (!agent) return out;

    function read(name) {
      var idx = statElementOf(stat, agent, name);
      if (idx == null) return null;
      try {
        var v = agent.getElementData(idx);
        return (typeof v === 'number' && isFinite(v)) ? v : null;
      } catch (e) {
        return null;
      }
    }

    out.frameTimeMs = read('T_Frame_Time');
    out.drawCall = read('CT_DrawCall');
    out.triangles = read('CT_Triangle');
    out.gpuMemMB = read('M_GPUMemory');      // 单位字节，下面换算
    if (out.gpuMemMB != null) out.gpuMemMB = Math.round(out.gpuMemMB / 1048576 * 100) / 100;
    out.sprite2D = read('C_Sprite2DCount');
    out.sprite3D = read('C_Sprite3DCount');
    return out;
  }

  // ------------------------------------------------------------------
  // rAF 包裹采样（帧间隔 / 回调耗时 / 卡顿帧）
  // ------------------------------------------------------------------
  function getRafApi() {
    if (global.requestAnimationFrame) return global.requestAnimationFrame;
    if (global.wx && wx.requestAnimationFrame) return wx.requestAnimationFrame;
    return null;
  }

  function wrapRaf() {
    if (!raf.api) return;
    var _raf = raf.api;
    global.requestAnimationFrame = function (cb) {
      return _raf.call(global, function (ts) {
        var now = (typeof performance !== 'undefined' && performance.now) ? performance.now() : (ts || Date.now());
        if (raf.lastTs > 0) {
          var gap = now - raf.lastTs;
          raf.count++;
          raf.gapSum += gap;
          if (gap > raf.gapMax) raf.gapMax = gap;
          // 卡顿判定：帧间隔超过预期（30fps 基准 33.3ms，超过 2 倍记 overrun）
          if (gap > 66.7) raf.overrun++;
        }
        raf.lastTs = now;
        var t0 = now;
        cb(ts);           // 执行原始回调（含 Laya 主循环）
        var busy = (typeof performance !== 'undefined' && performance.now) ? performance.now() - t0 : 0;
        raf.busySum += busy;
      });
    };
  }

  /** 读取本采样周期内的 rAF 统计 */
  function readRafStats() {
    if (raf.count === 0) return null;
    var avgGap = raf.gapSum / raf.count;
    var busyPct = 0;
    if (raf.busySum > 0 && raf.gapSum > 0) busyPct = Math.round(raf.busySum / raf.gapSum * 1000) / 10;
    var out = {
      fps: Math.round(1000 / avgGap),         // 由帧间隔换算
      avgGapMs: Math.round(avgGap * 10) / 10,
      maxGapMs: Math.round(raf.gapMax * 10) / 10,
      busyPct: busyPct,                       // JS 主线程忙占比（%）
      overrun: raf.overrun,                   // 卡顿帧数
    };
    // 归零，进入下一周期
    raf.count = 0; raf.gapSum = 0; raf.gapMax = 0; raf.busySum = 0; raf.overrun = 0;
    return out;
  }

  // ------------------------------------------------------------------
  // 采样 & 落盘
  // ------------------------------------------------------------------
  function sample() {
    if (!state.running) return;
    var now = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
    var rec = { t_ms: Math.round(now - state.startTs) };

    if (CONFIG.rafStats) rec.raf = readRafStats();
    if (CONFIG.engineStats) rec.engine = readEngineStats();
    if (CONFIG.sceneTracking) {
      rec.scene = state.sceneStack[state.sceneStack.length - 1] || null;
      if (state.marks.length) {
        // 附带上一周期内的 mark（打点偏移 = 相对 t_ms 的毫秒值）
        var t0 = (now - CONFIG.sampleIntervalMs);
        rec.marks = state.marks.filter(function (m) { return m.t >= t0; })
          .map(function (m) { return { name: m.name, dt_ms: Math.round(m.t - t0) }; });
        state.marks = state.marks.filter(function (m) { return m.t < t0; });
      }
    }

    state.samples.push(rec);
    appendLog(rec);
  }

  function appendLog(rec) {
    if (!CONFIG.writeToFile) return;
    var line = JSON.stringify(rec) + '\n';
    try {
      if (typeof wx !== 'undefined' && wx.getFileSystemManager && wx.env) {
        var fs = wx.getFileSystemManager();
        var file = (wx.env.USER_DATA_PATH || '') + '/' + CONFIG.logFilename;
        try {
          fs.appendFileSync(file, line);
        } catch (e) {
          // 文件不存在则先创建
          try { fs.writeFileSync(file, line, 'utf8'); } catch (e2) { /* 忽略 */ }
        }
      }
    } catch (e) {
      CONFIG.writeToFile = false; // 文件系统不可用则静默降级为内存缓存
    }
  }

  // ------------------------------------------------------------------
  // 对外接口
  // ------------------------------------------------------------------
  var PerfDogSDK = {
    start: function (config) {
      if (state.running) return;
      if (config) for (var k in config) if (Object.prototype.hasOwnProperty.call(config, k)) CONFIG[k] = config[k];

      state.running = true;
      state.startTs = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();

      // 引擎统计探测（供真机验证上报）
      var stat = getStat();
      var agent = findStatAgent();
      if (stat) {
        try {
          var probe = {
            statFound: !!stat,
            statAgentFound: !!agent,
            fps: stat.FPS != null ? stat.FPS : null,
          };
          console.log('[PerfDogSDK] 引擎统计探测: ' + JSON.stringify(probe));
        } catch (e) { console.log('[PerfDogSDK] 引擎统计探测失败: ' + e); }
      } else {
        console.log('[PerfDogSDK] 未找到 Laya.Stat，仅用 rAF 采样（降级）');
      }

      if (CONFIG.rafStats) {
        raf.api = getRafApi();
        if (raf.api) wrapRaf();
      }

      state.timer = setInterval(sample, CONFIG.sampleIntervalMs);
      console.log('[PerfDogSDK] started, interval=' + CONFIG.sampleIntervalMs + 'ms');
    },

    stop: function () {
      if (!state.running) return;
      state.running = false;
      if (state.timer) clearInterval(state.timer);
      state.timer = null;
      console.log('[PerfDogSDK] stopped, samples=' + state.samples.length);
    },

    /** 场景打点：scene('战斗-甲修')，压栈当前场景 */
    scene: function (name) {
      if (!CONFIG.sceneTracking) return;
      state.sceneStack.push(String(name));
    },

    /** 场景结束（弹栈回上一场景） */
    sceneEnd: function () {
      if (!CONFIG.sceneTracking) return;
      if (state.sceneStack.length > 1) state.sceneStack.pop();
    },

    /** 关键节点打点：mark('修甲开始') */
    mark: function (name) {
      if (!CONFIG.sceneTracking) return;
      var now = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
      state.marks.push({ name: String(name), t: now - state.startTs });
    },

    /** 导出数据：返回 JSON 数组（内存缓存）；指定 asString=true 返回 JSONL 文本 */
    report: function (asString) {
      if (asString) {
        return state.samples.map(function (r) { return JSON.stringify(r); }).join('\n');
      }
      return state.samples.slice();
    },
  };

  // 全局挂载（微信小游戏 / 浏览器环境直接 window.PerfDogSDK 或 globalThis.PerfDogSDK）
  global.PerfDogSDK = PerfDogSDK;
  // CJS 环境兼容（Node/bun 直接 require 时也能拿到）
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { PerfDogSDK: PerfDogSDK };
  }
})(
  // 取真正的全局对象：globalThis 在微信小游戏 / 现代 JS 环境均可用；
  // 旧环境依次回退 window / this。不能用裸 this（CJS 下 this === module.exports）。
  typeof globalThis !== 'undefined' ? globalThis
    : (typeof window !== 'undefined' ? window : this)
);
