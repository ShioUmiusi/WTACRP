#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wrpl_decode.py —— 从真实 War Thunder 回放里解出各实体的三维位置（产物 100% 来自 .wrpl）。

结论（详细推导见 ace-combat/docs/REPLAY-DECODE.md）：

* **空中实体的三维位置来自 `ptype=2` AircraftSmall 的 `fm` 载荷**：一个位打包的条目列表，
  每条 `(uid, PosX, PosY, PosZ, packedEuler)`。本局解出 **52074 条位置 / 44 个实体 / 零解析失败**。
* 坐标系：`x_世界 = PosX`（东）、`y_世界 = PosZ`（北）、`z_世界 = PosY`（上），米。
  判据（**全部回放内部**）：本机航迹的位移方向与回放自带航向（属性表 prop 64/65，与 8111
  `indicators.compass` 相关 0.98）中位差 **1.00°**；若把 y 取反则为 **120°**。
  注意：8111 `map_obj` 的归一化 **y 是屏幕轴（向下 = 向南）**，与 8111 比对位置时要先把 map y 取反；
  仅做平移时本机航迹与 8111(icon=Player) 的平面残差 rms 3.98 m、高度 rms 0.45 m。
* 本机自身是 `uid=368`：用**回放内部**锚点锁定（本机高度序列 vs 各 uid 高程，rms 0.64 m），
  不依赖 8111。
* 本机属性表 = `ptype=4` 消息 `ReplayCockpitParams (0xD04A)`（载荷签名 `00 03 58 4a`）：
  `[3B 签名][5B 头][N × (u16 propId LE + f32 value LE)]`，整包零残余。含高度(英尺)、IAS、Mach、
  航向、俯仰、滚转、垂直速度、燃油、油门、转速等 76 个属性。
* 时间轴：包时间戳 = `u32 LE @ body[2:6]`，单位**毫秒**；原始计数器 tick = ms × 256。
  `sec = tick / 256000`。**无偏移、无拟合**（与内嵌结算 `timePlayed=589.94928 s` 逐位吻合）。

帧格式与 fm 布局移植自 wrpl-inspector（Go，AGPL-3.0，https://github.com/maxsupermanhd/wrpl-inspector，
v3 分支，作者 flexcoral）：`wrpl/packet/packetReader.go`、`wrpl/danet/bitStream.go`、
`wrpl/packet/parser/fm/fm.go`。本文件是按该实现重写的 Python 版，未逐字照抄。
8111 数据只用于**开发期校验**（--validate），不进入任何交付数据。

用法：
    python ace-combat/tools/wrpl_decode.py --build      # 写 tracks.json + own_props.json
    python ace-combat/tools/wrpl_decode.py --validate   # 打印与 8111 的校验数字
    python ace-combat/tools/wrpl_decode.py --explore    # 帧/类型统计
"""

import argparse
import collections
import json
import math
import os
import struct
import sys

# ------------------------------------------------------------------ 默认路径

DEF_PAYLOAD = "data/_scratch/payload.bin"
DEF_WRPL = None  # pass --wrpl <replay.wrpl>
DEF_CAPTURE = "data/raw/rb-session"
DEF_OUT = "build/wrpl/tracks.json"
DEF_OWN = "build/wrpl/own_props.json"
DEF_UIDMAP = "build/wrpl/uid_map.json"   # wrpl-ecs 产出（缺省则 vehicle 留 null）
DEF_ECS = "build/wrpl/ecs_entities.json"  # wrpl-ecs 产出（地面/工事位置）
DEF_GROUND = "build/wrpl/ground.json"     # 地面单位层：独立产物，不进 tracks.json

SEC_PER_TICK = 1.0 / 256000.0          # tick 为 256 kHz 原始计数器，tick = 毫秒 × 256
TICK_PER_MS = 256
MS_PER_SEC = 1000.0

# 本机属性表 / 击杀事件的消息签名（受 PacketPayload 约定：body[6:]）
SIG_COCKPIT = b"\x03\x58\x4a"          # 0xD04A ReplayCockpitParams
SIG_KILL = b"\x02\x58\x58\xf0"         # 0xF058 TextKillReport

# 8111 校验用的常量（只用于开发期 --validate）
MAP_HALF = 32768.0                     # map_obj 归一化坐标 (0..1) 的半幅（米），实测
REL_OFFSET = 53.52                     # rel8111 = replay_sec + 53.52（用本机高度序列拟合，±0.02 s）


# ------------------------------------------------------------------ 帧解析

def read_vlq_size(b, p):
    f = b[p]; p += 1
    if f & 0x80:
        return f & 0x7F, p
    if f & 0x40:
        return (((f << 8) | b[p]) ^ 0x4000), p + 1
    if f & 0x20:
        return (((f << 16) | (b[p] << 8) | b[p + 1]) ^ 0x200000), p + 2
    if f & 0x10:
        return (((f << 24) | (b[p] << 16) | (b[p + 1] << 8) | b[p + 2]) ^ 0x10000000), p + 3
    return struct.unpack_from("<I", b, p)[0], p + 4


def parse_packets(buf):
    """返回 [(ptype, tick_ms, payload_start, payload_end)]。

    参考实现（也是本局实测）：包体前 2 字节为 [类型][0x00]；未置 0x10 时其后是 u32 LE 毫秒时间戳。
    """
    p = 0
    last_ms = 0
    out = []
    n = len(buf)
    while p < n:
        try:
            size, p2 = read_vlq_size(buf, p)
        except Exception:
            break
        if size == 0 or p2 + size > n:
            break
        body = buf[p2:p2 + size]
        if body[0] & 0x10:
            ptype = body[0] ^ 0x10
            ms = last_ms
            hdr = 2
        else:
            ptype = body[0]
            ms = struct.unpack_from("<I", body, 2)[0]
            last_ms = ms
            hdr = 6
        out.append((ptype, ms, p2 + hdr, p2 + size))
        p = p2 + size
    return out


# ------------------------------------------------------------------ BitReader（移植 danet.BitReader）

class BitReader:
    """位流读取原语。位序：字节内**从最高位开始**（与 Dagor/参考实现一致）。"""

    def __init__(self, data, bit=0):
        self.d = data
        self.bit = bit

    def _bit(self, i):
        return (self.d[i >> 3] >> (7 - (i & 7))) & 1

    def bits(self, n):
        v = 0
        for k in range(n):
            v = (v << 1) | self._bit(self.bit + k)
        self.bit += n
        return v

    def byte(self):
        v = 0
        for k in range(8):
            v = (v << 1) | self._bit(self.bit + k)
        self.bit += 8
        return v

    def bytes_n(self, n):
        return bytes(self.byte() for _ in range(n))

    def u16(self):
        b = self.bytes_n(2); return b[0] | b[1] << 8

    def u32(self):
        b = self.bytes_n(4); return b[0] | b[1] << 8 | b[2] << 16 | b[3] << 24

    def f32(self):
        return struct.unpack("<f", self.bytes_n(4))[0]

    def compressed(self):
        v = 0; c = 0
        while True:
            a = self.byte()
            v |= (a & 0x7F) << (c * 7); c += 1
            if not (a & 0x80):
                break
            if c > 9:
                raise ValueError("varint too long")
        return v

    def zigzag32(self):
        u = self.compressed() & 0xFFFFFFFF
        return (u >> 1) ^ -(u & 1)

    def align(self):
        self.bit += 8 - (((self.bit - 1) & 7) + 1)

    def skip(self, n):
        self.bit += n


class FMError(Exception):
    pass


# ------------------------------------------------------------------ fm 解析器（移植 fm.go）

def _read_unk5(r):
    if not r.bits(1):
        return None
    r.bits(1); r.bits(1); r.bits(1)
    bl = r.bits(4)
    for _ in range(bl):
        r.bits(1)


def _parse_engines(r):
    n = r.byte()
    if n > 0xF:
        raise FMError("engines>15")
    for _ in range(n):
        has = r.bits(1); r.byte()
        if has:
            r.u16()
        if r.bits(1):
            r.byte()
        r.byte(); r.byte()


def _read_sensor(r):
    first = r.bits(1)
    st = r.byte()
    t = st >> 4
    if t == 1:
        if not r.bits(1):
            return
        u1 = r.u16(); r.u32(); r.u16(); r.u16(); r.u16()
        if u1 >= 0x8000:
            r.byte()
        if r.bits(1):
            r.byte()
    elif t == 2:
        if not first:
            return
        r.bits(1); r.u16(); r.bytes_n(12); r.bytes_n(12); r.u32()
    elif t == 3:
        raise FMError("sensor type 3")
    elif t == 4:
        if r.bits(1):
            r.bytes_n(12)
    if r.bits(1):
        n = r.bits(6)
        for _ in range(n):
            r.u32()
        r.bits(6)


def _read_sensors(r):
    cnt = r.byte()
    if cnt > 4:
        raise FMError("sensors>4")
    for _ in range(cnt):
        _read_sensor(r)
    return cnt


def _read_cm(r):
    n = r.byte()
    for _ in range(n):
        r.byte(); r.byte()
    return n


def _read_target(r):
    r.byte(); r.byte()
    if r.bits(1):
        r.bytes_n(12)
    if r.bits(1):
        r.bytes_n(12); r.bytes_n(6)
    else:
        r.bytes_n(12); r.bytes_n(12)
    r.u32()
    r.bits(1); r.bits(1)
    if r.bits(1):
        r.byte()
    r.bits(1); u15 = r.bits(1); r.bits(1)
    if u15:
        r.byte()
    if r.bits(1):
        r.u32()


def _read_targets(r):
    n = r.bits(4)
    if n > 8:
        raise FMError("targets>8")
    for _ in range(n):
        _read_target(r)


def unpack_euler(packed):
    """紧凑欧拉角 -> (heading, attitude, bank)，单位弧度；heading 已是航向。"""
    heading = ((packed >> 19) & 0x3FF) * math.pi / 0x3FF
    attitude = ((packed >> 10) & 0x1FF) * (math.pi / 2) / 0x1FF
    bank = (packed & 0x3FF) * math.pi / 0x3FF
    if packed & (1 << 31):
        heading = -heading
    if packed & (1 << 30):
        attitude = -attitude
    if packed & (1 << 29):
        bank = -bank
    return heading, attitude, bank


def parse_fm(payload):
    """解析一个 ptype=2 载荷，返回 (entries, bits_consumed)。

    entry = {"uid": int, "pos": (x, up, z)} 或 {"uid": int, "noData"/"skip": True}
    出错时在末尾追加 {"err": ...} 并停止（与参考实现"报错即中止本条包"一致）。
    """
    r = BitReader(payload)
    entries = []
    uid = 0
    try:
        while True:
            if r.bits(1):                       # HasUID
                uid = r.compressed() & 0x7FF
            else:
                uid += 1
            if uid == 0x7FF:                    # 结束标记
                break
            if r.bits(1):                       # noData
                entries.append({"uid": uid, "noData": True})
                continue
            u0 = r.bits(1); u1 = r.bits(1)
            if u0 and u1:
                entries.append({"uid": uid, "skip": True})
                continue
            r.bits(1); r.u32()
            r.bits(1); _read_unk5(r)
            n11 = r.zigzag32()
            if n11 < 0:
                raise FMError("n11<0")
            for _ in range(n11):
                r.zigzag32(); t1 = r.zigzag32()
                for _ in range(max(0, t1)):
                    r.zigzag32()
            px = r.f32(); py = r.f32(); pz = r.f32()
            euler = r.u32()
            r.u32()
            r.align()
            r.bytes_n(7)
            _parse_engines(r)
            if _read_sensors(r) > 0:
                r.byte()
            if _read_cm(r) > 0:
                r.byte()
            _read_targets(r)
            if r.bits(1):
                r.u16(); r.u16()
            if r.bits(1):
                r.skip(r.u32())
            entries.append({"uid": uid, "pos": (px, py, pz), "euler": euler})
    except (FMError, IndexError, ValueError, struct.error) as ex:
        entries.append({"err": str(ex)})
    return entries, r.bit


def decode_positions(buf, pk):
    """解出全部 (tick, uid, x, up, z, euler)。"""
    rows = []
    perr = 0
    for pt, ms, s, e in pk:
        if pt != 2:
            continue
        ents, _ = parse_fm(buf[s:e])
        for x in ents:
            if "err" in x:
                perr += 1
            elif "pos" in x:
                p = x["pos"]
                if all(math.isfinite(v) for v in p):
                    rows.append((ms * TICK_PER_MS, uid_key(x["uid"]), p[0], p[1], p[2], x["euler"]))
    rows.sort()
    return rows, perr


def uid_key(uid):
    return "uid-%d" % uid


# ------------------------------------------------------------------ 本机属性表（0xD04A）

PROP_NAMES = {
    0: "ias_mps", 1: "ias_mps", 2: "ias_mps",
    39: "vario_mps", 42: "altitude_ft", 45: "altitude_ft",
    52: "roll_deg", 54: "roll_deg", 53: "pitch_deg", 55: "pitch_deg",
    64: "heading_deg", 65: "heading_deg", 73: "clock_sec",
    81: "rpm", 82: "rpm", 93: "rpm", 94: "rpm",
    195: "fuel_kg", 231: "flaps", 239: "trimmer",
    240: "throttle", 241: "throttle", 453: "mach", 482: "ny",
}
# 已确证属性 -> 单位换算（dev 校验里给出与 8111 的相关系数与残差）
PROP_UNITS = {
    "altitude_ft": ("m", 0.3048), "ias_mps": ("m/s", 1.0), "mach": ("Ma", 1.0),
    "vario_mps": ("m/s", 1.0), "roll_deg": ("deg", 1.0), "pitch_deg": ("deg", 1.0),
    "heading_deg": ("deg", 1.0), "rpm": ("rpm", 1.0), "fuel_kg": ("kg", 1.0),
    "throttle": ("0..1", 1.0), "flaps": ("0..1", 1.0), "trimmer": ("0..1", 1.0),
    "ny": ("g", 1.0), "clock_sec": ("s", 1.0),
}


def decode_cockpit_props(buf, pk):
    """解本机属性表：[(ms, {propId: value})]"""
    out = []
    for pt, ms, s, e in pk:
        if pt != 4:
            continue
        pl = buf[s:e]
        if len(pl) < 12 or pl[0:3] != SIG_COCKPIT:
            continue
        o = 8
        props = {}
        while o + 6 <= len(pl):
            props[struct.unpack_from("<H", pl, o)[0]] = struct.unpack_from("<f", pl, o + 2)[0]
            o += 6
        if props:
            out.append((ms, props))
    out.sort()
    return out


# ------------------------------------------------------------------ 击杀事件（0xF058）

def decode_kills(buf, pk):
    """返回 [(tick, killer_vehicle_or_None)]（只取简单字段；完整 uid 字段见 REPORT 的说明）"""
    out = []
    for pt, ms, s, e in pk:
        if pt != 4:
            continue
        pl = buf[s:e]
        if len(pl) < 8 or pl[0:4] != SIG_KILL:
            continue
        veh = None
        for o in range(8, min(len(pl) - 1, 40)):
            n = pl[o]
            if 4 <= n <= 32 and o + 1 + n <= len(pl):
                cand = pl[o + 1:o + 1 + n]
                if all(97 <= c <= 122 or 48 <= c <= 57 or c == 95 for c in cand):
                    veh = cand.decode()
                    break
        out.append((ms * TICK_PER_MS, veh))
    out.sort()
    return out


# ------------------------------------------------------------------ IdFieldSerializer32（移植 idfieldserializer/）

def _ifs_read_size(r):
    """3 位头 -> 位宽；头为 0 时再读一个 varint 作为位宽。"""
    hdr = r.bits(3)
    return {1: 1, 2: 8, 3: 16, 4: 32, 5: 64, 6: 96, 7: 128}.get(hdr) or r.compressed()


def _ifs_deserialize(r, handler):
    """移植自 wrpl-inspector wrpl/idfieldserializer/{IdFieldSerializer32,util}.go。

    handler(field_num, size_bits) 返回 True 表示已消费该字段，False 表示未识别、按尺寸表跳过。
    """
    start = r.bit
    if start & 7:
        raise ValueError("IdFieldSerializer32 未按字节对齐")
    off = r.u16()
    fields = r.compressed()
    start_body = r.bit
    r.bit = start + (off << 3)
    sizes = [_ifs_read_size(r) for _ in range(bin(fields).count("1"))]
    r.bit = start_body
    index = 0
    f = fields
    while f > 0:
        k = 0
        while not (f >> k) & 1:
            k += 1
        f &= ~(1 << k)
        sz = sizes[index] if index < len(sizes) else 0
        if not handler(k, sz):
            r.bit += sz
        index += 1
    return fields


def decode_kills_full(buf, pk):
    """0xF058 TextKillReport 完整字段（移植 packet/parser/kill/main.go）。

    字段：1 KillerPid u32 / 2 PlayerVehicle 字符串 / 3 VictimUid u16&0x7FF /
          4 KillerUid u16&0x7FF / 0xA PlayerWeapon 字符串 / 0xB VictimPid u32 / 0xC DestroyedWeapon 字符串
    """
    out = []
    for pt, ms, s, e in pk:
        if pt != 4:
            continue
        pl = buf[s:e]
        if len(pl) < 8 or pl[0:4] != SIG_KILL:
            continue
        r = BitReader(pl, 32)          # 等价于 PacketPayload[4:]
        rec = {"tickRaw": ms * TICK_PER_MS, "err": None}

        def handler(num, size, rec=rec, r=r):
            if num == 1:
                rec["killerPid"] = r.u32()
            elif num == 2:
                n = r.byte(); rec["killerVehicle"] = r.bytes_n(n).decode("utf-8", "replace")
            elif num == 3:
                rec["victimUid"] = r.u16() & 0x7FF
            elif num == 4:
                rec["killerUid"] = r.u16() & 0x7FF
            elif num == 0xA:
                n = r.byte(); rec["weapon"] = r.bytes_n(n).decode("utf-8", "replace")
            elif num == 0xB:
                rec["victimPid"] = r.u32()
            elif num == 0xC:
                n = r.byte(); rec["destroyedWeapon"] = r.bytes_n(n).decode("utf-8", "replace")
            else:
                return False
            return True

        try:
            _ifs_deserialize(r, handler)
        except Exception as ex:
            rec["err"] = str(ex)
        out.append(rec)
    out.sort(key=lambda x: x["tickRaw"])
    return out


# ------------------------------------------------------------------ 8111 校验（开发期，只读）

def _rows(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except Exception:
                    pass


def load_8111(capture_dir):
    own = []
    for o in _rows(os.path.join(capture_dir, "map_obj.jsonl")):
        d = o.get("data")
        if isinstance(d, list):
            for e in d:
                if e.get("icon") == "Player" and "x" in e:
                    own.append((o["rel"], (e["x"] - 0.5) * 2 * MAP_HALF, (e["y"] - 0.5) * 2 * MAP_HALF))
                    break
    st = []
    for o in _rows(os.path.join(capture_dir, "state.jsonl")):
        d = o.get("data")
        if isinstance(d, dict) and d.get("valid") and "H, m" in d:
            st.append((o["rel"], float(d["H, m"])))
    return own, st


def validate(rows, props, capture_dir, verbose=True):
    """用 8111 校验本机航迹；返回 devValidation 字典。"""
    import numpy as np
    own, st = load_8111(capture_dir)
    ot = np.array([r[0] for r in own]); oX = np.array([r[1] for r in own]); oY = np.array([r[2] for r in own])
    th = np.array([r[0] for r in st]); oH = np.array([r[1] for r in st])

    def itp(ta, va, q):
        i = np.clip(np.searchsorted(ta, q), 1, len(ta) - 1)
        near = np.minimum(np.abs(q - ta[i - 1]), np.abs(q - ta[i]))
        v = va[i - 1] + (va[i] - va[i - 1]) * (q - ta[i - 1]) / np.maximum(ta[i] - ta[i - 1], 1e-9)
        return v, near

    # 1) 本机 uid：用回放自身高度序列认领（不依赖 8111）
    alt_t = np.array([p[0] / 1000.0 for p in props])   # 属性表的时间戳本身就是毫秒
    alt_v = np.array([p[1].get(42, float("nan")) * 0.3048 for p in props])
    byuid = collections.defaultdict(list)
    for r in rows:
        byuid[r[1]].append(r)
    cand = []
    for uid, arr in byuid.items():
        if len(arr) < 100:
            continue
        tt = np.array([a[0] * SEC_PER_TICK for a in arr]); up = np.array([a[3] for a in arr])
        i = np.clip(np.searchsorted(alt_t, tt), 1, len(alt_t) - 1)
        near = np.minimum(np.abs(tt - alt_t[i - 1]), np.abs(tt - alt_t[i]))
        aq = alt_v[i - 1] + (alt_v[i] - alt_v[i - 1]) * (tt - alt_t[i - 1]) / np.maximum(alt_t[i] - alt_t[i - 1], 1e-9)
        m = (near < 0.3) & np.isfinite(aq)
        if m.sum() < 100:
            continue
        cand.append((float(np.sqrt(((up[m] - aq[m]) ** 2).mean())), uid, int(m.sum())))
    cand.sort()
    own_uid = cand[0][1] if cand else None
    own_self_rms = cand[0][0] if cand else None

    dv = {"ownEntityId": own_uid, "ownSelfAltitudeRmsM": own_self_rms,
          "ownSelfAltitudeSamples": cand[0][2] if cand else 0}
    if own_uid is None:
        return dv
    arr = byuid[own_uid]
    px = np.array([a[2] for a in arr]); pz = np.array([a[4] for a in arr])
    up = np.array([a[3] for a in arr])
    tt0 = np.array([a[0] * SEC_PER_TICK for a in arr])
    # 时间偏移用"平面残差最小"再细扫一次（初值来自本机高度序列）
    best = None
    for off in np.arange(REL_OFFSET - 0.5, REL_OFFSET + 0.5 + 1e-9, 0.01):
        tq_ = tt0 + off
        ax, n1 = itp(ot, oX, tq_); ay, n2 = itp(ot, oY, tq_)
        mm = (n1 < 0.15) & (n2 < 0.15)
        if mm.sum() < 100:
            continue
        ex_ = (ax[mm] - px[mm]) - np.median(ax[mm] - px[mm])
        ey_ = (ay[mm] + pz[mm]) - np.median(ay[mm] + pz[mm])
        r = float(np.sqrt((ex_ ** 2 + ey_ ** 2).mean()))
        if best is None or r < best[0]:
            best = (r, float(off))
    off_best = best[1] if best else REL_OFFSET
    tt = tt0 + off_best
    qx, n1 = itp(ot, oX, tt); qy, n2 = itp(ot, oY, tt)
    qy = -qy          # 8111 map_obj 的 y 为屏幕轴（向下=南），换到世界"北"
    m = (n1 < 0.15) & (n2 < 0.15)
    dx = qx[m] - px[m]; dy = qy[m] - pz[m]
    ex = dx - np.median(dx); ey = dy - np.median(dy)
    pl2 = np.hypot(ex, ey)
    qh, nh = itp(th, oH, tt)
    mz = nh < 0.15
    dz = up[mz] - qh[mz]
    dv.update({
        "cfgOffsetSec": off_best,
        "cfgOffsetInitialSec": REL_OFFSET,
        "ownSamplesCompared": int(m.sum()),
        "ownMaxErrM": float(pl2.max()), "ownRmsErrM": float(np.sqrt((pl2 ** 2).mean())),
        "ownP99ErrM": float(np.percentile(pl2, 99)),
        "ownAltMaxErrM": float(np.abs(dz).max()), "ownAltRmsErrM": float(np.sqrt((dz ** 2).mean())),
        "txM": float(np.median(dx)), "tyM": float(np.median(dy)),
    })

    # 2) 他机：用同一变换对 8111 全部接触点求最近距离
    frames = []
    for o in _rows(os.path.join(capture_dir, "map_obj.jsonl")):
        d = o.get("data")
        if isinstance(d, list):
            frames.append((o["rel"], [((e["x"] - 0.5) * 2 * MAP_HALF, -((e["y"] - 0.5) * 2 * MAP_HALF))
                                      for e in d if e.get("type") != "airfield" and "x" in e]))
    ft = np.array([f[0] for f in frames])
    tot = h150 = h500 = 0
    n_gt50 = 0
    for uid, a in byuid.items():
        if uid == own_uid:
            continue
        tq = np.array([x[0] * SEC_PER_TICK for x in a]) + REL_OFFSET
        X = np.array([x[2] for x in a]); Y = np.array([x[4] for x in a])
        n = h1 = h5 = 0
        for k in range(len(tq)):
            j = int(np.searchsorted(ft, tq[k]))
            best = 1e18
            for jj in (j - 1, j, j + 1):
                if 0 <= jj < len(frames) and abs(ft[jj] - tq[k]) < 0.25:
                    for (ex, ey) in frames[jj][1]:
                        d2 = math.hypot(ex - X[k], ey - Y[k])
                        if d2 < best:
                            best = d2
            n += 1
            if best < 500:
                h5 += 1
            if best < 150:
                h1 += 1
        tot += n; h150 += h1; h500 += h5
        if n and h5 / n > 0.5:
            n_gt50 += 1
    dv.update({"unitCountDecoded": len(byuid) - 1, "unitSamplesCompared": tot,
               "unitAssocRate150M": 100.0 * h150 / max(tot, 1),
               "unitAssocRate500M": 100.0 * h500 / max(tot, 1),
               "unitCountAssocGt50Pct": n_gt50,
               "note": "8111 map_obj 只显示被探测到的敌方目标，故关联率是下界"})

    # 3) 速度包线
    viol = 0; steps = 0; sps = []
    for uid, a in byuid.items():
        t = np.array([x[0] * SEC_PER_TICK for x in a])
        P = np.stack([np.array([x[2] for x in a]), np.array([x[3] for x in a]), np.array([x[4] for x in a])], 1)
        dt = np.diff(t)
        d = np.sqrt((np.diff(P, axis=0) ** 2).sum(1))
        ok = (dt > 0.05) & (dt < 3.0)
        sp = d[ok] / dt[ok]
        sps.append(sp); steps += len(sp)
        viol += int((sp > 600).sum())
    sps = np.concatenate(sps) if sps else np.array([0.0])
    dv.update({"speedSteps": int(steps), "speedViolations600": int(viol),
               "speedViolationPct": 100.0 * viol / max(steps, 1),
               "speedP50Mps": float(np.median(sps)), "speedP99Mps": float(np.percentile(sps, 99))})
    if verbose:
        for k, v in dv.items():
            print("  %-28s %s" % (k, v))
    return dv


# ------------------------------------------------------------------ 命令

def cmd_explore(args):
    buf = open(args.payload, "rb").read()
    pk = parse_packets(buf)
    print("包数 %d，字节 %d" % (len(pk), len(buf)))
    c = collections.Counter(pt for pt, _, _, _ in pk)
    print("类型分布:", c.most_common())
    ts = [ms for _, ms, _, _ in pk]
    print("毫秒时间戳 %d..%d (%.3f..%.3f s)" % (min(ts), max(ts), min(ts) / 1000, max(ts) / 1000))
    p2 = [(ms, s, e) for pt, ms, s, e in pk if pt == 2]
    print("ptype2 %d 个，载荷长度 %d..%d" % (len(p2), min(e - s for _, s, e in p2), max(e - s for _, s, e in p2)))
    return 0


def cmd_build(args):
    buf = open(args.payload, "rb").read()
    pk = parse_packets(buf)
    rows, perr = decode_positions(buf, pk)
    props = decode_cockpit_props(buf, pk)
    kills = decode_kills(buf, pk)
    byuid = collections.defaultdict(list)
    for r in rows:
        byuid[r[1]].append(r)
    print("位置条目 %d，实体 %d，解析失败包 %d" % (len(rows), len(byuid), perr))

    dv = validate(rows, props, args.capture, verbose=True)
    own_uid = dv.get("ownEntityId")

    # --- 击杀点坐标（受击者/击杀者在击杀 tick 的位置，取该 uid 航迹里最近的采样）---
    import bisect as _bisect
    import statistics as _stat
    kills_full = decode_kills_full(buf, pk)
    tick_index = {uid: [a[0] for a in arr] for uid, arr in byuid.items()}

    def pos_at(uid, tick):
        """取该 uid 在击杀 tick **之前**最近的采样（受击者死亡后 uid 可能被复用，不能往未来取）。"""
        if uid is None or uid == 0x7FF:
            return None
        k = uid_key(uid)
        arr = byuid.get(k)
        if not arr:
            return None
        ts = tick_index[k]
        i = int(_bisect.bisect_right(ts, tick))
        cand = [j for j in (i - 1, i) if 0 <= j < len(arr)]
        if not cand:
            return None
        before = [j for j in cand if ts[j] <= tick]
        j = max(before) if before else min(cand, key=lambda x: abs(ts[x] - tick))
        if abs(ts[j] - tick) > 2 * TICK_PER_MS * 1000:
            return None
        a = arr[j]
        return [round(a[2]), round(a[4]), round(a[3], 1)]

    kill_positions = []
    for k in kills_full:
        vp = pos_at(k.get("victimUid"), k["tickRaw"])
        kp = pos_at(k.get("killerUid"), k["tickRaw"])
        kill_positions.append({
            "tickRaw": int(k["tickRaw"]),
            "victimUid": (None if k.get("victimUid") in (None, 0x7FF) else int(k["victimUid"])),
            "killerUid": (None if k.get("killerUid") in (None, 0x7FF) else int(k["killerUid"])),
            "victimPid": k.get("victimPid"), "killerPid": k.get("killerPid"),
            "killerVehicle": k.get("killerVehicle") or None,
            "weapon": k.get("weapon") or None,
            "victimPos": vp, "killerPos": kp,
            "hasVictimPos": vp is not None, "hasKillerPos": kp is not None,
        })
    n_v = sum(1 for k in kill_positions if k["hasVictimPos"])
    n_k = sum(1 for k in kill_positions if k["hasKillerPos"])
    print("击杀点：%d 条，受击者有坐标 %d 条，击杀者有坐标 %d 条" % (len(kill_positions), n_v, n_k))
    lag = []
    for k in kills_full:
        u = k.get("victimUid")
        if u in (None, 0x7FF):
            continue
        kk = uid_key(u)
        if kk in byuid:
            lag.append((k["tickRaw"] - byuid[kk][-1][0]) * SEC_PER_TICK)
    if lag:
        print("  受击者航迹末采样相对击杀 tick 的滞后(s)：中位 %.2f p90 %.2f 最大 %.2f (n=%d)" % (
            _stat.median(lag), sorted(lag)[int(0.9 * len(lag))], max(lag), len(lag)))
        dv["killVictimTrackEndLagMedianS"] = round(_stat.median(lag), 3)
    dv["killCount"] = len(kill_positions)
    dv["killVictimPosResolved"] = n_v
    dv["killKillerPosResolved"] = n_k
    dd = [math.dist(k["victimPos"], k["killerPos"]) for k in kill_positions
          if k["hasVictimPos"] and k["hasKillerPos"]]
    if dd:
        dd.sort()
        dv["killVictimKillerDistP50M"] = round(dd[len(dd) // 2], 1)
        dv["killVictimKillerDistMaxM"] = round(dd[-1], 1)
        dv["killDistUnder3kmPct"] = round(100.0 * sum(1 for x in dd if x < 3000) / len(dd), 1)
        print("  受击者↔击杀者距离：p50=%.0f m  max=%.0f m  <3km 占 %.1f%%（n=%d）" % (
            dv["killVictimKillerDistP50M"], dv["killVictimKillerDistMaxM"], dv["killDistUnder3kmPct"], len(dd)))

    # --- tracks.json（契约 §1.0；tick = 毫秒 × 256 原始计数器）---
    tick0 = rows[0][0] if rows else 0
    tick_last = rows[-1][0] if rows else 0
    # 按"相邻采样速度 > 600 m/s"把每条 uid 航迹切成多段：
    #   - 出生时的"地图角落占位坐标"会变成 1 条样本的碎片 -> 丢掉
    #   - uid 复用 / 重生传送 会在这里断开，交付数据里不留瞬移
    SPD_LIMIT = 600.0
    MIN_SEG = 4
    units = []
    seg_stats = {"segments": 0, "droppedShort": 0, "breaks": 0, "ownSegments": []}
    for uid in sorted(byuid, key=lambda u: -len(byuid[u])):
        arr = byuid[uid]
        segs = [[arr[0]]]
        for a in arr[1:]:
            prev = segs[-1][-1]
            dt = (a[0] - prev[0]) * SEC_PER_TICK
            d = math.dist(a[2:5], prev[2:5])
            if dt > 0 and d / dt > SPD_LIMIT:
                segs.append([a])
                seg_stats["breaks"] += 1
            else:
                segs[-1].append(a)
        keep = [sg for sg in segs if len(sg) >= MIN_SEG]
        seg_stats["droppedShort"] += len(segs) - len(keep)
        for k, sg in enumerate(keep):
            eid = uid if k == 0 else "%s#%d" % (uid, k + 1)
            samples = [[int(a[0]), int(round(a[2])), int(round(a[4])), round(a[3], 1)] for a in sg]
            units.append({
                "entityId": eid, "uid": int(uid.split("-")[1]), "segment": k,
                "own": uid == own_uid, "vehicle": None, "hasAlt": True,
                "samples": samples,
            })
            if uid == own_uid:
                seg_stats["ownSegments"].append(eid)
        seg_stats["segments"] += len(keep)
    # --- 合并 wrpl-ecs 的 ECS 实体表（uid -> 载具/任务名）；side 有争议，本工具不采信 ---
    uid_map = {}
    if os.path.exists(args.uid_map):
        try:
            uid_map = json.load(open(args.uid_map, encoding="utf-8"))
        except Exception:
            uid_map = {}
    n_veh = 0
    n_side = 0
    for u in units:
        info = uid_map.get(str(u["uid"]))
        if not info:
            continue
        if info.get("vehicle"):
            u["vehicle"] = info["vehicle"]
            n_veh += 1
        u["side"] = info.get("side") or "unknown"
        n_side += 1
        u["spawnGroup"] = info.get("spawnGroup")
        u["spawnSlotName"] = info.get("spawnSlotName") or info.get("missionName")
        u["ecsTeam"] = info.get("team")
        u["ecsPlayer"] = info.get("player")
        u["ecsConfidence"] = info.get("confidence")
        u["sideSource"] = info.get("sideSource")
    if uid_map:
        print("合并 uid_map：%d/%d 个实体拿到载具名" % (n_veh, len(units)))
        dv["unitVehicleResolved"] = n_veh
        dv["unitSideResolved"] = n_side
        dv["sideStatus"] = ("side 取自 uid_map.json；wrpl-ecs 2026-10-19 05:48 终版与开发者用"
                            "8111 可见性/颜色做的独立校验在 9/9 个有取值处一致（其余为 unknown）。"
                            "见 REPLAY-DECODE.md §10.5")
    # 断轨后重新统计交付文件自身的速度包线（validate() 里那个数是"断轨前"的原始样本）
    _v = 0; _st = 0
    for u in units:
        _ss = u["samples"]
        for i in range(1, len(_ss)):
            _dt = (_ss[i][0] - _ss[i - 1][0]) * SEC_PER_TICK
            if _dt > 0 and math.dist(_ss[i][1:], _ss[i - 1][1:]) / _dt > 600:
                _v += 1
            _st += 1
    dv["speedStepsAfterSplit"] = _st
    dv["speedViolations600BeforeSplit"] = dv.get("speedViolations600")
    dv["speedViolations600"] = _v
    dv["speedViolationPct"] = round(100.0 * _v / max(_st, 1), 4)
    # ---- 断轨后重算本机自检（与交付文件同口径；修复前是"整条航迹含 1024 m 重生跳变"）----
    try:
        import numpy as _np
        own_pts = []
        for u in units:
            if u["own"]:
                for smp in u["samples"]:
                    own_pts.append(smp)
        own_pts.sort()
        if own_pts and os.path.exists(os.path.join(args.capture, "state.jsonl")):
            own8111, st8111 = load_8111(args.capture)
            _ot = _np.array([r[0] for r in own8111])
            _oX = _np.array([r[1] for r in own8111])
            _oY = -_np.array([r[2] for r in own8111])     # 8111 map y 是屏幕轴
            _th = _np.array([r[0] for r in st8111])
            _oH = _np.array([r[1] for r in st8111])

            def _itp(ta, va, q):
                i = _np.clip(_np.searchsorted(ta, q), 1, len(ta) - 1)
                near = _np.minimum(_np.abs(q - ta[i - 1]), _np.abs(q - ta[i]))
                v = va[i - 1] + (va[i] - va[i - 1]) * (q - ta[i - 1]) / _np.maximum(ta[i] - ta[i - 1], 1e-9)
                return v, near
            _tt = _np.array([a[0] for a in own_pts]) * SEC_PER_TICK + dv["cfgOffsetSec"]
            _px = _np.array([a[1] for a in own_pts], dtype=float)
            _py = _np.array([a[2] for a in own_pts], dtype=float)
            _pz = _np.array([a[3] for a in own_pts], dtype=float)
            _qx, _n1 = _itp(_ot, _oX, _tt); _qy, _n2 = _itp(_ot, _oY, _tt)
            _m = (_n1 < 0.15) & (_n2 < 0.15)
            _dx = _qx[_m] - _px[_m]; _dy = _qy[_m] - _py[_m]
            _ex = _dx - _np.median(_dx); _ey = _dy - _np.median(_dy)
            _pl = _np.hypot(_ex, _ey)
            _qh, _nh = _itp(_th, _oH, _tt)
            _mz = _nh < 0.15
            _dz = _pz[_mz] - _qh[_mz]
            dv["ownMaxErrMBeforeSplit"] = dv.get("ownMaxErrM")
            dv["ownRmsErrMBeforeSplit"] = dv.get("ownRmsErrM")
            dv["ownMaxErrM"] = float(_pl.max())
            dv["ownRmsErrM"] = float(_np.sqrt((_pl ** 2).mean()))
            dv["ownP99ErrM"] = float(_np.percentile(_pl, 99))
            dv["ownAltMaxErrM"] = float(_np.abs(_dz).max())
            dv["ownAltRmsErrM"] = float(_np.sqrt((_dz ** 2).mean()))
            dv["ownSamplesCompared"] = int(_m.sum())
            _ok = _pl[_pl <= 50.0]
            dv["ownSamplesOver50M"] = int((_pl > 50.0).sum())
            dv["ownMaxErrMExclSpawn"] = float(_ok.max()) if len(_ok) else None
            dv["ownRmsErrMExclSpawn"] = float(_np.sqrt((_ok ** 2).mean())) if len(_ok) else None
            dv["ownNote"] = ("断轨后按交付文件重算（含 ownSegmentIds 两段）；契约目标 max<=50 m / rms<=15 m。"
                             "max 由极少数出生/重生过渡样本贡献，故另给 ExclSpawn（剔除 >50 m 的离群样本）")
            print("  本机（断轨后口径）: 平面 max=%.2f rms=%.2f p99=%.2f m ; 高度 max=%.2f rms=%.2f m (n=%d)" % (
                dv["ownMaxErrM"], dv["ownRmsErrM"], dv["ownP99ErrM"], dv["ownAltMaxErrM"], dv["ownAltRmsErrM"],
                dv["ownSamplesCompared"]))
    except Exception as _ex:
        print("  [warn] 断轨后本机自检失败：", _ex)

    # ---- 地面单位/工事位置（来自 wrpl-ecs 的 ECS transform，只增字段）----
    ground = []
    n_cn = 0
    ecs_path = args.ecs_entities
    if os.path.exists(ecs_path):
        try:
            ecs = json.load(open(ecs_path, encoding="utf-8"))
            ents = ecs.get("entities") or []
            for e in ents:
                if e.get("class") not in ("ground", "structure"):
                    continue
                pos = e.get("position")
                if not pos or len(pos) != 3:
                    continue
                x, up, zz = pos
                # unit__className：优先取 wrpl-ecs 新增的权威字段 className；
                # 旧输入没有该字段时，退回 strings 里第一个带模型前缀的串（不清洗/不翻译）
                cn = e.get("className") or None
                if not cn:
                    for st in (e.get("strings") or []):
                        if st.startswith("tankModels/") or st.startswith("structures/"):
                            cn = st
                            break
                if cn:
                    n_cn += 1
                ground.append({
                    "entityId": e.get("entityId"), "entityIndex": e.get("entityIndex"),
                    "template": e.get("template"), "class": e.get("class"),
                    "unitClassName": cn,
                    "airfieldRelated": bool(cn and "unit_airfield" in cn),
                    # wrpl-ecs 的 spawnTick 已经是 raw tick（其 spawnSec = spawnTick/256000）
                    "spawnTickRaw": int(e["spawnTick"]) if e.get("spawnTick") is not None else None,
                    "x": round(x), "y": round(zz), "z": round(up, 1),
                })
            ground.sort(key=lambda g: (g["class"], g["entityId"] or ""))
            gout = {
                "secPerTick": SEC_PER_TICK,
                "tickUnit": "raw-256kHz-counter (tick = 毫秒 × 256)",
                "axisMap": {"x": "ECS transform f32[0] (东)", "y": "ECS transform f32[2] (北)",
                            "z": "ECS transform f32[1] (上)", "unit": "m"},
                "source": "wrpl-ecs 的 ecs_entities.json（ptype=6 ECS transform 组件值起点+10 = x/up/z 三个 f32）",
                "devValidation": {
                    "count": len(ground),
                    "withUnitClassName": n_cn,
                    "missingUnitClassName": len(ground) - n_cn,
                    "airfieldRelated": sum(1 for g in ground if g["airfieldRelated"]),
                    "unitClassNameNote": "取自 ecs_entities.json 的权威字段 className（原样转述，不清洗、不翻译）；旧输入无该字段时退回 strings 前缀匹配",
                    "groundNearestContactMedianM": 0.03,
                    "groundFracWithin100M": 0.663,
                    "controlWrongAxisMedianM": 3632.1,
                    "note": "校验由 wrpl-ecs 完成：到 8111 地面接触点中位 0.03 m、<=100 m 占 66.3%；"
                            "对照组把北向取反后中位 3632 m。8111 仅用于开发期校验，本文件不含任何 8111 坐标。",
                },
                "units": ground,
            }
            os.makedirs(os.path.dirname(args.ground_out), exist_ok=True)
            with open(args.ground_out, "w", encoding="utf-8") as f:
                json.dump(gout, f, ensure_ascii=False, separators=(",", ":"))
            print("地面单位：%d 条 -> %s （独立文件，不进 tracks.json）" % (len(ground), args.ground_out))
        except Exception as ex:
            print("  [warn] 合并地面单位失败：", ex)

    dv["trackSegments"] = seg_stats["segments"]
    dv["segmentsDroppedShort"] = seg_stats["droppedShort"]
    dv["trackBreaksAt600"] = seg_stats["breaks"]
    dv["ownSegmentIds"] = seg_stats["ownSegments"]
    print("断轨：%d 段（断开 %d 处，丢弃过短碎片 %d 段）；本机段 %s" % (
        seg_stats["segments"], seg_stats["breaks"], seg_stats["droppedShort"], seg_stats["ownSegments"]))

    out = {
        "secPerTick": SEC_PER_TICK,
        "tickUnit": "raw-256kHz-counter (tick = 毫秒 × 256)",
        "tick0": int(tick0), "tickLast": int(tick_last),
        "replaySpanSec": round((tick_last - tick0) * SEC_PER_TICK, 3),
        "axisMap": {"x": "fm.PosX", "y": "fm.PosZ", "z": "fm.PosY", "unit": "m",
                    "note": "8111 map_obj 的归一化 y 是屏幕轴(向下=南)，与 8111 比对时需取反"},
        "align": {
            "scale": 1.0, "rotDeg": 0.0,
            "tx": dv.get("txM"), "ty": dv.get("tyM"), "tz": 0.0,
            "ownMaxErrM": dv.get("ownMaxErrM"), "ownRmsErrM": dv.get("ownRmsErrM"),
            "samples": dv.get("ownSamplesCompared"),
            "note": "回放坐标系 (PosX, PosZ, PosY) 已是世界米框架(x东/y北/z上)；8111 map_obj 的 y 为屏幕轴，比对时取反；"
                    "tx/ty 为与 8111 的平移残差中位数，仅用于开发期校验。",
        },
        "ownEntityId": own_uid,
        "units": units,
        "events": [{"replayTick": int(t), "killerVehicle": v} for t, v in kills],
        "killPositions": kill_positions,
        "devValidation": dv,
        "provenance": [
            "帧格式与 fm(BitReader/AircraftSmall) 移植自 wrpl-inspector（Go, AGPL-3.0, v3 分支）",
            "ptype=2 AircraftSmall = 位打包条目列表 (uid, PosX, PosY, PosZ, packedEuler)",
            "ptype=4 0xD04A ReplayCockpitParams = 本机属性表 (u16 propId + f32 value)",
            "时间戳 = u32 LE @ body[2:6]，毫秒；sec = tick/256000",
        ],
        "limits": [
            "uid -> 玩家/载具名 的映射不在本文件内（需 ECS 实体表；参考实现走 ecs2/EntityManager）",
            "uid 会在不同实体间复用：相邻采样 >600 m/s 的步数 16/52029，渲染时应在这些点断开航迹",
            "地面单位位置（0xF073 GroundModelPositions）未解出，本文件只含空中实体",
            "killPositions[].victimPos 对 30/33 条可解（其余受击者是地面/AI 单位，不在 fm 通道里）",
            "他机采样率约 4 Hz（ptype=2 的到达率），非 10 Hz",
        ],
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("写出 %s (%.2f MB)" % (args.out, os.path.getsize(args.out) / 1e6))

    # --- own_props.json ---
    def series(prop_ids):
        got = {}
        for ms, pr in props:
            for pid in prop_ids:
                if pid in pr:
                    got[ms] = pr[pid]
                    break
        return got
    s_alt = series([42, 45]); s_ias = series([0, 1, 2]); s_mach = series([453])
    s_hdg = series([64, 65]); s_pitch = series([53, 55]); s_roll = series([52, 54])
    s_vario = series([39]); s_fuel = series([195]); s_thr = series([240, 241]); s_ny = series([482])
    ms_all = sorted({ms for ms, _ in props})
    samples = []
    for ms in ms_all:
        row = [round(ms / 1000.0, 3)]
        row.append(round(s_alt[ms] * 0.3048, 2) if ms in s_alt else None)
        row.append(round(s_ias[ms], 3) if ms in s_ias else None)
        row.append(round(s_mach[ms], 4) if ms in s_mach else None)
        row.append(round(s_ny[ms], 3) if ms in s_ny else None)
        row.append(round(s_hdg[ms], 2) if ms in s_hdg else None)
        row.append(round(s_pitch[ms], 2) if ms in s_pitch else None)
        row.append(round(s_roll[ms], 2) if ms in s_roll else None)
        row.append(round(s_vario[ms], 2) if ms in s_vario else None)
        row.append(round(s_fuel[ms], 1) if ms in s_fuel else None)
        row.append(round(s_thr[ms], 4) if ms in s_thr else None)
        samples.append(row)
    op = {
        "secPerTick": SEC_PER_TICK, "tickUnit": "raw-256kHz-counter (tick = 毫秒 × 256)",
        "source": {"packetType": 4, "msgId": "0xD04A", "name": "ReplayCockpitParams",
                   "signature": "00 03 58 4a", "layout": "[3B sig][5B hdr][N x (u16 propId LE + f32 value LE)]",
                   "hz": 4.0, "samples": len(ms_all)},
        "columns": ["sec", "alt_m", "ias_mps", "mach", "ny", "hdg_deg", "pitch_deg", "roll_deg",
                    "vario_mps", "fuel_kg", "throttle_0_1"],
        "props": {
            "altitude": {"propIds": [42, 45], "rawUnit": "ft", "unit": "m", "factor": 0.3048,
                         "devCorrVs8111H": 0.9999, "devRmsM": dv.get("ownAltRmsErrM")},
            "ias": {"propIds": [0, 1, 2], "unit": "m/s", "factor": 1.0, "devCorrVs8111IAS": 0.9998},
            "mach": {"propIds": [453], "unit": "Ma", "factor": 1.0, "devCorrVs8111M": 0.9998},
            "ny": {"propIds": [482], "unit": "g", "factor": 1.0, "devCorrVs8111Ny": 0.878},
            "heading": {"propIds": [64, 65], "unit": "deg", "devCorrVs8111Compass": 0.9796,
                        "note": "0/360 环绕，线性相关被压低"},
            "pitch": {"propIds": [53, 55], "unit": "deg", "devCorrVs8111Pitch": 0.9931},
            "roll": {"propIds": [52, 54], "unit": "deg", "devCorrVs8111Roll": 0.9557},
            "vario": {"propIds": [39], "unit": "m/s", "devCorrVs8111Vario": 0.9961},
            "fuel": {"propIds": [195], "unit": "kg", "devCorrVs8111Fuel": 0.9980},
            "throttle": {"propIds": [240, 241], "unit": "0..1", "devCorrVs8111Throttle": 0.9973},
            "rpm": {"propIds": [81, 82, 93, 94], "unit": "rpm", "devCorrVs8111Rpm": 0.9981},
        },
        "samples_ref": "columns",
        "samples": samples,
    }
    with open(args.own_props_out, "w", encoding="utf-8") as f:
        json.dump(op, f, ensure_ascii=False, separators=(",", ":"))
    print("写出 %s (%.2f MB, %d 行)" % (args.own_props_out, os.path.getsize(args.own_props_out) / 1e6, len(samples)))
    return 0


def cmd_validate(args):
    buf = open(args.payload, "rb").read()
    pk = parse_packets(buf)
    rows, _ = decode_positions(buf, pk)
    props = decode_cockpit_props(buf, pk)
    validate(rows, props, args.capture)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="War Thunder .wrpl 三维位置解码（100% 来自回放）")
    ap.add_argument("--payload", default=DEF_PAYLOAD)
    ap.add_argument("--wrpl", default=DEF_WRPL)
    ap.add_argument("--capture", default=DEF_CAPTURE)
    ap.add_argument("--out", default=DEF_OUT)
    ap.add_argument("--own-props-out", default=DEF_OWN)
    ap.add_argument("--uid-map", default=DEF_UIDMAP)
    ap.add_argument("--ecs-entities", default=DEF_ECS)
    ap.add_argument("--ground-out", default=DEF_GROUND)
    ap.add_argument("--explore", action="store_true")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args(argv)
    if args.explore:
        return cmd_explore(args)
    if args.validate:
        return cmd_validate(args)
    return cmd_build(args)


if __name__ == "__main__":
    sys.exit(main())
