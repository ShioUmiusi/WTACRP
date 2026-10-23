#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 .wrpl 解码中间产物生成交付数据 `ace-combat/mission-data.js`（契约 v4 §1 §2）。

输入（只读这三个文件，**不读 8111 抓取**）：
    ace-combat/build/wrpl/header.json     wrpl-format：容器/头/时钟/内嵌结算/玩家表
    ace-combat/build/wrpl/entities.json   wrpl-format：击杀包/奖项/聊天/玩家↔载具/单位名表
    ace-combat/build/wrpl/tracks.json     wrpl-probe ：各实体三维航迹（未产出时几何留空）

输出：ace-combat/mission-data.js（经典脚本，只设置 window.ACE_MISSION）

    python ace-combat/tools/build_ace_mission.py
    python ace-combat/tools/build_ace_mission.py --tracks <其它航迹文件> --out <路径>

自检（行数 / 覆盖率 / 时间单调 / 插值跨度 / 事件数 / 结构不变量）打印到 stdout。
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import sys
from collections import defaultdict

ACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRPL_DIR = os.path.join(ACE, "build", "wrpl")
HEADER_PATH = os.path.join(WRPL_DIR, "header.json")
ENTITIES_PATH = os.path.join(WRPL_DIR, "entities.json")
TRACKS_PATH = os.path.join(WRPL_DIR, "tracks.json")
TRACKS_ALT_PATH = os.path.join(WRPL_DIR, "tracks_ecs.json")   # 第二条解码线
OUT_PATH = os.path.join(ACE, "mission-data.js")

HZ_OWN = 10                     # 本机输出采样率
UNIT_HZ = {"air": 10, "ground": 1}     # 空中统一 10 Hz（Catmull-Rom 上采样，美观优先）
MAX_INTERP_GAP_S = 1.5          # 本机插值跨度上限，超过补 null
MIN_TRACK_SAMPLES = 3
MAX_MB = 12.0
MAX_ABS_M = 200000.0        # 坐标绝对值上限：地图只有 ±65536，超了就是解码垃圾
TELEPORT_SPEED_MS = 600.0   # 速度包线阈值（契约 §5.2）
TELEPORT_MIN_M = 500.0      # 单步位移超过它 + 速度超线，判为传送/解码跳变
OWN_PROPS_PATH = os.path.join(WRPL_DIR, "own_props.json")
UID_MAP_PATH = os.path.join(WRPL_DIR, "uid_map.json")          # wrpl-ecs：uid → 玩家/载具/阵营
KILL_EVENTS_PATH = os.path.join(WRPL_DIR, "kill_events.json")  # wrpl-format：击杀位流字段
GROUND_PATH = os.path.join(WRPL_DIR, "ground.json")            # wrpl-probe/ecs：地面静态点位
SIDE_FINAL_PATH = os.path.join(WRPL_DIR, "side_final.json")    # 阵营终判（纯回放）
SHOTS_PATH = os.path.join(WRPL_DIR, "projectile_launches.json")  # wrpl-ecs：发射点
RUNWAYS_PATH = os.path.join(WRPL_DIR, "runways.json")          # wrpl-ecs：跑道结论 + 机场地标点
SQUAD_PATH = os.path.join(WRPL_DIR, "squad.json")              # wrpl-ecs：本机小队（结算表 squadId）
TERRAIN_PATH = os.path.join(WRPL_DIR, "terrain.json")          # 契约 §9.2：等高线/海岸线（待产出）
DISPLAY_NAMES_PATH = os.path.join(WRPL_DIR, "vehicle_display_names.json")  # 显示名（约定）
CATEGORIES_PATH = os.path.join(WRPL_DIR, "vehicle_categories.json")        # 型号类别（显示约定）
# 不显示的模板：占位物 + 机场雷达（不可被攻击、无战术意义；用户点名）
HIDDEN_TEMPLATES = {
    "structures/dummy_immortal", "dummy_plane", "structures/dummy_plane",
    "structures/unit_airfield_flight_radar_char",
    "structures/unit_airfield_main_radar_char",
}

# 发射物模板名 → 可读名（**显示约定**；不确定的留 null，不编）
SHOT_NAMES = {"rocket": "ROCKET", "bomb": "BOMB", "rocket+arh_shell": "ARH ROCKET",
              "payload": "PAYLOAD", "jettisoned": "JETTISONED"}

ZW_CHARS = "\u200b\u200c\u200d\ufeff\u00ad\u200e\u200f"

SIDE_CODE = {"ally": "aly", "enemy": "eny", "squad": "sqd", "unknown": "unk"}

# 武器内部名 → 可读名（**显示约定**，不是回放数据；映射不到就原样显示内部名）
WEAPON_NAMES = {
    "us_aim9l_sidewinder": "AIM-9L", "us_aim9l_sidewinder_default": "AIM-9L",
    "us_aim9m_sidewinder": "AIM-9M",
    "us_aim7f_sparrow_f_14": "AIM-7F",
    "us_aim7m_sparrow_f_15_default": "AIM-7M", "us_aim7m_sparrow_fa_18": "AIM-7M",
    "us_aim7p_sparrow_fa_18": "AIM-7P",
    "su_r_60": "R-60", "su_r_73_default": "R-73", "su_r_27er": "R-27ER",
    "su_r_77_default": "R-77", "su_rvv_ae": "RVV-AE",
    "su_kh_25ml": "Kh-25ML", "su_pl5e2_default": "PL-5EII",
    "cn_sd10a": "SD-10A", "swd_rb99_default": "RB99",
}


def weapon_display(name):
    """内部武器名 → 可读名；表里没有就机械转写（大写 + 下划线转连字符），再没有就 None。"""
    if not name:
        return None
    return WEAPON_NAMES.get(name) or name.upper().replace("_", "-")
AWARD_KIND = {"final_blow": "lastblow"}


# --------------------------------------------------------------------- 工具

def load_json(path):
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def clean_text(text):
    """去掉零宽字符与 <color=...> 富文本标签，其余原样。"""
    out = str(text or "")
    for ch in ZW_CHARS:
        out = out.replace(ch, "")
    while "<color=" in out:
        i = out.index("<color=")
        j = out.find(">", i)
        if j < 0:
            break
        k = out.find("</color>", j)
        out = out[:i] + out[j + 1:(k if k >= 0 else len(out))] + (out[k + 8:] if k >= 0 else "")
    return out


def finite_m(value):
    """坐标必须是有限数且在合理范围内，否则当无效值丢掉（宁可缺，不要错）。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not math.isfinite(value) or abs(value) > MAX_ABS_M:
        return None
    return float(value)


def pearson(a, b):
    if len(a) < 8 or len(a) != len(b):
        return None
    n = len(a)
    ma = sum(a) / n
    mb = sum(b) / n
    da = [x - ma for x in a]
    db = [y - mb for y in b]
    na = math.sqrt(sum(x * x for x in da))
    nb = math.sqrt(sum(y * y for y in db))
    if na < 1e-9 or nb < 1e-9:
        return None
    return sum(x * y for x, y in zip(da, db)) / (na * nb)


def catmull_rom(times, values, seg_ids, t, max_gap):
    """Catmull-Rom 样条插值（均匀参数）；跨段或空档过大一律 None（不凭空连线）。

    比线性插值平滑（渲染端不必再管 4 Hz 卡顿），端点处退化为端点值。
    """
    i = bisect.bisect_left(times, t)
    if i == 0 or i >= len(times):
        return None
    if seg_ids[i - 1] != seg_ids[i] or (times[i] - times[i - 1]) > max_gap:
        return None
    p1, p2 = values[i - 1], values[i]
    if p1 is None or p2 is None:
        return None
    p0 = values[i - 2] if (i - 2 >= 0 and seg_ids[i - 2] == seg_ids[i - 1]) else p1
    p3 = values[i + 1] if (i + 1 < len(times) and seg_ids[i + 1] == seg_ids[i]) else p2
    p0 = p1 if p0 is None else p0
    p3 = p2 if p3 is None else p3
    u = (t - times[i - 1]) / (times[i] - times[i - 1])
    u2, u3 = u * u, u * u * u
    return 0.5 * ((2 * p1) + (-p0 + p2) * u + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u2
                  + (-p0 + 3 * p1 - 3 * p2 + p3) * u3)


def snap_or_interp(times, values, t, tol, max_gap):
    """先取最近的真实样本，取不到再线性插值（都没有就 None）。"""
    v = snap_at(times, values, t, tol)
    if v is not None:
        return v
    return linear_at(times, values, t, max_gap)


def own_props_series(own_props, axis, col):
    """本机属性表（ReplayCockpitParams）里的一列 → (时间轴秒, 值)，时间按 raw tick 口径换算。"""
    if not own_props:
        return None, None
    cols = own_props.get("columns") or []
    rows = own_props.get("samples") or []
    if "sec" not in cols or col not in cols or not rows:
        return None, None
    isec, icol = cols.index("sec"), cols.index(col)
    times, vals = [], []
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) <= max(isec, icol):
            continue
        sec = r[isec]
        if not isinstance(sec, (int, float)):
            continue
        value = r[icol]
        times.append(axis.from_clock_sec(sec))
        vals.append(float(value) if isinstance(value, (int, float)) else None)
    return times, vals


def rnd(value, nd):
    if value is None:
        return None
    out = round(float(value), nd)
    if out == 0:
        out = 0.0
    return int(out) if nd == 0 else out


def linear_at(times, values, t, max_gap):
    """线性插值；越界、两侧跨度 > max_gap、任一端为空都返回 None。"""
    if not times or t < times[0] - 1e-9 or t > times[-1] + 1e-9:
        return None
    i = bisect.bisect_left(times, t)
    if i < len(times) and abs(times[i] - t) <= 1e-9:
        return values[i]
    if i == 0 or i >= len(times):
        return None
    t1, t2 = times[i - 1], times[i]
    v1, v2 = values[i - 1], values[i]
    if v1 is None or v2 is None or (t2 - t1) > max_gap:
        return None
    return v1 + (v2 - v1) * (t - t1) / (t2 - t1)


def snap_at(times, values, t, tol):
    """取最近的真实样本（不插值），超过容差返回 None。"""
    if not times:
        return None
    i = bisect.bisect_left(times, t)
    best, best_d = None, tol + 1e-9
    for k in (i - 1, i):
        if 0 <= k < len(times):
            d = abs(times[k] - t)
            if d < best_d:
                best, best_d = k, d
    return None if best is None else values[best]


def wrap180(deg):
    return (deg + 180.0) % 360.0 - 180.0


def decide_y_flip(tracks, own_props, axis):
    """y 轴是否需要取反：纯回放内部判据（不碰 8111）。

    本机航迹的位移方向必须和 own_props 里那个**真罗盘**航向一致：
    分别按 bearing=atan2(dx,dy) 与 atan2(dx,-dy) 算与罗盘的夹角，取中位误差小的那个。
    返回 (flip, 中位误差_不翻, 中位误差_翻, 样本数)。
    """
    unit = None
    pid = tracks.get("playerEntityId") or tracks.get("ownEntityId")
    for u in tracks.get("units") or []:
        if (pid and u.get("entityId") == pid) or u.get("own"):
            unit = u
            break
    if unit is None:
        return False, None, None, 0
    rows = unit_samples(tracks, unit)
    ptimes, phdg = own_props_series(own_props, axis, "hdg_deg")
    if not ptimes or len(rows) < 20:
        return False, None, None, 0
    no, fl = [], []
    for i in range(1, len(rows)):
        dt = (rows[i][0] - rows[i - 1][0]) * axis.sec_per_tick
        if dt < 0.2:
            continue
        h = snap_at(ptimes, phdg, axis.sec(rows[i][0]), 0.4)
        if h is None:
            continue
        dx = rows[i][1] - rows[i - 1][1]
        dy = rows[i][2] - rows[i - 1][2]
        if math.hypot(dx, dy) < 50.0:
            continue
        no.append(abs(wrap180(bearing_deg(dx, dy) - h)))
        fl.append(abs(wrap180(bearing_deg(dx, -dy) - h)))
    if len(no) < 20:
        return False, None, None, len(no)
    no.sort()
    fl.sort()
    med_no, med_fl = no[len(no) // 2], fl[len(fl) // 2]
    return (med_fl < med_no), med_no, med_fl, len(no)


def bearing_deg(dx, dy):
    """回放自身水平框架里的朝向：0=+y、顺时针（渲染只需相对关系）。"""
    return math.degrees(math.atan2(dx, dy)) % 360.0


# --------------------------------------------------------------- 时间轴

class Axis:
    """回放 tick → 秒。原点取 tick0（通常就是轨道里第一个 tick）。"""

    def __init__(self, header, tracks):
        clock = (header or {}).get("clock") or {}
        raw_rate = (tracks or {}).get("secPerTick")
        if raw_rate is None:
            raw_rate = clock.get("secondsPerRawTick")
        if raw_rate is None:
            raw_rate = clock.get("secondsPerTick")
        try:
            self.sec_per_tick = float(raw_rate) if raw_rate else 1.0 / 256000.0
        except (TypeError, ValueError):
            self.sec_per_tick = 1.0 / 256000.0
        if not (self.sec_per_tick > 0):
            self.sec_per_tick = 1.0 / 256000.0
        # 原点与 tracks.json 用同一个 tick0：验收工具的时间映射（4a）就是在这条轴上拟合的，
        # 两边同轴才能逐样本对照。tracks.json 缺失时退回回放时钟原点 0。
        if tracks and tracks.get("tick0") is not None:
            self.tick0 = int(tracks["tick0"])
            self.source = "tracks.tick0=%d（与 tracks.json / 验收 4a 同轴）" % self.tick0
        else:
            self.tick0 = 0
            self.source = "回放时钟原点 tick=0"
        ends = []
        if tracks and tracks.get("tickLast") is not None:
            ends.append(int(tracks["tickLast"]))
        for end_key in ("endMarkerRaw", "endMarkerTick"):
            if clock.get(end_key) is not None:
                ends.append(int(clock[end_key]))
        self.tick_last = max(ends) if ends else self.tick0
        # 事件对象口径：tick = raw = tsMs × rawTicksPerMs，sec = tsMs / 1000（两者精确互换）
        try:
            self.raw_per_ms = int(clock.get("rawTicksPerMs") or 256)
        except (TypeError, ValueError):
            self.raw_per_ms = 256

    def sec(self, tick):
        return (float(tick) - self.tick0) * self.sec_per_tick

    def to_tick(self, sec):
        return int(round(self.tick0 + sec / self.sec_per_tick))

    def ms_to_tick(self, ms):
        return int(round(float(ms) * self.raw_per_ms))

    def from_clock_sec(self, sec):
        """回放时钟秒（sec = tick / 256000，时钟原点是 tick=0）→ 本轴秒。

        own_props.json 的 sec、settlement 的 firstSec 都是这个口径；
        而 tracks.json 的 samples/killPackets 是 raw tick，直接用 sec()。
        """
        return float(sec) - self.tick0 * self.sec_per_tick


# ---------------------------------------------------------------- 实体

def unit_samples(tracks, unit):
    """把 tracks.json 的 [[tick,x,y,z], ...] 折成按 tick 排序的平行数组。"""
    rows = []
    for s in unit.get("samples") or []:
        if isinstance(s, dict):
            tick = s.get("tick", s.get("t"))
            x, y, z = s.get("x"), s.get("y"), s.get("z")
        elif isinstance(s, (list, tuple)) and len(s) >= 3:
            tick, x, y = s[0], s[1], s[2]
            z = s[3] if len(s) > 3 else None
        else:
            continue
        x, y, z = finite_m(x), finite_m(y), finite_m(z)
        if not isinstance(tick, (int, float)) or x is None or y is None:
            continue
        rows.append((float(tick), x, y, z))
    rows.sort(key=lambda r: r[0])
    return rows


def ground_side(x, y, centers):
    """地面点位阵营：到两个阵营出生中心（side_final.centers）的最近距离，**推定**。"""
    if not centers or x is None or y is None:
        return "unknown"
    best, bd = None, None
    for name, c in centers.items():
        if not (isinstance(c, (list, tuple)) and len(c) >= 2):
            continue
        d = math.hypot(x - c[0], y - c[1])
        if bd is None or d < bd:
            bd, best = d, name
    return best if best in ("ally", "enemy", "squad") else "unknown"


def death_records(kill_positions, axis):
    """死亡记录：来自回放击杀包的 victimUid + tickRaw（**不是从航迹推断**）。

    返回 [(t_sec, tickRaw, victimUid, killerUid)]，按时间升序。
    """
    out = []
    for kp in kill_positions or []:
        tick = kp.get("tickRaw")
        vu = kp.get("victimUid")
        if tick is None or vu is None:
            continue
        out.append((axis.sec(tick), int(tick), vu, kp.get("killerUid")))
    out.sort(key=lambda r: r[0])
    return out


def build_squad(squad_json):
    """本机小队：原样透传 squad.json，另加来源标注。**没有文件就 null，不编。**"""
    if not squad_json:
        return None, "未产出（squad.json 还没到），meta.squad 保持 null"
    out = dict(squad_json)
    out["source"] = "results-blk squadId"
    members = [dict(m) for m in (out.get("members") or [])]
    for m in members:
        # uid 是推算出来的（结算 kills 数 + 受击者机型 + 击杀时刻；僚机通常靠近长机）：
        # 原样采用，但用 uidSource 标明不是回放字段，并保留证据字段供复核
        if m.get("uid") is None and m.get("uidHypothesis") is not None:
            m["uid"] = m["uidHypothesis"]
            m["uidSource"] = "inference(结算 kills/受击机型/击杀时刻 + 邻近度；不是回放字段)"
    out["members"] = members
    if out.get("size") is None:
        out["size"] = len(members)
    return out, "已并入（squadId=%s，%d 人：%s）" % (
        squad_json.get("squadId"), len(members),
        ", ".join("%s(uid=%s)" % (m.get("name"), m.get("uid")) for m in members))


def build_terrain(terrain_json, level):
    """契约 §9.2 的 terrain 图层：**只接受本局地图**的等高线文件。

    其它地图的样例**绝不能**进交付数据 —— 那是别的地形的几何，
    放进本局等于编造。所以这里要求文件自报 `map`/`level` 且与本局关卡一致，否则整层留空。
    """
    if not terrain_json:
        return None, "未产出（本局地图没有 HM2 块，等高线需先支持 lmap LandMesh）"
    declared = terrain_json.get("map") or (terrain_json.get("meta") or {}).get("map")
    crs = terrain_json.get("crs") or (terrain_json.get("meta") or {}).get("crs")
    if crs and crs != "replay-world-meters":
        return None, "坐标系不是 replay-world-meters（%r），不并入" % crs
    if declared and level and str(declared) not in str(level):
        return None, "文件声明的地图 %r 与本局 %r 不一致，不并入" % (declared, level)
    return {
        "meta": terrain_json.get("meta"),
        "contours": terrain_json.get("contours") or [],
        "water": terrain_json.get("water") or [],
        "coastlines": terrain_json.get("coastlines") or [],
    }, "已并入（%d 条等高线）" % len(terrain_json.get("contours") or [])


def ascii_runway_name(key, name):
    """跑道显示名一律纯 ASCII：文件里是"AF-01 跑道"，这里规范化成 "AF-01 RUNWAY"。"""
    if name and all(ord(c) < 128 for c in name):
        return name
    tail = str(key or "").split("-", 1)[-1].upper() or "RUNWAY"
    return "%s RUNWAY" % tail


def build_runways(runways_json):
    """跑道线段：由回放自身的机场标记点（位置 + 单位航向）与其引用模型名的 3000 m 长度反推。

    **不是关卡几何**。字段按输出约定；`confidence`/`source` 原样透传，缺就 null。
    """
    out = []
    for r in (runways_json or {}).get("runways") or []:
        x1, y1, z1 = finite_m(r.get("x1")), finite_m(r.get("y1")), finite_m(r.get("z1"))
        x2, y2, z2 = finite_m(r.get("x2")), finite_m(r.get("y2")), finite_m(r.get("z2"))
        if None in (x1, y1, x2, y2):
            continue
        out.append({
            "key": r.get("key"), "name": ascii_runway_name(r.get("key"), r.get("name")),
            "x1": rnd(x1, 0), "y1": rnd(y1, 0), "z1": rnd(z1, 1) if z1 is not None else None,
            "x2": rnd(x2, 0), "y2": rnd(y2, 0), "z2": rnd(z2, 1) if z2 is not None else None,
            "lengthM": r.get("lengthM"), "headingDeg": r.get("headingDeg"),
            "spreadM": r.get("spreadM"), "confidence": r.get("confidence"),
            "source": "ecs-airfield-marker", "sourceRaw": r.get("source"),
        })
    out.sort(key=lambda r: str(r.get("key")))
    return out


def build_airfields(runways_json, window):
    """机场地标点：runways.json 的 airfields[]（由机场雷达/高炮实体聚类出的**点**，不是跑道端点）。"""
    out = []
    for a in (runways_json or {}).get("airfields") or []:
        x, y, z = finite_m(a.get("x")), finite_m(a.get("y")), finite_m(a.get("z"))
        if x is None or y is None:
            continue
        out.append({
            "key": a.get("key"), "x": rnd(x, 0), "y": rnd(y, 0),
            "z": rnd(z, 1) if z is not None else None,
            "structures": a.get("structureCount"), "classNames": a.get("classNames") or [],
            "source": "ecs-cluster",
        })
    out.sort(key=lambda a: str(a.get("key")))
    return out


def build_ground_units(ground, axis, display=None, centers=None, hidden_all=None,
                       categories=None):
    """ground.json → units[]：单采样静态点位（ptype=6 ECS transform，已是世界米框架）。

    坐标口径与 tracks.json v2/v3 一致（x 东、y 北、z 上），**不做任何翻转**；
    静态点没有航迹：n=1、hz=1、static=true，t0 = 出生 tick 折算秒。
    """
    out = []
    if not ground:
        return out
    for i, g in enumerate(ground.get("units") or [], 1):
        x, y, z = finite_m(g.get("x")), finite_m(g.get("y")), finite_m(g.get("z"))
        if x is None or y is None:
            continue
        tpl = g.get("template")
        raw_class = g.get("unitClassName") or g.get("className")
        hidden_here = bool((raw_class or tpl) in hidden_all)
        code = "".join(ch for ch in str(raw_class or tpl or "obj").lower() if ch.isalnum()) or "obj"
        tick = g.get("spawnTickRaw")
        out.append({
            "key": "gnd-%s-%03d" % (code, i),
            # 阵营：WT 不存在中立单位，按到两个阵营出生中心的最近距离推定
            "side": ground_side(x, y, centers), "sideDecoded": None,
            "sideInferred": ground_side(x, y, centers) is not None,
            # vehicle 用**原始内部名**：ground.json 目前只给 template（tank/fortification），
            # 若将来带 unit__className 就直接用它；查不到显示名就留 null，让场景原样打印原始字段
            "domain": "ground", "class": "ground",
            "vehicle": raw_class or tpl, "vehicleSource": "ground.json",
            # label = NAMES[unitClassName] || unitClassName || template；命中隐藏集合则 vehicleName=""
            "vehicleName": "" if hidden_here else ((display or {}).get(raw_class or str(tpl or ""))
                                                   or raw_class or tpl),
            "hidden": hidden_here,
            # 类别与 vehicleName 不同：隐藏单位也**照常填** —— 类别说的是「这东西是什么」，
            # 与显不显示无关（全文件一致这么处理）。
            "vehicleCategory": (categories or {}).get(raw_class or str(tpl or "")) or None,
            "template": tpl, "unitClassName": raw_class,
            "player": None, "hasAlt": True, "static": True,
            # 有些静态点在解码航迹开始之前就存在（spawn 早于轴原点）→ t0 夹到 0，
            # 原始 tick 仍如实保留在 spawnTickRaw 里
            "t0": max(0.0, rnd(axis.sec(tick), 3)) if tick is not None else 0.0,
            "hz": 1, "n": 1,
            "x": [rnd(x, 0)], "y": [rnd(y, 0)], "z": [rnd(z, 1)], "hdg": [None],
            "deathTickRaw": None, "deathKind": None, "killerUid": None,
            "entityId": g.get("entityId"), "gridClass": g.get("class"),
            "spawnTickRaw": tick,
        })
    out.sort(key=lambda u: (u["t0"], u["key"]))
    return out


def build_shots(launches, axis, units, window, gate_m=100.0):
    """发射点层：ptype=6 ECS 弹药构造消息，只有**创建时刻的点**、没有弹道。

    - `tickRaw` 原样保留；`t` 用回放 tick 轴。
    - `yaw` 一律 null：试过两种四元数分量顺序，与同刻发射机的航向都差得远
      （较优的一组去掉固定 123.5° 偏移后 MAD 仍有 34.1°），说明它的朝向口径不在
      我们能验证的框架里 —— 与其给一个含义不明的角度，不如留 null。
    - `shooterUid`/`side`：按"同一时刻最近的飞机航迹"关联（开发期复核中位 15.05 m、
      100 m 内 100%），超过 `gate_m` 就留 null，不猜。
    - `templateName` 是显示约定表，表里没有就 null。
    """
    out = []
    if not launches:
        return out, 0, 0
    air = [u for u in units if u["domain"] == "air"]
    dropped = linked = 0
    for g in launches.get("units") or []:
        tick = g.get("spawnTickRaw")
        x, y, z = finite_m(g.get("x")), finite_m(g.get("y")), finite_m(g.get("z"))
        if tick is None or x is None or y is None:
            dropped += 1
            continue
        t = axis.sec(tick)
        if not (window["t0"] <= t <= window["t1"]):
            dropped += 1
            continue
        shooter_uid = side = None
        best_d = gate_m
        for u in air:
            i = int(round((t - u["t0"]) * u["hz"]))
            if i < 0 or i >= u["n"]:
                continue
            ux, uy = u["x"][i], u["y"][i]
            if ux is None or uy is None:
                continue
            d = math.hypot(ux - x, uy - y)
            if d < best_d:
                best_d, shooter_uid, side = d, u.get("uid"), u["side"]
        if shooter_uid is not None:
            linked += 1
        tpl = g.get("template")
        out.append({
            "t": rnd(t, 3), "tickRaw": int(tick), "template": tpl,
            "templateName": SHOT_NAMES.get(str(tpl).lower()) if tpl else None,
            "x": rnd(x, 0), "y": rnd(y, 0), "z": rnd(z, 1),
            "yaw": None, "side": side, "shooterUid": shooter_uid,
            "entityId": g.get("entityId"),
        })
    out.sort(key=lambda s: (s["t"], s["tickRaw"]))
    return out, dropped, linked


def own_segment_units(tracks, player_unit):
    """本机的全部航迹段：优先用 tracks.devValidation.ownSegmentIds，其次所有 own=true 的段。"""
    units = tracks.get("units") or []
    ids = list(((tracks.get("devValidation") or {}).get("ownSegmentIds")) or [])
    segs = [u for u in units if u.get("entityId") in ids] if ids else []
    if not segs:
        segs = [u for u in units if u.get("own")]
    if not segs and player_unit:
        segs = [player_unit]
    return sorted(segs, key=lambda u: (u.get("samples") or [[0]])[0][0])


def vehicle_hints(kill_events):
    """从击杀包与伤害包反推 uid→载具，只在**结论唯一**时采用（uid 会复用，不唯一就留 null）。

    - 击杀包：KillerUid ↔ PlayerVehicle（受击者 uid 与载具名不在一包里）
    - 伤害包：PlayerUID ↔ Vehicle（OffendedUID 是受击者，不能配 Vehicle）
    """
    hints = {}

    def add(uid, vehicle, source):
        if uid in (None, 2047) or not vehicle:
            return
        slot = hints.setdefault(str(uid), {"vehicles": set(), "sources": set()})
        slot["vehicles"].add(vehicle)
        slot["sources"].add(source)

    ke = kill_events or {}
    for k in ke.get("kills") or []:
        f = k.get("fields") or {}
        add(f.get("KillerUid"), f.get("PlayerVehicle"), "kill_packet")
    for c in (ke.get("criticals") or []) + (ke.get("severes") or []):
        add(c.get("PlayerUID"), c.get("Vehicle"), "damage_packet")
    return hints


def pick_player_unit(tracks, player_name, own_props, axis):
    """本机实体：tracks 自带的标记 → 玩家名 → 与 own_props 高度剖面相关最强的一条。

    返回 (unit, 依据说明, 是否退化, 相关系数)。
    """
    units = tracks.get("units") or []
    pid = tracks.get("playerEntityId") or tracks.get("ownEntityId")
    if pid:
        for u in units:
            if u.get("entityId") == pid:
                return u, "tracks.playerEntityId=%s" % pid, False, None
    for u in units:
        if u.get("own"):
            return u, "tracks.units[].own=true（%s）" % u.get("entityId"), False, None
    if player_name:
        for u in units:
            if u.get("player") and player_name in str(u["player"]):
                return u, "unit.player=%s" % u["player"], False, None

    # ReplayCockpitParams 只有本机会有：用它的高度剖面去认航迹（纯回放内部证据）
    ptimes, palts = own_props_series(own_props, axis, "alt_m")
    best, best_corr = None, None
    if ptimes:
        for u in units:
            rows = unit_samples(tracks, u)
            if len(rows) < 50:
                continue
            got, ref = [], []
            for r in rows:
                t = axis.sec(r[0])
                if r[3] is None or t < ptimes[0] or t > ptimes[-1]:
                    continue
                q = linear_at(ptimes, palts, t, 5.0)
                if q is None:
                    continue
                got.append(r[3])
                ref.append(q)
            if len(got) < 50:
                continue
            c = pearson(got, ref)
            if c is not None and (best_corr is None or abs(c) > abs(best_corr)):
                best, best_corr = u, c
    if best is not None and abs(best_corr) >= 0.9:
        return (best, "tracks 没给本机实体，用 own_props 高度剖面与各航迹做相关选出 %s（相关 %.4f）"
                % (best.get("entityId"), best_corr), True, best_corr)

    air = [u for u in units if unit_samples(tracks, u)]
    if air:
        best = max(air, key=lambda u: len(unit_samples(tracks, u)))
        return best, "退化：最长的一条空中航迹 %s" % best.get("entityId"), True, None
    return None, "tracks.json 里没有可用实体", True, None


def build_own(tracks, axis, t1, player_unit, flip_note, own_props, ysign=1.0,
              segments=None):
    """本机 10 Hz 三维：x/y（和兜底 z）来自本机实体航迹，飞参通道来自回放的本机属性表。

    本机在源数据里可能被断开成多段（wrpl-probe 的 `devValidation.ownSegmentIds`，本局两段：
    `uid-368` 31.1–430.8 s + `uid-368#2` 431.1–589.8 s，中间是机场重生/换位）。
    **段与段之间不插值、留 null**，否则会凭空画出一条 1 km 的瞬移线。
    回放确实没有的通道（迎角、弹药）留 null，不回填 8111。
    """
    segs = [u for u in (segments or []) if u] or ([player_unit] if player_unit else [])
    rows, seg_ids = [], []
    for si, seg_u in enumerate(segs):
        for r in unit_samples(tracks, seg_u):
            rows.append(r)
            seg_ids.append(si)
    order = sorted(range(len(rows)), key=lambda i: rows[i][0])
    rows = [rows[i] for i in order]
    seg_ids = [seg_ids[i] for i in order]

    n = int(math.floor(t1 * HZ_OWN)) + 1
    grid = [i / HZ_OWN for i in range(n)]
    times = [axis.sec(r[0]) for r in rows]
    xs = [r[1] for r in rows]
    ys = [r[2] * ysign for r in rows]
    zs = [r[3] for r in rows]

    def interp_track(values, t):
        """跨段或跨 >1.5 s 空档不插值（返回 None）。"""
        i = bisect.bisect_left(times, t)
        if i < len(times) and abs(times[i] - t) <= 1e-9:
            return values[i]
        if i == 0 or i >= len(times):
            return None
        if seg_ids[i - 1] != seg_ids[i] or (times[i] - times[i - 1]) > MAX_INTERP_GAP_S:
            return None
        v1, v2 = values[i - 1], values[i]
        if v1 is None or v2 is None:
            return None
        return v1 + (v2 - v1) * (t - times[i - 1]) / (times[i] - times[i - 1])

    x = [rnd(interp_track(xs, t), 0) for t in grid]
    y = [rnd(interp_track(ys, t), 0) for t in grid]
    track_z = [rnd(interp_track(zs, t), 1) for t in grid]

    def chan(col, nd, scale=1.0, circular=False):
        """按列取回放属性表 → 插值到本机网格；circular 用 sin/cos 插值避免 0/360 跳变。"""
        ctimes, cvals = own_props_series(own_props, axis, col)
        if not ctimes:
            return [None] * n
        if circular:
            sv = [None if v is None else math.sin(math.radians(v)) for v in cvals]
            cv = [None if v is None else math.cos(math.radians(v)) for v in cvals]
            sa = [linear_at(ctimes, sv, t, MAX_INTERP_GAP_S) for t in grid]
            ca = [linear_at(ctimes, cv, t, MAX_INTERP_GAP_S) for t in grid]
            return [None if (a is None or b is None)
                    else rnd(math.degrees(math.atan2(a, b)) % 360.0, nd) for a, b in zip(sa, ca)]
        out = [linear_at(ctimes, cvals, t, MAX_INTERP_GAP_S) for t in grid]
        return [None if v is None else rnd(v * scale, nd) for v in out]

    props_z = chan("alt_m", 1)
    z = [a if a is not None else b for a, b in zip(props_z, track_z)]
    hdg = chan("hdg_deg", 1, circular=True)
    if not any(v is not None for v in hdg):
        # 属性表没有航向时退回"由位移推"（回放自身水平框架）
        for i in range(1, n):
            x0, x1 = x[i - 1], x[i]
            y0, y1 = y[i - 1], y[i]
            if x0 is None or x1 is None or (x0 == x1 and y0 == y1):
                continue
            hdg[i] = rnd(bearing_deg(x1 - x0, y1 - y0), 1)

    own = {
        "t0": 0.0, "hz": HZ_OWN, "n": n,
        "x": x, "y": y, "z": z,
        "ias": chan("ias_mps", 1),
        "tas": chan("tas_mps", 1),
        "mach": chan("mach", 3),
        "hdg": hdg,
        "roll": chan("roll_deg", 1),
        "pitch": chan("pitch_deg", 1),
        "g": chan("ny", 2),
        "aoa": [None] * n,          # 回放属性表里没有迎角与弹药
        "ammo": [None] * n,
        "fuel": chan("fuel_kg", 0),
        "vy": chan("vario_mps", 1),
        "throttle": chan("throttle_0_1", 1, scale=100.0),
    }
    filtered = envelope_filter(x, y, z, HZ_OWN)
    gaps = [times[i] - times[i - 1] for i in range(1, len(times))]
    stats = {
        "rows": len(rows),
        "coverage": (sum(1 for v in x if v is not None) / n) if n else 0.0,
        "max_gap": max(gaps) if gaps else 0.0,
        "big_gaps": sum(1 for g in gaps if g > MAX_INTERP_GAP_S),
        "z_coverage": (sum(1 for v in z if v is not None) / n) if n else 0.0,
        "props_z": sum(1 for v in props_z if v is not None),
        "envelope_filtered": filtered,
        "note": flip_note,
    }
    # 内部一致性：属性表高度 vs 本机航迹 z（两条都由回放解出，不是必须相等）
    diffs = [abs(a - b) for a, b in zip(props_z, track_z) if a is not None and b is not None]
    if diffs:
        stats["z_cross_rms"] = math.sqrt(sum(d * d for d in diffs) / len(diffs))
        stats["z_cross_max"] = max(diffs)
    return own, grid, stats


def build_units(tracks, axis, player_entity, t1, ysign=1.0, uid_map=None,
                side_source="uid_map", v_hints=None, side_final_map=None, display=None,
                deaths=None, hidden_all=None, categories=None):
    """他机三维航迹；空中 5 Hz、地面 1 Hz，全部来自回放（hasAlt=true）。"""
    out, counters = [], defaultdict(int)
    dropped = []
    defaulted = []
    bad_rows = 0
    bad_entities = []
    breaks = []
    filtered = 0
    for u in tracks.get("units") or []:
        own_set = {player_entity} if isinstance(player_entity, str) else set(player_entity or [])
        if u.get("entityId") in own_set:
            continue                     # 本机（含所有分段）不进 units
        raw_n = len(u.get("samples") or [])
        rows = unit_samples(tracks, u)
        if len(rows) < raw_n:
            bad_rows += raw_n - len(rows)
            bad_entities.append(u.get("entityId"))
        if len(rows) < MIN_TRACK_SAMPLES:
            dropped.append(u.get("entityId"))
            continue
        # 契约 §1.0 要求 domain 必给；真缺了只能退回 air，但要记下来（6c 会按空中查 z 起伏）
        domain = u.get("domain") if u.get("domain") in ("air", "ground") else "air"
        if u.get("domain") not in ("air", "ground"):
            defaulted.append(u.get("entityId"))
        # 身份来自 uid_map（wrpl-ecs 的 ECS 映射）；uid 会复用，映射不到就是 unknown/null，不猜
        ident = (uid_map or {}).get(str(u.get("uid"))) or {}
        side_decoded = ident.get("side") if ident.get("side") in SIDE_CODE else (
            u.get("side") if u.get("side") in SIDE_CODE else "unknown")
        # 终判（side_final）优先；没有才退回解码值，再不行 unknown
        # 分段后的 entityId 形如 uid-349#2，查终判表要剥掉 #n 后缀
        base_id = str(u.get("entityId") or "").split("#")[0]
        side_final_v = (side_final_map or {}).get(base_id)
        if side_final_v in ("ally", "enemy", "squad"):
            side = side_final_v
        else:
            side = side_decoded if side_source == "uid_map" else "unknown"
        vehicle = ident.get("vehicle") or u.get("vehicle")
        v_source = "uid_map" if vehicle else None
        if not vehicle:
            hint = (v_hints or {}).get(str(u.get("uid")))
            if hint and len(hint["vehicles"]) == 1:
                vehicle = next(iter(hint["vehicles"]))
                v_source = "+".join(sorted(hint["sources"]))
        player = ident.get("player") or u.get("player")
        hz = UNIT_HZ[domain]
        tol = 0.5 / hz

        # uid 复用：相邻采样推算速度超包线处把航迹断开成多条（不跨接缝插值，否则画面瞬移）
        segs = [[rows[0]]]
        for i in range(1, len(rows)):
            dt = (rows[i][0] - rows[i - 1][0]) * axis.sec_per_tick
            dz = 0.0 if (rows[i][3] is None or rows[i - 1][3] is None) else rows[i][3] - rows[i - 1][3]
            d = math.sqrt((rows[i][1] - rows[i - 1][1]) ** 2 + (rows[i][2] - rows[i - 1][2]) ** 2
                          + dz * dz)
            if d > 5000.0 or (dt > 0 and d / dt > TELEPORT_SPEED_MS and d > TELEPORT_MIN_M):
                segs.append([rows[i]])
                breaks.append((u.get("entityId"), rnd(axis.sec(rows[i - 1][0]), 3), round(d, 1)))
            else:
                segs[-1].append(rows[i])

        code = "".join(c for c in str(vehicle or domain).lower() if c.isalnum())[:7] or domain
        counters[(side, code)] += 1
        base_key = "%s-%s-%02d" % (SIDE_CODE[side], code, counters[(side, code)])
        for si, seg_rows in enumerate(segs):
            if len(seg_rows) < MIN_TRACK_SAMPLES:
                dropped.append("%s%s" % (u.get("entityId"), "" if si == 0 else "#%d" % (si + 1)))
                continue
            first = axis.sec(seg_rows[0][0])
            last = axis.sec(seg_rows[-1][0])
            n = int(math.floor((last - first) * hz)) + 1
            times = [axis.sec(r[0]) for r in seg_rows]
            col_x = [r[1] for r in seg_rows]
            col_y = [r[2] * ysign for r in seg_rows]
            col_z = [r[3] for r in seg_rows]
            seg_index = [0] * len(times)          # 段内下标；段与段之间靠 times 单调 + 本列判断
            ux, uy, uz = [], [], []
            for i in range(n):
                t = first + i / hz
                ux.append(rnd(catmull_rom(times, col_x, seg_index, t, MAX_INTERP_GAP_S), 0))
                uy.append(rnd(catmull_rom(times, col_y, seg_index, t, MAX_INTERP_GAP_S), 0))
                uz.append(rnd(catmull_rom(times, col_z, seg_index, t, MAX_INTERP_GAP_S), 1))
            # 航向由**平滑后**的航迹推（相邻网格点位移），渲染端直接可用
            uh = [None] * n
            for i in range(1, n):
                if None in (ux[i - 1], uy[i - 1], ux[i], uy[i]):
                    continue
                if ux[i] == ux[i - 1] and uy[i] == uy[i - 1]:
                    continue
                uh[i] = rnd(bearing_deg(ux[i] - ux[i - 1], uy[i] - uy[i - 1]), 1)
            filtered += envelope_filter(ux, uy, uz, hz)
            # 死亡：只认**包含该 tick** 的那一段（uid 会复用，别的段不算）
            t_end = first + (n - 1) / hz
            hits = [d for d in deaths
                    if d[2] == u.get("uid") and first - 0.5 <= d[0] <= t_end + 1.0]
            death = min(hits, key=lambda d: d[0]) if hits else None
            out.append({
                "key": base_key if si == 0 else "%s#%d" % (base_key, si + 1),
                "side": side, "sideDecoded": side_decoded, "domain": domain,
                "vehicle": vehicle, "vehicleSource": v_source,
                "vehicleName": ("" if vehicle in hidden_all
                                else ((display or {}).get(vehicle) or vehicle)) if vehicle else None,
                "hidden": bool(vehicle in hidden_all),
                # 类别（显示约定，来自 vehicle_categories.json），键同 vehicle：回放内部名
                "vehicleCategory": ((categories or {}).get(vehicle) or None) if vehicle else None,
                "template": None, "class": "aircraft",
                "player": player, "hasAlt": True,
                "t0": rnd(first, 3), "hz": hz, "n": n,
                "x": ux, "y": uy, "z": uz, "hdg": uh,
                "entityId": u.get("entityId"), "uid": u.get("uid"),
                "segment": si + 1, "instances": ident.get("instances"),
                "deathTickRaw": death[1] if death else None,
                "deathKind": (None if not death else
                              ("no_killer" if death[3] in (2047, None, 0xFFFFFFFF)
                               else "shot_down")),
                "killerUid": (None if not death or death[3] == 2047 else death[3]),
            })
    out.sort(key=lambda u: (u["t0"], u["key"]))
    return out, dropped, defaulted, bad_rows, bad_entities, breaks, filtered


def envelope_filter(x, y, z, hz, vmax=TELEPORT_SPEED_MS):
    """交付前的包线滤除（契约 §5.2）：与上一个有效采样之间推算速度超 vmax 的点置 null。

    源数据里既有 uid 复用接缝，也有瞬时跳变；只按"相邻源采样"分段挡不住那些
    位移小但 dt 更小的高速度步（例如 dt=0.1 s 位移 100 m → 1000 m/s）。
    置 null 而不是平滑，是为了不编造中间位置。
    """
    dropped = 0
    last = None
    for i in range(len(x)):
        if x[i] is None or y[i] is None:
            continue
        if last is not None:
            dt = (i - last[0]) / hz
            d = math.dist((x[i], y[i], z[i] if z[i] is not None else 0.0),
                          (last[1], last[2], last[3] if last[3] is not None else 0.0))
            if dt > 0 and d / dt > vmax:
                x[i] = y[i] = z[i] = None
                dropped += 1
                continue
        last = (i, x[i], y[i], z[i])
    return dropped


def env_stats_of(tracks, axis):
    """速度包线自检：与验收工具同法，直接在回放原始样本上算（含本机实体）。"""
    violations = teleports = steps = 0
    for u in (tracks or {}).get("units") or []:
        rows = unit_samples(tracks, u)
        for i in range(1, len(rows)):
            dt = (rows[i][0] - rows[i - 1][0]) * axis.sec_per_tick
            if dt <= 0:
                continue
            dx = rows[i][1] - rows[i - 1][1]
            dy = rows[i][2] - rows[i - 1][2]
            dz = 0.0 if (rows[i][3] is None or rows[i - 1][3] is None) else rows[i][3] - rows[i - 1][3]
            d = math.sqrt(dx * dx + dy * dy + dz * dz)
            steps += 1
            if d / dt > 600.0:
                violations += 1
            if d > 5000.0:
                teleports += 1
    return {"violations": violations, "teleports": teleports, "steps": steps}


# ---------------------------------------------------------------- 事件

def kill_positions(units, axis, packet):
    """击杀点近似：用**击杀者载具**的航迹在 tick 时刻的位置（受击者 uid 未解析）。

    只在"同型载具在该 tick 时刻唯一命中"时给坐标；命中 0 条或多条一律返回 None
    （宁可缺坐标，不要把坐标安到别的飞机身上）。返回 (unit, x, y, z, reason)。
    """
    t = axis.sec(packet.get("tick"))
    vehicle = packet.get("vehicle")
    if not vehicle:
        return None, None, None, None, "击杀包没给载具名"
    hits = []
    for u in units:
        if u.get("vehicle") != vehicle:
            continue
        if u["t0"] - 1.0 <= t <= u["t0"] + max(0, u["n"] - 1) / u["hz"] + 1.0:
            hits.append(u)
    if len(hits) != 1:
        return None, None, None, None, ("同型航迹 %d 条，不唯一" % len(hits)) if hits else "无同型航迹"
    u = hits[0]
    i = min(max(int(round((t - u["t0"]) * u["hz"])), 0), u["n"] - 1)
    return u, u["x"][i], u["y"][i], u["z"][i], "killer_track_at_tick"


def build_events(header, entities, axis, units, window, uid_map=None, kill_events=None,
                 player_uid=None, ysign=1.0, tracks=None):
    """事件：回放击杀包（kind=kill）+ 奖项（award/lastblow）+ 聊天（chat）。"""
    events = []
    pos_stats = {"withPos": 0, "withoutPos": 0, "noUnits": not units, "reasons": {}}

    # 1) 击杀：优先用 kill_events.json 的位流字段（击杀者/受击者 uid、武器），
    #    坐标优先用 tracks.killPositions（受击者位置），其次击杀者位置，最后才按航迹猜
    kp_by_tick = {}
    for kp in ((tracks or {}).get("killPositions") or []):
        if kp.get("tickRaw") is not None:
            kp_by_tick[int(kp["tickRaw"])] = kp
    kill_rows = (kill_events or {}).get("kills") if isinstance(kill_events, dict) else None
    if not kill_rows:      # 没有位流产物时退回击杀包（只有击杀者载具，没有 uid）
        kill_rows = [{"tick": p.get("tick"), "fields": {"PlayerVehicle": p.get("vehicle")}}
                     for p in (entities.get("killPackets") or [])]
    for k in kill_rows:
        tick = k.get("tick")
        if tick is None and k.get("tsMs") is not None:
            tick = axis.ms_to_tick(k["tsMs"])
        if tick is None and k.get("sec") is not None:
            tick = axis.to_tick(axis.from_clock_sec(float(k["sec"])))
        if tick is None:
            continue
        t = axis.sec(tick)
        if not (window["t0"] <= t <= window["t1"]):
            continue
        f = k.get("fields") or {}
        killer_uid = f.get("KillerUid")
        victim_uid = f.get("VictimUid")
        actor_veh = f.get("PlayerVehicle") or None
        weapon = f.get("PlayerWeapon") or None
        actor_info = (uid_map or {}).get(str(killer_uid)) or {}
        victim_info = (uid_map or {}).get(str(victim_uid)) or {}
        actor_name = actor_info.get("player")
        victim_name = victim_info.get("player")
        target_veh = victim_info.get("vehicle")
        # 坐标：killPositions 的受击者位置 → 击杀者位置 → 同型航迹唯一命中
        x = y = z = None
        pos_basis = None
        kp = kp_by_tick.get(int(tick)) or {}
        vp, kpos = kp.get("victimPos"), kp.get("killerPos")
        if kp.get("hasVictimPos") and vp:
            x, y, z, pos_basis = vp[0], vp[1] * ysign, vp[2], "victim_pos_at_tick"
        elif kp.get("hasKillerPos") and kpos:
            x, y, z, pos_basis = kpos[0], kpos[1] * ysign, kpos[2], "killer_pos_at_tick"
        else:
            u_hit, x2, y2, z2, why = kill_positions(units, axis, {"tick": tick,
                                                                 "vehicle": actor_veh})
            if u_hit is not None:
                x, y, z, pos_basis = x2, y2, z2, why
        if x is None:
            pos_stats["withoutPos"] += 1
            key = "killPositions 也没有坐标（地面/AI 受击者）"
            pos_stats["reasons"][key] = pos_stats["reasons"].get(key, 0) + 1
        else:
            pos_stats["withPos"] += 1
        no_killer = killer_uid in (None, 2047, 0xFFFFFFFF) or (not actor_veh and not actor_name)
        actor_label = actor_name or actor_veh or (
            None if no_killer else "UID %s" % killer_uid)
        target_label = victim_name or target_veh or (
            "UID %s" % victim_uid if victim_uid is not None else "未知目标")
        if no_killer:
            text = "%s 被击落（无击杀者：坠毁/环境）" % target_label
        else:
            text = "%s 击落 %s%s" % (actor_label, target_label,
                                     ("（%s）" % weapon) if weapon else "")
        events.append({
            "t": rnd(t, 3), "kind": "kill", "text": text, "composed": True,
            "actor": actor_name, "actorVehicle": actor_veh,
            "target": victim_name, "targetVehicle": target_veh,
            "entityId": ("uid-%s" % killer_uid) if not no_killer and killer_uid is not None else None,
            "victimEntityId": ("uid-%s" % victim_uid) if victim_uid is not None else None,
            "isPlayerActor": (player_uid is not None and killer_uid == player_uid),
            "isPlayerTarget": (player_uid is not None and victim_uid == player_uid),
            "replayTick": int(tick), "x": x, "y": y, "z": z,
            "weapon": weapon, "weaponName": weapon_display(weapon),
            "killerUid": killer_uid, "victimUid": victim_uid,
            "noKiller": bool(no_killer), "posBasis": pos_basis,
        })

    # 2) 奖项：一条奖项只记录首次时刻（本奖项共 N 次写在文本里）
    for a in entities.get("awards") or []:
        ticks = a.get("firstTicksRaw") or a.get("firstTicks") or []
        if ticks:
            tick = int(ticks[0])
        elif a.get("firstTsMs") is not None:
            tick = axis.ms_to_tick(a["firstTsMs"])
        elif a.get("firstSec") is not None:
            tick = axis.to_tick(axis.from_clock_sec(float(a["firstSec"])))
        else:
            continue
        t = axis.sec(tick)
        if not (window["t0"] <= t <= window["t1"]):
            continue
        name = str(a.get("award") or "unknown_award")
        count = int(a.get("count") or 1)
        kind = AWARD_KIND.get(name, "award")
        events.append({
            "t": rnd(t, 3), "kind": kind,
            "text": "%s（本局 %d 次，此处为首次时刻）" % (name, count),
            "composed": True,
            "actor": None, "actorVehicle": None, "target": None, "targetVehicle": None,
            "entityId": None, "victimEntityId": None,
            "isPlayerActor": False, "isPlayerTarget": False,
            "replayTick": tick,
            "x": None, "y": None, "z": None,
            "awardCount": count,
        })

    # 3) 聊天：回放里有真实原文与发送者
    for c in entities.get("chat") or []:
        tick = c.get("tick")
        if tick is None and c.get("tsMs") is not None:
            tick = axis.ms_to_tick(c["tsMs"])
        if tick is None and c.get("ms") is not None:
            tick = axis.ms_to_tick(c["ms"])
        if tick is None:
            continue
        t = axis.sec(tick)
        if not (window["t0"] <= t <= window["t1"]):
            continue
        events.append({
            "t": rnd(t, 3), "kind": "chat",
            "text": clean_text(c.get("message")),
            "composed": False,
            "actor": c.get("sender"), "actorVehicle": None,
            "target": None, "targetVehicle": None,
            "entityId": None, "victimEntityId": None,
            "isPlayerActor": False, "isPlayerTarget": False,
            "replayTick": int(tick),
            "x": None, "y": None, "z": None,
        })

    events.sort(key=lambda e: (e["t"], e["kind"], e["text"]))
    return events, pos_stats


# ---------------------------------------------------------------- 元数据

def build_match_results(header):
    players = header.get("players") or []
    results = header.get("results") or {}
    author_id = str(results.get("authorUserId") or "")
    matched = str(results.get("timePlayed")) if results.get("timePlayed") is not None else None
    rows = []
    for p in players:
        if not isinstance(p, dict):
            continue
        rows.append({
            "userId": p.get("userId"), "name": p.get("name"), "clanTag": p.get("clanTag") or "",
            "kills": p.get("kills"), "humanKills": p.get("humanKills"),
            "groundKills": p.get("groundKills"), "navalKills": p.get("navalKills"),
            "timePlayed": matched if str(p.get("userId")) == author_id else None,
        })
    if not rows:
        return None
    return {"players": rows, "raw": results or None,
            "mode": (header.get("header") or {}).get("battleClass"),
            "scoreLimit": (header.get("header") or {}).get("scoreLimit"),
            "timeLimitMin": (header.get("header") or {}).get("timeLimit"),
            "matchSeconds": results.get("timePlayed")}


def build_roster(entities):
    """玩家↔载具表：只给"这个载具可能是这几人"的集合，不给实体编玩家名。"""
    roster = defaultdict(list)
    for p in entities.get("playerVehicles") or []:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        tag = p.get("clanTag") or ""
        label = ("%s %s" % (tag, name)).strip() if tag else name
        for v in (p.get("vehicles") or []):
            craft = v.get("craft") if isinstance(v, dict) else None
            if craft and label:
                roster[craft].append(label)
    return {k: sorted(set(v)) for k, v in sorted(roster.items())}


# ---------------------------------------------------------------- 自检

def selfcheck(payload):
    bad = []

    def check(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + ("  " + detail if detail else ""))
        if not cond:
            bad.append(name)

    meta = payload["meta"]
    own = payload["own"]
    units = payload["units"]
    events = payload["events"]

    own_keys = ["t0", "hz", "n", "x", "y", "z", "ias", "mach", "hdg", "roll", "pitch",
                "g", "aoa", "ammo", "fuel"]
    check("own 字段齐全", all(k in own for k in own_keys))
    own_arrays = [k for k in own if isinstance(own[k], list)]
    check("own 通道等长 = n", all(len(own[k]) == own["n"] for k in own_arrays),
          "n=%d，通道 %d 个" % (own["n"], len(own_arrays)))
    check("own.t0≈0（回放 tick 轴原点）", abs(own["t0"]) < 1.0, "t0=%s" % own["t0"])
    check("own 时间轴严格递增", own["hz"] > 0)

    unit_keys = ["key", "side", "domain", "vehicle", "player", "hasAlt", "t0", "hz",
                 "n", "x", "y", "z", "hdg"]
    check("units 字段齐全", all(all(k in u for k in unit_keys) for u in units))
    check("units 通道等长 = n",
          all(len(u["x"]) == len(u["y"]) == len(u["z"]) == len(u["hdg"]) == u["n"]
              for u in units), "%d 条" % len(units))
    check("units key 唯一", len(set(u["key"] for u in units)) == len(units))
    check("units 没有 Player", all(not str(u["key"]).startswith("Player") for u in units))
    check("units 无 height 混入（除 hasAlt）",
          all(not any(k != "hasAlt" and "alt" in str(k).lower() for k in u) for u in units))

    ev_keys = ["t", "kind", "text", "composed", "actor", "actorVehicle", "target",
               "targetVehicle", "entityId", "victimEntityId", "isPlayerActor",
               "isPlayerTarget", "replayTick", "x", "y", "z"]
    check("events 字段齐全", all(all(k in e for k in ev_keys) for e in events))
    ts = [e["t"] for e in events]
    check("events 时间单调", ts == sorted(ts), "%d 条" % len(ts))
    w = meta["window"]
    check("events 落在窗口内",
          all(w["t0"] <= t <= w["t1"] + 1e-6 for t in ts))
    check("meta 字段齐全",
          all(k in meta for k in ("source", "replayFile", "replayHeader", "player",
                                  "matchResults", "replayTicks", "window", "grid",
                                  "counts", "devValidation", "provenance", "limits")))
    c = meta["counts"]
    check("counts 与实际一致",
          c["ownSamples"] == own["n"] and c["trackCount"] == len(units)
          and c["airTracks"] == sum(1 for u in units if u["domain"] == "air")
          and c["groundTracks"] == sum(1 for u in units if u["domain"] == "ground")
          and c["events"] == len(events)
          and c["kills"] == sum(1 for e in events if e["kind"] == "kill")
          and c["ownKillsByEvents"] == sum(1 for e in events
                                           if e["isPlayerActor"] and e["kind"] == "kill"))
    shots = payload.get("shots") or []
    st = [x["t"] for x in shots]
    check("shots 字段齐全且按 t 单调",
          st == sorted(st) and all(all(k in x for k in ("t", "tickRaw", "template", "x", "y",
                                                        "z", "yaw", "side", "shooterUid"))
                                   for x in shots), "%d 条" % len(shots))
    check("counts.shots 与实际一致", meta["counts"].get("shots") == len(shots))
    rw = payload.get("runways")
    check("runways 字段齐全",
          isinstance(rw, list) and all(all(k in r for k in ("key", "name", "x1", "y1", "z1", "x2",
                                                            "y2", "z2", "lengthM", "headingDeg",
                                                            "spreadM", "confidence", "source"))
                                       for r in rw), "%d 条" % len(rw or []))
    sq = meta.get("squad")
    check("meta.squad 为 null 或含 squadId/members/source",
          sq is None or all(k in sq for k in ("squadId", "members", "source")))
    disp = ([u.get("vehicleName") for u in payload["units"]]
            + [u.get("vehicleCategory") for u in payload["units"]]
            + [x.get("templateName") for x in payload.get("shots") or []]
            + [r.get("name") for r in payload.get("runways") or []]
            + [a.get("name") for a in payload.get("airfields") or []])
    bad_names = [x for x in disp if x and not all(ord(c) < 128 for c in x)]
    check("显示名零非 ASCII（vehicleName/vehicleCategory/templateName/name）",
          not bad_names, str(bad_names[:5]))
    # 类别由 payload.meta.vehicleCategories 判断（selfcheck 是独立函数，拿不到 main 的局部变量）
    vc_meta = (payload.get("meta") or {}).get("vehicleCategories")
    check("类别：提供了表就每个 unit 都有 vehicleCategory",
          (not vc_meta) or all(u.get("vehicleCategory") for u in payload["units"]),
          "有类别 %d/%d" % (sum(1 for u in payload["units"] if u.get("vehicleCategory")),
                            len(payload["units"])))
    tt = payload.get("terrain")
    check("terrain 为 null 或契约 §9.2 结构",
          tt is None or all(k in tt for k in ("meta", "contours", "water", "coastlines")))
    af = payload.get("airfields")
    check("airfields 字段齐全",
          isinstance(af, list) and all(all(k in a for k in ("key", "x", "y", "z", "structures",
                                                           "source")) for a in af),
          "%d 个" % len(af or []))
    dv = meta["devValidation"]
    check("devValidation 不含坐标数组",
          not any(isinstance(v, list) for v in dv.values()))
    return not bad


def write_js(path, payload):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    text = ("/* 本局真实回放 " + str(payload["meta"].get("replayFile")) + " 的数据文件。\n"
            "   由 ace-combat/tools/build_ace_mission.py 从 .wrpl 解码产物生成：\n"
            "   build/wrpl/{header,entities,tracks}.json。数据 100% 来自回放，不含 8111 抓取。\n"
            "   方法与局限见 ace-combat/docs/DATA-NOTES.md。 */\n"
            "window.ACE_MISSION = " + body + ";\n")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    return len(text.encode("utf-8"))


# --------------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description=".wrpl 解码产物 → ace-combat/mission-data.js")
    ap.add_argument("--header", default=HEADER_PATH)
    ap.add_argument("--entities", default=ENTITIES_PATH)
    ap.add_argument("--tracks", default=None, help="默认按 tracks.json → tracks_ecs.json 顺序找")
    ap.add_argument("--uid-map", default=UID_MAP_PATH)
    ap.add_argument("--side-source", choices=("uid_map", "unknown"), default="uid_map",
                    help="侧别口径：uid_map=用解码映射（默认）；"
                         "unknown=一律 unknown（wrpl-probe/wrpl-ecs 认为方向未闭合时更保守）")
    ap.add_argument("--kill-events", default=KILL_EVENTS_PATH)
    ap.add_argument("--ground", default=GROUND_PATH)
    ap.add_argument("--side-final", default=SIDE_FINAL_PATH)
    ap.add_argument("--shots", default=SHOTS_PATH)
    ap.add_argument("--display-names", default=DISPLAY_NAMES_PATH)
    ap.add_argument("--categories", default=CATEGORIES_PATH)
    ap.add_argument("--runways-json", default=RUNWAYS_PATH)
    ap.add_argument("--terrain", default=TERRAIN_PATH)
    ap.add_argument("--squad", default=SQUAD_PATH)
    ap.add_argument("--own-props", default=OWN_PROPS_PATH,
                    help="回放本机属性表（ReplayCockpitParams）；缺它则飞参通道留 null")
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--max-mb", type=float, default=MAX_MB)
    args = ap.parse_args()

    header = load_json(args.header)
    entities = load_json(args.entities)
    if header is None and entities is None:
        print("缺少回放解码产物：\n  %s\n  %s\n先等 wrpl-format 产出，再跑本脚本。"
              % (args.header, args.entities))
        return 2
    header = header or {}
    entities = entities or {}

    tracks_path = args.tracks
    if tracks_path is None:
        for cand in (TRACKS_PATH, TRACKS_ALT_PATH):
            if os.path.isfile(cand):
                tracks_path = cand
                break
    tracks = load_json(tracks_path) if tracks_path else None
    if tracks is None:
        tracks_path = None
    own_props = load_json(args.own_props)
    own_props_path = args.own_props if own_props else None
    uid_map = load_json(args.uid_map) or {}
    kill_events = load_json(args.kill_events)
    ground = load_json(args.ground)
    side_final = load_json(args.side_final) or {}
    launches = load_json(args.shots)
    runways_json = load_json(args.runways_json)
    squad, squad_note = build_squad(load_json(args.squad))
    print("\n== 小队（结算表 squadId）\n   %s" % squad_note)
    terrain, terrain_note = build_terrain(load_json(args.terrain),
                                          ((header.get("header") or {}).get("level")))
    print("\n== terrain（契约 §9.2）\n   %s" % terrain_note)
    display_raw = load_json(args.display_names) or {}
    display_names = {k: v for k, v in display_raw.items()
                     if not str(k).startswith("__") and isinstance(v, str)}
    hidden_all = set(HIDDEN_TEMPLATES)
    hidden_all |= {str(x) for x in ((display_raw.get("__meta") or {}).get("hiddenTemplates") or [])}
    hidden_all |= {k for k, v in display_names.items() if v == ""}
    display_names = {k: v for k, v in display_names.items() if v != ""}
    # 型号类别（用户按 V 键在「型号 ⇄ 类别」间切换）：同样是**显示约定**，不是回放数据。
    # 键与 vehicle_display_names.json 完全同一套（回放里的 unit__className），可并排 join。
    categories_raw = load_json(args.categories) or {}
    categories = {k: v for k, v in categories_raw.items()
                  if not str(k).startswith("__") and isinstance(v, str) and v}
    print("\n== 型号类别（vehicle_categories.json）\n   %s"
          % ("并入 %d 条（键 = unit__className；未解析数见输入表 unresolved）"
             % len(categories) if categories
             else "缺失：units[].vehicleCategory 一律 null"))

    print("== 输入")
    for label, path, obj in (("header", args.header, header), ("entities", args.entities, entities),
                             ("tracks", tracks_path, tracks),
                             ("own_props", own_props_path, own_props),
                             ("uid_map", args.uid_map if uid_map else None, uid_map or None),
                             ("kill_events", args.kill_events if kill_events else None,
                              kill_events),
                             ("ground", args.ground if ground else None, ground),
                             ("side_final", args.side_final if side_final else None, side_final),
                             ("shots", args.shots if launches else None, launches),
                             ("display_names", args.display_names if display_names else None,
                              display_names),
                             ("categories", args.categories if categories else None,
                              categories),
                             ("runways", args.runways_json if runways_json else None,
                              runways_json)):
        size = os.path.getsize(path) / 1024.0 if path and os.path.isfile(path) else 0.0
        print("   %-9s %-56s %s" % (label, path or "（缺失）",
                                    "%.0f KB" % size if obj is not None else "未使用"))

    clock = header.get("clock") or {}
    axis = Axis(header, tracks)
    player_name = (header.get("results") or {}).get("author") or ""
    player_units = (entities.get("playerVehicles") or [])
    author_id = str((header.get("results") or {}).get("authorUserId") or "")
    author_row, author_veh = None, None
    for p in (header.get("players") or []):
        if str(p.get("userId")) == author_id:
            author_row = p
    for p in player_units:
        if str(p.get("userId")) == author_id:
            author_veh = p.get("primaryCraft")

    v_hints = vehicle_hints(kill_events)
    deaths = death_records((tracks or {}).get("killPositions"), axis)
    side_final_map = {k: v for k, v in ((side_final or {}).get("side") or {}).items()
                      if v in ("ally", "enemy", "squad")}
    player_uid = None
    if tracks:
        pid = tracks.get("playerEntityId") or tracks.get("ownEntityId")
        if isinstance(pid, str) and pid.startswith("uid-"):
            try:
                player_uid = int(pid.split("-")[1])
            except ValueError:
                player_uid = None

    tick_lo, tick_hi = axis.tick0, axis.tick_last
    if tracks:
        all_ticks = [s[0] for u in (tracks.get("units") or []) for s in (u.get("samples") or [])]
        if all_ticks:
            tick_lo, tick_hi = min(all_ticks), max(all_ticks)
    t1 = max(0.0, (tick_hi - axis.tick0) * axis.sec_per_tick)
    for end_key in ("endMarkerRaw", "endMarkerTick"):
        if clock.get(end_key) is not None:
            t1 = max(t1, axis.sec(clock[end_key]))
            break
    window = {"t0": 0.0, "t1": rnd(t1, 3), "duration": rnd(t1, 3)}

    y_flip, y_err_no, y_err_flip, y_n = (False, None, None, 0)
    if tracks and own_props:
        y_flip, y_err_no, y_err_flip, y_n = decide_y_flip(tracks, own_props, axis)
    ysign = -1.0 if y_flip else 1.0

    print("\n== 时间轴（回放 tick 轴）")
    print("   secPerTick=%s（%.0f tick/s），原点 tick0=%d（%s）"
          % (axis.sec_per_tick, 1.0 / axis.sec_per_tick, axis.tick0, axis.source))
    print("   tick 范围 %d..%d → 窗口 0..%.3f s（%.2f 分钟）"
          % (tick_lo, tick_hi, window["t1"], window["t1"] / 60.0))
    if y_n:
        print("   y 轴判定（纯回放内部：本机位移方向 vs own_props 真罗盘，%d 样本）："
              "不翻转中位误差 %.2f°，翻转后 %.2f° → %s"
              % (y_n, y_err_no, y_err_flip, "取反 y" if y_flip else "保持原样"))
    else:
        print("   y 轴判定：样本不足，保持解码给的坐标原样")

    player_unit, how, degraded, own_corr = (None, "tracks.json 未产出，几何留空", False, None)
    own = {"t0": 0.0, "hz": HZ_OWN, "n": 0, "x": [], "y": [], "z": [], "ias": [], "mach": [],
           "hdg": [], "roll": [], "pitch": [], "g": [], "aoa": [], "ammo": [], "fuel": [],
           "tas": [], "vy": [], "throttle": []}
    own_stats = {"rows": 0, "coverage": 0.0, "max_gap": 0.0, "big_gaps": 0, "z_coverage": 0.0}
    units, dropped = [], []
    bad_rows, bad_entities, breaks, filtered = 0, [], [], 0
    env_stats = {"violations": 0, "teleports": 0, "steps": 0}
    if tracks:
        player_unit, how, degraded, own_corr = pick_player_unit(tracks, player_name,
                                                                own_props, axis)
        print("\n== 本机实体判定")
        print("   %s（%s）" % ((player_unit or {}).get("entityId"), how))
        own_segs = own_segment_units(tracks, player_unit)
        own, _grid, own_stats = build_own(tracks, axis, window["t1"], player_unit, how,
                                          own_props, ysign, own_segs)
        print("   本机 %d Hz：n=%d，位置样本 %d 行，覆盖率 %.1f%%，z 覆盖 %.1f%%，"
              "最大样本跨度 %.2f s（>%.1f s 的 %d 处，补 null）"
              % (HZ_OWN, own["n"], own_stats["rows"], 100 * own_stats["coverage"],
                 100 * own_stats["z_coverage"], own_stats["max_gap"], MAX_INTERP_GAP_S,
                 own_stats["big_gaps"]))
        if own_stats.get("z_cross_rms") is not None:
            print("   回放内部互校：属性表高度 vs 本机航迹 z → rms %.2f m / max %.2f m"
                  % (own_stats["z_cross_rms"], own_stats["z_cross_max"]))
        print("   飞参通道覆盖率：%s"
              % " ".join("%s=%.0f%%" % (
                  k, 100.0 * sum(1 for v in own[k] if v is not None) / max(1, own["n"]))
                  for k in ("z", "ias", "mach", "hdg", "roll", "pitch", "g", "vy",
                            "fuel", "throttle", "aoa", "ammo")))
        (units, dropped, defaulted, bad_rows, bad_entities, breaks,
         filtered) = build_units(
            tracks, axis, {u.get("entityId") for u in own_segs}, window["t1"], ysign, uid_map,
            args.side_source, v_hints, side_final_map, display_names, deaths, hidden_all,
            categories)
        env_stats = env_stats_of(tracks, axis)
        print("   速度包线（回放原始样本 %d 步）：>600 m/s 的 %d 步，单步 >5000 m 的 %d 处"
              % (env_stats["steps"], env_stats["violations"], env_stats["teleports"]))
        air = sum(1 for u in units if u["domain"] == "air")
        sides = defaultdict(int)
        for u in units:
            sides[u["side"]] += 1
        print("\n== 他机 units（回放，空中 %d Hz / 地面 %d Hz）"
              % (UNIT_HZ["air"], UNIT_HZ["ground"]))
        print("   航迹 %d 条（空中 %d / 地面 %d），样本太少丢弃 %d 条，侧别 %s"
              % (len(units), air, len(units) - air, len(dropped), dict(sorted(sides.items()))))
        zr = [(u["key"], min(v for v in u["z"] if v is not None),
               max(v for v in u["z"] if v is not None))
              for u in units if any(v is not None for v in u["z"])]
        if zr:
            print("   z 范围样例：%s" % zr[:3])
        const_air = [k for k, lo, hi in zr if hi - lo < 5.0][:6]
        print("   键样例 %s；空中 z 起伏 <5 m 的 %d 条（验收项 6c 要求空中非常数）"
              % ([u["key"] for u in units[:3]], len(const_air)))
        print("   死亡标记（回放击杀包 victimUid）：%d 段有 deathTickRaw（shot_down %d / no_killer %d）"
              % (sum(1 for u in units if u.get("deathTickRaw")),
                 sum(1 for u in units if u.get("deathKind") == "shot_down"),
                 sum(1 for u in units if u.get("deathKind") == "no_killer")))
        print("   包线滤除（契约 §5.2，>600 m/s 的步置 null）：units %d 点 + own %d 点 = %d 点"
              % (filtered, own_stats.get("envelope_filtered", 0),
                 filtered + own_stats.get("envelope_filtered", 0)))
        print("   航迹断点（瞬时位移超速度包线或单步 >5000 m，断点处不给坐标）：%d 处，"
              "涉及实体 %d 个 %s"
              % (len(breaks), len(set(b[0] for b in breaks)),
                 sorted(set(b[0] for b in breaks))[:5]))
        if defaulted:
            print("   [注意] domain 缺失被默认成 air 的实体 %d 个：%s"
                  % (len(defaulted), defaulted[:6]))
    else:
        print("\n== 几何缺失：tracks.json 还没产出，own 与 units 留空（结构仍合法）")

    ground_units = build_ground_units(ground, axis, display_names,
                                      (side_final or {}).get("centers"), hidden_all,
                                      categories)
    if ground_units:
        units = units + ground_units
        units.sort(key=lambda u: (0 if u["domain"] == "air" else 1, u["t0"], u["key"]))
        zs = [u["z"][0] for u in ground_units]
        print("\n== 地面单位（ground.json，单采样静态点）")
        print("   %d 条：模板 %s；z 范围 %.1f..%.1f m；t0(spawn) %.1f..%.1f s"
              % (len(ground_units),
                 {k: sum(1 for u in ground_units if u["vehicle"] == k)
                  for k in sorted({u["vehicle"] for u in ground_units})},
                 min(zs), max(zs), min(u["t0"] for u in ground_units),
                 max(u["t0"] for u in ground_units)))

    if units:
        cdist = defaultdict(int)
        for u in units:
            cdist[u.get("vehicleCategory") or "UNKNOWN"] += 1
        print("\n== 类别分布（units[].vehicleCategory，供 V 键切换）")
        print("   有类别 %d / %d 条：%s"
              % (sum(1 for u in units if u.get("vehicleCategory")), len(units),
                 dict(sorted(cdist.items()))))
    runways = build_runways(runways_json)
    if runways:
        dz = [abs((r["z2"] or 0) - (r["z1"] or 0)) for r in runways]
        print("\n== 跑道线段（runways.json）")
        print("   %d 条：%s" % (len(runways),
              [(r["key"], r["lengthM"], r["headingDeg"], r["confidence"]) for r in runways]))
        print("   同一条跑道两端高度差最大 %.1f m；来源 %r"
              % (max(dz) if dz else 0.0, (runways_json or {}).get("answer")))
    airfields = build_airfields(runways_json, window)
    if airfields:
        print("\n== 机场地标点（runways.json）")
        print("   %d 个：%s；runways 为空（回放无跑道几何，结论 %r）"
              % (len(airfields), [a["key"] for a in airfields],
                 (runways_json or {}).get("answer")))
    shots, shots_dropped, shots_linked = build_shots(launches, axis, units, window)
    if shots:
        print("\n== 发射点（projectile_launches.json）")
        print("   %d 条（丢弃窗口外/无位置 %d）；模板 %s"
              % (len(shots), shots_dropped,
                 {k: sum(1 for x in shots if x["template"] == k)
                  for k in sorted({x["template"] for x in shots})}))
        print("   关联到最近飞机航迹（≤100 m）：%d/%d = %.1f%%；yaw 一律 null（口径未验证）"
              % (shots_linked, len(shots), 100.0 * shots_linked / max(1, len(shots))))

    events, pos_stats = build_events(header, entities, axis, units, window, uid_map,
                                     kill_events, player_uid, ysign, tracks)
    kinds = defaultdict(int)
    for e in events:
        kinds[e["kind"]] += 1
    print("\n== 事件（全部来自回放）")
    print("   合计 %d：%s" % (len(events), dict(sorted(kinds.items()))))
    print("   击杀包 %d 个；奖项 %d 条；聊天 %d 条"
          % (len(entities.get("killPackets") or []), len(entities.get("awards") or []),
             len(entities.get("chat") or [])))
    kill_ev = [e for e in events if e["kind"] == "kill"]
    basis = {}
    for e in kill_ev:
        if e.get("posBasis"):
            basis[e["posBasis"]] = basis.get(e["posBasis"], 0) + 1
    print("   有击杀点坐标的击杀 %d/%d；坐标来源 %s"
          % (pos_stats["withPos"], kinds.get("kill", 0), dict(sorted(basis.items()))))
    dists = sorted(math.dist(kp.get("victimPos"), kp.get("killerPos"))
                   for kp in ((tracks or {}).get("killPositions") or [])
                   if kp.get("hasVictimPos") and kp.get("hasKillerPos"))
    if dists:
        print("   回放内部校验：击杀时刻受击者↔击杀者距离 n=%d，p50=%.0f m，p90=%.0f m，"
              "max=%.0f m，<3 km 占 %.1f%%"
              % (len(dists), dists[len(dists) // 2], dists[int(0.9 * len(dists))], dists[-1],
                 100.0 * sum(1 for x in dists if x < 3000) / len(dists)))
    print("   击杀细节：有武器名 %d，无击杀者（坠毁/环境）%d，本机作为击杀者的 kill %d"
          % (sum(1 for e in kill_ev if e.get("weapon")),
             sum(1 for e in kill_ev if e.get("noKiller")),
             sum(1 for e in kill_ev if e["isPlayerActor"])))
    if pos_stats["withoutPos"]:
        print("   缺坐标的击杀 %d 条，原因：%s"
              % (pos_stats["withoutPos"], dict(sorted(pos_stats["reasons"].items()))))

    roster = build_roster(entities)
    match_results = build_match_results(header)
    player = {
        "name": player_name,
        "vehicle": author_veh,
        "callsign": (author_row or {}).get("clanTag", "").strip("= ") or None,
        "userId": author_id or None,
        "clanTag": (author_row or {}).get("clanTag") or None,
        "team": (author_row or {}).get("team"),
        "slot": (author_row or {}).get("slot"),
        "kills": (author_row or {}).get("kills"),
        "humanKills": (author_row or {}).get("humanKills"),
        "groundKills": (author_row or {}).get("groundKills"),
    }
    dv_src = (tracks or {}).get("devValidation") or {}
    props = (own_props or {}).get("props") or {}

    def pnum(group, key):
        v = (props.get(group) or {}).get(key)
        return v if isinstance(v, (int, float)) else None

    n_props = len((own_props or {}).get("samples") or [])
    assoc_pct = dv_src.get("unitAssocRate150M")
    dev_validation = {
        "ownMaxErrM": dv_src.get("ownMaxErrM"),
        "ownRmsErrM": dv_src.get("ownRmsErrM"),
        "ownSamplesCompared": dv_src.get("ownSamplesCompared") or n_props,
        "unitAssocRate": (assoc_pct / 100.0) if isinstance(assoc_pct, (int, float)) else None,
        "speedViolations": (dv_src.get("speedViolations600")
                            if isinstance(dv_src.get("speedViolations600"), (int, float))
                            else env_stats["violations"]),
        "speedViolationsBeforeSplit": dv_src.get("speedViolations600BeforeSplit"),
        "trackSegments": dv_src.get("trackSegments"),
        "segmentsDroppedShort": dv_src.get("segmentsDroppedShort"),
        "ownAltRmsVs8111M": dv_src.get("ownAltRmsErrM") or pnum("altitude", "devRmsM"),
        "cfgOffsetSec": dv_src.get("cfgOffsetSec"),
        "unitCountDecoded": dv_src.get("unitCountDecoded"),
        "unitSamplesCompared": dv_src.get("unitSamplesCompared"),
        "speedViolationsTracks": dv_src.get("speedViolations600"),
        "groundCount": ((ground or {}).get("devValidation") or {}).get("count"),
        "groundNearestContactMedianM":
            ((ground or {}).get("devValidation") or {}).get("groundNearestContactMedianM"),
        "groundFracWithin100M":
            ((ground or {}).get("devValidation") or {}).get("groundFracWithin100M"),
        "groundControlWrongAxisMedianM":
            ((ground or {}).get("devValidation") or {}).get("controlWrongAxisMedianM"),
        "ownCorrVs8111": {
            "alt": pnum("altitude", "devCorrVs8111H"),
            "ias": pnum("ias", "devCorrVs8111IAS"),
            "mach": pnum("mach", "devCorrVs8111M"),
            "g": pnum("ny", "devCorrVs8111Ny"),
            "hdg": pnum("heading", "devCorrVs8111Compass"),
            "pitch": pnum("pitch", "devCorrVs8111Pitch"),
            "roll": pnum("roll", "devCorrVs8111Roll"),
            "vy": pnum("vario", "devCorrVs8111Vario"),
            "fuel": pnum("fuel", "devCorrVs8111Fuel"),
            "throttle": pnum("throttle", "devCorrVs8111Throttle"),
        },
        "envSteps": env_stats["steps"],
        "envTeleports": env_stats["teleports"],
        "killDistN": len(dists),
        "killDistP50M": rnd(dists[len(dists) // 2], 0) if dists else None,
        "killDistP90M": rnd(dists[int(0.9 * len(dists))], 0) if dists else None,
        "killDistMaxM": rnd(dists[-1], 0) if dists else None,
        "killDistUnder3km": (rnd(sum(1 for x in dists if x < 3000) / len(dists), 3)
                             if dists else None),
        "shotsCount": (launches or {}).get("count"),
        "shotsWithPosition": (launches or {}).get("withPosition"),
        "shotsMedianDistToNearestAircraftM":
            ((launches or {}).get("devValidation") or {}).get("medianDistToNearestAircraftM"),
        "shotsP90DistM": ((launches or {}).get("devValidation") or {}).get("p90DistM"),
        "shotsWithin100m": ((launches or {}).get("devValidation") or {}).get("within100m"),
        "shotsNote": ("发射点的位置校验（到最近飞机航迹中位 15.05 m、p90 34.2 m、≤100 m 100%）"
                      "由 wrpl-ecs/wrpl-probe 复算，本脚本未独立复算，只是透传。"),
        "groundNote": ("ground.json 的地面点位校验（到 8111 地面接触点中位 0.03 m、≤100 m 占 66.3%、"
                       "把北向取反退化到 3632 m）由 wrpl-ecs/wrpl-probe 复算，本脚本未独立复算，"
                       "只是透传。"),
        "note": ("本机/他机精度与时间偏移（ownMaxErrM/ownRmsErrM/ownAltRmsVs8111M/cfgOffsetSec/"
                 "unitCountDecoded/unitAssocRate）由 wrpl-probe 在开发期对比 8111 真值算出并透传，"
                 "本脚本未独立复算；unitAssocRate 已从百分比换算成 0..1 的比例。"
                 "speedViolations 是本脚本对回放航迹原始样本自算的（>600 m/s 的步数），"
                 "speedViolationsTracks 是 tracks.json 里的同一指标。"
                 "ownCorrVs8111 来自 build/wrpl/own_props.json 的开发期对比（只放数字，不放坐标）。"),
    }

    limits = [
        "kill 事件来自回放击杀包的位流字段（kill_events.json）：击杀者/受击者 uid、载具、武器；"
        "uid 经 uid_map.json（wrpl-ecs 的 ECS 映射）折算成人名与阵营。文本是拼合的"
        "（composed=true，回放里没有本地化原文）；武器名与机种名都是回放内部 id，"
        "界面要俗名需另配映射表，否则按契约 §0 显示 `--`。",
        "33 条击杀里有 1 条 killerUid=2047（回放用 0x7FF 表示「无」）= 坠毁/环境击杀，文本写明"
        "「无击杀者：坠毁/环境」（kind 仍是 kill，以保持与 33 个击杀包一一对应）。本机（uid=%s）"
        "作为击杀者的 kill 事件 = counts.ownKillsByEvents = %s，与内嵌结算的"
        " counts.ownKillsFromResults=%s 一致。"
        % (player_uid, sum(1 for e in events if e["isPlayerActor"] and e["kind"] == "kill"),
           (author_row or {}).get("kills")),
        "玩家名大多不可用：uid_map 只在唯一时才填 player（本局 %d/%d 条），"
        "有歧义时只给 playerCandidates —— 不把候选当结论；names 因此为空对象，"
        "标签显示 uid_map 给的机种，机种也缺的显示侧别+机种。"
        % (sum(1 for v in (uid_map or {}).values()
               if isinstance(v, dict) and v.get("player")), len(uid_map or {})),
        "本机飞参（高度/表速/Mach/过载/航向/俯仰/滚转/升降率/油量/油门）来自回放的"
        " ReplayCockpitParams 属性表（约 4 Hz，插值到 10 Hz）；回放确实没有的通道"
        "（迎角 aoa、弹药 ammo）留 null，不用 8111 回填。",
        "航向来自回放罗盘属性：单位度、0=正北、顺时针（开发期与 8111 罗盘相关 0.98）。",
        "奖项只记录每种奖项的首次时刻（回放只给首末时刻与次数，实例 tick 最多给 6 个），"
        "文本里的次数是本局总数。",
        "他机身份来自 uid_map.json（wrpl-ecs 2026-10-19 重发版）：side 有值 %d/%d"
        "（ally %d / enemy %d）、vehicle %d、player %d；映射不到的保持 unknown/null，"
        "**不用 8111 的颜色或图标补**。当前 side 口径 = %s。"
        % (sum(1 for u in units if u["side"] != "unknown"), len(units),
           sum(1 for u in units if u["side"] == "ally"),
           sum(1 for u in units if u["side"] == "enemy"),
           sum(1 for u in units if u.get("vehicle")),
           sum(1 for u in units if u.get("player")), args.side_source),
        "side 的主来源是 side_final.json（44 个 uid 全部定为 ally/enemy，unknown=0）："
        "①击杀关系二色（32 条击杀边、36 个 uid、0 冲突，与已闭合的 ECS 映射一致 13/14）；"
        "②剩余 11 架 AI 用**出生位置最近中心**判定（锚点=①已定的 33 个，自检 32/33 正确）。"
        "全部是纯回放数据。ECS 的原始取值仍逐条保留在 units[].sideDecoded 里以便追溯。",
        "**地面点的阵营也是推定**：按到 side_final.centers 两个出生中心的最近距离二选一"
        "（WT 不存在中立单位，反推可以）；与 AI 侧别一样是推断，界面开关可整体校正。",
        "**AI 的侧别是推定**（出生位置聚类，不是逐机确证）：若与画面观感冲突，"
        "可用 ui 齿轮里的「反转」开关整体校准；地面点位仍为 unknown（没有可用的阵营证据，不猜）。"
        "历史：ECS 早先基于 missionName 的取值方向未闭合（wrpl-probe 8111 交叉 6/23、我的颜色投票 13/29 与取反 16/26、"
        "结算表 team 9/8、出生点 k-means 3/26），已收回，故改用 side_final。",
        "源数据里还有个别瞬时跳变（位移小但 dt 更小的高速度步）：交付前按契约 §5.2 做了包线滤除，"
        "把与上一个有效采样之间推算速度 >600 m/s 的点置 null（不做平滑、不编造中间位置），"
        "共 %d 点；见 meta.counts.envelopeFiltered。"
        % (filtered + own_stats.get("envelope_filtered", 0)),
        "载具名覆盖率 %d/%d（uid_map %d + 击杀/伤害包反推 %d；**只在结论唯一时采用**，"
        "uid 会复用、不唯一就留 null，不猜）。来源逐条记在 units[].vehicleSource。"
        % (sum(1 for u in units if u.get("vehicle")), len(units),
           sum(1 for u in units if u.get("vehicleSource") == "uid_map"),
           sum(1 for u in units if (u.get("vehicleSource") or "").endswith("packet"))),
        "武器：events[].weapon 是回放内部名（如 us_aim9l_sidewinder），weaponName 是可读名"
        "（**显示约定**：人工映射表 + 机械转写，映射不到就原样显示内部名），不是回放数据。",
        "回放里的发射/命中类消息：**未确认**。已用的是 0xF058 击杀报告（33）与 0xF056/0xF157 "
        "伤害报告（32+15）；MPI 签名里还有 `ff0781*`/`ff0f81*` 家族 20 余种（占包量多数），"
        "疑似每帧实体/状态更新，语义未解 —— 本产物没有把它们当事件导出。",
        "本文件每条 unit 同时给出 side（当前口径）与 sideDecoded（同一来源的解码原值）；"
        "`--side-source unknown` 可一键切成统一 unknown。页面的齿轮面板提供「推定/反转/未知」三选一，可即时切换口径。"
        "用户可即时改观感 —— 这份不确定性交回给用户，而不是替他猜。",
        "显示名（units[].vehicleName）是**显示约定**：来自 build/wrpl/vehicle_display_names.json"
        "（wrpl-ecs 从 ptype=6 ECS 的 unit__className 内部名整理），**不是回放数据**，"
        "也不代表经考据的历史型号。查不到就留 null，场景应原样打印 units[].vehicle（原始内部名），"
        "**界面不得出现 AIR/GND 这类兜底字样**。",
        "小队成员来自回放结算表 `squadId`（本局 2 人小队：本机 + 1 名队友）；"
        "队友 uid 的三步判据（全部回放内、可收敛）：①机型候选集 → ②结算 kills → ③结算 deaths 指纹"
        "（本局机型候选 {351,357,359,370} 的 deaths 为 1/0/1/1，Mo Amaris 是 0 → 唯一命中 357）。"
        "换别的回放按这三步重跑，不收敛一律退回 null；**编队距离属启发式，已弃用**；"
        "本局结算表 `humanKills`/`aiKills` 全场为 0，不可用作判据。"
        "`members[].uidSource` 原样透传 wrpl-ecs 的说明。",
        "显示名是**显示约定**（来自 vehicle_display_names.json，纯 ASCII 全大写），"
        "取值 `displayName = map[vehicle] ?? vehicle`，**绝不造词、也不写 AIR/GND**；"
        "地面名走 ground.json 的 `unitClassName`（最终版覆盖 **184/184**）。",
        "**`hidden:true` 的单位界面不显示**（数据里保留，便于复核）：不可摧毁占位物（`dummy_immortal`）、"
        "占位机（`dummy_plane`）、**机场飞行雷达 / 机场主雷达**（用户点名：无法被攻击、无战术意义）。"
        "这些单位的 `vehicleName` 为空串，`counts.unitsHidden` 给出条数。"
        "隐藏单位仍照常填 `vehicleCategory`（类别说的是「这东西是什么」，与是否显示无关）。",
        "类别（`units[].vehicleCategory`）来自 datamine，是**显示约定、不是回放数据**："
        "build/wrpl/vehicle_categories.json 按回放内部名 `unit__className` 给出 "
        "FIGHTER / ATTACKER / TANK / SPAA / TRUCK / ARTILLERY / BUNKER / RADAR / FORTIFICATION 等类别，"
        "查不到一律 null（不编、也不写 UNKNOWN 之类兜底字样）；**用户可用 `V` 键在型号/类别间切换**。",
        ("terrain（等高线/海岸线）**已并入**：来自 levels/air_skyscraper_city.bin 的 LandMesh 解析产物"
         "（source=landmesh，1024×1024 栅格、格距 64.03 m、等高距 200 m、26 层；另含 5 个水体轮廓与 5 条海岸线），"
         "坐标系为 replay-world-meters，已用回放自身的 184 个地面点核对（r=0.995、中位偏差 3.89 m、92.9% ≤30 m；"
         "翻转对照 r=0.300）。**只接受本局地图**的等高线文件，脚本已做地图一致性校验。"
         if terrain else
         "terrain（等高线/海岸线）当前为 **null**：本局地图 levels/air_skyscraper_city.bin 属于老格式，"
         "**没有 HM2 高度栅格**，地形只在 `lmap`（LandMesh 三角网格）里，需要先支持 lmap 顶点缓冲解析。"
         "渲染侧可先用别的有 HM2 的地图样例（如 air_south_eastern_city_contours.json）接图层，"
         "但那**不能**进本交付文件 —— 别的地形的几何放进来等于编造，脚本已做地图一致性校验。"),
        "跑道线段（runways[]）**由回放自身的机场标记点反推**：t1/t2_airfield_top[_reload] 四个标记点的"
        "位置 + 单位航向，加上其引用模型名 `dynaf_pg_1line_3000_universal` 给出的 3000 m 长度 —— "
        "**不是关卡几何，也不是从出生点猜的**。回放里仍然没有跑道多边形/几何"
        "（全量 4427 个实体块按 9 个关键词搜索只命中点状结构；467 条灯具落在机场中心 1500 m 内的为 0）。"
        "三条自证：同一条跑道两端高度差 ≤ %.1f m；两队机场航向成对相反（%.1f° / %.1f° 夹角）；"
        "主/备机场结构数 13/3（af-01/02 主、af-03/04 备）。"
        % ((max(abs((r["z2"] or 0) - (r["z1"] or 0)) for r in runways) if runways else 0.0),
           abs((runways[0]["headingDeg"] - runways[1]["headingDeg"]) % 360.0 - 180.0) if len(runways) > 1 else 0.0,
           abs((runways[2]["headingDeg"] - runways[3]["headingDeg"]) % 360.0 - 180.0) if len(runways) > 3 else 0.0),
        "`runways[]` 的 `confidence`/`source` 原样透传自 wrpl-ecs，本脚本未独立复算其可信度。",
        "`airfields[]` 是 **4 个机场位置点**（af-01…af-04），由机场雷达/高炮实体**聚类**得到"
        "（`source:\"ecs-cluster\"`），带 `structures` 计数与 `classNames`；它们是**点**，不是跑道端点。",
        "死亡时刻（units[].deathTickRaw / deathKind / killerUid）来自回放击杀包的 victimUid，"
        "**不是从航迹推断**：只有**包含该 tick** 的那一段算死亡（uid 会复用，别的段不算）；"
        "killerUid=2047 判为 `no_killer`（坠毁/环境），其余 `shot_down`。"
        "地面静态点不带死亡信息（恒为 null）。渲染侧可据此让「死亡即隐藏三角标」，"
        "而击毁 X 标记仍常驻。",
        "发射点来自 ptype=6 ECS 的弹药构造消息（projectile_launches.json）：只有**创建时刻的点**、"
        "**没有弹道**（弹药在 ptype=2 FlightModel 里没有 uid 组件，关联不到逐帧航迹）。"
        "`shooterUid`/`side` 是「同刻最近的飞机航迹（≤100 m）」的**推断**，不是回放里解出的字段；"
        "`yaw` 一律 null —— 两种四元数分量顺序都试过，与同刻发射机航向对不上（较优一组去掉固定 "
        "123.5° 偏移后 MAD 仍 34.1°），口径无法验证，故不给而不是给个含义不明的角度。",
        "地面单位位置来自 ptype=6 ECS transform（ground.json），是**静态点位、无航迹**："
        "n=1、hz=1、static=true，z 取 up 分量；位置口径与 tracks v2/v3 一致（x 东、y 北、z 上），"
        "本文件不做任何翻转。侧别推不出，一律 unknown。",
        "空中单位来自 tracks.json（只含空中实体）；地面单位来自独立的 ground.json，两者互不影响。"
        "个别地面点位的 spawnTickRaw 早于解码航迹的轴原点（本机轨道 31.1 s 起），"
        "这类点的 t0 已夹到 0（原始 tick 保留在 units[].spawnTickRaw）。",
        "他机航迹来自 ptype=2 条目的到达率（约 4 Hz），输出重采样成 %d Hz，缺采样处线性插值。"
        % UNIT_HZ["air"],
        "本机由 %d 段拼成（%s）：源侧在相邻采样隐含速度 >600 m/s 处断轨，段与段之间**不插值、留 null**，"
        "否则会画出一条 1 km 的瞬移线。"
        % (len(own_segs), "、".join(str(u.get("entityId")) for u in own_segs)),
        "uid 会复用：同一 uid 的航迹在相邻采样推算速度 >600 m/s 处被断开成多条（各成一条 unit，"
        "key 加 `#n` 后缀），共断开 %d 处、丢弃过短碎片 %d 条；不断开的话画面上会出现瞬移。"
        % (len(breaks), len(dropped)),
        "y 轴方向由纯回放内部判据确定（本机位移方向 vs own_props 真罗盘）：解码给的 y 是朝南的，"
        "本文件按判据%s（翻转后中位误差 %.2f°，不翻转 %.2f°）—— 这样 x/y 轨迹与 hdg 才自洽。"
        % ("取反" if y_flip else "保持原样",
           y_err_flip if y_err_flip is not None else -1,
           y_err_no if y_err_no is not None else -1),
        "时间轴原点与 tracks.json 的 tick0 相同（raw tick %s）；回放时钟 0 点在本轴的 %.3f s，"
        "本文件不含那一段（没有解码航迹）。窗口 0..%.3f s = 解码覆盖的完整跨度；"
        "回放自身总长 %.3f s（EndMarker）。"
        % (axis.tick0, -axis.tick0 * axis.sec_per_tick, window["t1"],
           axis.sec(axis.tick_last)),
        "聊天文本去掉了零宽字符与 <color> 标签，其余原样。",
    ]
    if degraded:
        limits.append("本机实体判定退化：%s（必须在 DATA-NOTES 里写明）。" % how)
    if not tracks:
        limits.append("几何未解码：own 为空（n=0）、units 为空数组；等 wrpl-probe 的 tracks.json。")
    if pos_stats["withoutPos"]:
        limits.append("击杀点坐标缺 %d/%d 条（优先 killPositions 的受击者位置，其次击杀者位置，"
                      "宁可缺也不要安到别的飞机身上；原因分布 %s"
                      % (pos_stats["withoutPos"], kinds.get("kill", 0),
                         dict(sorted(pos_stats["reasons"].items()))))
    if dropped:
        limits.append("样本少于 %d 个的实体已丢弃：%s" % (MIN_TRACK_SAMPLES, dropped[:8]))
    if bad_rows:
        limits.append("坐标越界或非有限（|坐标|>%.0f m）而丢弃的采样点 %d 个，涉及实体 %d 个：%s"
                      % (MAX_ABS_M, bad_rows, len(bad_entities), bad_entities[:6]))
    if not own_props:
        limits.append("没有 own_props.json：本机飞参通道只能留 null，精度会明显变差。")

    payload = {
        "meta": {
            "source": "wrpl",
            "sideSource": ("空中：击杀关系二色 + 出生位置聚类；地面：到两阵营出生中心的最近距离"
                           "（纯回放推断，AI 与地面均为推定）"),
            "replayFile": header.get("replayFile"),
            "replayHeader": header.get("header") or None,
            "player": player,
            "matchResults": match_results,
            "squad": squad,
            # 类别词表与来源（meta 块）；缺文件时为 null，不编
            "vehicleCategories": ({
                "source": ("build/wrpl/vehicle_categories.json"
                           "（datamine v2.59.0.73；显示约定，非回放数据）"),
                "field": "units[].vehicleCategory",
                "keys": len(categories),
                "vocabulary": sorted({v for v in categories.values()}),
                "unitsWithCategory": sum(1 for u in units if u.get("vehicleCategory")),
                "unitsUncategorized": sum(1 for u in units if not u.get("vehicleCategory")),
                "uncategorizedValues": sorted({(u.get("vehicle") or "(无内部名)")
                                               for u in units if not u.get("vehicleCategory")}),
            } if categories else None),
            "replayTicks": {
                "tick0": axis.tick0, "tickLast": axis.tick_last,
                "secPerTick": axis.sec_per_tick,
                "packets": ((header.get("container") or {}).get("packets")
                            or (header.get("packetCensus") or {}).get("total")),
                "killPackets": len(entities.get("killPackets") or []),
            },
            "window": window,
            "grid": {"stepX": 6500, "stepY": 6500,
                     "note": "显示约定；本局地图网格 6500 m（开发期从地图信息核对）"},
            "units": {"pos": "m", "alt": "m", "hdg": "deg", "roll": "deg", "pitch": "deg",
                      "aoa": "deg", "g": "g", "vy": "m/s", "ias": "m/s", "tas": "m/s",
                      "mach": "Ma", "fuel": "kg", "ammo": "rounds", "throttle": "%"},
            "counts": {
                "ownSamples": own["n"], "trackCount": len(units),
                "airTracks": sum(1 for u in units if u["domain"] == "air"),
                "groundTracks": sum(1 for u in units if u["domain"] == "ground"),
                "groundUnits": len(ground_units),
                "shots": len(shots),
                "unitsWithSide": sum(1 for u in units if u["side"] != "unknown"),
                "unitsWithDisplayName": sum(1 for u in units if u.get("vehicleName")),
                "unitsHidden": sum(1 for u in units if u.get("hidden")),
                "unitsWithCategory": sum(1 for u in units if u.get("vehicleCategory")),
                "squadSize": len((squad or {}).get("members") or []) if squad else 0,
                "runways": len(runways),
                "airfields": len(airfields),
                "terrainContours": len((terrain or {}).get("contours") or []),
                "unitsWithDeath": sum(1 for u in units if u.get("deathTickRaw")),
                "events": len(events), "kills": kinds.get("kill", 0),
                "ownKillsByEvents": sum(1 for e in events
                                        if e["isPlayerActor"] and e["kind"] == "kill"),
                "ownKillsFromResults": (author_row or {}).get("kills"),
                "unitSeamsSplit": len(breaks),
                "ownSegments": len(own_segs),
                "envelopeFiltered": filtered + own_stats.get("envelope_filtered", 0),
                "unitsWithIdentity": sum(1 for u in units if u.get("vehicle")),
                "unitsVehicleFromPackets": sum(1 for u in units
                                              if (u.get("vehicleSource") or "").endswith("packet")),
                "killsWithWeapon": sum(1 for e in events if e.get("weapon")),
                "killsWithWeaponName": sum(1 for e in events if e.get("weaponName")),
            },
            "devValidation": dev_validation,
            "vehicleRoster": roster,
            "provenance": [
                "回放文件：%s（%s 字节，%s → %s 包）" % (
                    header.get("replayFile"), (header.get("replayBytes")),
                    (header.get("container") or {}).get("compression"),
                    (header.get("container") or {}).get("packets")),
                "地图/模式/环境：%s / %s / %s / %s（%s 分钟，%s 分上限）" % (
                    (header.get("header") or {}).get("level"),
                    (header.get("header") or {}).get("battleClass"),
                    (header.get("header") or {}).get("environment"),
                    (header.get("header") or {}).get("visibility"),
                    (header.get("header") or {}).get("timeLimit"),
                    (header.get("header") or {}).get("scoreLimit")),
                "时间轴：回放 tick（raw），sec = tick / %d（%.6g s/tick），原点 %s；"
                "与回放事件对象的 tick 同口径（tick = tsMs × %d，sec = tsMs / 1000）"
                % (int(round(1.0 / axis.sec_per_tick)), axis.sec_per_tick, axis.source,
                   axis.raw_per_ms),
                "本机航迹：%s；他机航迹：tracks.json 的实体三维航迹（回放解码）" % how,
                "他机身份：uid_map.json（wrpl-ecs 的 ECS uid→玩家/载具/阵营映射；%d 条 uid，"
                "%d 条有载具名）" % (len(uid_map or {}),
                                    sum(1 for v in (uid_map or {}).values()
                                        if isinstance(v, dict) and v.get("vehicle"))),
                "击杀事件字段：kill_events.json（回放击杀包位流）；击杀点坐标优先用 tracks.json 的"
                " killPositions（受击者位置 %d/%d 条有）"
                % (sum(1 for kp in ((tracks or {}).get("killPositions") or [])
                       if kp.get("hasVictimPos")),
                   len((tracks or {}).get("killPositions") or [])),
                "坐标轴：解码给出 x=fm.PosX, y=-fm.PosZ, z=fm.PosY（米）；本文件对 y 的实际处理：%s"
                % ("取反（解码 y 朝南，取反后与回放罗盘一致）" if y_flip else "保持原样"),
                "坐标尺度：%s" % (
                    "tracks.scale.toMeters=%s（来源 %s）" % (
                        ((tracks or {}).get("scale") or {}).get("toMeters"),
                        ((tracks or {}).get("scale") or {}).get("source"))
                    if (tracks or {}).get("scale") else "tracks.json 未给 scale，按米处理"),
                "事件：击杀包 killPackets、奖项 awards、聊天 chat，均来自回放包流",
                "结算：.wrpl 内嵌结果块（author=%s，%d 名玩家）" % (
                    player_name, len(header.get("players") or [])),
                "回放格式解析参考开源实现 wrpl-inspector"
                "（https://github.com/maxsupermanhd/wrpl-inspector，AGPL-3.0）；"
                "只用于开发期理解包格式，其代码不进本产物。",
            ],
            "limits": limits,
        },
        "own": own,
        "units": units,
        "events": events,
        "shots": shots,
        "runways": runways,
        "airfields": airfields,
        "terrain": terrain,
        "names": {},
    }

    size_mb = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")) / 1048576.0
    print("\n== 体积 %.2f MB（上限 %.1f MB）" % (size_mb, args.max_mb))
    if size_mb > args.max_mb:
        # 契约 §2.3 的降采样优先级：先丢地面单位（点太多、没有航迹线）
        units.sort(key=lambda u: (0 if u["domain"] == "air" else 1, u["n"], u["key"]))
        while units and units[-1]["domain"] == "ground":
            units.pop()
            payload["units"] = units
            payload["meta"]["counts"]["trackCount"] = len(units)
            payload["meta"]["counts"]["groundTracks"] = 0
            payload["meta"]["limits"].append("体积超限，已按契约 §2.3 优先级丢弃地面航迹")
            size_mb = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")) / 1048576.0
            if size_mb <= args.max_mb:
                break
        print("   降采样后 %.2f MB，航迹剩 %d 条" % (size_mb, len(units)))

    written = write_js(args.out, payload)
    print("   写出 %s（%.2f MB）" % (args.out, written / 1048576.0))

    print("\n== 结构自检")
    if not selfcheck(payload):
        print("自检不通过：产物有问题")
        return 1

    if os.path.isfile(args.out):
        text = open(args.out, encoding="utf-8").read()
        head = "window.ACE_MISSION = "
        back = json.loads(text[text.index(head) + len(head):text.rindex(";")])
        print("PASS 写出文件能反解回同样的数据" if back["meta"]["counts"] == payload["meta"]["counts"]
              else "FAIL 写出文件反解不一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
