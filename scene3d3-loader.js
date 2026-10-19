/* =====================================================================
 * scene3d3-loader.js —— 经典脚本桥：让 mission.js 继续用 window.ACEScene
 * ---------------------------------------------------------------------
 * three.js 必须以 ES module 加载（import），而 mission.js 是普通 <script>。
 * 做法：先挂一个**代理对象**，把调用排队；模块就绪后把队列回放给真实实现。
 * 代理本身不替换（mission.js 可能缓存了引用），所以 window.ACEScene 永远是代理。
 * ===================================================================== */
(function () {
  'use strict';
  var real = null, queue = [], MAXQ = 64;
  /* 渲染器版本号：改 scene.js 后把它 +1，避免浏览器用缓存的旧模块 */
  /* ⚠ 每次改 three3d/scene.js **必须**把这个数字 +1：它是模块 URL 的版本戳，
   * 不递增的话浏览器会一直用缓存里的旧模块（2026-10-22 踩过：修好的代码在用户那边没生效）。 */
  var NV = '56';

  function dispatch(name, args) {
    if (real) { return real[name].apply(real, args); }
    if (name !== 'render') { queue.push([name, args]); }   /* render 丢帧无所谓 */
    return undefined;
  }

  var API = ['init', 'render', 'setCamera', 'setOption', 'getReady', 'getStats'];
  var proxy = {};
  API.forEach(function (n) {
    proxy[n] = function () { return dispatch(n, arguments); };
  });
  proxy.__isProxy = true;
  window.ACEScene = proxy;

  import('./three3d/scene.js?v=' + NV).then(function () {
    real = window.ACESceneReal || null;
    if (!real) { throw new Error('scene3d3 模块没有导出 ACESceneReal'); }
    /* 回放排队的调用（init 最先） */
    for (var i = 0; i < queue.length; i++) {
      try { real[queue[i][0]].apply(real, queue[i][1]); } catch (e) { console.error('[scene3d3] 队列回放失败', e); }
    }
    queue.length = 0;
    window.__SCENE3D3_READY = true;
  }).catch(function (e) {
    console.error('[scene3d3] 模块加载失败：', e);
    window.__SCENE3D3_ERROR = String(e && e.message || e);
  });
})();
