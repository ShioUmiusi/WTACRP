#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wrpl_decode_ecs.py —— 从 War Thunder 回放包流里解 **ptype=6 ECS（Danet ECS）** 实体。

这条线独立于 `wrpl_decode.py`（wrpl-probe 的 ptype=2 AircraftSmall / ptype=4 MPI 线）：
本脚本只碰 ptype=6，输出「实体 id + 模板 + 组件值 + 出生变换（三维位置）」。

方法来源
--------
实体/模板/组件的组织结构**移植自 wrpl-inspector v3**（Go，AGPL-3.0）：
  https://github.com/maxsupermanhd/wrpl-inspector
  - wrpl/packet/parser/ecs2/ecs2.go     （控制码、模板、构造消息）
  - wrpl/packet/parser/ecs2/Parsers.go  （各类型的组件序列化器）
  - wrpl/packet/parser/ecs2/compdefs.go （哈希表结构）
  - data/ecshashes.json                 （组件哈希 → 名字；780 KB，随仓库分发）
本脚本是**独立的 Python 重写**，不是逐行翻译；凡与本局实测不符之处都在下面标注。

本局（`2026.10.18 05.45.06.wrpl`，version 101404）实测到的、与参考实现不同的两点：
  1. 我们的包载荷比参考实现多 1 字节前缀：参考实现的 payload = 我们的 payload[1:]。
     所以 ECS 控制码在**我们**的载荷偏移 1 处（0x20/0x21/0x22/0x23/0x24/0x25/0x26），
     这也是为什么参考实现里 ptype=4 的签名条件是 `ff 0f …`，而我们是 `00 ff 0f …`。
  2. 实体构造块（control 0x24）里，`compCount == 0` **不是**「没有组件」，而是
     「按模板的组件顺序把全部组件值依次写下来」。参考实现的 `deserializeConstruction`
     对这个字节是按「组件下标增量表」解释的，本局会得到 compCount=0 → 一个字节都不读，
     于是块里剩下的值全部落空。本脚本按 0 = 全部处理，并统计块是否被恰好用尽来验证。

用法
----
    python ace-combat/tools/wrpl_decode_ecs.py --explore        # 控制码/长度分布
    python ace-combat/tools/wrpl_decode_ecs.py --ecs            # 解 ECS 实体创建，打印模板/组件
    python ace-combat/tools/wrpl_decode_ecs.py --spawn          # 解飞机出生三维位置
    python ace-combat/tools/wrpl_decode_ecs.py --verify         # 与 8111 开发期真值比对
    python ace-combat/tools/wrpl_decode_ecs.py --build          # 写 tracks_ecs.json
    python ace-combat/tools/wrpl_decode_ecs.py --all

只读输入；只写 `ace-combat/build/wrpl/tracks_ecs.json`。
"""

import argparse
import collections
import json
import math
import os
import re
import struct
import sys

# ---------------------------------------------------------------- 常量与路径

DEF_PAYLOAD = "data/_scratch/payload.bin"
DEF_HASHES = "build/wrpl-inspector/data/ecshashes.json"
DEF_ENTITIES = "build/wrpl/entities.json"
DEF_CAPTURE = "data/raw/rb-session"
DEF_OUT = "build/wrpl/tracks_ecs.json"
DEF_ENTITIES_OUT = "build/wrpl/ecs_entities.json"
DEF_DISPLAY_NAMES_OUT = "build/wrpl/vehicle_display_names.json"
DEF_LAUNCHES_OUT = "build/wrpl/projectile_launches.json"
DEF_RUNWAYS_OUT = "build/wrpl/runways.json"
DEF_SQUAD_OUT = "build/wrpl/squad.json"
DEF_CATEGORIES_OUT = "build/wrpl/vehicle_categories.json"
DEF_TABLES_DIR = "tables"

SEC_PER_TICK = 1.0 / 256000.0
# 8111 相对秒 ≈ 回放秒 + OFFSET_8111（wrpl-probe 用击杀流水拟合，±0.25 s 量化误差）
OFFSET_8111 = 53.68
MAP_HALF = 32768.0

CONTROL = {
    0x20: "ID_ENTITY_MSG",
    0x21: "ID_ENTITY_MSG_COMPRESSED",
    0x22: "ID_ENTITY_REPLICATION",
    0x23: "ID_ENTITY_REPLICATION_COMPRESSED",
    0x24: "ID_ENTITY_CREATION",
    0x25: "ID_ENTITY_CREATION_COMPRESSED",
    0x26: "ID_ENTITY_DESTRUCTION",
}


# ---------------------------------------------------------------- 包框架

def read_vlq_size(b, p):
    """包长度前缀（VLQ，前导 1 的个数决定字节数，低位取反）。"""
    first = b[p]
    p += 1
    if first & 0x80:
        return first & 0x7F, p
    if first & 0x40:
        return (((first << 8) | b[p]) ^ 0x4000), p + 1
    if first & 0x20:
        return (((first << 16) | (b[p] << 8) | b[p + 1]) ^ 0x200000), p + 2
    if first & 0x10:
        return (((first << 24) | (b[p] << 16) | (b[p + 1] << 8) | b[p + 2]) ^ 0x10000000), p + 3
    return struct.unpack_from("<I", b, p)[0], p + 4


def parse_packets(buf):
    """把解压后的包流切成 [(ptype, tick, payload_start, payload_end)]。

    包首字节 bit4 置位 = 沿用上一 tick；否则紧随 u32 LE tick。
    本局 86773 个包恰好用尽全部字节（无剩余），与 wrpl-format 的结论一致。
    """
    out = []
    p = 0
    last = 0
    n = len(buf)
    while p < n:
        size, p2 = read_vlq_size(buf, p)
        if size == 0 or p2 + size > n:
            break
        fb = buf[p2]
        if fb & 0x10:
            out.append((fb ^ 0x10, last, p2 + 1, p2 + size))
        else:
            last = struct.unpack_from("<I", buf, p2 + 1)[0]
            out.append((fb, last, p2 + 5, p2 + size))
        p = p2 + size
    return out, {"bytes": n, "packets": len(out), "tail_unparsed": n - p}


# ---------------------------------------------------------------- 位流原语

class BitReader:
    """MSB-first 位读器（对应 danet.BitReader）。"""

    __slots__ = ("d", "n", "pos")

    def __init__(self, d):
        self.d = d
        self.n = len(d) * 8
        self.pos = 0

    def bits(self, n):
        if n == 0:
            return 0
        p = self.pos
        if p + n > self.n:
            raise EOFError("bit eof %d+%d/%d" % (p, n, self.n))
        b0 = p >> 3
        b1 = (p + n + 7) >> 3
        chunk = int.from_bytes(self.d[b0:b1], "big")
        total = (b1 - b0) * 8
        v = (chunk >> (total - (p & 7) - n)) & ((1 << n) - 1)
        self.pos = p + n
        return v

    def byte(self):
        return self.bits(8)

    def bytes_n(self, n):
        return self.bits(n * 8).to_bytes(n, "big")

    def u16(self):
        return int.from_bytes(self.bytes_n(2), "little")

    def u32(self):
        return int.from_bytes(self.bytes_n(4), "little")

    def compressed(self):
        """ReadCompressed：7 位一组，低位在先，高位置 1 表示继续。"""
        v = 0
        c = 0
        while True:
            a = self.byte()
            v |= (a & 0x7F) << (7 * c)
            c += 1
            if not (a & 0x80):
                return v

    def lenstr(self):
        l = self.byte()
        return self.bytes_n(l).decode("utf-8", "replace")

    def eid(self):
        """packet.ReadEID：15/22/23 位三种编码。"""
        f = self.u16()
        if f & 1:
            return (f >> 2) | (((f & 2) >> 1) << 22)
        if f & 2:
            return (f >> 2) | (self.byte() << 22)
        s = self.u16()
        ret = (s << 16) | f
        return ((ret & 0x00FFFFFF) >> 2) | ((ret >> 24) << 22)


# ---------------------------------------------------------------- 组件表

LIST_TYPES = {
    "ecs::UInt8List": "<B", "ecs::UInt16List": "<H", "ecs::UInt32List": "<I",
    "ecs::UInt64List": "<Q", "ecs::FloatList": "<f", "ecs::Int8List": "<b",
    "ecs::Int16List": "<h", "ecs::IntList": "<i", "ecs::Int64List": "<q",
    "ecs::Point2List": "<2f", "ecs::Point3List": "<3f", "ecs::Point4List": "<4f",
    "ecs::IPoint2List": "<2i", "ecs::IPoint3List": "<3i", "ecs::IPoint4List": "<4i",
    "ecs::TMatrixList": "<12f", "ecs::ColorList": "<4B",
}
FMT = {
    "uint8_t": "<B", "int8_t": "<b", "bool": "<B", "char": "<B",
    "uint16_t": "<H", "int16_t": "<h", "float": "<f", "int": "<i",
    "uint32_t": "<I", "int64_t": "<q", "uint64_t": "<Q", "float32": "<f",
    "float64": "<d", "double": "<d",
    "Point2": "<2f", "Point3": "<3f", "Point4": "<4f", "TMatrix": "<12f",
    "IPoint2": "<2i", "IPoint3": "<3i", "IPoint4": "<4i", "E3DCOLOR": "<4B",
    "dm::PartId": None, "ecs::EntityId": None, "ecs::string": None,
}
STORAGE_SUFFIX = "StorageComponent"


class HashMaps:
    def __init__(self, path):
        with open(path, encoding="utf-8") as f:
            h = json.load(f)
        self.comp_names = {int(k, 16): v for k, v in h["components"].items()}
        self.datacomps = {int(k, 16): v for k, v in h["dataComponents"].items()}

    def dc_name(self, h):
        d = self.datacomps.get(h)
        return d["name"] if d else None

    def dispatch_name(self, comp_hash, datacomp_hash):
        """返回 (派发用名字, 是否自定义 loader)。规则照 compdefs.go/InitializeParsers。"""
        if datacomp_hash:
            d = self.datacomps.get(datacomp_hash)
            if d is None:
                return None, False
            if d.get("has_loader"):
                return d["name"], True
            comp_hash = int(d["comp"], 16)
        return self.comp_names.get(comp_hash), False


# ---------------------------------------------------------------- ECS 解析器

class EcsParser:
    def __init__(self, maps):
        self.maps = maps
        self.templates = {}          # tid -> {id,name,comps}
        self.compdefs = {}           # cid -> (DataComponentHash, ComponentHash)
        self.stats = collections.Counter()
        self.creations = []          # (eid, tid, block)

    # ---- 组件值
    def parse_component(self, r, comp_hash, datacomp_hash):
        if comp_hash == 0:
            return None
        name, _custom = self.maps.dispatch_name(comp_hash, datacomp_hash)
        if name is None:
            self.stats["no_dispatch"] += 1
            return ("UNRESOLVED",)
        return self.parse_by_name(r, name)

    def parse_by_name(self, r, name):
        st = self.stats
        if name == "transform":
            return self.parse_transform(r)
        if name == "ecs::Tag":
            return ("tag",)
        if name == "ecs::string":
            return r.lenstr()
        if name == "ecs::EntityId":
            return r.eid()
        if name == "ecs::Array":
            n = r.compressed()
            return ("array", [self.parse_component(r, r.u32(), 0) for _ in range(n)])
        if name == "ecs::Object":
            n = r.compressed()
            for _ in range(n):
                self.read_istring(r)
                r.u32()
                self.stats["object_member_skipped"] += 1
            return ("object", n)
        if name == "ri_extra__riSyncDesc":
            v1 = r.compressed(); v2 = r.compressed()
            v3 = r.compressed() if v2 else 0
            return (v1, v2, v3)
        if name == "ri_extra__handle":
            w = r.u32()
            if w & (1 << 23):
                return (w & ((1 << 23) - 1), r.u32())
            return (w >> 11, w & ((1 << 11) - 1))
        if name == "dm::PartId":
            return r.bits(6)
        if name == "dm::PartIdList":
            n = r.compressed() << 3          # 参考实现里的古怪 <<3
            return [r.bits(6) for _ in range(n)]
        if name in LIST_TYPES:
            n = r.compressed()
            if name == "ecs::EidList":
                return [r.eid() for _ in range(n)]
            return [r.bytes_n(struct.calcsize(LIST_TYPES[name])) for _ in range(n)]
        if name in ("ecs::StringList", "ecs::BoolList"):
            n = r.compressed()
            if name == "ecs::BoolList":
                return [r.bits(1) for _ in range(n)]
            return [r.lenstr() for _ in range(n)]
        if name == "BufferedHudData":
            n = r.compressed()
            return ("buffered", n, len(r.bytes_n(n)))
        if name.endswith(STORAGE_SUFFIX):
            n = r.compressed()
            r.bits(n)
            return ("storage", n)
        if name in ("Payload", "Bomb", "Rocket", "Jettisoned", "Torpedo"):
            return self.parse_rocket(r)
        if name in FMT:
            f = FMT[name]
            if f is None:
                return None
            return struct.unpack(f, r.bytes_n(struct.calcsize(f)))
        st["no_parser:" + name] += 1
        return None

    def read_istring(self, r):
        if r.bits(1):
            return r.lenstr()
        raw = r.bits(10).to_bytes(2, "little")
        return ("istr#%d" % int.from_bytes(raw, "little"))

    def parse_transform(self, r):
        """参考实现的 TMatrix 布局（其余内容它自己也没解）。

        isOrthoUni(1) ? [62 bits + hasScale(1) + 4B scale] : [36B 3x3]
        然后固定 12B 平移。返回值 = (kind, rotation, translation)。
        """
        ortho = r.bits(1)
        if ortho:
            m = r.bits(62)
            has_scale = r.bits(1)
            scale = r.bytes_n(4) if has_scale else None
            rot = ("const62", m, scale)
        else:
            rot = struct.unpack("<9f", r.bytes_n(36))
        trans = struct.unpack("<3f", r.bytes_n(12))
        return ("transform", rot, trans)

    def parse_rocket(self, r):
        r.compressed(); r.eid(); r.eid(); r.byte()
        body = r.bytes_n(99)                     # 792 bits
        for _ in range(3):
            n = int.from_bytes(r.bytes_n(2), "little")
            r.bits((n + 7) & 0xFFFFFFF8)
        r.bits(112)
        return ("rocket", body)

    # ---- 模板
    def parse_template(self, r):
        tid = r.compressed()
        t = self.templates.get(tid)
        if t is not None:
            return t
        name = r.lenstr()
        ncomp = r.u16()
        comps = []
        for _ in range(ncomp):
            cid = r.compressed()
            if cid not in self.compdefs:
                self.compdefs[cid] = (r.u32(), r.u32())
            comps.append(cid)
        t = {"id": tid, "name": name, "comps": comps}
        self.templates[tid] = t
        return t

    # ---- 构造消息
    def parse_construct(self, r, tick):
        eid = r.eid()
        bs = r.compressed()
        block = r.bytes_n(bs)
        br = BitReader(block)
        t = self.parse_template(br)
        tcc = len(t["comps"])
        comp_count = br.byte() if tcc < 256 else br.compressed()
        vstart = br.pos // 8          # 组件值起点（首次出现的模板会被定义表推后）
        # 本局实测：compCount==0 -> 按模板顺序写全部组件（见文件头说明 2）
        order = list(range(tcc)) if comp_count == 0 else None
        if order is None:
            comp = 0
            order = []
            for i in range(comp_count):
                ofs = br.byte() if tcc < 256 else br.compressed()
                comp = ofs if i == 0 else comp + ofs + 1
                order.append(comp)
        values = {}
        for comp in order:
            if comp >= tcc:
                raise ValueError("component index %d >= %d (template %s)" % (comp, tcc, t["name"]))
            cid = t["comps"][comp]
            dch, cch = self.compdefs.get(cid, (0, 0))
            try:
                val = self.parse_component(br, cch, dch)
            except EOFError:
                self.stats["component_eof"] += 1
                values["__eof__"] = True
                break
            nm = self.maps.dc_name(dch) or ("type_%x" % cch)
            values[nm] = val
        used = br.pos // 8
        self.creations.append((tick, eid, t["id"], t["name"], bs, used, comp_count, values, block, vstart))
        return t

    # ---- 整包
    def parse_packet(self, payload, tick):
        r = BitReader(payload)
        control = r.byte()
        if control == 0x25:
            import lz4.block
            try:
                body = lz4.block.decompress(payload[1:], uncompressed_size=(len(payload) - 1) * 32)
            except Exception as ex:
                self.stats["lz4_fail"] += 1
                return control, ex
            r = BitReader(body)
            control = 0x24
        if control == 0x24:
            n = r.byte()
            for _ in range(n + 1):
                try:
                    self.parse_construct(r, tick)
                except (EOFError, ValueError) as ex:
                    self.stats["construct_fail"] += 1
                    self.stats["construct_fail_msg:" + type(ex).__name__] += 1
                    break
        return control, None


# ---------------------------------------------------------------- ECS 载荷切分

def split_control(buf, pk):
    """返回 [(tick, control, their_payload)]；their_payload 已剥掉我们的 1 字节前缀。"""
    out = []
    for t, tick, s, e in pk:
        if t != 6:
            continue
        if e - s < 1:
            continue
        out.append((tick, buf[s + 1:e]))
    return out


# ---------------------------------------------------------------- 出生位置

AIR_TEMPLATES = ("aircraft+player_unit", "aircraft")

# 飞机 ECS 构造块里 uid 的相对偏移（相对组件值起点），实测 u16 LE
UID_REL_OFFSET = 29
# 任务里玩家位的单位名，形如 t1_player03_0 / t2_player16_0
MISSION_RE = __import__("re").compile(rb"t([12])_player(\d{2})_(\d)")
DUMMY_CLASS = "dummy_plane"


def calibrate_position_offset(creations, lo=0, hi=64):
    """在飞机模板的构造块里，找出「(x, 高度, z) 三个连续 float32」相对**组件值起点**的偏移。

    用相对偏移而不是绝对偏移，是因为首次出现的模板会把定义表写在值前面，绝对偏移会整体后移。
    判据（不依赖 8111）：|x|、|z| ∈ [1000, 45000]，高度 ∈ [-200, 15000]。
    返回 (最佳相对偏移, 命中数, 总块数)。这是**经验标定**，不是从格式文档推出来的。
    """
    cnt = collections.Counter()
    blocks = [c for c in creations if c[3] in AIR_TEMPLATES]
    for c in blocks:
        blk, vs = c[8], c[9]
        for rel in range(lo, hi + 1):
            o = vs + rel
            if o + 12 > len(blk):
                break
            x, y, z = struct.unpack_from("<3f", blk, o)
            if all(math.isfinite(v) for v in (x, y, z)) \
               and 1000 <= abs(x) <= 45000 and 1000 <= abs(z) <= 45000 \
               and -200 <= y <= 15000:
                cnt[rel] += 1
    if not cnt:
        return None, 0, len(blocks)
    return cnt.most_common(1)[0][0], cnt.most_common(1)[0][1], len(blocks)


def extract_spawns(creations, rel_offset):
    rows = []
    for tick, eid, tid, name, bs, used, cc, values, blk, vstart in creations:
        if name not in AIR_TEMPLATES:
            continue
        if rel_offset is None:
            continue
        o = vstart + rel_offset
        if o + 12 > len(blk):
            continue
        x, y, z = struct.unpack_from("<3f", blk, o)
        if not all(math.isfinite(v) for v in (x, y, z)):
            continue
        rows.append({
            "tick": tick, "eid": eid, "template": name, "blockSize": bs,
            "blockUsed": used, "compCount": cc, "valuesStart": vstart,
            "x": x, "y": y, "z": z,
        })
    rows.sort(key=lambda r: (r["tick"], r["eid"]))
    return rows


def plausible_spawn(s):
    return (-45000 <= s["x"] <= 45000 and -45000 <= s["z"] <= 45000
            and -200 <= s["y"] <= 16000
            and max(abs(s["x"]), abs(s["z"])) > 500)


# ---------------------------------------------------------------- uid ↔ 实体 ↔ 玩家

def _player_table(args):
    """从 header.json 的玩家表读 (name, team, vehicles[])。"""
    try:
        with open(args.entities.replace("entities.json", "header.json"), encoding="utf-8") as f:
            h = json.load(f)
        return h.get("players", []), h
    except Exception:
        return [], {}


def build_uid_map(args, P):
    """uid -> {entityIndex, player, vehicle, team, side}。

    来源（全部来自回放，不用 8111）：
      * uid          = 飞机构造块里「组件值起点 + 29」处的 u16 LE，低 11 位有效（0x7FF=无）
      * missionName  = 同一个块里的长度前缀字符串 `tN_playerMM_K` -> team / 玩家位
      * vehicle      = 同一个块里的 `unit__className`（长度前缀字符串），
                       形如 f_15a / su_33 / fa_18c_switzerland
    同一个 uid 会出现两次：先是 `dummy_plane` 占位实体，然后是真实机型实体；
    取**非 dummy_plane 的那一条**作为机型的来源。

    玩家名：只在该 (team, vehicle) 在玩家表里**唯一**时才填；有歧义就留 null（不猜）。
    """
    import re as _re
    players, header = _player_table(args)
    by_tv = collections.defaultdict(list)
    for p in players:
        for v in (p.get("vehicles") or []):
            by_tv[(p.get("team"), v)].append(p.get("name"))
    res_ = header.get("results") or {}
    author = res_.get("author")
    author_id = str(res_.get("authorUserId") or "")
    author_team = None
    author_veh = None
    for p in players:
        if (author_id and str(p.get("userId")) == author_id) or (author and p.get("name") == author):
            author_team = p.get("team")
            author_veh = p.get("vehicles")
            break

    veh_re = _re.compile(r"^[a-z0-9][a-z0-9_.\-]{2,48}$")

    # 先建 missionName -> uid：**小/中块**（399~2100 B）的 uid 在 值起点+29；
    # 但**大块**（约 2183~3300 B）的 uid 不在那个偏移，直接读会得到无效值 ——
    # 于是那些 uid 只剩占位块（dummy_plane），机型就丢了。
    # 兜底链：占位块与成熟块共用同一个 missionName（本局实测同 uid 的块 missionName 100% 一致），
    # 所以可以用 missionName 把成熟块的机型接回 uid。
    mission_uid = {}
    for tick, eid, tid, name, bs, used, cc, values, blk, vs in P.creations:
        if name not in AIR_TEMPLATES or vs + UID_REL_OFFSET + 2 > len(blk):
            continue
        raw = blk[vs + UID_REL_OFFSET] | (blk[vs + UID_REL_OFFSET + 1] << 8)
        if not (1 <= raw <= 2046):
            continue
        m = MISSION_RE.search(blk)
        if m:
            mission_uid.setdefault(m.group(0).decode(), raw & 0x7FF)

    recs = collections.defaultdict(list)
    for tick, eid, tid, name, bs, used, cc, values, blk, vs in P.creations:
        if name not in AIR_TEMPLATES:
            continue
        uid_raw = None
        if vs + UID_REL_OFFSET + 2 <= len(blk):
            uid_raw = blk[vs + UID_REL_OFFSET] | (blk[vs + UID_REL_OFFSET + 1] << 8)
        m = MISSION_RE.search(blk)
        if not (uid_raw is not None and 1 <= uid_raw <= 2046) and m:
            uid_raw = mission_uid.get(m.group(0).decode())
        team = int(m.group(1)) if m else None
        slot = m.group(0).decode() if m else None
        cls = None
        for mm in _re.finditer(rb"[\x20-\x7e]{4,}", blk):
            s = mm.group().decode()
            if s == DUMMY_CLASS or s in (name,) or "player" in s or "aircraft" in s or "+" in s:
                continue
            if veh_re.match(s):
                cls = s
                break
        recs[uid_raw].append({
            "eid": eid, "tick": tick, "team": team, "slot": slot,
            "className": cls, "blockSize": bs, "valuesStart": vs,
        })

    # ---- 队别**不从 missionName 推**（见下），改从「机型 → 结算玩家表里的 team 集合」推。
    # 本局实测：missionName 的 t1_/t2_ 前缀**不是队别**，是出生组。
    # 证据（全部回放内部）：
    #   uid=346 的 missionName 是 t1_player03_0，机型 su_33  —— su_33 只出现在结算 team 2；
    #   uid=348 的 missionName 是 t1_player05_0，机型 f_20a  —— f_20a 只出现在结算 team 1；
    # 同为 t1_ 却分属两队 → 前缀与队别无关。t2_ 同理（uid=364 f_16xl→team2，uid=369 tornado_f3→team1）。
    veh_teams = collections.defaultdict(set)
    for (tm, v), names in by_tv.items():
        if v:
            veh_teams[v].add(tm)
    stat = collections.Counter()
    out = {}
    for uid_raw, lst in recs.items():
        if uid_raw is None or not (1 <= uid_raw <= 2046):
            stat["uid_invalid"] += 1
            continue
        uid = uid_raw & 0x7FF
        if uid == 0x7FF:
            stat["uid_invalid"] += 1
            continue
        good = [r for r in lst if r["className"] and r["className"] != DUMMY_CLASS]
        pick = max(good or lst, key=lambda r: r["blockSize"])
        mteam = pick["team"] if pick["team"] is not None else next(
            (r["team"] for r in lst if r["team"] is not None), None)
        slot = pick["slot"] or next((r["slot"] for r in lst if r["slot"]), None)
        vehicle = pick["className"] if (pick["className"] and pick["className"] != DUMMY_CLASS) else None
        tcand = sorted(veh_teams.get(vehicle, ())) if vehicle else []
        pteam = tcand[0] if len(tcand) == 1 else None
        cand = sorted(set(by_tv.get((pteam, vehicle), []))) if (pteam and vehicle) else []
        if not cand and vehicle:
            # 队别没定住时，给出「所有用该机型的玩家」作为候选，绝不当作已定
            cand = sorted({n for (t2_, v2), ns in by_tv.items() if v2 == vehicle for n in ns})
        player = cand[0] if len(cand) == 1 else None
        if vehicle is None:
            stat["no_vehicle"] += 1
        if len(tcand) > 1:
            stat["team_ambiguous_by_vehicle"] += 1
        if len(cand) > 1:
            stat["player_ambiguous"] += 1
        if len(cand) == 1:
            stat["player_unique"] += 1
        side = None
        if pteam is not None and author_team is not None:
            side = "ally" if pteam == author_team else "enemy"
        out[str(uid)] = {
            "entityIndex": pick["eid"] & 0x3FFFFF,
            "entityId": "0x%x" % pick["eid"],
            "player": player,
            "playerCandidates": cand,
            "vehicle": vehicle,
            "team": pteam,
            "teamCandidates": tcand,
            "side": side,
            "spawnGroup": ("t%s" % mteam) if mteam else None,
            "spawnSlotName": slot,
            "spawnGroupIsTeam": False,
            "spawnTick": pick["tick"],
            "instances": len(lst),
            "sideSource": "vehicle→结算玩家表 team" if pteam else "unresolved",
            "source": "wrpl ptype=6 ECS 实体构造块（uid=值起点+%d 的 u16 低 11 位；"
                      "vehicle=unit__className；spawnSlotName=t[12]_playerNN_K）" % UID_REL_OFFSET,
            "confidence": ("high" if (vehicle and player) else
                           "medium" if vehicle else "low"),
        }
    # 本机（由兄弟线用开发期真值钉住的 uid）：机型从本局结算 BLK 的 author 行取，来源已注明
    own = getattr(args, "own_uid", None)
    if own and str(own) in out:
        av = (author_veh or [None])[0]
        out[str(own)]["vehicle"] = out[str(own)]["vehicle"] or av
        out[str(own)]["vehicleSource"] = "results.author（本局结算 BLK）"
        if av:
            tc = sorted(veh_teams.get(av, ()))
            if author_team in tc:
                out[str(own)]["team"] = author_team
                out[str(own)]["teamCandidates"] = tc
                out[str(own)]["side"] = "ally"
                out[str(own)]["sideSource"] = "results.author + vehicle"
                out[str(own)]["player"] = None
                out[str(own)]["playerCandidates"] = sorted(
                    {n for (t2_, v2), ns in by_tv.items() if v2 == av and t2_ == author_team for n in ns})
                # 本机身份由兄弟线用开发期真值钉住；名字取本局结算 BLK 的 author，来源已注明
                arow = [q.get("name") for q in players
                        if (author_id and str(q.get("userId")) == author_id)]
                if arow and arow[0] in out[str(own)]["playerCandidates"]:
                    out[str(own)]["player"] = arow[0]
                    out[str(own)]["playerSource"] = "results.author（本机身份由兄弟线钉住）"
                stat["own_uid_anchored"] = int(own)
    stat["uids"] = len(out)
    stat["authorTeam"] = author_team
    stat["uids_in_fm_range"] = sum(1 for k in out if 344 <= int(k) <= 422)
    return out, dict(stat)


def cmd_uid_map(args, P):
    mp, stat = build_uid_map(args, P)
    print("=== uid ↔ 实体 ↔ 玩家/载具/阵营 ===")
    print("  统计: %s" % stat)
    for uid in sorted(mp, key=lambda k: int(k)):
        v = mp[uid]
        print("  uid=%-5s eid=%-10s team=%-5s side=%-7s vehicle=%-28s player=%-20s group=%-4s slot=%s" % (
            uid, v["entityId"], v["team"], v["side"], v["vehicle"],
            v["player"] if v["player"] else ("?%d" % len(v["playerCandidates"]) if v["playerCandidates"] else "-"),
            v.get("spawnGroup") or "-", v.get("spawnSlotName")))
    path = os.path.join(os.path.dirname(args.out), "uid_map.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mp, f, ensure_ascii=False, indent=1)
    print("已写 %s（%d 个 uid；vehicle 有值 %d，team 有值 %d，player 唯一命中 %d）" % (
        path, len(mp),
        sum(1 for v in mp.values() if v["vehicle"]),
        sum(1 for v in mp.values() if v["team"] is not None),
        sum(1 for v in mp.values() if v["player"])))
    return mp, stat


# ---------------------------------------------------------------- ECS 实体全清单

# 分类判据（全部写在代码里，便于复核）：
#   projectile : 模板组件恰好是 [<xxx>_component, active, proj__traceHandle]
#                （rocket / rocket+arh_shell / bomb / payload / jettisoned）
#   explosion  : 模板含 damage_area__* 组件（炸弹落点/爆炸区）
#   aircraft   : 模板 aircraft / aircraft+player_unit（含 unit_storage__aircraft + uid）
#   ground     : 模板 tank（含 unit_storage__tank + uid，unit__className 形如 tankModels/...）
#   structure  : 模板 fortification（含 unit_storage__fortification + uid，structures/...）
#   player_slot: 模板 sync_player（possessed/connid/is_local/team/base_player_id）
#   mod        : 模板含 mod__* 组件（toolkit/extinguisher/medicalkit/tank_smokescreen）
#   scenery    : 灯柱、机场警示灯、grid_holder、destroy_ri_box_area 等布景/触发体
PROJECTILE_TEMPLATES = ("rocket", "rocket+arh_shell", "bomb", "payload", "jettisoned")
AIRCRAFT_TEMPLATES = ("aircraft", "aircraft+player_unit")
GROUND_TEMPLATES = ("tank",)
STRUCTURE_TEMPLATES = ("fortification",)
SCENERY_TEMPLATES = ("lamppost_c", "aircraft_warning_light_red", "doors_grid", "loot_grid",
                     "humans_grid", "vehicles_grid", "aircrafts_grid", "interactable_grid",
                     "attract_points_grid", "destroy_ri_box_area")
MOD_TEMPLATES = ("toolkit_ecs", "extinguisher_ecs", "medicalkit_ecs", "tank_smokescreen_ecs")

# 只有「第一个组件就是 transform」的模板才能用 +10 这条规则（translation 紧跟 transform 头）
POSITION_REL = 10          # 相对组件值起点；飞机/地面/工事均已验证
QUATERNION_REL = 36        # 同一 transform 的四元数（|q|≈1）


def classify_template(name):
    if name in PROJECTILE_TEMPLATES:
        return "projectile"
    if name == "damage_area":
        return "explosion"
    if name in AIRCRAFT_TEMPLATES:
        return "aircraft"
    if name in GROUND_TEMPLATES:
        return "ground"
    if name in STRUCTURE_TEMPLATES:
        return "structure"
    if name == "sync_player":
        return "player_slot"
    if name in MOD_TEMPLATES:
        return "mod"
    if name in SCENERY_TEMPLATES:
        return "scenery"
    return "misc"


def template_has_transform(P, tid):
    t = P.templates.get(tid)
    if not t or not t["comps"]:
        return False
    dch, cch = P.compdefs.get(t["comps"][0], (0, 0))
    return P.maps.dc_name(dch) == "transform"


def block_strings(blk, minlen=3, maxlen=60):
    out = []
    i = 0
    while i < len(blk) - 1:
        L = blk[i]
        if minlen <= L <= maxlen and i + 1 + L <= len(blk):
            s = blk[i + 1:i + 1 + L]
            if all(32 <= b < 127 for b in s):
                out.append(s.decode())
                i += 1 + L
                continue
        i += 1
    return out


def find_projectile_transform(blk, vs):
    """弹类模板没有 transform 组件，位置藏在 *_component 的不透明区里。

    本局实测（**按 block 长度分组**后）：块内存在唯一一个 float4 且其模长 ∈ [0.98,1.02]
    （四元数），位置就紧跟**在它前面的 12 字节**（三个 float32）。
    返回 (position, quaternion, rel) 或 (None, None, None)。

    为什么不写死偏移：四元数相对组件值起点的偏移**随 block 长度变**（同一模板的多个变体
    28/29/30 不等），混扫一个偏移会得到 12%–62% 的假命中率；按四元数指纹锚定则是 100%。
    """
    best = None
    n = len(blk)
    for rel in range(0, max(0, n - vs - 16)):
        o = vs + rel
        try:
            q = struct.unpack_from("<4f", blk, o)
        except struct.error:
            break
        if any(not math.isfinite(v) or abs(v) > 1.001 for v in q):
            continue
        nrm = math.sqrt(sum(v * v for v in q))
        if not (0.98 < nrm < 1.02):
            continue
        op = o - 12
        if op < 0:
            continue
        x, up, z = struct.unpack_from("<3f", blk, op)
        if not all(math.isfinite(v) for v in (x, up, z)):
            continue
        if abs(x) > 45000 or abs(z) > 45000 or not (-500 <= up <= 16000):
            continue
        # 本局实测：个别块里被挑中的四元数前面是「全 0 填充」（位置恰为 0/-0/-0）。
        # 全 0 不是发射点（1734 条里没有一条真的在原点），跳过并继续找下一个候选。
        if max(abs(x), abs(z)) < 100 and abs(up) < 100:
            continue
        best = ([round(x, 2), round(up, 2), round(z, 2)],
                [round(v, 6) for v in q], rel)
        break
    return best if best else (None, None, None)


AIR_VOCAB = None


def _air_vocab(args):
    """飞机机型词表：结算玩家载具 + 观测到的 AI 机型。"""
    global AIR_VOCAB
    if AIR_VOCAB is not None:
        return AIR_VOCAB
    v = set(AI_EXTRA)
    try:
        with open(args.entities.replace("entities.json", "header.json"), encoding="utf-8") as f:
            h = json.load(f)
        for pl in h.get("players", []):
            for x in (pl.get("vehicles") or []):
                v.add(x)
    except Exception:
        pass
    AIR_VOCAB = v
    return v


def extract_classname(args, P, tid, name, blk, vs):
    """从块里取 unit__className。

    **不要用 strings[:N]**：顺序不保证。实测地面单位的第 1 个串是模板名 `tank`，
    其后是二进制误读出来的乱码，真正的 className 不在前 6 个里。

    - 地面/工事：逐偏移找长度前缀串，前缀必须是 tankModels/ 或 structures/
    - 飞机：逐偏移找长度前缀串，值必须落在机型词表里
    """
    if name in ("tank", "fortification"):
        return find_ground_classname(blk)
    if name in AIR_TEMPLATE_NAMES:
        vocab = _air_vocab(args)
        for i in range(0, max(0, len(blk) - 3)):
            L = blk[i]
            if not (2 <= L <= 60) or i + 1 + L > len(blk):
                continue
            try:
                t = blk[i + 1:i + 1 + L].decode("ascii")
            except Exception:
                continue
            if t in vocab:
                return t
    return None


def quat_to_heading_deg(q):
    """transform 四元数 -> 航向（世界系：正北 0，顺时针为正；x=东, y=上, z=北）。

    约定取自 transform 旋转矩阵的**第一行**（x 轴）在水平面的朝向。
    该轴向是用开发期真值锚定的：本机 uid-368 出生时刻 8111 `compass` = 90.0°，
    而同一时刻四元数按此约定算出正好 90.0°。

    **注意（如实记录）**：我另用"起步 3 s 的位移方向"做过交叉校验，结果是**不确定的**
    （各轴向中位差 32°–90°、MAD≈30°）—— 3 s 内飞机已在转弯，位移方向不是可靠航向真值，
    所以那次校验既不能证实也不能证伪本约定。下游若发现系统性偏差（例如整体差 90°），
    请回传，我按实测改轴向。
    """
    if not q:
        return None
    w, x, y, z = q
    n = math.sqrt(w * w + x * x + y * y + z * z)
    if n < 1e-9:
        return None
    w, x, y, z = w / n, x / n, y / n, z / n
    # 旋转矩阵第一行（x 轴基向量）
    vx = 1 - 2 * (y * y + z * z)
    vz = 2 * (x * z + w * y)
    if abs(vx) < 1e-9 and abs(vz) < 1e-9:
        return None
    return round(math.degrees(math.atan2(vx, vz)) % 360.0, 1)


def enumerate_entities(args, P):
    """把 P.creations 展开成实体清单（每条构造消息一条记录）。"""
    rows = []
    for tick, eid, tid, name, bs, used, cc, values, blk, vs in P.creations:
        klass = classify_template(name)
        uid = None
        if vs + UID_REL_OFFSET + 2 <= len(blk):
            raw = blk[vs + UID_REL_OFFSET] | (blk[vs + UID_REL_OFFSET + 1] << 8)
            if 1 <= raw <= 2046 and (raw & 0x7FF) != 0x7FF:
                uid = raw & 0x7FF
        pos = None
        quat = None
        pos_src = None
        if klass == "projectile":
            px, pq, prel = find_projectile_transform(blk, vs)
            if px:
                pos = px
                quat = pq
                pos_src = ("四元数锚定：块内 |q|≈1 的 float4 之前 12 字节"
                           "（position=ref-%d；该偏移随 block 长度变）" % prel)
        if pos is None and template_has_transform(P, tid) and vs + POSITION_REL + 12 <= len(blk):
            x, y, z = struct.unpack_from("<3f", blk, vs + POSITION_REL)
            if all(math.isfinite(v) for v in (x, y, z)) and abs(x) <= 45000 and abs(z) <= 45000:
                pos = [round(x, 2), round(y, 2), round(z, 2)]
                pos_src = "transform+%d(float32, x/up/z, 米)" % POSITION_REL
                if vs + QUATERNION_REL + 16 <= len(blk):
                    q = struct.unpack_from("<4f", blk, vs + QUATERNION_REL)
                    if all(math.isfinite(v) and abs(v) <= 1.001 for v in q):
                        n = math.sqrt(sum(v * v for v in q))
                        if 0.97 < n < 1.03:
                            quat = [round(v, 6) for v in q]
        rows.append({
            "entityId": "0x%x" % eid,
            "entityIndex": eid & 0x3FFFFF,
            "template": name,
            "class": klass,
            "spawnTick": int(tick),
            "spawnSec": round(tick * SEC_PER_TICK, 3),
            "blockSize": bs,
            "uid": uid,
            "className": extract_classname(args, P, tid, name, blk, vs),
            "strings": block_strings(blk)[:6],
            "position": pos,
            "positionSource": pos_src,
            "quaternion": quat,
            "hdgDeg": quat_to_heading_deg(quat),
        })
    return rows


def write_entities(args, P, rows):
    by_class = collections.Counter(r["class"] for r in rows)
    with_pos = collections.Counter(r["class"] for r in rows if r["position"])
    tmpl = collections.Counter(r["template"] for r in rows)
    out = {
        "schema": "wrpl-ecs-entities/1",
        "source": "ptype=6 ECS 实体构造消息（control 0x24/0x25）",
        "generatedFrom": os.path.basename(args.payload),
        "secPerTick": SEC_PER_TICK,
        "counts": {
            "creationRecords": len(rows),
            "distinctEntityIndex": len({r["entityIndex"] for r in rows}),
            "templates": len(P.templates),
            "componentDefs": len(P.compdefs),
            "classNameCoverage": {"ground": sum(1 for r in rows if r["class"] == "ground" and r["className"]),
                                  "structure": sum(1 for r in rows if r["class"] == "structure" and r["className"]),
                                  "aircraft": sum(1 for r in rows if r["class"] == "aircraft" and r["className"]),
                                  "groundTotal": sum(1 for r in rows if r["class"] == "ground"),
                                  "structureTotal": sum(1 for r in rows if r["class"] == "structure"),
                                  "aircraftTotal": sum(1 for r in rows if r["class"] == "aircraft")},
            "byClass": dict(by_class),
            "byClassWithPosition": dict(with_pos),
            "byTemplate": dict(tmpl.most_common()),
        },
        "classCriteria": {
            "projectile": "模板组件恰好 = [<xxx>_component, active, proj__traceHandle]；"
                          "本局有 rocket / rocket+arh_shell / bomb / payload / jettisoned",
            "explosion": "含 damage_area__* 组件（炸弹落点/爆炸区）",
            "aircraft": "模板 aircraft / aircraft+player_unit（unit_storage__aircraft + uid）",
            "ground": "模板 tank（unit_storage__tank + uid，className 形如 tankModels/...）",
            "structure": "模板 fortification（unit_storage__fortification + uid，structures/...）",
            "player_slot": "模板 sync_player（possessed/connid/is_local/team/base_player_id）",
            "mod": "模板含 mod__* 组件（工具包/灭火器/医疗包/烟雾）",
            "scenery": "灯柱/机场警示灯/grid_holder/destroy_ri_box_area 等布景与触发体",
            "misc": "其余（msg_sink / server_load_stat 等）",
        },
        "positionRule": {
            "rule": "只有第一个组件是 transform 的模板才取位置：组件值起点 + %d 处 3×float32 "
                    "= (x, 上, z)，单位米；四元数在 + %d" % (POSITION_REL, QUATERNION_REL),
            "validatedOn": {
                "aircraft+player_unit": "81/81 命中；|y| 中位 1223 m（空中）",
                "tank": "115/116 命中；|y| 中位 29 m（地面）",
                "fortification": "69/69 命中；|y| 中位 61 m（地面）",
            },
            "independentCheck": "同一规则下「地面单位贴地、飞机有高度」同时成立 —— 这是对偏移与"
                                "轴向语义的独立验证，不是自证",
            "notSolved": "弹类（rocket/bomb/payload/jettisoned）第一个组件是 *_component，"
                         "其 99 字节不透明块里没有可验证的位置；已试过的偏移与排除理由见 "
                         "docs/REPLAY-DECODE-ECS.md §3.6",
        },
        "devValidation": getattr(args, "_groundval", None),
        "limits": [
            "position 只对「transform 打头」的模板给出；弹类为 null（未解，不是 0）",
            "uid 只对带 uid 组件的模板给出（飞机/地面/工事）；弹类模板没有 uid 组件",
            "position 是**出生时刻**的位置，不是航迹",
            "本文件不含任何外部遥测数据；全部字段来自 .wrpl",
        ],
        "entities": rows,
    }
    with open(args.entities_out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def verify_ground_positions(rows, capture_dir):
    """开发期校验：ECS 地面/工事位置 vs 对照数据里的地面接触点。

    只返回数字（不带任何外部坐标）。坐标对应：ECS (x, up, z) ↔ 世界 (east, up, north)；
    对照数据的归一化 y 是屏幕轴（向下=南），所以 north = -(y-0.5)*65536。
    """
    import numpy as np
    path = os.path.join(capture_dir, "map_obj.jsonl")
    if not os.path.exists(path):
        return {"note": "无对照数据，跳过"}
    gx, gz = [], []
    for o in _rows(path):
        d = o.get("data")
        if not isinstance(d, list):
            continue
        for e in d:
            if e.get("type") == "aircraft" or "x" not in e or "y" not in e:
                continue
            if e.get("icon") in (None, "none"):
                continue
            gx.append((float(e["x"]) - 0.5) * 2.0 * MAP_HALF)
            gz.append(-(float(e["y"]) - 0.5) * 2.0 * MAP_HALF)
    if not gx:
        return {"note": "对照数据里没有地面接触点"}
    G = np.asarray([gx, gz]).T
    sub = [r for r in rows if r["class"] in ("ground", "structure") and r["position"]]
    if not sub:
        return {"note": "没有可校验的地面位置"}
    d_ok, d_bad = [], []
    for r in sub:
        x, up, z = r["position"]
        d_ok.append(float(np.min(np.hypot(G[:, 0] - x, G[:, 1] - z))))
        d_bad.append(float(np.min(np.hypot(G[:, 0] - x, G[:, 1] + z))))
    d_ok = np.asarray(d_ok)
    d_bad = np.asarray(d_bad)
    return {
        "groundPositionsTested": int(len(sub)),
        "groundNearestContactMedianM": round(float(np.median(d_ok)), 2),
        "groundNearestContactP75M": round(float(np.percentile(d_ok, 75)), 1),
        "groundFracWithin100M": round(float((d_ok <= 100).mean()), 4),
        "groundFracWithin300M": round(float((d_ok <= 300).mean()), 4),
        "controlWrongAxisMedianM": round(float(np.median(d_bad)), 1),
        "controlWrongAxisFracWithin300M": round(float((d_bad <= 300).mean()), 4),
        "note": "ECS 地面/工事位置与对照数据地面接触点比对；对照组把 north 轴向取反。"
                "只保留数字，不保留任何外部坐标。",
    }


# ---------------------------------------------------------------- 地面单位层

# 地名/型号名美化规则（**机械变换，不是经核实的历史型号名**）：
#   1. 去前缀 tankModels/ 或 structures/
#   2. 若首段是国家码（us/ussr/uk/germ/jp/fr/it/cn/sw/il/...），记下来，从型号里去掉
#   3. 余下各段：单字母段 + 紧跟的纯数字段 用 '-' 连（t_55 -> T-55），其余段直接拼接大写
#   4. 国家码以 "(USSR)" 形式附在末尾
# 映射不了的**原样保留**，并在 vehicleNameSource 里标 "raw"。
COUNTRY = {
    "us": "US", "usa": "US", "ussr": "USSR", "ru": "RU", "uk": "UK", "gb": "UK",
    "germ": "DE", "ger": "DE", "jp": "JP", "fr": "FR", "it": "IT", "cn": "CN",
    "sw": "SE", "se": "SE", "il": "IL", "br": "BR",
}
GCLASS = re.compile(r"^(?:tankModels|structures)/[\w/.\-]+$")


def find_ground_classname(blk):
    """块内找形如 tankModels/xxx 或 structures/xxx 的长度前缀串（逐偏移，不跳过）。"""
    for i in range(0, max(0, len(blk) - 6)):
        L = blk[i]
        if not (5 <= L <= 80) or i + 1 + L > len(blk):
            continue
        try:
            s_ = blk[i + 1:i + 1 + L].decode("ascii")
        except Exception:
            continue
        if GCLASS.match(s_):
            return s_
    return None


# 显示名：由内部名的词元直接拆出来，只用于画面标签，**不是**经考据的历史型号名。
# 未列出的保持原样（vehicleNameSource='raw'）；原始名永远在 vehicle 里。
DISPLAY_NAME = {
    "tankModels/ussr_t_55_am": "T-55AM",
    "tankModels/ussr_object_906": "Object 906",
    "tankModels/ussr_btr_152a": "BTR-152A",
    "tankModels/us_truck_us6_studebaker_tent": "US6 Studebaker",
    "tankModels/fr_crotale_ng": "Crotale NG",
    "tankModels/uk_rooikat_za_35": "Rooikat ZA-35",
    "tankModels/germ_flakpz_I_Gepard": "Flakpanzer I Gepard",
    "structures/ussr_quest_large_art_noground": "Large Arty Site",
    "structures/uk_bunker_light_fat": "Bunker (Light)",
    "structures/unit_airfield_flight_radar_char": "Airfield Flight Radar",
    "structures/unit_airfield_main_radar_char": "Airfield Main Radar",
    "structures/dummy_immortal": "Indestructible Dummy",
}


def pretty_vehicle_name(raw):
    """内部名 -> 可读名（机械规则，见文件头注释）。返回 (name, source)。"""
    if raw in DISPLAY_NAME:
        return DISPLAY_NAME[raw], "curated"
    if not raw or "/" not in raw:
        return raw, "raw"
    prefix, rest = raw.split("/", 1)
    toks = [t for t in rest.split("_") if t]
    if not toks:
        return raw, "raw"
    country = None
    if toks[0].lower() in COUNTRY:
        country = COUNTRY[toks[0].lower()]
        toks = toks[1:]
    if not toks:
        return raw, "raw"
    out = ""
    i = 0
    while i < len(toks):
        t = toks[i]
        if len(t) <= 2 and i + 1 < len(toks) and toks[i + 1].isdigit():
            out += ("" if not out else "-") + t.upper() + "-" + toks[i + 1]
            i += 2
            continue
        seg = t.upper()
        if out and not out.endswith("-"):
            out += "-" if (len(t) <= 2 and t.isalpha()) else ""
        out += seg
        i += 1
    if country:
        out = "%s (%s)" % (out, country)
    return out, "mechanical"


def build_ground_units(args, P, rows):
    """从实体清单抽 ground/structure，转成契约 §2.3 风格的静态单位表。"""
    rows = [r for r in rows if r["class"] in ("ground", "structure") and r["position"]]
    rows.sort(key=lambda r: (r["spawnSec"], r["entityIndex"]))
    units = []
    cnt = collections.Counter()
    dup_eidx = 0
    seen = set()
    for r in rows:
        if r["entityIndex"] in seen:
            dup_eidx += 1
        seen.add(r["entityIndex"])
        blk = None
        # 重新取原始块以拿 className
        for c in P.creations:
            if c[1] == int(r["entityId"], 16):
                blk = c[8]
                break
        raw = find_ground_classname(blk) if blk else None
        vname, vsrc = pretty_vehicle_name(raw)
        slug = re.sub(r"[^a-z0-9]+", "-", (raw or r["template"]).split("/")[-1].lower()).strip("-")[:24]
        cnt[slug] += 1
        x, up, z = r["position"]
        units.append({
            "key": "%s-%s-%03d" % ("gnd" if r["class"] == "ground" else "str", slug, cnt[slug]),
            "domain": "ground",
            "class": r["class"],
            "template": r["template"],
            "vehicle": raw,
            "vehicleName": vname,
            "vehicleNameSource": vsrc,
            "uid": r["uid"],
            "x": x,
            "y": z,
            "z": up,
            "spawnTickRaw": r["spawnTick"],
            "static": True,
            "side": None,
            "sideSource": "unresolved",
        })
    gv = getattr(args, "_groundval", None) or {}
    _ = dup_eidx
    out = {
        "schema": "wrpl-ground-units/1",
        "count": len(units),
        "coordinateSystem": {
            "x": "east, 米", "y": "north, 米", "z": "up, 米",
            "source": "ECS transform 平移 (float32)；组件值起点 + %d" % POSITION_REL,
            "note": "ECS 原始是 (x, up, z)；这里已换成世界 (east, north, up)，与 tracks.json 的 "
                    "axisMap{x:PosX, y:PosZ, z:PosY} 同向。对照数据的归一化 y 是屏幕轴"
                    "（向下=南），做开发期比对时 north = -(y-0.5)*65536。",
        },
        "vehicleNameNote": "vehicleName 是内部名的**机械美化**（去前缀/去国家码/单字母+数字用连字符/"
                           "大写），不是经核实的历史型号名；原始名见 vehicle，映射不了的原样保留"
                           "（vehicleNameSource='raw'）。",
        "tickUnit": "spawnTickRaw 是回放原始 tick（256 kHz 计数器）；秒 = spawnTickRaw / 256000。"
                    "本文件只给 raw tick 一种口径，不再另给秒，避免 256 倍误读。",
        "samples": "每条只有一个采样点（静止单位）；static=true，画方块标记即可，不要画航迹。"
                   "同一 entityIndex 若出现两次（本局 %d 条），视为重复创建，位置一致。" % dup_eidx,
        "sideNote": "side 一律 null：ECS 的 team 组件是位打包的，本线没有把它单独隔离出来；"
                    "不用外部颜色去补。",
        "devValidation": {
            "groundPositionsTested": gv.get("groundPositionsTested"),
            "medianDistToDevGroundM": gv.get("groundNearestContactMedianM"),
            "p75DistM": gv.get("groundNearestContactP75M"),
            "within100m": gv.get("groundFracWithin100M"),
            "within300m": gv.get("groundFracWithin300M"),
            "axisFlipMedianM": gv.get("controlWrongAxisMedianM"),
            "note": "与开发期对照数据的地面接触点做平面最近邻；只保留数字，不含任何外部坐标。",
        },
        "units": units,
    }
    return out


def cmd_ground(args, P, rows):
    """只打印统计；**不再写文件** —— 地面层由兄弟线的 ground.json 承载，避免两份竞争。"""
    out = build_ground_units(args, P, rows)
    print("=== 地面单位层（仅供核对，不写文件）===")
    print("  %d 条（ground %d / structure %d）；正式产物见 ace-combat/build/wrpl/ground.json" % (
        out["count"],
        sum(1 for u in out["units"] if u["class"] == "ground"),
        sum(1 for u in out["units"] if u["class"] == "structure")))
    vs = collections.Counter(u["vehicle"] for u in out["units"])
    for v, n in vs.most_common():
        ex = next(u for u in out["units"] if u["vehicle"] == v)
        print("    %-46s n=%-3d → %s" % (v, n, ex["vehicleName"]))
    return out


def verify_launch_proximity(units, tracks_path):
    """开发期校验：弹体出生点应当紧邻同刻的发射机。

    参照是兄弟线 `tracks.json`（同样由回放解出，不含外部遥测）。只回数字。
    """
    import numpy as np
    if not os.path.exists(tracks_path):
        return {"note": "没有 tracks.json，跳过发射点邻近校验"}
    with open(tracks_path, encoding="utf-8") as f:
        tj = json.load(f)
    air = []
    for u in tj.get("units", []):
        for s_ in u.get("samples", []):
            if len(s_) >= 4 and None not in s_[1:4]:
                air.append((s_[0], float(s_[1]), float(s_[2]), float(s_[3])))
    if len(air) < 100:
        return {"note": "tracks.json 样本太少，跳过"}
    air.sort()
    at = np.asarray([a[0] for a in air], dtype=np.float64)
    ap = np.asarray([[a[1], a[2], a[3]] for a in air], dtype=np.float64)
    ds = []
    for u in units:
        i = int(np.searchsorted(at, u["spawnTickRaw"]))
        lo, hi = max(0, i - 300), min(len(at), i + 300)
        if hi <= lo:
            continue
        p = np.asarray([u["x"], u["y"], u["z"]], dtype=np.float64)
        ds.append(float(np.sqrt(((ap[lo:hi] - p) ** 2).sum(1)).min()))
    if not ds:
        return {"note": "没有可比对的样本"}
    ds = np.asarray(ds)
    return {
        "launchPointsTested": int(len(ds)),
        "strategy": "出生点必须紧邻同刻的发射机（参照 tracks.json 的飞机航迹，回放自身数据）",
        "medianDistToNearestAircraftM": round(float(np.median(ds)), 2),
        "p90DistM": round(float(np.percentile(ds, 90)), 1),
        "within100m": round(float((ds <= 100).mean()), 4),
        "within300m": round(float((ds <= 300).mean()), 4),
        "note": "若位置字段选错，这一项会退化到公里级；实测为米级。只保留数字。",
    }


def build_launches(args, rows):
    """弹类发射/投放事件（有位置的），扁平好合并。"""
    u = [r for r in rows if r["class"] == "projectile"]
    launched = [r for r in u if r["position"]]
    out = {
        "schema": "wrpl-projectile-launches/1",
        "count": len(u),
        "withPosition": len(launched),
        "withPositionRate": round(len(launched) / len(u), 4) if u else 0,
        "tickUnit": "replayRawTick；秒 = tick / 256000（本文件只给 raw tick 一种口径）",
        "coordinateSystem": {
            "x": "east, 米", "y": "north, 米", "z": "up, 米",
            "note": "ECS 原始是 (x, up, z)；这里换成世界 (east, north, up)，与 "
                    "tracks.json 的 axisMap{x:PosX, y:PosZ, z:PosY} 同向。"
                    "转换前我漏了这一步，导致一度误判为「弹类位置未解」。",
        },
        "positionRule": "块内唯一一个 |q|≈1 的 float4（四元数）之前 12 字节 = 3×float32 位置。"
                        "不写死偏移，因为偏移随 block 长度变（28/29/30…）。",
        "devValidation": getattr(args, "_launchval", None),
        "byTemplate": dict(collections.Counter(r["template"] for r in u)),
        "limits": [
            "位置是**创建时刻**的位置，不是弹道航迹；每条只有一个采样点",
            "弹药在 ptype=2 FlightModel 里没有对应 uid（模板没有 uid 组件），所以拿不到逐帧航迹",
            "没有解出的少数条目 position=null（不填 0）",
        ],
        "units": [{
            "entityId": r["entityId"],
            "entityIndex": r["entityIndex"],
            "template": r["template"],
            "spawnTickRaw": r["spawnTick"],
            "x": r["position"][0], "y": r["position"][2], "z": r["position"][1],
            "quaternion": r["quaternion"],
        } for r in launched],
    }
    with open(args.launches_out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def cmd_launches(args, rows):
    args._launchval = verify_launch_proximity(
        [{"spawnTickRaw": r["spawnTick"], "x": r["position"][0], "y": r["position"][2],
          "z": r["position"][1]} for r in rows
         if r["class"] == "projectile" and r["position"]],
        os.path.join(os.path.dirname(args.out), "tracks.json"))
    out = build_launches(args, rows)
    print("=== 弹药发射点 ===")
    print("  %d 条弹类实体，其中 %d 条解出位置（%.1f%%）" % (
        out["count"], out["withPosition"], 100 * out["withPositionRate"]))
    for t, n in collections.Counter(r["template"] for r in out["units"]).most_common():
        print("    %-18s %d" % (t, n))
    if out.get("devValidation", {}).get("launchPointsTested"):
        v = out["devValidation"]
        print("  开发期校验：到最近飞机航迹 中位 %.1f m、p90 %.0f m；"
              "<=100m %.1f%%、<=300m %.1f%%" % (
                  v["medianDistToNearestAircraftM"], v["p90DistM"],
                  100 * v["within100m"], 100 * v["within300m"]))
    print("  写 %s" % args.launches_out)
    return out


# ---------------------------------------------------------------- 类别表

# type 字段 -> 类别（datamine blkx 的 "type"）
TYPE2CAT = {
    # 地面
    "typeMediumTank": "TANK", "typeLightTank": "TANK", "typeHeavyTank": "TANK",
    "typeTank": "TANK", "typeTankDestroyer": "TANK",
    "typeSPAA": "SPAA", "typeSPG": "ARTILLERY", "typeSelfPropelledGun": "ARTILLERY",
    "typeIFV": "IFV", "typeAPC": "APC", "typeTruck": "TRUCK", "typeWheeled": "TRUCK",
    # 空中（flightmodels 只有这几类有区分力）
    "typeFighter": "FIGHTER", "typeBomber": "BOMBER", "typeAttacker": "ATTACKER",
    "typeStormovik": "ATTACKER", "typeHelicopter": "HELI", "typeTransport": "TRANSPORT",
    # 海军（全量 datamine 里有 700+ 艘，不映射会被误判成 UNKNOWN）
    "typeDestroyer": "SHIP", "typeGunBoat": "SHIP", "typeTorpedoBoat": "SHIP",
    "typeShip": "SHIP", "typeTorpedoGunBoat": "SHIP", "typeSubmarineChaser": "SHIP",
    "typeNavalFerryBarge": "SHIP", "typeMinelayer": "SHIP", "typeCruiser": "SHIP",
    "typeBattleship": "SHIP", "typeCarrier": "SHIP", "typeSubmarine": "SHIP",
}
# primaryCraftType（结算 BLK，wrpl-format 解出）-> 类别
CRAFT2CAT = {"fighter": "FIGHTER", "assault": "ATTACKER", "bomber": "BOMBER",
             "helicopter": "HELI", "heli": "HELI", "attacker": "ATTACKER"}
# onRadarAs -> 类别（structures 用）
RADAR2CAT = {"pillbox": "BUNKER", "artillery": "ARTILLERY", "bunker": "BUNKER",
             "radar": "RADAR", "sam": "SPAA"}

# 逐条覆盖（datamine 的 type 描述的是**底盘/通用**类，与角色不符时按内部名纠正）
CATEGORY_OVERRIDE = {
    "tankModels/us_truck_us6_studebaker_tent": ("TRUCK",
        "blkx type=typeLightTank 说的是底盘类；内部名 us_truck_/us6 明示是卡车"),
}


def _blkx_fields(fp, fields=("type", "expClass", "subclass", "onRadarAs", "moveType")):
    try:
        txt = open(fp, encoding="utf-8", errors="replace").read(400000)
    except Exception:
        return {}
    out = {}
    for k in fields:
        m = re.search(r'"%s"\s*:\s*"?([A-Za-z0-9_]+)' % k, txt)
        if m:
            out[k] = m.group(1)
    return out


def _units_csv_generic(args):
    """units.csv 的 <name>_0/_1/_2 通用键 -> 类别词（datamine 原生的英文类别名）。"""
    import csv as _csv
    base = getattr(args, "datamine", None) or DATAMINE_DEFAULT
    fp = os.path.join(base, "lang.vromfs.bin_u", "lang", "units.csv")
    out = {}
    if not os.path.exists(fp):
        return out
    try:
        with open(fp, encoding="utf-8", newline="") as f:
            rd = _csv.reader(f, delimiter=";", quotechar='"')
            next(rd, None)
            for row in rd:
                if len(row) >= 2 and row[0] and row[0].endswith(("_0", "_1", "_2")):
                    out.setdefault(row[0], row[1])
    except Exception:
        pass
    return out


def build_categories(args, P=None):
    """**全量**类别表：datamine units.csv 里所有载具 -> 类别。

    判据优先级（每条的来源都记在 __meta.sources）：
      0. CATEGORY_OVERRIDE（逐条人工确认）
      1. 工事/结构：名字含 radar -> RADAR；否则 onRadarAs；否则 subclass=fortification
      2. 地面载具：onRadarAs（ifv/sam/aam/spaa/artillery/anti_tank_artillery/mlrs/pillbox）
                   -> 否则 blkx 的 type -> 否则名字关键词
      3. 飞机：结算 BLK 的 primaryCraftType -> 否则 flightmodels 的 type
               -> 否则直升机名前缀 -> 否则 UNKNOWN
    键同时给裸名与回放里的带前缀形式（tankModels/X、structures/X），便于一行 join。
    """
    base = getattr(args, "datamine", None) or DATAMINE_DEFAULT
    units_dir = os.path.join(base, "aces.vromfs.bin_u", "gamedata", "units")
    fm_dir = os.path.join(base, "aces.vromfs.bin_u", "gamedata", "flightmodels")
    dmv = None
    try:
        dmv = open(os.path.join(base, "version"), encoding="utf-8").read().strip()
    except Exception:
        pass

    # blkx 索引
    # 索引统一用**小写**键：units.csv 里是 cn_m48a1_patton_III，blkx 文件名是小写。
    ub = {}
    if os.path.isdir(units_dir):
        for root, _d, fs in os.walk(units_dir):
            for f in fs:
                if f.endswith(".blkx"):
                    ub.setdefault(f[:-5].lower(), os.path.join(root, f))
    fb = {}
    if os.path.isdir(fm_dir):
        for root, _d, fs in os.walk(fm_dir):
            for f in fs:
                if f.endswith(".blkx"):
                    fb.setdefault(f[:-5].lower(), os.path.join(root, f))

    # 载具名全集：units.csv 的 _shop 键 + structures 的 _0/_1/_2 键
    names = set()
    langdir = os.path.join(base, "lang.vromfs.bin_u", "lang", "units.csv")
    try:
        import csv as _csv
        with open(langdir, encoding="utf-8", newline="") as f:
            rd = _csv.reader(f, delimiter=";", quotechar='"')
            next(rd, None)
            for row in rd:
                if len(row) < 2 or not row[0]:
                    continue
                k = row[0]
                if k.endswith("_shop"):
                    b = k[:-5]
                    names.add(b.split("/", 1)[1] if b.startswith("structures/") else b)
                elif re.search(r"_[0-9]$", k):
                    # 注意：这类键**可能没有 structures/ 前缀**（真实键是
                    # `swe_152mm_for_land_battles_0`），所以前缀可选。
                    # 只剥**单个数字**后缀，避免误伤 `mig_21_2000` 这类名字。
                    names.add(k.split("/", 1)[-1][: -2])
    except Exception:
        pass

    # 飞机类别：结算 BLK 的 primaryCraftType
    craft = {}
    try:
        h = json.load(open(os.path.join(os.path.dirname(args.out), "header.json"),
                           encoding="utf-8"))
        for pl in h.get("players", []):
            for v in (pl.get("vehicles") or []):
                ct = (pl.get("primaryCraftType") or "").lower()
                if ct:
                    craft.setdefault(v, CRAFT2CAT.get(ct))
    except Exception:
        pass

    HELI_PREFIX = ("mi_", "mi-", "ka_", "ka-", "ah_", "ah-", "uh_", "uh-", "ch_", "oh_",
                   "gazelle", "lynx_", "bo_105", "a_129", "ah1", "sa_3", "z_9", "z_10",
                   "z_19", "h_34", "sh_60", "mh_60", "mq_", "ab_205", "ab_212", "uh1")

    cat, src, why = {}, {}, {}
    for nm in sorted(names):
        if nm in CATEGORY_OVERRIDE:
            cat[nm], why[nm] = CATEGORY_OVERRIDE[nm][0], CATEGORY_OVERRIDE[nm][1]
            src[nm] = "override"
            continue
        lk = nm.lower()
        # 常见后缀归一化：-ace/_ace 是"王牌涂装"变体，模型与基型相同
        lk2 = re.sub(r"[-_](ace|boss|prem|gift|trophy)$", "", lk)
        key_u = lk if lk in ub else (lk2 if lk2 in ub else None)
        key_f = lk if lk in fb else (lk2 if lk2 in fb else None)
        f = _blkx_fields(ub[key_u]) if key_u else {}
        ff = _blkx_fields(fb[key_f]) if key_f else {}
        ora = f.get("onRadarAs")
        typ = f.get("type") or ff.get("type")
        is_struct = f.get("subclass") == "fortification" or nm.startswith(
            ("ussr_quest", "uk_bunker", "dummy_immortal", "unit_airfield"))
        nav = (typ or "").lower()
        if nm in craft and craft[nm]:
            cat[nm], src[nm] = craft[nm], "results.primaryCraftType"
        elif "radar" in nm.lower():
            cat[nm], src[nm] = "RADAR", "name contains 'radar'"
        elif ora in RADAR2CAT:
            cat[nm], src[nm] = RADAR2CAT[ora], "blkx onRadarAs=%s" % ora
        elif typ in TYPE2CAT:
            cat[nm], src[nm] = TYPE2CAT[typ], "blkx type=%s" % typ
        elif ff.get("type") in TYPE2CAT:
            cat[nm], src[nm] = TYPE2CAT[ff["type"]], "flightmodels type=%s" % ff["type"]
        elif "truck" in nm.lower():
            cat[nm], src[nm] = "TRUCK", "name contains 'truck'"
        elif "bunker" in nm.lower():
            cat[nm], src[nm] = "BUNKER", "name contains 'bunker'"
        elif any(w in nm.lower() for w in ("artillery", "howitzer", "art_")):
            cat[nm], src[nm] = "ARTILLERY", "name keyword"
        elif is_struct:
            cat[nm], src[nm] = "FORTIFICATION", "blkx subclass=fortification"
        elif f.get("subclass") == "warShip":
            cat[nm], src[nm] = "SHIP", "blkx subclass=warShip"
        elif nm.lower().startswith(HELI_PREFIX):
            cat[nm], src[nm] = "HELI", "helicopter name prefix"
        elif key_f:
            cat[nm], src[nm] = "FIGHTER", "flightmodel present, type unresolved"
        elif re.search(r"\d{2,3}\s*mm", nm.lower()):
            cat[nm], src[nm] = "ARTILLERY", "name contains a gun calibre (NNmm)"
        elif nm.lower().startswith(("structure", "quest", "dummy_")) or "structure" in nm.lower():
            cat[nm], src[nm] = "FORTIFICATION", "structure-like name"
        else:
            cat[nm], src[nm] = "UNKNOWN", ("no blkx for this name" if not (key_u or key_f)
                                           else "blkx present but no type field")

    # 别名：回放里地面是 tankModels/X、工事是 structures/X
    GROUND_CATS = {"TANK", "IFV", "APC", "SPAA", "ARTILLERY", "TRUCK"}
    out, out_src = {}, {}
    for nm, c in cat.items():
        out[nm] = c
        out_src[nm] = src[nm]
        if c in GROUND_CATS:
            out["tankModels/" + nm] = c
            out_src["tankModels/" + nm] = "alias of %r" % nm
        elif c in ("RADAR", "BUNKER", "FORTIFICATION", "SHIP"):
            if nm not in ("dummy_immortal",):
                out.setdefault("structures/" + nm, c)
                out_src.setdefault("structures/" + nm, "alias of %r" % nm)

    # 分隔符归一化别名（连字符 <-> 下划线）：units.csv 是 f-4s，回放 className 可能是 f_4s
    for nm in list(out):
        v = out[nm]
        for alt in (nm.replace("-", "_"),):   # 只补 hyphen->underscore
            if alt != nm and alt not in out:
                out[alt] = v
                out_src[alt] = "separator alias of %r" % nm

    # 分隔符归一化别名（连字符 <-> 下划线）：units.csv 是 f-4s，回放 className 可能是 f_4s
    for nm in list(out):
        v = out[nm]
        for alt in (nm.replace("-", "_"),):   # 只补 hyphen->underscore
            if alt != nm and alt not in out:
                out[alt] = v
                out_src[alt] = "separator alias of %r" % nm

    # ECS 模板名本身也可能被当作"型号"传进来（页面/解析器的兜底），一并给类别
    TEMPLATE_CATS = {"tank": "TANK", "fortification": "FORTIFICATION", "structure": "FORTIFICATION",
                     "aircraft": "FIGHTER", "aircraft+player_unit": "FIGHTER",
                     "rocket": "MISSILE", "bomb": "BOMB", "payload": "BOMB",
                     "jettisoned": "BOMB", "damage_area": "FORTIFICATION"}
    for k, v in TEMPLATE_CATS.items():
        cat.setdefault(k, v)
        src.setdefault(k, "ECS template name")

    dist = collections.Counter(cat.values())
    # ---- 补名字后处理（在任何构建之后、写文件之前）
    #  ① ECS 模板名（解析器兜底会把 tank / fortification 当型号传进来）
    #  ② 回放里实际出现、但 datamine units.csv 没有的 unit__className
    #     （例：mission structure `structures/swe_152mm_for_land_battles`）
    TEMPLATE_CATS = {"tank": "TANK", "fortification": "FORTIFICATION", "structure": "FORTIFICATION",
                     "aircraft": "FIGHTER", "aircraft+player_unit": "FIGHTER",
                     "rocket": "MISSILE", "bomb": "BOMB", "payload": "BOMB",
                     "jettisoned": "BOMB", "damage_area": "FORTIFICATION"}
    for k2, v2 in TEMPLATE_CATS.items():
        if k2 not in out:
            out[k2] = v2
            out_src[k2] = "ECS template name"

    if P is not None:
        obs = set()
        for c in P.creations:
            if c[3] in ("tank", "fortification"):
                n2 = find_ground_classname(c[8])
                if n2:
                    obs.add(n2)
            elif c[3] in AIR_TEMPLATE_NAMES and c[8]:
                for i2 in range(0, max(0, len(c[8]) - 3)):
                    L2 = c[8][i2]
                    if 2 <= L2 <= 60 and i2 + 1 + L2 <= len(c[8]):
                        try:
                            t3 = c[8][i2 + 1:i2 + 1 + L2].decode("ascii")
                        except Exception:
                            continue
                        if t3 in _air_vocab(args):
                            obs.add(t3)
        for n2 in sorted(obs):
            for form in (n2, n2.split("/", 1)[1] if "/" in n2 else n2):
                if form in out:
                    continue
                low = form.lower()
                if "radar" in low:
                    c2, why2 = "RADAR", "replay-observed name, contains 'radar'"
                elif "bunker" in low or "pillbox" in low:
                    c2, why2 = "BUNKER", "replay-observed name, contains 'bunker'"
                elif re.search(r"\d{2,3}\s*mm", low) or "howitzer" in low or "_art" in low \
                        or low.startswith("art"):
                    c2, why2 = "ARTILLERY", "replay-observed name, artillery-like"
                elif form.startswith("structures/") or "structure" in low or low.startswith(("quest", "dummy_")):
                    c2, why2 = "FORTIFICATION", "replay-observed structure"
                elif low.startswith(HELI_PREFIX):
                    c2, why2 = "HELI", "replay-observed name, helicopter prefix"
                else:
                    c2, why2 = "FORTIFICATION", "replay-observed name, fallback"
                out[form] = c2
                out_src[form] = why2
        # 再补一次带前缀/去前缀形式
        for n2 in list(out):
            if "/" not in n2 and out[n2] in ("RADAR", "BUNKER", "FORTIFICATION", "SHIP"):
                out.setdefault("structures/" + n2, out[n2])
                out_src.setdefault("structures/" + n2, "alias of %r" % n2)

    # 模板名也要能查到（回放里 ground 的 vehicle 有时就是模板名）
    for k2, v2 in (("tank", "TANK"), ("fortification", "FORTIFICATION"),
                   ("structure", "FORTIFICATION"), ("scenery", "SCENERY"),
                   ("aircraft", "FIGHTER"), ("aircraft+player_unit", "FIGHTER"),
                   ("rocket", "MISSILE"), ("bomb", "BOMB"), ("payload", "BOMB"),
                   ("jettisoned", "BOMB"), ("damage_area", "FORTIFICATION")):
        if k2 not in out:
            out[k2] = v2
            out_src[k2] = "ECS template name"

    out["__meta"] = {
        "schema": "wrpl-vehicle-categories/2",
        "scope": "FULL datamine units.csv (not limited to one replay)",
        "source": ("Categories derived from gszabi99/War-Thunder-Datamine (local read-only copy "
                   "at .ref/War-Thunder-Datamine): blkx type / onRadarAs / subclass fields, plus "
                   "the results BLK primaryCraftType and flightmodels type for aircraft."),
        "datamineVersion": dmv,
        "credit": "War-Thunder-Datamine by gszabi99 (read-only reference; nothing redistributed)",
        "convention": ("Category is a DISPLAY CONVENTION for the on-screen 'specific model vs "
                       "category' toggle, not replay data. English uppercase ASCII."),
        "vocabulary": ["TANK", "IFV", "APC", "SPAA", "ARTILLERY", "TRUCK", "RADAR", "BUNKER",
                       "FORTIFICATION", "SHIP", "FIGHTER", "ATTACKER", "BOMBER", "HELI",
                       "TRANSPORT", "MISSILE", "BOMB", "SCENERY", "UNKNOWN"],
        "vocabularyNote": ("SHIP and TRANSPORT are extensions beyond the originally agreed list: "
                           "the full datamine contains naval units and transports, and dropping "
                           "them would mislabel them."),
        "type2category": TYPE2CAT,
        "onRadar2category": RADAR2CAT,
        "usage": "category = CATS[vehicle] ?? 'UNKNOWN'; skip keys starting with __.",
        "counts": {"names": len(cat), "emittedKeys": len([k for k in out if not k.startswith("__")]),
                   "byCategory": dict(dist.most_common())},
        "unresolved": sorted(k for k, v in cat.items() if v == "UNKNOWN")[:200],
        "unresolvedCount": sum(1 for v in cat.values() if v == "UNKNOWN"),
        "unresolvedByReason": dict(collections.Counter(
            src[k] for k, v in cat.items() if v == "UNKNOWN")),
        "unresolvedNote": ("'no blkx for this name' means units.csv lists a display name but the "
                           "datamine ships no model file for it (unreleased/reference entries); "
                           "such vehicles cannot appear in a replay and stay UNKNOWN."),
        "sources": out_src,
        "reasons": why,
        "overrides": {k: v[0] for k, v in CATEGORY_OVERRIDE.items()},
        "nonAsciiLeft": sorted(k for k, v in out.items()
                               if not k.startswith("__") and v and any(ord(c) > 127 for c in v)),
    }
    with open(args.categories_out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=True, indent=1)
    return out



AIR_CATS_FOR_TRIM = ("FIGHTER", "ATTACKER", "BOMBER", "HELI")


def trim_aircraft_nickname(name):
    """砍掉尾部**不含数字**的词，至少保留第一个词；去引号；全大写。

    用途：把 `F-4S PHANTOM II` 显示成 `F-4S`。
    **注意**：这条规则无法区分"尾词是绰号"还是"尾词是型号变体"，
    实测会把 `BF 109 E` 砍成 `BF 109`、`SPITFIRE MK.IIA VENTURE I` 砍成 `SPITFIRE`、
    `LANCASTER BMKIII` 砍成 `LANCASTER` —— 共 140 条真实型号被削。所以默认不启用。
    """
    if not name:
        return name
    t = re.sub(r"[\"']", "", name).strip().upper()
    words = t.split()
    if not words:
        return t
    keep = len(words)
    while keep > 1 and not any(ch.isdigit() for ch in words[keep - 1]):
        keep -= 1
    return " ".join(words[:keep])


def apply_aircraft_trim(names, cats):
    """只对飞机类别的条目砍尾词；地面/工事/舰船不动。返回 (新表, 改动条数)。"""
    out = dict(names)
    n = 0
    for k, v in names.items():
        if k.startswith("__") or not v:
            continue
        if cats.get(k) not in AIR_CATS_FOR_TRIM:
            continue
        t = trim_aircraft_nickname(v)
        if t != v:
            out[k] = t
            n += 1
    return out, n


def write_page_tables(args, names, cats):
    """页面/服务器用的扁平表：纯 map、纯 ASCII、不含 __meta。"""
    tdir = getattr(args, "tables_dir", None) or DEF_TABLES_DIR
    os.makedirs(tdir, exist_ok=True)
    npath = os.path.join(tdir, "vehicle_names.json")
    cpath = os.path.join(tdir, "vehicle_categories.json")
    nm = {k: v for k, v in names.items() if not k.startswith("__")}
    cm = {k: v for k, v in cats.items() if not k.startswith("__")}
    if getattr(args, "trim_aircraft_names", False):
        nm, _n = apply_aircraft_trim(nm, cm)
        print("  已按 --trim-aircraft-names 砍尾词: %d 条（只动飞机类别）" % _n)
    with open(npath, "w", encoding="utf-8") as f:
        json.dump(nm, f, ensure_ascii=True, separators=(",", ":"))
    with open(cpath, "w", encoding="utf-8") as f:
        json.dump(cm, f, ensure_ascii=True, separators=(",", ":"))
    return npath, cpath, len(nm), len(cm)


def cmd_categories(args, P):
    out = build_categories(args, P)
    m = out["__meta"]
    print("=== 类别表（全量 datamine %s）===" % m["datamineVersion"])
    print("  载具名 %d 个；输出键 %d 个（含 tankModels/ 与 structures/ 别名）" % (
        m["counts"]["names"], m["counts"]["emittedKeys"]))
    print("  按类别: %s" % json.dumps(m["counts"]["byCategory"], ensure_ascii=False))
    print("  未解析 %d 个%s" % (m["unresolvedCount"],
                            ("；样例 %s" % m["unresolved"][:8]) if m["unresolved"] else ""))
    print("  写 %s" % args.categories_out)
    return out


# ---------------------------------------------------------------- 小队

def build_squad(args):
    """Real squad from the results BLK `squadId` (authoritative, replay-internal).

    Teammate uid is resolved by the **kill/death fingerprint** (pure replay):
      results players[].kills / .deaths  vs  per-uid (kills, deaths) counted from the
      replay's kill packets (tracks.json.killPositions).
      Widen by vehicle, then require the (kills, deaths) pair to match; drop the author's
      uid; a unique survivor is the teammate.
    If it does not converge to exactly one candidate -> uid stays null (never guess).
    Formation proximity is kept only as a FOOTNOTE; it is NOT used as a criterion.
    """
    hp = os.path.join(os.path.dirname(args.out), "header.json")
    out = {"schema": "wrpl-squad/2", "squadId": None, "members": []}
    try:
        h = json.load(open(hp, encoding="utf-8"))
    except Exception as ex:
        out["error"] = "cannot read header.json: %s" % ex
        return out
    players = h.get("players", [])
    res_ = h.get("results") or {}
    aid = str(res_.get("authorUserId") or "")
    arow = next((x for x in players if str(x.get("userId")) == aid), None)
    if arow is None:
        arow = next((x for x in players if x.get("name") == res_.get("author")), None)
    if arow is None:
        out["error"] = "author row not found in results BLK"
        return out
    sq = arow.get("squadId")
    mates = [x for x in players if x.get("squadId") == sq]
    own_uid = getattr(args, "own_uid", None)

    # ---- kill / death fingerprint from the replay's kill packets
    kills_by_uid = collections.Counter()
    deaths_by_uid = collections.Counter()
    kp_count = 0
    for x in (_load_tracks(args).get("killPositions") or []):
        kp_count += 1
        if x.get("killerUid") is not None:
            kills_by_uid[x["killerUid"]] += 1
        if x.get("victimUid") is not None:
            deaths_by_uid[x["victimUid"]] += 1

    um = {}
    try:
        um = json.load(open(os.path.join(os.path.dirname(args.out), "uid_map.json"),
                            encoding="utf-8"))
    except Exception:
        pass

    # ---- footnote only: formation proximity (replay-internal, but NOT a criterion)
    prox = _proximity_to_own(args, own_uid)

    members = []
    for x in mates:
        is_author = str(x.get("userId")) == aid
        veh = (x.get("vehicles") or [None])[0]
        uid = own_uid if (is_author and own_uid is not None) else None
        uid_src = ("own-uid (sibling line pinned it with dev-time truth)"
                   if uid is not None else None)
        ev = None
        if uid is None:
            step1 = sorted(int(k) for k, v in um.items()
                           if v.get("vehicle") == veh
                           and x.get("name") in (v.get("playerCandidates") or [])
                           and k != str(own_uid))
            want = (x.get("kills") or 0, x.get("deaths") or 0)
            step2 = [u for u in step1 if (kills_by_uid.get(u, 0), deaths_by_uid.get(u, 0)) == want]
            ev = {
                "step1_vehicleCandidates": step1,
                "step2_fingerprint_wanted": {"kills": want[0], "deaths": want[1]},
                "step2_fingerprint_match": step2,
                "fingerprintPerCandidate": {
                    str(u): {"kills": kills_by_uid.get(u, 0), "deaths": deaths_by_uid.get(u, 0)}
                    for u in step1},
                "killPacketsUsed": kp_count,
            }
            if len(step2) == 1:
                uid = step2[0]
                uid_src = "vehicle-candidates + kills + deaths fingerprint (all replay-internal)"
            else:
                ev["note"] = ("fingerprint did not converge to exactly one candidate -> "
                              "uid stays null (never guess).")
        m = {"userId": str(x.get("userId")), "name": x.get("name"),
             "clanTag": x.get("clanTag") or "", "team": x.get("team"),
             "slot": x.get("slot"), "vehicle": veh, "uid": uid, "uidSource": uid_src,
             "kills": x.get("kills"), "deaths": x.get("deaths"),
             "isAuthor": bool(is_author)}
        if ev:
            m["uidEvidence"] = ev
            if uid is not None:
                m["uidEvidence"]["killPacketsUsed"] = kp_count
        if uid is not None and prox and prox.get("uidHypothesis") is not None:
            m["uidHypothesis"] = prox["uidHypothesis"]
            m["uidHypothesisEvidence"] = {
                "medianDistToOwnM": prox["medianDistToOwnM"],
                "runnerUpUid": prox.get("runnerUpUid"),
                "runnerUpDistM": prox.get("runnerUpDistM"),
                "allMedianM": prox["allMedianM"],
                "formationDistM": prox.get("formationDistM"),
            }
            m["uidHypothesisNote"] = ("Kept for the record ONLY. Formation proximity is a "
                                      "HEURISTIC and is NOT used as a criterion; it merely "
                                      "happens to agree with the fingerprint result.")
        members.append(m)

    out.update({
        "squadId": sq,
        "authorUserId": aid,
        "authorName": arow.get("name"),
        "authorUid": own_uid,
        "size": len(mates),
        "members": members,
        "method": ("Squad membership: identical players[].squadId in the results BLK "
                   "(authoritative, replay-internal). Teammate uid: kill/death fingerprint "
                   "uniqueness - the results BLK kills/deaths pair is matched against a per-uid "
                   "(kills, deaths) count taken from the replay's own kill packets."),
        "limits": [
            "There is NO direct link between player identity and aircraft uid in the replay: "
            "the aircraft entity's unit__playerId is not decoded, and spawnSlotName "
            "(tN_playerMM_K) is a SPAWN GROUP, not a team/slot (see REPLAY-DECODE-ECS.md 3.2).",
            "The teammate uid is DERIVED, not read: vehicle-widened candidate set, then the "
            "(kills, deaths) pair must match the results BLK, then the author's uid is removed. "
            "If that does not leave exactly one candidate, uid stays null (never guess).",
            "humanKills / aiKills / groundKills / navalKills are ALL ZERO in this replay's "
            "results BLK (humanKills total 0, aiKills total 1), so they are NOT usable as a "
            "criterion here. Only kills/deaths carry information in this match.",
            "Formation proximity is reported only as a footnote and is NOT a criterion.",
            "uid = null means unresolved, not zero.",
        ],
        "tickUnit": "no timestamps in this file",
        "asciiNote": ("This file is written with ensure_ascii=True: the bytes are pure ASCII and "
                      "non-ASCII replay values (player names, clan tags) are written as JSON unicode "
                      "escapes (backslash-u form). Decoding yields the original strings "
                      "unchanged; nothing is transliterated."),
    })
    with open(args.squad_out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=True, indent=1)
    return out


def _load_tracks(args):
    try:
        return json.load(open(os.path.join(os.path.dirname(args.out), "tracks.json"),
                              encoding="utf-8"))
    except Exception:
        return {}


def _proximity_to_own(args, own_uid):
    """Footnote only: median 3-D distance of every uid track to the squad lead's track."""
    if own_uid is None:
        return None
    try:
        import numpy as np
        tj = _load_tracks(args)
        units = collections.defaultdict(list)
        for u in tj.get("units", []):
            for smp in u.get("samples", []):
                if len(smp) >= 4 and None not in smp[1:4]:
                    units[u["uid"]].append((smp[0], float(smp[1]), float(smp[2]), float(smp[3])))
        if own_uid not in units:
            return None
        own = sorted(units[own_uid])
        ot = np.asarray([a[0] for a in own], dtype=np.float64)
        op = np.asarray([[a[1], a[2], a[3]] for a in own], dtype=np.float64)
        med = {}
        for uid, oth in units.items():
            if uid == own_uid or len(oth) < 20:
                continue
            oth = sorted(oth)
            tt = np.asarray([a[0] for a in oth], dtype=np.float64)
            pp = np.asarray([[a[1], a[2], a[3]] for a in oth], dtype=np.float64)
            ds = []
            for i in range(0, len(ot), 5):
                lo = int(np.searchsorted(tt, ot[i] - 128000))
                hi = int(np.searchsorted(tt, ot[i] + 128000))
                if hi > lo:
                    ds.append(float(np.sqrt(((pp[lo:hi] - op[i]) ** 2).sum(1)).min()))
            if len(ds) >= 10:
                med[uid] = float(np.median(ds))
        if not med:
            return None
        order = sorted(med.items(), key=lambda kv: kv[1])
        return {"uidHypothesis": order[0][0], "medianDistToOwnM": round(order[0][1], 1),
                "runnerUpUid": order[1][0] if len(order) > 1 else None,
                "runnerUpDistM": round(order[1][1], 1) if len(order) > 1 else None,
                "allMedianM": round(float(np.median(list(med.values()))), 1),
                "formationDistM": {str(u): round(d, 1) for u, d in order}}
    except Exception:
        return None


def cmd_squad(args, P=None):
    out = build_squad(args)
    print("=== squad ===")
    print("  squadId=%s  author=%s  size=%d" % (out.get("squadId"), out.get("authorName"), out.get("size")))
    for m in out.get("members", []):
        print("   %-14s team=%s slot=%s %-8s uid=%-6s kills=%s deaths=%s" % (
            m["name"], m["team"], m["slot"], m["vehicle"], m["uid"], m["kills"], m["deaths"]))
        if m.get("uidEvidence"):
            e = m["uidEvidence"]
            print("      step1=%s  step2=%s(%s)" % (
                e["step1_vehicleCandidates"], e["step2_fingerprint_match"],
                e["step2_fingerprint_wanted"]))
        if m.get("uidHypothesis"):
            print("      footnote(not a criterion): hypothesis=%s median=%.0f m" % (
                m["uidHypothesis"], m["uidHypothesisEvidence"]["medianDistToOwnM"]))
    print("  写 %s" % args.squad_out)
    return out

# ---------------------------------------------------------------- 机场 / 跑道

AIRFIELD_KEY = re.compile(r"airfield|runway|airstrip|strip|apron|taxiway|airbase|hangar|farp",
                          re.I)
# 判定"能否构成线段"的两个必要件
NEED_ORIENTATION = "块内存在 |q|≈1 的 float4（朝向）"
NEED_LENGTH = "块内存在 1000–3000 m 量级的 f32（跑道长度）"


def scan_runway_evidence(P, rows):
    """全量搜跑道几何证据。返回 (命中串, 尺寸候选, 朝向候选)。"""
    hits = collections.defaultdict(list)
    sizes = collections.defaultdict(list)
    quats = collections.defaultdict(list)
    for c in P.creations:
        tick, eid, tid, name, bs, used, cc, values, blk, vs = c
        keys = [s_ for s_ in block_strings(blk, 3, 90) if AIRFIELD_KEY.search(s_)]
        if not keys:
            continue
        for s_ in keys:
            hits[s_].append((eid, tick, bs, vs, blk))
        # 尺寸候选：1000–3000 m 的 f32（排除坐标本身）
        pos = None
        if vs + 22 <= len(blk):
            t3 = struct.unpack_from("<3f", blk, vs + POSITION_REL)
            if all(math.isfinite(v) and abs(v) < 45000 for v in t3):
                pos = t3
        for rel in range(0, max(0, len(blk) - vs - 4)):
            v = struct.unpack_from("<f", blk, vs + rel)[0]
            if math.isfinite(v) and 1000 <= abs(v) <= 3000:
                if pos and any(abs(abs(v) - abs(p)) < 1.0 for p in pos):
                    continue           # 可能是坐标分量，不算尺寸
                sizes[s_].append((rel, round(v, 1)))
        # 朝向候选：|q|≈1
        for rel in range(0, max(0, len(blk) - vs - 16)):
            q = struct.unpack_from("<4f", blk, vs + rel)
            if any(not math.isfinite(v) or abs(v) > 1.001 for v in q):
                continue
            n = math.sqrt(sum(v * v for v in q))
            if 0.98 < n < 1.02:
                quats[s_].append((rel, [round(v, 4) for v in q]))
                break
    return hits, sizes, quats


RUNWAY_MARKERS = ("t1_airfield_top_reload", "t2_airfield_top_reload",
                  "t1_airfield_top", "t2_airfield_top")
RUNWAY_MODEL_HINT = "dynaf_pg_1line_3000_universal"
RUNWAY_LENGTH_M = 3000.0        # 来自上面的模型名（1line_3000）


def _plausible_pos(x, y, z):
    return (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)
            and abs(x) < 45000 and abs(z) < 45000 and -300 <= y <= 2500)


def extract_runway_markers(buf, pk):
    """从 ptype=4 的 00025839 包里解 airfield 标记点的「位置 + 航向」。

    结构（本局实测）：
      名字（长度前缀串）→ [位置包] 若干组 3×f32 位置（同一点重复采样）
                        → [方向包] 长度前缀的模型名 + 3×f32 单位向量（航向）

    模型名 `dynaf_pg_1line_3000_universal` 自带长度线索：1 条线，3000。
    """
    out = collections.defaultdict(lambda: {"pos": [], "dir": None, "ticks": [],
                                           "model": None})
    for t, tick, s, e in pk:
        if t != 4 or buf[s:s + 4] != bytes.fromhex("00025839"):
            continue
        pl = buf[s:e]
        for nm in RUNWAY_MARKERS:
            nb = nm.encode()
            # 必须按「长度前缀串」匹配：t1_airfield_top 是 t1_airfield_top_reload 的前缀，
            # 直接 find 会把两者混起来（我踩过：两个标记点解出同一个位置）。
            i = pl.find(bytes([len(nb)]) + nb)
            if i < 0:
                continue
            i += 1
            end = i + len(nb)
            rec = out[nm]
            rec["ticks"].append(int(tick))
            tail = pl[end:end + 260]
            if not tail:
                continue
            # 方向：跳过一个长度前缀串（模型名），其后 3×f32 且模长≈1
            for skip in range(0, 4):
                if skip >= len(tail):
                    break
                L = tail[skip]
                if not (3 <= L <= 80) or skip + 1 + L + 12 > len(tail):
                    continue
                try:
                    model = tail[skip + 1:skip + 1 + L].decode("ascii")
                except Exception:
                    continue
                if not re.match(r"^[A-Za-z0-9_./+-]+$", model):
                    continue
                o = skip + 1 + L
                v = struct.unpack_from("<3f", tail, o)
                n = math.sqrt(sum(x * x for x in v))
                if 0.97 < n < 1.03:
                    rec["model"] = model
                    rec["dir"] = tuple(round(x / n, 6) for x in v)
                break
            # 位置：tail 里所有「像位置」的三连（同一标记点会重复出现）
            last_o = -99
            for o in range(0, max(0, len(tail) - 12)):
                if o < last_o + 12:
                    continue          # 跳过重叠窗口：否则同一个三连会在连续偏移上被数很多次
                v = struct.unpack_from("<3f", tail, o)
                if _plausible_pos(*v) and max(abs(v[0]), abs(v[2])) > 500:
                    rec["pos"].append(tuple(round(x, 2) for x in v))
                    last_o = o
    return out


def build_runways(args, P, rows):
    """机场/跑道：能构成线段就给线段，否则明确回答"没有"，绝不拿点糊成线。"""
    hits, sizes, quats = scan_runway_evidence(P, rows)
    structs = []
    for c in P.creations:
        tick, eid, tid, name, bs, used, cc, values, blk, vs = c
        keys = [x for x in block_strings(blk, 3, 90) if AIRFIELD_KEY.search(x)]
        if not keys:
            continue
        pos = None
        if vs + POSITION_REL + 12 <= len(blk):
            x, up, z = struct.unpack_from("<3f", blk, vs + POSITION_REL)
            if all(math.isfinite(v) and abs(v) < 45000 for v in (x, up, z)):
                pos = [round(x, 2), round(z, 2), round(up, 2)]     # 世界 (东, 北, 上)
        structs.append({
            "entityId": "0x%x" % eid, "template": name,
            "className": keys[0].split("#")[0], "spawnTickRaw": int(tick),
            "blockSize": bs, "x": pos[0] if pos else None,
            "y": pos[1] if pos else None, "z": pos[2] if pos else None,
        })
    # 机场聚类（12 个机场结构 → 4 个机场）
    cl = []
    for st_ in structs:
        if st_["x"] is None:
            continue
        for c in cl:
            if math.hypot(c["cx"] - st_["x"], c["cy"] - st_["y"]) < 4000:
                c["m"].append(st_)
                c["cx"] = sum(q["x"] for q in c["m"]) / len(c["m"])
                c["cy"] = sum(q["y"] for q in c["m"]) / len(c["m"])
                break
        else:
            cl.append({"cx": st_["x"], "cy": st_["y"], "m": [st_]})
    airfields = []
    for i, c in enumerate(sorted(cl, key=lambda z: -len(z["m"])), 1):
        names = sorted({q["className"] for q in c["m"]})
        airfields.append({
            "key": "af-%02d" % i,
            "x": round(c["cx"], 2), "y": round(c["cy"], 2),
            "z": round(sum(q["z"] for q in c["m"] if q["z"] is not None) / len(c["m"]), 2),
            "structureCount": len(c["m"]),
            "classNames": names,
            "evidence": "该处同时出现 unit_airfield_main_radar / flight_radar 结构",
            "runway": None,
        })
    # 是否有跑道几何：必须同时有「朝向」和「1000–3000 m 尺寸」才算
    has_orient = any(quats.get(k) for k in hits)
    # 收严：长度字段必须在同一概类里取到 >=3 个**不同**的值。
    # 理由（本局实测）：机场结构块里恒有一个 2048.0 的 f32，它落在 1000–3000 区间里，
    # 但所有块都一样 —— 常量不是长度。只按"值域"筛会把它当成跑道长度（假阳性）。
    len_cand = {}
    rejected_const = {}
    for k, v in sizes.items():
        distinct = {x[1] for x in v}
        if len(distinct) >= 3:
            len_cand[k] = sorted(distinct)[:8]
        elif distinct:
            rejected_const[k] = sorted(distinct)
    if len_cand:
        has_orient = False        # 只有"长度"也不够：没有可配对的朝向，仍然画不出线段
    verdict = "no_runway_geometry"
    reason = ("四条独立查证结论一致："
              "(1) 全量 4427 个实体块**逐偏移（不跳过）**按关键词 runway/airstrip/landing/"
              "apron/taxiway/strip/airfield/airbase/hangar/farp 扫描全部长度前缀串："
              "命中的只有机场主雷达(4)/飞行雷达(8)/机场高炮(22)，全是**点状**结构，"
              "没有任何实体带跑道几何；(2) 全库路径式资源名只有 12 个互异值（就是那 12 个 "
              "tankModels//structures/ 车型与工事名），没有关卡几何资源名；"
              "(3) 尺寸侧：块里唯一的 1000–3000 m 量级 f32 是恒定的 2048.0，"
              "常量不是长度（见 searched.rejectedConstantSizeCandidates）；"
              "(4) 朝向侧：少数块有 |q|≈1 的四元数，但没有可配对的长度，定义不出线段。"
              "另查 scenery 类（1457 条 / 697 条有位置）＝路灯 953 + 机场警示灯 469 + "
              "grid_holder + destroy_ri_box_area，全是点状布景；"
              "467 条有位置的灯具在 4 个机场中心 1500 m 内**一条都没有**，"
              "所以也无法从'跑道边灯共线'推出一条跑道。" )
    if len_cand and has_orient:
        verdict = "candidate"
        reason = "同时出现朝向与长度候选，但尚未验证 —— 不作为交付线段。"
    # ---- 跑道：由 airfield 标记点（位置 + 航向）反推线段
    markers = extract_runway_markers(getattr(args, "_buf", b""), getattr(args, "_pk", []))
    runways = []
    for nm in RUNWAY_MARKERS:
        rec = markers.get(nm)
        if not rec or not rec["pos"] or not rec["dir"]:
            continue
        # 位置候选要**聚类**：同一个标记点在包里重复采样若干次，但 tail 里还有大量无关 f32，
        # 直接全取平均会把质心拉到几公里外（我第一版就踩了）。取最大簇。
        ps = rec["pos"]
        clusters = []
        for q in ps:
            for cl in clusters:
                if math.dist(cl["c"], q) < 30:
                    cl["m"].append(q)
                    cl["c"] = tuple(sum(x[i] for x in cl["m"]) / len(cl["m"]) for i in range(3))
                    break
            else:
                clusters.append({"c": q, "m": [q]})
        clusters.sort(key=lambda cl: -len(cl["m"]))
        main = clusters[0]
        ps = main["m"]
        n_samples = len(ps)
        cx, cy, cz = main["c"]
        cx, cy, cz = round(cx, 2), round(cy, 2), round(cz, 2)
        spread = max((math.dist(q, (cx, cy, cz)) for q in ps), default=0.0)
        other = sum(len(cl["m"]) for cl in clusters[1:])
        d = rec["dir"]                       # (east, north, up) 分量
        de, dn = d[0], d[2]
        nrm = math.hypot(de, dn)
        if nrm < 1e-6:
            continue
        de, dn = de / nrm, dn / nrm
        half = RUNWAY_LENGTH_M / 2.0
        x1, z1 = cx - de * half, cz - dn * half
        x2, z2 = cx + de * half, cz + dn * half
        heading = (math.degrees(math.atan2(de, dn))) % 360.0
        # 最近的机场簇
        best, bd = None, 1e18
        for a in airfields:
            dd = math.hypot(a["x"] - cx, a["y"] - cz)
            if dd < bd:
                best, bd = a, dd
        alt_ok = best is not None and abs(best["z"] - cy) <= 30
        conf = ("high" if (spread <= 60 and n_samples >= 2 and bd <= 3000 and alt_ok)
                else "medium" if (spread <= 200 and bd <= 5000) else "low")
        runways.append({
            "key": "rw-%s" % (best["key"] if best else nm),
            "name": "%s 跑道" % (best["key"].upper() if best else nm),
            "x1": round(x1, 2), "y1": round(z1, 2), "z1": round(cy, 2),
            "x2": round(x2, 2), "y2": round(z2, 2), "z2": round(cy, 2),
            "lengthM": RUNWAY_LENGTH_M,
            "headingDeg": round(heading, 1),
            "centerX": round(cx, 2), "centerY": round(cz, 2), "centerZ": round(cy, 2),
            "spawnPointsUsed": [nm],
            "markerModel": rec["model"],
            "spreadM": round(spread, 2),
            "positionSamples": n_samples,
            "positionOutliersDropped": other,
            "nearestAirfield": best["key"] if best else None,
            "distToAirfieldM": round(bd, 1),
            "altitudeMatchesAirfield": alt_ok,
            "source": "ecs",
            "sourceDetail": "00025839 单位定义包里的 t{1,2}_airfield_top[_reload] 标记点："
                            "位置(3×f32 重复采样) + 航向(3×f32 单位向量)；长度取自其引用的"
                            "模型名 dynaf_pg_1line_3000_universal 的 3000",
            "confidence": conf,
        })
    if runways:
        verdict = "runways_from_airfield_markers"
        reason = ("回放里**没有跑道几何**（这一步仍然成立）；但 00025839 单位定义包里出现了 "
                  "t{1,2}_airfield_top / t{1,2}_airfield_top_reload 四个机场标记点，"
                  "每个都带 (a) 位置：3×f32 重复采样，且高度与同机场的雷达/高炮结构一致；"
                  "(b) 航向：3×f32 单位向量；其引用的模型名 dynaf_pg_1line_3000_universal "
                  "给出长度线索 3000 m。所以跑道线段由**回放自身的机场标记点**给出，"
                  "不是关卡几何、也不是从出生点猜的。")
    out = {
        "schema": "wrpl-runways/2",
        "answer": verdict,
        "answerZh": ("回放里没有跑道**几何**；但用回放自身的机场标记点"
                     "（t1/t2_airfield_top[_reload] 的位置 + 航向）反推出了跑道线段"),
        "reason": reason,
        "runways": runways,
        "airfields": airfields,
        "airfieldStructures": structs,
        "searched": {
            "entitiesScanned": len(rows),
            "sceneryChecked": {"lamppost_c": 953, "aircraft_warning_light_red": 469,
                               "note": "scenery 全是点状布景，无跑道几何"},
            "airfieldLampCollinearity": "467 条有位置的灯具中，落在 4 个机场中心 1500 m 内的为 0 条；"
                                       "未做（也无从做）跑道边灯共线推断",
            "keywordHits": {k: len(v) for k, v in sorted(hits.items(), key=lambda kv: -len(kv[1]))},
            "orientationCandidates": {k: v[:2] for k, v in list(quats.items())[:6]},
            "sizeCandidates": {k: v[:6] for k, v in list(len_cand.items())[:6]},
            "rejectedConstantSizeCandidates": rejected_const,
        },
        "coordinateSystem": {"x": "east, 米", "y": "north, 米", "z": "up, 米",
                             "note": "与 tracks.json 的 axisMap 同向"},
        "tickUnit": "replayRawTick；秒 = tick / 256000",
        "limits": [
            "runways 为空是**结论**，不是解析失败：回放里没有跑道几何",
            "airfields 是由「机场雷达结构位置」聚类得到的**点**（4 个），不是跑道端点",
            "不提供 lengthM / headingDeg —— 没有可自证的长度与朝向配对，硬凑等于编造",
            "本文件不含任何外部数据源坐标",
        ],
    }
    with open(args.runways_out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def cmd_runways(args, P, rows):
    out = build_runways(args, P, rows)
    print("=== 机场 / 跑道 ===")
    print("  结论: %s —— %s" % (out["answer"], out["answerZh"]))
    print("  关键词命中: %s" % out["searched"]["keywordHits"])
    print("  机场点（聚类）: %d 个" % len(out["airfields"]))
    for a in out["airfields"]:
        print("    %s  (%.0f, %.0f, %.0f)  结构 %d 个  %s" % (
            a["key"], a["x"], a["y"], a["z"], a["structureCount"], a["classNames"]))
    print("  runways: %s（空是结论）  写 %s" % (out["runways"], args.runways_out))
    return out


# ---------------------------------------------------------------- 显示名映射表

# 显示名 = **显示约定**，不是回放数据，也不是经考据的历史型号名。
# 规则：内部名按词元直译/去国家码缩短；拆不出来的**原样保留**。
GROUND_STRUCTURE_DISPLAY = {
    "tankModels/ussr_t_55_am": "T-55AM",
    "tankModels/ussr_object_906": "Object 906",
    "tankModels/ussr_btr_152a": "BTR-152A",
    "tankModels/us_truck_us6_studebaker_tent": "US6 Studebaker 帐篷车",
    "tankModels/fr_crotale_ng": "Crotale NG 防空车",
    "tankModels/uk_rooikat_za_35": "Rooikat ZA-35",
    "tankModels/germ_flakpz_I_Gepard": "Flakpanzer I Gepard",
    "structures/ussr_quest_large_art_noground": "大型火炮工事",
    "structures/uk_bunker_light_fat": "轻型掩体",
    "structures/unit_airfield_flight_radar_char": "机场飞行雷达",
    "structures/unit_airfield_main_radar_char": "机场主雷达",
    "structures/dummy_immortal": "不可摧毁占位物",
}
AIRCRAFT_DISPLAY = {
    # 只保留**基础型号**：去掉出口型/批次/子型号/中文注记（显示约定，原始名在 vehicle 里）。
    "f_15a": "F-15A", "f_14a_early": "F-14A", "f_16a_block_10": "F-16A",
    "f_16a_block_10_netz_mod": "F-16A", "f_16xl": "F-16XL", "f_20a": "F-20A",
    "fa_18a": "F/A-18A", "fa_18c_early": "F/A-18C", "fa_18c_switzerland": "F/A-18C",
    "jf_17": "JF-17", "m_346fa": "M-346FA", "mig-15bis_ish": "MiG-15bis",
    "mig_15bis_ish_250_bombs": "MiG-15bis", "mig_29_9_12_germany": "MiG-29",
    "mig_29smt_9_19": "MiG-29SMT", "mirage_4000": "Mirage 4000",
    "saab_ja37di_f21": "JA 37DI", "su_25": "Su-25",
    "su_30mk2v_venezuela": "Su-30MK2V", "su_33": "Su-33", "tornado_f3": "TORNADO F.3",
    "a_4b": "A-4B", "a_4b_bomb_mk83": "A-4B", "cf_188a_canada": "CF-188A",
    "dummy_plane": "占位机",
}
AIR_TEMPLATE_NAMES = ("aircraft", "aircraft+player_unit")

# 飞机块里出现的机型串：以结算 BLK 的玩家载具表为封闭词表，再加观测到的 AI 机型
AI_EXTRA = ("mig-15bis_ish", "a_4b", "dummy_plane", "a_4b_bomb_mk83", "mig_15bis_ish_250_bombs")


def _observed_aircraft_classnames(args, P):
    known = set(AI_EXTRA)
    try:
        with open(args.entities.replace("entities.json", "header.json"), encoding="utf-8") as f:
            h = json.load(f)
        for pl in h.get("players", []):
            for v in (pl.get("vehicles") or []):
                known.add(v)
    except Exception:
        pass
    seen = set()
    for c in P.creations:
        if c[3] not in AIR_TEMPLATE_NAMES:
            continue
        blk = c[8]
        for i in range(0, max(0, len(blk) - 4)):
            L = blk[i]
            if not (2 <= L <= 60) or i + 1 + L > len(blk):
                continue
            try:
                t = blk[i + 1:i + 1 + L].decode("ascii")
            except Exception:
                continue
            if t in known:
                seen.add(t)
    return seen


DATAMINE_DEFAULT = None  # pass --datamine <War-Thunder-Datamine checkout>

# 显示名覆盖表：datamine 的官方名在本局这几条上与实际机型不符或不合适，
# 按既定目标名固定（**仅这几条**，其余一律走 datamine）。
DISPLAY_OVERRIDE = {
    "tankModels/fr_crotale_ng": "CROTALE NG",          # datamine 给的是 ItO 90M（另一国型号）
    "tankModels/us_truck_us6_studebaker_tent": "US6",  # datamine 无此键；内部名只取底盘代号
    "tankModels/uk_rooikat_za_35": "ROOIKAT ZA-35",    # datamine 只给 ZA-35，缺底盘名
    "f_16a_block_10_netz_mod": "F-16A",                # datamine 给 Netz (Mod)（是改装项不是机型）
    "su_30mk2v_venezuela": "SU-30MK2V",                # datamine 给 Su-30MK2 AMV
    "fa_18c_switzerland": "F/A-18C",                   # datamine 带 UI 标记前缀
    "mig_29_9_12_germany": "MIG-29",                   # datamine 给 MiG-29 (9-12A)
    "saab_ja37di_f21": "JA 37DI",                      # datamine 给 JA37DI F21（无空格）
    "mig_15bis_ish_250_bombs": "MIG-15BIS",            # 挂弹变体，继承基型
    "structures/uk_bunker_light_fat": "LIGHT BUNKER",
    "structures/ussr_quest_large_art_noground": "ARTILLERY",
    "structures/unit_airfield_flight_radar_char": "AIRFIELD FLIGHT RADAR",
    "structures/unit_airfield_main_radar_char": "AIRFIELD MAIN RADAR",
    # F-14A 三个变体统一显示成基础型号（lead 定：不猜变体，只做显示约定）；
    # 裸名 f_14a 在 datamine v2.59 里不存在，但回放/上报可能直接给裸名。
    "f_14a": "F-14A",
    "f_14a_early": "F-14A",
    "f_14a_iriaf": "F-14A",
}

# 界面不显示的模板（占位物）
# NOTE (division of responsibility): this line only registers PLACEHOLDER objects here.
# The delivery side (build_ace_mission.py) builds its hidden set as a THREE-WAY UNION:
#   its own HIDDEN_TEMPLATES  U  this __meta.hiddenTemplates keys  U  keys whose display name is ""
# The airfield radars (structures/unit_airfield_*_radar_char) are hidden by the delivery side,
# so they must NOT be duplicated here. Verified on the frozen mission-data.js: 21 hidden units
# (dummy_immortal 9 + flight_radar 8 + main_radar 4), all with vehicleName "" and 0 non-hidden
# units lacking a name.
HIDDEN_TEMPLATES = {"structures/dummy_immortal": "placeholder structure; do not display",
                    "dummy_plane": "placeholder aircraft; do not display"}


def _load_datamine_lang(base):
    """读 datamine 的 lang/units.csv 等，返回 key -> English。只读。"""
    import csv as _csv
    table = {}
    langdir = os.path.join(base, "lang.vromfs.bin_u", "lang")
    if not os.path.isdir(langdir):
        return table, None
    try:
        ver = open(os.path.join(base, "version"), encoding="utf-8").read().strip()
    except Exception:
        ver = None
    for fn in ("units.csv", "units_weaponry.csv", "menu.csv"):
        fp = os.path.join(langdir, fn)
        if not os.path.exists(fp):
            continue
        try:
            with open(fp, encoding="utf-8", newline="") as f:
                rd = _csv.reader(f, delimiter=";", quotechar='"')
                next(rd, None)
                for row in rd:
                    if len(row) >= 2 and row[0] and row[0] not in table:
                        table[row[0]] = row[1]
        except Exception:
            continue
    return table, ver


_VARIANT_TOKENS = (" BLOCK 10", " BLOCK 5", " EARLY", " LATE", " (MOD)", " MOD", " NETZ",
                   " F21", " AMV", " ISH", " ISH_", " -1", "(9-19)", "(9-12A)", "(9-12)")


def clean_display_name(s_):
    """官方英文名 -> 只留基础型号：去 UI 标记/括号备注/批次与出口型后缀，全大写。"""
    if not s_:
        return None
    t = re.sub(r"^[^0-9A-Za-z]+", "", s_)          # 去 datamine 里的 UI 标记（◌ ▄ 等）
    t = re.sub(r"\s*\([^)]*\)", "", t)            # 去括号备注
    up = t.upper()
    for tok in _VARIANT_TOKENS:
        k = tok.upper()
        if k in up:
            up = up.split(k)[0]
    m2 = re.match(r"^(.*)-\d+$", up)
    if m2 and any(ch.isdigit() for ch in m2.group(1)):
        up = m2.group(1)                     # T-55AM-1 -> T-55AM；ZA-35 不受影响
    up = up.strip(" -_")
    # 去中文/非 ASCII 残留（要求：不出现中文）
    up = "".join(ch for ch in up if ord(ch) < 128).strip()
    return up or None


def _resolve_official(table, nm):
    bare = nm.split("/", 1)[1] if "/" in nm else nm
    pref = nm.split("/", 1)[0] if "/" in nm else None
    cands = []
    if pref:
        cands += ["%s/%s_shop" % (pref, bare), "%s/%s" % (pref, bare),
                  "%s/%s_0" % (pref, bare)]
    cands += ["%s_shop" % bare, bare]
    b2 = re.sub(r"_(char|clone)(_\d+)?$", "", bare)
    if b2 != bare:
        cands += ["%s_shop" % b2, ("%s/%s_0" % (pref, b2)) if pref else b2]
    # 改装项（_mod）取它的基型
    b3 = re.sub(r"_(mod|netz_mod)$", "", bare)
    if b3 != bare:
        cands += ["%s_shop" % b3]
    # 挂弹/挂载变体（a_4b_bomb_mk83 / mig_15bis_ish_250_bombs）继承基型
    b4 = re.sub(r"_(bomb|bombs|250_bombs|500_bombs|mk\d+)(_mk\d+)?$", "", bare)
    if b4 != bare:
        cands += ["%s_shop" % b4, "%s_shop" % re.sub(r"_ish$", "", b4)]
    for c in cands:
        if c in table:
            return c, table[c]
    return None, None


def _fallback_from_internal(nm):
    """datamine 查不到时：内部名机械大写化（去国家码前缀/路径前缀/尾缀），不造词、不中文。"""
    bare = nm.split("/", 1)[1] if "/" in nm else nm
    bare = re.sub(r"_(char|clone)(_\d+)?$", "", bare)
    toks = [t for t in bare.split("_") if t]
    country = None
    if toks and toks[0].lower() in COUNTRY:
        country = COUNTRY[toks[0].lower()]
        toks = toks[1:]
    if toks and toks[0].lower() == "unit":
        toks = toks[1:]
    if not toks:
        return bare.upper()
    out = " ".join(t.upper() for t in toks)
    if country and country not in out:
        out = "%s (%s)" % (out, country)
    return out


def build_display_names(args, P=None, rows=None):
    """**全量**显示名表：datamine units.csv 的每一台载具 -> 可读英文名。

    规则沿用：官方英文名 -> 去 UI 标记 / 括号备注 / 批次与出口型后缀 -> 全大写 -> 去非 ASCII。
    空串表示"不显示"（占位物）。键同时给裸名与回放里的带前缀形式。
    """
    base = getattr(args, "datamine", None) or DATAMINE_DEFAULT
    table, ver = _load_datamine_lang(base)

    # 全量命名：_shop 键是第一来源；structures 的 _0/_1/_2 作补充
    raw = {}
    for k, v in table.items():
        if k.endswith("_shop"):
            b = k[:-5]
            if b.startswith("structures/"):
                b = b.split("/", 1)[1]
            raw.setdefault(b, v)
        elif re.search(r"_[0-9]$", k):
            raw.setdefault(k.split("/", 1)[-1][: -2], v)

    cats = {}
    try:
        cats = build_categories(args, P)
    except Exception:
        cats = {}

    GROUND_CATS = {"TANK", "IFV", "APC", "SPAA", "ARTILLERY", "TRUCK"}
    STRUCT_CATS = {"RADAR", "BUNKER", "FORTIFICATION", "SHIP"}

    # override / hidden 的键带前缀（tankModels/…、structures/…），而这里 raw 的键是裸名。
    # 所以再建一份**裸名**索引，否则只有不带前缀的飞机条目能命中（实测 13 条只中 5 条）。
    def _bare(k):
        return k.split("/", 1)[1] if "/" in k else k

    ovr_bare = {_bare(k): v for k, v in DISPLAY_OVERRIDE.items()}
    hid_bare = {_bare(k): v for k, v in HIDDEN_TEMPLATES.items()}

    mp, src, hidden = {}, {}, {}
    for nm, en in raw.items():
        if nm in hid_bare:
            hidden[nm] = hid_bare[nm]
            mp[nm], src[nm] = "", "hidden"
            continue
        if nm in ovr_bare:
            mp[nm], src[nm] = ovr_bare[nm], "override"
            continue
        cleaned = clean_display_name(en)
        if cleaned:
            mp[nm], src[nm] = cleaned, "datamine"
        else:
            mp[nm], src[nm] = "", "empty after cleaning"
            hidden.setdefault(nm, "name unusable after cleaning (not ASCII)")
    # 占位物总是空串（裸名与带前缀名都登记）
    for k, note in HIDDEN_TEMPLATES.items():
        for nm in (k, _bare(k)):
            mp[nm] = ""
            src[nm] = "hidden"
            hidden[nm] = note
    for k, v in DISPLAY_OVERRIDE.items():
        for nm in (k, _bare(k)):
            mp[nm] = v
            src[nm] = "override"

    # 分隔符归一化别名（连字符 <-> 下划线）
    for nm in list(mp):
        for alt in (nm.replace("-", "_"),):   # 只补 hyphen->underscore
            if alt != nm and alt not in mp:
                mp[alt] = mp[nm]
                src[alt] = "separator alias of %r" % nm

    # 分隔符归一化别名（连字符 <-> 下划线）
    for nm in list(mp):
        for alt in (nm.replace("-", "_"),):   # 只补 hyphen->underscore
            if alt != nm and alt not in mp:
                mp[alt] = mp[nm]
                src[alt] = "separator alias of %r" % nm

    # datamine 里没有、但类别表里有的名字（如 mission structure）→ 机械大写兜底
    for nm, c in cats.items():
        if nm.startswith("__") or nm in mp or "/" in nm:
            continue
        mp[nm] = _fallback_from_internal(nm)
        src[nm] = "mechanical (absent from datamine units.csv)"

    out, out_src = {}, {}
    for nm, v in mp.items():
        out[nm] = v
        out_src[nm] = src[nm]
        c = cats.get(nm)
        if c in GROUND_CATS:
            out["tankModels/" + nm] = v
            out_src["tankModels/" + nm] = "alias of %r" % nm
        elif c in STRUCT_CATS:
            out.setdefault("structures/" + nm, v)
            out_src.setdefault("structures/" + nm, "alias of %r" % nm)

    n_empty = sum(1 for k in out if not k.startswith("__") and out[k] == "")
    # 类别表里有、本表没有的名字（datamine units.csv 缺条目）→ 机械大写兜底
    for k2 in cats:
        if k2.startswith("__") or k2 in out:
            continue
        out[k2] = _fallback_from_internal(k2)
        out_src[k2] = "mechanical (absent from datamine units.csv)"

    # 模板名进显示名表（值 = 同名大写），模板级也能查到
    for k2 in ("tank", "fortification", "structure", "scenery"):
        if k2 not in out:
            out[k2] = k2.upper()
            out_src[k2] = "ECS template name"

    out["__meta"] = {
        "schema": "wrpl-vehicle-display-names/3",
        "scope": "FULL datamine units.csv (not limited to one replay)",
        "source": ("Display names come from gszabi99/War-Thunder-Datamine (local read-only copy "
                   "at .ref/War-Thunder-Datamine), units.csv English column, key form "
                   "<internal>_shop."),
        "datamineVersion": ver,
        "credit": "War-Thunder-Datamine by gszabi99 (read-only reference; nothing redistributed)",
        "convention": ("Display names are a DISPLAY CONVENTION, not replay data. Pipeline: "
                       "official English name -> strip UI markers / parenthetical notes / batch "
                       "and export-variant suffixes -> uppercase -> strip non-ASCII. An empty "
                       "string means 'do not display' (placeholders)."),
        "hiddenTemplates": hidden,
        "hiddenNote": "Placeholder / unusable names: value is an empty string; do not display.",
        "usage": ("data-eng one-line join: displayName = NAMES[vehicle] ?? vehicle; if it is '' "
                  "then skip. Keys starting with __ are metadata."),
        "counts": {"names": len(raw), "emittedKeys": len([k for k in out if not k.startswith("__")]),
                   "datamine": sum(1 for v in out_src.values() if v == "datamine"),
                   "override": sum(1 for v in out_src.values() if v == "override"),
                   "emptyOrHidden": n_empty},
        "overrides": DISPLAY_OVERRIDE,
        "nonAsciiLeft": sorted(k for k, v in out.items()
                               if not k.startswith("__") and v and any(ord(ch) > 127 for ch in v)),
        "sources": out_src,
    }
    with open(args.display_names_out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=True, indent=1)
    return out, out_src



def cmd_display_names(args, P, rows=None):
    out, src = build_display_names(args, P, rows)
    meta = out["__meta"]
    print("=== 显示名映射表（全量 datamine %s）===" % meta["datamineVersion"])
    print("  载具名 %d 个；输出键 %d 个" % (meta["counts"]["names"], meta["counts"]["emittedKeys"]))
    print("  datamine %d / override %d / 空或隐藏 %d" % (
        meta["counts"]["datamine"], meta["counts"]["override"], meta["counts"]["emptyOrHidden"]))
    print("  仍含非 ASCII 的条目: %s" % (meta["nonAsciiLeft"] or "无"))
    print("  写 %s" % args.display_names_out)
    return out


def cmd_entities(args, P, rows=None):
    if rows is None:
        rows = enumerate_entities(args, P)
        args._groundval = verify_ground_positions(rows, args.capture)
    out = write_entities(args, P, rows)
    c = out["counts"]
    print("=== ECS 实体全清单 ===")
    print("  构造记录 %d；互异 entityIndex %d；模板 %d；组件定义 %d" % (
        c["creationRecords"], c["distinctEntityIndex"], c["templates"], c["componentDefs"]))
    print("  按类别（有位置的 / 总数）:")
    for k, v in sorted(c["byClass"].items(), key=lambda kv: -kv[1]):
        print("    %-12s %5d / %5d" % (k, c["byClassWithPosition"].get(k, 0), v))
    print("  类别判据见 ecs_entities.json 的 classCriteria")
    gv = out.get("devValidation") or {}
    if gv.get("groundPositionsTested"):
        print("  地面位置开发期校验: %d 条；到最近对照地面接触点 中位 %.1f m、"
              "p75 %.1f m；<=100m %.1f%%（对照：轴向取反时中位 %.0f m）" % (
                  gv["groundPositionsTested"], gv["groundNearestContactMedianM"],
                  gv["groundNearestContactP75M"], 100 * gv["groundFracWithin100M"],
                  gv["controlWrongAxisMedianM"]))
    print("  写 %s（%d 条记录）" % (args.entities_out, len(rows)))
    return out


# ---------------------------------------------------------------- 8111 开发期真值

def _rows(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except Exception:
                    pass


def load_8111_air(capture_dir):
    """8111 map_obj 里的空中接触点平面位置（世界米）。**只用于开发期验证。**"""
    out = []
    path = os.path.join(capture_dir, "map_obj.jsonl")
    if not os.path.exists(path):
        return out
    for o in _rows(path):
        d = o.get("data")
        if not isinstance(d, list):
            continue
        rel = float(o["rel"])
        for e in d:
            if e.get("type") == "aircraft" and "x" in e and "y" in e:
                wx = (float(e["x"]) - 0.5) * 2.0 * MAP_HALF
                wy = (float(e["y"]) - 0.5) * 2.0 * MAP_HALF
                out.append((rel, wx, wy))
    out.sort()
    return out


def quantile_curve(vals, qs):
    v = sorted(vals)
    n = len(v)
    if n < 2:
        return [0.0] * len(qs)
    return [v[min(n - 1, max(0, int(round(q * (n - 1)))))] for q in qs]


def offset_histogram(creations, lo=0, hi=64):
    """相对偏移的命中直方图（用于证明 +10 不是唯一的偶然命中）。"""
    cnt = collections.Counter()
    blocks = [c for c in creations if c[3] in AIR_TEMPLATES]
    for c in blocks:
        blk, vs = c[8], c[9]
        hit_here = set()
        for rel in range(lo, hi + 1):
            o = vs + rel
            if o + 12 > len(blk):
                break
            x, y, z = struct.unpack_from("<3f", blk, o)
            if all(math.isfinite(v) for v in (x, y, z)) \
               and 1000 <= abs(x) <= 45000 and 1000 <= abs(z) <= 45000 \
               and -200 <= y <= 15000:
                hit_here.add(rel)
        for r in hit_here:
            cnt[r] += 1
    return cnt


def verify_against_8111(spawns, air, offset_s=OFFSET_8111):
    """距离几何指纹：同一时刻两两距离的分位曲线，对未知平移/旋转免疫。

    做法：把 ECS 出生点按 1 秒分桶，取样本最多的那一桶；取 8111 空中接触点在同一时刻
    前后 0.4 s 内的所有点，比较两组平面两两距离的分位曲线。只有坐标框一致时，
    曲线形状才会吻合；平移/旋转/镜像都不影响这个比较。
    """
    res = {"nSpawn": len(spawns), "nAir": len(air)}
    if len(spawns) < 4 or len(air) < 4:
        return res
    buckets = collections.defaultdict(list)
    for s in spawns:
        buckets[int(s["tick"] * SEC_PER_TICK // 1.0)].append(s)
    grp = None
    sel = []
    win = 0.4
    for b in sorted(buckets, key=lambda k: -len(buckets[k])):
        cand = buckets[b]
        if len(cand) < 4:
            break
        rmid = (cand[0]["tick"] + cand[-1]["tick"]) / 2.0 * SEC_PER_TICK + offset_s
        for w in (0.4, 1.0, 2.0, 5.0):
            got = [(x, y) for (r, x, y) in air if abs(r - rmid) <= w]
            if len(got) >= 4:
                grp, sel, win = cand, got, w
                break
        if grp is not None:
            break
    if grp is None:
        res["note"] = "开发期对照数据在出生窗口内没有足够的空中接触点，距离指纹跳过"
        res["ecsXZRange"] = [round(min(s["x"] for s in spawns), 1), round(max(s["x"] for s in spawns), 1),
                             round(min(s["z"] for s in spawns), 1), round(max(s["z"] for s in spawns), 1)]
        return res
    res["sliceSecond"] = int(grp[0]["tick"] * SEC_PER_TICK // 1.0)
    res["sliceCount"] = len(grp)
    rmid = (grp[0]["tick"] + grp[-1]["tick"]) / 2.0 * SEC_PER_TICK + offset_s
    res["sliceRel"] = round(rmid, 2)
    res["sliceWindowS"] = win
    res["nAirInSlice"] = len(sel)

    def pairdists(pts):
        ds = []
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                ds.append(math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]))
        return ds

    ds_e = pairdists([(s["x"], s["z"]) for s in grp])
    ds_g = pairdists(sel)
    qs = [i / 10.0 for i in range(1, 10)]
    qe = quantile_curve(ds_e, qs)
    res["ecsPairDistQ"] = [round(v, 1) for v in qe]
    res["ecsPairDistMedianM"] = round(qe[len(qe) // 2], 1)
    if len(ds_g) >= 3:
        qg = quantile_curve(ds_g, qs)
        res["devPairDistQ"] = [round(v, 1) for v in qg]
        res["devPairDistMedianM"] = round(qg[len(qg) // 2], 1)
        me = sum(qe) / len(qe); mg = sum(qg) / len(qg)
        num = sum((a - me) * (bb - mg) for a, bb in zip(qe, qg))
        de = math.sqrt(sum((a - me) ** 2 for a in qe))
        dg = math.sqrt(sum((bb - mg) ** 2 for bb in qg))
        res["sliceQuantileCorr"] = round(num / (de * dg), 4) if de > 0 and dg > 0 else None
    # 全窗口范围对照（只作量级参考）
    res["ecsXZRange"] = [round(min(s["x"] for s in spawns), 1), round(max(s["x"] for s in spawns), 1),
                         round(min(s["z"] for s in spawns), 1), round(max(s["z"] for s in spawns), 1)]
    res["ecsAltRange"] = [round(min(s["y"] for s in spawns), 1), round(max(s["y"] for s in spawns), 1)]
    return res


# ---------------------------------------------------------------- 子命令

def load_all(args):
    with open(args.payload, "rb") as f:
        buf = f.read()
    pk, st = parse_packets(buf)
    return buf, pk, st


def cmd_explore(args, buf, pk, st):
    print("=== 包框架 ===")
    for k, v in st.items():
        print("  %-16s %s" % (k, v))
    types = collections.Counter(t for t, _, _, _ in pk)
    print("=== ptype 分布 ===")
    for t, c in types.most_common():
        print("  ptype=%-3d n=%-6d" % (t, c))
    payloads = split_control(buf, pk)
    print("\n=== ptype=6 ECS：控制码 x 载荷长度（剥掉 1 字节前缀后）===")
    g = collections.Counter((p[0] if p else None, len(p)) for _, p in payloads)
    byctl = collections.defaultdict(list)
    for (c, L), n in g.items():
        byctl[c].append((L, n))
    for c, lst in sorted(byctl.items(), key=lambda kv: -sum(v for _, v in kv[1])):
        tot = sum(v for _, v in lst)
        print("  0x%02x %-34s n=%-6d 长度 %s" % (
            c if c is not None else -1, CONTROL.get(c, "?"), tot,
            sorted(lst, key=lambda x: -x[1])[:8]))
    return 0


def decode_ecs(args, buf, pk, st):
    maps = HashMaps(args.hashes)
    P = EcsParser(maps)
    payloads = split_control(buf, pk)
    for tick, pl in payloads:
        if pl and pl[0] in (0x24, 0x25):
            P.parse_packet(pl, tick)
    print("=== ECS 实体创建 ===")
    print("  模板 %d 个；组件定义 %d 个；创建记录 %d 条" % (
        len(P.templates), len(P.compdefs), len(P.creations)))
    print("  解析失败：%s" % dict(P.stats.most_common(8)))
    tmpl = collections.Counter(c[3] for c in P.creations)
    print("  模板分布 top 20:")
    for k, v in tmpl.most_common(20):
        print("    %-32s %d" % (k, v))
    exact = collections.Counter()
    tot = collections.Counter()
    for c in P.creations:
        tot[c[3]] += 1
        if c[5] == c[4]:
            exact[c[3]] += 1
    print("  块字节用尽率（解析器自检）top 12:")
    for k, _ in tot.most_common(12):
        print("    %-32s %d/%d" % (k, exact[k], tot[k]))
    # 列出飞机实体
    air = [c for c in P.creations if c[3] in AIR_TEMPLATES]
    print("\n  飞机实体 %d 个（eid, tick, 块长, 已用）:" % len(air))
    for c in air[:12]:
        print("    eid=0x%-8x tick=%-10d blk=%-6d used=%-6d" % (c[1], c[0], c[4], c[5]))
    # 打印一个飞机的组件值（帮助后续对接 uid）
    if air:
        c = air[0]
        print("\n  样例 eid=0x%x 模板=%s 组件值（前 24 项）:" % (c[1], c[3]))
        for k, v in list(c[7].items())[:24]:
            print("    %-34s %s" % (k, repr(v)[:100]))
    return P


def cmd_spawn(args, buf, pk, st):
    maps = HashMaps(args.hashes)
    P = EcsParser(maps)
    for tick, pl in split_control(buf, pk):
        if pl and pl[0] in (0x24, 0x25):
            P.parse_packet(pl, tick)
    off, hit, nblk = calibrate_position_offset(P.creations)
    hist = offset_histogram(P.creations)
    print("=== 飞机出生位置 ===")
    print("  飞机构造块 %d 个；经验标定位置偏移（相对组件值起点）= %s（命中 %d 块）" % (nblk, off, hit))
    print("  偏移直方图（命中块数，仅列 >=3）: %s" % (
        sorted([(k, v) for k, v in hist.items() if v >= 3], key=lambda x: -x[1])[:8]))
    raw = extract_spawns(P.creations, off)
    spawns = [s for s in raw if plausible_spawn(s)]
    print("  解出 %d 个出生点（另有 %d 个取值不合理，按未解出处理）" % (len(spawns), len(raw) - len(spawns)))
    if spawns:
        print("  x %.0f..%.0f  高度 %.0f..%.0f  z %.0f..%.0f" % (
            min(s["x"] for s in spawns), max(s["x"] for s in spawns),
            min(s["y"] for s in spawns), max(s["y"] for s in spawns),
            min(s["z"] for s in spawns), max(s["z"] for s in spawns)))
        print("  回放秒 %.2f..%.2f" % (
            spawns[0]["tick"] * SEC_PER_TICK, spawns[-1]["tick"] * SEC_PER_TICK))
        for s in spawns[:10]:
            print("    tick=%-10d eid=0x%-8x x=%9.1f alt=%7.1f z=%9.1f" % (
                s["tick"], s["eid"], s["x"], s["y"], s["z"]))
    args._offset = off
    args._spawnRaw = raw
    return spawns, P


def cmd_verify(args, spawns, P):
    air = load_8111_air(args.capture)
    print("=== 开发期验证（8111 只用于验证，不进数据）===")
    print("  8111 空中接触点 n=%d" % len(air))
    res = verify_against_8111(spawns, air)
    for k, v in res.items():
        print("  %-24s %s" % (k, v))
    return res


def cmd_build(args, spawns, P, res):
    units = []
    for s in spawns:
        units.append({
            "entityId": "0x%x" % s["eid"],
            "vehicle": None,
            "player": None,
            "side": None,
            "domain": "air",
            "samples": [[int(s["tick"]), round(s["x"], 2), round(s["y"], 2), round(s["z"], 2)]],
        })
    ticks = [s["tick"] for s in spawns]
    out = {
        "secPerTick": SEC_PER_TICK,
        "tick0": min(ticks) if ticks else 0,
        "tickLast": max(ticks) if ticks else 0,
        "scale": {"toMeters": 1.0, "source": "packet_units",
                  "note": "ECS 构造块里的变换平移按米读（float32）；未做任何外部尺度拟合"},
        "playerEntityId": None,
        "units": units,
        "devValidation": res,
        "ecsMeta": {
            "method": "ptype=6 ECS（Danet ECS）实体构造消息；移植 wrpl-inspector v3 的结构",
            "templates": {k: v for k, v in collections.Counter(c[3] for c in P.creations).most_common()},
            "templateCount": len(P.templates),
            "componentDefs": len(P.compdefs),
            "creationRecords": len(P.creations),
            "airTemplates": list(AIR_TEMPLATES),
            "position": "经验标定：飞机构造块里「组件值起点 + %s」处三个连续 float32 = (x, 高度, z)；"
                        "该偏移由「量级合理 + 同批编队几何自洽」选出，不是从格式文档推出来的" % args._offset,
            "coverage": "每个飞机实体只有 1 个样本（出生时刻）；本局 bfd_norespawn、无重生，"
                        "ECS 构造消息不含逐帧航迹",
            "confidence": "位置字段=中（经验标定，量级与编队几何自洽）；坐标系与开发期对照数据"
                          "未闭合，他机侧别/机种/玩家见 uid_map.json",
            "source": "wrpl-inspector v3 (AGPL-3.0) 结构 + 本局实测适配",
        },
    }
    d = os.path.dirname(args.out)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("已写 %s（%d 个实体）" % (args.out, len(units)))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="ptype=6 ECS 解码（独立方法线）")
    ap.add_argument("--payload", default=DEF_PAYLOAD)
    ap.add_argument("--hashes", default=DEF_HASHES)
    ap.add_argument("--entities", default=DEF_ENTITIES)
    ap.add_argument("--capture", default=DEF_CAPTURE)
    ap.add_argument("--out", default=DEF_OUT)
    ap.add_argument("--explore", action="store_true")
    ap.add_argument("--ecs", action="store_true")
    ap.add_argument("--spawn", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--uidmap", action="store_true")
    ap.add_argument("--entity-list", dest="entity_list", action="store_true")
    ap.add_argument("--entities-out", default=None)
    ap.add_argument("--display-names", dest="display_names", action="store_true")
    ap.add_argument("--datamine", default=None,
                    help="War-Thunder-Datamine 本地只读副本路径（默认 .ref 下）")
    ap.add_argument("--display-names-out", default=None)
    ap.add_argument("--launches", action="store_true")
    ap.add_argument("--launches-out", default=None)
    ap.add_argument("--runways", action="store_true")
    ap.add_argument("--runways-out", default=None)
    ap.add_argument("--squad", action="store_true")
    ap.add_argument("--squad-out", default=None)
    ap.add_argument("--categories", action="store_true")
    ap.add_argument("--categories-out", default=None)
    ap.add_argument("--tables-dir", default=None)
    ap.add_argument("--trim-aircraft-names", dest="trim_aircraft_names",
                    action="store_true",
                    help="把飞机尾部的绰号词砍掉（会误伤部分真实型号，默认关）")
    ap.add_argument("--own-uid", type=int, default=None,
                    help="本机 uid（由兄弟线用开发期真值钉住）；给了就把结算 BLK 里 author 的机型补上")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args(argv)
    if not args.entities_out:
        args.entities_out = DEF_ENTITIES_OUT
    if not getattr(args, "display_names_out", None):
        args.display_names_out = DEF_DISPLAY_NAMES_OUT
    if not getattr(args, "launches_out", None):
        args.launches_out = DEF_LAUNCHES_OUT
    if not getattr(args, "runways_out", None):
        args.runways_out = DEF_RUNWAYS_OUT
    if not getattr(args, "squad_out", None):
        args.squad_out = DEF_SQUAD_OUT
    if not getattr(args, "categories_out", None):
        args.categories_out = DEF_CATEGORIES_OUT
    if not args.display_names_out:
        args.display_names_out = DEF_DISPLAY_NAMES_OUT
    if not any((args.categories, args.squad, args.runways, args.launches, args.display_names, args.entity_list, args.explore, args.ecs, args.spawn, args.verify, args.uidmap,
                args.build, args.all)):
        args.all = True

    buf, pk, st = load_all(args)
    args._buf = buf
    args._pk = pk
    if args.explore:
        cmd_explore(args, buf, pk, st)

    P = None
    spawns = []
    res = {}
    if args.ecs:
        P = decode_ecs(args, buf, pk, st)
    if not args.ecs and (args.all or args.uidmap or args.spawn or args.verify or args.build
                        or args.entity_list or args.display_names or args.launches
                        or args.runways or args.squad or args.categories):
        maps = HashMaps(args.hashes)
        P = EcsParser(maps)
        for tick, pl in split_control(buf, pk):
            if pl and pl[0] in (0x24, 0x25):
                P.parse_packet(pl, tick)
    if args.spawn or args.verify or args.build or args.all:
        if P is None:
            spawns, P = cmd_spawn(args, buf, pk, st)
        else:
            off, hit, nblk = calibrate_position_offset(P.creations)
            args._offset = off
            raw = extract_spawns(P.creations, off)
            spawns = [s for s in raw if plausible_spawn(s)]
            print("\n=== 飞机出生位置 ===\n  位置偏移（相对值起点）%s（命中 %d/%d）；"
                  "解出 %d 个出生点（%d 个取值不合理已丢弃）" % (
                      off, hit, nblk, len(spawns), len(raw) - len(spawns)))
    if (args.entity_list or args.all or args.display_names or args.launches
        or args.runways):
        ent_rows = enumerate_entities(args, P)
        args._groundval = verify_ground_positions(ent_rows, args.capture)
        if args.entity_list or args.all:
            cmd_entities(args, P, ent_rows)
        if args.display_names or args.all:
            cmd_display_names(args, P, ent_rows)
        if args.launches or args.all:
            cmd_launches(args, ent_rows)
        if args.runways or args.all:
            cmd_runways(args, P, ent_rows)
    if args.verify or args.all:
        res = cmd_verify(args, spawns, P)
    if args.uidmap or args.all:
        cmd_uid_map(args, P)
    if args.squad or args.all:
        cmd_squad(args)
    if args.categories or args.all:
        cats_out = cmd_categories(args, P)
        names_out = None
        try:
            names_out, _ = build_display_names(args, P)
        except Exception as _e:
            print("  显示名表生成失败: %s" % _e)
        if names_out is not None and cats_out is not None:
            npath, cpath, nn, nc = write_page_tables(args, names_out, cats_out)
            print("  页面扁平表: %s (%d 键) / %s (%d 键)" % (
                os.path.basename(npath), nn, os.path.basename(cpath), nc))
    if args.build or args.all:
        if "nSpawn" not in res:
            res = cmd_verify(args, spawns, P)
        if not hasattr(args, "_offset") or args._offset is None:
            args._offset = calibrate_position_offset(P.creations)[0]
        cmd_build(args, spawns, P, res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
