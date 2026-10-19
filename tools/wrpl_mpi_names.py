#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 MPI 消息 id 与参考实现里的名字对上，导出 `mpi_names.json`。

参考实现：
    wrpl-inspector —— https://github.com/maxsupermanhd/wrpl-inspector
    许可：AGPL-3.0（`wrpl/header.go` 等文件头载明 Copyright (C) 2025 flexcoral）
    名字表来源文件：`inspector/tabs/mpipackets/names.go`
    （该文件自注：表本身来自 https://github.com/moleium/wrpl 的 modules/packets.hpp）

我们只**移植知识**（消息 id ↔ 名字这份对应关系是事实性数据），
本脚本与 `tools/wrpl_header.py` 的实现都是自写的 Python，没有复制参考实现的代码。

关键对齐（本局实测标定，见 REPLAY-FORMAT.md §5）：
    ptype=4 载荷布局 = [u8 ?][u8 ?][u8 0x58][u8 idLo][u8 idHi]…
    MPI 消息 id = u16 LE = payload[3] | payload[4] << 8
参考实现读的 4 字节「signature」= 我们的 payload[1:5]，最后两字节就是 id（小端）。
wrpl crate 用 payload[0:4] 当签名，实际上**错开了一位**。

用法：
    python ace-combat/tools/wrpl_mpi_names.py
    python ace-combat/tools/wrpl_mpi_names.py --names-go <path/to/names.go>
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wrpl_header as W  # noqa: E402  （同目录的解析库）

DEFAULT_NAMES_GO = os.path.join(
    "ace-combat", "build", "wrpl-inspector",
    "inspector", "tabs", "mpipackets", "names.go")
DEFAULT_OUT = os.path.join("ace-combat", "build", "wrpl", "mpi_names.json")

SOURCE_REPO = "https://github.com/maxsupermanhd/wrpl-inspector"
SOURCE_FILE = "inspector/tabs/mpipackets/names.go"
SOURCE_LICENSE = "AGPL-3.0"
SOURCE_NOTE = ("名字表由 wrpl-inspector 的 names.go 转写，该文件自注其数据来自 "
               "https://github.com/moleium/wrpl 的 modules/packets.hpp")

#: id -> 名字。与参考实现 names.go 的 `MPINames` 一致；可用 --names-go 重新解析覆盖。
MPI_NAMES = {
    0xB00C: "InfTroopSync",
    0xB00E: "ReplicatedObjectCreate",
    0xB013: "DoFlakExplosion",
    0xB01D: "SemChangeAircraftRequest",
    0xB01E: "PpChangeAircraftResponse",
    0xB02C: "SetTimeSpeedEx",
    0xB062: "SpectateRequestId",
    0xB063: "ControlsConfirmation",
    0xB065: "FmwControlsPacket",
    0xB066: "FmwAuthorityApprovedState",
    0xB068: "MissionEvent",
    0xB06E: "UnitBulletHitPartCount",
    0xB07B: "MissionProgressDataReceived",
    0xB07D: "MissionProgressTimeDataReceived",
    0xB08B: "VehicleControlsPacket",
    0xB08C: "VehicleAuthorityApprovedState",
    0xB091: "AppUnitVersionMismatch",
    0xB092: "AppUnitVersionMatch",
    0xB095: "GmDesyncStats",
    0xB097: "GmDoEscape",
    0xB0A8: "UnitDesyncData",
    0xB0C5: "UnitRequestWinch",
    0xB0CE: "WalkerControlsPacket",
    0xB0D1: "ResponseChallenge",
    0xB0D3: "ShowUpObjToTeamResponse",
    0xB0D4: "ShowUpObjToTeamRequest",
    0xB0DC: "RocketControlsPacket",
    0xB0E5: "ConfirmAuthorityApprovedState",
    0xB0F5: "UnitHighlight",
    0xB107: "EACClientToServerMsg",
    0xB11D: "CloudsTracesData",
    0xB12E: "TerraformIntegrityCheckDataRequest",
    0xB145: "HumanControlsPacket",
    0xD01F: "PlayFlakExplosionVisual",
    0xD020: "GmSetGunAngles",
    0xD039: "DeferredReplicatedObjectCreate",
    0xD043: "ReplayCameraParams",
    0xD046: "UnitReloadGun",
    0xD047: "FmwRpm",
    0xD04A: "ReplayCockpitParams",
    0xD08E: "PlayExplosionVisual",
    0xD0AD: "UnitDelayedStatusChange",
    0xD0FE: "ProjectileHitReplay",
    0xD136: "DeferredReflectionData",
    0xD137: "UnitDataSnapshot",
    0xD146: "ReplayVrHandsState",
    0xF016: "GmGroundExplosionSmokeEffect",
    0xF017: "GmGroundExplosionFireEffect",
    0xF018: "GmDoInactive",
    0xF01A: "GmDoSingleShotReliable",
    0xF01C: "GmDoStopFire",
    0xF026: "TurretYawImmobile",
    0xF027: "TurretPitchImmobile",
    0xF028: "RunTriggerAction",
    0xF02D: "ReflectionData",
    0xF02F: "UnitRequestRespawn",
    0xF032: "UnitRequestRepair",
    0xF033: "UnitRepair",
    0xF037: "UnitDetected",
    0xF038: "OwnerPlayerViewDirection",
    0xF03B: "HintPlayFromScript",
    0xF04B: "OnUnitDead",
    0xF04D: "UnitDoExplosion",
    0xF050: "UnitBailoutResponse",
    0xF053: "UnitRequestRearm",
    0xF054: "UnitRearm",
    0xF055: "MissionFailOrSuccess",
    0xF056: "TextCriticalHitReport",
    0xF058: "TextKillReport",
    0xF05D: "FadeToDebriefing",
    0xF06A: "DialogMessageAction",
    0xF06B: "ResetGroundModelPositionDeltas",
    0xF071: "UnitReloadGuns",
    0xF073: "GroundModelPositions",
    0xF074: "GroundModelPositionsServerReplay",
    0xF078: "TextStreak",
    0xF07A: "MissionProgressData",
    0xF07C: "MissionProgressTimeData",
    0xF08D: "FmwAuthorityApprovedPartialState",
    0xF08F: "UnitAttachEffectRelative",
    0xF091: "GmCutPartResponse",
    0xF099: "PlayLandcrashExplosions",
    0xF09A: "DvmUnreliableHpReflectionData",
    0xF09B: "DvmUnreliableHpReflectionReply",
    0xF09C: "EconUpdateWeapon",
    0xF0A0: "GmDoSingleShotUnreliable",
    0xF0A3: "VehicleAuthorityApprovedPartialState",
    0xF0A6: "UnitArtilleryShootingAtPos",
    0xF0AA: "RedundancyReflectionData",
    0xF0AB: "DvmCutDecor",
    0xF0B1: "UnitSingleShot",
    0xF0B3: "DebugSphere",
    0xF0B6: "SmartCutsceneFinished",
    0xF0B8: "PlayAreaEffects",
    0xF0BB: "UnitRequestDamageModelEvents",
    0xF0BC: "UnitResponseDamageModelEvents",
    0xF0BD: "UnitBulletRearm",
    0xF0C2: "UnitLastEffectiveHit",
    0xF0C3: "MessageStartGame",
    0xF0C8: "UseOrderRequest",
    0xF0CA: "UnitRequestRepairAssist",
    0xF0CB: "DvmDamageDataForReferee",
    0xF0D2: "OrderResult",
    0xF0D5: "UnitToggleGunners",
    0xF0D6: "GmDoStartFireWithDist",
    0xF0D8: "HudMarkMapSquare",
    0xF0DB: "ShellsData",
    0xF0DD: "GmRequestToggleCurWeapon",
    0xF0DF: "CustomWeatherParams",
    0xF0E1: "GmRequestChangeCrew",
    0xF0E2: "GmRequestRepair",
    0xF0E3: "RocketAuthorityApprovedState",
    0xF0E6: "GmApplyExplosionImpulse",
    0xF0E9: "UnitHitEffects",
    0xF0EC: "MissionPlayHint",
    0xF0ED: "UnitPickupItem",
    0xF0F0: "UnitSetTimeFuseDistance",
    0xF0F6: "UnitScoutResult",
    0xF0F7: "InteractiveObjectAuthorityApprovedState",
    0xF0F8: "InteractiveObjectAuthorityApprovedPartialState",
    0xF0F9: "UnitRefuel",
    0xF0FD: "UnitRequestChangeShotFreq",
    0xF101: "UnitRequestRepairWithoutMod",
    0xF103: "ConsoleUnitCommand",
    0xF104: "TextHitReport",
    0xF109: "GmEngineOnOff",
    0xF10A: "UnitChangeNightVision",
    0xF10C: "RecreateTorpedoes",
    0xF10F: "SquadTargetDesignationRequest",
    0xF118: "UnitDamagePartKill",
    0xF119: "GmToggleOptics",
    0xF11A: "ShellsDataServerReplay",
    0xF11E: "UnitRequestSwitchOnSupport",
    0xF11F: "GmRequestToggleStealth",
    0xF120: "UnitFriendlyFire",
    0xF121: "ActiveProtectionSystemTriggering",
    0xF122: "TerraformPatchAlt",
    0xF123: "TerraformData",
    0xF124: "GmToggleTerraform",
    0xF125: "GmDoWeaponLockOnReliable",
    0xF126: "GmDoWeaponLockOnUnreliable",
    0xF127: "UnitRequestExtinguishAssist",
    0xF128: "UnitRequestExtinguishWithoutMod",
    0xF129: "UnitResponseSwitchOnSupport",
    0xF12C: "ECSNetUnitsData",
    0xF12D: "ECSNetUnitsDataServerReplay",
    0xF12F: "TerraformIntegrityCheckDataResponce",
    0xF130: "UnitSmokeScreen",
    0xF131: "UnitChangeCurShortcut",
    0xF132: "ShotFailedApsWorking",
    0xF133: "UnitOnExplosion",
    0xF134: "UnitRequestChangeSupportPlane",
    0xF135: "UnitSupportPlaneAttackCommand",
    0xF13B: "DvmDamageDataForReplay",
    0xF13D: "GmCutAllWreckedParts",
    0xF140: "UnitRequestUnlimitedControl",
    0xF142: "TargetDesignationMark",
    0xF143: "SetBenchmarkMode",
    0xF144: "UnitOnHit",
    0xF147: "UnitDoProximityExplosion",
    0xF148: "ForceMusic",
}

_RE_ENTRY = re.compile(r"^\s*(0x[0-9A-Fa-f]{4})\s*:\s*\"([^\"]+)\"\s*,", re.M)


def parse_names_go(path: str):
    """从参考实现的 names.go 里解析 `MPINames` 表。解析失败返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        return None
    out = {}
    for m in _RE_ENTRY.finditer(src):
        out[int(m.group(1), 16)] = m.group(2)
    return out or None


def mpi_id(payload: bytes):
    """ptype=4 载荷（= 参考实现的 PacketPayload）-> MPI 消息 id。

    参考实现按 `[u8 kind][u8 0x58][u16 LE msgId][body]` 布局，
    所以 id = payload[2] | payload[3] << 8。
    """
    if len(payload) < 4:
        return None
    return payload[2] | (payload[3] << 8)


def main(argv=None):
    ap = argparse.ArgumentParser(description="MPI 消息 id -> 参考实现名字")
    ap.add_argument("--replay", default=W.DEFAULT_REPLAY)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--names-go", default=DEFAULT_NAMES_GO,
                    help="参考实现的 names.go；能读就用它覆盖内嵌表")
    args = ap.parse_args(argv)

    names = dict(MPI_NAMES)
    names_src = "内嵌转写自 %s" % SOURCE_FILE
    parsed = parse_names_go(args.names_go)
    if parsed:
        names = parsed
        names_src = "运行时解析 %s" % args.names_go
        print("已从 %s 解析 %d 条名字" % (args.names_go, len(parsed)))
    else:
        print("未能读取 %s（沙箱可能拒绝子进程访问该目录），使用内嵌转写表 %d 条"
              % (args.names_go, len(names)))

    with open(args.replay, "rb") as fh:
        d = fh.read()
    hdr = W.parse_replay_header(d)
    info = W.locate_packet_stream(d, hdr)
    payload = W.decompress_stream(d, info)
    packets = W.parse_packets(payload)

    counts = collections.Counter()
    lens = collections.defaultdict(collections.Counter)
    legacy = {}
    first_sec = {}
    last_sec = {}
    for ptype, tick, hl, body in packets:
        pl = body[hl:]
        if ptype != 4:
            continue
        mid = mpi_id(pl)
        if mid is None:
            continue
        counts[mid] += 1
        lens[mid][len(pl) - 4] += 1          # 去掉 4 字节消息头（kind + 0x58 + id u16）
        legacy.setdefault(mid, pl[:4].hex())
        sec = tick * 1000.0 / W.TICKS_PER_SECOND / 1000.0
        if mid not in first_sec:
            first_sec[mid] = round(sec, 3)
        last_sec[mid] = round(sec, 3)

    rows = []
    for mid, cnt in counts.most_common():
        rows.append({
            "msgId": "0x%04X" % mid,
            "name": names.get(mid, "<未收录>"),
            "count": cnt,
            "firstSec": first_sec[mid],
            "lastSec": last_sec[mid],
            "payloadLenModes": [[l, c] for l, c in lens[mid].most_common(3)],
            "legacy4ByteSignature": legacy[mid],
        })
    unnamed = [r for r in rows if r["name"] == "<未收录>"]
    unused_names = sorted(k for k in names if k not in counts)

    doc = {
        "schema": "wrpl-mpi-names/1",
        "generatedBy": "ace-combat/tools/wrpl_mpi_names.py",
        "generatedAtUtc": datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "replayFile": os.path.basename(args.replay),
        "idExtraction": "ptype=4 载荷 -> u16 LE = payload[3] | payload[4] << 8；"
                        "参考实现读的 4 字节 signature 等于我们的 payload[1:5]",
        "source": {
            "repository": SOURCE_REPO,
            "file": SOURCE_FILE,
            "license": SOURCE_LICENSE,
            "dataOriginNote": SOURCE_NOTE,
            "namesLoadedFrom": names_src,
            "usage": "只移植知识（id ↔ 名字对应关系），解析实现为自写 Python",
        },
        "totalMpiPackets": sum(counts.values()),
        "distinctIds": len(counts),
        "messages": rows,
        "idsNotInReferenceTable": [r["msgId"] for r in unnamed],
        "referenceNamesNotSeenInThisReplay": ["0x%04X" % k for k in unused_names],
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(W.wrap_json_text(json.dumps(doc, ensure_ascii=False,
                                             separators=(",", ":"))))

    print("MPI 包 %d 个，%d 个不同的 msgId；写了 %s"
          % (doc["totalMpiPackets"], doc["distinctIds"], args.out))
    print("本局出现但参考表未收录：%s" % (doc["idsNotInReferenceTable"] or "无"))
    for r in rows[:14]:
        print("   %-8s %-34s n=%6d  长度众数=%s" %
              (r["msgId"], r["name"], r["count"], r["payloadLenModes"][:1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
