/* ECS 构造块组件走查（移植自 wrpl-inspector 的 wrpl/packet/parser/ecs2，Go，MIT/AGPL 见 vendor 说明）
 *
 * 上游格式（ecs2.go:198-265）：构造块 = [模板头][组件数][逐组件: 增量下标 + 按类型解析的值]。
 * 老代码用「vstart + 固定偏移」读飞机模板的 unit__playerId（+119），坦克模板组件集合不同就失效；
 * 这里按类型逐个走，与模板无关。
 *
 * 名字表：configure({components, dataComponents})，两份都是 hash -> 名字 / {comp, has_loader}。
 * 没配就抛错，不做猜测。
 */
(function (root) {
  'use strict';

  /* ------------------------------------------------------------ 1. 位读取 */

  function BitReader(d) { this.d = d; this.n = d.length * 8; this.pos = 0; }
  BitReader.prototype.bits = function (n) {
    if (n === 0) return 0;
    var p = this.pos;
    if (p + n > this.n) throw new Error('bit eof ' + p + '+' + n + '/' + this.n);
    var v = 0, d = this.d;
    for (var k = 0; k < n; k++) {
      var q = p + k;
      v = (v << 1) | ((d[q >> 3] >> (7 - (q & 7))) & 1);
      if (k >= 30) v = v >>> 0;
    }
    this.pos = p + n;
    return v;
  };
  BitReader.prototype.byte = function () { return this.bits(8); };
  BitReader.prototype.bytesN = function (n) {
    var out = new Uint8Array(n);
    for (var i = 0; i < n; i++) out[i] = this.bits(8);
    return out;
  };
  BitReader.prototype.u16 = function () { var b = this.bytesN(2); return b[0] | (b[1] << 8); };
  BitReader.prototype.u32 = function () {
    var b = this.bytesN(4);
    return (b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)) >>> 0;
  };
  BitReader.prototype.s32 = function () { return this.u32() | 0; };
  var DV = new DataView(new ArrayBuffer(4));
  BitReader.prototype.f32 = function () {
    DV.setUint8(0, this.byte()); DV.setUint8(1, this.byte());
    DV.setUint8(2, this.byte()); DV.setUint8(3, this.byte());
    return DV.getFloat32(0, true);
  };
  /** 变长无符号整数：7 位一组，高位续（BitReader.compressed 与上游 readECSCompressed 同构）。 */
  BitReader.prototype.compressed = function () {
    var v = 0, c = 0;
    for (;;) {
      var a = this.byte();
      v += (a & 0x7F) * Math.pow(2, 7 * c); c += 1;
      if (!(a & 0x80)) return v;
      if (c > 9) throw new Error('varint too long');
    }
  };
  /** 实体 id：2 字节起步的变长编码（packet.ReadEID / wrpl-parse.js 的 eid()）。 */
  BitReader.prototype.eid = function () {
    var f = this.u16();
    if (f & 1) return ((f >> 2) | (((f & 2) >> 1) << 22)) >>> 0;
    if (f & 2) return ((f >> 2) | (this.byte() << 22)) >>> 0;
    var s = this.u16();
    var ret = ((s << 16) | f) >>> 0;
    return ((((ret & 0x00FFFFFF) >>> 2) | ((ret >>> 24) << 22))) >>> 0;
  };
  BitReader.prototype.skip = function (n) {
    if (this.pos + n > this.n) throw new Error('skip eof ' + this.pos + '+' + n + '/' + this.n);
    this.pos += n;
  };
  BitReader.prototype.cstr = function () {
    var out = [];
    for (;;) {
      var c = this.byte();
      if (c === 0) break;
      out.push(c);
      if (out.length > 4096) throw new Error('string too long');
    }
    var s = '';
    for (var i = 0; i < out.length; i++) s += String.fromCharCode(out[i]);
    return s;
  };

  /* ------------------------------------------------------------ 2. 类型表 */

  /** 定长 POD 的位宽（opaque_component.go:fixedPODComponentBits 的同构表）。 */
  var POD_BITS = {
    'bool': 1, 'uint8_t': 8, 'uint16_t': 16,
    'float': 32, 'int': 32, 'uint32_t': 32, 'E3DCOLOR': 32,
    'Point2': 64, 'IPoint2': 64,
    'Point3': 96, 'IPoint3': 96,
    'Point4': 128, 'IPoint4': 128,
    'TMatrix': 384
  };

  var LIST_ELEM = {
    'ecs::UInt8List': 'uint8_t', 'ecs::UInt16List': 'uint16_t', 'ecs::UInt32List': 'uint32_t',
    'ecs::Int8List': 'int8_t', 'ecs::Int16List': 'int16_t', 'ecs::IntList': 'int32_t', 'ecs::Int64List': 'int64_t',
    'ecs::FloatList': 'float', 'ecs::Point2List': 'Point2', 'ecs::Point3List': 'Point3',
    'ecs::Point4List': 'Point4', 'ecs::IPoint2List': 'IPoint2', 'ecs::IPoint3List': 'IPoint3',
    'ecs::IPoint4List': 'IPoint4', 'ecs::TMatrixList': 'TMatrix', 'ecs::ColorList': 'E3DCOLOR'
  };

  var ELEM_BITS = { 'int8_t': 8, 'int16_t': 16, 'int32_t': 32, 'int64_t': 64 };

  var STORAGE_NAMES = {
    'FlightModelWrapStorageComponent': 1, 'HeavyVehicleModelStorageComponent': 1,
    'WarShipModelStorageComponent': 1, 'InfantryTroopStorageComponent': 1,
    'HumanStorageComponent': 1, 'WalkerVehicleStorageComponent': 1,
    'FortificationModelStorageComponent': 1, 'LightVehicleModelStorageComponent': 1,
    'BarrageBalloonStorageComponent': 1
  };

  var MISSILE_NAMES = { 'Payload': 1, 'Bomb': 1, 'Rocket': 1, 'Jettisoned': 1, 'Torpedo': 1 };

  /* ------------------------------------------------------------ 3. 状态 */

  var CFG = { components: null, data: null };
  var OPTS = null;                              // walk() 的调试开关（storageBits 覆盖等）   // Map<uint32,string>, Map<uint32,{name,comp,has_loader}>
  var interned = Object.create(null);           // ecs::Object 的键串内联表（跨块共享，同上游 ctx）

  function configure(cfg) {
    CFG.components = cfg.components instanceof Map ? cfg.components : new Map(Object.entries(cfg.components || {}).map(function (e) { return [Number(e[0]) >>> 0, e[1]]; }));
    var src = cfg.dataComponents || {};
    CFG.data = (src instanceof Map) ? src : new Map(Object.entries(src).map(function (e) {
      var v = e[1];
      return [Number(e[0]) >>> 0, (typeof v === 'string') ? { name: v, comp: 0, has_loader: false } : v];
    }));
    interned = Object.create(null);
  }

  function compName(h) { return CFG.components.get(h >>> 0) || null; }

  /* ------------------------------------------------------------ 4. 组件值 */

  function readPOD(r, name) {
    switch (name) {
      case 'bool': return r.bits(1) === 1;
      case 'uint8_t': return r.bits(8);
      case 'uint16_t': return r.u16();
      case 'uint32_t': return r.u32();
      case 'int': return r.s32();
      case 'float': return r.f32();
      case 'E3DCOLOR': return r.u32();
      case 'int8_t': return (r.bits(8) << 24) >> 24;
      case 'int16_t': return (r.u16() << 16) >> 16;
      case 'int32_t': return r.s32();
      case 'int64_t': return r.u32() + r.u32() * 4294967296;
      default: return null;
    }
  }

  /** 单个组件的值；name 是组件类型名（components 表的 value）。不认识就抛错，不猜长度。 */
  function readValue(r, typeName) {
    if (POD_BITS[typeName] !== undefined) {
      if (typeName === 'TMatrix') { r.skip(384); return null; }
      if (typeName === 'Point4' || typeName === 'IPoint4') { r.skip(128); return null; }
      if (typeName === 'Point3' || typeName === 'IPoint3') { r.skip(96); return null; }
      if (typeName === 'Point2' || typeName === 'IPoint2') { r.skip(64); return null; }
      return readPOD(r, typeName);
    }
    if (ELEM_BITS[typeName] !== undefined) return readPOD(r, typeName);
    if (LIST_ELEM[typeName] !== undefined) {
      var n = r.compressed(), out = new Array(n);
      var elem = LIST_ELEM[typeName];
      for (var i = 0; i < n; i++) out[i] = readValue(r, elem);
      return out;
    }
    switch (typeName) {
      case 'ecs::string': return r.cstr();
      case 'ecs::EntityId': return r.eid();
      case 'ecs::StringList': {
        var ns = r.compressed(), ls = new Array(ns);
        for (var j = 0; j < ns; j++) ls[j] = r.cstr();
        return ls;
      }
      case 'ecs::EidList': {
        var ne = r.compressed(), le = new Array(ne);
        for (var k = 0; k < ne; k++) le[k] = r.eid();
        return le;
      }
      case 'ecs::BoolList': {
        var nb = r.compressed(), lb = new Array(nb);
        for (var m = 0; m < nb; m++) lb[m] = r.bits(1) === 1;
        return lb;
      }
      case 'BufferedHudData': {
        var nh = r.compressed(), lh = new Array(nh);
        for (var p = 0; p < nh; p++) lh[p] = r.bits(8);
        return lh;
      }
      case 'dm::PartId': return r.bits(6);
      case 'dm::PartIdList': {
        /* 上游 ecsCheckPartIDListCount：rawCount*8 个元素，每个 6 位。 */
        var np = r.compressed() * 8, lp = new Array(np);
        for (var q = 0; q < np; q++) lp[q] = r.bits(6);
        return lp;
      }
      case 'ecs::Array': {
        var na = r.compressed(), la = new Array(na);
        for (var s = 0; s < na; s++) la[s] = readChild(r);
        return la;
      }
      case 'ecs::Object': {
        var no = r.compressed(), obj = {};
        for (var t = 0; t < no; t++) {
          var key = readIString(r);
          obj[key] = readChild(r);
        }
        return obj;
      }
      default:
        if (STORAGE_NAMES[typeName]) {
          /* 上游 StorageParser 也只读原始位块（内部结构未解），长度对就等价。 */
          var bs = r.compressed();
          if (OPTS && OPTS.storageBits != null) { r.skip(OPTS.storageBits); return { storageBits: OPTS.storageBits, raw: bs }; }
          r.skip(bs);
          return { storageBits: bs };
        }
        if (MISSILE_NAMES[typeName]) return readRocket(r);
        throw new Error('未知组件类型 ' + typeName);
    }
  }

  /** 上游 read_istring：首字节 1 → 直接跟字符串；0 → 10 位内联下标。 */
  function readIString(r) {
    if (r.bits(1) === 1) return r.cstr();
    var idx = r.bits(10);
    if (interned[idx] === undefined) interned[idx] = r.cstr();
    return interned[idx];
  }

  function readChild(r) {
    var typeId = r.u32();
    if (typeId === 0) return null;
    var nm = compName(typeId);
    if (!nm) throw new Error('子组件类型未登记 0x' + (typeId >>> 0).toString(16));
    return { type: nm, value: readValue(r, nm) };
  }

  /** RocketSerializer（Parsers.go）：本文件只需要长度算对，字段值不重要。 */
  function readRocket(r) {
    r.compressed();
    r.eid(); r.eid();
    r.byte();
    r.skip(792);
    for (var i = 0; i < 3; i++) {
      var n = r.u16();
      r.skip((n + 7) & 0xFFFFFFF8);
    }
    r.skip(112);
    return null;
  }

  /* ------------------------------------------------------------ 5. 数据组件（has_loader 的三个） */

  function readDataComp(r, def) {
    var dn = def && def.name;
    switch (dn) {
      case 'transform': {
        var ortho = r.bits(1) === 1;
        if (ortho) {
          r.skip(62);
          if (r.bits(1) === 1) r.skip(32);
        } else {
          r.skip(288);
        }
        r.skip(96);            // 平移（上游保留为 V4，这里不取值）
        return null;
      }
      case 'ri_extra__riSyncDesc': {
        var v1 = r.compressed(), v2 = r.compressed();
        if (v2 !== 0) r.compressed();
        return { v1: v1, v2: v2 };
      }
      case 'ri_extra__handle': {
        var word = r.u32();
        if (word & (1 << 23)) r.u32();
        return null;
      }
      default: return 'skip';
    }
  }

  /* ------------------------------------------------------------ 6. 走查 */

  function readLenStr(r) {
    var n = r.byte();
    var b = r.bytesN(n);
    var s = '';
    for (var i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
    return s;
  }

  /**
   * 走一个构造块。block 是 Uint8Array（从块首字节开始），compdefs 是
   * 「模板内组件下标 -> [dataHash, compHash]」的映射（由调用方在读模板时建好）。
   * 返回 { template, name, comps: [{index, name, type, value}] }。
   */
  function walk(block, compdefs, templates, vstart, countAt, opts) {
    OPTS = opts || null;
    var r = new BitReader(block);
    var tid = r.compressed();
    var t = templates && templates[tid];
    var tname, comps, i, cid;
    if (t) {
      tname = t.name; comps = t.comps;
    } else {
      /* 只有调用方没给模板缓存时才自己读完整块头。 */
      tname = readLenStr(r);
      var ncomp = r.u16();
      comps = [];
      for (i = 0; i < ncomp; i++) {
        cid = r.compressed();
        if (!compdefs[cid]) compdefs[cid] = [r.u32(), r.u32()];
        comps.push(cid);
      }
      if (templates) templates[tid] = { id: tid, name: tname, comps: comps };
    }
    var ncomp2 = comps.length;
    var listCount;
    if (countAt != null) {
      /* 调用方直接指定条数字节的位置（调试/异形块用）。 */
      r.pos = countAt * 8;
      listCount = (ncomp2 < 256) ? r.byte() : r.compressed();
    } else if (vstart) {
      /* vstart 是调用方已经读到的“组件条数之后”的字节位置；条数本身就在它前一字节。 */
      r.pos = (vstart - 1) * 8;
      listCount = (ncomp2 < 256) ? r.byte() : r.compressed();
    } else {
      listCount = (ncomp2 < 256) ? r.byte() : r.compressed();
    }
    /* 已登记模板的块：[tid][0x00][变长条数]。0x00 是名字长度占位，不是零组件。 */
    if (listCount === 0 && r.pos < r.n) listCount = r.compressed();
    var idx = 0, out = [];
    var trace = [];
    for (i = 0; i < listCount; i++) {
      try {
      var ofs = (ncomp2 < 256) ? r.byte() : r.compressed();
      idx = (i === 0) ? ofs : (idx + ofs + 1);
      var tr = { entry: i, index: idx, startBit: r.pos }; trace.push(tr);
      if (idx >= comps.length) throw new Error('组件下标越界 ' + idx + '/' + comps.length + ' @' + tname);
      var def = compdefs[comps[idx]];
      var dataHash = def[0] >>> 0, compHash = def[1] >>> 0;
      if (compHash === 0) continue;      // 上游：comp_type == 0 → 不占字节
      var ddef = CFG.data.get(dataHash);
      var cname = compName(compHash);
      var value;
      tr.name = cname || (ddef && ddef.name) || ('0x' + compHash.toString(16));
      tr.valueStartBit = r.pos;
      if (dataHash !== 0) {
        var got = readDataComp(r, ddef);
        if (got !== 'skip') { out.push({ index: idx, name: ddef ? ddef.name : '0x' + dataHash.toString(16), type: cname, value: got }); continue; }
        /* 无自定义加载器：退回组件类型解析（上游 InitializeParsers 的规则） */
        if (!cname) throw new Error('数据组件无名字表项 0x' + dataHash.toString(16));
        value = readValue(r, cname);
        tr.endBit = r.pos;
        out.push({ index: idx, name: ddef ? ddef.name : '0x' + dataHash.toString(16), type: cname, value: value });
        continue;
      }
      if (!cname) throw new Error('组件类型无名字表项 0x' + compHash.toString(16) + ' @' + tname);
      value = readValue(r, cname);
      tr.endBit = r.pos;
      out.push({ index: idx, name: cname, type: cname, value: value });
      trace.push({ entry: i, index: idx, name: cname, pos: r.pos });
      } catch (e) {
        e.trace = trace; e.listCount = listCount; e.entry = i; e.lastIdx = idx;
        throw e;
      }
    }
    return { template: tname, comps: out, consumedBits: r.pos, totalBits: r.n };
  }

  /** 从走查结果里取一个字段（按 data 组件名，取不到返回 undefined）。 */
  function pick(comps, name) {
    for (var i = 0; i < comps.length; i++) if (comps[i].name === name) return comps[i].value;
    return undefined;
  }

  var API = { configure: configure, walk: walk, pick: pick, BitReader: BitReader,
              POD_BITS: POD_BITS, STORAGE_NAMES: STORAGE_NAMES, MISSILE_NAMES: MISSILE_NAMES };
  root.ECSComps = API;
  if (typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof window !== 'undefined' ? window : globalThis);
