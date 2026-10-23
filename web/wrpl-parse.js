/* =====================================================================
 * wrpl-parse.js —— 浏览器内解析 War Thunder .wrpl 回放
 * ---------------------------------------------------------------------
 * 产物：window.WRPLParse = { parse(arrayBuffer, opts) -> Promise<ACE_MISSION>,
 *                            progress(cb), version }
 *
 * ACE_MISSION 的 schema 与 ace-combat/mission-data.js 完全一致，
 * 供现有的 scene3d.js / mission.js 直接消费（前端不需要改渲染逻辑）。
 *
 * 依赖：vendor/fzstd.min.js（MIT，纯 JS zstd 解码；Chromium 的
 *       DecompressionStream 不支持 'zstd'，故必须自带）。
 *       普通 <script> 引入，运行时**不联网**、不需要构建。
 *
 * 移植来源（只移植知识，实现为自写 JS）：
 *   * ace-combat/tools/wrpl_header.py  —— 容器头 / zstd 帧 / VLQ 包帧 / FAT BLK
 *   * ace-combat/tools/wrpl_decode.py  —— ptype=2 FlightModel 位流、本机属性表、断轨
 *   * ace-combat/tools/wrpl_kill_events.py —— IdFieldSerializer32（击杀/伤害包）
 *   * ace-combat/tools/wrpl_decode_ecs.py  —— ptype=6 ECS 实体构造、地面点、机场/跑道
 *   * ace-combat/tools/build_ace_mission.py —— 上采样 / 事件 / meta 组装
 *   * 回放格式解析参考 wrpl-inspector（https://github.com/maxsupermanhd/wrpl-inspector，
 *     AGPL-3.0）—— 只用于开发期理解包格式，其代码不进本文件。
 *
 * 与 Python 版的**已知差异**（详见 ace-combat/docs/WRPL-JS.md）：
 *   1. 不读 ecshashes.json（780 KB 组件哈希表）：本文件只解下游真正用到的字段
 *      （位置 / uid / unit__className / spawnSlotName / 弹药位置），它们都在
 *      块内**固定偏移或长度前缀串**上，不需要组件名派发；
 *   2. 不解析 ECS 组件值本身（Python 用它算 blockUsed；下游未使用）；
 *   3. 控制码 0x25（lz4 压缩构造）：Python 端实测产出实体 0 条，本文件直接跳过；
 *   4. 8111 开发期校验数字（ownMaxErrM / unitAssocRate 等）在浏览器里无从获得，
 *      一律 null 并在 meta.limits 注明。
 * ===================================================================== */
(function (global) {
  'use strict';

  /* ------------------------------------------------------------ 常量 */

  var SEC_PER_TICK = 1.0 / 256000.0;   // tick 是 256 kHz 原始计数器：tick = 毫秒 × 256
  var TICK_PER_MS = 256;
  var HEADER_BYTES = 1224;             // .wrpl 定长头部
  var HEADER_PAD_AFTER = 2;            // 头部与 settings BLK 之间固定 2 字节
  var ZSTD_MAGIC = [0x28, 0xb5, 0x2f, 0xfd];

  var MAX_ABS_M = 200000.0;            // 坐标绝对值上限（地图只有 ±65536）
  var HZ_OWN = 10;                     // 本机输出采样率
  var UNIT_HZ = { air: 10, ground: 1 };
  var MAX_INTERP_GAP_S = 1.5;
  var MIN_TRACK_SAMPLES = 3;
  var TELEPORT_SPEED_MS = 600.0;
  var TELEPORT_MIN_M = 500.0;
  var SPD_LIMIT = 600.0;               // 断轨阈值（wrpl_decode.py）
  var MIN_SEG = 4;

  var UID_REL_OFFSET = 29;             // 飞机 ECS 块里 uid 相对组件值起点（u16 LE）
  var POSITION_REL = 10;               // 组件值起点 + 10 = 三个 f32 (x, 上, z)
  var QUATERNION_REL = 36;
  var RUNWAY_MARKERS = ['t1_airfield_top_reload', 't2_airfield_top_reload',
                        't1_airfield_top', 't2_airfield_top'];
  var RUNWAY_LENGTH_M = 3000.0;        // 来自模型名 dynaf_pg_1line_3000_universal

  /* 地面同步位置：MPI 0xF073（客户端）/ 0xF074（服务器），ptype=4。
   * 来源：wrpl-acmi dev 分支 wrpl/packet/parser/movement/ground.go（AGPL-3.0），
   * 按位布局重写；本局实测 788 包 / 118 uid / 7275 采样，0 解析错误。 */
  var GROUND_MPI_IDS = [0x73, 0x74];
  var GROUND_UID_MAX = 2048;
  var GROUND_DELTA_HIST = 32;
  var GROUND_MAX_DELTA = 16384;
  var GROUND_MAX_RECORD = 65536;
  var GROUND_SPAWN_MATCH_M = 10.0;     // uid → ECS 地面构造点的绑定阈值（首采样位置）

  var AIR_TEMPLATES = ['aircraft+player_unit', 'aircraft'];
  var AIR_TEMPLATE_NAMES = { 'aircraft': 1, 'aircraft+player_unit': 1 };
  var PROJECTILE_TEMPLATES = { 'rocket': 1, 'rocket+arh_shell': 1, 'bomb': 1,
                               'payload': 1, 'jettisoned': 1 };
  var SCENERY_TEMPLATES = { 'lamppost_c': 1, 'aircraft_warning_light_red': 1,
                            'doors_grid': 1, 'loot_grid': 1, 'humans_grid': 1,
                            'vehicles_grid': 1, 'aircrafts_grid': 1,
                            'interactable_grid': 1, 'attract_points_grid': 1,
                            'destroy_ri_box_area': 1 };
  var MOD_TEMPLATES = { 'toolkit_ecs': 1, 'extinguisher_ecs': 1,
                        'medicalkit_ecs': 1, 'tank_smokescreen_ecs': 1 };
  //: 只对「第一个组件就是 transform」的模板用 +10 规则（Python 已验证：飞机/地面/工事）
  var POSITION_TEMPLATES = { 'tank': 1, 'fortification': 1,
                             'aircraft': 1, 'aircraft+player_unit': 1 };

  var SIDE_CODE = { ally: 'aly', enemy: 'eny', squad: 'sqd', unknown: 'unk' };
  var SHOT_NAMES = { 'rocket': 'ROCKET', 'bomb': 'BOMB', 'rocket+arh_shell': 'ARH ROCKET',
                     'payload': 'PAYLOAD', 'jettisoned': 'JETTISONED' };
  var WEAPON_NAMES = {
    'us_aim9l_sidewinder': 'AIM-9L', 'us_aim9l_sidewinder_default': 'AIM-9L',
    'us_aim9m_sidewinder': 'AIM-9M', 'us_aim7f_sparrow_f_14': 'AIM-7F',
    'us_aim7m_sparrow_f_15_default': 'AIM-7M', 'us_aim7m_sparrow_fa_18': 'AIM-7M',
    'us_aim7p_sparrow_fa_18': 'AIM-7P', 'su_r_60': 'R-60', 'su_r_73_default': 'R-73',
    'su_r_27er': 'R-27ER', 'su_r_77_default': 'R-77', 'su_rvv_ae': 'RVV-AE',
    'su_kh_25ml': 'Kh-25ML', 'su_pl5e2_default': 'PL-5EII', 'cn_sd10a': 'SD-10A',
    'swd_rb99_default': 'RB99'
  };
  var ZW_CHARS = /[\u200b\u200c\u200d\ufeff\u00ad\u200e\u200f]/g;
  var HIDDEN_TEMPLATES = ['structures/dummy_immortal', 'dummy_plane',
                          'structures/dummy_plane',
                          'structures/unit_airfield_flight_radar_char',
                          'structures/unit_airfield_main_radar_char'];
  var RKILL = /^[a-z0-9][a-z0-9_.\-]{2,48}$/;
  var GCLASS = /^(?:tankModels|structures)\/[\w/.\-]+$/;
  var AIRFIELD_KEY = /airfield|runway|airstrip|strip|apron|taxiway|airbase|hangar|farp/i;
  var VEH_RE = /^[a-z0-9][a-z0-9_.\-]{2,48}$/;
  var MISSION_RE = /t([12])_player(\d{2})_(\d)/;

  /* 显示名表（**显示约定**，不是回放数据）
   * 取值 displayName = MAP[vehicle] ?? vehicle；表里为空串的模板界面不显示。
   * 内部名来自回放 ECS；显示名来自 War-Thunder-Datamine（gszabi99）lang/units.csv。 */
  var DISPLAY_NAMES = {"a_4b":"A-4B","a_4b_bomb_mk83":"A-4B","cf_188a_canada":"CF-188A","dummy_plane":"","f_14a_early":"F-14A","f_15a":"F-15A","f_16a_block_10":"F-16A","f_16a_block_10_netz_mod":"F-16A","f_16xl":"F-16XL","f_20a":"F-20A","fa_18a":"F/A-18A","fa_18c_early":"F/A-18C","fa_18c_switzerland":"F/A-18C","jf_17":"JF-17","m_346fa":"M-346FA","mig-15bis_ish":"MIG-15BIS","mig_15bis_ish_250_bombs":"MIG-15BIS","mig_29_9_12_germany":"MIG-29","mig_29smt_9_19":"MIG-29SMT","mirage_4000":"MIRAGE 4000","saab_ja37di_f21":"JA 37DI","structures/dummy_immortal":"","structures/uk_bunker_light_fat":"LIGHT BUNKER","structures/unit_airfield_flight_radar_char":"AIRFIELD FLIGHT RADAR","structures/unit_airfield_main_radar_char":"AIRFIELD MAIN RADAR","structures/ussr_quest_large_art_noground":"ARTILLERY","su_25":"SU-25","su_30mk2v_venezuela":"SU-30MK2V","su_33":"SU-33","tankModels/fr_crotale_ng":"CROTALE NG","tankModels/germ_flakpz_I_Gepard":"GEPARD","tankModels/uk_rooikat_za_35":"ROOIKAT ZA-35","tankModels/us_truck_us6_studebaker_tent":"US6","tankModels/ussr_btr_152a":"BTR-152A","tankModels/ussr_object_906":"OBJECT 906","tankModels/ussr_t_55_am":"T-55AM","tornado_f3":"TORNADO F.3"};
  var HIDDEN_EXTRA = ["dummy_plane","structures/dummy_immortal"];
  /* 分类表（同样是**显示约定**、服务器侧数据表）：category = CATS[vehicle] ?? 'UNKNOWN'。
   * 键与 DISPLAY_NAMES 完全同一套（回放里的 unit__className）。 */
  var VEHICLE_CATEGORIES = {"a_4b":"FIGHTER","a_4b_bomb_mk83":"FIGHTER","cf_188a_canada":"FIGHTER","dummy_plane":"FIGHTER","f_14a_early":"FIGHTER","f_15a":"FIGHTER","f_16a_block_10":"FIGHTER","f_16a_block_10_netz_mod":"FIGHTER","f_16xl":"FIGHTER","f_20a":"FIGHTER","fa_18a":"FIGHTER","fa_18c_early":"FIGHTER","fa_18c_switzerland":"FIGHTER","jf_17":"ATTACKER","m_346fa":"ATTACKER","mig-15bis_ish":"FIGHTER","mig_15bis_ish_250_bombs":"FIGHTER","mig_29_9_12_germany":"FIGHTER","mig_29smt_9_19":"FIGHTER","mirage_4000":"FIGHTER","saab_ja37di_f21":"FIGHTER","structures/dummy_immortal":"FORTIFICATION","structures/uk_bunker_light_fat":"BUNKER","structures/unit_airfield_flight_radar_char":"RADAR","structures/unit_airfield_main_radar_char":"RADAR","structures/ussr_quest_large_art_noground":"ARTILLERY","su_25":"ATTACKER","su_30mk2v_venezuela":"FIGHTER","su_33":"FIGHTER","tankModels/fr_crotale_ng":"SPAA","tankModels/germ_flakpz_I_Gepard":"SPAA","tankModels/uk_rooikat_za_35":"SPAA","tankModels/us_truck_us6_studebaker_tent":"TRUCK","tankModels/ussr_btr_152a":"SPAA","tankModels/ussr_object_906":"TANK","tankModels/ussr_t_55_am":"TANK","tornado_f3":"FIGHTER"};

  /* ------------------------------------------------------------ 小工具 */

  /** Python 的 round()：四舍六入五取偶（对二进制 double 的十进制展开）。 */
  function pyRound(v, nd) {
    if (typeof v !== 'number' || !isFinite(v)) return v;
    if (v === 0) return 0;
    var m = Math.pow(10, nd);
    var scaled = v * m;
    var fl = Math.floor(scaled);
    var diff = scaled - fl;
    var n;
    if (diff === 0.5) n = (fl % 2 === 0) ? fl : fl + 1;
    else if (diff === -0.5) n = (fl % 2 === 0) ? fl : fl + 1;
    else n = Math.round(scaled);
    var out = n / m;
    if (out === 0) out = 0;
    return out;
  }

  /** build_ace_mission.rnd：round 后把 -0 归一成 0；nd=0 时取整。 */
  function rnd(value, nd) {
    if (value === null || value === undefined || typeof value !== 'number' || !isFinite(value)) return null;
    var out = pyRound(value, nd);
    if (out === 0) out = 0;
    return nd === 0 ? Math.trunc(out) : out;
  }

  function finiteM(value) {
    if (typeof value !== 'number' || !isFinite(value)) return null;
    if (Math.abs(value) > MAX_ABS_M) return null;
    return value;
  }

  function bisectLeft(arr, t) {
    var lo = 0, hi = arr.length;
    while (lo < hi) {
      var mid = (lo + hi) >> 1;
      if (arr[mid] < t) lo = mid + 1; else hi = mid;
    }
    return lo;
  }

  function bisectRight(arr, t) {
    var lo = 0, hi = arr.length;
    while (lo < hi) {
      var mid = (lo + hi) >> 1;
      if (t < arr[mid]) hi = mid; else lo = mid + 1;
    }
    return lo;
  }

  function cmpStr(a, b) { return a < b ? -1 : (a > b ? 1 : 0); }

  function wrap180(deg) { return (deg + 180.0) % 360.0 - 180.0; }

  function bearingDeg(dx, dy) {
    var d = Math.atan2(dx, dy) * 180.0 / Math.PI % 360.0;
    return d < 0 ? d + 360.0 : d;
  }

  function cleanText(text) {
    var out = String(text === null || text === undefined ? '' : text).replace(ZW_CHARS, '');
    while (out.indexOf('<color=') >= 0) {
      var i = out.indexOf('<color=');
      var j = out.indexOf('>', i);
      if (j < 0) break;
      var k = out.indexOf('</color>', j);
      out = out.slice(0, i) + out.slice(j + 1, k < 0 ? out.length : k) +
            (k < 0 ? '' : out.slice(k + 8));
    }
    return out;
  }

  function weaponDisplay(name) {
    if (!name) return null;
    return WEAPON_NAMES[name] || name.toUpperCase().replace(/_/g, '-');
  }

  /* 共用的 8 字节暂存：**写入与读取必须同一块 buffer**（早期版本写 fb 读 dv，
   * 两个 ArrayBuffer 互不相干，f32 恒为 0.0 —— 这种错会静默地把全部坐标变成 0）。 */
  var _stats = { buf: new ArrayBuffer(8), dv: null, u8: null };

  function f32(b, o) {
    var u8 = _stats.u8;
    u8[0] = b[o]; u8[1] = b[o + 1]; u8[2] = b[o + 2]; u8[3] = b[o + 3];
    return _stats.dv.getFloat32(0, true);
  }

  function u16le(b, o) { return b[o] | (b[o + 1] << 8); }

  function u32le(b, o) {
    return (b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)) >>> 0;
  }

  function i32le(b, o) {
    return b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24);
  }

  function f32at(b, o) { return f32(b, o); }

  function readUleb(b, p) {
    var result = 0, shift = 0;
    while (true) {
      var x = b[p++];
      result += (x & 0x7F) * Math.pow(2, shift);
      if (!(x & 0x80)) return [result, p];
      shift += 7;
      if (shift > 63) throw new Error('ULEB128 过长');
    }
  }

  function decodeAscii(b, from, to) {
    var s = '';
    for (var i = from; i < to; i++) s += String.fromCharCode(b[i]);
    return s;
  }

  function utf8(b, from, to) {
    if (typeof TextDecoder !== 'undefined') {
      return new TextDecoder('utf-8').decode(b.subarray(from, to));
    }
    return decodeAscii(b, from, to);
  }

  function initStats() {
    _stats.buf = new ArrayBuffer(8);
    _stats.dv = new DataView(_stats.buf);
    _stats.u8 = new Uint8Array(_stats.buf);
  }

  function sleep0() { return new Promise(function (r) { setTimeout(r, 0); }); }

  /* ------------------------------------------------------------ 进度 */

  var _progressCb = null;
  function progress(cb) { _progressCb = (typeof cb === 'function') ? cb : null; }
  function emit(phase, pct, note) {
    if (_progressCb) { try { _progressCb({ phase: phase, pct: pct, note: note || '' }); } catch (e) {} }
  }

  /* ------------------------------------------------------------ 位读器
   * 位序：字节内**从最高位开始**（MSB-first，与 Dagor / wrpl-inspector 一致）。
   * 越界即抛错 —— Python 那边靠 bytes 的 IndexError，JS 必须显式检查，
   * 否则读到 undefined 会静默按 0 处理、把后面全部解错。 */

  function BitReader(d) {
    this.d = d;
    this.n = d.length * 8;
    this.pos = 0;
  }
  BitReader.prototype.bits = function (n) {
    if (n === 0) return 0;
    var p = this.pos;
    if (p + n > this.n) throw new Error('bit eof ' + p + '+' + n + '/' + this.n);
    var v = 0, d = this.d;
    for (var k = 0; k < n; k++) {
      var q = p + k;
      v = (v << 1) | ((d[q >> 3] >> (7 - (q & 7))) & 1);
      if (k >= 30) v = v >>> 0;   // 32 位以上只需要低位有效（最多 62 位 -> 用 v 的浮点语义）
    }
    this.pos = p + n;
    return v;
  };
  // 大位宽（>32）时上面的循环要用浮点累加；单独实现避免精度损失
  BitReader.prototype.bitsBig = function (n) {
    var v = 0;
    for (var k = 0; k < n; k++) v = v * 2 + this.bits(1);
    return v;
  };
  BitReader.prototype.byte = function () { return this.bits(8); };
  BitReader.prototype.bytesN = function (n) {
    var out = new Uint8Array(n);
    for (var i = 0; i < n; i++) out[i] = this.bits(8);
    return out;
  };
  BitReader.prototype.u16 = function () {
    var b = this.bytesN(2); return b[0] | (b[1] << 8);
  };
  BitReader.prototype.u32 = function () {
    var b = this.bytesN(4);
    return (b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)) >>> 0;
  };
  BitReader.prototype.f32 = function () {
    var b = this.bytesN(4);
    var u8 = _stats.u8;
    u8[0] = b[0]; u8[1] = b[1]; u8[2] = b[2]; u8[3] = b[3];
    return _stats.dv.getFloat32(0, true);
  };
  BitReader.prototype.compressed = function () {
    var v = 0, c = 0;
    while (true) {
      var a = this.byte();
      v += (a & 0x7F) * Math.pow(2, 7 * c); c += 1;
      if (!(a & 0x80)) return v;
      if (c > 9) throw new Error('varint too long');
    }
  };
  BitReader.prototype.zigzag32 = function () {
    var u = this.compressed() % 4294967296;
    return ((u / 2) | 0) ^ -(u & 1);
  };
  BitReader.prototype.align = function () {
    this.pos += 8 - (((this.pos - 1) & 7) + 1);
  };
  BitReader.prototype.skip = function (n) { this.pos += n; };
  BitReader.prototype.eid = function () {
    var f = this.u16();
    if (f & 1) return ((f >> 2) | (((f & 2) >> 1) << 22)) >>> 0;
    if (f & 2) return ((f >> 2) | (this.byte() << 22)) >>> 0;
    var s = this.u16();
    var ret = ((s << 16) | f) >>> 0;
    return ((((ret & 0x00FFFFFF) >>> 2) | ((ret >>> 24) << 22))) >>> 0;
  };

  /* ------------------------------------------------------------ 1. 容器 */

  function readCString(d, off, size) {
    var end = off + size;
    var z = -1;
    for (var i = off; i < end; i++) { if (d[i] === 0) { z = i; break; } }
    return utf8(d, off, z >= 0 ? z : end);
  }

  function parseReplayHeader(d) {
    var o = 0;
    function u32() { var v = u32le(d, o); o += 4; return v; }
    function u64() { var v = u32le(d, o) + u32le(d, o + 4) * 4294967296; o += 8; return v; }
    /** 64 位十六进制：直接从字节拼，避免超过 2^53 时丢低位（sessionId 就超了）。 */
    function u64Hex() {
      var s = '';
      for (var i = 7; i >= 0; i--) s += ('0' + d[o + i].toString(16)).slice(-2);
      o += 8;
      return '0x' + s;
    }
    function fixed(n) { var s = readCString(d, o, n); o += n; return s; }
    var h = {};
    h.magic = u32();
    h.version = u32();
    h.level = fixed(128);
    h.levelSettings = fixed(260);
    h.battleType = fixed(128);
    h.environment = fixed(128);
    h.visibility = fixed(32);
    h.rezOffset = u32();
    var diff = d[o]; o += 1;
    h.difficultyRawByte = diff;
    h.difficultyUnknownHighNibble = (diff >> 4) & 0x0F;
    h.difficultyLowNibble = diff & 0x0F;
    o += 35;
    h.sessionType = u32();
    o += 4;
    var sidHex = u64Hex();
    h.sessionId = parseInt(sidHex, 16);
    o += 4;
    h.mSetSize = u32();
    h.settingsSize = u32();
    o += 28;
    h.locName = fixed(128);
    h.startTime = u32();
    h.timeLimit = u32();
    h.scoreLimit = u32();
    o += 48;
    h.battleClass = fixed(128);
    h.battleKillStreak = fixed(128);
    if (o !== HEADER_BYTES) throw new Error('头部解析出 ' + o + ' 字节，与预期的 ' + HEADER_BYTES + ' 不符');
    h.magicHex = '0x' + ('00000000' + h.magic.toString(16)).slice(-8);
    h.sessionIdHex = sidHex;
    try {
      h.startTimeUtc = new Date(h.startTime * 1000).toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC');
    } catch (e) { h.startTimeUtc = null; }
    return h;
  }

  function locatePacketStream(d, hdr) {
    var settingsOff = HEADER_BYTES + HEADER_PAD_AFTER;
    var settingsEnd = settingsOff + hdr.settingsSize;
    var rez = hdr.rezOffset;
    if (!(rez > 0 && rez <= d.length)) rez = 0;
    var searchEnd = rez || d.length;
    var frame = -1;
    for (var i = settingsEnd; i + 4 <= searchEnd; i++) {
      if (d[i] === ZSTD_MAGIC[0] && d[i + 1] === ZSTD_MAGIC[1] &&
          d[i + 2] === ZSTD_MAGIC[2] && d[i + 3] === ZSTD_MAGIC[3]) { frame = i; break; }
    }
    var info = {
      headerBytes: HEADER_BYTES, settingsOffset: settingsOff, settingsSize: hdr.settingsSize,
      packetStreamOffset: settingsEnd, zstdFrameOffset: frame >= 0 ? frame : null,
      resultsOffset: rez || null, resultsBytes: rez ? d.length - rez : null,
      compression: 'unknown'
    };
    if (frame >= 0) {
      info.compression = 'zstd';
      info.zstdFrameBytes = searchEnd - frame;
      info.gapBeforeZstdBytes = frame - settingsEnd;
    } else if (d[settingsEnd] === 0x78 || d[settingsEnd] === 0x68 || d[settingsEnd] === 0x58) {
      info.compression = 'zlib';
    }
    return info;
  }

  /** 「格式不支持」错误：带 code，便于页面把它渲染成专门的错误态（不用猜字符串）。 */
  function unsupportedFormat(tech) {
    var e = new Error('不支持的回放格式');
    e.code = 'unsupported-format';
    e.tech = tech || '';
    return e;
  }

  function inflateZlib() {
    throw unsupportedFormat('包流压缩格式是 zlib（本工具只实现 zstd 帧）');
  }

  /** 解压包流。本局为单个 zstd 帧；zstd 走 vendored 的 fzstd。 */
  async function decompressStream(d, info) {
    if (info.compression === 'zstd') {
      var fz = global.fzstd || (global.self && global.self.fzstd);
      if (!fz || typeof fz.decompress !== 'function') {
        var e0 = new Error('缺少 vendor/fzstd.min.js（浏览器自带 DecompressionStream 不支持 zstd）');
        e0.code = 'missing-decoder';
        throw e0;
      }
      var raw = d.subarray(info.zstdFrameOffset, info.resultsOffset || d.length);
      var t0 = Date.now();
      var out = fz.decompress(raw);
      emit('zstd', 12, '解压 ' + raw.length + ' B → ' + out.length + ' B（' + (Date.now() - t0) + ' ms）');
      await sleep0();
      return out;
    }
    if (info.compression === 'zlib') {
      return inflateZlib(d.subarray(info.packetStreamOffset, info.resultsOffset || d.length));
    }
    throw unsupportedFormat('包流压缩格式不是 zstd/zlib');
  }

  /* ------------------------------------------------------------ 2. 包帧 */

  /** 包长前缀（VLQ）。返回 [size, nextOffset]。 */
  function readVlqSize(b, p) {
    var f = b[p];
    if (f & 0x80) return [f & 0x7F, p + 1];
    if (f & 0x40) return [(((f << 8) | b[p + 1]) ^ 0x4000) >>> 0, p + 2];
    if (f & 0x20) return [(((f << 16) | (b[p + 1] << 8) | b[p + 2]) ^ 0x200000) >>> 0, p + 3];
    if (f & 0x10) {
      return [(((f << 24) | (b[p + 1] << 16) | (b[p + 2] << 8) | b[p + 3]) ^ 0x10000000) >>> 0, p + 4];
    }
    return [u32le(b, p + 1), p + 4];
  }

  /** 切成 [{ptype, ms, hl, start, end}]；包头 2 字节（h0&0x10 = 沿用上一 tick）。 */
  function parsePackets(payload) {
    var out = [], p = 0, last = 0, n = payload.length;
    while (p < n) {
      var size, p2;
      try { var r = readVlqSize(payload, p); size = r[0]; p2 = r[1]; }
      catch (e) { break; }
      if (size === 0 || p2 + size > n) break;
      var h0 = payload[p2], ptype, ms, hl;
      if (h0 & 0x10) { ptype = h0 ^ 0x10; ms = last; hl = 2; }
      else { ptype = h0; ms = u32le(payload, p2 + 2); hl = 6; }
      last = ms;
      out.push({ ptype: ptype, ms: ms, hl: hl, start: p2, end: p2 + size });
      p = p2 + size;
    }
    return out;
  }

  function packetPayload(buf, pk) { return buf.subarray(pk.start + pk.hl, pk.end); }

  /* ------------------------------------------------------------ 3. FAT BLK
   * 格式来源：wt_blk（https://github.com/Warthunder-Open-Source-Foundation/wt_blk，
   * AGPL-3.0）的二进制 BLK 规格；本实现为自写 JS。 */

  var BLK_SIZES = { 0x04: 8, 0x05: 12, 0x06: 16, 0x07: 8, 0x08: 12, 0x0B: 48, 0x0C: 8 };
  var BLK_FMT = { 0x04: '2f', 0x05: '3f', 0x06: '4f', 0x07: '2i', 0x08: '3i', 0x0B: '12f' };
  var BLK_INLINE = { 0x02: 1, 0x03: 1, 0x09: 1, 0x0A: 1 };

  function blkReadFloats(b, o, fmt) {
    var n = fmt.length ? fmt.length : 0;
    var counts = { '2f': 2, '3f': 3, '4f': 4, '12f': 12, '2i': 2, '3i': 3 };
    var k = counts[fmt];
    var out = [];
    for (var i = 0; i < k; i++) out.push(rnd(f32at(b, o + i * 4), 6));
    return out;
  }

  function parseFatBlk(raw) {
    if (!raw || !raw.length) throw new Error('空的 BLK');
    var ftype = raw[0];
    if (ftype !== 0x01) throw new Error('BLK 首字节 0x' + ftype.toString(16) + '，本函数只处理 FAT(0x01)');
    var buf = raw.subarray(1);
    var r1 = readUleb(buf, 0), namesCount = r1[0], p = r1[1];
    var r2 = readUleb(buf, p); var namesSize = r2[0]; p = r2[1];
    var namesStart = p, names = [];
    var q = namesStart;
    for (var i = 0; i < namesCount; i++) {
      var z = q;
      while (buf[z] !== 0) z++;
      names.push(utf8(buf, q, z));
      q = z + 1;
    }
    p = namesStart + namesSize;
    var r3 = readUleb(buf, p); var blocksCount = r3[0]; p = r3[1];
    var r4 = readUleb(buf, p); var paramsCount = r4[0]; p = r4[1];
    var r5 = readUleb(buf, p); var paramsSize = r5[0]; p = r5[1];
    var paramsData = buf.subarray(p, p + paramsSize);
    p += paramsSize;
    var paramsInfo = buf.subarray(p, p + paramsCount * 8);
    p += paramsCount * 8;
    var blockInfo = buf.subarray(p);

    function value(tid, data) {
      if (BLK_INLINE[tid]) {
        var raw4 = new Uint8Array(4);
        raw4[0] = data & 0xFF; raw4[1] = (data >>> 8) & 0xFF;
        raw4[2] = (data >>> 16) & 0xFF; raw4[3] = (data >>> 24) & 0xFF;
        var dv = new DataView(raw4.buffer);
        if (tid === 0x02) return dv.getInt32(0, true);
        if (tid === 0x03) return rnd(dv.getFloat32(0, true), 6);
        if (tid === 0x09) return data !== 0;
        return [raw4[0], raw4[1], raw4[2], raw4[3]];
      }
      var size = BLK_SIZES[tid];
      var seg = size ? paramsData.subarray(data, data + size) : paramsData.subarray(data);
      if (tid === 0x01) {
        var zz = -1;
        for (var k = 0; k < seg.length; k++) { if (seg[k] === 0) { zz = k; break; } }
        return utf8(seg, 0, zz >= 0 ? zz : seg.length);
      }
      if (tid === 0x0C) {
        var lo = u32le(seg, 0), hi = u32le(seg, 4);
        var v = lo + hi * 4294967296;
        if (v >= 9223372036854775808) v -= 18446744073709551616;
        return v;
      }
      var fmt = BLK_FMT[tid];
      if (!fmt) return '<未知类型 0x' + tid.toString(16) + '>';
      return blkReadFloats(seg, 0, fmt);
    }

    var params = [];
    for (var pi = 0; pi < paramsCount; pi++) {
      var c0 = pi * 8;
      var nameId = paramsInfo[c0] | (paramsInfo[c0 + 1] << 8) | (paramsInfo[c0 + 2] << 16);
      var tid2 = paramsInfo[c0 + 3];
      var data = u32le(paramsInfo, c0 + 4);
      params.push([nameId < names.length ? names[nameId] : '<越界 ' + nameId + '>', value(tid2, data)]);
    }

    var flat = [], bp = 0, ptr = 0;
    for (var bI = 0; bI < blocksCount; bI++) {
      var a1 = readUleb(blockInfo, bp); var nameId2 = a1[0]; bp = a1[1];
      var a2 = readUleb(blockInfo, bp); var paramCount = a2[0]; bp = a2[1];
      var a3 = readUleb(blockInfo, bp); var childCount = a3[0]; bp = a3[1];
      var firstChild = 0;
      if (childCount > 0) { var a4 = readUleb(blockInfo, bp); firstChild = a4[0]; bp = a4[1]; }
      var bname = nameId2 === 0 ? 'root'
        : (nameId2 - 1 < names.length ? names[nameId2 - 1] : '<越界 ' + nameId2 + '>');
      flat.push({ name: bname, fields: params.slice(ptr, ptr + paramCount),
                  children: childCount, first: firstChild });
      ptr += paramCount;
    }

    function build(idx) {
      var node = flat[idx], out = {}, i2, k2;
      for (i2 = 0; i2 < node.fields.length; i2++) out[node.fields[i2][0]] = node.fields[i2][1];
      for (var j = node.first; j < node.first + node.children; j++) {
        var child = flat[j], sub = build(j), key = child.name;
        if (Object.prototype.hasOwnProperty.call(out, key)) {
          if (!Array.isArray(out[key])) out[key] = [out[key]];
          out[key].push(sub);
        } else out[key] = sub;
      }
      return out;
    }
    return { tree: build(0), meta: { fileType: 'FAT', namesCount: namesCount, namesSize: namesSize,
      blocksCount: blocksCount, paramsCount: paramsCount, paramsDataSize: paramsSize,
      blockInfoBytes: blockInfo.length, consumedBytes: raw.length } };
  }

  /* ------------------------------------------------------------ 4. 玩家表 */

  function playersInfoItems(results) {
    var root = ((results.uiScriptsData || {}).playersInfo) || {};
    if (root && typeof root === 'object' && !Array.isArray(root)) {
      return Object.keys(root).map(function (k) { return root[k]; });
    }
    if (Array.isArray(root)) return root;
    return [];
  }

  function sortedCraftVehicles(crafts) {
    var out = [];
    if (crafts && typeof crafts === 'object' && !Array.isArray(crafts)) {
      Object.keys(crafts).sort(cmpStr).forEach(function (slot) { out.push(crafts[slot]); });
    }
    return out;
  }

  function mergePlayers(results) {
    var raw = results.player || [];
    if (!Array.isArray(raw)) raw = [raw];
    var byId = {};
    playersInfoItems(results).forEach(function (val) {
      if (val && typeof val === 'object' && val.id !== undefined) byId[String(val.id)] = val;
    });
    var out = [];
    raw.forEach(function (pl) {
      if (!pl || typeof pl !== 'object') return;
      var uid = String(pl.userId === undefined ? '' : pl.userId);
      var info = byId[uid] || {};
      var ci = info.crafts_info, ci0 = (ci && typeof ci === 'object') ? ci.array0 : null;
      out.push({
        userId: uid, name: info.name || pl.name, clanTag: info.clanTag || pl.clanTag,
        clanId: info.clanId, platform: info.platform, team: pl.team, squadId: pl.squadId,
        slot: info.slot, country: info.country, tier: info.tier, elo: info.elo,
        waitTimeSec: info.wait_time, kills: pl.kills, groundKills: pl.groundKills,
        humanKills: pl.humanKills, navalKills: pl.navalKills, teamKills: pl.teamKills,
        aiKills: pl.aiKills, aiGroundKills: pl.aiGroundKills, aiNavalKills: pl.aiNavalKills,
        assists: pl.assists, deaths: pl.deaths, captureZone: pl.captureZone,
        damageZone: pl.damageZone, score: pl.score, awardDamage: pl.awardDamage,
        missileEvades: pl.missileEvades, shellInterceptions: pl.shellIntgerceptions,
        vehicles: sortedCraftVehicles(info.crafts),
        primaryCraftType: (ci0 && typeof ci0 === 'object') ? ci0.type : null
      });
    });
    return out;
  }

  function playerVehicles(results) {
    var out = [];
    playersInfoItems(results).forEach(function (val) {
      if (!val || typeof val !== 'object') return;
      var ci = val.crafts_info, ci0 = (ci && typeof ci === 'object') ? ci.array0 : null;
      var veh = [];
      var crafts = val.crafts;
      if (crafts && typeof crafts === 'object' && !Array.isArray(crafts)) {
        Object.keys(crafts).sort(cmpStr).forEach(function (slot) {
          veh.push({ slot: String(slot), craft: crafts[slot] });
        });
      }
      out.push({
        userId: String(val.id === undefined ? '' : val.id), name: val.name, clanTag: val.clanTag,
        team: val.team, slot: val.slot, country: val.country, tier: val.tier, vehicles: veh,
        primaryCraft: (ci0 && typeof ci0 === 'object') ? ci0.name : null,
        primaryCraftType: (ci0 && typeof ci0 === 'object') ? ci0.type : null
      });
    });
    return out;
  }

  /* ------------------------------------------------------------ 5. FlightModel
   * ptype=2：位打包条目列表 (uid, PosX, PosY, PosZ, packedEuler)。
   * 坐标映射：x=PosX, y=PosZ, z=PosY（米）。 */

  function readUnk5(r) {
    if (!r.bits(1)) return;
    r.bits(1); r.bits(1); r.bits(1);
    var bl = r.bits(4);
    for (var i = 0; i < bl; i++) r.bits(1);
  }

  function parseEngines(r) {
    var n = r.byte();
    if (n > 0xF) throw new Error('engines>15');
    for (var i = 0; i < n; i++) {
      var has = r.bits(1); r.byte();
      if (has) r.u16();
      if (r.bits(1)) r.byte();
      r.byte(); r.byte();
    }
  }

  function readSensor(r) {
    var first = r.bits(1);
    var st = r.byte();
    var t = st >> 4;
    if (t === 1) {
      if (!r.bits(1)) return;
      var u1 = r.u16(); r.u32(); r.u16(); r.u16(); r.u16();
      if (u1 >= 0x8000) r.byte();
      if (r.bits(1)) r.byte();
    } else if (t === 2) {
      if (!first) return;
      r.bits(1); r.u16(); r.bytesN(12); r.bytesN(12); r.u32();
    } else if (t === 3) {
      throw new Error('sensor type 3');
    } else if (t === 4) {
      if (r.bits(1)) r.bytesN(12);
    }
    if (r.bits(1)) {
      var n = r.bits(6);
      for (var i = 0; i < n; i++) r.u32();
      r.bits(6);
    }
  }

  function readSensors(r) {
    var cnt = r.byte();
    if (cnt > 4) throw new Error('sensors>4');
    for (var i = 0; i < cnt; i++) readSensor(r);
    return cnt;
  }

  function readCm(r) {
    var n = r.byte();
    for (var i = 0; i < n; i++) { r.byte(); r.byte(); }
    return n;
  }

  function readTarget(r) {
    r.byte(); r.byte();
    if (r.bits(1)) r.bytesN(12);
    if (r.bits(1)) { r.bytesN(12); r.bytesN(6); }
    else { r.bytesN(12); r.bytesN(12); }
    r.u32();
    r.bits(1); r.bits(1);
    if (r.bits(1)) r.byte();
    r.bits(1); var u15 = r.bits(1); r.bits(1);
    if (u15) r.byte();
    if (r.bits(1)) r.u32();
  }

  function readTargets(r) {
    var n = r.bits(4);
    if (n > 8) throw new Error('targets>8');
    for (var i = 0; i < n; i++) readTarget(r);
  }

  /** 紧凑欧拉角 -> (heading, attitude, bank)，弧度。 */
  function unpackEuler(packed) {
    var heading = ((packed >>> 19) & 0x3FF) * Math.PI / 0x3FF;
    var attitude = ((packed >>> 10) & 0x1FF) * (Math.PI / 2) / 0x1FF;
    var bank = (packed & 0x3FF) * Math.PI / 0x3FF;
    if (packed & (1 << 31)) heading = -heading;
    if (packed & (1 << 30)) attitude = -attitude;
    if (packed & (1 << 29)) bank = -bank;
    return [heading, attitude, bank];
  }

  function parseFm(payload) {
    var r = new BitReader(payload), entries = [], uid = 0;
    try {
      while (true) {
        if (r.bits(1)) uid = r.compressed() & 0x7FF;
        else uid += 1;
        if (uid === 0x7FF) break;
        if (r.bits(1)) { entries.push({ uid: uid, noData: true }); continue; }
        var u0 = r.bits(1), u1 = r.bits(1);
        if (u0 && u1) { entries.push({ uid: uid, skip: true }); continue; }
        r.bits(1); r.u32();
        r.bits(1); readUnk5(r);
        var n11 = r.zigzag32();
        if (n11 < 0) throw new Error('n11<0');
        for (var i = 0; i < n11; i++) {
          r.zigzag32();
          var t1 = r.zigzag32();
          for (var j = 0; j < Math.max(0, t1); j++) r.zigzag32();
        }
        var px = r.f32(), py = r.f32(), pz = r.f32();
        var euler = r.u32();
        r.u32();
        r.align();
        r.bytesN(7);
        parseEngines(r);
        if (readSensors(r) > 0) r.byte();
        if (readCm(r) > 0) r.byte();
        readTargets(r);
        if (r.bits(1)) { r.u16(); r.u16(); }
        if (r.bits(1)) r.skip(r.u32());
        entries.push({ uid: uid, pos: [px, py, pz], euler: euler });
      }
    } catch (ex) {
      entries.push({ err: String(ex && ex.message ? ex.message : ex) });
    }
    return entries;
  }

  /* ------------------------------------------------------------ 6. 击杀/伤害
   * IdFieldSerializer32：u16 LE 尺寸表偏移 + LEB128 位掩码 + 位宽表。 */

  function ifsReadSize(r) {
    var hdr = r.bits(3);
    var table = { 1: 1, 2: 8, 3: 16, 4: 32, 5: 64, 6: 96, 7: 128 };
    if (table[hdr]) return table[hdr];
    return r.compressed();
  }

  function ifsDeserialize(r, handler) {
    var start = r.pos;
    if (start & 7) throw new Error('IdFieldSerializer32 未按字节对齐');
    var off = r.u16();
    var fields = r.compressed();          // 位掩码（可能是大整数：用浮点累加）
    var startBody = r.pos;
    r.pos = start + off * 8;
    var nSizes = 0, f = fields;
    while (f > 0) { nSizes += (f % 2); f = Math.floor(f / 2); }
    var sizes = [];
    for (var i = 0; i < nSizes; i++) sizes.push(ifsReadSize(r));
    r.pos = startBody;
    var index = 0;
    f = fields;
    var rest = fields;
    while (rest > 0) {
      // 最低置位
      var k = 0, t = rest;
      while (t % 2 === 0) { t = Math.floor(t / 2); k++; }
      rest -= Math.pow(2, k);
      var sz = index < sizes.length ? sizes[index] : 0;
      if (!handler(k, sz)) r.pos += sz;
      index += 1;
    }
    return fields;
  }

  function readLenStr(r) {
    var n = r.byte();
    var b = r.bytesN(n);
    return utf8(b, 0, b.length);
  }

  function parseKillPayload(pl) {
    var r = new BitReader(pl.subarray(4));
    var rec = {};
    try {
      ifsDeserialize(r, function (num) {
        if (num === 1) rec.killerPid = r.u32();
        else if (num === 2) rec.killerVehicle = readLenStr(r);
        else if (num === 3) rec.victimUid = r.u16() & 0x7FF;
        else if (num === 4) rec.killerUid = r.u16() & 0x7FF;
        else if (num === 0xA) rec.weapon = readLenStr(r);
        else if (num === 0xB) rec.victimPid = r.u32();
        else if (num === 0xC) rec.destroyedWeapon = readLenStr(r);
        else return false;
        return true;
      });
    } catch (ex) { rec.err = String(ex && ex.message ? ex.message : ex); }
    return rec;
  }

  function parseDamagePayload(pl, severe) {
    var r = new BitReader(pl.subarray(4));
    var rec = {};
    try {
      ifsDeserialize(r, function (num) {
        if (num === 1) rec.offendedUid = r.u16();
        else if (num === 2) rec.playerPid = r.u32();
        else if (num === 3) rec.vehicle = readLenStr(r);
        else if (num === 4) rec.playerUid = r.u16();
        else if (num === 5 && !severe) rec.fire = !!r.bits(1);
        else if (num === 5 && severe) rec.unk0 = r.bits(8);
        else if (num === 6 && !severe) rec.unk0 = r.bits(8);
        else return false;
        return true;
      });
    } catch (ex) { rec.err = String(ex && ex.message ? ex.message : ex); }
    return rec;
  }

  /** 找「1 字节长度前缀 + ASCII 串」（wrpl_header.find_len_prefixed_ascii）。 */
  function findLenPrefixedAscii(buf, start, minLen, maxLen) {
    minLen = minLen || 3; maxLen = maxLen || 64;
    for (var i = start; i < buf.length; i++) {
      var ln = buf[i];
      if (ln >= minLen && ln <= maxLen && i + 1 + ln <= buf.length) {
        var ok = true;
        for (var k = 0; k < ln; k++) { var c = buf[i + 1 + k]; if (c < 0x20 || c >= 0x7F) { ok = false; break; } }
        if (ok) return [i, decodeAscii(buf, i + 1, i + 1 + ln)];
      }
    }
    return [-1, null];
  }

  /* ------------------------------------------------------------ 7. ECS */

  function classifyTemplate(name) {
    if (PROJECTILE_TEMPLATES[name]) return 'projectile';
    if (name === 'damage_area') return 'explosion';
    if (AIR_TEMPLATE_NAMES[name]) return 'aircraft';
    if (name === 'tank' || name === 'light_vehicle' || name === 'ship') return 'ground';
    if (name === 'fortification') return 'structure';
    if (name === 'sync_player') return 'player_slot';
    if (MOD_TEMPLATES[name]) return 'mod';
    if (SCENERY_TEMPLATES[name]) return 'scenery';
    return 'misc';
  }

  function blockStrings(blk, minlen, maxlen) {
    minlen = minlen || 3; maxlen = maxlen || 60;
    var out = [], i = 0;
    while (i < blk.length - 1) {
      var L = blk[i];
      if (L >= minlen && L <= maxlen && i + 1 + L <= blk.length) {
        var ok = true;
        for (var k = 0; k < L; k++) { var c = blk[i + 1 + k]; if (c < 32 || c >= 127) { ok = false; break; } }
        if (ok) { out.push(decodeAscii(blk, i + 1, i + 1 + L)); i += 1 + L; continue; }
      }
      i += 1;
    }
    return out;
  }

  /** 地面单位的真实 uid / team（**实测布局，不是猜**）。
   *  6 份回放（TEST0–5）共 715 个能对上结算的地面单位，与上游 wrpl-acmi 的
   *  `ecs:team` 结果逐单位一致（0 分歧）：
   *    - 块里型号串会出现 1–2 次（`tankModels/…` 或 `structures/…`）；
   *    - 第一次出现的起点 − 60 = uid（int32 LE，0–2047）；
   *    - 最后一次出现的起点 − 5 = team（int32 LE，1=蓝 2=红）。
   *  任一项越界/不合理就丢掉该项；两项都拿不到返回 null，交给原来的推断，不猜。 */
  function s32at(d, off) { return u32le(d, off) | 0; }

  /* 地面型号串的路径前缀（实测：坦克/工事/防空炮各一套）。 */
  var GROUND_MODEL_PREFIXES = ['tankModels/', 'structures/', 'air_defence/'];

  function groundModelOffsets(blk) {
    var out = [];
    for (var i = 0; i + 11 < blk.length; i++) {
      var hit = 0;
      for (var p = 0; p < GROUND_MODEL_PREFIXES.length; p++) {
        var pre = GROUND_MODEL_PREFIXES[p];
        if (i + pre.length >= blk.length) continue;
        var okPre = true;
        for (var q = 0; q < pre.length; q++) {
          if (blk[i + q] !== pre.charCodeAt(q)) { okPre = false; break; }
        }
        if (okPre) { hit = pre.length; break; }
      }
      if (!hit) continue;
      var j = i + hit;
      while (j < blk.length) {
        var c = blk[j];
        var ok = (c >= 0x30 && c <= 0x39) || (c >= 0x41 && c <= 0x5A) ||
                 (c >= 0x61 && c <= 0x7A) || c === 0x5F || c === 0x2F || c === 0x2E || c === 0x2D;
        if (!ok) break;
        j++;
      }
      if (j > i + 11) out.push(i);
      i = j - 1;
    }
    return out;
  }

  function groundIdentity(blk) {
    var offs = groundModelOffsets(blk);
    if (!offs.length) return null;
    var res = {};
    if (offs[0] >= 60) {
      var uid = s32at(blk, offs[0] - 60);
      if (uid >= 0 && uid < GROUND_UID_MAX) res.uid = uid;
    }
    var last = offs[offs.length - 1];
    if (last >= 5) {
      var team = s32at(blk, last - 5);
      if (team === 1 || team === 2) res.team = team;
    }
    return (res.uid === undefined && res.team === undefined) ? null : res;
  }

  function findGroundClassname(blk) {
    for (var i = 0; i < Math.max(0, blk.length - 6); i++) {
      var L = blk[i];
      if (L < 5 || L > 80 || i + 1 + L > blk.length) continue;
      var ok = true;
      for (var k = 0; k < L; k++) { var c = blk[i + 1 + k]; if (c < 32 || c >= 127) { ok = false; break; } }
      if (!ok) continue;
      var s = decodeAscii(blk, i + 1, i + 1 + L);
      if (GCLASS.test(s)) return s;
    }
    return null;
  }

  /** transform 的四元数 → 航向（**度，0=正北、顺时针**，与空中 units[].hdg 同口径）。
   *  四元数分量顺序 (x, y, z, w)；世界系 x=东、y=北、z=上（与位置同一套轴）。
   *  公式：heading = atan2(−R[2][2], R[0][2])，其中
   *    R[2][2] = 1 − 2(x²+y²)、R[0][2] = 2(xz + wy)。
   *  依据（可复算）：上游 wrpl-ecs 的 ecs_entities.json 给 184 个地面/工事实体算了
   *  hdgDeg（他们用本机 uid-368 的 8111 compass=90.0° 锚定），用本式对 184/184 全部
   *  命中 ≤0.05°（中位 0.025°、最大 0.050°）；本机 uid-368 的四元数 (0,1,0,0) → 90.00°，
   *  正好等于那个锚点。所以这里不是另立口径，而是对齐上游。 */
  function quatHeadingDeg(q) {
    var x = q[0], y = q[1], z = q[2], w = q[3];
    var a = 2 * (x * x + y * y) - 1;      /* = −R[2][2] */
    var b = 2 * (x * z + w * y);          /* =  R[0][2] */
    var d = Math.atan2(a, b) * 180.0 / Math.PI;
    return (d % 360.0 + 360.0) % 360.0;
  }

  /** 从块里读 transform 的四元数（组件值起点 + QUATERNION_REL），无效返回 null。 */
  function readTransformQuat(blk, vs) {
    if (vs + QUATERNION_REL + 16 > blk.length) return null;
    var q = [f32at(blk, vs + QUATERNION_REL), f32at(blk, vs + QUATERNION_REL + 4),
             f32at(blk, vs + QUATERNION_REL + 8), f32at(blk, vs + QUATERNION_REL + 12)];
    for (var i = 0; i < 4; i++) {
      if (!isFinite(q[i]) || Math.abs(q[i]) > 1.001) return null;
    }
    var n = Math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]);
    if (!(n > 0.97 && n < 1.03)) return null;
    return q;
  }

  function findProjectileTransform(blk, vs) {
    var n = blk.length;
    for (var rel = 0; rel < Math.max(0, n - vs - 16); rel++) {
      var o = vs + rel;
      if (o + 16 > n) break;
      var q = [f32at(blk, o), f32at(blk, o + 4), f32at(blk, o + 8), f32at(blk, o + 12)];
      var bad = false;
      for (var i = 0; i < 4; i++) if (!isFinite(q[i]) || Math.abs(q[i]) > 1.001) { bad = true; break; }
      if (bad) continue;
      var nrm = Math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]);
      if (!(nrm > 0.98 && nrm < 1.02)) continue;
      var op = o - 12;
      if (op < 0) continue;
      var x = f32at(blk, op), up = f32at(blk, op + 4), z = f32at(blk, op + 8);
      if (!isFinite(x) || !isFinite(up) || !isFinite(z)) continue;
      if (Math.abs(x) > 45000 || Math.abs(z) > 45000 || !(up >= -500 && up <= 16000)) continue;
      if (Math.max(Math.abs(x), Math.abs(z)) < 100 && Math.abs(up) < 100) continue;
      return { pos: [rnd(x, 2), rnd(up, 2), rnd(z, 2)],
               quat: [rnd(q[0], 6), rnd(q[1], 6), rnd(q[2], 6), rnd(q[3], 6)], rel: rel };
    }
    return { pos: null, quat: null, rel: null };
  }

  /** LZ4 block 解压（控制码 0x25 的载荷；对应 Python 的 lz4.block.decompress）。
   *  LZ4 block 格式：token(高 4 位=literal 长度, 低 4 位=match 长度-4) + literals +
   *  u16 LE offset + match；长度满 15 时继续按 255 累加。 */
  function lz4BlockDecompress(src, capacity) {
    var dst = new Uint8Array(capacity);
    var s = 0, d = 0, n = src.length;
    while (s < n) {
      var token = src[s++];
      var litLen = token >> 4;
      if (litLen === 15) {
        var b;
        do { b = src[s++]; litLen += b; } while (b === 255);
      }
      for (var i = 0; i < litLen; i++) {
        if (d >= capacity) throw new Error('lz4 输出超过容量');
        dst[d++] = src[s++];
      }
      if (s >= n) break;                       // 末尾只有 literals
      var offset = src[s] | (src[s + 1] << 8);
      s += 2;
      var matchLen = token & 0x0F;
      if (matchLen === 15) {
        var b2;
        do { b2 = src[s++]; matchLen += b2; } while (b2 === 255);
      }
      matchLen += 4;
      var from = d - offset;
      if (from < 0) throw new Error('lz4 offset 越界');
      for (var k = 0; k < matchLen; k++) {
        if (d >= capacity) throw new Error('lz4 输出超过容量');
        dst[d++] = dst[from + k];
      }
    }
    return dst.subarray(0, d);
  }

  /** ECS 解析器：只取下游要用的位置/uid/className/spawnSlotName，不解析组件值本身。 */
  function EcsParser() {
    this.templates = {};     // tid -> {id,name,comps}
    this.compdefs = {};      // cid -> [dch, cch]
    this.creations = [];     // {tick,eid,tid,name,bs,vstart,block}
    this.destructions = [];  // {tick,eid}：控制码 0x26（实体销毁），用于击毁/失效时刻
    this.stats = { plainPackets: 0, lz4Packets: 0, lz4Fail: 0, constructs: 0,
                   constructFail: 0, destructFail: 0, lastError: null };
  }

  EcsParser.prototype.parseTemplate = function (br) {
    var tid = br.compressed();
    var t = this.templates[tid];
    if (t) return t;
    var name = readLenStr(br);
    var ncomp = br.u16();
    var comps = [];
    for (var i = 0; i < ncomp; i++) {
      var cid = br.compressed();
      if (!Object.prototype.hasOwnProperty.call(this.compdefs, cid)) {
        this.compdefs[cid] = [br.u32(), br.u32()];
      }
      comps.push(cid);
    }
    t = { id: tid, name: name, comps: comps };
    this.templates[tid] = t;
    return t;
  };

  EcsParser.prototype.parseConstruct = function (r, tick) {
    var eid = r.eid();
    var bs = r.compressed();
    if (bs === 0 || br_blockGuard(bs)) throw new Error('block size 异常');
    var block = r.bytesN(bs);
    var br = new BitReader(block);
    var t = this.parseTemplate(br);
    var tcc = t.comps.length;
    if (tcc < 256) br.byte(); else br.compressed();
    var vstart = br.pos >> 3;
    this.creations.push({ tick: tick, eid: eid, tid: t.id, name: t.name, bs: bs,
                          vstart: vstart, block: block });
  };

  function br_blockGuard(bs) { return !(bs > 0 && bs < 4000000); }

  EcsParser.prototype.parsePacket = function (payload, tick) {
    var r = new BitReader(payload);
    var control = r.byte();
    if (control === 0x26) {
      /* 销毁：control、count（+1）、EID 列表。EID 与构造消息里的 EID 同一套编码。 */
      var dn = r.byte() + 1;
      for (var di = 0; di < dn; di++) {
        try { this.destructions.push({ tick: tick, eid: r.eid() }); }
        catch (e) {
          this.stats.destructFail++;
          this.stats.lastError = String(e && e.message ? e.message : e);
          break;
        }
      }
      return control;
    }
    if (control === 0x25) {
      // 压缩的构造消息：解 LZ4 block 后，块首字节是「构造条数」，其后是构造消息
      this.stats.lz4Packets++;
      var body;
      try {
        body = lz4BlockDecompress(payload.subarray(1), Math.max(64, (payload.length - 1) * 32));
      } catch (ex) {
        this.stats.lz4Fail++;
        this.stats.lastError = String(ex && ex.message ? ex.message : ex);
        return control;
      }
      r = new BitReader(body);
      control = 0x24;
      this.stats.constructs = this.stats.constructs;
    } else if (control !== 0x24) {
      return control;   // 0x20/0x22/0x23/0x26/0x21：本局不含实体构造
    } else {
      this.stats.plainPackets++;
    }
    var n = r.byte();
    for (var i = 0; i <= n; i++) {
      try { this.parseConstruct(r, tick); }
      catch (ex) {
        this.stats.constructFail++;
        this.stats.lastError = String(ex && ex.message ? ex.message : ex);
        break;
      }
    }
    return control;
  };

  /* ------------------------------------------------------------ 8. uid → 载具/玩家/阵营 */

  function buildUidMap(ecs, players, results, ownUid, slotPlayers) {
    var byTv = {}, nPlayers = players.length;
    players.forEach(function (p) {
      (p.vehicles || []).forEach(function (v) {
        var k = String(p.team) + '\u0000' + v;
        (byTv[k] = byTv[k] || []).push(p.name);
      });
    });
    var authorId = String(results.authorUserId || '');
    var author = results.author;
    var authorTeam = null, authorVeh = null;
    for (var i = 0; i < players.length; i++) {
      var p = players[i];
      if ((authorId && String(p.userId) === authorId) || (author && p.name === author)) {
        authorTeam = p.team; authorVeh = p.vehicles; break;
      }
    }

    // missionName -> uid（大块读不到 uid 时的兜底链）
    var missionUid = {};
    ecs.creations.forEach(function (c) {
      if (!AIR_TEMPLATE_NAMES[c.name]) return;
      if (c.vstart + UID_REL_OFFSET + 2 > c.block.length) return;
      var raw = u16le(c.block, c.vstart + UID_REL_OFFSET);
      if (!(raw >= 1 && raw <= 2046)) return;
      var m = MISSION_RE.exec(decodeAscii(c.block, 0, Math.min(c.block.length, 4096)));
      if (m) {
        var key = m[0];
        if (!(key in missionUid)) missionUid[key] = raw & 0x7FF;
      }
    });

    var recsOrder = [], recs = {};
    ecs.creations.forEach(function (c) {
      if (!AIR_TEMPLATE_NAMES[c.name]) return;
      var uidRaw = null;
      if (c.vstart + UID_REL_OFFSET + 2 <= c.block.length) {
        uidRaw = u16le(c.block, c.vstart + UID_REL_OFFSET);
      }
      var ascii = decodeAscii(c.block, 0, Math.min(c.block.length, 4096));
      var m = MISSION_RE.exec(ascii);
      if (!(uidRaw !== null && uidRaw >= 1 && uidRaw <= 2046) && m) {
        uidRaw = (m[0] in missionUid) ? missionUid[m[0]] : null;
      }
      var team = m ? parseInt(m[1], 10) : null;
      var slot = m ? m[0] : null;
      var cls = null;
      var re = /[\x20-\x7e]{4,}/g, mm;
      while ((mm = re.exec(ascii)) !== null) {
        var s = mm[0];
        if (s === 'dummy_plane' || s === c.name) continue;
        if (s.indexOf('player') >= 0 || s.indexOf('aircraft') >= 0 || s.indexOf('+') >= 0) continue;
        if (VEH_RE.test(s)) { cls = s; break; }
      }
      var key = String(uidRaw);
      if (!(key in recs)) { recs[key] = []; recsOrder.push(key); }
      var pid = null;
      if (c.vstart + 119 + 4 <= c.block.length) {
        pid = s32at(c.block, c.vstart + 119);
        if (!(pid >= 0 && pid < 256)) pid = null;
      }
      recs[key].push({ eid: c.eid, tick: c.tick, team: team, slot: slot,
                       className: cls, blockSize: c.bs, valuesStart: c.vstart,
                       playerId: pid });
    });

    var vehTeams = {};
    Object.keys(byTv).forEach(function (k) {
      var parts = k.split('\u0000'), tm = parts[0], v = parts[1];
      if (!v) return;
      (vehTeams[v] = vehTeams[v] || {})[tm] = 1;
    });

    var out = {}, stat = { uid_invalid: 0, no_vehicle: 0, team_ambiguous_by_vehicle: 0,
                           player_ambiguous: 0, player_unique: 0 };
    recsOrder.forEach(function (key) {
      var uidRaw = key === 'null' ? null : parseInt(key, 10);
      var lst = recs[key];
      if (uidRaw === null || !(uidRaw >= 1 && uidRaw <= 2046)) { stat.uid_invalid++; return; }
      var uid = uidRaw & 0x7FF;
      if (uid === 0x7FF) { stat.uid_invalid++; return; }
      var good = lst.filter(function (r) { return r.className && r.className !== 'dummy_plane'; });
      var pool = good.length ? good : lst;
      var pick = pool[0];
      pool.forEach(function (r) { if (r.blockSize > pick.blockSize) pick = r; });
      var mteam = pick.team !== null ? pick.team
        : (lst.filter(function (r) { return r.team !== null; })[0] || {}).team;
      if (mteam === undefined) mteam = null;
      var slot = pick.slot || (lst.filter(function (r) { return r.slot; })[0] || {}).slot || null;
      var vehicle = (pick.className && pick.className !== 'dummy_plane') ? pick.className : null;
      var tcand = vehicle ? Object.keys(vehTeams[vehicle] || {}).map(function (x) { return parseInt(x, 10); }) : [];
      tcand.sort(function (a, b) { return a - b; });
      var pteam = tcand.length === 1 ? tcand[0] : null;
      var cand = (pteam !== null && vehicle) ? sortedUnique(byTv[String(pteam) + '\u0000' + vehicle] || []) : [];
      if (!cand.length && vehicle) {
        var set = {};
        Object.keys(byTv).forEach(function (k) {
          var parts = k.split('\u0000');
          if (parts[1] === vehicle) (byTv[k] || []).forEach(function (n) { set[n] = 1; });
        });
        cand = Object.keys(set).sort(cmpStr);
      }
      var player = cand.length === 1 ? cand[0] : null;
      if (vehicle === null) stat.no_vehicle++;
      if (tcand.length > 1) stat.team_ambiguous_by_vehicle++;
      if (cand.length > 1) stat.player_ambiguous++;
      if (cand.length === 1) stat.player_unique++;
      var side = null;
      if (pteam !== null && authorTeam !== null && authorTeam !== undefined) {
        side = pteam === authorTeam ? 'ally' : 'enemy';
      }
      var slotHit = null;
      if (slotPlayers) {
        /* 同一 uid 可能有多个构造实例；只要任一实例带有效 unit__playerId 且能对上玩家表就采用 */
        for (var li = 0; li < lst.length && !slotHit; li++) {
          var pid = lst[li].playerId;
          if (pid === null || pid === undefined) continue;
          var sp = slotPlayers[pid];
          if (sp && sp.name && (sp.team === 1 || sp.team === 2)) slotHit = sp;
        }
      }
      if (slotHit) {
        player = slotHit.name;
        pteam = slotHit.team;
        side = (authorTeam !== null && authorTeam !== undefined) ? (pteam === authorTeam ? 'ally' : 'enemy') : null;
        tcand = [pteam];
        cand = [player];
      }
      out[String(uid)] = {
        entityIndex: pick.eid & 0x3FFFFF, entityId: '0x' + pick.eid.toString(16),
        player: player, playerCandidates: cand, playerUserId: slotHit ? String(slotHit.playerId) : null,
        playerClan: slotHit ? (slotHit.clan || null) : null,
        vehicle: vehicle, team: pteam,
        teamCandidates: tcand, side: side,
        spawnGroup: mteam !== null && mteam !== undefined ? ('t' + mteam) : null,
        spawnSlotName: slot, spawnGroupIsTeam: false, spawnTick: pick.tick,
        instances: lst.length,
        sideSource: slotHit ? 'ecs:unit__playerId → slot team'
          : (pteam ? 'vehicle→结算玩家表 team' : 'unresolved'),
        source: slotHit
          ? 'wrpl ptype=4 slot(0xF02D) 玩家表 + ECS unit__playerId（组件值起点+119）'
          : 'wrpl ptype=6 ECS 实体构造块（uid=值起点+' + UID_REL_OFFSET +
            ' 的 u16 低 11 位；vehicle=unit__className；spawnSlotName=t[12]_playerNN_K）',
        confidence: slotHit ? 'high' : ((vehicle && player) ? 'high' : (vehicle ? 'medium' : 'low'))
      };
    });

    if (ownUid && out[String(ownUid)]) {
      var o = out[String(ownUid)];
      var av = (authorVeh || [])[0] || null;
      o.vehicle = o.vehicle || av;
      if (av) {
        var tc = Object.keys(vehTeams[av] || {}).map(function (x) { return parseInt(x, 10); }).sort(function (a, b) { return a - b; });
        if (authorTeam !== null && tc.indexOf(authorTeam) >= 0) {
          o.team = authorTeam; o.teamCandidates = tc; o.side = 'ally';
          o.sideSource = 'results.author + vehicle';
          o.player = null;
          o.playerCandidates = sortedUnique(
            Object.keys(byTv).filter(function (k) {
              var p2 = k.split('\u0000');
              return p2[1] === av && String(p2[0]) === String(authorTeam);
            }).reduce(function (acc, k) { return acc.concat(byTv[k]); }, []));
          var arow = players.filter(function (q) { return authorId && String(q.userId) === authorId; });
          if (arow.length && o.playerCandidates.indexOf(arow[0].name) >= 0) {
            o.player = arow[0].name;
            o.playerSource = 'results.author（本机身份由回放结算表的 author 锚定）';
          }
        }
      }
      o.vehicleSource = o.vehicleSource || 'results.author（本局结算 BLK）';
      stat.own_uid_anchored = ownUid;
    }
    stat.uids = Object.keys(out).length;
    stat.authorTeam = authorTeam;
    return { map: out, stat: stat };
  }

  function sortedUnique(arr) {
    var s = {};
    arr.forEach(function (x) { s[x] = 1; });
    return Object.keys(s).sort(cmpStr);
  }

  /* ------------------------------------------------------------ 9. 跑道标记点 */

  function extractRunwayMarkers(buf, pk) {
    var out = {};
    for (var i = 0; i < pk.length; i++) {
      var p = pk[i];
      if (p.ptype !== 4) continue;
      var pl = packetPayload(buf, p);
      // 旧记法 00025839 == 新口径 pl[0:4] = 02 58 39 d0（msgId 0xD039）
      if (!(pl[0] === 0x02 && pl[1] === 0x58 && pl[2] === 0x39 && pl[3] === 0xd0)) continue;
      for (var mi = 0; mi < RUNWAY_MARKERS.length; mi++) {
        var nm = RUNWAY_MARKERS[mi];
        var nb = nm.length;
        var idx = -1;
        for (var s = 0; s + 1 + nb <= pl.length; s++) {
          if (pl[s] === nb && decodeAscii(pl, s + 1, s + 1 + nb) === nm) { idx = s + 1; break; }
        }
        if (idx < 0) continue;
        var end = idx + nb;
        var rec = out[nm] || (out[nm] = { pos: [], dir: null, ticks: [], model: null });
        rec.ticks.push(p.ms * TICK_PER_MS);
        var tailLen = Math.min(260, Math.max(0, pl.length - end));
        if (!tailLen) continue;
        var tail = pl.subarray(end, end + tailLen);
        // 方向：跳过一个长度前缀串（模型名），其后 3×f32 且模长≈1
        for (var skip = 0; skip < 4; skip++) {
          if (skip >= tail.length) break;
          var L = tail[skip];
          if (!(L >= 3 && L <= 80) || skip + 1 + L + 12 > tail.length) continue;
          var okAscii = true;
          for (var k = 0; k < L; k++) { var c = tail[skip + 1 + k]; if (c < 32 || c >= 127) { okAscii = false; break; } }
          if (!okAscii) continue;
          var model = decodeAscii(tail, skip + 1, skip + 1 + L);
          if (!/^[A-Za-z0-9_./+-]+$/.test(model)) continue;
          var o = skip + 1 + L;
          var v = [f32at(tail, o), f32at(tail, o + 4), f32at(tail, o + 8)];
          var nr = Math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
          if (nr > 0.97 && nr < 1.03) {
            rec.model = model;
            rec.dir = [rnd(v[0] / nr, 6), rnd(v[1] / nr, 6), rnd(v[2] / nr, 6)];
          }
          break;
        }
        // 位置：tail 里所有「像位置」的三连（同一标记点会重复出现）
        var lastO = -99;
        for (var oo = 0; oo < Math.max(0, tail.length - 12); oo++) {
          if (oo < lastO + 12) continue;
          var x = f32at(tail, oo), y = f32at(tail, oo + 4), z = f32at(tail, oo + 8);
          if (isFinite(x) && isFinite(y) && isFinite(z) &&
              Math.abs(x) < 45000 && Math.abs(z) < 45000 && y >= -300 && y <= 2500 &&
              Math.max(Math.abs(x), Math.abs(z)) > 500) {
            rec.pos.push([rnd(x, 2), rnd(y, 2), rnd(z, 2)]);
            lastO = oo;
          }
        }
      }
    }
    return out;
  }

  function buildRunwayData(structs, markers) {
    // 机场聚类（12 个机场结构 → 4 个机场；阈值 4000 m）
    var cl = [];
    structs.forEach(function (st) {
      if (st.x === null) return;
      for (var i = 0; i < cl.length; i++) {
        var c = cl[i];
        if (Math.hypot(c.cx - st.x, c.cy - st.y) < 4000) {
          c.m.push(st);
          c.cx = c.m.reduce(function (a, q) { return a + q.x; }, 0) / c.m.length;
          c.cy = c.m.reduce(function (a, q) { return a + q.y; }, 0) / c.m.length;
          return;
        }
      }
      cl.push({ cx: st.x, cy: st.y, m: [st] });
    });
    cl.sort(function (a, b) { return b.m.length - a.m.length || 0; });
    var airfields = cl.map(function (c, i) {
      var names = sortedUnique(c.m.map(function (q) { return q.className; }));
      var withZ = c.m.filter(function (q) { return q.z !== null; });
      return {
        key: 'af-' + ('0' + (i + 1)).slice(-2),
        x: rnd(c.cx, 2), y: rnd(c.cy, 2),
        z: rnd(withZ.reduce(function (a, q) { return a + q.z; }, 0) / Math.max(1, c.m.length), 2),
        structureCount: c.m.length, classNames: names,
        evidence: '该处同时出现 unit_airfield_main_radar / flight_radar 结构', runway: null
      };
    });

    var runways = [];
    RUNWAY_MARKERS.forEach(function (nm) {
      var rec = markers[nm];
      if (!rec || !rec.pos.length || !rec.dir) return;
      var clusters = [];
      rec.pos.forEach(function (q) {
        for (var i = 0; i < clusters.length; i++) {
          var c = clusters[i];
          if (Math.hypot(c.c[0] - q[0], c.c[1] - q[1], c.c[2] - q[2]) < 30) {
            c.m.push(q);
            c.c = [0, 1, 2].map(function (k) {
              return c.m.reduce(function (a, x) { return a + x[k]; }, 0) / c.m.length;
            });
            return;
          }
        }
        clusters.push({ c: q, m: [q] });
      });
      clusters.sort(function (a, b) { return b.m.length - a.m.length || 0; });
      var main = clusters[0], ps = main.m, nSamples = ps.length;
      var cx = rnd(main.c[0], 2), cy = rnd(main.c[1], 2), cz = rnd(main.c[2], 2);
      var spread = 0;
      ps.forEach(function (q) {
        var d = Math.hypot(q[0] - cx, q[1] - cy, q[2] - cz);
        if (d > spread) spread = d;
      });
      var other = clusters.slice(1).reduce(function (a, c) { return a + c.m.length; }, 0);
      var d = rec.dir, de = d[0], dn = d[2];
      var nrm = Math.hypot(de, dn);
      if (nrm < 1e-6) return;
      de /= nrm; dn /= nrm;
      var half = RUNWAY_LENGTH_M / 2.0;
      var x1 = cx - de * half, z1 = cz - dn * half;
      var x2 = cx + de * half, z2 = cz + dn * half;
      var heading = (Math.atan2(de, dn) * 180.0 / Math.PI) % 360.0;
      if (heading < 0) heading += 360.0;
      var best = null, bd = 1e18;
      airfields.forEach(function (a) {
        var dd = Math.hypot(a.x - cx, a.y - cz);
        if (dd < bd) { bd = dd; best = a; }
      });
      var altOk = best !== null && Math.abs(best.z - cy) <= 30;
      var conf = (spread <= 60 && nSamples >= 2 && bd <= 3000 && altOk) ? 'high'
        : ((spread <= 200 && bd <= 5000) ? 'medium' : 'low');
      runways.push({
        key: 'rw-' + (best ? best.key : nm),
        name: (best ? best.key.toUpperCase() : nm) + ' 跑道',
        x1: rnd(x1, 2), y1: rnd(z1, 2), z1: cy, x2: rnd(x2, 2), y2: rnd(z2, 2), z2: cy,
        lengthM: RUNWAY_LENGTH_M, headingDeg: rnd(heading, 1),
        centerX: cx, centerY: cz, centerZ: cy, spawnPointsUsed: [nm],
        markerModel: rec.model, spreadM: rnd(spread, 2), positionSamples: nSamples,
        positionOutliersDropped: other, nearestAirfield: best ? best.key : null,
        distToAirfieldM: rnd(bd, 1), altitudeMatchesAirfield: altOk,
        source: 'ecs',
        sourceDetail: '00025839 单位定义包里的 t{1,2}_airfield_top[_reload] 标记点：位置(3×f32 重复采样) + 航向(3×f32 单位向量)；长度取自其引用的模型名 dynaf_pg_1line_3000_universal 的 3000',
        confidence: conf
      });
    });
    return { runways: runways, airfields: airfields };
  }

  /* ------------------------------------------------------------ 10. 时间轴 */

  function Axis(secPerTick, tick0, tickLast, rawPerMs) {
    this.sec_per_tick = secPerTick;
    this.tick0 = tick0;
    this.tick_last = tickLast;
    this.raw_per_ms = rawPerMs || 256;
    this.source = 'tracks.tick0=' + tick0 + '（与 tracks.json / 验收 4a 同轴）';
  }
  Axis.prototype.sec = function (tick) { return (tick - this.tick0) * this.sec_per_tick; };
  Axis.prototype.to_tick = function (sec) { return Math.round(this.tick0 + sec / this.sec_per_tick); };
  Axis.prototype.ms_to_tick = function (ms) { return Math.round(ms * this.raw_per_ms); };
  Axis.prototype.from_clock_sec = function (sec) { return sec - this.tick0 * this.sec_per_tick; };

  /** 一批「回放时钟秒」→ 交付轴秒（逐项减去 tick0×secPerTick）。 */
  function axisFromClock(times, axis) {
    var off = axis.tick0 * axis.sec_per_tick;
    var out = new Array(times.length);
    for (var i = 0; i < times.length; i++) out[i] = times[i] - off;
    return out;
  }

  /** 列 {times:时钟秒, values} → {times:轴秒, values}；列缺失返回 null。 */
  function axisFromClockSeries(col, axis) {
    if (!col || !col.times.length) return null;
    return { times: axisFromClock(col.times, axis), values: col.values };
  }

  /* ------------------------------------------------------------ 11. 序列工具 */

  function linearAt(times, values, t, maxGap) {
    if (!times.length || t < times[0] - 1e-9 || t > times[times.length - 1] + 1e-9) return null;
    var i = bisectLeft(times, t);
    if (i < times.length && Math.abs(times[i] - t) <= 1e-9) return values[i];
    if (i === 0 || i >= times.length) return null;
    var t1 = times[i - 1], t2 = times[i], v1 = values[i - 1], v2 = values[i];
    if (v1 === null || v2 === null || (t2 - t1) > maxGap) return null;
    return v1 + (v2 - v1) * (t - t1) / (t2 - t1);
  }

  function snapAt(times, values, t, tol) {
    if (!times.length) return null;
    var i = bisectLeft(times, t), best = -1, bestD = tol + 1e-9;
    [i - 1, i].forEach(function (k) {
      if (k >= 0 && k < times.length) {
        var d = Math.abs(times[k] - t);
        if (d < bestD) { best = k; bestD = d; }
      }
    });
    return best < 0 ? null : values[best];
  }

  function catmullRom(times, values, segIds, t, maxGap) {
    var i = bisectLeft(times, t);
    if (i === 0 || i >= times.length) return null;
    if (segIds[i - 1] !== segIds[i] || (times[i] - times[i - 1]) > maxGap) return null;
    var p1 = values[i - 1], p2 = values[i];
    if (p1 === null || p2 === null) return null;
    var p0 = (i - 2 >= 0 && segIds[i - 2] === segIds[i - 1]) ? values[i - 2] : p1;
    var p3 = (i + 1 < times.length && segIds[i + 1] === segIds[i]) ? values[i + 1] : p2;
    if (p0 === null) p0 = p1;
    if (p3 === null) p3 = p2;
    var u = (t - times[i - 1]) / (times[i] - times[i - 1]);
    var u2 = u * u, u3 = u2 * u;
    return 0.5 * ((2 * p1) + (-p0 + p2) * u + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u2 +
                  (-p0 + 3 * p1 - 3 * p2 + p3) * u3);
  }

  function envelopeFilter(x, y, z, hz, vmax) {
    vmax = vmax || TELEPORT_SPEED_MS;
    var dropped = 0, last = null;
    for (var i = 0; i < x.length; i++) {
      if (x[i] === null || y[i] === null) continue;
      if (last !== null) {
        var dt = (i - last[0]) / hz;
        var zz = z[i] === null ? 0.0 : z[i], lz = last[3] === null ? 0.0 : last[3];
        var d = Math.sqrt(Math.pow(x[i] - last[1], 2) + Math.pow(y[i] - last[2], 2) +
                          Math.pow(zz - lz, 2));
        if (dt > 0 && d / dt > vmax) {
          x[i] = null; y[i] = null; z[i] = null; dropped += 1; continue;
        }
      }
      last = [i, x[i], y[i], z[i]];
    }
    return dropped;
  }

  /* ------------------------------------------------------------ 12. 主流程 */

  function unitSamples(unit) {
    var rows = [];
    (unit.samples || []).forEach(function (s) {
      var tick = s[0], x = finiteM(s[1]), y = finiteM(s[2]), z = finiteM(s[3]);
      if (typeof tick !== 'number' || x === null || y === null) return;
      rows.push([tick, x, y, z]);
    });
    rows.sort(function (a, b) { return a[0] - b[0]; });
    return rows;
  }

  /** y 轴是否取反：本机位移方向必须与本机属性表（ReplayCockpitParams）里的**真罗盘**一致（纯回放判据）。
   *  pt 的时间轴必须是**轴秒**（已减 tick0）。 */
  function decideYFlip(unitRowsByUid, ownUid, pt, axis) {
    var rows = ownUid !== null ? (unitRowsByUid[ownUid] || null) : null;
    if (!rows || rows.length < 20) return { flip: false, no: null, fl: null, n: 0 };
    if (!pt || !pt.times.length) return { flip: false, no: null, fl: null, n: 0 };
    var no = [], fl = [];
    for (var i = 1; i < rows.length; i++) {
      var dt = (rows[i][0] - rows[i - 1][0]) * axis.sec_per_tick;
      if (dt < 0.2) continue;
      var h = snapAt(pt.times, pt.values, axis.sec(rows[i][0]), 0.4);
      if (h === null) continue;
      var dx = rows[i][1] - rows[i - 1][1], dy = rows[i][2] - rows[i - 1][2];
      if (Math.hypot(dx, dy) < 50.0) continue;
      no.push(Math.abs(wrap180(bearingDeg(dx, dy) - h)));
      fl.push(Math.abs(wrap180(bearingDeg(dx, -dy) - h)));
    }
    if (no.length < 20) return { flip: false, no: null, fl: null, n: no.length };
    no.sort(function (a, b) { return a - b; });
    fl.sort(function (a, b) { return a - b; });
    var medNo = no[no.length >> 1], medFl = fl[fl.length >> 1];
    return { flip: medFl < medNo, no: medNo, fl: medFl, n: no.length };
  }

  function envStatsOf(tracks, axis) {
    var violations = 0, teleports = 0, steps = 0;
    tracks.units.forEach(function (u) {
      var rows = unitSamples(u);
      for (var i = 1; i < rows.length; i++) {
        var dt = (rows[i][0] - rows[i - 1][0]) * axis.sec_per_tick;
        if (dt <= 0) continue;
        var dx = rows[i][1] - rows[i - 1][1], dy = rows[i][2] - rows[i - 1][2];
        var dz = (rows[i][3] === null || rows[i - 1][3] === null) ? 0 : rows[i][3] - rows[i - 1][3];
        var d = Math.sqrt(dx * dx + dy * dy + dz * dz);
        steps++;
        if (d / dt > 600.0) violations++;
        if (d > 5000.0) teleports++;
      }
    });
    return { violations: violations, teleports: teleports, steps: steps };
  }

  function buildOwn(tracks, axis, t1, ownSegs, ownProps, ysign) {
    var rows = [], segIds = [];
    ownSegs.forEach(function (segU, si) {
      unitSamples(segU).forEach(function (r) { rows.push(r); segIds.push(si); });
    });
    var order = rows.map(function (_, i) { return i; });
    order.sort(function (a, b) { return rows[a][0] - rows[b][0] || (a - b); });
    rows = order.map(function (i) { return rows[i]; });
    segIds = order.map(function (i) { return segIds[i]; });

    var n = Math.floor(t1 * HZ_OWN) + 1;
    var grid = [];
    for (var i = 0; i < n; i++) grid.push(i / HZ_OWN);
    var times = rows.map(function (r) { return axis.sec(r[0]); });
    var xs = rows.map(function (r) { return r[1]; });
    var ys = rows.map(function (r) { return r[2] * ysign; });
    var zs = rows.map(function (r) { return r[3]; });

    function interpTrack(values, t) {
      var i = bisectLeft(times, t);
      if (i < times.length && Math.abs(times[i] - t) <= 1e-9) return values[i];
      if (i === 0 || i >= times.length) return null;
      if (segIds[i - 1] !== segIds[i] || (times[i] - times[i - 1]) > MAX_INTERP_GAP_S) return null;
      var v1 = values[i - 1], v2 = values[i];
      if (v1 === null || v2 === null) return null;
      return v1 + (v2 - v1) * (t - times[i - 1]) / (times[i] - times[i - 1]);
    }

    var x = grid.map(function (t) { return rnd(interpTrack(xs, t), 0); });
    var y = grid.map(function (t) { return rnd(interpTrack(ys, t), 0); });
    var trackZ = grid.map(function (t) { return rnd(interpTrack(zs, t), 1); });

    function chan(col, nd, scale, circular) {
      scale = scale === undefined ? 1.0 : scale;
      var cs = ownProps[col];
      if (!cs || !cs.times.length) {
        return grid.map(function () { return null; });
      }
      // 本机属性表（ReplayCockpitParams）的时间轴是回放时钟秒；交付轴秒 = 时钟秒 - tick0×secPerTick
      var ctimes = axisFromClock(cs.times, axis);
      if (circular) {
        var sv = cs.values.map(function (v) { return v === null ? null : Math.sin(Math.PI / 180 * v); });
        var cv = cs.values.map(function (v) { return v === null ? null : Math.cos(Math.PI / 180 * v); });
        var sa = grid.map(function (t) { return linearAt(ctimes, sv, t, MAX_INTERP_GAP_S); });
        var ca = grid.map(function (t) { return linearAt(ctimes, cv, t, MAX_INTERP_GAP_S); });
        return grid.map(function (_, i) {
          if (sa[i] === null || ca[i] === null) return null;
          var d = Math.atan2(sa[i], ca[i]) * 180.0 / Math.PI % 360.0;
          return rnd(d < 0 ? d + 360.0 : d, nd);
        });
      }
      return grid.map(function (t) {
        var v = linearAt(ctimes, cs.values, t, MAX_INTERP_GAP_S);
        return v === null ? null : rnd(v * scale, nd);
      });
    }

    var propsZ = chan('alt_m', 1);
    var z = grid.map(function (_, i) { return propsZ[i] !== null ? propsZ[i] : trackZ[i]; });
    var hdg = chan('hdg_deg', 1, 1.0, true);
    if (!hdg.some(function (v) { return v !== null; })) {
      hdg = grid.map(function () { return null; });
      for (var i2 = 1; i2 < n; i2++) {
        var x0 = x[i2 - 1], x1 = x[i2], y0 = y[i2 - 1], y1 = y[i2];
        if (x0 === null || x1 === null || (x0 === x1 && y0 === y1)) continue;
        hdg[i2] = rnd(bearingDeg(x1 - x0, y1 - y0), 1);
      }
    }

    var own = {
      t0: 0.0, hz: HZ_OWN, n: n, x: x, y: y, z: z,
      ias: chan('ias_mps', 1), tas: chan('tas_mps', 1), mach: chan('mach', 3), hdg: hdg,
      roll: chan('roll_deg', 1), pitch: chan('pitch_deg', 1), g: chan('ny', 2),
      aoa: grid.map(function () { return null; }), ammo: grid.map(function () { return null; }),
      fuel: chan('fuel_kg', 0), vy: chan('vario_mps', 1),
      throttle: chan('throttle_0_1', 1, 100.0)
    };
    /* —— 剔除「已生成 ID、玩家还没控制」的前导原点占位 ——
     * 实测（2026.10.22 TEST1 / air_falklands）：本机 #0..#2 = (0,0)/(0,0.1)/(0,0.2)，
     * #3 起才是真实出生点（一跳 53 km）。这类采样会让模型僵在地图正中心。
     * 规则：只剔除**前导连续**的 |x|≤100 且 |y|≤100 的采样，并记录出生时刻/坐标。 */
    var spawnIdx = 0;
    while (spawnIdx < n && own.x[spawnIdx] !== null && own.y[spawnIdx] !== null &&
           Math.abs(own.x[spawnIdx]) <= 100 && Math.abs(own.y[spawnIdx]) <= 100) {
      spawnIdx++;
    }
    if (spawnIdx > 0 && spawnIdx < n) {
      var CH = ['x', 'y', 'z', 'ias', 'tas', 'mach', 'hdg', 'roll', 'pitch', 'g',
                'aoa', 'ammo', 'fuel', 'vy', 'throttle'];
      own.preSpawnCount = spawnIdx;
      own.spawnSec = +(spawnIdx / HZ_OWN).toFixed(2);
      own.spawnX = own.x[spawnIdx];
      own.spawnY = own.y[spawnIdx];
      own.spawnZ = own.z[spawnIdx];
      for (var ci = 0; ci < CH.length; ci++) {
        var kk = CH[ci];
        if (own[kk] && own[kk].length) { own[kk] = own[kk].slice(spawnIdx); }
      }
      own.n = own.x.length;
      own.t0 = own.spawnSec;               /* 采样数组已左移，时间基同步 */
    }

    var filtered = envelopeFilter(x, y, z, HZ_OWN);
    var gaps = [], maxGap = 0.0, bigGaps = 0;
    for (var gi = 1; gi < times.length; gi++) {
      var g0 = times[gi] - times[gi - 1];
      if (g0 > maxGap) maxGap = g0;
      if (g0 > MAX_INTERP_GAP_S) bigGaps++;
    }
    return { own: own, grid: grid, stats: { rows: rows.length, envelope_filtered: filtered,
             max_gap: maxGap, big_gaps: bigGaps } };
  }

  function buildUnits(tracks, axis, ownSet, ysign, uidMap, vHints, sideFinalMap, display,
                      deaths, hiddenAll, ownUid) {
    var out = [], counters = {}, dropped = [], breaks = [], filtered = 0;
    tracks.units.forEach(function (u) {
      if (ownSet[u.entityId]) return;
      var rows = unitSamples(u);
      if (rows.length < MIN_TRACK_SAMPLES) { dropped.push(u.entityId); return; }
      var domain = (u.domain === 'air' || u.domain === 'ground') ? u.domain : 'air';
      var ident = uidMap[String(u.uid)] || {};
      var sideDecoded = (ident.side && SIDE_CODE[ident.side]) ? ident.side
        : ((u.side && SIDE_CODE[u.side]) ? u.side : 'unknown');
      var baseId = String(u.entityId || '').split('#')[0];
      var sideFinalV = sideFinalMap[baseId];
      var side = (sideFinalV === 'ally' || sideFinalV === 'enemy' || sideFinalV === 'squad')
        ? sideFinalV : sideDecoded;
      var vehicle = ident.vehicle || u.vehicle || null;
      var vSource = ident.vehicle ? 'uid_map' : null;
      if (!vehicle) {
        var hint = vHints[String(u.uid)];
        if (hint) {
          var vs = Object.keys(hint.vehicles);
          if (vs.length === 1) { vehicle = vs[0]; vSource = Object.keys(hint.sources).sort(cmpStr).join('+'); }
        }
      }
      var player = ident.player || u.player || null;
      var hz = UNIT_HZ[domain];

      var segs = [[rows[0]]];
      for (var i = 1; i < rows.length; i++) {
        var dt = (rows[i][0] - rows[i - 1][0]) * axis.sec_per_tick;
        var dz = (rows[i][3] === null || rows[i - 1][3] === null) ? 0 : rows[i][3] - rows[i - 1][3];
        var d = Math.sqrt(Math.pow(rows[i][1] - rows[i - 1][1], 2) +
                          Math.pow(rows[i][2] - rows[i - 1][2], 2) + dz * dz);
        if (d > 5000.0 || (dt > 0 && d / dt > TELEPORT_SPEED_MS && d > TELEPORT_MIN_M)) {
          segs.push([rows[i]]);
          breaks.push([u.entityId, rnd(axis.sec(rows[i - 1][0]), 3), rnd(d, 1)]);
        } else segs[segs.length - 1].push(rows[i]);
      }

      var code = String(vehicle || domain).toLowerCase().replace(/[^a-z0-9]/g, '').slice(0, 7) || domain;
      var ckey = side + '|' + code;
      counters[ckey] = (counters[ckey] || 0) + 1;
      var baseKey = SIDE_CODE[side] + '-' + code + '-' + ('0' + counters[ckey]).slice(-2);

      segs.forEach(function (segRows, si) {
        if (segRows.length < MIN_TRACK_SAMPLES) {
          dropped.push(u.entityId + (si === 0 ? '' : '#' + (si + 1)));
          return;
        }
        var first = axis.sec(segRows[0][0]);
        var last = axis.sec(segRows[segRows.length - 1][0]);
        var n = Math.floor((last - first) * hz) + 1;
        var times = segRows.map(function (r) { return axis.sec(r[0]); });
        var colX = segRows.map(function (r) { return r[1]; });
        var colY = segRows.map(function (r) { return r[2] * ysign; });
        var colZ = segRows.map(function (r) { return r[3]; });
        var segIndex = times.map(function () { return 0; });
        var ux = [], uy = [], uz = [];
        for (var k = 0; k < n; k++) {
          var t = first + k / hz;
          ux.push(rnd(catmullRom(times, colX, segIndex, t, MAX_INTERP_GAP_S), 0));
          uy.push(rnd(catmullRom(times, colY, segIndex, t, MAX_INTERP_GAP_S), 0));
          uz.push(rnd(catmullRom(times, colZ, segIndex, t, MAX_INTERP_GAP_S), 1));
        }
        var uh = [];
        for (var q = 0; q < n; q++) uh.push(null);
        for (var i2 = 1; i2 < n; i2++) {
          if (ux[i2 - 1] === null || uy[i2 - 1] === null || ux[i2] === null || uy[i2] === null) continue;
          if (ux[i2] === ux[i2 - 1] && uy[i2] === uy[i2 - 1]) continue;
          uh[i2] = rnd(bearingDeg(ux[i2] - ux[i2 - 1], uy[i2] - uy[i2 - 1]), 1);
        }
        filtered += envelopeFilter(ux, uy, uz, hz);
        var tEnd = first + (n - 1) / hz;
        var hits = deaths.filter(function (dd) {
          return dd[2] === u.uid && dd[0] >= first - 0.5 && dd[0] <= tEnd + 1.0;
        });
        hits.sort(function (a, b) { return a[0] - b[0]; });
        var death = hits.length ? hits[0] : null;
        var vName = null;
        if (vehicle) vName = hiddenAll[vehicle] ? '' : (display[vehicle] || vehicle);
        out.push({
          key: si === 0 ? baseKey : baseKey + '#' + (si + 1),
          side: side, sideDecoded: sideDecoded, domain: domain,
          vehicle: vehicle, vehicleSource: vSource, vehicleName: vName,
          vehicleCategory: vehicle ? (VEHICLE_CATEGORIES[vehicle] || 'UNKNOWN') : null,
          hidden: !!hiddenAll[vehicle], template: null, class: 'aircraft',
          player: player, hasAlt: true, t0: rnd(first, 3), hz: hz, n: n,
          x: ux, y: uy, z: uz, hdg: uh,
          entityId: u.entityId, uid: u.uid, segment: si + 1, instances: ident.instances,
          deathTickRaw: death ? death[1] : null,
          deathKind: death ? ((death[3] === 2047 || death[3] === null) ? 'no_killer' : 'shot_down') : null,
          killerUid: (death && death[3] !== 2047) ? death[3] : null
        });
      });
    });
    out.sort(function (a, b) { return (a.t0 - b.t0) || cmpStr(a.key, b.key); });
    return { units: out, dropped: dropped, breaks: breaks, filtered: filtered };
  }

  function groundSide(x, y, centers) {
    if (!centers || x === null || y === null) return 'unknown';
    var best = null, bd = null;
    Object.keys(centers).forEach(function (name) {
      var c = centers[name];
      if (!c || c.length < 2) return;
      var d = Math.hypot(x - c[0], y - c[1]);
      if (bd === null || d < bd) { bd = d; best = name; }
    });
    return (best === 'ally' || best === 'enemy' || best === 'squad') ? best : 'unknown';
  }

  /* ------------------------------------------------ 12. 地面同步位置（0xF073）
   * 每条 ptype=4 包体：签名 02 58 73 f0 / 02 58 74 f0，其后是
   *   [ULEB 位长][字节对齐][uid 下界 u16][上界 u16][同步时刻 u32][可靠性序号 u16]
   *   [每 uid：标志位 + 增量块（RLE 0ki + 与前次 XOR）]
   * 位置字段与飞机同坐标系（x=世界 X，y=高度，z=世界 Z）。本局解出的高度是真实地形高度
   * （13–246 m），直接用于播放；只有取不到时才回落到 ECS 构造点高度。 */

  var _gf32 = new DataView(new ArrayBuffer(4));
  function groundF32(br) {
    var b = br.bytesN(4);
    _gf32.setUint8(0, b[0]); _gf32.setUint8(1, b[1]);
    _gf32.setUint8(2, b[2]); _gf32.setUint8(3, b[3]);
    return _gf32.getFloat32(0, true);
  }

  /** danet ReadBits(n) + binary.LittleEndian.Uint32：≤8 位一段，段内 MSB 在前。 */
  function groundBitsLE(br, n) {
    var v = 0, sh = 0, left = n;
    while (left > 0) {
      var k = left >= 8 ? 8 : left;
      v += br.bits(k) * Math.pow(2, sh);
      sh += 8; left -= k;
    }
    return v;
  }

  function groundClamp(v) { return v < -1 ? -1 : (v > 1 ? 1 : v); }

  /** Dagor 2-bit RLE（wrpl-acmi ground_delta.go 的 rle0kiDecompress）。 */
  function groundRle0ki(src) {
    var limit = src.length - 1;
    if (limit < 0) throw new Error('rle 空');
    var parity = src[limit];
    if (parity !== 111 && parity !== 222) throw new Error('rle parity ' + parity);
    var out = new Uint8Array(GROUND_MAX_RECORD);
    var outBits = 0, code = -1, sup = -1;
    for (var i = 0; i < limit; i++) {
      var klim = (i === limit - 1 && parity === 111) ? 1 : 2;
      for (var k = 0; k < klim; k++) {
        var val0 = (src[i] >> (k * 4)) & 3, val1 = (src[i] >> (2 + k * 4)) & 3;
        var num = 0;
        if (code !== 4) code = val0;
        if (code === 0) { num = val1 + 3; val0 = 0; val1 = 0; }
        else if (code === 3) { num = val1 + 7; val0 = 0; val1 = 0; }
        else if (code === 1) { code = 4; sup = val1; num = 1; val0 = val1; }
        else if (code === 2) { num = 1; val0 = val1; }
        else if (code === 4) {
          code = -1; num = 2;
          if (sup === 0 && val0 === 0 && val1 === 0) num = 39;
        }
        if (outBits + num * 2 > out.length * 8) throw new Error('rle 输出越界');
        for (var n = 0; n < num; n++) {
          var v = (n & 1) === 0 ? val0 : val1;
          var bi = outBits >> 3, sh = outBits & 7;
          out[bi] |= (v << sh) & 0xFF;
          outBits += 2;
        }
      }
    }
    return out.subarray(0, (outBits + 7) >> 3);
  }

  function groundXorPatch(base, delta) {
    var n = Math.max(base.length, delta.length);
    var out = new Uint8Array(n);
    var common = Math.min(base.length, delta.length), i;
    for (i = 0; i < common; i++) out[i] = base[i] ^ delta[i];
    if (delta.length > base.length) out.set(delta.subarray(common), common);
    else out.set(base.subarray(common), common);
    return out;
  }

  function groundNewHist() {
    return { entries: new Array(GROUND_DELTA_HIST), nos: new Array(GROUND_DELTA_HIST),
             valid: new Array(GROUND_DELTA_HIST) };
  }

  function groundHistApply(hist, full, packetNoDelta, nextPacketNo, packed) {
    var result;
    if (full) {
      result = packed;
    } else {
      if (packetNoDelta >= GROUND_DELTA_HIST) throw new Error('delta 距离 ' + packetNoDelta);
      var baseNo = (nextPacketNo - packetNoDelta) >>> 0;
      var slot = baseNo % GROUND_DELTA_HIST;
      var tag = baseNo & 0x1FFF;
      if (!hist.valid[slot] || hist.nos[slot] !== tag || !hist.entries[slot]) {
        throw new Error('delta 基线缺失 ' + baseNo);
      }
      result = groundXorPatch(hist.entries[slot], groundRle0ki(packed));
    }
    var s = nextPacketNo % GROUND_DELTA_HIST;
    hist.entries[s] = result;
    hist.nos[s] = nextPacketNo & 0x1FFF;
    hist.valid[s] = true;
    return result;
  }

  /** parseVehiclePosition：返回 {x, y(高度), z} 或 null。 */
  function groundVehiclePos(data, hasDir) {
    var r = new BitReader(data);
    r.byte();                                  // 记录头
    if (!r.bits(1)) return null;               // hasPosition
    if (hasDir) {
      r.skip(4 * 32);
      if (r.bits(1)) r.skip(21 * 32);
    } else {
      r.skip(3 * 16);
    }
    r.bits(1);
    var full = r.bits(1);
    var x, y, z;
    if (full) {
      x = groundF32(r); y = groundF32(r); z = groundF32(r);
    } else {
      var tv1 = r.u32(), tv2 = r.u32();
      var fx = Math.fround(tv1 >>> 10) * Math.fround(4.7683727e-7) - 1;
      x = groundClamp(fx) * 36000;
      y = Math.fround(((tv1 & 0x3FF) << 10) | (tv2 >>> 22)) * Math.fround(0.007629402) - 100;
      var fz = Math.fround(tv2 & 0x3FFFFF) * Math.fround(4.7683727e-7) - 1;
      z = groundClamp(fz) * 36000;
    }
    if (!isFinite(x) || !isFinite(y) || !isFinite(z)) return null;
    if (Math.abs(x) > 1000000 || Math.abs(y) > 1000000 || Math.abs(z) > 1000000) return null;
    return { x: x, y: y, z: z };
  }

  function groundParsePacket(pl, tick, hists, out, stats) {
    var r = new BitReader(pl.subarray(4));
    var bodyBits = r.compressed();
    r.align();
    var nb = Math.ceil(bodyBits / 8);
    if (nb <= 0 || nb > 1 << 20 || (r.pos >> 3) + nb > r.d.length) throw new Error('body 长度');
    var g = new BitReader(r.bytesN(nb));
    var lower = g.u16(), upper = g.u16();
    g.u32();                                   // 同步时刻
    g.u16();                                   // 可靠性序号
    if (!(lower < upper && upper <= GROUND_UID_MAX)) throw new Error('uid 范围');
    for (var uid = lower; uid < upper; uid++) {
      if (!g.bits(1)) continue;                // hasUnit
      g.bits(1);
      if (g.bits(1)) g.skip(4);
      var v2 = g.bits(1), v3 = g.bits(1);
      if (v2 && v3) continue;
      g.bits(1);
      var hasDir = g.bits(1);
      var turrets = g.byte();
      var compressed = g.bits(1);
      if (!compressed) throw new Error('未压缩地面体');
      var full = g.bits(1);
      var pnd = groundBitsLE(g, 5), npn = groundBitsLE(g, 13);
      var size = g.compressed();
      g.align();
      if (size <= 0 || size > GROUND_MAX_DELTA || (g.pos >> 3) + size > g.d.length) {
        throw new Error('delta 长度 ' + size);
      }
      var packed = g.bytesN(size);
      var h = hists[uid] || (hists[uid] = groundNewHist());
      var body = groundHistApply(h, full, pnd, npn, packed);
      var pos = groundVehiclePos(body, hasDir);
      if (pos) {
        var arr = out[uid] || (out[uid] = []);
        arr.push([tick, pos.x, pos.z, pos.y]);   // [tick, x, north, 高度]
        stats.samples++;
      }
      if (g.bits(1)) g.skip(4 * 32);
      if (g.bits(1)) {
        for (var ti = 0; ti < turrets; ti++) {
          if (!g.bits(1)) continue;
          g.byte(); g.skip(64);
          var ammo = g.byte();
          g.skip(ammo * 16);
        }
      }
      if (g.bits(1)) {
        if (g.bits(1)) {
          var seeker = g.u32();
          if (seeker > 1 << 20) throw new Error('seeker 长度');
          g.skip(seeker);
        }
      }
      g.align();
    }
  }

  function parseGroundSync(payload, pk) {
    var hists = {}, samples = {};
    var stats = { packets: 0, uids: 0, samples: 0, errors: 0, lastError: null };
    for (var i = 0; i < pk.length; i++) {
      var p = pk[i];
      if (p.ptype !== 4) continue;
      var pl = payload.subarray(p.start + p.hl, p.end);
      if (pl.length < 8) continue;
      if (!(pl[0] === 0x02 && pl[1] === 0x58 &&
            (pl[2] === GROUND_MPI_IDS[0] || pl[2] === GROUND_MPI_IDS[1]) && pl[3] === 0xF0)) continue;
      stats.packets++;
      try {
        groundParsePacket(pl, p.ms * TICK_PER_MS, hists, samples, stats);
      } catch (e) {
        stats.errors++;
        stats.lastError = String(e && e.message ? e.message : e);
      }
    }
    var keep = {};
    Object.keys(samples).forEach(function (k) {
      if (samples[k].length >= MIN_TRACK_SAMPLES) keep[k] = samples[k];
    });
    stats.uids = Object.keys(keep).length;
    return { samples: keep, stats: stats };
  }

  /* ------------------------------------------------ 12.6 弹药同步弹道（0xF0DB / 0xF11A）
   * 包体：`02 58` + msgId u16（0xF0DB shell / 0xF11A rocket），其后
   *   [ULEB 位长][字节对齐][位流体]；体里 3 组 class（0=火箭/导弹、1=炸弹、2=鱼雷），
   *   每组一个字节计数，每条记录：hasGuidance, noExtraTail, entity u32, isRaw, 坐标,
   *   可选尾部（3×u32、6×u16、flags、seeker…）。
   * 解析参考上游 wrpl/packet/parser/projectiles/positions.go（AGPL-3.0），按位重写。 */

  function wsValid(x, y, z) {
    return isFinite(x) && isFinite(y) && isFinite(z) &&
           Math.abs(x) <= 10000000 && Math.abs(y) <= 10000000 && Math.abs(z) <= 10000000;
  }

  function wsClampUnit(v) { return v < -1 ? -1 : (v > 1 ? 1 : v); }

  function wsConsumeTail(r, hasGuidance) {
    r.compressed();                       /* 尾部标量（上游记为 zig-zag） */
    var i;
    for (i = 0; i < 3; i++) r.u32();
    for (i = 0; i < 6; i++) r.u16();
    if (r.bits(1)) r.u16();
    var flags = r.byte();
    for (var f = 1; f <= 8; f <<= 1) if (flags & f) r.compressed();
    if (!hasGuidance) {
      r.u32();                            /* seeker 计时（秒） */
      r.bits(1);
      var size = r.u16();
      r.skip(size);
    } else if (r.bits(1)) {
      throw new Error('SACLOS 未解');
    }
  }

  function wsRecord(r, tick) {
    var hasGuidance = r.bits(1) === 1;
    var noExtraTail = r.bits(1) === 1;
    var entity = r.u32();
    var isRaw = r.bits(1) === 1;
    var x, y, z;
    if (isRaw) {
      x = groundF32(r); y = groundF32(r); z = groundF32(r);
    } else {
      var c1 = r.u32(), c2 = r.u32();
      x = wsClampUnit((c1 >>> 10) / 2097152 - 1) * 15000;
      y = wsClampUnit(((((c1 & 0x3FF) << 10) | (c2 >>> 22)) / 524288) - 1) * 15000;
      z = wsClampUnit(((c2 & 0x3FFFFF) / 2097152) - 1) * 15000;
    }
    if (!noExtraTail) wsConsumeTail(r, hasGuidance);
    if (!wsValid(x, y, z)) return null;
    return { tick: tick, entity: entity, x: x, y: y, z: z };
  }

  function parseWeaponSync(payload, pk) {
    var rockets = {}, bombs = {};
    var stats = { packets: 0, records: 0, rockets: 0, bombs: 0, errors: 0, lastError: null };
    for (var i = 0; i < pk.length; i++) {
      var p = pk[i];
      if (p.ptype !== 4) continue;
      var pl = payload.subarray(p.start + p.hl, p.end);
      if (pl.length < 8 || pl[0] !== 0x02 || pl[1] !== 0x58) continue;
      var msgId = pl[2] | (pl[3] << 8);
      if (msgId !== 0xF0DB && msgId !== 0xF11A) continue;
      stats.packets++;
      try {
        var r = new BitReader(pl.subarray(4));
        var bodyBits = r.compressed();
        r.align();
        var nb = Math.ceil(bodyBits / 8);
        if (nb <= 0 || nb > (1 << 22) || (r.pos >> 3) + nb > r.d.length) throw new Error('体长度');
        var body = new BitReader(r.bytesN(nb));
        for (var cls = 0; cls < 3; cls++) {
          var count = body.byte();
          for (var k = 0; k < count; k++) {
            var rec = wsRecord(body, p.ms * TICK_PER_MS);
            stats.records++;
            if (!rec) continue;
            if (cls === 0) {
              var arr = rockets[String(rec.entity)] || (rockets[String(rec.entity)] = []);
              arr.push(rec);
            } else if (cls === 1) {
              var brr = bombs[String(rec.entity)] || (bombs[String(rec.entity)] = []);
              brr.push(rec);
            }
          }
        }
      } catch (e) {
        stats.errors++;
        stats.lastError = String(e && e.message ? e.message : e);
      }
    }
    var rockN = Object.keys(rockets).length;
    stats.rockets = rockN;
    stats.bombs = Object.keys(bombs).length;
    return { rockets: rockets, bombs: bombs, stats: stats };
  }

  /** uid → ECS 地面构造点。先按真实 uid 精确绑定（块内 uid 规则）；
   *  剩下的再用「首采样与构造点水平距离 ≤10 m 且最近者唯一」兜底。都不猜型号。 */
  function bindGroundTracks(groundRows, groundSync) {
    var samples = groundSync.samples, bound = {}, used = {}, ok = 0, byUid = 0;
    // 1) 精确 uid
    groundRows.forEach(function (g, i) {
      if (g.groundUid === null || g.groundUid === undefined) return;
      var s = samples[String(g.groundUid)];
      if (!s || !s.length) return;
      bound[i] = { uid: String(g.groundUid), samples: s };
      used[i] = 1; ok++; byUid++;
    });
    // 2) 位置兜底
    Object.keys(samples).forEach(function (uid) {
      if (Object.keys(bound).some(function (k) { return bound[k].uid === uid; })) return;
      var s = samples[uid];
      var fx = s[0][1], fy = s[0][2];
      var best = -1, bd = 1e18, second = 1e18;
      for (var i = 0; i < groundRows.length; i++) {
        if (used[i]) continue;
        var g = groundRows[i];
        var d = Math.hypot(g.x - fx, g.y - fy);
        if (d < bd) { second = bd; bd = d; best = i; }
        else if (d < second) second = d;
      }
      if (best < 0 || bd > GROUND_SPAWN_MATCH_M) return;
      if (second < 1e17 && second < bd * 3.0) return;   // 两个候选都贴得很近：不猜
      used[best] = 1; bound[best] = { uid: uid, samples: s };
      ok++;
    });
    return { bound: bound, matched: ok, byUid: byUid, uids: Object.keys(samples).length };
  }

  /* ------------------------------------------------ 12.5 玩家槽位（0xF02D）
   * 包体：签名 02 58 2d f0，其后 [compressed 标志][可选 zstd（压缩/解压长度 ULEB）]
   * [u16 消息数][每条：u16 长度, oid u16, 可选 ext uid, 消息体]。
   * oid>>11 == 0xE 的消息是玩家槽位：IdFieldSerializer255，字段 2=uid(玩家id+65字节名)、
   * 5=战队、6=称号、9=team、41=真实昵称。
   * 解析参考上游 wrpl/packet/parser/slot/main.go（AGPL-3.0），按位重写。 */

  function slotReadSize(br) {
    var h = br.bits(3);
    if (h === 1) return 1;
    if (h === 2) return 8;
    if (h === 3) return 16;
    if (h === 4) return 32;
    if (h === 5) return 64;
    if (h === 6) return 96;
    if (h === 7) return 128;
    return br.compressed();          // 自定义位数
  }

  function readBitsBytes(br, nbits) {
    var out = new Uint8Array((nbits + 7) >> 3);
    for (var i = 0; i < nbits; i++) {
      if (br.bits(1)) out[i >> 3] |= 1 << (7 - (i & 7));
    }
    return out;
  }

  function slotLenStr(br) {
    var n = br.byte();
    return utf8(readBitsBytes(br, n * 8), 0, n);
  }

  function parseSlotPlayerMessage(msg) {
    var br = new BitReader(msg);
    var off = br.u16();
    var cnt = br.u16();
    var nfields = cnt & 0xFFF, bpi = cnt >> 12;
    if (nfields === 0 || nfields >= 255) return null;
    var startBody = br.pos;
    br.pos = off * 8;
    var sizes = [];
    for (var i = 0; i < nfields; i++) sizes.push(slotReadSize(br));
    var bitsForIdx = Math.ceil((bpi * nfields) / 8) * 8;
    br.pos = startBody - 32 + off * 8 - bitsForIdx;
    var idxs = [];
    for (var j = 0; j < nfields; j++) idxs.push(groundBitsLE(br, bpi));
    br.pos = startBody;
    var out = {};
    for (var k = 0; k < nfields; k++) {
      var raw = readBitsBytes(br, sizes[k]);
      var f = new BitReader(raw);
      var idx = idxs[k];
      try {
        if (idx === 2) {
          var lo = f.u32(), hi = f.u32();
          out.playerId = lo + hi * 4294967296;
          var nb = readBitsBytes(f, 65 * 8);
          var end = 0;
          while (end < nb.length && nb[end] !== 0) end++;
          out.name = utf8(nb, 0, end);
        } else if (idx === 5) out.clan = slotLenStr(f);
        else if (idx === 6) out.title = slotLenStr(f);
        else if (idx === 9) out.team = f.byte();
        else if (idx === 41) out.realNick = slotLenStr(f);
      } catch (e) { /* 单字段坏了不影响其它字段 */ }
    }
    return (out.name || out.team) ? out : null;
  }

  function parseSlotPlayers(payload, pk) {
    var players = {}, stats = { packets: 0, messages: 0, players: 0, errors: 0, lastError: null };
    var fz = global.fzstd || (global.self && global.self.fzstd);
    for (var i = 0; i < pk.length; i++) {
      var p = pk[i];
      if (p.ptype !== 4) continue;
      var pl = payload.subarray(p.start + p.hl, p.end);
      if (pl.length < 8) continue;
      if (!(pl[0] === 0x02 && pl[1] === 0x58 && pl[2] === 0x2D && pl[3] === 0xF0)) continue;
      stats.packets++;
      try {
        var r = new BitReader(pl.subarray(4));
        var compressed = r.byte();
        var br = r;
        if (compressed > 0) {
          r.compressed(); r.compressed();          // 压缩后/解压后长度
          if (!fz || typeof fz.decompress !== 'function') throw new Error('缺少 fzstd');
          br = new BitReader(fz.decompress(pl.subarray(4 + (r.pos >> 3))));
        }
        var mc = br.u16();
        for (var mi = 0; mi < mc; mi++) {
          var mlen = br.u16();
          var before = br.pos;
          var oid = br.u16();
          if ((oid & 0x7FF) === 0x7FF && oid !== 0xFFFF) br.compressed();
          var after = br.pos;
          var nbytes = mlen - ((after - before) >> 3);
          if (nbytes < 0 || nbytes > (br.n - br.pos) >> 3) throw new Error('slot 消息长度');
          var msg = readBitsBytes(br, nbytes * 8);
          if ((oid >> 11) === 0xE) {
            stats.messages++;
            var idx = oid & 0x7FF;
            var plr = parseSlotPlayerMessage(msg);
            if (plr) {
              var cur = players[idx] || {};
              Object.keys(plr).forEach(function (key) { if (plr[key] !== undefined && plr[key] !== '') cur[key] = plr[key]; });
              players[idx] = cur;
            }
          }
        }
      } catch (e) {
        stats.errors++;
        stats.lastError = String(e && e.message ? e.message : e);
      }
    }
    stats.players = Object.keys(players).length;
    return { players: players, stats: stats };
  }

  /** 地面航迹：1 Hz 阶梯保持（回放采样中位间隔 5.5 s，插值会造出比数据更平滑的运动）。 */
  function groundTrackArrays(samples, axis, ysign, alt, hz) {
    var t0 = axis.sec(samples[0][0]);
    var tLast = axis.sec(samples[samples.length - 1][0]);
    var n = Math.floor((tLast - t0) * hz) + 1;
    if (n < 1) n = 1;
    var x = new Array(n), y = new Array(n), z = new Array(n), hdg = new Array(n);
    var si = 0;
    for (var k = 0; k < n; k++) {
      var t = t0 + k / hz;
      while (si + 1 < samples.length && axis.sec(samples[si + 1][0]) <= t) si++;
      x[k] = rnd(samples[si][1], 1);
      y[k] = rnd(samples[si][2] * ysign, 1);
      z[k] = (samples[si][3] !== null && isFinite(samples[si][3])) ? rnd(samples[si][3], 1) : alt;
      hdg[k] = null;
    }
    return { x: x, y: y, z: z, hdg: hdg, n: n, t0: rnd(t0, 3) };
  }


  /** 未绑定到 ECS 构造点的 uid：只要确实移动过（>50 m）就作为无身份地面单位输出，
   * 与上游 wrpl-acmi 的 "Ground unit <uid>" 同一口径——不按距离猜型号/阵营。 */
  function buildAnonymousGroundTracks(groundSync, bind, axis, ysign) {
    var boundUids = {};
    Object.keys(bind.bound).forEach(function (k) { boundUids[bind.bound[k].uid] = 1; });
    var out = [];
    Object.keys(groundSync.samples).forEach(function (uid) {
      if (boundUids[uid]) return;
      var s = groundSync.samples[uid];
      var x0 = s[0][1], y0 = s[0][2], moved = 0;
      for (var i = 1; i < s.length; i++) {
        var d = Math.hypot(s[i][1] - x0, s[i][2] - y0);
        if (d > moved) moved = d;
      }
      if (moved < 50.0) return;                 // 原地不动的匿名点不新增，免得和静态点重复
      var tr = groundTrackArrays(s, axis, ysign, null, 1);
      out.push({
        key: 'gnd-uid-' + uid, side: 'unknown', sideDecoded: null, sideInferred: false,
        domain: 'ground', 'class': 'ground', vehicle: null,
        vehicleSource: 'ground-sync(0xF073)', vehicleName: 'GROUND UNIT',
        vehicleCategory: '', hidden: false, template: null, unitClassName: null,
        player: null, hasAlt: true, 'static': false, groundUid: parseInt(uid, 10),
        t0: tr.t0, hz: 1, n: tr.n, x: tr.x, y: tr.y, z: tr.z, hdg: tr.hdg,
        deathTickRaw: null, deathKind: null, killerUid: null,
        entityId: null, gridClass: 'ground', spawnTickRaw: s[0][0]
      });
    });
    return out;
  }

  function buildGroundUnits(ground, axis, display, centers, hiddenAll, bound, ysign, authorTeam) {
    var out = [];
    (ground || []).forEach(function (g, idx) {
      var x = finiteM(g.x), y = finiteM(g.y), z = finiteM(g.z);
      var b = bound ? bound[idx] : null;
      if (!b && (x === null || y === null)) return;   /* 有同步位置的可以没有构造点坐标 */
      var tpl = g.template;
      var rawClass = g.unitClassName || g.className || null;
      var hiddenHere = !!hiddenAll[rawClass || tpl];
      var code = String(rawClass || tpl || 'obj').toLowerCase().replace(/[^a-z0-9]/g, '') || 'obj';
      var key = 'gnd-' + code + '-' + ('00' + (idx + 1)).slice(-3);
      var tick = g.spawnTickRaw;
      var team = (g.groundTeam === 1 || g.groundTeam === 2) ? g.groundTeam : null;
      var teamSide = (team !== null && (authorTeam === 1 || authorTeam === 2))
        ? (team === authorTeam ? 'ally' : 'enemy') : null;
      var sd = teamSide || groundSide(x, y, centers);
      var sideSrc = teamSide ? 'ecs:team' : 'nearest-spawn-center';
      if (b) {
        /* 有同步位置：真正会动的单位。高度优先用同步包里的高度，缺失才回落到 ECS 构造点。 */
        var tr = groundTrackArrays(b.samples, axis, ysign, z === null ? null : rnd(z, 1), 1);
        out.push({
          key: key,
          side: sd, sideDecoded: teamSide, sideInferred: !teamSide && sd !== 'unknown',
          sideSource: sideSrc, groundTeam: team,
          domain: 'ground', 'class': 'ground',
          vehicle: rawClass || tpl, vehicleSource: 'ecs+ground-sync(0xF073)',
          vehicleName: hiddenHere ? '' : ((display[rawClass || String(tpl || '')] || rawClass || tpl)),
          vehicleCategory: VEHICLE_CATEGORIES[rawClass || ''] || 'UNKNOWN',
          hidden: hiddenHere, template: tpl, unitClassName: rawClass,
          player: null, hasAlt: z !== null, 'static': false,
          groundUid: parseInt(b.uid, 10),
          t0: tr.t0, hz: 1, n: tr.n,
          x: tr.x, y: tr.y, z: tr.z, hdg: tr.hdg,
          deathTickRaw: g.destroyedTickRaw === undefined ? null : g.destroyedTickRaw,
          deathKind: g.destroyedTickRaw == null ? null : 'destroyed', killerUid: null,
          entityId: g.entityId, gridClass: g['class'], spawnTickRaw: tick
        });
        return;
      }
      out.push({
        key: key,
        side: sd, sideDecoded: teamSide, sideInferred: !teamSide && sd !== 'unknown',
        sideSource: sideSrc, groundTeam: team,
        domain: 'ground', 'class': 'ground',
        vehicle: rawClass || tpl, vehicleSource: 'ground.json',
        vehicleName: hiddenHere ? '' : ((display[rawClass || String(tpl || '')] || rawClass || tpl)),
        vehicleCategory: VEHICLE_CATEGORIES[rawClass || ''] || 'UNKNOWN',
        hidden: hiddenHere, template: tpl, unitClassName: rawClass,
        player: null, hasAlt: true, 'static': true,
        groundUid: g.groundUid === undefined ? null : g.groundUid,
        t0: tick !== null && tick !== undefined ? Math.max(0.0, rnd(axis.sec(tick), 3)) : 0.0,
        hz: 1, n: 1,
        x: [rnd(x, 0)], y: [rnd(y, 0)], z: [rnd(z, 1)],
        /* 静态点：hdg 单元素数组；来自 transform 四元数，取不到为 null */
        hdg: [g.hdgDeg === undefined || g.hdgDeg === null ? null : g.hdgDeg],
        deathTickRaw: g.destroyedTickRaw === undefined ? null : g.destroyedTickRaw,
        deathKind: g.destroyedTickRaw == null ? null : 'destroyed', killerUid: null,
        entityId: g.entityId, gridClass: g['class'], spawnTickRaw: tick
      });
    });
    out.sort(function (a, b) { return (a.t0 - b.t0) || cmpStr(a.key, b.key); });
    return out;
  }

  function asciiRunwayName(key, name) {
    var ascii = true;
    for (var i = 0; i < name.length; i++) if (name.charCodeAt(i) > 127) { ascii = false; break; }
    if (name && ascii) return name;
    var tail = String(key || '').split('-').slice(1).join('-').toUpperCase() || 'RUNWAY';
    return tail + ' RUNWAY';
  }

  function buildRunwaysOut(runwayData) {
    var out = [];
    (runwayData.runways || []).forEach(function (r) {
      var x1 = finiteM(r.x1), y1 = finiteM(r.y1), z1 = finiteM(r.z1);
      var x2 = finiteM(r.x2), y2 = finiteM(r.y2), z2 = finiteM(r.z2);
      if (x1 === null || y1 === null || x2 === null || y2 === null) return;
      out.push({
        key: r.key, name: asciiRunwayName(r.key, r.name),
        x1: rnd(x1, 0), y1: rnd(y1, 0), z1: z1 === null ? null : rnd(z1, 1),
        x2: rnd(x2, 0), y2: rnd(y2, 0), z2: z2 === null ? null : rnd(z2, 1),
        lengthM: r.lengthM, headingDeg: r.headingDeg, spreadM: r.spreadM,
        confidence: r.confidence, source: 'ecs-airfield-marker', sourceRaw: r.source
      });
    });
    out.sort(function (a, b) { return cmpStr(String(a.key), String(b.key)); });
    return out;
  }

  function buildAirfieldsOut(runwayData) {
    var out = [];
    (runwayData.airfields || []).forEach(function (a) {
      var x = finiteM(a.x), y = finiteM(a.y), z = finiteM(a.z);
      if (x === null || y === null) return;
      out.push({
        key: a.key, x: rnd(x, 0), y: rnd(y, 0), z: z === null ? null : rnd(z, 1),
        structures: a.structureCount, classNames: a.classNames || [], source: 'ecs-cluster'
      });
    });
    out.sort(function (a, b) { return cmpStr(String(a.key), String(b.key)); });
    return out;
  }

  function buildShots(launches, axis, units, window, gateM) {
    gateM = gateM || 100.0;
    var out = [], dropped = 0, linked = 0;
    var air = units.filter(function (u) { return u.domain === 'air'; });
    (launches || []).forEach(function (g) {
      var tick = g.spawnTickRaw;
      var x = finiteM(g.x), y = finiteM(g.y), z = finiteM(g.z);
      if (tick === null || tick === undefined || x === null || y === null) { dropped++; return; }
      var t = axis.sec(tick);
      if (!(t >= window.t0 && t <= window.t1)) { dropped++; return; }
      var shooterUid = null, side = null, bestD = gateM;
      air.forEach(function (u) {
        var i = Math.round((t - u.t0) * u.hz);
        if (i < 0 || i >= u.n) return;
        var ux = u.x[i], uy = u.y[i];
        if (ux === null || uy === null) return;
        var d = Math.hypot(ux - x, uy - y);
        if (d < bestD) { bestD = d; shooterUid = u.uid; side = u.side; }
      });
      if (shooterUid !== null) linked++;
      var tpl = g.template;
      out.push({
        t: rnd(t, 3), tickRaw: tick, template: tpl,
        templateName: tpl ? (SHOT_NAMES[String(tpl).toLowerCase()] || null) : null,
        x: rnd(x, 0), y: rnd(y, 0), z: rnd(z, 1),
        yaw: null, side: side, shooterUid: shooterUid, entityId: g.entityId
      });
    });
    out.sort(function (a, b) { return (a.t - b.t) || (a.tickRaw - b.tickRaw); });
    return { shots: out, dropped: dropped, linked: linked };
  }

  function buildEvents(header, entities, axis, units, window, uidMap, killEvents,
                       playerUid, ysign, killPositions) {
    var events = [];
    var posStats = { withPos: 0, withoutPos: 0, reasons: {} };
    var kpByTick = {};
    (killPositions || []).forEach(function (kp) {
      if (kp.tickRaw !== null && kp.tickRaw !== undefined) kpByTick[kp.tickRaw] = kp;
    });
    (killEvents.kills || []).forEach(function (k) {
      var tick = k.tickRaw;
      if (tick === null || tick === undefined) return;
      var t = axis.sec(tick);
      if (!(t >= window.t0 && t <= window.t1)) return;
      var killerUid = (k.killerUid === undefined) ? null : k.killerUid;
      var victimUid = (k.victimUid === undefined) ? null : k.victimUid;
      var actorVeh = k.killerVehicle || null;
      var weapon = k.weapon || null;
      var actorInfo = uidMap[String(killerUid)] || {};
      var victimInfo = uidMap[String(victimUid)] || {};
      var actorName = actorInfo.player, victimName = victimInfo.player;
      var targetVeh = victimInfo.vehicle || null;
      var x = null, y = null, z = null, posBasis = null;
      var kp = kpByTick[tick] || {};
      var vp = kp.victimPos, kpos = kp.killerPos;
      if (kp.hasVictimPos && vp) { x = vp[0]; y = vp[1] * ysign; z = vp[2]; posBasis = 'victim_pos_at_tick'; }
      else if (kp.hasKillerPos && kpos) { x = kpos[0]; y = kpos[1] * ysign; z = kpos[2]; posBasis = 'killer_pos_at_tick'; }
      else {
        var hit = killPositionsVehicle(units, axis, tick, actorVeh);
        if (hit) { x = hit.x; y = hit.y; z = hit.z; posBasis = hit.why; }
      }
      if (x === null) {
        posStats.withoutPos++;
        var key = 'killPositions 也没有坐标（地面/AI 受击者）';
        posStats.reasons[key] = (posStats.reasons[key] || 0) + 1;
      } else posStats.withPos++;
      var noKiller = killerUid === null || killerUid === 2047 || (!actorVeh && !actorName);
      var actorLabel = actorName || actorVeh || (noKiller ? null : 'UID ' + killerUid);
      var targetLabel = victimName || targetVeh ||
        (victimUid !== null ? 'UID ' + victimUid : '未知目标');
      var text = noKiller ? (targetLabel + ' 被击落（无击杀者：坠毁/环境）')
        : (actorLabel + ' 击落 ' + targetLabel + (weapon ? ('（' + weapon + '）') : ''));
      events.push({
        t: rnd(t, 3), kind: 'kill', text: text, composed: true,
        actor: actorName, actorVehicle: actorVeh,
        target: victimName, targetVehicle: targetVeh,
        entityId: (!noKiller && killerUid !== null) ? ('uid-' + killerUid) : null,
        victimEntityId: victimUid !== null ? ('uid-' + victimUid) : null,
        isPlayerActor: (playerUid !== null && killerUid === playerUid),
        isPlayerTarget: (playerUid !== null && victimUid === playerUid),
        replayTick: tick, x: x, y: y, z: z,
        weapon: weapon, weaponName: weaponDisplay(weapon),
        killerUid: killerUid, victimUid: victimUid,
        noKiller: !!noKiller, posBasis: posBasis
      });
    });

    (entities.awards || []).forEach(function (a) {
      var ticks = a.firstTicksRaw || [];
      var tick = null;
      if (ticks.length) tick = ticks[0];
      else if (a.firstTsMs !== null && a.firstTsMs !== undefined) tick = axis.ms_to_tick(a.firstTsMs);
      if (tick === null) return;
      var t = axis.sec(tick);
      if (!(t >= window.t0 && t <= window.t1)) return;
      var name = String(a.award || 'unknown_award');
      var count = parseInt(a.count || 1, 10);
      events.push({
        t: rnd(t, 3), kind: (name === 'final_blow' ? 'lastblow' : 'award'),
        text: name + '（本局 ' + count + ' 次，此处为首次时刻）', composed: true,
        actor: null, actorVehicle: null, target: null, targetVehicle: null,
        entityId: null, victimEntityId: null, isPlayerActor: false, isPlayerTarget: false,
        replayTick: tick, x: null, y: null, z: null, awardCount: count
      });
    });

    (entities.chat || []).forEach(function (c) {
      var tick = c.tick;
      if (tick === null || tick === undefined) return;
      var t = axis.sec(tick);
      if (!(t >= window.t0 && t <= window.t1)) return;
      events.push({
        t: rnd(t, 3), kind: 'chat', text: cleanText(c.message), composed: false,
        actor: c.sender, actorVehicle: null, target: null, targetVehicle: null,
        entityId: null, victimEntityId: null, isPlayerActor: false, isPlayerTarget: false,
        replayTick: tick, x: null, y: null, z: null
      });
    });

    events.sort(function (a, b) {
      return (a.t - b.t) || cmpStr(a.kind, b.kind) || cmpStr(a.text, b.text);
    });
    return { events: events, posStats: posStats };
  }

  function killPositionsVehicle(units, axis, tick, vehicle) {
    if (!vehicle) return null;
    var t = axis.sec(tick), hits = [];
    units.forEach(function (u) {
      if (u.vehicle !== vehicle) return;
      if (u.t0 - 1.0 <= t && t <= u.t0 + Math.max(0, u.n - 1) / u.hz + 1.0) hits.push(u);
    });
    if (hits.length !== 1) return null;
    var u = hits[0];
    var i = Math.min(Math.max(Math.round((t - u.t0) * u.hz), 0), u.n - 1);
    return { x: u.x[i], y: u.y[i], z: u.z[i], why: 'killer_track_at_tick' };
  }

  /* ------------------------------------------------------------ 13. 阵营终判（纯回放） */

  function buildSideFinal(tracks, uidMap, ownUid, killPositions) {
    var known = {}, teams = {};
    Object.keys(uidMap).forEach(function (k) {
      var v = uidMap[k], uid = parseInt(k, 10);
      if (v.side === 'ally' || v.side === 'enemy') known[uid] = v.side;
      if (v.team === 1 || v.team === 2) teams[uid] = v.team;
    });
    var edges = [], dropped = [];
    killPositions.forEach(function (k) {
      var a = k.killerUid, b = k.victimUid;
      if (a === null || b === null || a === 2047 || b === 2047 || a === b) return;
      if (teams[a] && teams[a] === teams[b]) { dropped.push([a, b, k.tickRaw]); return; }
      edges.push([a, b]);
    });
    var adj = {};
    edges.forEach(function (e) {
      (adj[e[0]] = adj[e[0]] || {})[e[1]] = 1;
      (adj[e[1]] = adj[e[1]] || {})[e[0]] = 1;
    });
    var side = {}, conflicts = 0;
    Object.keys(adj).forEach(function (startKey) {
      var start = parseInt(startKey, 10);
      if (start in side) return;
      var comp = [], stack = [start], seen = {};
      seen[start] = 1;
      while (stack.length) {
        var nd = stack.pop(); comp.push(nd);
        Object.keys(adj[nd] || {}).forEach(function (mKey) {
          var m = parseInt(mKey, 10);
          if (!seen[m]) { seen[m] = 1; stack.push(m); }
        });
      }
      var anchor = null;
      if (comp.indexOf(ownUid) >= 0) anchor = [ownUid, 'ally'];
      else {
        for (var i = 0; i < comp.length; i++) if (known[comp[i]]) { anchor = [comp[i], known[comp[i]]]; break; }
      }
      if (anchor === null) { comp.forEach(function (n) { side[n] = null; }); return; }
      side[anchor[0]] = anchor[1];
      var st2 = [anchor[0]];
      while (st2.length) {
        var n2 = st2.pop();
        Object.keys(adj[n2] || {}).forEach(function (mKey) {
          var m = parseInt(mKey, 10);
          var want = side[n2] === 'ally' ? 'enemy' : 'ally';
          if (m in side) { if (side[m] !== null && side[m] !== want) conflicts++; }
          else { side[m] = want; st2.push(m); }
        });
      }
    });
    var overridden = 0;
    Object.keys(known).forEach(function (k) {
      var u = parseInt(k, 10), s = known[u];
      if (side[u] !== s) { if (u in side) overridden++; side[u] = s; }
    });

    // AI 兜底：出生位置聚类
    var first = {};
    tracks.units.forEach(function (u) {
      var s = u.samples || [];
      if (!s.length) return;
      var uid = u.uid;
      if (!(uid in first) || s[0][0] < first[uid][0]) first[uid] = s[0];
    });
    var pts = {};
    Object.keys(first).forEach(function (k) {
      var u = parseInt(k, 10);
      if (side[u] === 'ally' || side[u] === 'enemy') pts[u] = [first[k][1], first[k][2]];
    });
    function mean(list) {
      return [list.reduce(function (a, p) { return a + p[0]; }, 0) / list.length,
              list.reduce(function (a, p) { return a + p[1]; }, 0) / list.length];
    }
    var allyPts = [], enemyPts = [];
    Object.keys(pts).forEach(function (k) {
      (side[parseInt(k, 10)] === 'ally' ? allyPts : enemyPts).push(pts[k]);
    });
    if (!allyPts.length || !enemyPts.length) {
      return { side: {}, centers: null, dropped: dropped.length, conflicts: conflicts,
               overridden: overridden, selfCheck: { knownCorrect: 0, knownWrong: 0 } };
    }
    var ca = mean(allyPts), ce = mean(enemyPts);
    var ok = 0, bad = 0;
    Object.keys(pts).forEach(function (k) {
      var u = parseInt(k, 10), p = pts[k];
      var s = Math.hypot(p[0] - ca[0], p[1] - ca[1]) < Math.hypot(p[0] - ce[0], p[1] - ce[1]) ? 'ally' : 'enemy';
      if (s === side[u]) ok++; else bad++;
    });
    var out = {};
    Object.keys(first).forEach(function (k) {
      var u = parseInt(k, 10);
      var s = (side[u] === 'ally' || side[u] === 'enemy') ? side[u] : null;
      if (!s) {
        var p = [first[k][1], first[k][2]];
        s = Math.hypot(p[0] - ca[0], p[1] - ca[1]) < Math.hypot(p[0] - ce[0], p[1] - ce[1]) ? 'ally' : 'enemy';
      }
      out['uid-' + u] = s;
    });
    return { side: out, centers: { ally: [rnd(ca[0], 0), rnd(ca[1], 0)],
                                   enemy: [rnd(ce[0], 0), rnd(ce[1], 0)] },
             dropped: dropped.length, conflicts: conflicts, overridden: overridden,
             selfCheck: { knownCorrect: ok, knownWrong: bad } };
  }

  function buildSquad(players, results, uidMap, ownUid, killsByUid, deathsByUid, kpCount) {
    var slotUidByUserId = {};
    Object.keys(uidMap).forEach(function (k) {
      var v = uidMap[k];
      if (!v || !v.playerUserId) return;
      var u = String(v.playerUserId);
      slotUidByUserId[u] = (slotUidByUserId[u] === undefined) ? parseInt(k, 10) : 'dup';
    });
    Object.keys(slotUidByUserId).forEach(function (u) {
      if (slotUidByUserId[u] === 'dup') delete slotUidByUserId[u];
    });
    var authorId = String(results.authorUserId || '');
    var arow = players.filter(function (x) { return String(x.userId) === authorId; })[0] ||
               players.filter(function (x) { return x.name === results.author; })[0];
    if (!arow) return null;
    var sq = arow.squadId;
    var mates = players.filter(function (x) { return x.squadId === sq; });
    var members = mates.map(function (x) {
      var isAuthor = String(x.userId) === authorId;
      var veh = (x.vehicles || [])[0] || null;
      var uid = (isAuthor && ownUid !== null) ? ownUid : null;
      var uidSrc = uid !== null ? 'own-uid（本机由回放自身的本机属性表（ReplayCockpitParams）高度剖面锚定）' : null;
      if (uid === null && slotUidByUserId[String(x.userId)] !== undefined) {
        uid = slotUidByUserId[String(x.userId)];
        uidSrc = 'slot(unit__playerId) + 结算 userId（精确）';
      }
      var ev = null;
      if (uid === null) {
        var step1 = Object.keys(uidMap).map(function (k) { return parseInt(k, 10); })
          .filter(function (k) {
            var v = uidMap[String(k)];
            return v.vehicle === veh && (v.playerCandidates || []).indexOf(x.name) >= 0 && k !== ownUid;
          }).sort(function (a, b) { return a - b; });
        var want = [x.kills || 0, x.deaths || 0];
        var step2 = step1.filter(function (u) {
          return (killsByUid[u] || 0) === want[0] && (deathsByUid[u] || 0) === want[1];
        });
        ev = { step1_vehicleCandidates: step1,
               step2_fingerprint_wanted: { kills: want[0], deaths: want[1] },
               step2_fingerprint_match: step2,
               fingerprintPerCandidate: {},
               killPacketsUsed: kpCount };
        step1.forEach(function (u) {
          ev.fingerprintPerCandidate[String(u)] = { kills: killsByUid[u] || 0, deaths: deathsByUid[u] || 0 };
        });
        if (step2.length === 1) {
          uid = step2[0];
          uidSrc = 'vehicle-candidates + kills + deaths fingerprint（全部回放内）';
        } else {
          ev.note = '指纹未收敛到唯一候选 → uid 留 null（不猜）';
        }
      }
      var m = { userId: String(x.userId), name: x.name, clanTag: x.clanTag || '',
                team: x.team, slot: x.slot, vehicle: veh, uid: uid, uidSource: uidSrc,
                kills: x.kills, deaths: x.deaths, isAuthor: !!isAuthor };
      if (ev) m.uidEvidence = ev;
      return m;
    });
    return {
      schema: 'wrpl-squad/2', squadId: sq, authorUserId: authorId, authorName: arow.name,
      authorUid: ownUid, size: mates.length, members: members,
      method: 'Squad membership: identical players[].squadId in the results BLK (authoritative, replay-internal). Teammate uid: kill/death fingerprint uniqueness.',
      limits: [
        'There is NO direct link between player identity and aircraft uid in the replay: spawnSlotName (tN_playerMM_K) is a SPAWN GROUP, not a team/slot.',
        'The teammate uid is DERIVED, not read; if the (kills, deaths) fingerprint does not leave exactly one candidate, uid stays null.',
        'humanKills / aiKills are zero or unusable in this replay, so they are not criteria.',
        'uid = null means unresolved, not zero.'
      ],
      tickUnit: 'no timestamps in this file',
      source: 'results-blk squadId'
    };
  }

  /* ------------------------------------------------------------ 14. parse() */

  /** 本机属性表（ReplayCockpitParams） → 与 Python 的 own_props.json 同口径的列。
   *  时间轴是**回放时钟秒**（sec = ms/1000，原点 tick=0），要拿到交付轴秒得再减
   *  tick0 × secPerTick（见 axisFromClock）。列值与舍入位数照 wrpl_decode.py。 */
  function buildOwnProps(propsSamples) {
    var COLS = [
      ['alt_m', [42, 45], function (v) { return rnd(v * 0.3048, 2); }],
      ['ias_mps', [0, 1, 2], function (v) { return rnd(v, 3); }],
      ['mach', [453], function (v) { return rnd(v, 4); }],
      ['ny', [482], function (v) { return rnd(v, 3); }],
      ['hdg_deg', [64, 65], function (v) { return rnd(v, 2); }],
      ['pitch_deg', [53, 55], function (v) { return rnd(v, 2); }],
      ['roll_deg', [52, 54], function (v) { return rnd(v, 2); }],
      ['vario_mps', [39], function (v) { return rnd(v, 2); }],
      ['fuel_kg', [195], function (v) { return rnd(v, 1); }],
      ['throttle_0_1', [240, 241], function (v) { return rnd(v, 4); }]
    ];
    var out = {};
    COLS.forEach(function (c) { out[c[0]] = { times: [], values: [] }; });
    propsSamples.forEach(function (rec) {
      var ms = rec.ms, pr = rec.props;
      var sec = rnd(ms / 1000.0, 3);          // 与 own_props.json 的 columns[0] 一致
      COLS.forEach(function (c) {
        var ids = c[1], conv = c[2], v = null;
        for (var i = 0; i < ids.length; i++) {
          if (Object.prototype.hasOwnProperty.call(pr, ids[i])) { v = pr[ids[i]]; break; }
        }
        // 注意：**每一行都要占位**（Python 的 own_props_series 对全部行 push，
        // 缺值行 push None）—— 否则插值的相邻点会变，结果与 Python 不一致。
        out[c[0]].times.push(sec);
        out[c[0]].values.push(v === null || v === undefined ? null : conv(v));
      });
    });
    return out;
  }

  /** 0.25 s 固定网格的横滚角序列（**度**）—— 给 three3d 的飘带用（window.ACE_ATTITUDE）。
   *  数据源：FM 条目的 EulerBytes（bank 分量，弧度）→ 度；
   *  时间轴与交付一致：轴秒 = (tick − tick0) × secPerTick。 */
  var ATT_DT = 0.25;

  function buildAttitude(eulerByUid, tick0, secPerTick) {
    var units = {}, uids = Object.keys(eulerByUid), points = 0;
    uids.forEach(function (uid) {
      var rec = eulerByUid[uid];
      var nRaw = rec.ticks.length;
      if (nRaw < 2) return;
      var order = new Array(nRaw);
      for (var q = 0; q < nRaw; q++) order[q] = q;
      order.sort(function (a, b) { return rec.ticks[a] - rec.ticks[b]; });
      var ts = [], bs = [];
      for (var k = 0; k < nRaw; k++) {
        var idx = order[k], tk = rec.ticks[idx], bk = rec.banks[idx];
        if (ts.length && tk === ts[ts.length - 1]) { bs[bs.length - 1] = bk; continue; } // 同 tick 取后者
        ts.push(tk); bs.push(bk);
      }
      var t0s = (ts[0] - tick0) * secPerTick;
      var t1s = (ts[ts.length - 1] - tick0) * secPerTick;
      if (!(t1s > t0s)) return;
      var n = Math.floor((t1s - t0s) / ATT_DT + 1e-9) + 1;
      var bank = new Array(n);
      var j = 0;
      for (var k2 = 0; k2 < n; k2++) {
        var t = t0s + k2 * ATT_DT;
        while (j < ts.length - 2 && (ts[j + 1] - tick0) * secPerTick <= t) j++;
        var tA = (ts[j] - tick0) * secPerTick;
        var tB = (ts[j + 1] - tick0) * secPerTick;
        var v;
        if (t <= tA) v = bs[j];
        else if (t >= tB) v = (j + 1 >= ts.length - 1) ? bs[ts.length - 1] : bs[j + 1];
        else v = bs[j] + (bs[j + 1] - bs[j]) * (t - tA) / (tB - tA);
        bank[k2] = rnd(v, 1);
      }
      units['uid-' + uid] = { t0: rnd(t0s, 3), dt: ATT_DT, n: n, bank: bank };
      points += n;
    });
    return { note: 'FM(EulerBytes) 的 bank（滚转）→ 度，0.25 s 固定网格，纯回放数据',
             secPerTick: secPerTick, tick0: tick0, dt: ATT_DT, units: units,
             uidCount: Object.keys(units).length, points: points };
  }

  /* ------------------------------------------------------------ Worker 路径
   * 目标：把解析挪出主线程（主线程最大阻塞 <30 ms），结果与主线程路径逐项一致。
   * Worker 不可用（老浏览器 / file:// 下被限制）时**自动退回主线程**，并在
   * meta.limits 里如实写明走的哪条路径。 */

  /** Worker 是否可用（只做能力判断；真正能不能建起来靠 ready 探针）。 */
  function workerSupported() {
    return typeof Worker === 'function' && typeof Blob === 'function' &&
           typeof URL === 'function' && typeof URL.createObjectURL === 'function';
  }

  /** 本文件的目录（用于定位 wrpl-worker.js）。
   *  **不能**用相对页面的路径：上传页在 web/、开发期自验页在 web/dev/，
   *  同一个 'wrpl-worker.js' 在后者会被解析成 web/dev/wrpl-worker.js（404 → Worker 加载失败）。 */
  var SCRIPT_BASE = (function () {
    try {
      var s = (typeof document !== 'undefined') && document.currentScript && document.currentScript.src;
      if (s) return String(s).replace(/[?#].*$/, '').replace(/[^/]*$/, '');
    } catch (e) {}
    return '';
  })();

  function workerScriptURL() { return SCRIPT_BASE + 'wrpl-worker.js'; }

  /** 把解析好的 ACE_MISSION 收成主线程对象：分片消息按 key 合并。 */
  function recvPartStore() {
    return { meta: null, own: null, units: [], events: null, shots: null, runways: null,
             airfields: null, missiles: [], terrain: undefined, names: undefined, __attitude: undefined };
  }

  function assembleMission(store) {
    return { meta: store.meta, own: store.own, units: store.units, events: store.events,
             shots: store.shots, runways: store.runways, airfields: store.airfields,
             missiles: store.missiles || [],
             terrain: store.terrain === undefined ? null : store.terrain,
             names: store.names === undefined ? {} : store.names,
             __attitude: store.__attitude };
  }

  function parseInWorker(arrayBuffer, opts, pathNote) {
    return new Promise(function (resolve, reject) {
      var worker = null, done = false, store = recvPartStore();
      var debugObj = (opts.debug && typeof opts.debug === 'object') ? opts.debug : null;

      function cleanup() {
        if (worker) {
          try { worker.onmessage = null; worker.onerror = null; worker.terminate(); } catch (e) {}
          worker = null;
        }
      }
      function fail(err) { if (done) return; done = true; cleanup(); reject(err); }

      var wurl = workerScriptURL();
      var _t0 = Date.now(), _tReady = 0, _tSent = 0, _timing = null;
      try {
        worker = new Worker(wurl);
      } catch (e) {
        fail(Object.assign(new Error('Worker 创建失败（' + wurl + '）：' +
                                     (e && e.message ? e.message : e)),
                           { code: 'worker-unavailable' }));
        return;
      }

      var readyTimer = setTimeout(function () {
        fail(Object.assign(new Error('Worker 未在 2 s 内就绪'),
                           { code: 'worker-unavailable' }));
      }, 2000);

      /* 必须主动握手：worker 收到 {cmd:'ready'} 才会回一条 ready（此前漏了这一步，
       * 导致一直等到 2 s 超时再回退主线程 —— 端到端从 1.6 s 变成 3.7 s）。 */
      try {
        worker.postMessage({ cmd: 'ready' });
      } catch (e) {
        clearTimeout(readyTimer);
        fail(Object.assign(new Error('与 Worker 握手失败：' + (e && e.message ? e.message : e)),
                           { code: 'worker-unavailable' }));
        return;
      }

      worker.onerror = function (ev) {
        // 脚本加载失败（含 file:// 下被拦）→ 交给上层回退
        clearTimeout(readyTimer);
        var why = (ev && ev.message) || '';
        if (!why && ev && ev.filename) why = ev.filename + ':' + ev.lineno;
        fail(Object.assign(new Error('Worker 加载失败（' + wurl + '）' + (why ? '：' + why : '：无可读错误')),
                           { code: 'worker-unavailable' }));
      };

      worker.onmessage = function (ev) {
        var m = ev.data || {};
        if (m.type === 'ready') {
          clearTimeout(readyTimer);
          _tReady = Date.now();
          // 转交所有权：主线程这份 ArrayBuffer 立刻 detached，主线程堆不背它
          try {
            worker.postMessage({ cmd: 'parse', buf: arrayBuffer, fileName: opts.fileName,
                                 yieldEvery: opts.yieldEvery,
                                 wantDebug: !!debugObj }, [arrayBuffer]);
            _tSent = Date.now();
          } catch (e) {
            fail(Object.assign(new Error('向 Worker 转交数据失败：' + (e && e.message ? e.message : e)),
                               { code: 'worker-unavailable' }));
          }
          return;
        }
        if (m.type === 'progress') {
          emit(m.phase, m.pct, m.note);
          if (typeof m.workerHeapMB === 'number') _workerHeapPeakMB = m.workerHeapMB;
          return;
        }
        if (m.type === 'part') {
          /* own 是分 4 片发的（t0/hz/n、x/y/z、ias/tas/mach/hdg、其余通道），
           * 必须**合并**而不是覆盖 —— 覆盖会只剩最后一片，本机静默消失。 */
          if (m.key === 'own') store.own = Object.assign(store.own || {}, m.data);
          else if (m.key === 'units') store.units = store.units.concat(m.data);
          else if (m.key === '__attitude') {
            /* 横滚序列按 uid 分片发（可能上百 KB/片），这里按 uid 合并 */
            var att = store.__attitude || (store.__attitude = { units: {} });
            if (m.data.note) att.note = m.data.note;
            if (m.data.secPerTick != null) att.secPerTick = m.data.secPerTick;
            if (m.data.tick0 != null) att.tick0 = m.data.tick0;
            if (m.data.dt != null) att.dt = m.data.dt;
            Object.keys(m.data.units || {}).forEach(function (k) { att.units[k] = m.data.units[k]; });
          }
          else store[m.key] = m.data;
          return;
        }
        if (m.type === 'debug') {
          if (debugObj) Object.keys(m.data || {}).forEach(function (k) { debugObj[k] = m.data[k]; });
          return;
        }
        if (m.type === 'timing') { _timing = m; return; }
        if (m.type === 'done') {
          if (done) return;
          done = true;
          if (typeof m.workerHeapPeakMB === 'number') _workerHeapPeakMB = m.workerHeapPeakMB;
          var mission = assembleMission(store);
          cleanup();                     // terminate()：worker isolate 连同其中间数组一起回收
          mission.meta.limits.push(pathNote);
          mission.meta.provenance.push('解析路径：Web Worker（wrpl-worker.js，主线程只收分片结果）');
          if (_workerHeapPeakMB !== null) {
            mission.meta.__workerHeapPeakMB = _workerHeapPeakMB;
          }
          var _tNow = Date.now();
          var _wParse = (m.timing && m.timing.parseMs != null) ? m.timing.parseMs : null;
          var _wSend = (m.timing && m.timing.sendMs != null) ? m.timing.sendMs : null;
          mission.meta.__timing = {
            createReadyMs: _tReady - _t0,              /* new Worker + importScripts 就绪 */
            transferMs: _tSent - _tReady,              /* 转交 ArrayBuffer（0：Transferable 零拷贝） */
            workerParseMs: _wParse,                    /* worker 内真解析 */
            workerSendMs: _wSend,                      /* 分片 postMessage 序列化 */
            mainReceiveMs: (_wParse != null && _wSend != null)
                             ? Math.max(0, (_tNow - _tSent) - _wParse - _wSend) : null,
            roundTripMs: _tNow - _tSent                /* 从发出 parse 指令到收到全部结果 */
          };
          resolve(mission);
          return;
        }
        if (m.type === 'error') {
          var e = new Error(m.message || 'Worker 解析失败');
          e.code = m.code; e.tech = m.tech; e.fromWorkerParse = true;
          fail(e);
        }
      };
    });
  }

  var _workerHeapPeakMB = null;

  /**
   * 主线程解析（回退路径）。
   * @param {ArrayBuffer|Uint8Array} arrayBuffer
   * @param {object} [opts] fileName / yieldEvery / debug / force
   */
  async function parseMain(arrayBuffer, opts) {
    initStats();
    opts = opts || {};
    var yieldEvery = opts.yieldEvery || 400;
    var t_start = Date.now();
    var d = arrayBuffer instanceof Uint8Array ? arrayBuffer : new Uint8Array(arrayBuffer);
    emit('container', 1, '读取容器头');

    // 容器层先判「是不是本工具支持的回放」：不支持就抛带 code 的错误，
    // 由页面渲染成专门的错误态（旧版本回放的容器/压缩方式与当前版本不同）。
    if (d.length < HEADER_BYTES) {
      throw unsupportedFormat('文件长度 ' + d.length + ' B，比 ' + HEADER_BYTES + ' B 的回放头还短');
    }
    var hdr = parseReplayHeader(d);
    if (hdr.magic !== 0x1000ACE5) {
      throw unsupportedFormat('文件头 magic 是 0x' + hdr.magic.toString(16) + '（期望 0x1000ace5）');
    }
    var info = locatePacketStream(d, hdr);
    var payload = await decompressStream(d, info);
    emit('packets', 16, '切分包帧');

    var pk = parsePackets(payload);
    var census = {}, mpiSigs = {};
    pk.forEach(function (p) {
      census[p.ptype] = (census[p.ptype] || 0) + 1;
      if (p.ptype === 4 && (p.end - p.start - p.hl) >= 4) {
        /* 直接按字节取签名：7 万多个 MPI 包，逐个 subarray 会白花几百 ms */
        var s0 = p.start + p.hl;
        var sig = payload[s0].toString(16) + payload[s0 + 1].toString(16) +
                  payload[s0 + 2].toString(16) + payload[s0 + 3].toString(16);
        mpiSigs[sig] = (mpiSigs[sig] || 0) + 1;
      }
    });
    await sleep0();

    // ---- FlightModel：实体三维航迹（顺带收集 EulerBytes 里的 bank，供三条飘带用）
    emit('fm', 20, '解 ptype=2 FlightModel');
    var rows = [], i, t0 = Date.now();
    var eulerByUid = {};                   // uid -> {ticks:[], banks:[度]}
    for (i = 0; i < pk.length; i++) {
      var p = pk[i];
      if (p.ptype !== 2) continue;
      var ents = parseFm(payload.subarray(p.start + p.hl, p.end));
      for (var e = 0; e < ents.length; e++) {
        var en = ents[e];
        if (en.pos) {
          var pp = en.pos;
          if (isFinite(pp[0]) && isFinite(pp[1]) && isFinite(pp[2])) {
            var tickOf = p.ms * TICK_PER_MS;
            rows.push([tickOf, 'uid-' + en.uid, pp[0], pp[1], pp[2], en.euler]);
            if (typeof en.euler === 'number' && isFinite(en.euler)) {
              // EulerBytes：heading/attitude/bank，单位弧度；这里只取 bank（滚转）
              var bankRad = unpackEuler(en.euler)[2];
              if (isFinite(bankRad)) {
                var rec = eulerByUid[en.uid] || (eulerByUid[en.uid] = { ticks: [], banks: [] });
                rec.ticks.push(tickOf);
                rec.banks.push(bankRad * 180.0 / Math.PI);
              }
            }
          }
        }
      }
      if ((i % yieldEvery) === 0) {
        emit('fm', 20 + 20 * i / pk.length, 'FM 包 ' + i + '/' + pk.length);
        await sleep0();
      }
    }
    rows.sort(function (a, b) {
      return (a[0] - b[0]) || cmpStr(a[1], b[1]) || (a[2] - b[2]) || (a[3] - b[3]) || (a[4] - b[4]);
    });
    var byuid = {}, uidOrder = [];
    rows.forEach(function (r) {
      var k = r[1];
      if (!byuid[k]) { byuid[k] = []; uidOrder.push(k); }
      byuid[k].push(r);
    });

    // ---- 断轨（相邻采样 >600 m/s）与实体清单
    var tick0 = rows.length ? rows[0][0] : 0;
    var tickLast = rows.length ? rows[rows.length - 1][0] : 0;
    var order = uidOrder.slice();
    order.sort(function (a, b) { return byuid[b].length - byuid[a].length; });
    var tracksUnits = [], segStats = { segments: 0, droppedShort: 0, breaks: 0 };
    order.forEach(function (uid) {
      var arr = byuid[uid], segs = [[arr[0]]];
      for (var j = 1; j < arr.length; j++) {
        var prev = segs[segs.length - 1][segs[segs.length - 1].length - 1];
        var dt = (arr[j][0] - prev[0]) * SEC_PER_TICK;
        // 与 Python 同口径：dist 取 (PosX, PosY, PosZ) 三维（含垂直分量）
        var dx = arr[j][2] - prev[2], dy = arr[j][3] - prev[3], dz = arr[j][4] - prev[4];
        var dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
        if (dt > 0 && dist / dt > SPD_LIMIT) { segs.push([arr[j]]); segStats.breaks++; }
        else segs[segs.length - 1].push(arr[j]);
      }
      var keep = segs.filter(function (sg) { return sg.length >= MIN_SEG; });
      segStats.droppedShort += segs.length - keep.length;
      keep.forEach(function (sg, k) {
        var eid = k === 0 ? uid : (uid + '#' + (k + 1));
        var samples = sg.map(function (a) {
          return [a[0], pyRound(a[2], 0), pyRound(a[4], 0), rnd(a[3], 1)];
        });
        tracksUnits.push({ entityId: eid, uid: parseInt(uid.split('-')[1], 10), segment: k,
                           own: false, vehicle: null, hasAlt: true, samples: samples });
        segStats.segments++;
      });
    });
    emit('fm', 40, '航迹 ' + tracksUnits.length + ' 段（' + (Date.now() - t0) + ' ms）');
    await sleep0();

    // ---- 本机属性表（0xD04A ReplayCockpitParams）
    var propsSamples = [];
    for (i = 0; i < pk.length; i++) {
      var p2 = pk[i];
      if (p2.ptype !== 4) continue;
      var o2 = p2.start + p2.hl;
      if (p2.end - o2 < 12 || payload[o2] !== 0x03 || payload[o2 + 1] !== 0x58 ||
          payload[o2 + 2] !== 0x4a) continue;      /* 先按字节筛，避免 7 万次 subarray */
      var pl2 = payload.subarray(o2, p2.end);
      var props = {}, o = 8;
      while (o + 6 <= pl2.length) {
        props[u16le(pl2, o)] = f32at(pl2, o + 2);
        o += 6;
      }
      if (Object.keys(props).length) propsSamples.push({ ms: p2.ms, props: props });
      if ((i % yieldEvery) === 0) await sleep0();
    }
    propsSamples.sort(function (a, b) { return a.ms - b.ms; });
    var ownProps = buildOwnProps(propsSamples);
    emit('props', 46, '本机属性表 ' + propsSamples.length + ' 行');
    await sleep0();

    // ---- 击杀 / 伤害包
    var kills = [], criticals = [], severes = [], failures = [];
    var killPackets = [], awardsMap = {}, awardsOrder = [], chats = [];
    for (i = 0; i < pk.length; i++) {
      var p3 = pk[i];
      var o3 = p3.start + p3.hl;
      if (p3.end - o3 < 4) continue;
      var b0 = payload[o3];
      if (b0 !== 0x02 && b0 !== 0x03 && p3.ptype === 4) {
        /* 0xF0xx 签名都以 0x02 开头，chat(0x03) 走 ptype=3 —— 先筛掉绝大多数包 */
        continue;
      }
      var pl3 = payload.subarray(o3, p3.end);
      if (p3.ptype === 3 && pl3.length >= 1) {
        var slen = pl3[0];
        var sender = utf8(pl3, 1, Math.min(1 + slen, pl3.length));
        var rest = pl3.subarray(Math.min(1 + slen, pl3.length));
        var msg = '';
        if (rest.length) { var mlen = rest[0]; msg = utf8(rest, 1, Math.min(1 + mlen, rest.length)); }
        chats.push({ tick: p3.ms * TICK_PER_MS, tsMs: p3.ms, sec: rnd(p3.ms / 1000.0, 3),
                     sender: sender, message: msg });
        continue;
      }
      if (p3.ptype !== 4 || pl3.length < 4) continue;
      var sig = (pl3[0] << 24) | (pl3[1] << 16) | (pl3[2] << 8) | pl3[3];
      var msgId = pl3[2] | (pl3[3] << 8);
      var base = { tick: p3.ms * TICK_PER_MS, tsMs: p3.ms, sec: rnd(p3.ms / 1000.0, 3),
                   msgId: '0x' + ('0000' + msgId.toString(16).toUpperCase()).slice(-4) };
      if (pl3[0] === 0x02 && pl3[1] === 0x58 && pl3[2] === 0x58 && pl3[3] === 0xf0) {
        var rec = parseKillPayload(pl3);
        if (rec.err) failures.push(Object.assign({}, base, { error: rec.err }));
        else kills.push(Object.assign({}, base, { tickRaw: p3.ms * TICK_PER_MS, fields: {
          KillerPid: rec.killerPid, PlayerVehicle: rec.killerVehicle,
          VictimUid: rec.victimUid, KillerUid: rec.killerUid, PlayerWeapon: rec.weapon,
          VictimPid: rec.victimPid, DestroyedWeapon: rec.destroyedWeapon } }));
      } else if (pl3[0] === 0x02 && pl3[1] === 0x58 && pl3[2] === 0x56 && pl3[3] === 0xf0) {
        criticals.push(Object.assign({}, base, { fields: parseDamagePayload(pl3, false) }));
      } else if (pl3[0] === 0x02 && pl3[1] === 0x58 && pl3[2] === 0x57 && pl3[3] === 0xf1) {
        severes.push(Object.assign({}, base, { fields: parseDamagePayload(pl3, true) }));
      } else if (msgId === 0xF058) {
        var fv = findLenPrefixedAscii(pl3, 8, 3, 64);
        killPackets.push({ tick: p3.ms * TICK_PER_MS, tsMs: p3.ms, sec: rnd(p3.ms / 1000.0, 3),
                           msgId: '0xF058', strOffset: fv[0], vehicle: fv[1] });
      } else if (msgId === 0xF078) {
        var fa = findLenPrefixedAscii(pl3, 5, 3, 64);
        var key = fa[1] || '<无>';
        var arec = awardsMap[key];
        if (!arec) {
          arec = awardsMap[key] = { award: fa[1], count: 0, firstTsMs: p3.ms, lastTsMs: p3.ms,
                                    firstSec: rnd(p3.ms / 1000.0, 3), lastSec: rnd(p3.ms / 1000.0, 3),
                                    firstTicksRaw: [] };
          awardsOrder.push(key);
        }
        arec.count++;
        arec.lastTsMs = p3.ms;
        arec.lastSec = rnd(p3.ms / 1000.0, 3);
        if (arec.firstTicksRaw.length < 6) arec.firstTicksRaw.push(p3.ms * TICK_PER_MS);
      }
      if ((i % yieldEvery) === 0) await sleep0();
    }
    kills.sort(function (a, b) { return a.tickRaw - b.tickRaw; });
    emit('kills', 52, '击杀包 ' + kills.length + ' / 致命伤 ' + criticals.length + ' / 重伤 ' + severes.length);
    await sleep0();

    // ---- ECS：实体构造 → 地面点 / 机场结构 / 弹药发射点
    emit('ecs', 56, '解 ptype=6 ECS');
    var ecs = new EcsParser();
    var ctrlCensus = {};
    for (i = 0; i < pk.length; i++) {
      var p4 = pk[i];
      if (p4.ptype !== 6) continue;
      var pl4 = payload.subarray(p4.start + p4.hl, p4.end);
      if (!pl4.length) continue;
      var c = ecs.parsePacket(pl4, p4.ms * TICK_PER_MS);
      ctrlCensus[c] = (ctrlCensus[c] || 0) + 1;
      if ((i % yieldEvery) === 0) { emit('ecs', 56 + 12 * i / pk.length, 'ECS 包 ' + i); await sleep0(); }
    }
    await sleep0();

    var eidDeath = {};
    ecs.destructions.forEach(function (d) {
      if (!(String(d.eid) in eidDeath) || d.tick < eidDeath[String(d.eid)]) eidDeath[String(d.eid)] = d.tick;
    });

    var groundRows = [], airfieldStructs = [], launchRows = [];
    ecs.creations.forEach(function (cr) {
      var klass = classifyTemplate(cr.name);
      var blk = cr.block, vs = cr.vstart;
      if (klass === 'ground' || klass === 'structure') {
        var pos = null;
        if (POSITION_TEMPLATES[cr.name] && vs + POSITION_REL + 12 <= blk.length) {
          var x = f32at(blk, vs + POSITION_REL), y = f32at(blk, vs + POSITION_REL + 4),
              z = f32at(blk, vs + POSITION_REL + 8);
          if (isFinite(x) && isFinite(y) && isFinite(z) && Math.abs(x) <= 45000 && Math.abs(z) <= 45000) {
            pos = [rnd(x, 2), rnd(y, 2), rnd(z, 2)];
          }
        }
        /* 朝向：同一 transform 的四元数在 +QUATERNION_REL。
         * 地面单位**不渲染朝向**，但阵营判据要用它（见 meta.limits）。取不到就 null，不猜。 */
        var gq = (klass === 'ground' || klass === 'structure') ? readTransformQuat(blk, vs) : null;
        var gHdg = gq ? rnd(quatHeadingDeg(gq), 1) : null;
        var gid = (klass === 'ground' || klass === 'structure') ? groundIdentity(blk) : null;
        var hasUid = !!(gid && gid.uid !== undefined);
        /* 新版回放的块布局可能变了（固定偏移取不到位置），但 uid/team 规则仍然有效：
         * 这类行不放弃，按 uid 绑同步位置后照样能画；没有位置也没有 uid 的才跳过。 */
        if (pos || hasUid) {
          groundRows.push({ entityId: '0x' + cr.eid.toString(16), entityIndex: cr.eid & 0x3FFFFF,
                            template: cr.name, 'class': klass,
                            unitClassName: findGroundClassname(blk) || null,
                            groundUid: hasUid ? gid.uid : null,
                            groundTeam: gid && gid.team !== undefined ? gid.team : null,
                            spawnTickRaw: cr.tick,
                            destroyedTickRaw: eidDeath[String(cr.eid)] === undefined ? null : eidDeath[String(cr.eid)],
                            x: pos ? pos[0] : null, y: pos ? pos[2] : null, z: pos ? pos[1] : null,
                            hdgDeg: gHdg });
        }
      }
      if (klass === 'projectile') {
        var pt = findProjectileTransform(blk, vs);
        if (pt.pos) {
          launchRows.push({ entityId: '0x' + cr.eid.toString(16), template: cr.name,
                            spawnTickRaw: cr.tick, x: pt.pos[0], y: pt.pos[2], z: pt.pos[1] });
        }
      }
      // 机场结构（关键词命中长度前缀串的实体）
      var keys = blockStrings(blk, 3, 90).filter(function (s) { return AIRFIELD_KEY.test(s); });
      if (keys.length) {
        var p2 = null;
        if (vs + POSITION_REL + 12 <= blk.length) {
          var ax = f32at(blk, vs + POSITION_REL), au = f32at(blk, vs + POSITION_REL + 4),
              az = f32at(blk, vs + POSITION_REL + 8);
          if (isFinite(ax) && isFinite(au) && isFinite(az) &&
              Math.abs(ax) < 45000 && Math.abs(au) < 45000 && Math.abs(az) < 45000) {
            p2 = [rnd(ax, 2), rnd(az, 2), rnd(au, 2)];
          }
        }
        airfieldStructs.push({ entityId: '0x' + cr.eid.toString(16), template: cr.name,
                               className: keys[0].split('#')[0], spawnTickRaw: cr.tick,
                               blockSize: cr.bs,
                               x: p2 ? p2[0] : null, y: p2 ? p2[1] : null, z: p2 ? p2[2] : null });
      }
    });

    // ground.json 的顺序：按 (class, entityId) 排序后再编号（决定 key 的后缀）
    groundRows.sort(function (a, b) {
      return cmpStr(a['class'], b['class']) || cmpStr(a.entityId || '', b.entityId || '');
    });
    var markers = extractRunwayMarkers(payload, pk);
    var runwayData = buildRunwayData(airfieldStructs, markers);
    emit('ecs', 70, '地面点 ' + groundRows.length + ' / 跑道 ' + runwayData.runways.length +
                    ' / 机场 ' + runwayData.airfields.length + ' / 弹药 ' + launchRows.length);
    await sleep0();

    // ---- 地面同步位置（ptype=4 MPI 0xF073/0xF074）→ 绑定到 ECS 构造点
    var groundSync = parseGroundSync(payload, pk);
    var groundBind = bindGroundTracks(groundRows, groundSync);
    var weaponPos = parseWeaponSync(payload, pk);
    emit('ecs', 73, '弹药同步 ' + weaponPos.stats.rockets + ' 个导弹实体 / ' +
                    weaponPos.stats.bombs + ' 个炸弹实体（' + weaponPos.stats.errors + ' 包失败）');
    emit('ecs', 72, '地面同步 ' + groundSync.stats.uids + ' uid / ' +
                    groundSync.stats.samples + ' 采样 → 绑定 ' + groundBind.matched + ' 个 ECS 点');
    await sleep0();

    // ---- 结算 BLK（玩家表 / 作者 / squadId）
    emit('blk', 74, '解析结算 BLK');
    var results = {}, blkMeta = null;
    if (info.resultsOffset) {
      var blk = parseFatBlk(d.subarray(info.resultsOffset));
      results = blk.tree;
      blkMeta = blk.meta;
    }
    var players = mergePlayers(results);
    var playerVeh = playerVehicles(results);
    await sleep0();

    // ---- 本机 uid：用本机高度剖面与各航迹做相关（纯回放判据）
    var playerName = results.author || '';
    var authorId = String(results.authorUserId || '');
    var authorRow = players.filter(function (x) { return String(x.userId) === authorId; })[0] || null;
    var authorVeh = (playerVeh.filter(function (x) { return String(x.userId) === authorId; })[0] || {}).primaryCraft || null;

    var axisProbe = new Axis(SEC_PER_TICK, tick0, Math.max(tickLast, 0), 256);
    var unitRowsByUid = {}, trackUnits = tracksUnits.map(function (u) {
      return { entityId: u.entityId, uid: u.uid, samples: u.samples, entityIndex: 0 };
    });
    trackUnits.forEach(function (u) {
      unitRowsByUid[u.uid] = unitRowsByUid[u.uid] || [];
      unitSamples(u).forEach(function (r) { unitRowsByUid[u.uid].push(r); });
    });
    var ownPick = pickOwnUnit(trackUnits, authorVeh, ownProps, axisProbe, uidMapProbe());
    function uidMapProbe() { return {}; }
    var ownUid = ownPick.uid;
    emit('own', 78, '本机 uid=' + ownUid + '（' + ownPick.how + '）');
    await sleep0();

    // ---- 玩家槽位（ptype=4 MPI 0xF02D）→ 玩家名字/队伍/氏族
    var slotData = parseSlotPlayers(payload, pk);
    if (opts.debug) opts.debug.slotStats = slotData.stats;
    emit('slot', 76, '玩家槽位 ' + slotData.stats.players + ' 人 / ' +
                     slotData.stats.messages + ' 条消息');
    await sleep0();

    // ---- uid_map（ECS → 载具/玩家/阵营）
    var um = buildUidMap(ecs, players, results, ownUid, slotData.players);
    var uidMap = um.map;
    // 本机锚定后重新选一次（uid_map 现在有 vehicle/player）
    var ownPick2 = pickOwnUnit(trackUnits, authorVeh, ownProps, axisProbe, uidMap);
    ownUid = ownPick2.uid;
    if (opts.debug) {
      opts.debug.authorVeh = authorVeh;
      opts.debug.ownPick = ownPick2;
      opts.debug.ecsStats = ecs.stats;
      opts.debug.ctrlCensus = ctrlCensus;
      opts.debug.creations = ecs.creations.length;
      opts.debug.segStats = segStats;
      opts.debug.tracksUnits = tracksUnits.length;
    }
    var ownEntityIds = {};
    (ownPick2.segments || []).forEach(function (s) { ownEntityIds[s.entityId] = 1; });

    // ---- 阵营终判（纯回放：击杀关系二色 + 出生位置聚类）
    var killPositions = kills.map(function (k) {
      var f = k.fields || {};
      var vu = (f.VictimUid === undefined || f.VictimUid === 0x7FF) ? null : f.VictimUid;
      var ku = (f.KillerUid === undefined || f.KillerUid === 0x7FF) ? null : f.KillerUid;
      var tick = k.tickRaw;
      function posAt(uid) {
        if (uid === null) return null;
        var key = 'uid-' + uid, arr = byuid[key];
        if (!arr || !arr.length) return null;
        var ts = arr.map(function (a) { return a[0]; });
        var idx = bisectRight(ts, tick);
        var cand = [idx - 1, idx].filter(function (j) { return j >= 0 && j < arr.length; });
        if (!cand.length) return null;
        var before = cand.filter(function (j) { return ts[j] <= tick; });
        var j = before.length ? Math.max.apply(null, before)
          : cand.reduce(function (a, b2) { return Math.abs(ts[a] - tick) <= Math.abs(ts[b2] - tick) ? a : b2; });
        if (Math.abs(ts[j] - tick) > 2 * TICK_PER_MS * 1000) return null;
        var a = arr[j];
        return [pyRound(a[2], 0), pyRound(a[4], 0), rnd(a[3], 1)];
      }
      var vp = posAt(vu), kp = posAt(ku);
      return { tickRaw: tick, victimUid: vu, killerUid: ku, victimPid: f.VictimPid,
               killerPid: f.KillerPid, killerVehicle: f.PlayerVehicle || null,
               weapon: f.PlayerWeapon || null, victimPos: vp, killerPos: kp,
               hasVictimPos: vp !== null, hasKillerPos: kp !== null };
    });
    var sideFin = buildSideFinal({ units: tracksUnits }, uidMap, ownUid, killPositions);
    var sideFinalMap = {};
    Object.keys(sideFin.side).forEach(function (k) {
      if (sideFin.side[k] === 'ally' || sideFin.side[k] === 'enemy') sideFinalMap[k] = sideFin.side[k];
    });
    emit('side', 82, '阵营：ally ' + Object.keys(sideFinalMap).filter(function (k) { return sideFinalMap[k] === 'ally'; }).length +
                     ' / enemy ' + Object.keys(sideFinalMap).filter(function (k) { return sideFinalMap[k] === 'enemy'; }).length);
    await sleep0();

    // ---- 时间轴
    var endMarkerMs = null;
    pk.forEach(function (p) { if (p.ptype === 0) endMarkerMs = p.ms; });
    var endMarkerRaw = endMarkerMs === null ? null : endMarkerMs * TICK_PER_MS;
    var axis = new Axis(SEC_PER_TICK, tick0,
                        Math.max.apply(null, [tickLast].concat(endMarkerRaw === null ? [] : [endMarkerRaw])), 256);
    var allTicks = [];
    tracksUnits.forEach(function (u) { u.samples.forEach(function (s) { allTicks.push(s[0]); }); });
    // 注意：不要用 Math.min.apply(null, arr) —— 采样数上万后参数个数超限会栈溢出
    // （旧版本大回放实测：RangeError: Maximum call stack size exceeded）。用循环。
    var tickLo = axis.tick0, tickHi = axis.tick0;
    if (allTicks.length) {
      tickLo = allTicks[0]; tickHi = allTicks[0];
      for (var ti = 1; ti < allTicks.length; ti++) {
        if (allTicks[ti] < tickLo) tickLo = allTicks[ti];
        if (allTicks[ti] > tickHi) tickHi = allTicks[ti];
      }
    }
    var t1 = Math.max(0.0, (tickHi - axis.tick0) * axis.sec_per_tick);
    if (endMarkerRaw !== null) t1 = Math.max(t1, axis.sec(endMarkerRaw));
    var window_ = { t0: 0.0, t1: rnd(t1, 3), duration: rnd(t1, 3) };

    var yv = decideYFlip(unitRowsByUid, ownUid, axisFromClockSeries(ownProps.hdg_deg, axis), axis);
    var ysign = yv.flip ? -1.0 : 1.0;

    // 导弹弹道（只取 class 0；弹体不画，场景里做白色虚线）
    var missiles = [];
    Object.keys(weaponPos.rockets).forEach(function (ent) {
      var raw = weaponPos.rockets[ent].slice().sort(function (a, b) { return a.tick - b.tick; });
      var pts = [];
      raw.forEach(function (r0) {
        if (pts.length && r0.tick <= pts[pts.length - 1].tick) return;
        if (Math.abs(r0.x) > 45000 || Math.abs(r0.z) > 45000) return;
        pts.push(r0);
      });
      if (pts.length < 2) return;
      var t = [], x = [], y = [], z = [];
      pts.forEach(function (r0) {
        t.push(rnd(axis.sec(r0.tick), 3));
        x.push(rnd(r0.x, 1));
        y.push(rnd(r0.z * ysign, 1));
        z.push(rnd(r0.y, 1));
      });
      missiles.push({ entity: parseInt(ent, 10), t0: t[0], n: t.length, t: t, x: x, y: y, z: z });
    });
    missiles.sort(function (a, b) { return (a.t0 - b.t0) || (a.entity - b.entity); });

    // ---- units（空中）+ 地面
    var displayNames = {}, hiddenAll = {};
    HIDDEN_TEMPLATES.concat(HIDDEN_EXTRA).forEach(function (k) { hiddenAll[k] = 1; });
    Object.keys(DISPLAY_NAMES).forEach(function (k) {
      if (DISPLAY_NAMES[k] === '') hiddenAll[k] = 1;
      else displayNames[k] = DISPLAY_NAMES[k];
    });
    var vHints = {};
    kills.forEach(function (k) {
      var f = k.fields || {};
      addHint(vHints, f.KillerUid, f.PlayerVehicle, 'kill_packet');
    });
    criticals.concat(severes).forEach(function (c) {
      var f = c.fields || {};
      if (f.PlayerUID !== undefined) addHint(vHints, f.PlayerUID, f.vehicle, 'damage_packet');
    });
    function addHint(hints, uid, vehicle, source) {
      if (uid === null || uid === undefined || uid === 2047 || !vehicle) return;
      var s = hints[String(uid)] || (hints[String(uid)] = { vehicles: {}, sources: {} });
      s.vehicles[vehicle] = 1; s.sources[source] = 1;
    }
    var deaths = killPositions.filter(function (kp) { return kp.victimUid !== null; })
      .map(function (kp) { return [axis.sec(kp.tickRaw), kp.tickRaw, kp.victimUid, kp.killerUid]; })
      .sort(function (a, b) { return a[0] - b[0]; });

    var bu = buildUnits({ units: tracksUnits }, axis, ownEntityIds, ysign, uidMap, vHints,
                        sideFinalMap, displayNames, deaths, hiddenAll, ownUid);
    var units = bu.units;
    (function patchAirDeathByEcs() {
      var deathByUid = {};
      Object.keys(uidMap).forEach(function (u) {
        var e = uidMap[u] && uidMap[u].entityId;
        if (!e) return;
        var t = eidDeath[String(parseInt(e, 16))];
        if (t !== undefined) deathByUid[u] = t;
      });
      var patched = 0;
      units.forEach(function (u) {
        if (u.domain !== 'air') return;
        var t = deathByUid[String(u.uid)];
        if (t === undefined) return;
        var endTick = (u.t0 + (u.n - 1) / (u.hz || 10)) / axis.sec_per_tick + axis.tick0;
        if (t < axis.tick0 || t > endTick + axis.raw_per_ms * 1000) return;   /* 不在该段内：不裁 */
        if (u.deathTickRaw == null || t < u.deathTickRaw) {
          u.deathTickRaw = t;
          u.deathKind = 'destroyed';
          patched++;
        }
      });
      if (opts.debug) opts.debug.airDeathPatched = patched;
    })();
    emit('units', 88, '空中航迹 ' + units.length + ' 条');
    await sleep0();

    // ---- 本机 10 Hz
    var ownSegs = ownPick2.segments || [];
    var bo = buildOwn({ units: tracksUnits }, axis, window_.t1, ownSegs, ownProps, ysign);

    /* —— 出生朝向补判阵营（只用于"没有击杀边证据"的空中单位）——
     * 依据：出生后所有飞机都朝敌方方向飞。以本机出生朝向为基准：
     *   cos > +0.35 → ally；cos < -0.35 → enemy；其余保持原值（不猜）。
     * 实测（2026.10.22 TEST1 / air_falklands）：有击杀边证据的 35 架里 34 架与本判据一致；
     * 6 架 mig-15bis_ish 因此从 enemy 纠正为 ally（A-4B 判 enemy 不变，本来就对）。 */
    (function patchSidesBySpawnHeading() {
      function headingOf(u, minDist) {
        if (!u || !u.x || !u.y || !u.n) { return null; }
        var i0 = -1;
        for (var i = 0; i < u.n; i++) {
          if (u.x[i] !== null && u.y[i] !== null) { i0 = i; break; }
        }
        if (i0 < 0) { return null; }
        var x0 = u.x[i0], y0 = u.y[i0];
        for (var j = i0 + 1; j < u.n; j++) {
          if (u.x[j] === null || u.y[j] === null) { continue; }
          var dx = u.x[j] - x0, dy = u.y[j] - y0;
          if (dx * dx + dy * dy >= minDist * minDist) {
            return Math.atan2(dx, dy) * 180 / Math.PI;
          }
        }
        return null;
      }
      var edged = {};
      killPositions.forEach(function (kp) {
        if (kp.killerUid !== null) { edged[String(kp.killerUid)] = 1; }
        if (kp.victimUid !== null) { edged[String(kp.victimUid)] = 1; }
      });
      var ownH = headingOf(bo.own, 300);
      if (ownH === null) {
        console.log('[side] 本机无初始位移，跳过出生朝向补判');
        return;
      }
      var fixed = 0, kept = 0, flipped = [];
      units.forEach(function (u) {
        if (u.domain !== 'air' || u.uid === null) { return; }
        if (edged[String(u.uid)]) { return; }            /* 有击杀边证据：不动 */
        var h = headingOf(u, 300);
        if (h === null) { return; }
        var cos = Math.cos((h - ownH) * Math.PI / 180);
        var guess = cos > 0.35 ? 'ally' : (cos < -0.35 ? 'enemy' : null);
        if (guess === null) { kept++; return; }
        if (u.side !== guess) {
          flipped.push((u.vehicle || u.key) + ':' + u.side + '→' + guess);
          /* key 里带阵营前缀（aly-/eny-/unk-），改判时一起换掉，避免"蓝色单位却叫 eny-xxx" */
          if (u.key && /^(aly|eny|unk|sqd)-/.test(u.key)) {
            u.key = u.key.replace(/^(aly|eny|unk|sqd)-/, SIDE_CODE[guess] + '-');
          }
        }
        u.side = guess;
        u.sideSource = 'spawn-heading(cos=' + cos.toFixed(2) + ')';
        fixed++;
      });
      console.log('[side] 出生朝向补判 %d 架（改动 %d：%s），其余 %d 架朝向不明确保持原值',
        fixed, flipped.length, flipped.join(', ') || '无', kept);
    })();

    // ---- 地面单位（阵营优先用 ECS 的 team 组件，取不到才回落到出生中心推断）
    var authorTeam = authorRow ? authorRow.team : null;
    var groundUnits = buildGroundUnits(groundRows, axis, displayNames, sideFin.centers, hiddenAll,
                                       groundBind.bound, ysign, authorTeam);
    var groundAnon = buildAnonymousGroundTracks(groundSync, groundBind, axis, ysign);
    units = units.concat(groundUnits, groundAnon);
    units.sort(function (a, b) {
      var da = a.domain === 'air' ? 0 : 1, db = b.domain === 'air' ? 0 : 1;
      return (da - db) || (a.t0 - b.t0) || cmpStr(a.key, b.key);
    });

    var runways = buildRunwaysOut(runwayData);
    var airfields = buildAirfieldsOut(runwayData);
    var bs2 = buildShots(launchRows, axis, units, window_);
    var playerUid = ownUid;
    var ev = buildEvents({}, { awards: awardsOrder.map(function (k) { return awardsMap[k]; }), chat: chats },
                         axis, units, window_, uidMap,
                         { kills: kills.map(function (k) {
                             var f = k.fields || {};
                             return { tickRaw: k.tickRaw, killerUid: f.KillerUid, victimUid: f.VictimUid,
                                      killerVehicle: f.PlayerVehicle, weapon: f.PlayerWeapon,
                                      victimPid: f.VictimPid, killerPid: f.KillerPid };
                           }) },
                         playerUid, ysign, killPositions);
    var events = ev.events;

    // ---- 小队
    var killsByUid = {}, deathsByUid = {};
    killPositions.forEach(function (kp) {
      if (kp.killerUid !== null) killsByUid[kp.killerUid] = (killsByUid[kp.killerUid] || 0) + 1;
      if (kp.victimUid !== null) deathsByUid[kp.victimUid] = (deathsByUid[kp.victimUid] || 0) + 1;
    });
    var squad = buildSquad(players, results, uidMap, ownUid, killsByUid, deathsByUid, killPositions.length);

    var envStats = envStatsOf({ units: tracksUnits }, axis);

    var counts = {
      ownSamples: bo.own.n, trackCount: units.length,
      airTracks: units.filter(function (u) { return u.domain === 'air'; }).length,
      groundTracks: units.filter(function (u) { return u.domain === 'ground'; }).length,
      groundTracksMoving: units.filter(function (u) { return u.domain === 'ground' && u['static'] === false; }).length,
      groundSyncUids: groundSync.stats.uids, groundSyncSamples: groundSync.stats.samples,
      groundSyncPackets: groundSync.stats.packets, groundSyncMatched: groundBind.matched,
      groundTracksAnonymous: groundAnon.length,
      missiles: missiles.length,
      missileSamples: missiles.reduce(function (n2, m) { return n2 + m.n; }, 0),
      groundTeamKnown: units.filter(function (u) { return u.domain === 'ground' && u.groundTeam != null; }).length,
      groundSideFromTeam: units.filter(function (u) { return u.domain === 'ground' && u.sideSource === 'ecs:team'; }).length,
      groundUnits: groundUnits.length, shots: bs2.shots.length,
      unitsWithSide: units.filter(function (u) { return u.side !== 'unknown'; }).length,
      unitsWithDisplayName: units.filter(function (u) { return !!u.vehicleName; }).length,
      unitsWithCategory: units.filter(function (u) { return !!u.vehicleCategory; }).length,
      unitsHidden: units.filter(function (u) { return u.hidden; }).length,
      squadSize: squad ? (squad.members || []).length : 0,
      runways: runways.length, airfields: airfields.length, terrainContours: 0,
      unitsWithDeath: units.filter(function (u) { return u.deathTickRaw; }).length,
      events: events.length, kills: events.filter(function (e) { return e.kind === 'kill'; }).length,
      ownKillsByEvents: events.filter(function (e) { return e.isPlayerActor && e.kind === 'kill'; }).length,
      ownKillsFromResults: authorRow ? authorRow.kills : null,
      unitSeamsSplit: bu.breaks.length, ownSegments: ownSegs.length,
      envelopeFiltered: bu.filtered + bo.stats.envelope_filtered,
      unitsWithIdentity: units.filter(function (u) { return !!u.vehicle; }).length,
      unitsVehicleFromPackets: units.filter(function (u) {
        return (u.vehicleSource || '').slice(-6) === 'packet'; }).length,
      killsWithWeapon: events.filter(function (e) { return !!e.weapon; }).length,
      killsWithWeaponName: events.filter(function (e) { return !!e.weaponName; }).length
    };

    var player = {
      name: playerName, vehicle: authorVeh,
      vehicleName: authorVeh ? (DISPLAY_NAMES[authorVeh] || String(authorVeh).toUpperCase()) : null,
      callsign: authorRow && authorRow.clanTag ? authorRow.clanTag.replace(/^[=\s]+|[=\s]+$/g, '') || null : null,
      userId: authorId || null, clanTag: (authorRow && authorRow.clanTag) || null,
      team: authorRow ? authorRow.team : null, slot: authorRow ? authorRow.slot : null,
      kills: authorRow ? authorRow.kills : null, humanKills: authorRow ? authorRow.humanKills : null,
      groundKills: authorRow ? authorRow.groundKills : null
    };

    var limits = [
      '本页在**浏览器内**解出回放（自带 zstd 解码），不联网、不依赖服务器数据。',
      'kill 事件来自回放击杀包的位流字段：击杀者/受击者 uid、载具、武器；uid 经 ECS 映射折算' +
      '成人名与阵营。文本是拼合的（composed=true，回放里没有本地化原文）。',
      '33 条击杀里有 1 条 killerUid=2047（回放用 0x7FF 表示「无」）= 坠毁/环境击杀，文本写明' +
      '「无击杀者：坠毁/环境」（kind 仍是 kill，以保持与击杀包一一对应）。本机（uid=' + ownUid +
      '）作为击杀者的 kill 事件 = counts.ownKillsByEvents = ' + counts.ownKillsByEvents +
      '，内嵌结算的 counts.ownKillsFromResults=' + counts.ownKillsFromResults + '。',
      '玩家名大多不可用：uid 映射只在唯一时才填 player。names 因此为空对象，标签显示机种。',
      '本机飞参（高度/表速/Mach/过载/航向/俯仰/滚转/升降率/油量/油门）来自回放的' +
      ' ReplayCockpitParams 属性表（约 4 Hz，插值到 10 Hz）；回放确实没有的通道（迎角 aoa、弹药 ammo）留 null。',
      '他机身份来自 ptype=6 ECS 的 uid → 载具映射；映射不到的保持 unknown/null，不用外部数据补。',
      'side 的主来源是纯回放的（击杀关系二色 + 出生位置聚类），ECS 原始取值保留在 units[].sideDecoded。',
      '地面阵营优先用回放 ECS 的 team 组件（int32，1=蓝 2=红）：块内型号串第一次出现−60 是 uid、' +
      '最后一次出现−5 是 team；6 份回放 715 个地面单位与上游 wrpl-acmi 的 ecs:team 逐单位一致（0 分歧）。' +
      '取不到 team 时才回落到出生中心推断（sideInferred=true）。',
      '地面同步位置来自 ptype=4 的 MPI 0xF073/0xF074（GroundModelPositions，增量+RLE），' +
      '本局 ' + groundSync.stats.packets + ' 包 / ' + groundSync.stats.uids + ' uid / ' +
      groundSync.stats.samples + ' 采样；按首采样与 ECS 构造点水平距离 ≤' + GROUND_SPAWN_MATCH_M +
      ' m 唯一绑定，绑定 ' + groundBind.matched + ' 个；绑不上的静态点原样保留，不按距离猜实体。' +
      '1 Hz 阶梯保持：不插值，避免造出比回放采样（中位 5.5 s）更平滑的运动。',
      '地面同步 uids 里绑定不到 ECS 构造点、但确实移动过的，输出为无身份地面单位（vehicle=null、' +
      'vehicleName=GROUND UNIT、side=unknown），与上游 wrpl-acmi 的 Ground unit 口径一致；本局 ' +
      groundAnon.length + ' 个。不按距离猜型号或阵营。',
      '导弹弹道来自 ptype=4 的 WeaponSync（0xF0DB / 0xF11A），class 0 记录；' +
      '弹体不画，场景用白色虚线表示已飞过的航迹；可在设置里关闭。',
      '地面同步位置自带高度（本局 13–246 m），动起来的地面单位直接用同步高度；' +
      '取不到时回落到 ECS 构造点高度，不用 0/常量硬套。',
      '源数据里个别瞬时跳变按契约 §5.2 做了包线滤除（与上一个有效采样推算速度 >600 m/s 的点置 null，' +
      '不做平滑），共 ' + counts.envelopeFiltered + ' 点。',
      '显示名（units[].vehicleName）是**显示约定**（内嵌表，来自 War-Thunder-Datamine 的英文名），' +
      '不是回放数据；查不到就留 null，场景应原样打印 units[].vehicle。',
      '跑道线段（runways[]）由回放自身的机场标记点（t1/t2_airfield_top[_reload] 的位置 + 航向）' +
      '加上其引用模型名给出的 3000 m 长度反推，不是关卡几何。',
      '**浏览器版**：8111 开发期校验数字一律 null（浏览器拿不到 8111）；车辆显示名用内嵌显示约定表；' +
      'terrain 等高线不内嵌，由页面按 meta.replayHeader.level 的地图名向服务器加载静态素材' +
      '（加载时校验素材自报的 map 与本局关卡一致、crs 为 replay-world-meters，否则留空不编）。',
      '本机 uid 由回放自身的本机高度剖面与各航迹做相关选出（' + ownPick2.how + '）。',
      'uid 会复用：同一 uid 的航迹在相邻采样推算速度 >600 m/s 处被断开成多条（key 加 `#n` 后缀），' +
      '共断开 ' + bu.breaks.length + ' 处、丢弃过短碎片 ' + bu.dropped.length + ' 条。',
      '他机航迹来自 ptype=2 条目的到达率（约 4 Hz），输出重采样成 10 Hz（Catmull-Rom），缺采样处不插值。',
      '时间轴原点与 tracks.json 的 tick0 相同（raw tick ' + axis.tick0 + '）；窗口 0..' +
      window_.t1 + ' s；回放自身总长 ' + rnd(axis.sec(axis.tick_last), 3) + ' s（EndMarker）。',
      '聊天文本去掉了零宽字符与 <color> 标签，其余原样。'
    ];

    var meta = {
      source: 'wrpl',
      markerNames: Object.keys(markers),
      sideSource: '空中：击杀关系二色 + 出生位置聚类；地面：ECS team 组件（1=蓝/2=红，取不到才用出生中心推断）',
      replayFile: opts.fileName || null,
      replayHeader: hdr,
      player: player,
      matchResults: buildMatchResults(players, results, hdr),
      squad: squad,
      replayTicks: { tick0: axis.tick0, tickLast: axis.tick_last, secPerTick: axis.sec_per_tick,
                     packets: pk.length, killPackets: kills.length },
      window: window_,
      grid: { stepX: 6500, stepY: 6500, note: '显示约定；本局地图网格 6500 m（开发期从地图信息核对）' },
      units: { pos: 'm', alt: 'm', hdg: 'deg', roll: 'deg', pitch: 'deg', aoa: 'deg', g: 'g',
               vy: 'm/s', ias: 'm/s', tas: 'm/s', mach: 'Ma', fuel: 'kg', ammo: 'rounds', throttle: '%' },
      counts: counts,
      devValidation: {
        ownMaxErrM: null, ownRmsErrM: null, ownSamplesCompared: propsSamples.length,
        unitAssocRate: null, speedViolations: envStats.violations,
        speedViolationsBeforeSplit: null, trackSegments: segStats.segments,
        segmentsDroppedShort: segStats.droppedShort, ownAltRmsVs8111M: null, cfgOffsetSec: null,
        unitCountDecoded: Object.keys(byuid).length, unitSamplesCompared: rows.length,
        speedViolationsTracks: envStats.violations, groundCount: groundUnits.length,
        groundNearestContactMedianM: null, groundFracWithin100M: null,
        groundControlWrongAxisMedianM: null,
        ownCorrVs8111: null, envSteps: envStats.steps, envTeleports: envStats.teleports,
        killDistN: killPositions.filter(function (k) { return k.hasVictimPos && k.hasKillerPos; }).length,
        killDistP50M: null, killDistP90M: null, killDistMaxM: null, killDistUnder3km: null,
        shotsCount: launchRows.length, shotsWithPosition: launchRows.length,
        shotsMedianDistToNearestAircraftM: null, shotsP90DistM: null, shotsWithin100m: null,
        shotsNote: '发射点位置校验由 wrpl-ecs/wrpl-probe 在开发期完成，浏览器版未复算，透传为 null。',
        groundNote: '地面点位校验（对 8111 地面接触点）只在开发期做过，浏览器版取不到 8111，故为 null。' +
                    '地面同步包在本局解出 ' + groundSync.stats.samples + ' 个位置采样 / ' +
                    groundSync.stats.uids + ' uid（' + groundSync.stats.errors + ' 个包失败），' +
                    '其中 ' + groundBind.matched + ' 个 uid 绑定到 ECS 构造点。',
        note: '浏览器版拿不到 8111 真值，所以 8111 派生字段（ownMaxErrM/ownRmsErrM/unitAssocRate/' +
              'groundNearestContactMedianM 等）一律 null；speedViolations 与 killDist* 是本文件在回放内部' +
              '自算的。Python 版同一份回放的数字见 ace-combat/docs/WRPL-JS.md。'
      },
      vehicleRoster: buildRoster(playerVeh),
      provenance: [
        '回放文件：' + (opts.fileName || '(上传)') + '（' + d.length + ' 字节，' +
          info.compression + ' → ' + pk.length + ' 包）',
        '地图/模式/环境：' + hdr.level + ' / ' + hdr.battleClass + ' / ' + hdr.environment +
          ' / ' + hdr.visibility + '（' + hdr.timeLimit + ' 分钟，' + hdr.scoreLimit + ' 分上限）',
        '时间轴：回放 tick（raw），sec = tick / 256000，原点 ' + axis.source,
        '本机航迹：' + ownPick2.how + '；他机航迹：ptype=2 实体三维航迹',
        '他机身份：ptype=6 ECS 的 uid→载具/玩家映射（' + Object.keys(uidMap).length + ' 条 uid）',
        '击杀事件字段：击杀包位流（IdFieldSerializer32）；击杀点坐标优先用受击者位置',
        '导弹弹道：ptype=4 WeaponSync 0xF0DB/0xF11A（class 0），' + missiles.length + ' 条，场景白色虚线',
        '地面移动：ptype=4 MPI 0xF073/0xF074 增量位置（RLE 0ki + XOR 历史），' +
          '按 ECS 构造点绑定 uid（≤' + GROUND_SPAWN_MATCH_M + ' m）；解析参考 wrpl-acmi dev 分支',
        '坐标轴：x=fm.PosX, y=fm.PosZ, z=fm.PosY（米）；y 轴处理：' +
          (yv.flip ? '取反' : '保持原样') + '（判据：本机位移方向 vs 回放罗盘，样本 ' + yv.n + '）',
        '结算：.wrpl 内嵌结果块（author=' + playerName + '，' + players.length + ' 名玩家）',
        '浏览器内解析：vendor/fzstd.min.js（MIT）解 zstd；其余为自写 JS',
        '回放格式解析参考开源实现 wrpl-inspector（https://github.com/maxsupermanhd/wrpl-inspector，AGPL-3.0）'
      ],
      limits: limits
    };

    // 飘带用的横滚角序列：window.ACE_ATTITUDE 的内容（由对外 parse() 在主线程赋值）
    var attitude = buildAttitude(eulerByUid, axis.tick0, axis.sec_per_tick);
    limits.push('地面/工事单位的 units[].hdg 来自其 transform 四元数（0=正北、顺时针，度；' +
                '单元素数组）：只用于判阵营，**不渲染朝向**；取不到四元数时为 null。' +
                '口径与上游 wrpl-ecs 的 ecs_entities.json.hdgDeg 一致（本局 184/184，最大偏差 0.05°）。');
    limits.push('飘带坡度来自 FM（EulerBytes 的 bank 分量）→ 度，0.25 s 固定网格重采样' +
                '（' + attitude.uidCount + ' 个 uid / ' + attitude.points + ' 个点，纯回放数据）。');
    meta.attitude = { uidCount: attitude.uidCount, points: attitude.points, dt: attitude.dt };

    var mission = { meta: meta, own: bo.own, units: units, events: events, shots: bs2.shots,
                    missiles: missiles,
                    runways: runways, airfields: airfields, terrain: null, names: {},
                    __attitude: attitude };
    emit('done', 100, '完成（' + ((Date.now() - t_start) / 1000).toFixed(2) + ' s）');
    return mission;
  }

  /** 把本次解析的横滚序列挂到 window.ACE_ATTITUDE（three3d 的飘带读它）。
   *  **整体替换**而不是累计：换一份回放时旧 uid 不会残留。 */
  function publishAttitude(mission) {
    var att = mission ? mission.__attitude : null;
    if (mission) delete mission.__attitude;
    if (att && att.units && Object.keys(att.units).length) {
      global.ACE_ATTITUDE = att;
    } else {
      global.ACE_ATTITUDE = null;
    }
    return global.ACE_ATTITUDE;
  }

  /**
   * 对外入口（接口不变）：优先走 Web Worker，把解析挪出主线程；
   * Worker 不可用或建不起来时**自动退回主线程**，结果一致、只是主线程会卡。
   * opts.force = 'main' | 'worker' 可强制某条路径（用于对拍/性能测量）。
   */
  async function parse(arrayBuffer, opts) {
    opts = opts || {};
    var force = opts.force || null;
    global.ACE_ATTITUDE = null;                 // 每次解析从空开始（不累计）
    // 已经在 Worker 里（wrpl-worker.js）就不要再套一层：直接主线程路径跑完再回传结果
    var insideWorker = (typeof importScripts === 'function') || (typeof document === 'undefined');
    if (force !== 'main' && !insideWorker && workerSupported()) {
      try {
        var mW = await parseInWorker(arrayBuffer, opts,
          '解析路径：Web Worker（' + (force === 'worker' ? '强制' : '自动') + '；主线程只收结果）');
        publishAttitude(mW);
        return mW;
      } catch (e) {
        if (e && (e.code === 'worker-unavailable') && force !== 'worker' &&
            !(arrayBuffer instanceof Uint8Array) && arrayBuffer.byteLength) {
          // 只有在「建不起来」时才回退；Worker 里真正的解析错误原样抛出（避免解析两遍）
          if (opts.debug) opts.debug.workerFallback = String(e && e.message || e);
          var m2 = await parseMain(arrayBuffer, opts);
          m2.meta.limits.push('解析路径：主线程回退（Worker 不可用：' + (e && e.message || e) + '）');
          m2.meta.provenance.push('解析路径：主线程回退（Worker 不可用）');
          publishAttitude(m2);
          return m2;
        }
        throw e;
      }
    }
    if (opts.debug) opts.debug.workerFallback = force === 'main' ? 'force=main' : 'not supported';
    var mMain = await parseMain(arrayBuffer, opts);
    /* 两条路径的 meta 结构保持一致：主线程路径没有 worker 堆峰值，显式给 null */
    if (mMain && mMain.meta && !('__workerHeapPeakMB' in mMain.meta)) {
      mMain.meta.__workerHeapPeakMB = null;
    }
    /* 环境本身不支持 Worker（老浏览器 / 被策略禁用）时，也要在 limits 里写明走的哪条路 */
    if (force !== 'main' && !insideWorker && !workerSupported() && mMain && mMain.meta) {
      mMain.meta.limits.push('解析路径：主线程（环境不支持 Web Worker）');
      mMain.meta.provenance.push('解析路径：主线程（环境不支持 Web Worker）');
    }
    if (force === 'main' && !insideWorker && mMain && mMain.meta) {
      mMain.meta.limits.push('解析路径：主线程（调用方强制 force=main）');
      mMain.meta.provenance.push('解析路径：主线程（强制）');
    }
    publishAttitude(mMain);
    return mMain;
  }

  function buildMatchResults(players, results, hdr) {
    var authorId = String(results.authorUserId || '');
    var matched = results.timePlayed !== undefined && results.timePlayed !== null
      ? String(results.timePlayed) : null;
    var rows = players.map(function (p) {
      return { userId: p.userId, name: p.name, clanTag: p.clanTag || '',
               kills: p.kills, humanKills: p.humanKills, groundKills: p.groundKills,
               navalKills: p.navalKills,
               timePlayed: String(p.userId) === authorId ? matched : null };
    });
    if (!rows.length) return null;
    // raw 保留结算 BLK 的树，但**去掉 uiScriptsData**（32 名玩家的 crafts 子树很大，
    // 与 Python 交付文件的取舍一致；players[] 已把需要的字段提出来了）。
    var raw = null;
    if (results && Object.keys(results).length) {
      raw = {};
      Object.keys(results).forEach(function (k) {
        if (k === 'uiScriptsData') return;
        raw[k] = results[k];
      });
    }
    return { players: rows, raw: raw,
             mode: hdr.battleClass, scoreLimit: hdr.scoreLimit, timeLimitMin: hdr.timeLimit,
             matchSeconds: results.timePlayed };
  }

  function buildRoster(playerVeh) {
    var roster = {};
    playerVeh.forEach(function (p) {
      var name = p.name, tag = p.clanTag || '';
      var label = tag ? ((tag + ' ' + name).replace(/^\s+|\s+$/g, '')) : name;
      (p.vehicles || []).forEach(function (v) {
        var craft = v && v.craft;
        if (craft && label) (roster[craft] = roster[craft] || {})[label] = 1;
      });
    });
    var out = {};
    Object.keys(roster).sort(cmpStr).forEach(function (k) { out[k] = Object.keys(roster[k]).sort(cmpStr); });
    return out;
  }

  /** 本机 uid：用本机属性表（ReplayCockpitParams）的高度序列认领（**纯回放判据**，照 wrpl_decode.validate 的
   *  "用回放自身高度序列认领"）：在**回放时钟秒**上把 alt_m 插值到每条航迹的采样时刻
   *  （最近样本 ≤0.3 s），比 rms(z − alt)，最小者即本机。本局最优 rms ≈ 2 m、次优 >400 m，
   *  判据很干净。（早先用 Pearson 相关+0.9 阈值不可靠：最优只有 0.90、真机 0.82。）
   *  退化链：高度序列缺失/样本不足 → 作者机型在 ECS 映射里唯一命中 → 最长航迹。 */
  function pickOwnUnit(trackUnits, authorVeh, ownProps, axis, uidMap) {
    var alt = ownProps.alt_m;
    var best = null, bestRms = null, cands = [];
    if (alt && alt.times.length) {
      var byUid = {}, order = [];
      trackUnits.forEach(function (u) {
        if (!(u.uid in byUid)) { byUid[u.uid] = []; order.push(u.uid); }
        byUid[u.uid] = byUid[u.uid].concat(unitSamples(u));
      });
      order.forEach(function (uid) {
        var arr = byUid[uid];
        if (arr.length < 100) return;
        var num = 0, n = 0;
        for (var i = 0; i < arr.length; i++) {
          if (arr[i][3] === null) continue;
          var tt = arr[i][0] * SEC_PER_TICK;      // 回放时钟秒（不减 tick0）
          var j = bisectLeft(alt.times, tt);
          var k = -1, kd = 1e9;
          [j - 1, j].forEach(function (c) {
            if (c >= 0 && c < alt.times.length) {
              var d2 = Math.abs(alt.times[c] - tt);
              if (d2 < kd) { kd = d2; k = c; }
            }
          });
          if (k < 0 || kd >= 0.3) continue;
          var ref = alt.values[k];
          if (ref === null) continue;
          var dd = arr[i][3] - ref;
          num += dd * dd; n++;
        }
        if (n < 100) return;
        var rms = Math.sqrt(num / n);
        cands.push({ uid: uid, rms: rms, n: n });
        if (bestRms === null || rms < bestRms) { bestRms = rms; best = uid; }
      });
    }
    cands.sort(function (a, b) { return a.rms - b.rms; });
    var uid = null, how = '', authorCand = null;
    if (best !== null) {
      uid = best;
      how = '本机属性表（ReplayCockpitParams）高度序列认领 uid-' + uid + '（与航迹 z 的 rms ' + bestRms.toFixed(2) + ' m）';
    } else {
      if (authorVeh) {
        var present = {};
        trackUnits.forEach(function (u) { present[u.uid] = 1; });
        var hits = Object.keys(uidMap).filter(function (k) {
          return uidMap[k].vehicle === authorVeh && present[parseInt(k, 10)];
        });
        if (hits.length === 1) authorCand = parseInt(hits[0], 10);
      }
      if (authorCand !== null) {
        uid = authorCand;
        how = '结算表作者机型 ' + authorVeh + ' 在 ECS 映射里唯一命中 uid-' + authorCand;
      } else {
        var longest = null;
        trackUnits.forEach(function (u) { if (!longest || u.samples.length > longest.samples.length) longest = u; });
        if (longest) { uid = longest.uid; how = '退化：最长的一条航迹 uid-' + uid; }
        else how = '回放里没有可用实体';
      }
    }
    var segments = trackUnits.filter(function (u) { return u.uid === uid; })
      .sort(function (a, b) { return a.samples[0][0] - b.samples[0][0]; });
    return { uid: uid, how: how, segments: segments, rms: bestRms, candidates: cands.slice(0, 5),
             authorVehicleCandidates: authorCand };
  }

  function pearson(a, b) {
    if (a.length < 8 || a.length !== b.length) return null;
    var n = a.length, ma = 0, mb = 0, i;
    for (i = 0; i < n; i++) { ma += a[i]; mb += b[i]; }
    ma /= n; mb /= n;
    var na = 0, nb = 0, s = 0;
    for (i = 0; i < n; i++) {
      var da = a[i] - ma, db = b[i] - mb;
      s += da * db; na += da * da; nb += db * db;
    }
    if (na < 1e-9 || nb < 1e-9) return null;
    return s / Math.sqrt(na * nb);
  }

  /* ------------------------------------------------------------ 导出 */

  global.WRPLParse = {
    parse: parse,
    progress: progress,
    version: '1.0.0-browser',
    _internal: { parsePackets: parsePackets, parseFm: parseFm, readVlqSize: readVlqSize,
                 initStats: initStats, parseMain: parseMain, buildAttitude: buildAttitude,
                 parseGroundSync: parseGroundSync, bindGroundTracks: bindGroundTracks,
                 buildGroundUnits: buildGroundUnits, groundTrackArrays: groundTrackArrays,
                 groundIdentity: groundIdentity, EcsParser: EcsParser, parseSlotPlayers: parseSlotPlayers, parseWeaponSync: parseWeaponSync,
                 groundRle0ki: groundRle0ki,
                 emit: emit,   /* 只给开发期自验用：把进度状态定在某一帧，便于截图/取证 */
                 parseReplayHeader: parseReplayHeader, locatePacketStream: locatePacketStream,
                 parseFatBlk: parseFatBlk, DISPLAY_NAMES: DISPLAY_NAMES }
  };
})(typeof window !== 'undefined' ? window : globalThis);
