#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 War Thunder `.wrpl` 回放里提取容器结构、内嵌结算 JSON、玩家表与单位/载具名表。

用法：
    python ace-combat/tools/wrpl_header.py
    python ace-combat/tools/wrpl_header.py --replay <file.wrpl> --out-dir <dir>

产物：
    <out-dir>/header.json    容器布局 + 文件头 + 回放时钟 + 内嵌结算 JSON + 玩家表
    <out-dir>/entities.json  单位名表、载具出生事件、玩家↔载具、击杀包、奖项、聊天、已排除项
    默认 <out-dir> = ace-combat/build/wrpl

依赖：Python 3.8+ 与 `zstandard`（pip install zstandard）。只读回放，从不写回放文件。

注意：本会话的沙箱里，子进程（python）对**既有**目录（tools/、data/_scratch/）只读，
只能写工作区根目录与根目录下新建的目录；因此产物默认落在 ace-combat/build/wrpl。

所有字段偏移都是在本局 `2026.10.18 05.45.06.wrpl` 上实测标定的；
格式来源与可信度见 `ace-combat/docs/REPLAY-FORMAT.md`。
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import re
import struct
import sys

DEFAULT_REPLAY = None  # pass --replay <path/to/replay.wrpl>
DEFAULT_OUT_DIR = os.path.join("build", "wrpl")

# --- 已实测的常量 ---------------------------------------------------------

#: .wrpl 头部定长（字段宽度累加所得）
HEADER_BYTES = 1224
#: 头部与 settings BLK 之间的固定 2 字节
HEADER_PAD_AFTER = 2
#: zstd 帧魔数
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
#: 时间戳单位：**毫秒**（u32 LE，位于包体 body[2:6]）。
#: 参考实现 wrpl-inspector 的 packetReader.go 也按毫秒读；
#: 实测 EndMarker = 589949 ms == 内嵌 timePlayed 589.94928 s
TICKS_PER_SECOND = 1000
#: 历史 raw 计数与毫秒的比值：raw = ms * 256（见 raw_of 的说明）
RAW_TICKS_PER_MS = 256
RAW_TICKS_PER_SECOND = RAW_TICKS_PER_MS * TICKS_PER_SECOND

#: 包类型（wrpl crate 的 ReplayPacketType）
PACKET_TYPES = {
    0: "EndMarker",
    1: "StartMarker",
    2: "AircraftSmall",
    3: "Chat",
    4: "MPI",
    5: "NextSegment",
    6: "ECS",
    7: "Snapshot",
    8: "ReplayHeaderInfo",
}

#: MPI (ptype=4) 载荷前 4 字节签名 -> 含义
MPI_SIGNATURES = {
    "00025878": "award 奖项（wrpl crate 已实现）",
    "00025858": "kill 击杀（wrpl crate 已实现）",
    "00025874": "model info / steering（wrpl crate 注释）",
    "00035843": "model info / turret angles（wrpl crate 注释）",
    "00025839": "unit/template 定义（本局标定：载荷内含 1 字节长度前缀的单位名）",
    "00025856": "单位载具装载/出生（本局标定：载荷内含载具名）",
    "00025857": "单位载具装载/出生（同上，另一变体）",
    "000058df": "任务/环境变量（载荷内含 serverTime / sunDir / wind 等明文键名）",
}

#: BLK 文件类型（wt_blk `FileType`）
_BLK_TYPES = {0x00: "BBF", 0x01: "FAT", 0x02: "FAT_ZSTD",
              0x03: "SLIM", 0x04: "SLIM_ZSTD", 0x05: "SLIM_ZST_DICT"}

#: 像真名字的形状（滤掉「巧合被当成 1 字节长度前缀」的二进制片段）；
#: 另要求含 `_` 或 `/`，否则纯字母的随机片段会大量误报。
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(/[A-Za-z0-9_.]+)?$")


def _looks_like_name(s: str) -> bool:
    return len(s) >= 4 and _NAME_RE.match(s) is not None and ("_" in s or "/" in s)


# --- 小工具 ---------------------------------------------------------------


def read_uleb(buf: bytes, p: int):
    """读 ULEB128，返回 (值, 新偏移)。"""
    result = 0
    shift = 0
    while True:
        b = buf[p]
        p += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, p
        shift += 7
        if shift > 63:
            raise ValueError("ULEB128 过长，解析越界")


def read_cstring(buf: bytes, off: int, size: int) -> str:
    """读定长 NUL 结尾字符串字段。"""
    raw = buf[off:off + size]
    zero = raw.find(b"\x00")
    if zero >= 0:
        raw = raw[:zero]
    return raw.decode("utf-8", "replace")


def ascii_runs(buf: bytes, min_len: int = 4):
    """返回 buf 里所有可打印 ASCII 串，用于探索性提取。"""
    return [m.group().decode("utf-8", "replace")
            for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, buf)]


def find_len_prefixed_ascii(buf: bytes, start: int = 0, min_len: int = 3, max_len: int = 64):
    """找「1 字节长度前缀 + ASCII 串」的第一处，返回 (偏移, 字符串)。"""
    i = start
    while i < len(buf):
        ln = buf[i]
        if min_len <= ln <= max_len and i + 1 + ln <= len(buf):
            cand = buf[i + 1:i + 1 + ln]
            if all(0x20 <= c < 0x7F for c in cand):
                return i, cand.decode("ascii")
        i += 1
    return -1, None


def extract_len_prefixed_ascii(buf: bytes, start: int = 0, min_len: int = 3, max_len: int = 64):
    """批出所有「1 字节长度前缀 + ASCII 串」，返回 [(偏移, 字符串)]。"""
    out = []
    i = start
    while i < len(buf):
        ln = buf[i]
        if min_len <= ln <= max_len and i + 1 + ln <= len(buf):
            cand = buf[i + 1:i + 1 + ln]
            if all(0x20 <= c < 0x7F for c in cand):
                out.append((i, cand.decode("ascii")))
                i += ln + 1
                continue
        i += 1
    return out


def _json_keys(node, prefix="", out=None, depth=0):
    """收集 BLK 树的键路径（只对样本取，深度上限 4，避免 32 份重复撑大文件）。"""
    if out is None:
        out = []
    if depth > 4:
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            path = "%s.%s" % (prefix, k) if prefix else k
            out.append(path)
            _json_keys(v, path, out, depth + 1)
    return out


def wrap_json_text(text: str, width: int = 118) -> str:
    """把紧凑 JSON 按结构边界折成短行（结果仍是合法 JSON，只是多了换行）。"""
    breaks = ",{}[]:"
    out = []
    cur = []
    in_str = False
    esc = False
    n = 0
    for ch in text:
        cur.append(ch)
        n += 1
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        if n >= width and not in_str and ch in breaks:
            out.append("".join(cur))
            cur = []
            n = 0
    if cur:
        out.append("".join(cur))
    return "\n".join(out) + "\n"


def write_json(path: str, obj) -> None:
    """写 JSON（先紧凑序列化，再按结构边界折行）。"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(wrap_json_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"))))


class _BytesReader:
    """给 zstandard.stream_reader 用的最小文件对象。"""

    def __init__(self, data: bytes):
        self._d = data
        self._p = 0

    def read(self, n=-1):
        if n is None or n < 0:
            out = self._d[self._p:]
            self._p = len(self._d)
            return out
        out = self._d[self._p:self._p + n]
        self._p += len(out)
        return out

    def readable(self):
        return True


# --- 1. 文件头 ------------------------------------------------------------


def parse_replay_header(d: bytes) -> dict:
    """按 wrpl crate `parse_header` 的字段宽度解析 .wrpl 定长头部。"""
    o = 0

    def u32():
        nonlocal o
        v = struct.unpack_from("<I", d, o)[0]
        o += 4
        return v

    def u64():
        nonlocal o
        v = struct.unpack_from("<Q", d, o)[0]
        o += 8
        return v

    def fixed(n):
        nonlocal o
        v = read_cstring(d, o, n)
        o += n
        return v

    hdr = {}
    hdr["magic"] = u32()
    hdr["version"] = u32()
    hdr["level"] = fixed(128)
    hdr["levelSettings"] = fixed(260)
    hdr["battleType"] = fixed(128)
    hdr["environment"] = fixed(128)
    hdr["visibility"] = fixed(32)
    hdr["rezOffset"] = u32()
    diff = d[o]
    o += 1
    hdr["difficultyRawByte"] = diff
    hdr["difficultyUnknownHighNibble"] = (diff >> 4) & 0x0F
    hdr["difficultyLowNibble"] = diff & 0x0F
    o += 35
    hdr["sessionType"] = u32()
    o += 4
    hdr["sessionId"] = u64()
    o += 4
    hdr["mSetSize"] = u32()
    hdr["settingsSize"] = u32()
    o += 28
    hdr["locName"] = fixed(128)
    hdr["startTime"] = u32()
    hdr["timeLimit"] = u32()
    hdr["scoreLimit"] = u32()
    o += 48
    hdr["battleClass"] = fixed(128)
    hdr["battleKillStreak"] = fixed(128)

    if o != HEADER_BYTES:
        raise ValueError("头部解析出 %d 字节，与预期的 %d 不符" % (o, HEADER_BYTES))
    hdr["magicHex"] = "0x%08x" % hdr["magic"]
    hdr["sessionIdHex"] = "0x%016x" % hdr["sessionId"]
    try:
        hdr["startTimeUtc"] = datetime.datetime.fromtimestamp(
            hdr["startTime"], datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OSError, OverflowError, ValueError):
        hdr["startTimeUtc"] = None
    return hdr


# --- 2. 包流（zstd 帧 + VLQ 包帧） ----------------------------------------


def locate_packet_stream(d: bytes, hdr: dict) -> dict:
    """定位 settings BLK 与包流。"""
    settings_off = HEADER_BYTES + HEADER_PAD_AFTER
    settings_end = settings_off + hdr["settingsSize"]
    rez = hdr["rezOffset"]
    if not (0 < rez <= len(d)):
        rez = None
    search_end = rez if rez else len(d)
    frame = d.find(ZSTD_MAGIC, settings_end, search_end)
    info = {
        "headerBytes": HEADER_BYTES,
        "settingsOffset": settings_off,
        "settingsSize": hdr["settingsSize"],
        "packetStreamOffset": settings_end,
        "zstdFrameOffset": frame if frame >= 0 else None,
        "resultsOffset": rez,
        "resultsBytes": (len(d) - rez) if rez else None,
        "compression": "unknown",
    }
    if frame >= 0:
        info["compression"] = "zstd"
        info["zstdFrameBytes"] = search_end - frame
        info["gapBeforeZstdBytes"] = frame - settings_end
    elif d[settings_end:settings_end + 1] in (b"\x78", b"\x68", b"\x58"):
        info["compression"] = "zlib"
    return info


def decompress_stream(d: bytes, info: dict) -> bytes:
    """解压包流。本局为单个 zstd 帧；更早的回放可能是 zlib。"""
    if info["compression"] == "zstd":
        try:
            import zstandard  # type: ignore
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise SystemExit("需要 `zstandard`：pip install zstandard") from exc
        raw = d[info["zstdFrameOffset"]:info["resultsOffset"]]
        return zstandard.ZstdDecompressor().stream_reader(_BytesReader(raw)).read()
    if info["compression"] == "zlib":
        import zlib
        return zlib.decompressobj().decompress(
            d[info["packetStreamOffset"]:info["resultsOffset"]])
    raise SystemExit("无法识别的包流压缩格式（既不是 zstd 也不是 zlib）")


def read_vlq_size(buf: bytes, p: int):
    """包长前缀（wrpl crate `read_vlq_size` 的位模式）。"""
    b = buf[p]
    p += 1
    if b & 0x80:
        return b & 0x7F, p
    if b & 0x40:
        return ((b << 8) | buf[p]) ^ 0x4000, p + 1
    if b & 0x20:
        return ((b << 16) | (buf[p] << 8) | buf[p + 1]) ^ 0x200000, p + 2
    if b & 0x10:
        return ((b << 24) | (buf[p] << 16) | (buf[p + 1] << 8) | buf[p + 2]) ^ 0x10000000, p + 3
    return int.from_bytes(buf[p:p + 4], "little"), p + 4


def parse_packets(payload: bytes):
    """解出全部包，返回 [(ptype, tick, headerLen, body)]；body 含包头。"""
    out = []
    p, last, n = 0, 0, len(payload)
    while p < n:
        try:
            size, p = read_vlq_size(payload, p)
        except IndexError:
            break
        if size < 0 or p + size > n:
            break
        body = payload[p:p + size]
        p += size
        t = body[0]
        if t & 0x10:                       # 时间戳与上一包相同
            # 包头最少 **2 字节**：body[0]=类型，body[1] 恒 0x00
            # （本局 86773 个包全部满足；参考实现 wrpl-inspector 也是读 2 字节）
            ptype, tick, hl = t ^ 0x10, last, 2
        else:
            # 带时间戳时包头 6 字节：类型 u8 + 0x00 u8 + u32 LE **毫秒**
            ptype, tick = t, int.from_bytes(body[2:6], "little")
            hl = 6
        last = tick
        out.append((ptype, tick, hl, body))
    return out


def ms_of(tick: int) -> float:
    """时间戳字段值 -> 毫秒。字段本身就是毫秒，这里只是保留函数名。"""
    return tick * 1000.0 / TICKS_PER_SECOND


def raw_of(ms: int) -> int:
    """毫秒 -> **历史 raw 计数**（raw = ms * 256）。

    早期把包头误当成 1 字节时，读到的是 body[1:5] = 0x00 加时间戳低 3 字节，
    等价于 `ms << 8`。这个编号不是文件里的字段，但 mission-data.js 的
    `replayTick` / `tracks.json` 都沿用了它，所以这里同时导出两种口径。
    """
    return ms * RAW_TICKS_PER_MS


# --- 3. FAT BLK 解包 ------------------------------------------------------


_BLK_SIZES = {0x04: 8, 0x05: 12, 0x06: 16, 0x07: 8, 0x08: 12, 0x0B: 48, 0x0C: 8}
_BLK_FMT = {0x04: "2f", 0x05: "3f", 0x06: "4f", 0x07: "2i", 0x08: "3i", 0x0B: "12f"}
_BLK_INLINE = (0x02, 0x03, 0x09, 0x0A)


def parse_fat_blk(raw: bytes):
    """解包 wt_blk 的 FAT 二进制 BLK。返回 (树, 元信息)。

    格式来源：https://github.com/Warthunder-Open-Source-Foundation/wt_blk
    的 `src/blk/mod.rs` 文档与 `src/blk/binary_deserialize/parser.rs`（AGPL-3.0）。
    """
    if not raw:
        raise ValueError("空的 BLK")
    ftype = raw[0]
    if _BLK_TYPES.get(ftype) != "FAT":
        raise ValueError("BLK 首字节 0x%02x（%s），本函数只处理 FAT"
                         % (ftype, _BLK_TYPES.get(ftype, "未知")))
    buf = raw[1:]

    names_count, p = read_uleb(buf, 0)
    names_size, p = read_uleb(buf, p)
    names_start = p
    names = []
    q = names_start
    for _ in range(names_count):
        z = buf.index(b"\x00", q)
        names.append(buf[q:z].decode("utf-8", "replace"))
        q = z + 1
    p = names_start + names_size

    blocks_count, p = read_uleb(buf, p)
    params_count, p = read_uleb(buf, p)
    params_size, p = read_uleb(buf, p)
    params_data = buf[p:p + params_size]
    p += params_size
    params_info = buf[p:p + params_count * 8]
    p += params_count * 8
    block_info = buf[p:]

    def value(tid: int, data: int):
        if tid in _BLK_INLINE:
            raw4 = struct.pack("<I", data)
            if tid == 0x02:
                return struct.unpack("<i", raw4)[0]
            if tid == 0x03:
                return round(struct.unpack("<f", raw4)[0], 6)
            if tid == 0x09:
                return data != 0
            return list(raw4)                      # Color r,g,b,a
        size = _BLK_SIZES.get(tid)
        seg = params_data[data:data + size] if size else params_data[data:]
        if tid == 0x01:                            # String
            z = seg.find(b"\x00")
            if z >= 0:
                seg = seg[:z]
            return seg.decode("utf-8", "replace")
        if tid == 0x0C:                            # Long
            return struct.unpack("<q", seg)[0]
        fmt = _BLK_FMT.get(tid)
        if fmt is None:
            return "<未知类型 0x%02x>" % tid
        return [round(x, 6) for x in struct.unpack("<" + fmt, seg)]

    # 字段定义：name_id u24 LE | type_id u8 | data u32 LE；字段名 **0 基**索引名字表
    params = []
    for i in range(params_count):
        chunk = params_info[i * 8:(i + 1) * 8]
        name_id = chunk[0] | (chunk[1] << 8) | (chunk[2] << 16)
        tid = chunk[3]
        data = int.from_bytes(chunk[4:8], "little")
        name = names[name_id] if name_id < len(names) else "<越界 %d>" % name_id
        params.append((name, value(tid, data)))

    # 嵌套表：每块 ULEB name_id / ULEB param_count / ULEB child_count
    #        /（child_count>0 时）ULEB first_child_idx。块名 **1 基**索引名字表，0 = root。
    bp = 0
    flat = []
    ptr = 0
    for _ in range(blocks_count):
        name_id, bp = read_uleb(block_info, bp)
        param_count, bp = read_uleb(block_info, bp)
        child_count, bp = read_uleb(block_info, bp)
        first_child = 0
        if child_count > 0:
            first_child, bp = read_uleb(block_info, bp)
        bname = "root" if name_id == 0 else (
            names[name_id - 1] if name_id - 1 < len(names) else "<越界 %d>" % name_id)
        fields = params[ptr:ptr + param_count]
        ptr += param_count
        flat.append({"name": bname, "fields": fields,
                     "children": child_count, "first": first_child})

    def build(idx: int):
        node = flat[idx]
        out = {}
        for k, v in node["fields"]:
            out[k] = v
        for j in range(node["first"], node["first"] + node["children"]):
            child = flat[j]
            sub = build(j)
            key = child["name"]
            if key in out:
                if not isinstance(out[key], list):
                    out[key] = [out[key]]
                out[key].append(sub)
            else:
                out[key] = sub
        return out

    meta = {"fileType": "FAT", "namesCount": names_count, "namesSize": names_size,
            "blocksCount": blocks_count, "paramsCount": params_count,
            "paramsDataSize": params_size, "blockInfoBytes": len(block_info),
            "consumedBytes": len(raw)}
    return build(0), meta


# --- 4. 结算表合并 --------------------------------------------------------


def _players_info_items(results: dict):
    root = ((results.get("uiScriptsData") or {}).get("playersInfo") or {})
    if isinstance(root, dict):
        return list(root.values())
    if isinstance(root, list):
        return root
    return []


def merge_players(results: dict):
    """把 player[] 与 uiScriptsData.playersInfo 合成一张玩家表。

    playersInfo 以 `__int_<userId>` 为键；合并后不丢字段，只是不再保持 BLK 原始嵌套。
    """
    raw = results.get("player") or []
    if not isinstance(raw, list):
        raw = [raw]
    by_id = {}
    for val in _players_info_items(results):
        if isinstance(val, dict) and "id" in val:
            by_id[str(val["id"])] = val

    out = []
    for pl in raw:
        if not isinstance(pl, dict):
            continue
        uid = str(pl.get("userId", ""))
        info = by_id.get(uid, {})
        crafts = info.get("crafts") or {}
        vehicles = []
        if isinstance(crafts, dict):
            for slot, name in sorted(crafts.items(), key=lambda kv: str(kv[0])):
                vehicles.append(name)
        ci = info.get("crafts_info")
        ci0 = ci.get("array0") if isinstance(ci, dict) else None
        out.append({
            "userId": uid,
            "name": info.get("name") or pl.get("name"),
            "clanTag": info.get("clanTag") or pl.get("clanTag"),
            "clanId": info.get("clanId"),
            "platform": info.get("platform"),
            "team": pl.get("team"),
            "squadId": pl.get("squadId"),
            "slot": info.get("slot"),
            "country": info.get("country"),
            "tier": info.get("tier"),
            "elo": info.get("elo"),
            "waitTimeSec": info.get("wait_time"),
            "kills": pl.get("kills"),
            "groundKills": pl.get("groundKills"),
            "humanKills": pl.get("humanKills"),
            "navalKills": pl.get("navalKills"),
            "teamKills": pl.get("teamKills"),
            "aiKills": pl.get("aiKills"),
            "aiGroundKills": pl.get("aiGroundKills"),
            "aiNavalKills": pl.get("aiNavalKills"),
            "assists": pl.get("assists"),
            "deaths": pl.get("deaths"),
            "captureZone": pl.get("captureZone"),
            "damageZone": pl.get("damageZone"),
            "score": pl.get("score"),
            "awardDamage": pl.get("awardDamage"),
            "missileEvades": pl.get("missileEvades"),
            "shellInterceptions": pl.get("shellIntgerceptions"),
            "vehicles": vehicles,
            "primaryCraftType": (ci0 or {}).get("type") if isinstance(ci0, dict) else None,
        })
    return out


def player_vehicles(results: dict):
    """每名玩家的载具（含 crafts_info 的类型与槽位）。"""
    out = []
    for val in _players_info_items(results):
        if not isinstance(val, dict):
            continue
        crafts = val.get("crafts") or {}
        veh = []
        if isinstance(crafts, dict):
            for slot, name in sorted(crafts.items(), key=lambda kv: str(kv[0])):
                veh.append({"slot": str(slot), "craft": name})
        ci = val.get("crafts_info")
        ci0 = ci.get("array0") if isinstance(ci, dict) else None
        out.append({
            "userId": str(val.get("id", "")),
            "name": val.get("name"),
            "clanTag": val.get("clanTag"),
            "team": val.get("team"),
            "slot": val.get("slot"),
            "country": val.get("country"),
            "tier": val.get("tier"),
            "vehicles": veh,
            "primaryCraft": (ci0 or {}).get("name") if isinstance(ci0, dict) else None,
            "primaryCraftType": (ci0 or {}).get("type") if isinstance(ci0, dict) else None,
        })
    return out


def scan_kill_text(payload: bytes):
    """回放里是否存在本地化击杀原文？对全流做字节搜索，计数作为反证。"""
    pats = [b"destroyed", b"shot down", b"has been", b"rammed", b"critically",
            b"\xe5\x87\xbb\xe8\x90\xbd",            # 击落 (UTF-8)
            b"\xe6\x91\xa7\xe6\xaf\x81",            # 摧毁
            b"hudmsg", b"battleLog"]
    return {p.decode("utf-8", "replace"): payload.count(p) for p in pats}


# --- 5. 主流程 ------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description="从 .wrpl 提取头部/结算 JSON/玩家表/单位表")
    ap.add_argument("--replay", default=DEFAULT_REPLAY, help="回放文件路径")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="输出目录")
    ap.add_argument("--quiet", action="store_true", help="不打印摘要")
    args = ap.parse_args(argv)

    replay = os.path.abspath(args.replay)
    if not os.path.isfile(replay):
        raise SystemExit("找不到回放文件：%s" % replay)
    with open(replay, "rb") as fh:
        d = fh.read()

    hdr = parse_replay_header(d)
    info = locate_packet_stream(d, hdr)
    payload = decompress_stream(d, info)
    packets = parse_packets(payload)

    census = collections.Counter()
    mpi_sigs = collections.Counter()
    for ptype, _tick, hl, body in packets:
        census[ptype] += 1
        if ptype == 4 and len(body) >= hl + 4:
            mpi_sigs[body[hl:hl + 4].hex()] += 1

    results, blk_meta = parse_fat_blk(d[hdr["rezOffset"]:])
    players = merge_players(results)
    vehicles = player_vehicles(results)

    ticks = [t for _p, t, _h, _b in packets if t]
    start_tick = end_tick = None
    for ptype, tick, _hl, _body in packets:
        if ptype == 1:
            start_tick = tick
        elif ptype == 0:
            end_tick = tick
    clock = {
        "timestampField": "包体 body[2:6] 的 u32 LE，单位毫秒",
        "msPerSecond": TICKS_PER_SECOND,
        "rawTicksPerMs": RAW_TICKS_PER_MS,
        "rawTicksPerSecond": RAW_TICKS_PER_SECOND,
        "secondsPerRawTick": 1.0 / RAW_TICKS_PER_SECOND,
        "formula": "秒 = tsMs / 1000；tsMs = 包体 body[2:6] 的 u32 LE；raw = tsMs * 256",
        "unitConvention": (
            "事件对象里 tick = raw（= tsMs×256），与 mission-data.js 的 replayTick、"
            "tracks.json 的 tick 同口径；tsMs 才是文件里的字段值；sec = tsMs/1000。"
            "两者精确互换：tick = tsMs*256，tsMs = tick/256（整除，无舍入）。"),
        "deprecatedKeyNames": {"ticksPerSecond": "rawTicksPerSecond",
                               "secondsPerTick": "secondsPerRawTick",
                               "tickQuantum": "(已移除：时间戳无量化)",
                               "firstPacketTick": "firstPacketRaw",
                               "startMarkerTick": "startMarkerRaw",
                               "endMarkerTick": "endMarkerRaw"},
        "firstPacketMs": min(ticks) if ticks else None,
        "firstPacketRaw": raw_of(min(ticks)) if ticks else None,
        "startMarkerMs": start_tick,
        "startMarkerRaw": None if start_tick is None else raw_of(start_tick),
        "startMarkerSec": None if start_tick is None else start_tick / 1000.0,
        "endMarkerMs": end_tick,
        "endMarkerRaw": None if end_tick is None else raw_of(end_tick),
        "endMarkerSec": None if end_tick is None else end_tick / 1000.0,
        "matchSecondsFromResults": results.get("timePlayed"),
        "evidence": ("EndMarker 包体 body[2:6] = 589949 ms，与内嵌 timePlayed 589.94928 s 逐位吻合；"
                     "StartMarker = 7822 ms。历史口径的 raw = ms×256 来自早期把包头误当 1 字节"
                     "（读到 body[1:5] = 0x00 + 时间戳低 3 字节 = ms<<8）；早期报告里"
                     "『差值最大公约数 = 256』『EndMarker tick 151026944』都是那个错位读法的产物，"
                     "不是文件字段的性质。"),
    }

    s_off, s_size = info["settingsOffset"], info["settingsSize"]
    settings_raw = d[s_off:s_off + s_size]
    settings = {
        "offset": s_off,
        "size": s_size,
        "firstByteHex": "0x%02x" % settings_raw[0] if settings_raw else None,
        "fileTypeInWtBlk": _BLK_TYPES.get(settings_raw[0]) if settings_raw else None,
        "decoded": False,
        "plaintextStrings": ascii_runs(settings_raw, 4),
        "note": "首字节不在 wt_blk FileType 枚举（0x00..0x05）内，未按 FAT/SLIM 解包；键名明文可见。",
    }

    unit_names = collections.Counter()
    unit_names_raw = 0
    spawn_events = []
    kill_packets = []
    awards = collections.OrderedDict()
    chats = []
    for ptype, tick, hl, body in packets:
        pl = body[hl:]
        if ptype == 3 and len(pl) >= 1:
            slen = pl[0]
            sender = pl[1:1 + slen].decode("utf-8", "replace")
            rest = pl[1 + slen:]
            msg = ""
            if rest:
                mlen = rest[0]
                msg = rest[1:1 + mlen].decode("utf-8", "replace")
            chats.append({"tick": raw_of(tick), "tsMs": tick,
                          "sec": round(tick / 1000.0, 3),
                          "sender": sender, "message": msg})
            continue
        if ptype != 4 or len(pl) < 4:
            continue
        # 参考实现 wrpl-inspector 把 PacketPayload[0:4] 当签名；本脚本的 pl 就是 PacketPayload
        # （两边都按 2 字节包头）。MPI 消息号 = u16 LE = pl[2] | pl[3] << 8。
        sig = pl[:4].hex()
        msg_id = pl[2] | (pl[3] << 8)
        if msg_id == 0xD039:                    # 旧记法 00025839 → DeferredReplicatedObjectCreate
            for _off, nm in extract_len_prefixed_ascii(pl, 3):
                unit_names_raw += 1
                if _looks_like_name(nm):
                    unit_names[nm] += 1
        elif msg_id in (0xF056, 0xF157):        # 旧记法 00025856 / 00025857
            # → TextCriticalHitReport / SevereDamage：字段含载具名与玩家 uid，不是「出生事件」
            off, veh = find_len_prefixed_ascii(pl, 9)
            if veh:
                spawn_events.append({
                    "tick": raw_of(tick), "tsMs": tick,
                    "sec": round(tick / 1000.0, 3),
                    "msgId": "0x%04X" % msg_id, "legacySignature": sig,
                    "vehicle": veh, "strOffset": off,
                    "prefixBytes": pl[3:9].hex(),
                })
        elif msg_id == 0xF058:                  # 旧记法 00025858 → TextKillReport
            off, veh = find_len_prefixed_ascii(pl, 8)
            kill_packets.append({
                "tick": raw_of(tick), "tsMs": tick,
                "sec": round(tick / 1000.0, 3),
                "msgId": "0x%04X" % msg_id, "legacySignature": sig,
                "strOffset": off, "vehicle": veh,
                "prefixBytes": pl[3:13].hex(),
            })
        elif msg_id == 0xF078:                  # 旧记法 00025878 → TextStreak
            _off, nm = find_len_prefixed_ascii(pl, 5)
            key = nm or "<无>"
            rec = awards.setdefault(key, {"award": nm, "count": 0,
                                          "firstTsMs": tick,
                                          "lastTsMs": tick,
                                          "firstSec": round(tick / 1000.0, 3),
                                          "lastSec": round(tick / 1000.0, 3),
                                          "firstTicksRaw": []})
            rec["count"] += 1
            rec["lastTsMs"] = tick
            rec["lastSec"] = round(tick / 1000.0, 3)
            if len(rec["firstTicksRaw"]) < 6:
                rec["firstTicksRaw"].append(raw_of(tick))

    name_hits = {}
    for p in players:
        nm = p["name"]
        if not nm:
            continue
        nb = nm.encode("utf-8")
        hits = [{"ptype": ptype, "tick": raw_of(tick), "tsMs": tick,
                 "sig": body[hl:hl + 4].hex()}
                for ptype, tick, hl, body in packets if nb in body]
        if hits:
            name_hits[nm] = hits

    slots = collections.OrderedDict()
    for p in players:
        if p.get("slot") is not None:
            slots.setdefault(str(p["slot"]), []).append(p["name"])
    # 伤害包里的载具名 -> 结算表里同型载具的玩家（交叉核对用，不是唯一映射）
    veh_link = collections.OrderedDict()
    for p in players:
        for v in p.get("vehicles") or []:
            veh_link.setdefault(v, []).append(p["name"])
    damage_vehicle_to_players = collections.OrderedDict()
    for ev in spawn_events:
        damage_vehicle_to_players[ev["vehicle"]] = veh_link.get(ev["vehicle"], [])

    provenance = [
        "wrpl crate 0.10.0 源码（AGPL-3.0）https://docs.rs/crate/wrpl/0.10.0/source/"
        "src/header.rs|packet.rs|parser.rs|results.rs —— 头部字段宽度、包帧格式、"
        "MPI 签名、rez_offset 用途、结算 JSON 结构",
        "wt_blk 0.3.1（AGPL-3.0）https://github.com/Warthunder-Open-Source-Foundation/wt_blk "
        "src/blk/mod.rs 与 src/blk/binary_deserialize/parser.rs —— FAT 二进制 BLK 布局",
        "本局回放 2026.10.18 05.45.06.wrpl 实测标定 —— tick/256=ms、zstd 帧起点、"
        "00025839 / 00025856 / 00025857 载荷内容",
    ]
    not_found = [
        "每名玩家的独立 timePlayed：结算 JSON 只有对局级 timePlayed；playersInfo.wait_time 是等待时长",
        "entityId ↔ 玩家名 的映射表：包里没有任何玩家名（除聊天包）",
        "击杀事件的本地化原文：全流搜索 destroyed / shot down / has been / 击落 / 摧毁 均为 0",
        "载具显示名：回放里只有内部 id（f_15a / su_25 / fa_18c_early 等）",
        "settings BLK 解析：首字节 0x49 不在 wt_blk 的 FileType 枚举内",
    ]

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    players_info_sample = _players_info_items(results)
    key_sample = {
        "player[0]": (results.get("player") or [{}])[0] if isinstance(results.get("player"), list)
        else results.get("player"),
        "playersInfo[sample]": players_info_sample[0] if players_info_sample else {},
    }
    header_json = {
        "schema": "wrpl-header/1",
        "generatedBy": "ace-combat/tools/wrpl_header.py",
        "generatedAtUtc": now,
        "replayFile": os.path.basename(replay),
        "replayBytes": len(d),
        "container": dict(info, decompressedBytes=len(payload), packets=len(packets)),
        "header": hdr,
        "clock": clock,
        "settingsBlk": settings,
        "resultsBlk": blk_meta,
        "results": {
            "status": results.get("status"),
            "timePlayed": results.get("timePlayed"),
            "authorUserId": results.get("authorUserId"),
            "author": results.get("author"),
            "matchingInfo": results.get("matchingInfo"),
            "player": results.get("player"),
            "_note": "uiScriptsData.playersInfo 的字段（含 crafts 载具）已全部并入本文件 players[] "
                     "与 entities.json/playerVehicles，未丢弃；matchingInfo 本局为空对象。"
                     "player[] 保持 BLK 原样。",
        },
        "resultsKeyPaths": sorted(set(_json_keys(key_sample))),
        "players": players,
        "playerCount": len(players),
        "packetCensus": {
            "total": len(packets),
            "byType": {PACKET_TYPES.get(k, str(k)): v for k, v in sorted(census.items())},
            "byTypeRaw": {str(k): v for k, v in sorted(census.items())},
            "mpiSignatures": {k: v for k, v in mpi_sigs.most_common()},
            "mpiSignatureMeaning": MPI_SIGNATURES,
        },
        "provenance": provenance,
        "notFound": not_found,
    }

    entity_json = {
        "schema": "wrpl-entities/1",
        "generatedBy": "ace-combat/tools/wrpl_header.py",
        "generatedAtUtc": now,
        "replayFile": os.path.basename(replay),
        "unitNameTable": {
            "signature": "00025839",
            "packets": sum(v for k, v in mpi_sigs.items() if len(k) == 8
                           and (int(k[4:6], 16) | (int(k[6:8], 16) << 8)) == 0xD039),
            "rawStringCount": unit_names_raw,
            "distinctNames": len(unit_names),
            "names": [{"name": k, "count": v} for k, v in unit_names.most_common()],
            "note": "载荷里是 1 字节长度前缀的 ASCII 单位/模板名；不含玩家名，也不含 entityId 数值。"
                    "过滤条件：长度≥4、形如标识符、且含 `_` 或 `/`。",
        },
        "playerVehicles": vehicles,
        "spawnVehicleEvents": spawn_events,
        "damageVehicleToPlayers": damage_vehicle_to_players,
        "playerSlots": slots,
        "killPackets": kill_packets,
        "awards": list(awards.values()),
        "chat": chats,
        "playerNamesInPacketStream": name_hits,
        "killTextSearch": scan_kill_text(payload),
        "conclusions": {
            "playerNamesInReplay": "只在内嵌结算 BLK 与 ptype=3 聊天包（sender 字段）里出现",
            "killLocalizedText": "不存在；击杀文本包（TextKillReport 0xF058）里是结构化字段，"
                                 "奖项包（TextStreak 0xF078）里是内部键名，都没有本地化原文",
            "killCoordinate": "TextKillReport 载荷里字段 3/4 是 VictimUid/KillerUid（u16，&0x7FF），"
                              "要拿坐标得先用 ECS 的 UID->Entity 表解析实体，再查该实体航迹；"
                              "本脚本没有实现 ECS",
            "entityIdMapping": "参考实现靠 ECS 的 UID->Entity 表（EntityManager.Uid_lookup，"
                               "UID 取 u16 低 11 位 &0x7FF）把伤害/击杀包关联到实体；"
                               "本脚本没实现 ECS，所以仍未解出 entityId ↔ 玩家。"
                               "注意：这些包里的玩家编号是 PID(u32)/UID(u16)，与 playersInfo.slot"
                               "（本局只取 0..6）不是一回事。",
            "damagePackets": "旧记法 00025856 = TextCriticalHitReport、00025857 = SevereDamage，"
                             "字段为 OffendedUID/PlayerPID/Vehicle/PlayerUID(/Fire)；"
                             "它们**不是**「载具出生事件」，spawnVehicleEvents 这个名字沿用旧版，"
                             "语义应理解为「伤害报告里的载具名」",
            "playerToVehicle": "playerVehicles 来自内嵌结算 JSON 的 crafts/crafts_info，"
                               "是**离线可用的玩家↔载具真值**（不依赖 8111）",
        },
        "provenance": provenance,
        "notFound": not_found,
    }

    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    h_path = os.path.join(out_dir, "header.json")
    e_path = os.path.join(out_dir, "entities.json")
    write_json(h_path, header_json)
    write_json(e_path, entity_json)

    if not args.quiet:
        print("回放: %s (%d 字节)" % (replay, len(d)))
        print("容器: 头部 %d B, settings @%d(%d B), zstd 帧 @%s, 解压 %d B, 结算 @%s"
              % (info["headerBytes"], info["settingsOffset"], info["settingsSize"],
                 info["zstdFrameOffset"], len(payload), info["resultsOffset"]))
        print("包: %d 个, 类型分布 %s" % (len(packets), dict(sorted(census.items()))))
        print("时钟: 秒 = tick/256000；StartMarker %.3f s, EndMarker %.3f s, 结算 timePlayed %.3f s"
              % (clock["startMarkerSec"] or -1, clock["endMarkerSec"] or -1,
                 results.get("timePlayed") or -1))
        print("玩家表: %d 人" % len(players))
        print("单位名 %d 个（原始候选 %d）；载具出生事件 %d 条；击杀包 %d 条；奖项 %d 种；聊天 %d 条"
              % (len(unit_names), unit_names_raw, len(spawn_events), len(kill_packets),
                 len(awards), len(chats)))
        print("写出: %s" % h_path)
        print("写出: %s" % e_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
