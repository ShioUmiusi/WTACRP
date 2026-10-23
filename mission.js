/* ============================================================================
 * ACE-COMBAT 极简回放 —— 播放控制与角落读数（契约 v4 §3.3 / §3.3.1）
 *
 * 职责边界：本文件只管时间轴、state 组装、底部控制条、角落读数、键盘、帮助浮层。
 * 画面（黑底网格 / 发光航迹 / 地面单位标 / 击杀位置 / 标签 / 机位）全部由
 * scene3d.js 的 window.ACEScene 负责。这里不画任何场景内容。
 *
 * 硬约束：普通脚本，无 ES module、无 fetch/XHR、无外部资源；file:// 双击可开。
 * 场景或数据缺失时不抛异常：页面照常跑，并在角落明确写出缺什么。
 * ==========================================================================*/
(function () {
  'use strict';

  // ---------- 常量 ----------
  var STEP = 1 / 60;      // 固定步长（秒）
  var MAX_DT = 0.5;       // 单帧最多消费 0.5 s 真实时间（切标签页回来不跳）
  var MAX_STEPS = 400;    // 单帧最多固定步数。卡顿时丢弃余量，绝不死循环
  var CAMERAS = ['follow', 'orbit', 'top', 'free'];
  var SPEEDS = [1, 2, 4, 8];
  var ALT_SCALES = [1, 2, 4];
  var KILL_HOLD = 6;      // 击杀标记交给场景时保留的秒数（场景可自行决定淡出）
  // §2.2 没写 ias 的量纲。data-eng 的交付口径是 SI（meta.units.ias = "m/s"），
  // 所以这里只认 meta.units.ias；拿不到就原样显示数值并提示单位未知，绝不替它猜。
  var IAS_UNIT_FALLBACK = '';
  // 呼号是用户自填的，不来自回放数据（契约 §8.3）。设置存这里，刷新后保留。
  var LS_KEY = 'ace-combat.identity';
  var DEFAULT_OWN = 'ALPHA 01';
  // 默认队友呼号条数 = 回放小队数据（meta.squad，成员来自结算表 squadId）里的**实际队友数**；
  // 读不到小队信息时保守预填 1 个，用户可在齿轮里自行加。
  var CALLSIGN_MAX = 16;

  // ---------- DOM ----------
  var stageEl = document.getElementById('stage');
  var canvasEl = document.getElementById('scene');
  var cornerEl = document.getElementById('corner');
  var controlsEl = document.getElementById('controls');
  var helpEl = document.getElementById('help');

  // ---------- 运行状态 ----------
  var WARN = [];                  // 降级/异常提示，显示在角落
  var DATA = null;                // window.ACE_MISSION
  var META = {};                  // DATA.meta
  var T0 = 0, DUR = 0;            // 任务内秒 0 对应的绝对秒；总时长
  var OWN = null;                 // 本机序列（已校验）
  var UNITS = [];                 // 其他目标序列（已校验）
  var EVENTS = [];                // 事件（已按 t 升序）
  var KILLS = [];                 // kind === 'kill' 的事件（进度条刻度）
  var NAMES = {};
  var SCENE = null, sceneBroken = false, sceneInitError = null;
  var camera = 'follow';
  var options = { altScale: 1, showTrails: true, showLabels: true, showGrid: true,
                  showMissiles: true, showOwnModel: true };
  var met = 0, playing = false, speed = 1;
  var acc = 0, lastTs = 0, frames = 0, rafId = 0;
  var dragging = false;
  var lastOwnRef = { x: 0, y: 0, has: false };   // 本机位置缺失时，接触点仍用最近已知本机位置作参考
  var identity = { own: DEFAULT_OWN, squad: [], sideMode: 'inferred', labelMode: DEFAULT_LABEL_MODE };   // 呼号 + 显示模式（非数据）
  // 阵营方向无法从回放独立闭合（ECS 任务名 / 出生点聚类 / 8111 颜色三方证据都约 50%），
  // 所以给用户一个自校准开关：推定 / 反转 / 未知。
  var SIDE_MODES = ['inferred', 'flipped', 'unknown'];
  var SIDE_MODE_LABEL = { inferred: '推定', flipped: '反转', unknown: '未知' };
  // 地面单位显示：类别（units[].vehicleCategory，数据侧给通用词）/ 具体型号（units[].vehicle）。
  // 只影响地面；空中单位始终显示型号（scene 侧规则）。
  var LABEL_MODES = ['category', 'model'];
  var LABEL_MODE_LABEL = { category: '类别', model: '具体型号' };
  var DEFAULT_LABEL_MODE = 'category';

  var RANKED = [];                // units 按航迹长度降序（仅诊断用，不参与队友绑定）
  var storageOK = true;           // localStorage 是否可用（file:// 下可能被拒）
  var squadKeys = [];             // 当前绑定到的实体 key
  // ---------- 基础工具 ----------
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function isObj(v) { return !!v && typeof v === 'object'; }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
  function pad2(v) { return (v < 10 ? '0' : '') + v; }
  function fmtSec(sec) {           // mm:ss
    if (!isNum(sec) || sec < 0) return '--:--';
    var s = Math.floor(sec + 1e-6);
    return pad2(Math.floor(s / 60)) + ':' + pad2(s % 60);
  }
  function fmtMet(v) {             // mm:ss.s
    if (!isNum(v)) return '--:--.-';
    var s = Math.max(0, v), whole = Math.floor(s);
    return pad2(Math.floor(whole / 60)) + ':' + pad2(whole % 60) + '.' + Math.floor((s - whole) * 10 + 1e-6);
  }
  function fmtNum(v, digits) {
    if (!isNum(v)) return '--';
    var d = digits == null ? 0 : digits;
    var s = v.toFixed(d);
    if (d === 0) {
      var neg = s.charAt(0) === '-';
      var body = neg ? s.slice(1) : s;
      var out = '';
      for (var i = 0; i < body.length; i++) {
        if (i > 0 && (body.length - i) % 3 === 0) out += ' ';
        out += body.charAt(i);
      }
      return (neg ? '-' : '') + out;
    }
    return s;
  }
  function errText(e) { return e && e.message ? String(e.message) : String(e); }
  function warn(msg) { if (WARN.indexOf(msg) < 0) WARN.push(msg); }
  function make(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function setText(node, text) { if (node && node.textContent !== text) node.textContent = text; }

  // ---------- 序列采样 ----------
  function seriesOf(o) {
    if (!isObj(o)) return null;
    if (!isNum(o.t0) || !isNum(o.n) || o.n <= 0) return null;
    if (!isNum(o.hz) || o.hz <= 0) return null;
    if (!o.x || !o.y) return null;
    return o;
  }
  // 线性插值。数组里允许 null（空档）。超出序列时间范围返回 null（不夹到端点，
  // 免得阵亡/未出现的单位被一直挂在最后一点上）。
  function lerpAt(s, t, arr) {
    if (!s || !arr) return null;
    var f = (t - s.t0) * s.hz;
    if (!isNum(f) || f < -0.5 || f > s.n - 0.5) return null;
    var i0 = Math.floor(f), i1, w;
    if (i0 < 0) i0 = 0;
    if (i0 > s.n - 1) i0 = s.n - 1;
    i1 = Math.min(i0 + 1, s.n - 1);
    w = clamp(f - i0, 0, 1);
    if (i1 === i0) w = 0;
    var a = arr[i0], b = arr[i1];
    var an = isNum(a) ? a : null, bn = isNum(b) ? b : null;
    if (an == null && bn == null) return null;
    if (an == null) return bn;
    if (bn == null) return an;
    return an + (bn - an) * w;
  }
  // 角度插值按最短弧，避免 359°→1° 时画面打转
  function angleAt(s, t, arr) {
    var a = lerpAt(s, t, arr);
    if (a == null) return null;
    var f = (t - s.t0) * s.hz;
    if (f < -0.5 || f > s.n - 0.5) return null;
    var i0 = clamp(Math.floor(f), 0, s.n - 1);
    var i1 = Math.min(i0 + 1, s.n - 1);
    var va = arr[i0], vb = arr[i1];
    if (!isNum(va) || !isNum(vb) || i1 === i0) return a;
    var d = vb - va;
    if (d > 180) d -= 360; else if (d < -180) d += 360;
    return va + d * clamp(f - i0, 0, 1);
  }
  // 离散量（弹药等）取最近样本，不做插值
  function nearestAt(s, t, arr) {
    if (!s || !arr) return null;
    var f = (t - s.t0) * s.hz;
    if (!isNum(f) || f < -0.5 || f > s.n - 0.5) return null;
    var i = clamp(Math.round(f), 0, s.n - 1);
    return isNum(arr[i]) ? arr[i] : null;
  }

  // ---------- 数据装载 ----------
  function loadData() {
    if (!isObj(window.ACE_MISSION)) {
      // 两种场景共用这句：双击 index.html 时是缺 mission-data.js；上传页则是还没解析完，
      // 解析完成后调用 ACEUI.mount(data) 即可。
      warn('未载入数据：同目录没有 mission-data.js；上传页可在解析完成后调用 ACEUI.mount(data)');
      return;
    }
    DATA = window.ACE_MISSION;
    META = isObj(DATA.meta) ? DATA.meta : {};
    var win = isObj(META.window) ? META.window : {};
    T0 = isNum(win.t0) ? win.t0 : 0;
    DUR = isNum(win.duration) ? Math.max(0, win.duration)
        : (isNum(win.t1) ? Math.max(0, win.t1 - T0) : 0);
    if (!(DUR > 0)) warn('meta.window.duration 不可用：时间轴长度为 0，只能手动 seek');

    OWN = seriesOf(DATA.own);
    if (!OWN) warn('own 序列缺失或格式不对：本机读数与相对方位无法计算');

    UNITS = [];
    var raw = DATA.units;
    if (raw && raw.length) {
      for (var i = 0; i < raw.length; i++) {
        var u = seriesOf(raw[i]);
        if (u && Array.isArray(u.x) && Array.isArray(u.y)) {
          // 静态点（地面单位）：回放里 n=1、hz=1、static:true，t0 可能被夹到 0。
          // 这种点没有"生存窗口"可言，按采样点插值判存活会让它永远不出现，所以单独标记。
          u.flat = (raw[i]['static'] === true) || (u.n === 1 && u.hz <= 1);
          UNITS.push(u);
        }
      }
      if (UNITS.length !== raw.length) {
        warn('units 里有 ' + (raw.length - UNITS.length) + ' 条格式不对，已跳过');
      }
    } else {
      warn('units 为空：没有他机目标可画');
    }

    EVENTS = Array.isArray(DATA.events) ? DATA.events.slice() : [];
    EVENTS.sort(function (a, b) { return (a && isNum(a.t) ? a.t : 0) - (b && isNum(b.t) ? b.t : 0); });
    KILLS = [];
    for (var k = 0; k < EVENTS.length; k++) {
      if (EVENTS[k] && EVENTS[k].kind === 'kill') KILLS.push(EVENTS[k]);
    }
    NAMES = isObj(DATA.names) ? DATA.names : {};
    buildSquadMemberKeys();      // 队友的真实来源：meta.squad.members[].uid → 实体 key

    if (META.source !== 'wrpl') {
      if (META.source === '8111-dev') {
        warn('开发脚手架数据（meta.source = "8111-dev"）：不是交付来源，仅供开发期自验');
      } else {
        warn('数据来源异常：meta.source = ' + (META.source === undefined ? '缺失' : JSON.stringify(META.source)) + '，契约要求为 "wrpl"');
      }
    }
    if (!(isObj(META.units) && typeof META.units.ias === 'string' && META.units.ias)) {
      warn('meta.units.ias 缺失：空速只显示数值不标单位（不替它猜 km/h 或 m/s）');
    }
  }

  // ---------- 回放小队（meta.squad）：队友的真实来源 ----------
  function squadMembers() {
    var sq = META.squad;
    if (isObj(sq) && Array.isArray(sq.members)) return sq.members;
    if (Array.isArray(sq)) return sq;      // 容错：直接是数组也认
    return null;
  }
  function isSelfMember(m) {
    if (!isObj(m)) return false;
    if (m.self === true || m.isPlayer === true || m.isAuthor === true) return true;
    var p = isObj(META.player) ? META.player : {};
    // userId 才是最可靠的对应：本局存档里成员名是"八幡 海鈴"，而 meta.player.name 是"=LyRe= 八幡 海鈴"，
    // 只比 name 会漏判，把本机也算成队友 → 多一条默认呼号、多一个绿标。
    if (m.userId !== null && m.userId !== undefined && p.userId !== null && p.userId !== undefined &&
        String(m.userId) === String(p.userId)) return true;
    if (m.uid !== null && m.uid !== undefined && p.userId !== null && p.userId !== undefined &&
        String(m.uid) === String(p.userId)) return true;
    if (m.name && p.name && String(m.name) === String(p.name)) return true;
    return false;
  }
  // 本机之外的队友数（不含 uid 是否为 null 的差别：那是"绑不绑得上"的问题，不是"有没有"的问题）
  function teammateSlots() {
    var members = squadMembers();
    if (!members) return 0;
    var n = 0;
    for (var i = 0; i < members.length; i++) if (!isSelfMember(members[i])) n++;
    return n;
  }
  function defaultSquadNames() {
    var n = teammateSlots();
    if (n < 1) n = 1;
    var out = [];
    for (var i = 0; i < n; i++) out.push('ALPHA ' + pad2(i + 2));
    return out;
  }
  // 真实队友 → 实体 key。只用回放小队数据里的 uid（entityId 是同一 id 的别名），
  // uid 为 null / 对不上航迹的一律不绑（宁缺勿错），也不按航迹长度去猜。
  var SQUAD_MEMBER_KEYS = [];      // 每个真实队友的"主航迹" key（与呼号按索引对齐）
  var SQUAD_ALL_KEYS = [];         // 同一 uid 的**所有航迹段**：回放会在断档处把一条航迹切成多段，
                                   // 只绑主段的话队友飞到另一半时间就不绿了。场景若愿意可全用。
  function buildSquadMemberKeys() {
    SQUAD_MEMBER_KEYS = [];
    SQUAD_ALL_KEYS = [];
    var members = squadMembers();
    if (!members) return;
    for (var i = 0; i < members.length; i++) {
      var m = members[i];
      if (!isObj(m) || isSelfMember(m)) continue;
      var uid = (m.uid === null || m.uid === undefined || m.uid === '') ? null : m.uid;
      if (uid === null && !m.entityId) continue;
      var hits = [];
      for (var j = 0; j < UNITS.length; j++) {
        var u = UNITS[j];
        var sameUid = uid !== null && u.uid !== null && u.uid !== undefined && String(u.uid) === String(uid);
        var sameEntity = !!m.entityId && !!u.entityId && String(u.entityId) === String(m.entityId);
        if (sameUid || sameEntity) hits.push(u);
      }
      if (!hits.length) continue;          // 对不上就不绑（宁缺勿错）
      // 主航迹取采样点最多的一段（同一实体，不是拿别人顶），保证标签/绿色覆盖最久
      var primary = hits[0];
      for (var h = 1; h < hits.length; h++) if (hits[h].n > primary.n) primary = hits[h];
      if (SQUAD_MEMBER_KEYS.indexOf(primary.key) < 0) SQUAD_MEMBER_KEYS.push(primary.key);
      for (var a = 0; a < hits.length; a++) {
        if (SQUAD_ALL_KEYS.indexOf(hits[a].key) < 0) SQUAD_ALL_KEYS.push(hits[a].key);
      }
    }
  }
  /* 阵营来源：按回放里实际存在的证据描述，而不是一句笼统的"推定" */
  function sideSourceShort() {
    var air = 0, airSide = 0, ground = 0, groundTeam = 0;
    for (var i = 0; i < UNITS.length; i++) {
      var u = UNITS[i];
      if (u.domain === 'ground') {
        ground++;
        if (u.groundTeam === 1 || u.groundTeam === 2) groundTeam++;
      } else if (u.domain === 'air') {
        air++;
        if (u.side === 'ally' || u.side === 'enemy') airSide++;
      }
    }
    var parts = [];
    if (air) parts.push('空中：' + (airSide ? '击杀关系 + 出生点' : '无证据'));
    if (ground) parts.push('地面：' + (groundTeam === ground && ground > 0 ? 'ECS team' : '按出生中心推断'));
    return parts.length ? parts.join(' · ') : '无单位';
  }

  function squadSourceLine() {
    var mates = teammateSlots();
    if (squadMembers() === null) return '回放没给小队信息（meta.squad 缺失）：队友呼号按友军航迹长度补位，不是回放里的小队。';
    if (mates === 0) return '回放小队里本机没有其他成员：这些呼号按友军航迹长度补位。';
    var miss = mates - SQUAD_MEMBER_KEYS.length;
    var sq = isObj(META.squad) ? META.squad : null;
    var sid = (sq && sq.squadId !== undefined && sq.squadId !== null) ? '（squadId ' + sq.squadId + '）' : '';
    return '小队来自回放结算表' + sid + '：本局 ' + (mates + 1) + ' 人小队（本机 + ' + mates + ' 名队友），' +
      '队友 uid 按回放内击杀/死亡指纹绑定航迹' +
      (miss > 0 ? '；其中 ' + miss + ' 名 uid 未在航迹中命中，不绑定' : '') + '。';
  }

  // ---------- 呼号（用户自填，不来自数据；契约 §8.3） ----------
  function cleanCallsign(s) {
    return String(s == null ? '' : s).replace(/[\r\n\t]/g, ' ').trim().slice(0, CALLSIGN_MAX);
  }
  function parseSquadList(text) {
    var out = [], seen = {};
    String(text == null ? '' : text).split(/[\n,，;；]+/).forEach(function (raw) {
      var s = cleanCallsign(raw);
      if (!s) return;
      var k = s.toUpperCase();
      if (seen[k]) return;
      seen[k] = 1;
      out.push(k);
    });
    return out;
  }
  var hasStoredIdentity = false;
  function loadIdentity() {
    var stored = null;
    hasStoredIdentity = false;
    try {
      var raw = window.localStorage.getItem(LS_KEY);
      if (raw) stored = JSON.parse(raw);
    } catch (e) {
      storageOK = false;
      warn('localStorage 读不到（' + errText(e) + '）：呼号设置只在本会话有效，刷新会回默认');
    }
    if (isObj(stored)) {
      hasStoredIdentity = true;
      var own = cleanCallsign(stored.own);
      identity.own = own ? own.toUpperCase() : DEFAULT_OWN;
      identity.squad = parseSquadList(Array.isArray(stored.squad) ? stored.squad.join('\n') : stored.squad);
      identity.sideMode = SIDE_MODES.indexOf(stored.sideMode) >= 0 ? stored.sideMode : 'inferred';
      // 旧值（如 'model'）按用户设置保留；只有"从没设置过"才用新默认（类别）
      identity.labelMode = LABEL_MODES.indexOf(stored.labelMode) >= 0 ? stored.labelMode : DEFAULT_LABEL_MODE;
    } else {
      identity.own = DEFAULT_OWN;
      identity.squad = defaultSquadNames();
      identity.sideMode = 'inferred';
      identity.labelMode = DEFAULT_LABEL_MODE;
    }
  }
  function saveIdentity() {
    if (!storageOK) return false;
    try {
      window.localStorage.setItem(LS_KEY, JSON.stringify({ own: identity.own, squad: identity.squad, sideMode: identity.sideMode, labelMode: identity.labelMode, v: 3 }));
      return true;
    } catch (e) {
      storageOK = false;
      warn('localStorage 写不进去（' + errText(e) + '）：设置只在本会话有效');
      return false;
    }
  }
  // "航迹长度" = 相邻有效采样点之间水平距离之和（米）。契约 §8.3 要求队友呼号按航迹长度顺序绑定。
  function trackLength(u) {
    var sum = 0, px = null, py = null;
    for (var i = 0; i < u.n; i++) {
      var x = u.x[i], y = u.y[i];
      if (!isNum(x) || !isNum(y)) { px = null; py = null; continue; }
      if (px !== null) sum += Math.sqrt((x - px) * (x - px) + (y - py) * (y - py));
      px = x; py = y;
    }
    return sum;
  }
  function rankUnits() {
    RANKED = [];
    for (var i = 0; i < UNITS.length; i++) {
      RANKED.push({ key: UNITS[i].key, len: trackLength(UNITS[i]), n: UNITS[i].n, side: UNITS[i].side });
    }
    RANKED.sort(function (a, b) {
      return (b.len - a.len) || (b.n - a.n) || (a.key < b.key ? -1 : (a.key > b.key ? 1 : 0));
    });
    // 队友绑定**不再**从航迹排序取。绑定只认 meta.squad 里 uid 对得上的成员（见 resolveSquadKeys）。
    resolveSquadKeys();
  }
  // 呼号按索引对齐下发：squadKeys[i] ↔ squad[i]。只有回放小队里 uid 对得上的成员才会绑到实体；
  // 多写的呼号保留在面板里但**不绑任何单位**（该槽位为 null），绝不按航迹长度补位绑非队友。
  function resolveSquadKeys() {
    var want = identity.squad.length;
    var out = [];
    for (var i = 0; i < want; i++) {
      out.push(i < SQUAD_MEMBER_KEYS.length ? SQUAD_MEMBER_KEYS[i] : null);
    }
    squadKeys = out;
    return out;
  }
  function setIdentity(own, squadText) {
    var o = cleanCallsign(own);
    identity.own = o ? o.toUpperCase() : DEFAULT_OWN;   // 本机留空就回默认，不显示空标签
    identity.squad = parseSquadList(squadText);
    resolveSquadKeys();
    saveIdentity();
    syncIdentityUI();
    renderOnce();
    return { own: identity.own, squad: identity.squad.slice(), squadKeys: squadKeys.slice() };
  }
  function resetIdentity() {
    identity.labelMode = DEFAULT_LABEL_MODE;   // 「恢复默认」也回类别
    return setIdentity(DEFAULT_OWN, defaultSquadNames().join('\n'));
  }
  function setLabelMode(mode) {
    if (LABEL_MODES.indexOf(mode) < 0) return identity.labelMode;
    identity.labelMode = mode;
    saveIdentity();
    syncIdentityUI();
    renderOnce();
    return mode;
  }
  function setSideMode(mode) {
    if (SIDE_MODES.indexOf(mode) < 0) return identity.sideMode;
    identity.sideMode = mode;
    saveIdentity();
    syncIdentityUI();
    renderOnce();
    return mode;
  }

  // ---------- 场景 ----------
  function initScene() {
    SCENE = isObj(window.ACEScene) ? window.ACEScene : null;
    if (!SCENE) {
      warn('缺少 scene3d.js：场景未加载（画面为黑底），播放控制仍可用');
      sizeFallbackCanvas();
      return;
    }
    if (!DATA) {
      warn('没有数据，未调用 ACEScene.init（避免场景空转）');
      return;
    }
    try {
      if (typeof SCENE.init === 'function') SCENE.init(canvasEl, DATA, { camera: camera });
    } catch (e) {
      sceneInitError = errText(e);
      warn('ACEScene.init 抛错：' + sceneInitError + '（已停用场景，控制仍可用）');
      SCENE = null;
      sizeFallbackCanvas();
      return;
    }
    // 注意：setOption 在 scene3d.js 里会"锁定"该键，锁定后 state.options 不再生效。
    // 所以开关状态只走 state.options 这一条路（见 setOption），这里不预置，避免两个来源打架。
    if (typeof SCENE.setCamera === 'function') {
      try { SCENE.setCamera(camera); } catch (e) { warn('ACEScene.setCamera 抛错：' + errText(e)); }
    }
  }

  // 场景缺席时给 canvas 一个真实的像素尺寸，至少不留 300x150 的模糊底
  var fallbackSized = false;
  function sizeFallbackCanvas() {
    if (!canvasEl) return;
    if (fallbackSized) return;
    fallbackSized = true;
    function fit() {
      var w = Math.max(1, window.innerWidth || 1920);
      var h = Math.max(1, window.innerHeight || 1080);
      canvasEl.width = w;
      canvasEl.height = h;
    }
    fit();
    window.addEventListener('resize', fit);
  }

  // ---------- state（契约 §3.3.1） ----------
  function ownAt(t) {
    if (!OWN) return null;
    var x = lerpAt(OWN, t, OWN.x), y = lerpAt(OWN, t, OWN.y);
    if (x == null || y == null) return null;
    var z = lerpAt(OWN, t, OWN.z);
    lastOwnRef.x = x; lastOwnRef.y = y; lastOwnRef.has = true;
    return {
      x: x, y: y, z: z,
      ias: lerpAt(OWN, t, OWN.ias),
      mach: lerpAt(OWN, t, OWN.mach),
      hdg: angleAt(OWN, t, OWN.hdg),
      roll: lerpAt(OWN, t, OWN.roll),
      pitch: lerpAt(OWN, t, OWN.pitch),
      g: lerpAt(OWN, t, OWN.g),
      aoa: lerpAt(OWN, t, OWN.aoa),
      vy: lerpAt(OWN, t, OWN.vy),
      ammo: nearestAt(OWN, t, OWN.ammo),
      fuel: lerpAt(OWN, t, OWN.fuel)
    };
  }

  function recentKills(t) {
    var out = [];
    for (var i = 0; i < KILLS.length; i++) {
      var e = KILLS[i];
      if (!e || !isNum(e.t)) continue;
      var age = t - e.t;
      if (age >= 0 && age <= KILL_HOLD) {
        out.push({
          t: e.t, age: age, x: e.x, y: e.y, z: isNum(e.z) ? e.z : null,
          isPlayerActor: !!e.isPlayerActor, isPlayerTarget: !!e.isPlayerTarget,
          text: e.text, actor: e.actor, target: e.target
        });
      }
    }
    return out;
  }

  function buildState() {
    var t = T0 + met;
    var own = ownAt(t);
    var refX = own ? own.x : lastOwnRef.x;
    var refY = own ? own.y : lastOwnRef.y;
    var contacts = [];
    for (var i = 0; i < UNITS.length; i++) {
      var u = UNITS[i];
      var ux, uy, uz, uhdg;
      if (u.flat) {
        // 静态点：不插值、不推进，只要已经到达它的 t0 就一直在场（t0 被夹到 0 的点全程可见）
        if (t < u.t0 - 1e-9) continue;
        ux = isNum(u.x[0]) ? u.x[0] : null;
        uy = isNum(u.y[0]) ? u.y[0] : null;
        uz = u.z ? (isNum(u.z[0]) ? u.z[0] : null) : null;
        uhdg = u.hdg ? (isNum(u.hdg[0]) ? u.hdg[0] : null) : null;
      } else {
        ux = lerpAt(u, t, u.x); uy = lerpAt(u, t, u.y);
        uz = lerpAt(u, t, u.z); uhdg = angleAt(u, t, u.hdg);
      }
      if (ux == null || uy == null) continue;
      var dx = ux - refX, dy = uy - refY;
      var nm = NAMES && NAMES[u.key];
      contacts.push({
        key: u.key,
        side: u.side || 'unknown',
        domain: u.domain || 'air',
        dx: dx, dy: dy, d: Math.sqrt(dx * dx + dy * dy),
        // 以下为契约之外的附加字段，场景用得上就用，不用也无妨
        x: ux, y: uy, z: uz, hasAlt: !!u.hasAlt,
        hdg: uhdg,
        isStatic: !!u.flat,
        sideDecoded: typeof u.sideDecoded === 'string' ? u.sideDecoded : (u.sideDecoded === undefined ? null : u.sideDecoded),
        name: nm && nm.name ? nm.name : null,
        vehicle: u.vehicle || (nm && nm.vehicle ? nm.vehicle : null)
      });
    }
    return {
      met: met,
      t: t,
      playing: playing,
      speed: speed,
      own: own,
      contacts: contacts,
      ownMissing: !own,
      options: {
        altScale: options.altScale,
        showTrails: options.showTrails,
        showLabels: options.showLabels,
        showGrid: options.showGrid,
        showMissiles: options.showMissiles,
        showOwnModel: options.showOwnModel
      },
      // 附加：场景画击杀标记与扩散环时省得自己再扫事件表
      recentKills: recentKills(t),
      // 契约 §8.4：呼号是用户自填的（非数据），squadKeys 由本文件按航迹长度解析后下发
      identity: {
        own: identity.own,
        squad: identity.squad.slice(),
        // 按索引对齐：squadKeys[i] ↔ squad[i]，未绑定的槽位是 null
        squadKeys: squadKeys.slice(),
        // 契约外附加：真实队友实体的所有航迹段（含主段之外的断档段），场景可用于完整覆盖
        squadAllKeys: SQUAD_ALL_KEYS.slice(),
        // 阵营方向未能独立闭合，场景按这个模式决定用 units[].side 还是取反后的 sideDecoded
        sideMode: identity.sideMode,
        // 型号显示：'model' 用 units[].vehicle，'category' 用 units[].vehicleCategory
        labelMode: identity.labelMode
      }
    };
  }

  // ---------- 时间轴 ----------
  function play() {
    if (!(DUR > 0)) return;
    if (met >= DUR - 1e-9) met = 0;
    playing = true; acc = 0;
    syncControls();
    renderOnce();          // 立刻反映到读数与控制条，不等下一帧
  }
  function pause() { playing = false; acc = 0; syncControls(); renderOnce(); }
  function togglePlay() { if (playing) pause(); else play(); }
  function setSpeed(v) {
    if (SPEEDS.indexOf(v) < 0) return;
    speed = v;
    acc = 0;
    syncControls();
  }
  function seekTo(sec) {
    if (!isNum(sec)) return;
    met = clamp(sec, 0, DUR > 0 ? DUR : 0);
    acc = 0;
    syncControls();
    renderOnce();
  }
  function step(h) {
    if (!(DUR > 0)) { playing = false; met = 0; return; }
    met += h * speed;
    if (met >= DUR) { met = DUR; playing = false; syncControls(); }
  }
  function replay() { met = 0; playing = DUR > 0; acc = 0; lastTs = 0; syncControls(); renderOnce(); }

  // ---------- 渲染循环 ----------
  function frame(ts) {
    rafId = window.requestAnimationFrame(frame);
    frames++;
    if (!lastTs) lastTs = ts;
    var dt = (ts - lastTs) / 1000;
    lastTs = ts;
    if (!isNum(dt) || dt < 0) dt = 0;
    if (dt > MAX_DT) dt = MAX_DT;

    if (playing) {
      acc += dt;
      var steps = 0;
      while (acc >= STEP && steps < MAX_STEPS) { step(STEP); acc -= STEP; steps++; }
      // 终止条件：单帧最多 MAX_STEPS 步；超出的余量直接丢掉（宁可慢一点，不要越积越多）
      if (acc >= STEP) acc = 0;
    } else {
      acc = 0;
    }
    renderOnce();
  }

  function renderOnce() {
    var state = buildState();
    if (SCENE && !sceneBroken) {
      try {
        if (typeof SCENE.render === 'function') SCENE.render(state);
      } catch (e) {
        sceneBroken = true;
        warn('ACEScene.render 抛错，已停用场景渲染：' + errText(e));
      }
    }
    updateCorner(state);
    updateControls(state);
  }

  // ---------- 角落读数 + 右上角齿轮（呼号面板） ----------
  var cv = {};
  function buildCorner() {
    if (!cornerEl) return;
    cornerEl.textContent = '';
    // 齿轮按钮常驻右上角：H 键只隐藏读数块，不隐藏它（否则设置入口就找不回来了）
    var gearRow = make('div', 'ace-gear-row');
    cv.gear = make('button', 'ace-btn ace-gear', '⚙');
    cv.gear.type = 'button';
    cv.gear.title = '呼号设置（本机 / 队友）';
    cv.gear.setAttribute('aria-label', '呼号设置');
    cv.gear.addEventListener('click', function () {
      cv.gear.blur();
      toggleIdentityPanel();
    });
    gearRow.appendChild(cv.gear);
    cornerEl.appendChild(gearRow);

    cv.wrap = make('div', 'ace-readout');       // H 键切换的就是这一块
    cv.rows = make('div', 'ace-rows');
    cv.time = addRow('任务时间');
    cv.alt = addRow('高度');
    cv.ias = addRow('空速');
    cv.status = addRow('提示');
    cv.status.row.classList.add('ace-dim');
    cv.status.row.hidden = true;
    cv.warn = make('div', 'ace-warn');
    cv.warn.hidden = true;
    cv.wrap.appendChild(cv.rows);
    cv.wrap.appendChild(cv.warn);
    cornerEl.appendChild(cv.wrap);
    // 数据来源与验证数字都不上画面；界面只保留可操作信息：
    // 来源/局限/误差全文保留在 ? 帮助里，诚实性不变。

    buildIdentityPanel();

    function addRow(key) {
      var row = make('div', 'ace-row');
      row.appendChild(make('span', 'ace-k', key));
      var val = make('span', 'ace-v', '--');
      row.appendChild(val);
      cv.rows.appendChild(row);
      return { row: row, val: val };
    }
  }

  // 呼号面板：本机呼号 + 队友呼号（每行一个）。存 localStorage，刷新后保留（契约 §8.3）。
  function buildIdentityPanel() {
    var p = make('div', 'ace-panel');
    p.hidden = true;
    cv.panel = p;
    p.appendChild(make('div', 'ace-panel-hd', '呼号与显示'));

    var r1 = make('label', 'ace-panel-row');
    r1.appendChild(make('span', 'ace-panel-k', '本机呼号'));
    cv.ownInput = make('input', 'ace-input');
    cv.ownInput.type = 'text';
    cv.ownInput.maxLength = CALLSIGN_MAX;
    cv.ownInput.spellcheck = false;
    cv.ownInput.value = identity.own;
    cv.ownInput.setAttribute('aria-label', '本机呼号');
    r1.appendChild(cv.ownInput);
    p.appendChild(r1);

    /* 呼号下一行：本机机体型号（显示在 3D 呼号的第二行） */
    var r1b = make('div', 'ace-panel-row ace-panel-modes');
    r1b.appendChild(make('span', 'ace-panel-k', '呼号下型号'));
    cv.ownModelBtn = make('button', 'ace-btn ace-mode', options.showOwnModel ? '显示' : '隐藏');
    cv.ownModelBtn.type = 'button';
    cv.ownModelBtn.addEventListener('click', function () {
      cv.ownModelBtn.blur();
      setOption('showOwnModel', !options.showOwnModel);
      cv.ownModelBtn.textContent = options.showOwnModel ? '显示' : '隐藏';
      cv.ownModelBtn.classList.toggle('on', options.showOwnModel);
    });
    cv.ownModelBtn.classList.toggle('on', options.showOwnModel);
    r1b.appendChild(cv.ownModelBtn);
    p.appendChild(r1b);

    var r2 = make('label', 'ace-panel-row ace-panel-col');
    r2.appendChild(make('span', 'ace-panel-k', '队友呼号（每行一个）'));
    cv.squadArea = make('textarea', 'ace-input ace-area');
    cv.squadArea.rows = 3;
    cv.squadArea.spellcheck = false;
    cv.squadArea.value = identity.squad.join('\n');
    cv.squadArea.setAttribute('aria-label', '队友呼号，每行一个');
    r2.appendChild(cv.squadArea);
    p.appendChild(r2);

    cv.squadNote = make('div', 'ace-panel-note ace-panel-bind');
    p.appendChild(cv.squadNote);
    cv.bindList = make('div', 'ace-panel-note ace-panel-bind');
    p.appendChild(cv.bindList);
    cv.bindNote = make('div', 'ace-panel-note ace-panel-bind');
    p.appendChild(cv.bindNote);

    /* 阵营来源：只读；按回放里实际可用的证据描述（空中=击杀关系/出生点，地面=ECS team/出生中心） */
    var r3 = make('div', 'ace-panel-row');
    r3.appendChild(make('span', 'ace-panel-k', '阵营来源'));
    var sideVal = make('span', 'ace-panel-val', sideSourceShort());
    if (META.sideSource) sideVal.title = String(META.sideSource);
    r3.appendChild(sideVal);
    cv.sideBtns = [];
    p.appendChild(r3);

    /* 地面单位显示：类别 / 具体型号 */
    var r4 = make('div', 'ace-panel-row ace-panel-modes');
    r4.appendChild(make('span', 'ace-panel-k', '地面单位显示'));
    cv.labelBtns = [];
    for (var li = 0; li < LABEL_MODES.length; li++) {
      (function (mode) {
        var b = make('button', 'ace-btn ace-mode', LABEL_MODE_LABEL[mode]);
        b.type = 'button';
        b.dataset.labelmode = mode;
        b.addEventListener('click', function () { b.blur(); setLabelMode(mode); });
        r4.appendChild(b);
        cv.labelBtns.push(b);
      })(LABEL_MODES[li]);
    }
    p.appendChild(r4);

    /* 导弹弹道：白色虚线（弹体不画） */
    var r5 = make('div', 'ace-panel-row ace-panel-modes');
    r5.appendChild(make('span', 'ace-panel-k', '导弹弹道'));
    cv.missileBtn = make('button', 'ace-btn ace-mode', options.showMissiles ? '显示' : '隐藏');
    cv.missileBtn.type = 'button';
    cv.missileBtn.addEventListener('click', function () {
      cv.missileBtn.blur();
      setOption('showMissiles', !options.showMissiles);
      cv.missileBtn.textContent = options.showMissiles ? '显示' : '隐藏';
      cv.missileBtn.classList.toggle('on', options.showMissiles);
    });
    cv.missileBtn.classList.toggle('on', options.showMissiles);
    r5.appendChild(cv.missileBtn);
    p.appendChild(r5);

    /* 调试（临时）：只保留自由镜头 */
    p.appendChild(make('div', 'ace-panel-hd ace-panel-sub', '调试'));
    var dbg = make('div', 'ace-debug');
    var bf = make('button', 'ace-btn', '自由镜头');
    bf.type = 'button';
    bf.addEventListener('click', function () {
      bf.blur();
      var cur = 'follow';
      try { if (SCENE && SCENE.getStats) { cur = SCENE.getStats().camMode || 'follow'; } } catch (e) {}
      var next = (cur === 'free') ? 'follow' : 'free';
      try { if (SCENE && SCENE.setCamera) SCENE.setCamera(next); } catch (e) {}
      bf.classList.toggle('on', next === 'free');
    });
    dbg.appendChild(bf);
    p.appendChild(dbg);

    var btns = make('div', 'ace-panel-btns');
    cv.resetBtn = make('button', 'ace-btn', '恢复默认');
    cv.resetBtn.type = 'button';
    cv.resetBtn.addEventListener('click', function () {
      cv.resetBtn.blur();
      resetIdentity();
      if (cv.ownInput) cv.ownInput.value = identity.own;
      if (cv.squadArea) cv.squadArea.value = defaultSquadNames().join('\n');
    });
    cv.closeBtn = make('button', 'ace-btn', '关闭');
    cv.closeBtn.type = 'button';
    cv.closeBtn.addEventListener('click', function () {
      cv.closeBtn.blur();
      toggleIdentityPanel(false);
    });
    btns.appendChild(cv.resetBtn);
    btns.appendChild(cv.closeBtn);
    p.appendChild(btns);

    cv.ownInput.addEventListener('input', function () {
      setIdentity(cv.ownInput.value, cv.squadArea.value);
    });
    cv.squadArea.addEventListener('input', function () {
      setIdentity(cv.ownInput.value, cv.squadArea.value);
    });
    cv.ownInput.addEventListener('keydown', function (ev) { ev.stopPropagation(); });
    cv.squadArea.addEventListener('keydown', function (ev) { ev.stopPropagation(); });
    cornerEl.appendChild(p);
    syncIdentityUI();
  }

  function toggleIdentityPanel(force) {
    if (!cv.panel) return;
    var show = typeof force === 'boolean' ? force : cv.panel.hidden;
    cv.panel.hidden = !show;
    if (show) {
      syncIdentityUI();
      if (cv.ownInput && cv.ownInput.focus) cv.ownInput.focus();
    }
  }

  function syncIdentityUI() {
    if (!cv.bindNote) return;
    var n = identity.squad.length, bound = 0, i;
    for (i = 0; i < squadKeys.length; i++) if (squadKeys[i]) bound++;
    var mates = teammateSlots();
    var msg = '已绑定 ' + bound + ' / ' + n + ' 个呼号';
    if (mates) msg += ' · 回放小队 ' + mates + ' 名，uid 直接命中 ' + SQUAD_MEMBER_KEYS.length + ' 名';
    else if (n) msg += ' · 回放没给小队信息，这些呼号暂不绑定任何单位';
    if (bound < n) msg += '；未绑定的呼号不会画成队友';
    if (!storageOK) msg += ' · localStorage 不可用，本次设置刷新后不保留';
    setText(cv.bindNote, msg);
    if (cv.squadNote) setText(cv.squadNote, squadSourceLine());
    if (cv.bindList) {
      var parts = [];
      for (i = 0; i < n; i++) parts.push(identity.squad[i] + (squadKeys[i] ? ' → ' + squadKeys[i] : ' → 未绑定'));
      setText(cv.bindList, parts.join(' · '));
    }
    if (cv.sideBtns) {
      for (var i = 0; i < cv.sideBtns.length; i++) {
        cv.sideBtns[i].classList.toggle('is-on', cv.sideBtns[i].dataset.mode === identity.sideMode);
      }
    }
    if (cv.labelBtns) {
      for (var l = 0; l < cv.labelBtns.length; l++) {
        cv.labelBtns[l].classList.toggle('is-on', cv.labelBtns[l].dataset.labelmode === identity.labelMode);
      }
    }
  }

  function sourceLine() {
    if (!DATA) return '数据来源：未加载 mission-data.js，画面为空';
    var file = META.replayFile ? String(META.replayFile) : '(未写 replayFile)';
    var limits = Array.isArray(META.limits) ? META.limits : [];
    var line;
    if (META.source === 'wrpl') {
      line = '数据来源：全部来自回放 ' + file;
    } else if (META.source === '8111-dev') {
      line = '数据来源：开发脚手架（8111-dev），不是交付来源';
    } else {
      line = '数据来源异常：meta.source = ' +
        (META.source === undefined ? '缺失' : JSON.stringify(META.source)) + '（契约要求 wrpl）';
    }
    if (limits.length) line += ' · 局限 ' + limits.length + ' 条（按 ? 看全文）';
    return line;
  }

  // 开发期验证只报数字与来源说明（契约 v4 §1.2：不许出现 8111 坐标）
  // 完整 note 放到 ? 帮助里，角落只留一行数字，免得读数变成信息面板。
  function devValidationLine() {
    var dv = META.devValidation;
    if (!isObj(dv)) return '';
    var bits = [];
    if (isNum(dv.ownMaxErrM)) bits.push('max ' + fmtNum(dv.ownMaxErrM, 1) + ' m');
    if (isNum(dv.ownRmsErrM)) bits.push('rms ' + fmtNum(dv.ownRmsErrM, 1) + ' m');
    if (isNum(dv.unitAssocRate)) {
      var r = dv.unitAssocRate;
      bits.push('关联 ' + (r <= 1 ? Math.round(r * 100) + '%' : fmtNum(r, 1)));
    }
    if (isNum(dv.speedViolations)) bits.push('越界 ' + dv.speedViolations);
    if (!bits.length) return '';
    return '对照 8111：' + bits.join(' / ');
  }

  function iasUnit() {
    var u = META.units;
    if (isObj(u) && typeof u.ias === 'string' && u.ias) return u.ias;
    return IAS_UNIT_FALLBACK;
  }

  function updateCorner(state) {
    if (!cv.rows) return;
    var own = state.own;
    setText(cv.time.val, 'T+' + fmtMet(state.met) + ' / ' + fmtSec(DUR));
    setText(cv.alt.val, own && isNum(own.z) ? fmtNum(own.z, 0) + ' m' : '-- m');
    var speedTxt = '--';
    if (own && isNum(own.ias)) speedTxt = fmtNum(own.ias, 0) + (iasUnit() ? ' ' + iasUnit() : '');
    if (own && isNum(own.mach)) speedTxt += '  M ' + own.mach.toFixed(2);
    if (own && !isNum(own.ias) && !isNum(own.mach)) speedTxt = '--（回放未给空速）';
    setText(cv.ias.val, speedTxt);
    // 角落不摆调试信息：只在"竖向放大不是 1 倍"时提示一句（契约 §4 要求倍数可见）
    var hints = [];
    if (options.altScale !== 1) hints.push('高度 ×' + options.altScale + '（视觉放大，非真实比例）');
    if (cv.status) {
      cv.status.row.hidden = hints.length === 0;
      if (hints.length) setText(cv.status.val, hints.join(' · '));
    }
    if (WARN.length) {
      cv.warn.hidden = false;
      var txt = WARN.join('\n');
      setText(cv.warn, txt);
    } else {
      cv.warn.hidden = true;
    }
  }
  function onOff(v) { return v ? '开' : '关'; }

  // ---------- 控制条 ----------
  var ct = {};
  // 按钮点完就 blur：焦点留在按钮上时按空格，浏览器会再"激活"一次按钮，
  // 叠加全局空格=播放/暂停就变成按一下动两次。blur 掉就能保证空格只走一条路径。
  function btn(label, cls, title, onClick) {
    var b = make('button', 'ace-btn' + (cls ? ' ' + cls : ''), label);
    b.type = 'button';
    if (title) b.title = title;
    b.addEventListener('click', function () {
      b.blur();
      onClick();
    });
    return b;
  }
  function buildControls() {
    if (!controlsEl) return;
    controlsEl.textContent = '';
    var bar = make('div', 'ace-bar');

    ct.play = btn('', 'ace-play', '播放 / 暂停（空格）', function () { togglePlay(); });
    ct.play.setAttribute('aria-label', '播放或暂停');
    bar.appendChild(ct.play);

    var speeds = make('div', 'ace-speeds');
    ct.speeds = [];
    for (var i = 0; i < SPEEDS.length; i++) {
      (function (v) {
        var b = btn(v + '×', 'ace-speed', '倍速 ' + v + '×（按 ' + v + '）', function () { setSpeed(v); });
        b.dataset.speed = String(v);
        speeds.appendChild(b);
        ct.speeds.push(b);
      })(SPEEDS[i]);
    }
    bar.appendChild(speeds);

    ct.track = make('div', 'ace-track');
    ct.track.setAttribute('role', 'slider');
    ct.track.setAttribute('tabindex', '0');
    ct.track.setAttribute('aria-label', '播放进度，点击或拖动可跳转');
    ct.track.appendChild(make('div', 'ace-rail'));
    ct.fill = make('div', 'ace-fill');
    ct.track.appendChild(ct.fill);
    ct.ticks = make('div', 'ace-ticks');
    ct.track.appendChild(ct.ticks);
    ct.knob = make('div', 'ace-knob');
    ct.track.appendChild(ct.knob);
    bindSeek(ct.track);
    bar.appendChild(ct.track);

    ct.time = make('div', 'ace-time', 'T+00:00.0 / 00:00');
    bar.appendChild(ct.time);


    ct.replay = btn('⟲', 'ace-replay', '重播（R）', function () { replay(); });
    ct.replay.setAttribute('aria-label', '重播');
    bar.appendChild(ct.replay);

    ct.diag = make('div', 'ace-diag');
    bar.appendChild(ct.diag);

    bar.appendChild(make('div', 'ace-hint', '空格 播放 · ←/→ 10s · ? 帮助'));

    controlsEl.appendChild(bar);
    buildTicks();
    syncControls();
  }

  function buildTicks() {
    if (!ct.ticks) return;
    ct.ticks.textContent = '';
    for (var i = 0; i < KILLS.length; i++) {
      var e = KILLS[i];
      if (!e || !isNum(e.t) || !(DUR > 0)) continue;
      var f = clamp((e.t - T0) / DUR, 0, 1);
      var tick = make('span', 'ace-tick' + (e.isPlayerActor ? ' is-player' : ''));
      tick.style.left = (f * 100).toFixed(3) + '%';
      tick.title = (e.text ? e.text : '击杀') + '（T+' + fmtSec(e.t - T0) + '）';
      ct.ticks.appendChild(tick);
    }
  }

  function bindSeek(track) {
    function frac(ev) {
      var r = track.getBoundingClientRect();
      if (!(r.width > 0)) return 0;
      return clamp((ev.clientX - r.left) / r.width, 0, 1);
    }
    function to(ev) {
      if (!(DUR > 0)) return;
      seekTo(frac(ev) * DUR);
    }
    track.addEventListener('pointerdown', function (ev) {
      dragging = true;
      try { track.setPointerCapture(ev.pointerId); } catch (e) { /* 无指针捕获也能拖 */ }
      to(ev);
      ev.preventDefault();
    });
    track.addEventListener('pointermove', function (ev) { if (dragging) to(ev); });
    track.addEventListener('pointerup', function (ev) {
      if (dragging) { dragging = false; try { track.releasePointerCapture(ev.pointerId); } catch (e) {} }
    });
    track.addEventListener('pointercancel', function () { dragging = false; });
    // 这里不再单独处理方向键：document 上的全局键盘已经处理 ←/→ 与 Home/End，
    // 两处都处理会让 focus 在进度条上时一次按键 seek 两回。
  }

  function syncControls() {
    if (!ct.play) return;
    setText(ct.play, playing ? '❚❚' : '▶');
    ct.play.classList.toggle('is-on', playing);
    if (ct.speeds) {
      for (var i = 0; i < ct.speeds.length; i++) {
        ct.speeds[i].classList.toggle('is-on', Number(ct.speeds[i].dataset.speed) === speed);
      }
    }
  }

  function updateControls(state) {
    if (!ct.track) return;
    var f = DUR > 0 ? clamp(met / DUR, 0, 1) : 0;
    ct.fill.style.width = (f * 100).toFixed(3) + '%';
    ct.knob.style.left = (f * 100).toFixed(3) + '%';
    ct.track.setAttribute('aria-valuemin', '0');
    ct.track.setAttribute('aria-valuemax', String(Math.round(DUR)));
    ct.track.setAttribute('aria-valuenow', String(Math.round(met)));
    setText(ct.time, 'T+' + fmtMet(met) + ' / ' + fmtSec(DUR));
    var diag = [];
    if (!DATA) diag.push('无数据');
    if (!SCENE) diag.push('无场景');
    setText(ct.diag, diag.join(' · '));
  }

  function setOption(k, v) {
    if (!Object.prototype.hasOwnProperty.call(options, k)) return;
    options[k] = v;
    // 只改本地状态：每帧随 state.options 交给场景。不调 ACEScene.setOption ——
    // 那个接口会锁定该键，之后 state.options 里的同名值就被忽略了（scene3d.js 有意为之）。
    syncControls();
    renderOnce();
  }

  function cycleCamera() {
    var i = CAMERAS.indexOf(camera);
    camera = CAMERAS[(i + 1) % CAMERAS.length];
    if (SCENE && typeof SCENE.setCamera === 'function') {
      try { SCENE.setCamera(camera); } catch (e) { warn('ACEScene.setCamera 抛错：' + errText(e)); }
    }
    renderOnce();
  }

  // ---------- 帮助 ----------
  var HELP_KEYS = [
    ['空格', '播放 / 暂停'],
    ['← / →', '后退 / 前进 10 秒'],
    ['1 / 2 / 4 / 8', '倍速'],
    ['C', '机位循环（follow → orbit → top → free）'],
    ['T', '航迹开关'],
    ['L', '标签开关'],
    ['G', '网格开关'],
    ['H', '角落读数开关'],
    ['V', '地面单位标签：类别 / 具体型号'],
    ['M', '导弹弹道开关'],
    ['R', '重播'],
    ['F', '全屏'],
    ['?', '本帮助']
  ];  function buildHelp() {
    if (!helpEl) return;
    helpEl.textContent = '';
    var box = make('div', 'ace-help-box');
    box.appendChild(make('div', 'ace-help-hd', '按键'));
    var grid = make('div', 'ace-help-grid');
    for (var i = 0; i < HELP_KEYS.length; i++) {
      grid.appendChild(make('span', 'ace-help-k', HELP_KEYS[i][0]));
      grid.appendChild(make('span', 'ace-help-v', HELP_KEYS[i][1]));
    }
    box.appendChild(grid);

    box.appendChild(make('div', 'ace-help-foot', '按 ? 或 Esc 关闭'));
    helpEl.appendChild(box);
  }
  function para(lines) {
    var p = make('div', 'ace-help-p');
    for (var i = 0; i < lines.length; i++) {
      p.appendChild(make('div', 'ace-help-li', '· ' + lines[i]));
    }
    return p;
  }
  function toggleHelp(force) {
    if (!helpEl) return;
    var show = typeof force === 'boolean' ? force : helpEl.hidden;
    helpEl.hidden = !show;
  }

  // ---------- 键盘 ----------
  function onKey(ev) {
    if (ev.ctrlKey || ev.altKey || ev.metaKey) return;
    var t = ev.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    var k = ev.key, used = true;
    if (k === ' ' || k === 'Spacebar') {
      togglePlay();
    } else if (k === 'ArrowLeft') {
      seekTo(met - 10);
    } else if (k === 'ArrowRight') {
      seekTo(met + 10);
    } else if (k === 'Home') {
      seekTo(0);
    } else if (k === 'End') {
      seekTo(DUR);
    } else if (k === '1' || k === '2' || k === '4' || k === '8') {
      setSpeed(Number(k));
    } else if (k === 'c' || k === 'C') {
      cycleCamera();
    } else if (k === 't' || k === 'T') {
      setOption('showTrails', !options.showTrails);
    } else if (k === 'l' || k === 'L') {
      setOption('showLabels', !options.showLabels);
    } else if (k === 'g' || k === 'G') {
      setOption('showGrid', !options.showGrid);
    } else if (k === 'v' || k === 'V') {
      setLabelMode(identity.labelMode === 'model' ? 'category' : 'model');
    } else if (k === 'm' || k === 'M') {
      setOption('showMissiles', !options.showMissiles);
      if (cv.missileBtn) {
        cv.missileBtn.textContent = options.showMissiles ? '显示' : '隐藏';
        cv.missileBtn.classList.toggle('on', options.showMissiles);
      }
    } else if (k === 'h' || k === 'H') {
      // 只隐藏读数块；齿轮按钮留在原位，否则设置入口跟着一起没了
      if (cv.wrap) {
        cv.wrap.classList.toggle('ace-hidden');
        if (cornerEl) cornerEl.classList.toggle('ace-readout-hidden', cv.wrap.classList.contains('ace-hidden'));
      }
    } else if (k === 'r' || k === 'R') {
      replay();
    } else if (k === 'f' || k === 'F') {
      toggleFullscreen();
    } else if (k === '?' || (k === '/' && ev.shiftKey)) {
      toggleHelp();
    } else if (k === 'Escape') {
      if (cv.panel && !cv.panel.hidden) toggleIdentityPanel(false);
      else toggleHelp(false);
    } else {
      used = false;
    }
    if (used) ev.preventDefault();
  }

  function toggleFullscreen() {
    var el = document.documentElement;
    try {
      if (!document.fullscreenElement) {
        var p = el.requestFullscreen ? el.requestFullscreen() : null;
        if (p && typeof p.catch === 'function') p.catch(function () { /* 用户拒绝或被策略挡下，不报错 */ });
      } else if (document.exitFullscreen) {
        var q = document.exitFullscreen();
        if (q && typeof q.catch === 'function') q.catch(function () {});
      }
    } catch (e) { warn('全屏切换失败：' + errText(e)); }
  }

  // ---------- 诊断（给浏览器自验与 verifier 用，只读） ----------
  // file:// 下 Chromium 允许读到 <link> 生成的 stylesheet 条目，但读 cssRules 会抛安全异常，
  // 所以这里区分"链上了"与"能读到规则"，不要把后者当成文件缺失。
  function sheetInfo(name) {
    var ss = document.styleSheets;
    for (var i = 0; i < ss.length; i++) {
      var h = ss[i].href || '';
      if (h.indexOf(name) >= 0) {
        var readable = false, rules = null;
        try { rules = ss[i].cssRules ? ss[i].cssRules.length : null; readable = !!ss[i].cssRules; }
        catch (e) { readable = false; }
        return { linked: true, rulesReadable: readable, ruleCount: rules, href: h };
      }
    }
    return { linked: false, rulesReadable: false, ruleCount: null, href: null };
  }
  function resourceReport() {
    var out = { total: 0, external: [], local: [] };
    try {
      var es = performance.getEntriesByType('resource') || [];
      for (var i = 0; i < es.length; i++) {
        var u = String(es[i].name || '');
        out.total++;
        if (/^[a-z]+:\/\//i.test(u) && !/^file:/i.test(u)) out.external.push(u);
        else out.local.push(u);
      }
    } catch (e) { /* 诊断本身不可用不影响播放 */ }
    return out;
  }
  function diagnostics() {
    var res = resourceReport();
    return {
      contract: 'v6',
      hasData: !!DATA,
      hasScene: !!SCENE,
      sceneBroken: sceneBroken,
      sceneInitError: sceneInitError,
      metaSource: META.source === undefined ? null : META.source,
      replayFile: META.replayFile || null,
      duration: DUR, t0: T0, met: met, playing: playing, speed: speed, camera: camera,
      options: {
        altScale: options.altScale, showTrails: options.showTrails,
        showLabels: options.showLabels, showGrid: options.showGrid,
        showMissiles: options.showMissiles, showOwnModel: options.showOwnModel
      },
      identity: {
        own: identity.own, squad: identity.squad.slice(), squadKeys: squadKeys.slice(),
        storageOK: storageOK, rankedUnits: RANKED.length, storageKey: LS_KEY,
        squadInData: squadMembers() !== null, squadMembers: squadMembers() ? squadMembers().length : 0,
        teammates: teammateSlots(), squadMemberKeys: SQUAD_MEMBER_KEYS.slice(),
        squadAllKeys: SQUAD_ALL_KEYS.slice(),
        squadBound: squadKeys.filter(function (k) { return !!k; }).length,
        labelMode: identity.labelMode,
        squadUnbound: squadKeys.filter(function (k) { return !k; }).length,
        sideMode: identity.sideMode,
        labelMode: identity.labelMode
      },
      counts: {
        ownSamples: OWN ? OWN.n : 0, units: UNITS.length,
        events: EVENTS.length, kills: KILLS.length, contactsNow: buildState().contacts.length,
        frames: frames
      },
      assets: {
        'mission-data.js': !!DATA,
        'scene3d.js': !!window.ACEScene,
        'ace.css': sheetInfo('ace.css'),
        'mission.css': sheetInfo('mission.css'),
        cssNote: 'file:// 下 cssRules 读不到属正常，只能用 linked 判断 <link> 是否被解析'
      },
      requests: res,
      warnings: WARN.slice()
    };
  }

  // ---------- 运行时装载（上传解析后复用同一套页面） ----------
  // 用法：解析完上传的 .wrpl 后调用 ACEUI.mount(missionData)（结构同契约 §2）。
  // 会重置时钟与场景绑定，但保留用户的呼号/显示设置（localStorage），只在"用户没自定义过"时
  // 按新数据的真实队友数重算默认呼号。
  function mount(data) {
    if (!isObj(data)) return false;
    window.ACE_MISSION = data;
    DATA = null; META = {};
    T0 = 0; DUR = 0; OWN = null; UNITS = []; EVENTS = []; KILLS = []; NAMES = {};
    RANKED = []; SQUAD_MEMBER_KEYS = []; SQUAD_ALL_KEYS = []; squadKeys = [];
    SCENE = null; sceneBroken = false; sceneInitError = null;
    met = 0; playing = false; acc = 0; lastTs = 0; dragging = false;
    lastOwnRef = { x: 0, y: 0, has: false };
    // 只保留环境类提示（localStorage），旧数据的告警丢掉
    WARN = WARN.filter(function (w) { return w.indexOf('localStorage') >= 0; });

    loadData();
    if (!hasStoredIdentity) identity.squad = defaultSquadNames();
    resolveSquadKeys();
    rankUnits();
    playing = DUR > 0;
    if (!ct.ticks) buildControls(); else { buildTicks(); syncControls(); }
    buildHelp();
    toggleHelp(false);
    toggleIdentityPanel(false);
    initScene();
    renderOnce();
    return true;
  }

  // ---------- 启动 ----------
  function boot() {
    if (!stageEl || !canvasEl || !cornerEl || !controlsEl || !helpEl) {
      // 页面骨架不对时也别静默：尽最大努力报告
      warn('index.html 缺少 #stage / #scene / #corner / #controls / #help 之一');
    }
    loadData();
    loadIdentity(); rankUnits();  // 呼号：先读 localStorage，再按航迹长度排序（§8.3）
    playing = DUR > 0;      // 双击即开就能看：有数据时自动开播，空格可暂停
    buildCorner();
    buildControls();
    buildHelp();
    toggleHelp(false);
    initScene();
    document.addEventListener('keydown', onKey);
    rafId = window.requestAnimationFrame(frame);
    renderOnce();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  // 调试/自验接口（只读为主，seek/play/pause 与键盘等价）
  window.ACEUI = {
    contract: 'v6',
    get met() { return met; },
    get t0() { return T0; },
    get duration() { return DUR; },
    get playing() { return playing; },
    get speed() { return speed; },
    get camera() { return camera; },
    get identity() {
      return { own: identity.own, squad: identity.squad.slice(), squadKeys: squadKeys.slice(), storageOK: storageOK };
    },
    setIdentity: setIdentity,
    resetIdentity: resetIdentity,
    get sideMode() { return identity.sideMode; },
    setSideMode: setSideMode,
    get labelMode() { return identity.labelMode; },
    setLabelMode: setLabelMode,
    toggleIdentityPanel: toggleIdentityPanel,
    get rankKeys() { return RANKED.map(function (r) { return r.key; }); },
    get rankLens() { return RANKED.map(function (r) { return Math.round(r.len); }); },
    get squadBinding() { return squadKeys.slice(); },
    get squadAllKeys() { return SQUAD_ALL_KEYS.slice(); },
    get squadMemberKeys() { return SQUAD_MEMBER_KEYS.slice(); },
    get teammateSlots() { return teammateSlots(); },
    squadSourceLine: squadSourceLine,
    sourceLine: sourceLine,
    mount: mount,

    get options() {
      return {
        altScale: options.altScale, showTrails: options.showTrails,
        showLabels: options.showLabels, showGrid: options.showGrid,
        showMissiles: options.showMissiles, showOwnModel: options.showOwnModel
      };
    },
    get frames() { return frames; },
    state: buildState,
    seek: seekTo, play: play, pause: pause, replay: replay, setSpeed: setSpeed,
    setOption: setOption, setCamera: function (m) { if (CAMERAS.indexOf(m) >= 0) { camera = m; if (SCENE && SCENE.setCamera) { try { SCENE.setCamera(m); } catch (e) {} } renderOnce(); } },
    toggleHelp: toggleHelp,
    diagnostics: diagnostics,
    warnings: function () { return WARN.slice(); }
  };
})();
