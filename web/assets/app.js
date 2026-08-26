/**
 * app.js — 自研 PerfDog Web 看板共享逻辑
 * 提供 window.PerfCharts：图表创建 / 渲染 / 统计 / 无数据自动隐藏
 * V0.2：新增帧时间 / 网络 / 电池温度图表
 */
(function () {
  'use strict';

  var _resizeBound = false;

  function makeChart(elId, group) {
    var el = document.getElementById(elId);
    if (!el) return null;
    var chart = echarts.init(el);
    // 同一 group 的图表可联动（历史看板：鼠标悬停/点击时垂直线贯穿各模块）
    if (group) chart.group = group;
    // 容器初始宽度可能未就绪：立即按实际宽度重绘，防止曲线挤在左半边
    chart.resize();
    // 窗口缩放时统一重绘所有图表（只注册一次监听）
    if (!_resizeBound) {
      _resizeBound = true;
      window.addEventListener('resize', function () {
        document.querySelectorAll('.chart').forEach(function (c) {
          var inst = echarts.getInstanceByDom(c);
          if (inst) inst.resize();
        });
      });
    }
    return chart;
  }

  // ---------------- 卡片拖拽排序（2026-08-14 优化项①） ----------------
  function initSortable(containerSel) {
    var container = typeof containerSel === 'string'
      ? document.querySelector(containerSel) : containerSel;
    if (!container) return;
    var cards = container.querySelectorAll('.chart-card');
    if (!cards.length) return;
    var storeKey = 'perfdog_order_' + (location.pathname.replace(/[^a-z0-9]/gi, '_') || 'root');

    // 应用上次保存的顺序
    try {
      var order = JSON.parse(localStorage.getItem(storeKey));
      if (order && order.length) {
        var byId = {};
        cards.forEach(function (c) { byId[c.id] = c; });
        order.forEach(function (id) { if (byId[id]) container.appendChild(byId[id]); });
      }
    } catch (e) {}

    var dragCard = null;
    function saveOrder() {
      var ids = [];
      container.querySelectorAll('.chart-card').forEach(function (c) { ids.push(c.id); });
      try { localStorage.setItem(storeKey, JSON.stringify(ids)); } catch (e) {}
    }
    function clearTargets() {
      container.querySelectorAll('.chart-card').forEach(function (c) { c.classList.remove('drop-target'); });
    }

    cards.forEach(function (card) {
      card.draggable = true;
      card.classList.add('sortable');
      card.addEventListener('dragstart', function () {
        dragCard = card;
        card.classList.add('dragging');
      });
      card.addEventListener('dragend', function () {
        dragCard = null;
        card.classList.remove('dragging');
        clearTargets();
        saveOrder();
      });
      card.addEventListener('dragover', function (e) { e.preventDefault(); });
      card.addEventListener('dragenter', function () { card.classList.add('drop-target'); });
      card.addEventListener('dragleave', function () { card.classList.remove('drop-target'); });
      card.addEventListener('drop', function (e) {
        e.preventDefault();
        if (!dragCard || dragCard === card) return;
        container.insertBefore(dragCard, card.nextSibling);
        clearTargets();
      });
    });
  }

  // ---------------- 图表联动（2026-08-14 优化项②） ----------------
  // 同一 group 的图表在鼠标悬停/点击时同步显示垂直线与 tooltip，便于纵向对比
  function enableLink(group) {
    try { echarts.connect(group); } catch (e) {}
  }

  // ---------------- 点击锁定贯穿线（2026-08-14 优化项④，v18 DOM 覆盖层方案） ----------------
  // 悬停看贯穿（跟随）；点击任意位置 → 像素坐标换算最近类目下标 → 在每张图上放一条
  // 绝对定位的 DOM 竖线（.pin-line）。DOM 线不依赖 ECharts markLine/tooltip 渲染，
  // 必然可见、不会被刷新/鼠标移动覆盖，双击解锁。
  // v18 修复：markLine 可能被 ECharts 内部渲染干扰、convertFromPixel 需兜底——
  // 线用 DOM 元素画（独立于 ECharts），坐标换算失败时按像素比例兜底。
  var _pinCharts = [];
  var _pinIdx = null;

  // v41：判断点击是否落在 legend 区域（右上角"自选数据"）。legend 也在 canvas 内，
  // 点击选中/取消系列会触发 DOM click → 若不拦截，_pinIndexAtPx 会把贯穿线锁到
  // legend 所在像素（右上角）。返回 true 表示命中了 legend，应跳过锁定/解锁。
  function _hitLegend(chart, clientX, clientY) {
    try {
      var dom = chart.getDom();
      var r = dom.getBoundingClientRect();
      var x = clientX - r.left;
      var y = clientY - r.top;
      var model = chart.getModel();
      // getComponent('legend', true) 返回所有 legend 模型（多图时 legend 通常一个，防御性处理）
      var legends = model.getComponent ? model.getComponent('legend', true) : null;
      var list = (Array.isArray(legends) ? legends : (legends ? [legends] : []));
      for (var i = 0; i < list.length; i++) {
        var lm = list[i];
        var rect = lm && lm.coordinateSystem && lm.coordinateSystem.getBoundingRect
          ? lm.coordinateSystem.getBoundingRect() : null;
        if (!rect) continue;
        // rect 是 ZRender 内部像素（可能受 devicePixelRatio 缩放）→ 换算成容器内 CSS 像素
        var pr = (typeof window.devicePixelRatio === 'number' && window.devicePixelRatio > 0)
          ? window.devicePixelRatio : 1;
        // 保守加 4px 容差：legend 换行/贴边时点击紧邻空白也算命中
        var pad = 4;
        var L = rect.x / pr - pad, T = rect.y / pr - pad,
            R = (rect.x + rect.width) / pr + pad, B = (rect.y + rect.height) / pr + pad;
        if (x >= L && x <= R && y >= T && y <= B) return true;
      }
    } catch (e) {}
    return false;
  }

  function enableClickPin(groupSel) {
    _pinCharts = [];
    try {
      var g = echarts.getGroup(groupSel) || [];
      _pinCharts = _pinCharts.concat(g);
    } catch (e) {}
    document.querySelectorAll('.chart').forEach(function (el) {
      var inst = echarts.getInstanceByDom(el);
      if (inst && _pinCharts.indexOf(inst) < 0) _pinCharts.push(inst);
    });
    if (!_pinCharts.length) { console.log('[PerfDog] 点击锁定: 未找到图表'); return; }
    console.log('[PerfDog] 点击锁定已启用, 图表数=' + _pinCharts.length);

    _pinCharts.forEach(function (chart) {
      if (!chart) return;
      var dom = chart.getDom();
      if (!dom || dom.__pinBound) return;
      dom.__pinBound = true;

      if (getComputedStyle(dom).position === 'static') dom.style.position = 'relative';

      dom.addEventListener('click', function (e) {
        var x = e.clientX, y = e.clientY;
        // v41：点击 legend（右上角自选数据）时不锁定/解锁贯穿线，避免把蓝线锁到 legend 位置
        if (_hitLegend(chart, x, y)) { console.log('[PerfDog] 点击 legend → 跳过贯穿线锁定'); return; }
        var r = dom.getBoundingClientRect();
        var localX = x - r.left;      // 容器内像素 x（所见即所点的位置）
        var idx = _pinIndexAtPx(chart, dom, x, y);
        console.log('[PerfDog] 点击 x=' + Math.round(x) + ' y=' + Math.round(y) +
                    ' → idx=' + idx + ' localX=' + Math.round(localX));
        if (idx === null || idx < 0) return;
        if (_pinIdx === idx) { console.log('[PerfDog] 再点同点 → 解锁'); _pinUnlockAll(); }
        else { console.log('[PerfDog] 锁定 index=' + idx); _pinLockAll(idx, localX); }
      });
      dom.addEventListener('dblclick', function () { console.log('[PerfDog] 双击 → 解锁'); _pinUnlockAll(); });
    });
  }

  function _pinIndexAtPx(chart, dom, clientX, clientY) {
    var r = dom.getBoundingClientRect();
    var x = clientX - r.left;
    var y = clientY - r.top;
    try {
      var pt = chart.convertFromPixel({ xAxisIndex: 0 }, [x, y]);
      if (pt && typeof pt[0] === 'number' && isFinite(pt[0])) {
        var idx = Math.round(pt[0]);
        var cats = ((chart.getOption().xAxis || [{}])[0] || {}).data || [];
        if (cats.length) return Math.max(0, Math.min(idx, cats.length - 1));
        return idx;
      }
    } catch (e) {}
    // 比例兜底：类目大致均匀分布
    var cats = ((chart.getOption().xAxis || [{}])[0] || {}).data || [];
    if (!cats.length || r.width <= 0) return null;
    var idx2 = Math.round(x / r.width * cats.length);
    return Math.max(0, Math.min(idx2, cats.length - 1));
  }

  function _pinLockAll(idx, pxX) {
    _pinIdx = idx;
    var localX = (typeof pxX === 'number' && isFinite(pxX)) ? pxX : null;
    _pinCharts.forEach(function (c) {
      if (!c) return;
      var dom = c.getDom();
      var line = dom.querySelector('.pin-line');
      if (!line) {
        line = document.createElement('div');
        line.className = 'pin-line';
        dom.appendChild(line);
      }
      // v22：蓝线直接用点击处像素定位（所见即所点）。
      // 不再 dispatch showTip——白线/tooltip 完全交给 ECharts 默认跟随鼠标，
      // 避免 showTip 拉取与鼠标移动抢位造成白线偏移。
      if (localX !== null) {
        var w = dom.clientWidth || dom.getBoundingClientRect().width;
        line.style.left = Math.max(0, Math.min(Math.round(localX), w - 2)) + 'px';
      } else {
        _pinPlaceLine(c, line, idx);   // 兜底：用换算
      }
    });
    _pinShowData(idx, localX);   // 蓝线位置显示该模块 tooltip 风格数据浮层
  }

  // 锁定时刻各模块数据浮层（仿 tooltip 样式，DOM 实现，独立于 ECharts tooltip 不与白线冲突）
  var _pinRows = [];
  var _PIN_FIELDS = {
    'chart-fps':       function (r) { var f = r.fps || {}; var s = []; if (f.fps != null) s.push('FPS ' + f.fps); if (f.jank_rate != null) s.push('Jank ' + (f.jank_rate * 100).toFixed(1) + '%'); return s.join(' · '); },
    'chart-frametime': function (r) { var f = r.fps || {}; var s = []; if (f.frame_p50_ms != null) s.push('P50 ' + f.frame_p50_ms + 'ms'); if (f.frame_max_ms != null) s.push('Max ' + f.frame_max_ms + 'ms'); return s.join(' · '); },
    'chart-cpu':       function (r) { var c = r.cpu || {}; var s = []; if (c.cpu_total_pct != null) s.push('总 ' + c.cpu_total_pct + '%'); if (c.cpu_proc_pct != null) s.push('进程 ' + c.cpu_proc_pct + '%'); if (_cores && c.cpu_proc_pct != null) s.push('占整机 ' + (c.cpu_proc_pct / _cores).toFixed(1) + '%'); return s.join(' · '); },
    'chart-mem':       function (r) { var m = r.mem || {}; var s = []; if (m.pss_kb != null) s.push('PSS ' + (m.pss_kb / 1024).toFixed(1) + 'MB'); if (m.vmrss_kb != null) s.push('RSS ' + (m.vmrss_kb / 1024).toFixed(1) + 'MB'); return s.join(' · '); },
    'chart-net':       function (r) { var n = r.net || {}; var s = []; if (n.rx_kbps != null) s.push('↓' + n.rx_kbps); if (n.tx_kbps != null) s.push('↑' + n.tx_kbps); return s.join(' · ') + ' KB/s'; },
    'chart-temp':      function (r) { var t = r.therm || {}; var s = []; if (t.temp_c != null) s.push(t.temp_c + '°C'); if (t.voltage_v != null) s.push(t.voltage_v + 'V'); return s.join(' · '); },
  };
  function setPinData(rows) { _pinRows = rows || []; }
  function _pinShowData(idx, localX) {
    var row = _pinRows[idx];
    if (!row) return;
    _pinCharts.forEach(function (c) {
      if (!c) return;
      var dom = c.getDom();
      var fn = _PIN_FIELDS[dom.id];
      var t = row.t_ms != null ? (row.t_ms / 1000).toFixed(1) + 's' : '';

      // ① 标题行右侧数据卡（V22 效果，稳定常驻展示）
      var card = dom.parentElement;
      if (card) {
        var head = card.querySelector('.chart-head');
        if (head) {
          var pd = head.querySelector('.pin-data');
          if (fn) {
            if (!pd) {
              pd = document.createElement('span');
              pd.className = 'pin-data';
              head.appendChild(pd);
            }
            pd.textContent = '📌 t=' + t + ' ' + fn(row);
            pd.style.display = 'inline-block';
          } else if (pd) {
            pd.style.display = 'none';
          }
        }
      }

      // ② 蓝线旁浮层（V23 当前效果，跟随蓝线定位）
      var tip = dom.querySelector('.pin-tip');
      if (!fn) { if (tip) tip.style.display = 'none'; return; }
      if (!tip) {
        tip = document.createElement('div');
        tip.className = 'pin-tip';
        dom.appendChild(tip);
      }
      tip.textContent = 't=' + t + '\n' + fn(row);
      tip.style.display = 'block';
      // 浮层定位：跟随蓝线像素 x，右缘溢出时翻转到线左侧
      var w = dom.clientWidth || dom.getBoundingClientRect().width;
      var x = (typeof localX === 'number' && isFinite(localX)) ? Math.round(localX) : null;
      if (x === null) {
        try { x = Math.round(w * (idx + 0.5) / (_pinRows.length || 1)); } catch (e) { x = 0; }
      }
      var tw = tip.offsetWidth || 150;
      var left = x + 10;
      if (left + tw > w - 4) left = Math.max(4, x - tw - 10);
      tip.style.left = left + 'px';
    });
  }

  function _pinPlaceLine(chart, line, idx) {
    var x = null;
    try {
      var px = chart.convertToPixel({ xAxisIndex: 0 }, idx);
      if (typeof px === 'number' && isFinite(px)) x = px;
    } catch (e) {}
    if (x === null) {
      var cats = ((chart.getOption().xAxis || [{}])[0] || {}).data || [];
      var r = chart.getDom().getBoundingClientRect();
      x = Math.round((idx + 0.5) / (cats.length || 1) * r.width);
    }
    line.style.left = (x | 0) + 'px';
  }

  function _pinUnlockAll() {
    _pinIdx = null;
    document.querySelectorAll('.pin-line').forEach(function (l) { l.style.left = '-9999px'; });
    document.querySelectorAll('.pin-tip').forEach(function (tip) { tip.style.display = 'none'; });
    document.querySelectorAll('.pin-data').forEach(function (pd) { pd.style.display = 'none'; });
  }

  // ---------------- 自定义时间拖动条（2026-08-14 v28，PerfDog 云端风格） ----------------
  // 弃用 ECharts 自带 slider，自写小型 HTML 拖动条：
  //   - 体积小（高 14px）；按下即拖，无需精确抓手柄
  //   - 两端细蓝色竖条 = 缩放；中间选区拖动 = 平移；点击选区外 = 窗口跳转到点击处
  //   - 拖动时图表实时跟随；所有图表共享同一窗口（一个拖动条操作全部联动）
  var _dz = { start: 0, end: 100, charts: [], rows: [], sliders: [], n: 0 };

  function createTimeSliders(charts, rows) {
    var list = [];
    if (Array.isArray(charts)) list = charts.filter(Boolean);
    else list = Object.keys(charts).map(function (k) { return charts[k]; }).filter(Boolean);
    if (!list.length || !rows.length) return;
    // 清理旧拖动条
    _dz.sliders.forEach(function (s) { if (s.el && s.el.parentNode) s.el.parentNode.removeChild(s.el); });
    _dz.sliders = [];
    _dz.charts = list;
    _dz.rows = rows;
    _dz.n = rows.length;
    _dz.start = 0;
    _dz.end = 100;
    list.forEach(function (chart) {
      if (!chart) return;
      var el = _buildSlider(chart);
      _dz.sliders.push(el);
    });
    _renderAllSliders();
  }

  function _buildSlider(chart) {
    var dom = chart.getDom();
    var el = document.createElement('div');
    el.className = 'pd-slider';
    el._chart = chart;
    var canvas = document.createElement('canvas');
    canvas.className = 'pd-wave';
    var sel = document.createElement('div');
    sel.className = 'pd-selection';
    var hl = document.createElement('div');
    hl.className = 'pd-handle pd-handle-l';
    var hr = document.createElement('div');
    hr.className = 'pd-handle pd-handle-r';
    el.appendChild(canvas);
    el.appendChild(sel);
    el.appendChild(hl);
    el.appendChild(hr);
    // 插到图表容器下方（卡片内）
    if (dom.parentNode) dom.parentNode.appendChild(el);
    _bindSlider(el, canvas);
    return el;
  }

  function _bindSlider(el, canvas) {
    el.addEventListener('mousedown', function (e) {
      e.preventDefault();
      var rect = el.getBoundingClientRect();
      if (rect.width <= 0) return;
      var ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      var pct = ratio * 100;
      var start = _dz.start, end = _dz.end;
      var mode;
      var hit = 2.5;   // 手柄命中放宽（%），好抓
      if (Math.abs(pct - start) <= hit) mode = 'resizeL';
      else if (Math.abs(pct - end) <= hit) mode = 'resizeR';
      else if (pct >= start && pct <= end) mode = 'move';
      else {
        // 点击选区外：窗口跳转（保持宽度，中心对齐点击处）
        var w = end - start;
        var ns = pct - w / 2;
        if (ns < 0) ns = 0;
        if (ns + w > 100) ns = 100 - w;
        _dz.start = ns;
        _dz.end = ns + w;
        mode = 'move';
      }
      var dragStartPct = pct;
      var baseStart = _dz.start, baseEnd = _dz.end;

      function onMove(ev) {
        var r2 = el.getBoundingClientRect();
        var p2 = Math.max(0, Math.min(1, (ev.clientX - r2.left) / r2.width)) * 100;
        var delta = p2 - dragStartPct;
        if (mode === 'resizeL') {
          _dz.start = Math.max(0, Math.min(baseStart + delta, baseEnd - 5));
        } else if (mode === 'resizeR') {
          _dz.end = Math.min(100, Math.max(baseEnd + delta, baseStart + 5));
        } else {
          var w2 = baseEnd - baseStart;
          var ns2 = baseStart + delta;
          if (ns2 < 0) ns2 = 0;
          if (ns2 + w2 > 100) ns2 = 100 - w2;
          _dz.start = ns2;
          _dz.end = ns2 + w2;
        }
        _syncDragUI();   // v29：rAF 节流合并渲染，避免 mousemove 高频触发 6 图重绘
      }
      function onUp() {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        _applyZoom();   // v30：松手强制应用最终窗口（防最后奇数帧图表未更新）
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
      _syncDragUI();
    });
    // 画迷你波形
    _drawWave(el, canvas);
  }

  function _drawWave(el, canvas) {
    try {
      var chart = el._chart;
      var opt = chart.getOption();
      var data = (opt.series && opt.series[0] && opt.series[0].data) || [];
      var w = canvas.clientWidth || el.clientWidth || 0;
      var h = canvas.clientHeight || el.clientHeight || 0;
      if (!w || !h || !data.length) return;
      var dpr = window.devicePixelRatio || 1;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      var ctx = canvas.getContext('2d');
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, w, h);
      // 数据归一化画线（跳过空值）
      var vals = [];
      for (var i = 0; i < data.length; i++) {
        var v = data[i];
        if (typeof v === 'number' && isFinite(v)) vals.push(v);
      }
      if (!vals.length) return;
      var lo = Math.min.apply(null, vals);
      var hi = Math.max.apply(null, vals);
      var span = hi - lo || 1;
      ctx.beginPath();
      var first = true;
      for (var j = 0; j < data.length; j++) {
        var vv = data[j];
        if (typeof vv !== 'number' || !isFinite(vv)) { first = true; continue; }
        var x = w * (j / (data.length - 1));
        var y = h - 2 - (vv - lo) / span * (h - 4);
        if (first) { ctx.moveTo(x, y); first = false; }
        else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = 'rgba(79,195,247,0.55)';
      ctx.lineWidth = 1;
      ctx.stroke();
    } catch (e) {}
  }

  // v30 性能优化升级：拖动条 UI 与图表渲染分离频率——
  //   滑块/选区/手柄用 CSS 更新（极廉价），跟随 rAF 每帧(60fps)；
  //   图表缩放重绘较贵，每隔一帧应用一次(~30fps)，观感无差异但重绘负载减半。
  //   松手时强制应用最终窗口，保证最终状态精确。
  var _rafPending = false;
  var _dragFrame = 0;
  function _syncDragUI() {
    if (_rafPending) return;
    _rafPending = true;
    requestAnimationFrame(function () {
      _rafPending = false;
      _renderAllSliders();      // 60fps：CSS 移动，廉价
      _dragFrame++;
      if (_dragFrame % 2 === 1) _applyZoom();   // 30fps：图表重绘，负载减半
    });
  }

  function _renderAllSliders() {
    _dz.sliders.forEach(function (el) {
      if (!el) return;
      var sel = el.querySelector('.pd-selection');
      var hl = el.querySelector('.pd-handle-l');
      var hr = el.querySelector('.pd-handle-r');
      if (!sel || !hl || !hr) return;
      var s = _dz.start, e = _dz.end;
      sel.style.left = s + '%';
      sel.style.width = (e - s) + '%';
      hl.style.left = s + '%';
      hr.style.left = e + '%';
    });
  }

  function _applyZoom() {
    _dz.charts.forEach(function (c) {
      try {
        // animation:false → 缩放无过渡动画，拖动即时生效不滞后（v29）
        c.dispatchAction({ type: 'dataZoom', dataZoomIndex: 0,
                           start: _dz.start, end: _dz.end, animation: false });
      } catch (e) {}
    });
  }

  function clearTimeSliders() {
    _dz.sliders.forEach(function (s) { if (s && s.parentNode) s.parentNode.removeChild(s); });
    _dz.sliders = [];
  }

  function clean(arr) {
    return arr.filter(function (v) { return typeof v === 'number' && isFinite(v); });
  }

  function avg(arr) { var a = clean(arr); return a.length ? a.reduce(function (s, v) { return s + v; }, 0) / a.length : null; }
  function min(arr) { var a = clean(arr); return a.length ? Math.min.apply(null, a) : null; }
  function max(arr) { var a = clean(arr); return a.length ? Math.max.apply(null, a) : null; }
  function p95(arr) {
    var a = clean(arr).slice().sort(function (x, y) { return x - y; });
    if (!a.length) return null;
    var idx = Math.ceil(a.length * 0.95) - 1;
    return a[Math.max(0, Math.min(idx, a.length - 1))];
  }

  // v41：把 jsonl 原始行清洗为"纯采样点"并抽出 meta 行的核数。
  // meta / target_switch 等 event 行（无 t_ms、无 fps/cpu 字段）不参与绘图与统计，
  // 否则会在 x 轴塞进 NaN 类目、污染帧/CPU 等系列；核数从 {"event":"meta","cores":N} 提取。
  // 返回 { rows: [...], cores: <number|null> }；cores 已同步 setCores。
  function prepareRows(raw) {
    var rows = [], cores = null;
    (raw || []).forEach(function (r) {
      if (!r || typeof r !== 'object') return;
      if (r.event) {
        if (r.event === 'meta' && r.cores) {
          var c = parseInt(r.cores, 10);
          if (isFinite(c) && c > 0) cores = c;
        }
        return;   // event 行（meta / target_switch）不当作采样点
      }
      rows.push(r);
    });
    setCores(cores);   // 无论是否读到都重置：避免切到无 meta 行的报告时沿用上一份的核数
    return { rows: rows, cores: cores };
  }

  function statText(arr, unit, digits) {
    var d = digits || 1;
    var a = clean(arr);
    if (!a.length) return '无数据';
    // 顺序：平均 → 最高 → 最低 → P95（最高/峰值最利于找优化方向）
    var parts = ['平均 ' + avg(a).toFixed(d) + unit,
                 '最高 ' + max(a).toFixed(d) + unit];
    if (a.length > 1) parts.push('最低 ' + min(a).toFixed(d) + unit,
                                 'P95 ' + p95(a).toFixed(d) + unit);
    return parts.join(' · ');
  }

  function timeAxis(rows) {
    return rows.map(function (r) { return r.t_ms != null ? Math.round(r.t_ms / 100) / 10 : null; });
  }
  function series(rows, getter) { return rows.map(getter); }

  var baseLine = { type: 'line', showSymbol: false, connectNulls: true,
                   lineStyle: { width: 1.6 }, sampling: 'lttb' };

  // v41：集中颜色映射——series 顶层 color（决定 legend 图标色 + tooltip marker 色）
  // 与 lineStyle.color（决定曲线色）必须取同一值，否则会出现"legend 图标一个色、
  // 曲线另一个色、白线 tooltip marker 又一个色"的错位。取值以既有 lineStyle.color 为准，
  // 不改变曲线本身颜色。
  var COLORS = {
    fps: '#4fc3f7', jank: '#ff8a65',
    p50: '#80deea', p95: '#ffab40', max: '#ef5350',
    cpu_total: '#81c784', cpu_proc: '#ffd54f', cpu_proc_of_total: '#b39ddb',
    pss: '#ba68c8', rss: '#90a4ae',
    rx: '#4dd0e1', tx: '#f06292',
    temp: '#ff7043', power: '#aed581',
  };

  // 核数（cpu_proc_pct ÷ 核数 = 进程占整机%）。实时看板从 /api/status 注入；
  // 历史报告从 jsonl 的 meta 行（{"event":"meta","cores":N}）读出；未知时 null，
  // 此时"进程占整机%"曲线不渲染（renderCpu 内判断）。
  var _cores = null;
  function setCores(n) {
    var v = parseInt(n, 10);
    _cores = (typeof v === 'number' && isFinite(v) && v > 0) ? v : null;
  }

  function baseOption(zoom) {
    var opt = {
      animation: false,
      // v29：禁用缩放/更新的过渡动画（dispatchAction dataZoom 也带 transition），
      // 拖动时间条时图表即时更新、无动画滞后感
      animationDurationUpdate: 0,
      // 绘图区尽量占满：left 给 Y 轴数字+单位，right 保持紧凑
      // bottom 在有时间滑动条时让出空间给 slider
      grid: { left: 56, right: 24, top: 34, bottom: 28 },
      tooltip: { trigger: 'axis', confine: true,
        valueFormatter: function (v) { return (typeof v === 'number') ? v.toFixed(2) : v; } },
      xAxis: { type: 'category', name: '秒', nameTextStyle: { fontSize: 10 }, axisLabel: { fontSize: 10 } },
      yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 10 } },
      // 图例移到右上角（grid 上方），避免与左侧 Y 轴单位重叠。
      // 颜色（2026-08-14）：选中=纯白实色（深底最醒目），未选中=35% 半透明弱化，
      // 修复"选中时与背景糊、未选中反而白色显眼"的倒置对比。
      legend: {
        top: 4, right: 8, left: 'auto', itemWidth: 14,
        textStyle: { fontSize: 11, color: '#ffffff' },
        inactiveColor: 'rgba(255,255,255,0.35)',
      },
    };

    // v28：不再使用 ECharts 自带 slider（交互反人类），改用自定义 HTML 拖动条。
    // 此处只保留一个无 UI 的 inside dataZoom 作为"缩放状态容器"，
    // 自定义拖动条通过 dispatchAction 控制它的 start/end → 图表缩放。
    if (zoom) {
      opt.dataZoom = [
        { type: 'inside', xAxisIndex: 0,
          zoomOnMouseWheel: false, moveOnMouseWheel: false,
          moveOnMouseMove: false, zoomOnMouseMove: false,
          start: 0, end: 100 },
      ];
    }
    return opt;
  }

  function applyTime(charts, rows, elIds) {
    var times = timeAxis(rows);
    elIds.forEach(function (id) {
      var c = charts[id];
      if (!c) return;
      var xo = {
        xAxis: {
          type: 'category', data: times, name: '秒',
          nameTextStyle: { fontSize: 10 },
          // 刻度精度自适应（2026-08-14）：
          // 未缩放 / 大范围 → 整数秒；拖动底部时间条放大后 → 采集最大精度（0.1s）
          axisLabel: {
            fontSize: 10,
            formatter: function (val) {
              var span = null;
              try {
                var dz = c.getOption().dataZoom;
                if (dz && dz[0] && typeof dz[0].endValue === 'number') {
                  span = dz[0].endValue - dz[0].startValue;
                }
              } catch (e) {}
              if (span === null || span >= 40) return Math.round(val) + '';
              return (Math.round(val * 10) / 10) + '';
            },
          },
        },
      };
      c.setOption(xo);
    });
  }

  // ---------------- 各指标渲染 ----------------
  function renderFps(chart, rows, zoom) {
    var fps = series(rows, function (r) { return r.fps ? r.fps.fps : null; });
    var jank = series(rows, function (r) {
      return r.fps && r.fps.jank_rate != null ? r.fps.jank_rate * 100 : null;
    });
    // FPS y 轴上限 = 实际数据最高帧率向上取 20 的倍数（不设 120 地板）：
    // 设备能跑多少就显示多少——60Hz 划到 60，120Hz 划到 120，144Hz 划到 160，更高同理
    var top = max(fps) || 60;
    var maxFps = Math.max(30, Math.ceil(top / 20) * 20);
    var step = Math.round(maxFps / 4 / 5) * 5 || 10;
    chart.setOption({
      ...baseOption(zoom),
      series: [
        Object.assign({}, baseLine, { name: 'FPS', data: fps, yAxisIndex: 0, color: COLORS.fps, lineStyle: { width: 1.6, color: COLORS.fps } }),
        Object.assign({}, baseLine, { name: 'Jank%', data: jank, yAxisIndex: 1, color: COLORS.jank, lineStyle: { width: 1.2, color: COLORS.jank } }),
      ],
      yAxis: [
        { type: 'value', min: 0, max: maxFps, interval: step, axisLabel: { fontSize: 10 } },
        { type: 'value', name: 'Jank%', nameLocation: 'middle', nameGap: 36,
          min: 0, max: 100, axisLabel: { fontSize: 10 }, splitLine: { show: false } },
      ],
    });
  }

  function renderFrameTime(chart, rows, zoom) {
    var p50 = series(rows, function (r) { return r.fps ? r.fps.frame_p50_ms : null; });
    var p95 = series(rows, function (r) { return r.fps ? r.fps.frame_p95_ms : null; });
    var mx = series(rows, function (r) { return r.fps ? r.fps.frame_max_ms : null; });
    chart.setOption({
      ...baseOption(zoom),
      yAxis: { type: 'value', name: 'ms', nameLocation: 'middle', nameGap: 36,
               min: 0, axisLabel: { fontSize: 10 } },
      series: [
        Object.assign({}, baseLine, { name: 'P50', data: p50, color: COLORS.p50, lineStyle: { width: 1.4, color: COLORS.p50 } }),
        Object.assign({}, baseLine, { name: 'P95', data: p95, color: COLORS.p95, lineStyle: { width: 1.6, color: COLORS.p95 } }),
        Object.assign({}, baseLine, { name: 'Max', data: mx, color: COLORS.max, lineStyle: { width: 1.2, color: COLORS.max } }),
      ],
    });
  }

  function renderCpu(chart, rows, zoom) {
    var total = series(rows, function (r) { return r.cpu ? r.cpu.cpu_total_pct : null; });
    var proc = series(rows, function (r) { return r.cpu ? r.cpu.cpu_proc_pct : null; });
    // v41：进程占整机% = cpu_proc_pct ÷ 核数。核数从 /api/status（实时）或 jsonl
    // meta 行（历史）取得；未知时不渲染该曲线（setCores 未注入 / meta 缺失）。
    var procOfTotal = _cores
      ? series(rows, function (r) {
          var p = r.cpu ? r.cpu.cpu_proc_pct : null;
          return (typeof p === 'number' && isFinite(p)) ? Math.round(p / _cores * 100) / 100 : null;
        })
      : null;
    var seriesList = [
      Object.assign({}, baseLine, { name: '整机%', data: total, color: COLORS.cpu_total, lineStyle: { width: 1.6, color: COLORS.cpu_total } }),
      Object.assign({}, baseLine, { name: '进程%', data: proc, color: COLORS.cpu_proc, lineStyle: { width: 1.6, color: COLORS.cpu_proc } }),
    ];
    if (procOfTotal) {
      seriesList.push(Object.assign({}, baseLine, {
        name: '进程占整机%', data: procOfTotal, color: COLORS.cpu_proc_of_total,
        lineStyle: { width: 1.4, color: COLORS.cpu_proc_of_total },
      }));
    }
    chart.setOption({
      ...baseOption(zoom),
      yAxis: { type: 'value', name: '%', nameLocation: 'middle', nameGap: 36,
               min: 0, axisLabel: { fontSize: 10 } },
      series: seriesList,
    });
  }

  function renderMem(chart, rows, zoom) {
    var pss = series(rows, function (r) {
      return r.mem && r.mem.pss_kb != null ? Math.round(r.mem.pss_kb / 1024 * 10) / 10 : null;
    });
    var rss = series(rows, function (r) {
      return r.mem && r.mem.vmrss_kb != null ? Math.round(r.mem.vmrss_kb / 1024 * 10) / 10 : null;
    });
    chart.setOption({
      ...baseOption(zoom),
      yAxis: { type: 'value', name: 'MB', nameLocation: 'middle', nameGap: 36,
               min: 0, axisLabel: { fontSize: 10 } },
      series: [
        Object.assign({}, baseLine, { name: 'PSS MB', data: pss, color: COLORS.pss, lineStyle: { width: 1.6, color: COLORS.pss } }),
        Object.assign({}, baseLine, { name: 'RSS MB', data: rss, color: COLORS.rss, lineStyle: { width: 1.2, color: COLORS.rss } }),
      ],
    });
  }

  function renderNet(chart, rows, zoom) {
    var rx = series(rows, function (r) { return r.net ? r.net.rx_kbps : null; });
    var tx = series(rows, function (r) { return r.net ? r.net.tx_kbps : null; });
    chart.setOption({
      ...baseOption(zoom),
      yAxis: { type: 'value', name: 'KB/s', nameLocation: 'middle', nameGap: 36,
               min: 0, axisLabel: { fontSize: 10 } },
      series: [
        Object.assign({}, baseLine, { name: '下行↓', data: rx, color: COLORS.rx, lineStyle: { width: 1.6, color: COLORS.rx } }),
        Object.assign({}, baseLine, { name: '上行↑', data: tx, color: COLORS.tx, lineStyle: { width: 1.6, color: COLORS.tx } }),
      ],
    });
  }

  function renderTemp(chart, rows, zoom) {
    var temp = series(rows, function (r) { return r.therm ? r.therm.temp_c : null; });
    var power = series(rows, function (r) { return r.therm ? r.therm.power_w : null; });
    chart.setOption({
      ...baseOption(zoom),
      yAxis: [
        { type: 'value', name: '°C', nameLocation: 'middle', nameGap: 36,
          min: 25, axisLabel: { fontSize: 10 } },
        { type: 'value', name: 'W', nameLocation: 'middle', nameGap: 36,
          min: 0, axisLabel: { fontSize: 10 }, splitLine: { show: false } },
      ],
      series: [
        Object.assign({}, baseLine, { name: '温度°C', data: temp, yAxisIndex: 0, color: COLORS.temp, lineStyle: { width: 1.6, color: COLORS.temp } }),
        Object.assign({}, baseLine, { name: '功率W', data: power, yAxisIndex: 1, color: COLORS.power, lineStyle: { width: 1.2, color: COLORS.power } }),
      ],
    });
  }

  // ---------------- 卡片可见性 ----------------
  function setCardVisible(chartId, visible) {
    var el = document.getElementById(chartId);
    if (!el || !el.parentElement) return;
    el.parentElement.style.display = visible ? '' : 'none';
  }

  function hasData(rows, getter) {
    return rows.some(function (r) { var v = getter(r); return typeof v === 'number' && isFinite(v); });
  }

  function renderAll(charts, rows, opts) {
    var zoom = !!(opts && opts.zoom);
    if (!rows.length) return;
    // 无数据指标自动隐藏对应卡片。
    // hasData 用 typeof number 判断（合法 0 值——静止 FPS/空载 CPU——不会隐藏卡片；
    // 2026-08-21 复核：getter 显式返回 null 代替 && 短路，语义等价且更清晰）
    var fpsVisible = hasData(rows, function (r) { return r.fps ? r.fps.fps : null; });
    var frameVisible = hasData(rows, function (r) { return r.fps ? r.fps.frame_p95_ms : null; });
    var cpuVisible = hasData(rows, function (r) { return r.cpu ? r.cpu.cpu_total_pct : null; });
    var memVisible = hasData(rows, function (r) { return r.mem ? r.mem.pss_kb : null; });
    var netVisible = hasData(rows, function (r) { return r.net ? r.net.rx_kbps : null; });
    var tempVisible = hasData(rows, function (r) { return r.therm ? r.therm.temp_c : null; });

    setCardVisible('chart-fps', fpsVisible);
    setCardVisible('chart-frametime', frameVisible);
    setCardVisible('chart-cpu', cpuVisible);
    setCardVisible('chart-mem', memVisible);
    setCardVisible('chart-net', netVisible);
    setCardVisible('chart-temp', tempVisible);

    if (fpsVisible && charts.fps) renderFps(charts.fps, rows, zoom);
    if (frameVisible && charts.frametime) renderFrameTime(charts.frametime, rows, zoom);
    if (cpuVisible && charts.cpu) renderCpu(charts.cpu, rows, zoom);
    if (memVisible && charts.mem) renderMem(charts.mem, rows, zoom);
    if (netVisible && charts.net) renderNet(charts.net, rows, zoom);
    if (tempVisible && charts.temp) renderTemp(charts.temp, rows, zoom);

    applyTime(charts, rows, ['fps', 'frametime', 'cpu', 'mem', 'net', 'temp']);

    // 关键：渲染后强制 resize，按当前容器实际宽度铺满（容器从隐藏转显示 / 窗口变化时
    // 若不 resize，echarts 会沿用旧宽度导致曲线只占左半边、右侧空白）
    ['fps', 'frametime', 'cpu', 'mem', 'net', 'temp'].forEach(function (k) {
      var c = charts[k];
      if (c) c.resize();
    });
  }

  // ---------------- 统计栏 ----------------
  function updateStats(charts, rows) {
    function put(id, text) { var el = document.getElementById(id); if (el) el.textContent = text; }
    put('stat-fps', 'FPS ' + statText(series(rows, function (r) { return r.fps ? r.fps.fps : null; }), ''));
    put('stat-frametime', '帧时间P95 ' + statText(series(rows, function (r) { return r.fps ? r.fps.frame_p95_ms : null; }), 'ms', 1));
    put('stat-cpu', '进程CPU ' + statText(series(rows, function (r) { return r.cpu ? r.cpu.cpu_proc_pct : null; }), '%'));
    put('stat-mem', 'PSS ' + statText(series(rows, function (r) { return r.mem && r.mem.pss_kb != null ? r.mem.pss_kb / 1024 : null; }), ' MB'));
    put('stat-net', '下行 ' + statText(series(rows, function (r) { return r.net ? r.net.rx_kbps : null; }), 'KB/s', 1));
    put('stat-temp', '温度 ' + statText(series(rows, function (r) { return r.therm ? r.therm.temp_c : null; }), '°C', 1));
  }

  // ---------------- 统计汇总 ----------------
  function computeStats(rows) {
    var fps = series(rows, function (r) { return r.fps ? r.fps.fps : null; });
    var jank = series(rows, function (r) {
      return r.fps && r.fps.jank_rate != null ? r.fps.jank_rate * 100 : null;
    });
    var ftP95 = series(rows, function (r) { return r.fps ? r.fps.frame_p95_ms : null; });
    var cpuProc = series(rows, function (r) { return r.cpu ? r.cpu.cpu_proc_pct : null; });
    var pss = series(rows, function (r) {
      return r.mem && r.mem.pss_kb != null ? r.mem.pss_kb / 1024 : null;
    });
    var temp = series(rows, function (r) { return r.therm ? r.therm.temp_c : null; });
    var durS = rows.length ? Math.round((rows[rows.length - 1].t_ms || 0) / 1000) : 0;
    return {
      count: rows.length, durS: durS,
      fps_avg: avg(fps), fps_min: min(fps), fps_p95: p95(fps),
      jank_avg: avg(jank),
      ft_p95_avg: avg(ftP95),
      cpu_avg: avg(cpuProc),
      pss_peak: max(pss), pss_avg: avg(pss),
      temp_avg: avg(temp),
    };
  }

  function renderSummary(elId, stats) {
    var el = document.getElementById(elId);
    if (!el || !stats) return;
    function fmt(v, d) { return (typeof v === 'number' && isFinite(v)) ? Number(v).toFixed(d) : '-'; }
    function item(k, v, small) {
      return '<div class="item"><div class="k">' + k + '</div><div class="v">' + v +
        (small ? '<small> ' + small + '</small>' : '') + '</div></div>';
    }
    el.innerHTML =
      item('平均帧率', fmt(stats.fps_avg, 1), 'FPS') +
      item('最低帧率', fmt(stats.fps_min, 1), 'FPS') +
      item('P95 帧率', fmt(stats.fps_p95, 1), 'FPS') +
      item('卡顿率', fmt(stats.jank_avg, 2), '%') +
      item('帧时间 P95', fmt(stats.ft_p95_avg, 1), 'ms') +
      item('平均进程CPU', fmt(stats.cpu_avg, 1), '%') +
      item('峰值内存', fmt(stats.pss_peak, 1), 'MB (PSS)') +
      item('平均内存', fmt(stats.pss_avg, 1), 'MB (PSS)') +
      item('平均温度', fmt(stats.temp_avg, 1), '°C') +
      item('采集时长', stats.durS + ' s', stats.count + ' 个采样点');
  }

  // ---------------- 事件标注层（2026-08-14 模式1：logcat console.log 叠加） ----------------
  // 把事件按 t_ms 映射到 x 轴类目值，在每张图画竖线标注；
  // 第一张图（FPS）附带文字标签，其余图只画线（避免标签 6 次重复）。
  // v36 修复：类目轴值是采样点时间（rows 的 t_ms 递增，多为整数秒），事件时刻
  // （如 10.4s）未必命中类目 → 此前 Math.round(ev.t_ms/100)/10 映射的类目在
  // ECharts 类目轴中找不到对应值会静默不渲染（大部分事件线丢失）。
  // 改为二分找最近采样点，保证标注线一定落在曲线窗口内。
  // v40：抽成纯函数并导出（window.PerfCharts.nearestCat），便于脱离浏览器做边界断言
  // （见 tests/test_nearest_cat.js）。times 必须是递增的 t_ms 数组。
  // 返回类目轴取值（秒，1 位小数）；times 为空或 ms 无效返回 null。
  function nearestCat(times, ms) {
    if (ms == null || !times || !times.length) return null;
    var cat = function (v) { return Math.round(v / 100) / 10; };
    if (ms <= times[0]) return cat(times[0]);
    if (ms >= times[times.length - 1]) return cat(times[times.length - 1]);
    var lo = 0, hi = times.length - 1;
    while (lo < hi - 1) {
      var mid = (lo + hi) >> 1;
      if (times[mid] <= ms) lo = mid; else hi = mid;
    }
    // 等距时取左侧采样点（与统计栏"落在哪个采样点"口径一致）
    return (ms - times[lo] <= times[hi] - ms) ? cat(times[lo]) : cat(times[hi]);
  }

  function renderEvents(charts, rows, events) {
    if (!events || !events.length || !rows || !rows.length) return;
    var MAX_EV = 200;
    var times = rows.map(function (r) { return r.t_ms; });
    var markers = [];
    events.forEach(function (ev) {
      if (markers.length >= MAX_EV) return;
      if (!ev || ev.t_ms == null) return;
      var cat = nearestCat(times, ev.t_ms);
      if (cat === null) return;
      var isErr = ev.level === 'E' || ev.level === 'F';
      markers.push({
        xAxis: cat,
        lineStyle: {
          color: isErr ? 'rgba(239,83,80,0.75)' : 'rgba(79,195,247,0.55)',
          width: 1, type: 'dashed',
        },
        label: {
          show: true,
          formatter: String(ev.text || '').slice(0, 16),
          fontSize: 9,
          color: isErr ? '#ef5350' : '#4fc3f7',
          position: 'insideEndTop',
        },
      });
    });
    if (!markers.length) return;
    var ids = Object.keys(charts);
    ids.forEach(function (id, idx) {
      var c = charts[id];
      if (!c) return;
      var mk = markers.map(function (m) {
        var o = { xAxis: m.xAxis, lineStyle: m.lineStyle };
        if (idx === 0) o.label = m.label;   // 仅首图带标签
        return o;
      });
      try {
        c.setOption({ series: [{ markLine: { symbol: 'none', silent: true, data: mk } }] });
      } catch (e) {}
    });
  }

  window.PerfCharts = {
    makeChart: makeChart,
    initSortable: initSortable,
    enableLink: enableLink,
    enableClickPin: enableClickPin,
    clearPin: _pinUnlockAll,
    createTimeSliders: createTimeSliders,
    clearTimeSliders: clearTimeSliders,
    renderEvents: renderEvents,
    nearestCat: nearestCat,   // 纯函数，导出供 tests/test_nearest_cat.js 断言
    setCores: setCores,       // 注入核数（实时看板 /api/status；历史报告 meta 行）
    prepareRows: prepareRows, // 清洗 event 行 + 抽取核数（历史报告/导出 HTML 用）
    setPinData: setPinData,
    renderAll: renderAll,
    updateStats: updateStats,
    computeStats: computeStats,
    renderSummary: renderSummary,
    _statText: statText,
  };
})();
