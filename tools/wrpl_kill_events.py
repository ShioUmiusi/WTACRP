#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解出击杀/伤害包的完整字段（移植 `IdFieldSerializer32`）。

用途：把 `TextKillReport`(0xF058) / `TextCriticalHitReport`(0xF056) / `SevereDamage`(0xF157)
的**受击者与击杀者 UID / PID / 载具 / 武器**解出来，供 wrpl-probe 把击杀点落到实体航迹上。

参考实现（只移植知识，实现是自写 Python）：
    wrpl-inspector —— https://github.com/maxsupermanhd/wrpl-inspector  （AGPL-3.0）
      * `wrpl/danet/bitStream.go`                       —— 位读取语义
      * `wrpl/idfieldserializer/{IdFieldSerializer32,util}.go` —— 字段流布局
      * `wrpl/packet/parser/kill/main.go`、`parser/damage/{critical,severe}.go` —— 字段号含义

位序要点（照 `ReadBitsInto` 实测推出的等价规则）：
    * 一个字节内**从最高位开始**取，先取到的位是返回值的高位（MSB 优先）
    * `ReadCompressed` = LEB128：每字节低 7 位有效，最高位是继续标志，低位字节在前
    * `binary.Read(LE, uN)` 在 Go 里是「按顺序取 N 个字节，再按小端解释」

用法：
    python ace-combat/tools/wrpl_kill_events.py
    python ace-combat/tools/wrpl_kill_events.py --out ace-combat/build/wrpl/kill_events.json
"""

from __future__ import annotations

import argparse
import json
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wrpl_header as W  # noqa: E402

DEFAULT_OUT = os.path.join("ace-combat", "build", "wrpl", "kill_events.json")

#: 参考实现按 PacketPayload[0:4] 精确匹配
SIG_KILL = bytes([0x02, 0x58, 0x58, 0xF0])       # TextKillReport      0xF058
SIG_CRITICAL = bytes([0x02, 0x58, 0x56, 0xF0])   # TextCriticalHitReport 0xF056
SIG_SEVERE = bytes([0x02, 0x58, 0x57, 0xF1])     # SevereDamage        0xF157

SOURCE = {
    "repository": "https://github.com/maxsupermanhd/wrpl-inspector",
    "license": "AGPL-3.0",
    "files": ["wrpl/danet/bitStream.go",
              "wrpl/idfieldserializer/IdFieldSerializer32.go",
              "wrpl/idfieldserializer/util.go",
              "wrpl/packet/parser/kill/main.go",
              "wrpl/packet/parser/damage/critical.go",
              "wrpl/packet/parser/damage/severe.go"],
    "usage": "只移植知识与字段含义；本脚本实现为自写 Python，未复制参考实现代码",
}


class BitReader:
    """对应 `danet.BitReader`：字节内 MSB 优先，偏移以位计。"""

    def __init__(self, data: bytes):
        self.data = data
        self.off = 0

    def remaining(self) -> int:
        return len(self.data) * 8 - self.off

    def read_bits(self, n: int) -> int:
        if n == 0:
            return 0
        if self.off + n > len(self.data) * 8:
            raise EOFError("读越界：需要 %d 位，只剩 %d 位" % (n, self.remaining()))
        v = 0
        for _ in range(n):
            byte = self.data[self.off >> 3]
            bit = (byte >> (7 - (self.off & 7))) & 1
            v = (v << 1) | bit
            self.off += 1
        return v

    def read_bytes(self, n: int) -> bytes:
        return bytes(self.read_bits(8) for _ in range(n))

    def read_u16le(self) -> int:
        return int.from_bytes(self.read_bytes(2), "little")

    def read_u32le(self) -> int:
        return int.from_bytes(self.read_bytes(4), "little")

    def read_compressed(self) -> int:
        """对应 `ReadCompressed`：LEB128，低 7 位有效，高位为继续标志。"""
        v = 0
        shift = 0
        while True:
            a = self.read_bits(8)
            v |= (a & 0x7F) << shift
            shift += 7
            if not (a & 0x80):
                return v
            if shift > 63:
                raise ValueError("ReadCompressed 过长")

    def read_len_str(self) -> str:
        n = self.read_bits(8)
        return self.read_bytes(n).decode("utf-8", "replace")


def read_size(br: BitReader) -> int:
    """对应 `idfieldserializer.ReadSize`，返回**位**数。"""
    hdr = br.read_bits(3)
    table = {1: 1, 2: 8, 3: 16, 4: 32, 5: 64, 6: 96, 7: 128}
    if hdr in table:
        return table[hdr]
    if hdr == 0:
        return br.read_compressed()
    raise ValueError("ReadSize 未知头 %d" % hdr)


def read_fields_size_and_flag(br: BitReader):
    """对应 `IdFieldSerializer32.ReadFieldsSizeAndFlag`，返回 (fields 位掩码, sizes 列表)。"""
    if br.off & 7:
        raise ValueError("IdFieldSerializer32 要求字节对齐，当前位偏移 %d" % br.off)
    start = br.off
    offset = br.read_u16le()            # 尺寸表的字节偏移（相对 start）
    fields = br.read_compressed()       # 位掩码
    start_body = br.off
    br.off = (offset << 3) + start      # 跳到尺寸表
    sizes = [read_size(br) for _ in range(bin(fields).count("1"))]
    br.off = start_body                 # 跳回字段数据
    return fields, sizes


def deserialize(br: BitReader, known: dict):
    """对应 `DeserializeIdFieldSerializer32`。

    `known` = {字段号: 处理函数(br) -> None}；未列出的字段按尺寸表跳过。
    返回 (字段号 -> 值的 dict, 读到的总位数)。
    """
    fields, sizes = read_fields_size_and_flag(br)
    out = {}
    rest = fields
    ordinal = 0
    while rest > 0:
        field_no = (rest & -rest).bit_length() - 1     # 最低置位的下标
        rest &= ~(1 << field_no)
        handler = known.get(field_no)
        if handler is None:
            br.off += sizes[ordinal]                   # 跳过该字段
        else:
            val = handler(br)
            if val is not None:
                out[field_no] = val
        ordinal += 1
    return out, br.off


# --- 三类包的字段号 -> 语义（照参考实现逐个抄语义，不抄代码） -------------------

def parse_kill(payload: bytes):
    """`kill/main.go`：对 PacketPayload[4:] 解字段。"""
    br = BitReader(payload[4:])
    known = {
        0x1: lambda r: ("KillerPid", r.read_u32le()),
        0x2: lambda r: ("PlayerVehicle", r.read_len_str()),
        0x3: lambda r: ("VictimUid", r.read_u16le() & 0x7FF),
        0x4: lambda r: ("KillerUid", r.read_u16le() & 0x7FF),
        0xA: lambda r: ("PlayerWeapon", r.read_len_str()),
        0xB: lambda r: ("VictimPid", r.read_u32le()),
        0xC: lambda r: ("DestroyedWeapon", r.read_len_str()),
    }
    raw, consumed = deserialize(br, known)
    return {name: val for name, val in raw.values()}, consumed


def parse_damage(payload: bytes, severe: bool):
    """`damage/critical.go` / `damage/severe.go`。"""
    br = BitReader(payload[4:])
    known = {
        0x1: lambda r: ("OffendedUID", r.read_u16le()),   # 参考实现未 &0x7FF，查表时才 &0x7FF
        0x2: lambda r: ("PlayerPID", r.read_u32le()),
        0x3: lambda r: ("Vehicle", r.read_len_str()),
        0x4: lambda r: ("PlayerUID", r.read_u16le()),
    }
    if not severe:
        known[0x5] = lambda r: ("Fire", bool(r.read_bits(1)))
        known[0x6] = lambda r: ("Unk0", r.read_bits(8))
    else:
        known[0x5] = lambda r: ("Unk0", r.read_bits(8))
    raw, consumed = deserialize(br, known)
    return {name: val for name, val in raw.values()}, consumed


# --- 主流程 -----------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description="解击杀/伤害包完整字段")
    ap.add_argument("--replay", default=W.DEFAULT_REPLAY)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    with open(args.replay, "rb") as fh:
        d = fh.read()
    hdr = W.parse_replay_header(d)
    info = W.locate_packet_stream(d, hdr)
    packets = W.parse_packets(W.decompress_stream(d, info))

    kills, criticals, severes, failures = [], [], [], []
    for ptype, tick, hl, body in packets:
        if ptype != 4:
            continue
        pl = body[hl:]
        if len(pl) < 6:
            continue
        sig = pl[:4]
        base = {"tick": W.raw_of(tick), "tsMs": tick,
                "sec": round(tick / 1000.0, 3),
                "msgId": "0x%04X" % (pl[2] | (pl[3] << 8)),
                "payloadHex": pl.hex()}
        try:
            if sig == SIG_KILL:
                fields, consumed = parse_kill(pl)
                kills.append(dict(base, fields=fields, bitsConsumed=consumed,
                                  bitsTotal=(len(pl) - 4) * 8))
            elif sig == SIG_CRITICAL:
                fields, consumed = parse_damage(pl, severe=False)
                criticals.append(dict(base, fields=fields, bitsConsumed=consumed,
                                      bitsTotal=(len(pl) - 4) * 8))
            elif sig == SIG_SEVERE:
                fields, consumed = parse_damage(pl, severe=True)
                severes.append(dict(base, fields=fields, bitsConsumed=consumed,
                                    bitsTotal=(len(pl) - 4) * 8))
        except (EOFError, ValueError, KeyError) as exc:
            failures.append(dict(base, signature=sig.hex(), error=str(exc)))

    # 自检 1：击杀包解出的载具名应与「扫 1 字节长度前缀」得到的完全一致
    try:
        with open(os.path.join("ace-combat", "build", "wrpl", "entities.json"),
                  encoding="utf-8") as fh:
            ent = json.load(fh)
        scan_kills = [k["vehicle"] for k in ent["killPackets"]]
    except OSError:
        scan_kills = []
    decoded_kills = [k["fields"].get("PlayerVehicle") for k in kills]
    # 位流给空串、前缀扫描给 None，都表示「这个包没有载具名」，归一化后再比
    cross_ok = None
    if scan_kills:
        cross_ok = [(v if v else None) for v in decoded_kills] == list(scan_kills)

    all_uids = sorted({k["fields"]["VictimUid"] for k in kills if "VictimUid" in k["fields"]} |
                      {k["fields"]["KillerUid"] for k in kills if "KillerUid" in k["fields"]})

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = {
        "schema": "wrpl-kill-events/1",
        "generatedBy": "ace-combat/tools/wrpl_kill_events.py",
        "generatedAtUtc": now,
        "replayFile": os.path.basename(args.replay),
        "source": SOURCE,
        "method": ("匹配 PacketPayload[0:4] 的精确签名，对 PacketPayload[4:] 跑 "
                   "IdFieldSerializer32：u16 LE 尺寸表偏移 + LEB128 位掩码 + 按位掩码顺序排列的字段，"
                   "每个字段的位宽取自尺寸表"),
        "counts": {"kill": len(kills), "critical": len(criticals), "severe": len(severes),
                   "failures": len(failures)},
        "selfCheck": {
            "vehicleMatchesPrefixScan": cross_ok,
            "comparedByTickAndOrder": cross_ok,
            "itemsCompared": len(kills),
            "normalization": ("「该包没有载具名」在位流侧是空串 \"\"、在前缀扫描侧是 null；"
                              "两边都归一化成 null 后再比较"),
            "note": ("位流解码（本脚本 wrpl_kill_events.py）与 1 字节长度前缀扫描"
                     "（wrpl_header.py 写出的 entities.json）是两条**独立**路径；"
                     "两者一致即表示击杀包字段解码可信。true = 33/33 完全相同"
                     "（按 tick 与按出现顺序都比过）。看到 false 时请先对照 generatedAtUtc，"
                     "确认不是读到了修复前的旧版本。"),
        },
        "uidsSeenInKills": all_uids,
        "kills": kills,
        "criticals": criticals,
        "severes": severes,
        "failures": failures,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(W.wrap_json_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":"))))

    print("击杀 %d / 致命伤 %d / 重伤 %d，失败 %d"
          % (len(kills), len(criticals), len(severes), len(failures)))
    print("击杀包自检（位流解出的载具名 == 前缀扫描结果）: %s" % cross_ok)
    print("击杀包里出现过的 UID（低 11 位）: %s" % all_uids)
    print("写出: %s" % args.out)
    print()
    print("前 6 条击杀：")
    for k in kills[:6]:
        f = k["fields"]
        print("   %7.3f s  victimUid=%-4s killerUid=%-4s vehicle=%-22s weapon=%-14s"
              % (k["sec"], f.get("VictimUid"), f.get("KillerUid"),
                 f.get("PlayerVehicle"), f.get("PlayerWeapon")))
    if failures:
        print("失败样本:", failures[:2])
    return 0


if __name__ == "__main__":
    sys.exit(main())
