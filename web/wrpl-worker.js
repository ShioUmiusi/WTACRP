/* =====================================================================
 * wrpl-worker.js —— 把 .wrpl 解析放到 Web Worker 里，主线程只收结果
 * ---------------------------------------------------------------------
 * 主线程（wrpl-parse.js 的 parseInWorker）发来的消息：
 *   { cmd: 'ready' }                      → 回 { type: 'ready' }
 *   { cmd: 'parse', buf, fileName, ... }  → 回若干 { type: 'progress' | 'part' | 'debug' }
 *                                           最后 { type: 'done', parts } 或 { type: 'error' }
 * `buf` 是 **Transferable**（主线程 postMessage 时转交所有权）：
 * 主线程那边这个 ArrayBuffer 立刻 detached，主线程堆不背这份数据。
 *
 * 本文件与本目录的 fzstd / wrpl-parse.js 用 importScripts 加载（本机文件，不联网）。
 * 解析结束后主动清引用（大数组置 null 并触发一次 GC 友好的空转），
 * 主线程收到 done 后会 terminate()，整个 isolate 一起回收。
 * ===================================================================== */
'use strict';

importScripts('vendor/fzstd.min.js', 'wrpl-parse.js');

var peakHeap = 0;
function heapNow() {
  // Chromium 的 Worker 里同样有 performance.memory（拿不到就返回 0）
  try {
    return (self.performance && self.performance.memory) ? self.performance.memory.usedJSHeapSize : 0;
  } catch (e) { return 0; }
}

WRPLParse.progress(function (p) {
  var h = heapNow();
  if (h > peakHeap) peakHeap = h;
  self.postMessage({ type: 'progress', phase: p.phase, pct: p.pct, note: p.note || '',
                     workerHeapMB: h ? +(h / 1048576).toFixed(0) : null });
});

/** 按「数值个数」估算体积，切成若干条消息，避免单条 structured clone 太长。 */
function chunkUnits(units, maxVals) {
  var out = [], cur = [], curVals = 0;
  function vals(u) {
    var n = 0;
    for (var i = 0; i < u.x.length; i++) n++;               // 每个数组 n 个元素
    return n * 5;                                           // x/y/z/hdg + 元信息，粗略按 5 列算
  }
  units.forEach(function (u) {
    var v = vals(u);
    if (cur.length && curVals + v > maxVals) { out.push(cur); cur = []; curVals = 0; }
    cur.push(u); curVals += v;
  });
  if (cur.length) out.push(cur);
  return out;
}

function sendOwn(own) {
  // 本机 17 个通道 × 5589 → 拆 4 条，单条 clone 不至于太长
  var groups = [['t0', 'hz', 'n'], ['x', 'y', 'z'], ['ias', 'tas', 'mach', 'hdg'],
                ['roll', 'pitch', 'g', 'aoa', 'ammo', 'fuel', 'vy', 'throttle']];
  groups.forEach(function (keys) {
    var part = {};
    keys.forEach(function (k) { part[k] = own[k]; });
    self.postMessage({ type: 'part', key: 'own', data: part });
  });
}

function sendMission(mission, wantDebug, dbg, timings) {
  var parts = 0, i;
  self.postMessage({ type: 'part', key: 'meta', data: mission.meta }); parts++;
  sendOwn(mission.own); parts += 4;
  var chunks = chunkUnits(mission.units || [], 120000);
  for (i = 0; i < chunks.length; i++) {
    self.postMessage({ type: 'part', key: 'units', data: chunks[i] });
    parts++;
  }
  ['events', 'shots', 'runways', 'airfields'].forEach(function (k) {
    self.postMessage({ type: 'part', key: k, data: mission[k] }); parts++;
  });
  /* 飘带用的横滚序列（可能上百 KB）：按 uid 分片发，避免单条 structured clone 太长 */
  var att = mission.__attitude;
  if (att && att.units) {
    var uids = Object.keys(att.units), buf = {}, cnt = 0;
    var head = { note: att.note, secPerTick: att.secPerTick, tick0: att.tick0, dt: att.dt };
    uids.forEach(function (uid, i) {
      buf[uid] = att.units[uid];
      cnt++;
      if (cnt >= 8 || i === uids.length - 1) {
        var data = {};
        Object.keys(head).forEach(function (k) { data[k] = head[k]; });
        data.units = buf;
        self.postMessage({ type: 'part', key: '__attitude', data: data });
        parts++;
        buf = {}; cnt = 0;
      }
    });
  }
  self.postMessage({ type: 'part', key: 'terrain', data: mission.terrain }); parts++;
  self.postMessage({ type: 'part', key: 'names', data: mission.names }); parts++;
  if (wantDebug && dbg) self.postMessage({ type: 'debug', data: dbg });
  self.postMessage({ type: 'done', parts: parts,
                     timing: { parseMs: timings ? timings.parseMs : null,
                               sendMs: timings && timings.tParsed ? (Date.now() - timings.tParsed) : null },
                     /* worker 里 performance.memory 常常取不到：拿不到就给 null，
                        不要给 0（0 读起来像"堆占用 0 MB"这样的事实陈述） */
                     workerHeapPeakMB: peakHeap ? +(peakHeap / 1048576).toFixed(1) : null });
}

self.onmessage = function (ev) {
  var msg = ev.data || {};
  if (msg.cmd === 'ready') {
    self.postMessage({ type: 'ready', hasFzstd: !!(self.fzstd && self.fzstd.decompress) });
    return;
  }
  if (msg.cmd !== 'parse') return;
  var dbg = msg.wantDebug ? {} : null;
  var opts = { fileName: msg.fileName, yieldEvery: msg.yieldEvery };
  if (dbg) opts.debug = dbg;
  /* 调 parseMain（原始解析）而不是公开的 parse：
   * 公开 parse 会把 __attitude 摘下来挂到 window.ACE_ATTITUDE，而 worker 里没有 window，
   * 结果就再也发不回主线程了（主线程那边 ACE_ATTITUDE 会是 0 个 uid）。 */
  var runner = (WRPLParse._internal && WRPLParse._internal.parseMain) || WRPLParse.parse;
  var tStart = Date.now();
  runner(msg.buf, opts).then(function (mission) {
    var tParsed = Date.now();
    /* 计时随 done 一起回（done 一到主线程就 resolve，单独发的 timing 会晚到、读不到） */
    sendMission(mission, msg.wantDebug, dbg, { parseMs: tParsed - tStart, tParsed: tParsed });
    // 主动清引用：大数组不再被 worker 持有（随后主线程会 terminate 整个 isolate）
    mission = null;
    if (dbg) dbg = null;
    self.setTimeout && self.setTimeout(function () {}, 0);
  }).catch(function (e) {
    self.postMessage({ type: 'error', message: String(e && e.message ? e.message : e),
                       code: e && e.code ? e.code : null, tech: e && e.tech ? e.tech : null,
                       fromWorkerParse: true });
  });
};
