#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ACE-COMBAT 支线独立复算 / 验收工具（verifier 所有，不采信任何作者自述数字）。

用法：
    python tools/check_ace_mission.py               # 跑全部检查（产物缺失的项标 SKIP）
    python tools/check_ace_mission.py --selftest    # 用自造小样本验证检查逻辑本身有效
    python tools/check_ace_mission.py --rebuild-cache
    python tools/check_ace_mission.py --seed 123 --checks 1,2,4

检查项（契约 v4 §1 §5）：
  1  随机 3 个时刻，本机 z/ias/hdg/g 与 data/raw/rb-session/{state,indicators}.jsonl 对照
  2  事件：窗口内去重 hudmsg 行数 / 独立重数回放击杀包 / 玩家击杀数
  3  结构：契约 §2 字段齐全、时间轴单调、平面通道不混入高度
  4  独立复算回放解码：本机 max/rms 误差（对 8111 真值）、他机关联率、速度包线违规数
  5  不编造性：units 的 z、未知项 `--`、来源与局限说明
  6  v4 回放独立性：生成脚本不读 8111、meta.source、每 unit 的 z 非常数、index.html 不引 dev

本工具只读别人的产物；自己写的东西只有 data/_scratch/ace-verify/** 与 stdout。
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import random
import re
import struct
import sys
import time
import traceback

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ZW_CHARS = "\u200b\u200c\u200d\ufeff\u00ad\u200e\u200f"
_ZW_MAP = dict.fromkeys(map(ord, ZW_CHARS), None)

PASS, FAIL, WARN, SKIP, INFO = "PASS", "FAIL", "WARN", "SKIP", "INFO"

KILL_VERBS = ("击落", "击毁")
CRASH_MARK = "坠毁"
DAMAGE_MARKS = ("致命攻击", "重创", "引燃")
AWARD_MARKS = ("先拔头筹", "最后一击", "首杀")
DISCONNECT_MARKS = ("已掉线", "NET_PLAYER_DISCONNECT")


def clean_msg(s):
    """去掉零宽字符，用于去重键与文本比对。"""
    return (s or "").translate(_ZW_MAP)


def wrap180(d):
    return (d + 180.0) % 360.0 - 180.0


def pearson(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 8:
        return -1.0
    a, b = a[m], b[m]
    a = a - a.mean()
    b = b - b.mean()
    na = math.sqrt(float((a * a).sum()))
    nb = math.sqrt(float((b * b).sum()))
    if na < 1e-9 or nb < 1e-9:
        return -1.0
    return float((a * b).sum()) / (na * nb)


def interp_series(grid_t, grid_v, q):
    """grid_t 单调递增；返回 q 处的线性插值（越界给 nan）。"""
    grid_t = np.asarray(grid_t, dtype=float)
    grid_v = np.asarray(grid_v, dtype=float)
    q = np.asarray(q, dtype=float)
    out = np.full(q.shape, np.nan)
    if grid_t.size < 2:
        return out
    m = (q >= grid_t[0]) & (q <= grid_t[-1])
    if m.any():
        out[m] = np.interp(q[m], grid_t, grid_v)
    return out


def iter_jsonl(path):
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield ln, json.loads(line)
            except Exception:
                yield ln, None


# ---------------------------------------------------------------- JS 字面量解析

class JsLiteralError(Exception):
    pass


_NUM_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


class JsParser:
    """只解析 JS 对象/数组字面量（数据文件够用）：支持单双引号、注释、尾逗号、
    无引号键、true/false/null/undefined/NaN/Infinity。"""

    def __init__(self, text):
        self.s = text
        self.i = 0
        self.n = len(text)

    def skip(self):
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif c == "/" and self.i + 1 < n and s[self.i + 1] == "/":
                j = s.find("\n", self.i)
                self.i = n if j < 0 else j + 1
            elif c == "/" and self.i + 1 < n and s[self.i + 1] == "*":
                j = s.find("*/", self.i + 2)
                self.i = n if j < 0 else j + 2
            else:
                break

    def value(self):
        self.skip()
        if self.i >= self.n:
            raise JsLiteralError("意外的结束")
        c = self.s[self.i]
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c in "\"'`":
            return self.string(c)
        if c.isdigit() or c in "+-.":
            return self.number()
        ident = self.ident()
        consts = {"true": True, "false": False, "null": None, "undefined": None,
                  "NaN": float("nan"), "Infinity": float("inf")}
        if ident in consts:
            return consts[ident]
        if ident == "void":  # void 0
            self.skip()
            if self.i < self.n and self.s[self.i].isdigit():
                self.number()
            return None
        return ident

    def ident(self):
        m = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", self.s[self.i:])
        if not m:
            raise JsLiteralError("无法解析的记号 @%d: %r" % (self.i, self.s[self.i:self.i + 20]))
        self.i += len(m.group(0))
        return m.group(0)

    def key(self):
        self.skip()
        c = self.s[self.i]
        if c in "\"'`":
            return self.string(c)
        if c.isdigit() or c in "+-.":
            return str(self.number())
        return self.ident()

    def number(self):
        m = _NUM_RE.match(self.s, self.i)
        if not m:
            raise JsLiteralError("坏数字 @%d" % self.i)
        self.i = m.end()
        txt = m.group(0)
        return float(txt) if ("." in txt or "e" in txt or "E" in txt) else int(txt)

    def string(self, q):
        self.i += 1
        out = []
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c == "\\":
                self.i += 1
                if self.i >= n:
                    break
                e = s[self.i]
                mapping = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
                           "0": "\0", "\\": "\\", "/": "/", "'": "'", '"': '"', "`": "`"}
                if e == "u":
                    hexs = s[self.i + 1:self.i + 5]
                    try:
                        out.append(chr(int(hexs, 16)))
                    except Exception:
                        out.append("\\u" + hexs)
                    self.i += 5
                    continue
                if e == "x":
                    hexs = s[self.i + 1:self.i + 3]
                    try:
                        out.append(chr(int(hexs, 16)))
                    except Exception:
                        out.append("\\x" + hexs)
                    self.i += 3
                    continue
                out.append(mapping.get(e, e))
                self.i += 1
                continue
            if c == q:
                self.i += 1
                return "".join(out)
            out.append(c)
            self.i += 1
        raise JsLiteralError("字符串未闭合")

    def arr(self):
        self.i += 1
        out = []
        while True:
            self.skip()
            if self.i >= self.n:
                raise JsLiteralError("数组未闭合")
            if self.s[self.i] == "]":
                self.i += 1
                return out
            out.append(self.value())
            self.skip()
            if self.i < self.n and self.s[self.i] == ",":
                self.i += 1
                continue
            if self.i < self.n and self.s[self.i] == "]":
                self.i += 1
                return out
            raise JsLiteralError("数组语法错误 @%d" % self.i)

    def obj(self):
        self.i += 1
        out = {}
        while True:
            self.skip()
            if self.i >= self.n:
                raise JsLiteralError("对象未闭合")
            if self.s[self.i] == "}":
                self.i += 1
                return out
            k = self.key()
            self.skip()
            if self.i >= self.n or self.s[self.i] != ":":
                raise JsLiteralError("对象缺冒号 @%d" % self.i)
            self.i += 1
            out[k] = self.value()
            self.skip()
            if self.i < self.n and self.s[self.i] == ",":
                self.i += 1
                continue
            if self.i < self.n and self.s[self.i] == "}":
                self.i += 1
                return out
            raise JsLiteralError("对象语法错误 @%d" % self.i)


def parse_js_global(path, name="ACE_MISSION"):
    """读 mission-data.js：返回 (对象, 原文)。"""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    idx = text.find(name)
    if idx < 0:
        raise JsLiteralError("找不到 %s" % name)
    j = text.find("{", idx)
    if j < 0:
        raise JsLiteralError("找不到对象起始")
    obj = JsParser(text[j:]).value()
    if not isinstance(obj, dict):
        raise JsLiteralError("ACE_MISSION 不是对象")
    return obj, text


def file_fingerprint(path):
    """mtime + sha256 前 16 位，写进证据，避免新旧产物混淆。"""
    try:
        st = os.stat(path)
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return "%s mtime=%s size=%d sha256=%s" % (
            os.path.basename(path), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            st.st_size, h.hexdigest()[:16])
    except Exception as exc:
        return "%s 指纹失败：%s" % (path, exc)


def load_json_file(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return None


# ---------------------------------------------------------------- 8111 原始数据

def _nearest(pairs_t, pairs, r, tol):
    i = bisect.bisect_left(pairs_t, r)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(pairs_t):
            d = abs(pairs_t[j] - r)
            if best is None or d < best[0]:
                best = (d, pairs[j])
    if best is None or best[0] > tol:
        return None
    return best[1]


def build_raw_cache(raw_dir, cache_path, contact_hz=2.0):
    """把 8111 原始 jsonl 抽成紧凑缓存（本机真值 + 平面接触 + hudmsg 去重行）。"""
    out = {"raw_dir": raw_dir, "files": {}}
    meta_path = os.path.join(raw_dir, "meta.json")
    if os.path.exists(meta_path):
        out["capture_meta"] = json.loads(read_text(meta_path) or "{}")

    # --- map_info 世界边界 ---
    bounds = {"min": [-65536.0, -65536.0], "max": [65536.0, 65536.0],
              "grid_steps": [5500.0, 5500.0], "grid_size": None, "n": 0}
    mi = os.path.join(raw_dir, "map_info.jsonl")
    if os.path.exists(mi):
        for ln, o in iter_jsonl(mi):
            if not o or o.get("ok") is False:
                continue
            d = o.get("data")
            if isinstance(d, dict) and d.get("valid", True):
                if d.get("map_min") and d.get("map_max"):
                    bounds["min"] = [float(d["map_min"][0]), float(d["map_min"][1])]
                    bounds["max"] = [float(d["map_max"][0]), float(d["map_max"][1])]
                if d.get("grid_steps"):
                    bounds["grid_steps"] = [float(x) for x in d["grid_steps"]]
                if d.get("grid_size"):
                    bounds["grid_size"] = [float(x) for x in d["grid_size"]]
                bounds["n"] += 1
    out["map_bounds"] = bounds
    span_x = bounds["max"][0] - bounds["min"][0]
    span_y = bounds["max"][1] - bounds["min"][1]

    def world_xy(nx, ny):
        return (bounds["min"][0] + nx * span_x, -(bounds["min"][1] + ny * span_y))

    # --- state / indicators ---
    st = []
    p = os.path.join(raw_dir, "state.jsonl")
    if os.path.exists(p):
        for ln, o in iter_jsonl(p):
            if not o or o.get("ok") is False or not isinstance(o.get("data"), dict):
                continue
            st.append((float(o["rel"]), o["data"]))
    ind = []
    p = os.path.join(raw_dir, "indicators.jsonl")
    if os.path.exists(p):
        for ln, o in iter_jsonl(p):
            if not o or o.get("ok") is False or not isinstance(o.get("data"), dict):
                continue
            ind.append((float(o["rel"]), o["data"]))
    ind_t = [x[0] for x in ind]

    def g(d, *keys):
        for k in keys:
            v = d.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
        return None

    own = {k: [] for k in ("rel", "z_m", "z_ft", "ias", "tas", "mach", "hdg", "ny",
                           "g_meter", "aoa", "vy", "roll", "pitch", "throttle",
                           "fuel", "ammo", "type", "valid_state", "valid_ind")}
    for rel, sd in st:
        oi = _nearest(ind_t, ind, rel, 0.15)
        idd = oi[1] if oi else {}
        own["rel"].append(rel)
        own["z_m"].append(g(sd, "H, m"))
        own["z_ft"].append((g(idd, "altitude_hour") or 0) * 0.3048 if idd else None)
        own["ias"].append(g(sd, "IAS, km/h"))
        own["tas"].append(g(sd, "TAS, km/h"))
        own["mach"].append(g(sd, "M"))
        own["hdg"].append(g(idd, "compass"))
        own["ny"].append(g(sd, "Ny"))
        own["g_meter"].append(g(idd, "g_meter"))
        own["aoa"].append(g(sd, "AoA, deg"))
        own["vy"].append(g(sd, "Vy, m/s"))
        own["roll"].append(g(idd, "aviahorizon_roll", "bank"))
        own["pitch"].append(g(idd, "aviahorizon_pitch"))
        own["throttle"].append(g(sd, "throttle 1, %"))
        own["fuel"].append(g(sd, "Mfuel, kg"))
        own["ammo"].append(g(idd, "ammo_counter1"))
        own["type"].append(idd.get("type") if idd else None)
        own["valid_state"].append(bool(sd.get("valid")))
        own["valid_ind"].append(bool(idd.get("valid")) if idd else False)

    out["own"] = own

    # --- map_obj：本机平面位置 + 接触点 ---
    pmo = os.path.join(raw_dir, "map_obj.jsonl")
    player = {"rel": [], "x": [], "y": []}
    contacts = []
    frames = 0
    next_keep = -1e9
    dk = 1.0 / max(0.1, contact_hz)
    if os.path.exists(pmo):
        for ln, o in iter_jsonl(pmo):
            if not o or o.get("ok") is False or not isinstance(o.get("data"), list):
                continue
            rel = float(o["rel"])
            frames += 1
            keep = rel >= next_keep
            if keep:
                next_keep = rel + dk
            for e in o["data"]:
                if not isinstance(e, dict) or "x" not in e:
                    continue
                icon = e.get("icon") or "none"
                if icon == "Player":
                    wx, wy = world_xy(float(e["x"]), float(e["y"]))
                    player["rel"].append(rel)
                    player["x"].append(wx)
                    player["y"].append(wy)
                    continue
                if not keep:
                    continue
                if icon.startswith("respawn_base"):
                    continue
                if e.get("type") not in ("aircraft", "ground_model"):
                    continue
                wx, wy = world_xy(float(e["x"]), float(e["y"]))
                contacts.append([round(rel, 3), e.get("type"), icon, e.get("color") or "",
                                 round(wx, 1), round(wy, 1)])
    out["player_xy"] = player
    out["contacts"] = contacts
    out["frames_map_obj"] = frames

    # --- hudmsg 去重 ---
    seen = set()
    hud = []
    ph = os.path.join(raw_dir, "hudmsg.jsonl")
    if os.path.exists(ph):
        for ln, o in iter_jsonl(ph):
            if not o or o.get("ok") is False:
                continue
            d = o.get("data")
            if not isinstance(d, dict):
                continue
            for tag in ("events", "damage"):
                for m in d.get(tag) or []:
                    if not isinstance(m, dict):
                        continue
                    msg = clean_msg(m.get("msg"))
                    key = (m.get("id"), msg, m.get("time"))
                    if key in seen:
                        continue
                    seen.add(key)
                    hud.append({"rel": float(o["rel"]), "id": m.get("id"),
                                "gt": m.get("time"), "msg": msg, "src": tag})
    out["hudmsg"] = hud

    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    except Exception as exc:
        # DSH 沙箱下进程可能没有工作区写权限：缓存写不了不影响本次复算
        print("[提示] 8111 缓存写入失败（%s）：%s" % (cache_path, exc))
    return out


def load_raw(cfg, results):
    raw_dir = cfg["raw_dir"]
    cache = os.path.join(cfg["cache_dir"], "raw8111.json")
    need = cfg["rebuild_cache"] or not os.path.exists(cache)
    if not need:
        try:
            with open(cache, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("raw_dir") == raw_dir:
                return data
        except Exception:
            pass
        need = True
    if not os.path.exists(os.path.join(raw_dir, "state.jsonl")):
        add(results, "raw.8111", "8111 原始数据可读", SKIP,
            "找不到 %s/state.jsonl" % raw_dir)
        return None
    try:
        data = build_raw_cache(raw_dir, cache, cfg["contact_hz"])
    except Exception as exc:
        add(results, "raw.8111", "8111 原始数据可读", FAIL,
            "解析失败：%s" % exc, [traceback.format_exc()[-800:]])
        return None
    add(results, "raw.8111", "8111 原始数据可读", PASS,
        "state %d 帧 / map_obj %d 帧 / 接触 %d / hudmsg 去重 %d 行 / 本机平面 %d 帧" % (
            len(data["own"]["rel"]), data.get("frames_map_obj", 0),
            len(data.get("contacts") or []), len(data.get("hudmsg") or []),
            len(data["player_xy"]["rel"])),
        ["缓存：%s" % cache,
         "抓取元数据：%s" % json.dumps(data.get("capture_meta"), ensure_ascii=False)])
    return data


# ---------------------------------------------------------------- 结果收集

def add(results, rid, title, status, detail, evidence=None):
    results.append({"id": rid, "title": title, "status": status,
                    "detail": detail, "evidence": list(evidence or [])})
    return results[-1]


def status_of(results, rid):
    for r in results:
        if r["id"] == rid:
            return r["status"]
    return None


def get_of(results, rid):
    for r in results:
        if r["id"] == rid:
            return r
    return None


# ---------------------------------------------------------------- 回放击杀包独立重数

def _read_vlq_size(b, p):
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


def count_kill_packets(path):
    """独立按包流分帧重数击杀包（ptype==4 且 payload[:4]==00025858）。"""
    with open(path, "rb") as f:
        buf = f.read()
    p = 0
    last_ts = 0
    ptypes = {}
    kill_ts = []
    nrec = 0
    while p < len(buf):
        try:
            size, p2 = _read_vlq_size(buf, p)
        except Exception:
            break
        if size == 0 or p2 + size > len(buf):
            break
        body = buf[p2:p2 + size]
        p = p2 + size
        nrec += 1
        fb = body[0]
        if fb & 0x10:
            ptype = fb ^ 0x10
            ts = last_ts
            hdr = 1
        else:
            ptype = fb
            ts = struct.unpack_from("<I", body, 1)[0]
            last_ts = ts
            hdr = 5
        ptypes[ptype] = ptypes.get(ptype, 0) + 1
        if ptype == 4 and body[hdr:hdr + 4].hex() == "00025858":
            kill_ts.append(ts)
    pattern = buf.count(b"\x00\x02\x58\x58")
    return {"records": nrec, "ptypes": ptypes, "kill_ticks": sorted(kill_ts),
            "pattern_hits": pattern, "bytes": len(buf)}


# ---------------------------------------------------------------- 航迹分析（numpy）

def normalize_tracks(t):
    """把不同写法的 tracks.json 归一到契约 §1.0 的形状（不改变数值）。"""
    if not isinstance(t, dict):
        return t
    notes = []
    if not t.get("units"):
        for key in ("tracks", "entities", "objects", "actorTracks"):
            if isinstance(t.get(key), list) and t.get(key):
                t["units"] = t[key]
                notes.append("units 取自 %s" % key)
                break
    for u in t.get("units") or []:
        if not isinstance(u, dict):
            continue
        if "entityId" not in u:
            for k in ("id", "uid", "key", "entity", "actorId"):
                if k in u:
                    u["entityId"] = u[k]
                    notes.append("entityId 取自 %s" % k)
                    break
    if "secPerTick" not in t:
        for k in ("secondsPerTick", "sec_per_tick"):
            if k in t:
                t["secPerTick"] = t[k]
                notes.append("secPerTick 取自 %s" % k)
                break
    t["_normalize_notes"] = notes
    return t


def load_tracks(path):
    with open(path, "r", encoding="utf-8") as f:
        return normalize_tracks(json.load(f))


def ent_array(unit):
    s = unit.get("samples") or unit.get("track") or unit.get("points") or []
    if s and isinstance(s[0], dict):
        conv = []
        for p in s:
            tick = p.get("tick", p.get("t", p.get("ts")))
            x = p.get("x")
            y = p.get("y")
            z = p.get("z", p.get("alt"))
            conv.append([tick, x, y, z])
        s = [row for row in conv if row[0] is not None and row[1] is not None
             and row[2] is not None and row[3] is not None]
    a = np.asarray(s, dtype=float) if s else np.zeros((0, 4))
    if a.ndim != 2 or a.shape[0] < 1:
        return None
    if a.shape[1] < 4:
        pad = np.full((a.shape[0], 4 - a.shape[1]), np.nan)
        a = np.hstack([a, pad])
    return a[np.argsort(a[:, 0])]


def speed_profile(t, x, y):
    t = np.asarray(t, dtype=float)
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    m = np.isfinite(t) & np.isfinite(x) & np.isfinite(y)
    t, x, y = t[m], x[m], y[m]
    if t.size < 4:
        return None, None
    dt = np.diff(t)
    d = np.hypot(np.diff(x), np.diff(y))
    good = dt > 1e-9
    if int(good.sum()) < 3:
        return None, None
    return 0.5 * (t[1:] + t[:-1])[good], d[good] / dt[good]


def fit_offset(ent_t, ent_v, ref_t, ref_v, res_hint=0.5, rng=None):
    """在 offset 上扫描，返回 (offset_s, corr)。rel = ent_t + offset。"""
    if ent_t is None or ref_t is None or ent_t.size < 8 or ref_t.size < 8:
        return None, -1.0
    span = float(ref_t[-1] - ref_t[0] + ent_t[-1] - ent_t[0]) + 60.0
    lo, hi = -0.35 * span, span
    best = (None, -1.0)
    for off in np.arange(lo, hi, res_hint):
        v = interp_series(ref_t, ref_v, ent_t + off)
        c = pearson(ent_v, v)
        if c > best[1]:
            best = (float(off), c)
    if best[0] is None:
        return None, -1.0
    off0 = best[0]
    for off in np.arange(off0 - res_hint, off0 + res_hint + 1e-9, res_hint / 25.0):
        v = interp_series(ref_t, ref_v, ent_t + off)
        c = pearson(ent_v, v)
        if c > best[1]:
            best = (float(off), c)
    return best


def fit_time_mapping(tracks, raw, results):
    """独立拟合 tick → 抓取 rel 的映射（不采信作者 offset），用速度/高度剖面互相关。"""
    it = {}
    if not tracks:
        return it
    sec = tracks.get("secPerTick")
    rate0 = None
    if isinstance(sec, (int, float)) and sec > 0:
        rate0 = sec if sec > 1 else 1.0 / sec
    units = tracks.get("units") or []
    ticks = []
    for u in units:
        a = ent_array(u)
        if a is not None and a.shape[0]:
            ticks.append((a[0, 0], a[-1, 0]))
    tick_lo = min(t[0] for t in ticks) if ticks else 0.0
    tick_hi = max(t[1] for t in ticks) if ticks else 0.0
    tick_span = max(1e-9, tick_hi - tick_lo)

    cand_rates = []

    def _plausible(r):
        # tick_span / r 是折算出的秒数：回放长度应落在 40 s ~ 2 h 之间
        return bool(r) and 40.0 <= (tick_span / r) <= 7200.0
    if _plausible(rate0):
        # 作者 secPerTick 已与 header.clock 互证（且被我独立分帧的击杀包 tick 佐证），
        # 不再当自由参数扫备选 rate：多一个自由度会在断轨后的短段上把 offset 拉偏。
        cand_rates = [rate0]
    else:
        for r in (256472.0, 256000.0, 262144.0):
            if _plausible(r):
                cand_rates.append(r)
        if not cand_rates:
            cand_rates = [rate0 or 256472.0]

    pxy = raw.get("player_xy") or {}
    ref_t0 = np.asarray(pxy.get("rel") or [], dtype=float)
    ref_t = ref_t0
    ref_v = None
    if ref_t0.size >= 8:
        _rt, ref_v = speed_profile(ref_t0, pxy.get("x"), pxy.get("y"))
        if _rt is not None:
            ref_t = _rt
        else:
            ref_v = None
    ow = raw.get("own") or {}
    ref_zt = np.asarray(ow.get("rel") or [], dtype=float)
    ref_zv = np.asarray([v if v is not None else np.nan for v in (ow.get("z_m") or [])], dtype=float)
    if ref_t.size < 8 and ref_zt.size < 8:
        return {"ok": False, "note": "8111 没有可用的本机剖面"}

    # 候选实体：速度量级接近本机、时长足够
    own_med = float(np.nanmedian(ref_v)) if ref_v is not None and ref_v.size else None
    cands = []
    for u in units:
        a = ent_array(u)
        if a is None or a.shape[0] < 300:
            continue
        t_tick = a[:, 0]
        if (t_tick[-1] - t_tick[0]) * (1.0 / cand_rates[0]) < 40.0:
            continue
        cands.append(u)
    if not cands:
        cands = [u for u in units if ent_array(u) is not None]
    if not cands:
        return {"ok": False, "note": "tracks.json 没有实体样本"}

    best = None
    for rate in cand_rates:
        sec_use = 1.0 / rate
        for u in cands:
            a = ent_array(u)
            t_rep = (a[:, 0] - tick_lo) * sec_use
            st, sv = speed_profile(t_rep, a[:, 1], a[:, 2])
            series = []
            if sv is not None and ref_v is not None:
                series.append(("speed", st, sv, ref_t, ref_v))
            az = np.asarray([v if np.isfinite(v) else np.nan for v in a[:, 3]], dtype=float)
            zt = t_rep[np.isfinite(az)]
            zv = az[np.isfinite(az)]
            if zt.size >= 8 and ref_zt.size >= 8:
                series.append(("alt", zt, zv, ref_zt, ref_zv))
            off, score, chans = joint_fit_offset(series)
            if off is None:
                continue
            if best is None or score > best["score"]:
                best = {"score": score, "offset_s": off, "rate": rate, "sec_per_tick": sec_use,
                        "entity": u.get("entityId"), "n_samples": int(a.shape[0]),
                        "tick_lo": float(tick_lo), "tick_hi": float(tick_hi),
                        "channels": {k: round(float(v), 3) for k, v in chans.items()}}
    if best is None:
        return {"ok": False, "note": "无法拟合时间映射"}
    best["ok"] = best["score"] >= 0.5
    best["author_sec_per_tick"] = sec
    best["ticks_per_sec_author"] = rate0
    best["note"] = ("最佳实体 %s，rate=%.1f ticks/s，offset=%.2f s，剖面相关 %.3f"
                    % (best["entity"], best["rate"], best["offset_s"], best["score"]))
    add(results, "4a.time-map", "tick→抓取时间映射（独立拟合）", PASS if best["ok"] else WARN,
        best["note"],
        ["通道相关：%s" % json.dumps(best["channels"], ensure_ascii=False),
         "作者 secPerTick=%s（折合 %.1f ticks/s），复算 %.6f" % (
             best["author_sec_per_tick"], rate0 or -1, best["sec_per_tick"]),
         "tick 范围 %.0f..%.0f（%.0f s）" % (best["tick_lo"], best["tick_hi"],
                                             (best["tick_hi"] - best["tick_lo"]) * best["sec_per_tick"])])
    return best


def _decimate(t, v, max_n=1200):
    """粗扫用的降采样（按步长抽点，不做平滑）。"""
    t = np.asarray(t, dtype=float)
    v = np.asarray(v, dtype=float)
    if t.size <= max_n:
        return t, v
    step = int(math.ceil(t.size / float(max_n)))
    return t[::step], v[::step]


def joint_fit_offset(series, res=0.5, max_n=1200):
    """多通道联合扫描 offset：速度剖面与高度剖面在同一个 offset 上一起打分。

    单通道各自扫描时，周期性航迹会出现多个等高假峰（自测样本里 10 s 与 45.8 s
    相关都到 1.000）；联合打分让只有真 offset 才同时对齐两个通道。
    粗扫用降采样提速，细扫用原始分辨率（±1.5 s，步长 res/25）保证 0.02 s 精度。

    series: [(名字, t, v, ref_t, ref_v), ...]；返回 (offset_s, score, {通道: 相关})
    """
    usable = []
    for name, t, v, rt, rv in series:
        if t is None or rt is None or np.size(t) < 8 or np.size(rt) < 8:
            continue
        t = np.asarray(t, dtype=float)
        v = np.asarray(v, dtype=float)
        rt = np.asarray(rt, dtype=float)
        rv = np.asarray(rv, dtype=float)
        if np.nanstd(v) < 1e-9 or np.nanstd(rv) < 1e-9:
            continue  # 常数通道没有信息量（例如地面单位的 z）
        usable.append((name, t, v, rt, rv))
    if not usable:
        return None, -1.0, {}
    span = (max(float(t[-1] - t[0]) for _, t, _, _, _ in usable)
            + max(float(rt[-1] - rt[0]) for _, _, _, rt, _ in usable))
    lo, hi = -0.35 * span, span

    def sc(off, subset):
        chans = {}
        tot = 0.0
        for name, t, v, rt, rv in subset:
            c = pearson(v, interp_series(rt, rv, t + off))
            chans[name] = c
            tot += max(c, -1.0)
        return tot / len(subset), chans

    coarse = []
    for name, t, v, rt, rv in usable:
        ct, cv = _decimate(t, v, max_n)
        crt, crv = _decimate(rt, rv, max_n)
        coarse.append((name, ct, cv, crt, crv))

    best_s, best_o = -2.0, None
    for off in np.arange(lo, hi, res):
        s, _ = sc(float(off), coarse)
        if s > best_s + 1e-9:      # 严格大于：并列时保留先扫到的（offset 更小）
            best_s, best_o = s, float(off)
    if best_o is None:
        return None, -1.0, {}
    final_s, final_o, final_ch = best_s, best_o, {}
    for off in np.arange(best_o - 3 * res, best_o + 3 * res + 1e-9, res / 25.0):
        s, ch = sc(float(off), usable)
        if s > final_s + 1e-9:
            final_s, final_o, final_ch = s, float(off), ch
    if not final_ch:
        final_s, final_ch = sc(final_o, usable)
    return final_o, final_s, final_ch


def procrustes_fit(P, Q, allow_reflection=True):
    P = np.asarray(P, dtype=float)
    Q = np.asarray(Q, dtype=float)
    m = np.isfinite(P).all(1) & np.isfinite(Q).all(1)
    P, Q = P[m], Q[m]
    if P.shape[0] < 3:
        return None
    Pc, Qc = P.mean(0), Q.mean(0)
    A, B = P - Pc, Q - Qc
    H = A.T @ B
    U, S, Vt = np.linalg.svd(H)
    if allow_reflection:
        R = Vt.T @ U.T
        scale = float(S.sum()) / max(1e-12, float((A * A).sum()))
    else:
        d = float(np.sign(np.linalg.det(Vt.T @ U.T)) or 1.0)
        D = np.diag([1.0, d])
        R = Vt.T @ D @ U.T
        scale = float((S * np.array([1.0, d])).sum()) / max(1e-12, float((A * A).sum()))
    t = Qc - scale * (R @ Pc)
    return {"scale": scale, "R": R, "t": t, "det": float(np.linalg.det(R)), "n": int(P.shape[0])}


def apply_sim(sim, P):
    P = np.asarray(P, dtype=float)
    return (sim["scale"] * (P @ sim["R"].T)) + sim["t"]


# ---------------------------------------------------------------- 检查 1：本机通道 vs 8111 真值

def sample_truth(raw, rel, keys=("z_m", "z_ft", "ias", "tas", "mach", "hdg", "ny",
                                 "g_meter", "aoa", "vy")):
    ow = raw.get("own") or {}
    t = np.asarray(ow.get("rel") or [], dtype=float)
    if t.size == 0:
        return None
    i = int(np.searchsorted(t, rel))
    out = {}
    for k in keys:
        arr = np.asarray([v if v is not None else np.nan for v in (ow.get(k) or [])], dtype=float)
        if arr.size != t.size:
            out[k] = None
            continue
        j = None
        bd = None
        for c in (i - 1, i, i + 1):
            if 0 <= c < t.size:
                d = abs(t[c] - rel)
                if bd is None or d < bd:
                    bd, j = d, c
        v = None
        if j is not None and bd is not None and bd <= 0.15 and np.isfinite(arr[j]):
            v = float(arr[j])
        else:
            q = interp_series(t, arr, np.array([rel]))[0]
            v = float(q) if np.isfinite(q) else None
        out[k] = v
    return out


def fit_mission_offset(own_t, own_val, ref_t, ref_val, keys=None):
    off, c = fit_offset(own_t, own_val, ref_t, ref_val)
    return off, c


def raw_facts(cfg, raw, results):
    """8111 原始真值基线：不依赖任何交付物就能独立算出来的数字。"""
    hud = raw.get("hudmsg") or []
    kinds = {}
    actors = {}
    for h in hud:
        k = classify_msg(h["msg"])
        kinds[k] = kinds.get(k, 0) + 1
        if k == "kill":
            a = actor_text(h["msg"]).strip()
            actors[a] = actors.get(a, 0) + 1
    top = sorted(actors.items(), key=lambda kv: -kv[1])[:8]
    ow = raw.get("own") or {}
    rel = ow.get("rel") or []
    pk = None
    if cfg.get("payload_bin") and os.path.exists(cfg["payload_bin"]):
        try:
            pk = count_kill_packets(cfg["payload_bin"])
        except Exception as exc:
            pk = {"error": str(exc)}
    ev = ["hudmsg 去重（整个抓取）%d 行：%s" % (len(hud), json.dumps(kinds, ensure_ascii=False)),
          "rel<60 s（上一局尾巴）的行 %d 行" % sum(1 for h in hud if h["rel"] < 60),
          "击杀者计数（前 8）：%s" % json.dumps(top, ensure_ascii=False)]
    if pk and "kill_ticks" in pk:
        ev.append("payload.bin 独立分帧：%d 条记录，击杀包 %d 个（ptype 前 8：%s）" % (
            pk["records"], len(pk["kill_ticks"]),
            json.dumps({str(k): v for k, v in sorted(pk["ptypes"].items(), key=lambda kv: -kv[1])[:8]})))
        ev.append("字节模式 00 02 58 58 出现 %d 次（仅参考，可能含误命中）" % pk["pattern_hits"])
    elif pk and pk.get("error"):
        ev.append("payload.bin 解析失败：%s" % pk["error"])
    else:
        ev.append("payload.bin 不存在：%s" % cfg.get("payload_bin"))
    ev.append("8111 本机 state 帧 %d，跨度 %.1f s；平面位置帧 %d；接触点 %d"
              % (len(rel), (rel[-1] - rel[0]) if rel else -1,
                 len((raw.get("player_xy") or {}).get("rel") or []),
                 len(raw.get("contacts") or [])))
    add(results, "0.raw-facts", "8111 原始真值基线（独立复算）", PASS,
        "hudmsg 去重 %d 行（击杀 %d / 伤害 %d / 掉线 %d）；回放击杀包 %s" % (
            len(hud), kinds.get("kill", 0), kinds.get("damage", 0), kinds.get("disconnect", 0),
            len(pk["kill_ticks"]) if pk and "kill_ticks" in pk else "未独立复核"), ev)


def check_own(cfg, md, raw, mapping, results, rng):
    own = md.get("own")
    if not isinstance(own, dict):
        add(results, "1.own-truth", "本机 z/ias/hdg/g 与 8111 真值", FAIL,
            "mission-data.js 里没有 own 对象")
        return
    need = ("t0", "hz", "n", "x", "y", "z", "ias", "hdg", "g")
    missing = [k for k in need if k not in own]
    if missing:
        add(results, "1.own-truth", "本机 z/ias/hdg/g 与 8111 真值", FAIL,
            "own 缺字段：%s" % missing)
        return
    meta = md.get("meta") or {}
    source = meta.get("source")
    t0 = float(own["t0"])
    hz = float(own["hz"]) if own.get("hz") else 0.0
    n = int(own.get("n") or 0)

    def col(k):
        v = own.get(k)
        if not isinstance(v, list) or len(v) != n:
            return None
        return np.asarray([np.nan if x is None else float(x) for x in v], dtype=float)

    x, y, z = col("x"), col("y"), col("z")
    ias, hdg, g, mach = col("ias"), col("hdg"), col("g"), col("mach")
    if hz <= 0 or n < 8 or x is None or y is None:
        reason = ("own.n=%s：几何尚未落地/未导出（不是数值错误，等 tracks.json）" % n) \
            if n == 0 else ("own 采样参数不合法：hz=%s n=%s x=%s y=%s"
                            % (hz, n, x is not None, y is not None))
        add(results, "1.own-truth", "本机 z/ias/hdg/g 与 8111 真值", FAIL, reason)
        return
    tx = t0 + np.arange(n) / hz

    # 时间映射：own 轴 → 抓取 rel
    pxy = raw.get("player_xy") or {}
    ref_t0 = np.asarray(pxy.get("rel") or [], dtype=float)
    ref_t = ref_t0
    ref_v = None
    if ref_t0.size >= 8:
        _rt, ref_v = speed_profile(ref_t0, pxy.get("x"), pxy.get("y"))
        if _rt is not None:
            ref_t = _rt
        else:
            ref_v = None
    ow = raw.get("own") or {}
    ref_zt = np.asarray(ow.get("rel") or [], dtype=float)
    ref_zv = np.asarray([v if v is not None else np.nan for v in (ow.get("z_m") or [])], dtype=float)
    M0, axis, corr, fit_notes = 0.0, "capture", None, []
    if abs(t0) < 1.0:
        series = []
        st, sv = speed_profile(tx, x, y)
        if sv is not None and ref_v is not None:
            series.append(("速度", st, sv, ref_t, ref_v))
        if z is not None and np.isfinite(z).sum() >= 8 and ref_zt.size >= 8:
            good = np.isfinite(z)
            series.append(("高度", tx[good], z[good], ref_zt, ref_zv))
        off, c, chans = joint_fit_offset(series)
        if mapping and mapping.get("ok"):
            # 4a 的 offset 由回放航迹与 8111 平面位置共同确定，4c 的平面残差 rms≈6 m 佐证它是对的；
            # 本机自己的剖面单独拟合容易落在弱相关假峰上（实测相关只有 0.6），故以 4a 为准。
            axis = "replay"
            # mapping.offset_s 定义在 (tick - tick_lo)×sec 轴上，tick_lo 是 tracks 全部实体的最小 tick；
            # mission-data 的轴原点 origin 可能与它不同，必须用 mapping 自己记录的 tick_lo 换算。
            tick_lo_s = float(mapping.get("tick_lo") or 0.0) * float(mapping.get("sec_per_tick") or 0.0)
            M0 = float(mapping["offset_s"]) - tick_lo_s + derive_axis_origin(md)[0]
            corr = float(mapping.get("score", -1.0))
            fit_notes = ["offset 取 4a 独立拟合结果并换算到本数据轴：M0=%.2f s（4a 原始 offset %.2f，"
                         "轴修正 %+.2f；剖面相关 %.3f；由 4c 平面残差佐证）"
                         % (M0, float(mapping["offset_s"]), M0 - float(mapping["offset_s"]), corr)]
            if off is not None:
                fit_notes.append("本机自身剖面单独拟合给 %.2f s（相关 %.3f），与 4a 相差 %.2f s"
                                 % (off, c, abs(off - M0)))
        elif off is not None:
            axis = "replay"
            M0 = float(off)
            corr = float(c)
            fit_notes = ["%s剖面相关 %.3f" % (k, v) for k, v in chans.items()]
            fit_notes.append("联合最佳 offset %.2f s（相关 %.3f）" % (M0, corr))
        else:
            axis = "unknown"
    else:
        fit_notes = ["own.t0=%.1f > 1，按抓取相对轴（offset=0）" % t0]

    if axis == "unknown":
        add(results, "1.own-truth", "本机 z/ias/hdg/g 与 8111 真值", SKIP,
            "own.t0≈0 但无法用剖面拟合出回放轴→抓取时间映射，无法取真值对照",
            fit_notes)
        return

    # 1b：整段序列复算（比 3 个随机点更有说服力）
    own_rel = tx + M0
    _ow = raw.get("own") or {}
    tru_t = np.asarray(_ow.get("rel") or [], dtype=float)

    def truth_arr(key):
        return np.asarray([v if v is not None else np.nan for v in (_ow.get(key) or [])], dtype=float)

    # 交付口径的量纲：契约没钉死 ias/tas 量纲，交付方在 meta.units 里声明；用 8111 真值反推校验
    decl_units = meta.get("units") if isinstance(meta.get("units"), dict) else {}
    ias_scale, ias_note = 1.0, ""
    if ias is not None and int(np.isfinite(ias).sum()) >= 50 and tru_t.size >= 8:
        tv0 = interp_series(tru_t, truth_arr("ias"), own_rel)
        mm0 = np.isfinite(tv0) & np.isfinite(ias) & (tv0 > 100)
        if int(mm0.sum()) >= 50:
            ratio = float(np.median(ias[mm0] / tv0[mm0]))
            if 0.9 <= ratio <= 1.1:
                ias_note = "own.ias 与 8111 IAS 同量纲（km/h），比值 %.3f" % ratio
            elif 0.24 <= ratio <= 0.32:
                ias_scale = 3.6
                ias_note = ("own.ias 是 m/s（与 8111 km/h 的比值 %.3f ≈ 1/3.6），比较时 ×3.6 折算"
                            % ratio)
            elif 0.48 <= ratio <= 0.60:
                ias_scale = 1.852
                ias_note = "own.ias 是节（比值 %.3f ≈ 1/1.852），比较时 ×1.852 折算" % ratio
            else:
                ias_note = "own.ias/8111 比值 %.3f 不是常见换算，按原值比较" % ratio
            if decl_units.get("ias"):
                ias_note += "；meta.units.ias=%r" % decl_units.get("ias")

    lines1b = []
    if z is not None and tru_t.size >= 8:
        for tname, arr in (("H,m", truth_arr("z_m")),
                           ("（参考）altitude_hour×0.3048（仪表读数，不参与判定）", truth_arr("z_ft"))):
            v = interp_series(tru_t, arr, own_rel)
            m = np.isfinite(v) & np.isfinite(z)
            if int(m.sum()) < 8:
                continue
            d = z[m] - v[m]
            dz = float(np.mean(d))
            d0 = d - dz
            lines1b.append("own.z vs 8111 %s：n=%d 原始 max=%.1f rms=%.1f m；去常数 datum %.1f m 后 "
                           "max=%.1f rms=%.1f m"
                           % (tname, int(m.sum()), float(np.max(np.abs(d))),
                              float(np.sqrt(np.mean(d ** 2))), dz,
                              float(np.max(np.abs(d0))), float(np.sqrt(np.mean(d0 ** 2)))))
    for label, col, key, scale in (("own.ias vs 8111 IAS(km/h)", ias, "ias", ias_scale),
                                   ("own.mach vs 8111 M", mach, "mach", 1.0)):
        if col is None or tru_t.size < 8:
            continue
        v = interp_series(tru_t, truth_arr(key), own_rel)
        m = np.isfinite(v) & np.isfinite(col)
        if int(m.sum()) < 8:
            continue
        d = col[m] * scale - v[m]
        lines1b.append("%s：n=%d max=%.3f rms=%.3f%s"
                       % (label, int(m.sum()), float(np.max(np.abs(d))),
                          float(np.sqrt(np.mean(d ** 2))),
                          ("（×%g 折算）" % scale) if scale != 1.0 else ""))
    if lines1b:
        ok1b = True
        for l in lines1b:
            if l.startswith("own.z") and "参考" not in l:
                mm = re.search(r"去常数 datum [-0-9.]+ m 后 max=([-0-9.]+) rms=([-0-9.]+)", l)
                if mm and (float(mm.group(1)) > 50.0 or float(mm.group(2)) > 15.0):
                    ok1b = False
        add(results, "1b.own-series", "本机整段序列 vs 8111 真值独立复算（z/ias/mach）",
            PASS if ok1b else WARN,
            ("；".join(lines1b))[:400], lines1b + ["回放源与 8111 的高度基准可能差一个常数 datum，"
                                                   "故同时报原始偏差与去常数后的偏差"])
    else:
        add(results, "1b.own-series", "本机整段序列 vs 8111 真值独立复算（z/ias/mach）", SKIP,
            "没有可比通道或重叠样本不足")

    idxs = sorted(rng.sample(range(2, n - 2), 3)) if n > 6 else [n // 2]
    rows = []
    devs = {"z": [], "ias": [], "hdg": [], "g": []}
    for i in idxs:
        rel = float(tx[i]) + M0
        tr = sample_truth(raw, rel)
        if tr is None:
            continue
        row = {"index": i, "t_own": round(float(tx[i]), 3), "rel": round(rel, 3)}
        if z is not None and np.isfinite(z[i]):
            cand = [abs(z[i] - v) for v in (tr["z_m"], tr["z_ft"]) if v is not None]
            d = min(cand) if cand else None
            row["z"] = [round(float(z[i]), 2), round(tr["z_m"], 2) if tr["z_m"] is not None else None,
                        round(tr["z_ft"], 2) if tr["z_ft"] is not None else None,
                        round(d, 2) if d is not None else None]
            if d is not None:
                devs["z"].append(d)
        if ias is not None and np.isfinite(ias[i]):
            own_ias_kmh = float(ias[i]) * ias_scale
            d = abs(own_ias_kmh - tr["ias"]) if tr["ias"] is not None else None
            row["ias"] = [round(float(ias[i]), 2), round(own_ias_kmh, 2), tr["ias"],
                          round(d, 2) if d is not None else None]
            if d is not None:
                devs["ias"].append(d)
        if hdg is not None and np.isfinite(hdg[i]):
            slow = tr["ias"] is not None and tr["ias"] < 50.0
            if slow or tr["hdg"] is None:
                row["hdg"] = [round(float(hdg[i]), 2), tr["hdg"],
                              "跳过（8111 空速 %s km/h，地面/低速航向无意义）" % tr["ias"]]
            else:
                d = abs(wrap180(hdg[i] - tr["hdg"]))
                row["hdg"] = [round(float(hdg[i]), 2), tr["hdg"], round(d, 2)]
                devs["hdg"].append(d)
        if g is not None and np.isfinite(g[i]):
            cand = [abs(g[i] - v) for v in (tr["ny"], tr["g_meter"]) if v is not None]
            d = min(cand) if cand else None
            row["g"] = [round(float(g[i]), 3), tr["ny"], tr["g_meter"],
                        round(d, 3) if d is not None else None]
            if d is not None:
                devs["g"].append(d)
        rows.append(row)

    def mx(k):
        return max(devs[k]) if devs[k] else None

    ev_lines = [json.dumps(r, ensure_ascii=False) for r in rows]
    ev_lines.append("时间映射：axis=%s offset=%.2f s 相关=%s" % (axis, M0, corr))
    ev_lines += fit_notes
    if ias_note:
        ev_lines.append("量纲核对：%s" % ias_note)
    ev_lines.append("容差：严格 z≤2 m / ias≤2 km/h / hdg≤1° / g≤0.05；回放源按 §5.2 z≤50 m、hdg≤5°")

    zdev, idev, hdev, gdev = mx("z"), mx("ias"), mx("hdg"), mx("g")
    strict_ok = (zdev is not None and zdev <= 2.0
                 and (idev is None or idev <= 2.0)
                 and (hdev is None or hdev <= 1.0)
                 and (gdev is None or gdev <= 0.05))
    if z is None or np.isfinite(z).sum() == 0:
        add(results, "1.own-truth", "本机 z/ias/hdg/g 与 8111 真值", FAIL,
            "own.z 全空：契约 §2.2 要求 z 为三维高度，不能缺")
        return
    decode_ok = (zdev is not None and zdev <= 50.0) and (hdev is None or hdev <= 5.0)
    if strict_ok:
        add(results, "1.own-truth", "本机 z/ias/hdg/g 与 8111 真值", PASS,
            "3 个随机时刻全部落在严格容差内（本机通道与 8111 同源）",
            ev_lines + ["最大偏差：z=%s m ias=%s km/h hdg=%s° g=%s" % (zdev, idev, hdev, gdev)])
        return
    if source == "wrpl" and decode_ok:
        extra = []
        if idev is not None and idev > 2.0:
            extra.append("ias 与 8111 差 %.2f km/h（回放源，不要求相等）" % idev)
        if gdev is not None and gdev > 0.05:
            extra.append("g 与 8111 差 %.3f（回放源，不要求相等）" % gdev)
        add(results, "1.own-truth", "本机 z/ias/hdg/g 与 8111 真值", PASS,
            "回放源：z/航向在解码容差内（z≤50 m、hdg≤5°）；ias/g 不要求与 8111 相等。"
            "严格 8111 容差不适用（契约 v4 §1）",
            ev_lines + ["最大偏差：z=%s m ias=%s km/h hdg=%s° g=%s" % (zdev, idev, hdev, gdev)] + extra)
        return
    add(results, "1.own-truth", "本机 z/ias/hdg/g 与 8111 真值", FAIL,
        "偏差超出容差：z=%s m（≤50）ias=%s km/h（≤2）hdg=%s°（≤5）g=%s（≤0.05）"
        % (zdev, idev, hdev, gdev),
        ev_lines)


# ---------------------------------------------------------------- 检查 2：事件

def derive_axis_origin(md):
    """数据驱动判定 mission 轴原点 origin：raw_sec = t + origin。

    候选两种约定：raw 轴（t = replayTick×secPerTick，origin=0）与
    tick0 原点轴（t = (replayTick-tick0)×secPerTick，origin=tick0×secPerTick）。
    用所有事件的 t 与 replayTick 的吻合数投票，返回 (origin, note, bad_count)。
    """
    meta = md.get("meta") or {}
    rt = meta.get("replayTicks") or {}
    evs = md.get("events") or []
    try:
        spt = float(rt.get("secPerTick"))
        tick0 = float(rt.get("tick0") or 0.0)
    except Exception:
        return 0.0, "replayTicks 不可用，按 origin=0 处理", 0
    cands = [("raw 轴（t = replayTick×secPerTick）", 0.0),
             ("tick0 原点轴（t = (replayTick-tick0)×secPerTick）", tick0 * spt)]
    scored = []
    for name, off in cands:
        bad = n = 0
        for e in evs:
            rtv, et = e.get("replayTick"), e.get("t")
            if isinstance(rtv, (int, float)) and isinstance(et, (int, float)):
                n += 1
                if abs(float(et) - (float(rtv) * spt - off)) > 0.02:
                    bad += 1
        scored.append((bad, n, name, off))
    scored.sort(key=lambda x: x[0])
    bad, n, name, off = scored[0]
    note = "轴判定：%s（%d/%d 条 event.t 吻合；候选 %s）" % (
        name, n - bad, n,
        "，".join("%s 偏差 %d" % (s[2].split("（")[0], s[0]) for s in scored))
    return off, note, bad


def classify_msg(msg):
    if any(v in msg for v in KILL_VERBS):
        return "kill"
    if CRASH_MARK in msg:
        return "crash"
    if any(v in msg for v in AWARD_MARKS):
        return "award"
    if any(v in msg for v in DISCONNECT_MARKS):
        return "disconnect"
    if any(v in msg for v in DAMAGE_MARKS):
        return "damage"
    return "other"


def actor_text(msg):
    cut = len(msg)
    for v in list(KILL_VERBS) + list(DAMAGE_MARKS) + [" 已", " 完成", " 先"]:
        i = msg.find(v)
        if 0 < i < cut:
            cut = i
    return msg[:cut]


def mission_axis(md, mapping):
    """返回 (mission_t → 抓取 rel 的加性偏移, 轴名)。"""
    meta = md.get("meta") or {}
    win = meta.get("window") or {}
    t0 = win.get("t0")
    try:
        t0f = float(t0)
    except Exception:
        return None, "unknown"
    if mapping and mapping.get("ok"):
        return float(mapping["offset_s"]) + t0f, "replay"
    if abs(t0f) > 1.0:
        return 0.0, "capture"
    return None, "unknown"


def _name_match(a, b):
    """玩家名匹配：header.players 的 name 不含 clanTag，纯等号会漏。"""
    a = clean_msg(a or "").strip()
    b = clean_msg(b or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    return (len(a) >= 3 and a in b) or (len(b) >= 3 and b in a)


def header_player_kills(cfg, player_name):
    p = cfg.get("header_path")
    if not p or not os.path.exists(p):
        return None, "header.json 不存在"
    try:
        with open(p, "r", encoding="utf-8") as f:
            h = json.load(f)
    except Exception as exc:
        return None, "header.json 解析失败：%s" % exc
    cand = None
    if isinstance(h, dict):
        for key in ("matchResults", "results", "players", "raw"):
            v = h.get(key)
            if isinstance(v, dict):
                for k2 in ("players", "player"):
                    if isinstance(v.get(k2), list):
                        cand = v[k2]
                        break
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                cand = v
            if cand:
                break
        if cand is None:
            for v in h.values():
                if isinstance(v, list) and v and isinstance(v[0], dict) and any(
                        "kills" in (x or {}) for x in v):
                    cand = v
                    break
    if not cand:
        return None, "header.json 里找不到玩家结算表"
    for row in cand:
        if not isinstance(row, dict):
            continue
        if _name_match(row.get("name"), player_name):
            k = row.get("kills")
            return (int(k) if isinstance(k, (int, float)) else None,
                    "header.json userId=%s kills=%s humanKills=%s groundKills=%s"
                    % (row.get("userId"), row.get("kills"), row.get("humanKills"),
                       row.get("groundKills")))
    return None, "header.json 玩家表里没有 %r" % player_name


def check_events(cfg, md, raw, mapping, results):
    meta = md.get("meta") or {}
    win = meta.get("window") or {}
    evs = md.get("events") or []
    source = meta.get("source")
    if not isinstance(win, dict) or win.get("t0") is None or win.get("t1") is None:
        add(results, "2a.events-vs-kills", "事件数与回放击杀包一致", FAIL,
            "meta.window 缺 t0/t1，无法界定窗口")
        return
    M, axis = mission_axis(md, mapping)
    axis_ok = M is not None
    t0 = float(win["t0"])
    t1 = float(win["t1"])
    hud = raw.get("hudmsg") or []
    if axis_ok:
        inside = [h for h in hud if t0 + M - 0.6 <= h["rel"] <= t1 + M + 0.6]
    else:
        inside = []
    kinds = {}
    for h in inside:
        k = classify_msg(h["msg"])
        kinds[k] = kinds.get(k, 0) + 1
    pname = ((meta.get("player") or {}).get("name")) or ""
    pk = [h for h in inside if classify_msg(h["msg"]) == "kill"
          and pname and clean_msg(pname) in actor_text(h["msg"])]
    base_ev = [("时间轴：%s，mission t + %.2f s = 抓取 rel" % (axis, M)) if axis_ok else
               "时间轴：缺 tracks.json，无法把 mission 轴映到抓取 rel（2a 只用回放 tick 轴，不受影响）",
               "声明窗口 [%g, %g] s（mission）%s"
               % (t0, t1, ("→ 抓取 rel [%.1f, %.1f]" % (t0 + M, t1 + M)) if axis_ok else ""),
               "窗口内去重 hudmsg %d 行%s，分类：%s"
               % (len(inside), "" if axis_ok else "（未算，缺时间映射）",
                  json.dumps(kinds, ensure_ascii=False)),
               "玩家（%s）作为击杀者的 hudmsg 原句：%d 条" % (pname, len(pk))]

    ev_kinds = {}
    for e in evs:
        k = e.get("kind")
        ev_kinds[k] = ev_kinds.get(k, 0) + 1
    ev_kill = ev_kinds.get("kill", 0)
    ev_2a = base_ev + ["events kind 分布：%s" % json.dumps(ev_kinds, ensure_ascii=False)]
    # 权威来源（契约 v5 §1）：回放击杀包。entities.json 由解码线独立从包流提取
    ent = load_json_file(cfg.get("entities_path"))
    packets = None
    if isinstance(ent, dict):
        for key in ("killPackets", "kills", "kill_packets"):
            if isinstance(ent.get(key), list) and ent.get(key):
                packets = ent[key]
                break
    indep = None
    if cfg.get("payload_bin") and os.path.exists(cfg["payload_bin"]):
        try:
            indep = count_kill_packets(cfg["payload_bin"])
            ev_2a.append("我用 payload.bin 独立分帧重数：%d 个击杀包（总记录 %d）"
                         % (len(indep["kill_ticks"]), indep["records"]))
        except Exception as exc:
            ev_2a.append("payload.bin 独立分帧失败：%s" % exc)
    claimed = (meta.get("replayTicks") or {}).get("killPackets")
    if claimed is not None:
        ev_2a.append("作者 meta.replayTicks.killPackets=%s" % claimed)
    if isinstance(packets, list) and packets:
        expect = len(packets)
        secs = sorted(float(p["sec"]) for p in packets if isinstance(p.get("sec"), (int, float)))
        ev_2a.append("entities.json killPackets：%d 个，sec %.3f..%.3f；样本 %s"
                     % (expect, secs[0] if secs else -1, secs[-1] if secs else -1,
                        json.dumps(packets[0], ensure_ascii=False)[:200]))
        if indep is not None and len(indep["kill_ticks"]) != expect:
            ev_2a.append("注意：我独立重数 %d 与 entities.json 的 %d 不一致，需 wrpl-format 复核"
                         % (len(indep["kill_ticks"]), expect))
    elif indep is not None:
        expect = len(indep["kill_ticks"])
        ev_2a.append("entities.json 缺失，退回用 payload.bin 独立重数")
    elif claimed is not None:
        expect = int(claimed)
        ev_2a.append("entities.json 与 payload.bin 都不可用，退回作者自述 killPackets")
    else:
        expect = None
    # 逐条对齐 tick / sec / vehicle
    unmatched = []
    no_tick = 0
    misspackets = []
    pairs = []
    ratio_list = []
    tick_exact = 0
    t0_win = float(win.get("t0") or 0.0)
    rt_meta = meta.get("replayTicks") or {}
    origin, axis_note, axis_bad_n = derive_axis_origin(md)
    ev_2a.append("mission 轴原点：raw 秒 = mission_t + %.3f s；%s" % (origin, axis_note))
    if isinstance(packets, list) and packets:
        used = set()
        for e in evs:
            if e.get("kind") != "kill":
                continue
            rt = e.get("replayTick")
            if not isinstance(rt, (int, float)):
                no_tick += 1
            et = e.get("t")
            best = None
            for i, p in enumerate(packets):
                if i in used:
                    continue
                sc = None
                if isinstance(et, (int, float)) and isinstance(p.get("sec"), (int, float)):
                    sc = abs(float(et) + origin - float(p["sec"]))
                elif isinstance(rt, (int, float)) and isinstance(p.get("tick"), (int, float)):
                    # 包没有 sec 时的兜底：只有 tick 精确相等才算候选，不做单位折算
                    sc = 0.001 if abs(float(p["tick"]) - float(rt)) <= 2 else None
                if sc is None:
                    continue
                if best is None or sc < best[0]:
                    best = (sc, i)
            if best is not None and best[0] <= 0.05:
                used.add(best[1])
                p = packets[best[1]]
                pairs.append((e, p))
                if isinstance(rt, (int, float)) and rt and isinstance(p.get("tick"), (int, float)):
                    ratio_list.append(float(p["tick"]) / float(rt))
                if isinstance(rt, (int, float)) and isinstance(p.get("tick"), (int, float)) \
                        and int(rt) == int(p["tick"]):
                    tick_exact += 1
                ev_2a.append("  event t=%.3f tick=%s ↔ packet sec=%.3f tick=%s（车种 %s）"
                             % (float(et or 0), rt, float(p.get("sec") or -1), p.get("tick"),
                                p.get("vehicle")))
            else:
                unmatched.append({"eventTick": rt, "eventT": et, "text": str(e.get("text"))[:60]})
        misspackets = [{"tick": p.get("tick"), "sec": p.get("sec"), "vehicle": p.get("vehicle")}
                       for i, p in enumerate(packets) if i not in used]
        ev_2a.append("逐条对齐（按 sec；tick 精确相等单独统计）：events(kind=kill) %d 条 → 命中 %d 个包；"
                     "对不上 %d 条；无 replayTick %d 条；回放侧未被引用 %d 个；tick 精确相等 %d/%d"
                     % (ev_kill, len(pairs), len(unmatched), no_tick, len(misspackets),
                        tick_exact, len(pairs)))
        rs = [round(r, 6) for r in ratio_list if r]
        if rs and len(set(rs)) == 1 and rs[0] != 1.0:
            ev_2a.append("跨产物 tick 时基不一致：entities.json tick / events.replayTick = %g（全一致）"
                         "——本次按 sec 对齐判定，单位统一问题见 2f" % rs[0])
        if unmatched:
            ev_2a.append("对不上的 events：%s" % json.dumps(unmatched[:6], ensure_ascii=False))
        if misspackets:
            ev_2a.append("未被任何 event 引用的击杀包：%s" % json.dumps(misspackets[:6], ensure_ascii=False))
    axis_bad = []
    if axis_bad_n:
        try:
            spt_axis = float(rt_meta.get("secPerTick"))
            for e in evs:
                rt = e.get("replayTick")
                if isinstance(rt, (int, float)) and isinstance(e.get("t"), (int, float)):
                    want = float(rt) * spt_axis - origin
                    if abs(float(e["t"]) - want) > 0.02:
                        axis_bad.append({"kind": e.get("kind"), "t": e.get("t"),
                                         "want": round(want, 3), "replayTick": rt})
            ev_2a.append("轴自洽：按判定出的 origin=%.3f，仍有 %d 条 event.t 不吻合（前 5：%s）"
                         % (origin, len(axis_bad), json.dumps(axis_bad[:5], ensure_ascii=False)))
        except Exception as exc:
            ev_2a.append("轴自洽核对异常：%s" % exc)
    if expect is None:
        add(results, "2a.events-vs-kills", "事件数与回放击杀包一致", WARN,
            "无法独立得到回放击杀包数", ev_2a)
    elif axis_bad:
        add(results, "2a.events-vs-kills", "事件数与回放击杀包一致", FAIL,
            "有 %d 条事件的 t 与 (replayTick-tick0)×secPerTick 不符（时间轴不一致）" % len(axis_bad),
            ev_2a)
    elif ev_kill != expect:
        add(results, "2a.events-vs-kills", "事件数与回放击杀包一致", FAIL,
            "events 中 kind=kill 共 %d 个，回放击杀包 %d 个（差 %+d）"
            % (ev_kill, expect, ev_kill - expect), ev_2a)
    elif (unmatched or misspackets):
        add(results, "2a.events-vs-kills", "事件数与回放击杀包一致", WARN,
            "条数一致（%d），但逐条对齐有出入：对不上 %d 条 / 未被引用 %d 个"
            % (expect, len(unmatched), len(misspackets)), ev_2a)
    else:
        add(results, "2a.events-vs-kills", "事件数与回放击杀包一致", PASS,
            "events(kind=kill) %d 条与 entities.json killPackets 按 sec 逐条一一对上（tick 精确相等 %d/%d）"
            % (ev_kill, tick_exact, len(pairs)), ev_2a)

    # 2a2：两条独立解码路径交叉核对（位流 kill_events.json ↔ 前缀扫描 entities.json）
    ke = load_json_file(cfg.get("kill_events_path"))
    ke_kills = ke.get("kills") if isinstance(ke, dict) else None
    if not isinstance(ke_kills, list) or not ke_kills:
        add(results, "2a2.kill-events", "位流解码与前缀扫描两条路径交叉核对", SKIP,
            "kill_events.json 不存在或没有 kills 数组")
    else:
        pk_by_tick = {}
        for p in (packets or []):
            if isinstance(p, dict) and isinstance(p.get("tick"), (int, float)):
                pk_by_tick[int(p["tick"])] = p
        diffs = []
        matched = 0
        for k in ke_kills:
            if not isinstance(k, dict):
                continue
            tk = k.get("tick")
            f = k.get("fields") or {}
            veh_ke = f.get("PlayerVehicle") or None
            p = pk_by_tick.get(int(tk)) if isinstance(tk, (int, float)) else None
            if p is None:
                diffs.append({"tick": tk, "why": "entities.json 里没有同 tick 的击杀包"})
                continue
            veh_ent = p.get("vehicle") or None
            if veh_ke != veh_ent:
                diffs.append({"tick": tk, "kill_events": veh_ke, "entities": veh_ent})
            else:
                matched += 1
        extra = [t for t in pk_by_tick if t not in {int(k["tick"]) for k in ke_kills
                                                    if isinstance(k, dict)
                                                    and isinstance(k.get("tick"), (int, float))}]
        # 本机 in-game uid → 独立复核击杀归属
        tr = load_json_file(cfg.get("tracks_path")) or {}
        player_uid = eid_key(tr.get("ownEntityId"))
        if not player_uid:
            for e in evs:
                if e.get("isPlayerActor") and e.get("entityId"):
                    player_uid = eid_key(e.get("entityId"))
                    break
        ke_pk = []
        if player_uid:
            for k in ke_kills:
                if not isinstance(k, dict):
                    continue
                f = k.get("fields") or {}
                if eid_key(f.get("KillerUid")) == player_uid:
                    ke_pk.append(k)
        md_pk = [e for e in evs if e.get("isPlayerActor") and e.get("kind") == "kill"]
        ke_pk_ticks = sorted(int(k["tick"]) for k in ke_pk if isinstance(k.get("tick"), (int, float)))
        md_pk_ticks = sorted(int(e["replayTick"]) for e in md_pk
                             if isinstance(e.get("replayTick"), (int, float)))
        sc = ke.get("selfCheck") or {}
        ev2a2 = ["作者 selfCheck：%s" % json.dumps(sc, ensure_ascii=False)[:300],
                 "按 tick+载具逐条核对 kill_events ↔ entities.killPackets：一致 %d/%d 条，差异 %d 条，"
                 "entities 侧多出 %d 条" % (matched, len(ke_kills), len(diffs), len(extra)),
                 "本机 in-game uid=%s；kill_events 里 KillerUid==本机 的击杀 %d 条（tick %s）；"
                 "mission-data 里 isPlayerActor 的击杀 %d 条（tick %s）"
                 % (player_uid or "未知", len(ke_pk_ticks), ke_pk_ticks, len(md_pk_ticks), md_pk_ticks),
                 "kill_events 头部字段：%s" % json.dumps({k: (ke_kills[0].get(k) if not isinstance(ke_kills[0].get(k), dict)
                                                              else list(ke_kills[0][k].keys()))
                                                          for k in list(ke_kills[0].keys())[:8]},
                                                         ensure_ascii=False)[:300]]
        if diffs:
            ev2a2.append("差异明细（前 6）：%s" % json.dumps(diffs[:6], ensure_ascii=False))
        if diffs or extra:
            add(results, "2a2.kill-events", "位流解码与前缀扫描两条路径交叉核对", FAIL,
                "两条路径对不上：一致 %d/%d，差异 %d 条，entities 多出 %d 条"
                % (matched, len(ke_kills), len(diffs), len(extra)), ev2a2)
        elif player_uid and ke_pk_ticks != md_pk_ticks:
            add(results, "2a2.kill-events", "位流解码与前缀扫描两条路径交叉核对", FAIL,
                "本机击杀归属对不上：kill_events(KillerUid=%s) 给 %s，mission-data 给 %s"
                % (player_uid, ke_pk_ticks, md_pk_ticks), ev2a2)
        else:
            add(results, "2a2.kill-events", "位流解码与前缀扫描两条路径交叉核对", PASS,
                "两条独立路径 %d/%d 条逐条一致；本机归属 %d 条也一致（KillerUid=%s）"
                % (matched, len(ke_kills), len(md_pk_ticks), player_uid or "未知"), ev2a2)

    # 2f：跨产物 tick 时基（单位）一致
    # 定案口径（wrpl-format §3.1）：tick = raw 256 kHz 计数器 = tsMs × 256；
    # 文件字段值叫 tsMs，raw 与 tsMs 精确互换（整除）。
    hdr2 = load_json_file(cfg.get("header_path")) or {}
    clock2 = hdr2.get("clock") or {}
    rt2 = meta.get("replayTicks") or {}
    ev2f = []
    fails2f = []
    warns2f = []
    RAW_PER_MS = 256.0
    spt = tl = None
    try:
        spt = float(rt2.get("secPerTick"))
        tl = float(rt2.get("tickLast"))
        ev2f.append("mission-data.replayTicks：secPerTick=%g（%g ticks/s）tickLast=%g → %.3f s"
                    % (spt, 1.0 / spt, tl, tl * spt))
    except Exception:
        warns2f.append("mission-data.replayTicks 缺 secPerTick/tickLast")
    if clock2:
        ev2f.append("header.clock：rawTicksPerSecond=%s secondsPerRawTick=%s rawTicksPerMs=%s"
                    % (clock2.get("rawTicksPerSecond"), clock2.get("secondsPerRawTick"),
                       clock2.get("rawTicksPerMs")))
        conv = clock2.get("unitConvention") or ""
        ev2f.append("header.clock 单位约定：%s" % (conv or clock2.get("formula") or "（无）"))
        if not conv:
            warns2f.append("header.clock 缺 unitConvention（单位约定未成文）")
        e_raw, e_ms = clock2.get("endMarkerRaw"), clock2.get("endMarkerMs")
        if isinstance(e_raw, (int, float)) and isinstance(e_ms, (int, float)):
            if abs(e_raw / RAW_PER_MS - e_ms) > 1.0:
                fails2f.append("header.clock endMarkerRaw/256=%.1f 与 endMarkerMs=%s 不符"
                               % (e_raw / RAW_PER_MS, e_ms))
            else:
                ev2f.append("header.clock endMarkerRaw/256 == endMarkerMs（%s / %s）" % (e_raw, e_ms))
        if isinstance(e_raw, (int, float)) and tl is not None and spt:
            dt_last = abs(e_raw - tl) * spt
            ev2f.append("tickLast 与 endMarkerRaw 差 %s raw ticks = %.3f s"
                        "（EndMarker 是最后一个包，末条航迹早于它属正常）" % (abs(e_raw - tl), dt_last))
            if dt_last > 1.0:
                fails2f.append("tickLast 与 endMarkerRaw 折算相差 %.3f s（>1 s）" % dt_last)
        c_spt = clock2.get("secondsPerRawTick")
        if isinstance(c_spt, (int, float)) and spt is not None and abs(c_spt - spt) > 1e-12:
            fails2f.append("secondsPerRawTick=%g 与 mission-data.secPerTick=%g 不一致" % (c_spt, spt))
    else:
        warns2f.append("header.clock 不可用，无法交叉核对")
    if isinstance(packets, list) and packets:
        n_raw = sum(1 for p in packets if isinstance(p, dict) and ("tick" in p or "tickRaw" in p))
        n_ms = sum(1 for p in packets if isinstance(p, dict) and "tsMs" in p)
        ev2f.append("entities.killPackets：带 raw tick 字段 %d/%d，带 tsMs %d/%d"
                    % (n_raw, len(packets), n_ms, len(packets)))
        bad = 0
        samples = []
        raw_eq_events = 0
        for p in packets:
            if not isinstance(p, dict):
                continue
            raw = p.get("tickRaw", p.get("tick"))
            ms = p.get("tsMs")
            if isinstance(raw, (int, float)) and isinstance(ms, (int, float)):
                if abs(raw / RAW_PER_MS - ms) > 1.0:
                    bad += 1
                    if len(samples) < 3:
                        samples.append({"raw": raw, "tsMs": ms, "raw/256": raw / RAW_PER_MS})
        if n_raw and n_ms:
            if bad:
                fails2f.append("有 %d 条 raw/256 与 tsMs 不符（示例 %s）"
                               % (bad, json.dumps(samples, ensure_ascii=False)))
            else:
                ev2f.append("raw/256 == tsMs 逐条相符（%d/%d）" % (len(packets), len(packets)))
        elif n_ms and not n_raw:
            warns2f.append("只有 tsMs、没有 raw tick 字段，合并时无法直接使用")
        else:
            warns2f.append("killPackets 缺 tsMs 对照，单位仍不明")
        if pairs:
            ev2f.append("与 events.replayTick 精确相等的击杀包 raw tick：%d/%d" % (tick_exact, len(pairs)))
            if tick_exact == 0:
                fails2f.append("events.replayTick 与 killPackets 的 raw tick 完全对不上（0/%d）" % len(pairs))
            elif tick_exact < len(pairs):
                warns2f.append("有 %d/%d 条 tick 不精确相等" % (len(pairs) - tick_exact, len(pairs)))
    else:
        ev2f.append("没有可用的 killPackets 做单位核对")
    if fails2f:
        add(results, "2f.tick-unit", "跨产物 tick 时基（单位）一致", FAIL, "；".join(fails2f[:4]), ev2f)
    elif warns2f:
        add(results, "2f.tick-unit", "跨产物 tick 时基（单位）一致", WARN, "；".join(warns2f[:4]), ev2f)
    else:
        add(results, "2f.tick-unit", "跨产物 tick 时基（单位）一致", PASS,
            "tick=raw 256 kHz、tsMs=毫秒、两者精确互换；与 mission-data.replayTick 同空间（逐条相符）", ev2f)

    awards = None
    if isinstance(ent, dict):
        for key in ("awards", "awardList"):
            if isinstance(ent.get(key), list) and ent.get(key):
                awards = ent[key]
                break
    if isinstance(awards, list) and awards:
        total = sum(int(a["count"]) for a in awards
                    if isinstance(a, dict) and isinstance(a.get("count"), (int, float)))
        n_given_ticks = sum(len(a.get("firstTicksRaw") or a.get("firstTicks") or [])
                            for a in awards if isinstance(a, dict))
        first0 = {}
        for a in awards:
            if isinstance(a, dict) and (a.get("firstTicksRaw") or a.get("firstTicks")):
                try:
                    first0[str(a.get("award"))] = int((a.get("firstTicksRaw") or a.get("firstTicks"))[0])
                except Exception:
                    pass
        def _awname(e):
            return str(e.get("text") or "").split("（")[0].split("(")[0].strip()
        ev_aw = [e for e in evs if e.get("kind") in ("award", "lastblow")]
        names_ev = [_awname(e) for e in ev_aw]
        names_tb = [str(a.get("award")) for a in awards if isinstance(a, dict)]
        missing = [n for n in names_tb if n not in names_ev]
        extra = [n for n in names_ev if n not in names_tb]
        first_sec = {}
        for a in awards:
            if isinstance(a, dict):
                try:
                    first_sec[str(a.get("award"))] = float(a.get("firstSec"))
                except Exception:
                    pass
        tick_ok = tick_bad = sec_ok = 0
        for e in ev_aw:
            nm = _awname(e)
            want = first0.get(nm)
            wsec = first_sec.get(nm)
            rt = e.get("replayTick")
            et = e.get("t")
            if want is not None and isinstance(rt, (int, float)) and int(rt) == want:
                tick_ok += 1
            else:
                tick_bad += 1
            if wsec is not None and isinstance(et, (int, float)) \
                    and abs(float(et) + origin - wsec) <= 0.05:
                sec_ok += 1
        ev2e = ["entities.json awards：%d 类，实例合计 %d（含 hidden_* 内部奖），给出的逐实例 tick 合计 %d，"
                "去重后 %d 个不同 tick" % (len(awards), total, n_given_ticks, len(set(first0.values()))),
                "对账式（data-eng §1.3 口径）：events(award)+events(lastblow) = len(awards[]) → %d vs %d"
                % (len(ev_aw), len(awards)),
                "按类名逐个对齐：缺失 %d 个 %s；events 多出 %d 个 %s；event.t == 该类 firstSec 的 %d/%d 条；"
                "replayTick == 该类 firstTicks[0] 的 %d 条（tick 编号单位不同，见 2f）"
                % (len(missing), missing[:6], len(extra), extra[:6], sec_ok, len(ev_aw), tick_ok),
                "说明：sum(count)=%d 是全场实例数（16 类 hidden_* 占 81 个），firstTicks 只给了被截断的采样，"
                "逐实例还原在数据上不可能；故不以 sum(count) 作对账依据" % total,
                "开发期旁证：8111 hudmsg 窗口内奖项播报只有 15 条（HUD 真正播出的子集），与回放 27 类本就不等"]
        if not ev_aw:
            add(results, "2e.awards", "events(award/lastblow) 与回放 awards 按类对齐", INFO,
                "events 里没有 award/lastblow（回放 awards 有 %d 类）；若契约要求导出奖项需补" % len(awards), ev2e)
        elif len(ev_aw) != len(awards) or missing or extra:
            add(results, "2e.awards", "events(award/lastblow) 与回放 awards 按类对齐", WARN,
                "events(award+lastblow) %d 条 vs awards %d 类；类名缺失 %d，多出 %d"
                % (len(ev_aw), len(awards), len(missing), len(extra)), ev2e)
        elif sec_ok != len(ev_aw):
            add(results, "2e.awards", "events(award/lastblow) 与回放 awards 按类对齐", WARN,
                "%d 类都在，但只有 %d/%d 条的 t 等于该类 firstSec（按时间对不齐）"
                % (len(awards), sec_ok, len(ev_aw)), ev2e)
        else:
            add(results, "2e.awards", "events(award/lastblow) 与回放 awards 按类对齐", PASS,
                "%d 类逐一对上，event.t 全部等于各类首个实例时刻（口径：不拿 sum(count)=%d 对账；"
                "tick 编号单位差异见 2f）" % (len(awards), total), ev2e)
    else:
        add(results, "2e.awards", "events(award/lastblow) 与回放 awards 按类对齐", SKIP,
            "entities.json 没有 awards 数组")

    if not axis_ok:
        add(results, "2b.events-vs-hudmsg", "事件数 vs 窗口内去重 hudmsg 行数", SKIP,
            "缺 tracks.json，无法把 mission 轴映到抓取 rel（开发期对照项，不影响交付判定）")
    elif len(evs) == len(inside):
        add(results, "2b.events-vs-hudmsg", "事件数 vs 窗口内去重 hudmsg 行数", PASS,
            "events %d vs %d" % (len(evs), len(inside)), base_ev)
    else:
        add(results, "2b.events-vs-hudmsg", "事件数 vs 窗口内去重 hudmsg 行数", WARN,
            "events %d vs hudmsg %d（开发期对照：契约 v5 §1 规定权威时间轴与事件数来自回放，"
            "8111 抓取窗口与回放窗口本就不重合，这里不作为期望值）"
            % (len(evs), len(inside)), base_ev)

    hk, hnote = header_player_kills(cfg, pname)
    counts = meta.get("counts") or {}
    md_own = counts.get("ownKills")
    md_res = counts.get("ownKillsFromResults")
    md_ev = counts.get("ownKillsByEvents", md_own)
    pkills = (meta.get("player") or {}).get("kills")
    mr = None
    mres = meta.get("matchResults") or {}
    for row in (mres.get("players") or []):
        if _name_match((row or {}).get("name"), pname):
            mr = (row or {}).get("kills")
    ev_own = sum(1 for e in evs if e.get("isPlayerActor") and e.get("kind") == "kill")
    truth = len(pk)
    truth_txt = ("8111 开发期对照：窗口内玩家击杀原文 %d 条" % truth) if axis_ok else \
        "8111 开发期对照：缺时间映射，未取"
    ev2c = base_ev + ["结算表：%s" % hnote,
                      "meta：player.kills=%s counts.ownKills=%s ownKillsFromResults=%s ownKillsByEvents=%s"
                      % (pkills, md_own, md_res, md_ev),
                      "events 中 isPlayerActor 且 kind=kill 的 %d 条（契约 §2.6：回放击杀包不含击杀者身份，"
                      "应为 0）；8111 hudmsg 窗口内玩家击杀原文 %d 条（开发期对照）" % (ev_own, truth)]
    settle = {k: v for k, v in (("header.players[本机]", hk), ("matchResults[本机]", mr),
                                ("counts.ownKillsFromResults", md_res),
                                ("meta.player.kills", pkills)) if isinstance(v, (int, float))}
    attrib = {k: v for k, v in (("events(isPlayerActor&&kill)", ev_own),
                                ("counts.ownKillsByEvents", md_ev)) if isinstance(v, (int, float))}
    problems = []
    if settle and len({int(v) for v in settle.values()}) > 1:
        problems.append("结算来源之间不一致：%s" % json.dumps(settle, ensure_ascii=False))
    if attrib and len({int(v) for v in attrib.values()}) > 1:
        problems.append("事件归属口径之间不一致：%s" % json.dumps(attrib, ensure_ascii=False))
    s_val = max(int(v) for v in settle.values()) if settle else None
    a_val = max(int(v) for v in attrib.values()) if attrib else None
    if s_val is not None and a_val is not None and a_val > s_val:
        problems.append("事件归属 %d 多于结算 %d（凭空归属）" % (a_val, s_val))
    ev2c.append("结算来源：%s；事件归属来源：%s；8111 开发期对照：窗口内 %d 条"
                % (json.dumps(settle, ensure_ascii=False), json.dumps(attrib, ensure_ascii=False), truth))
    if problems:
        add(results, "2c.player-kills", "玩家击杀口径（结算 vs 事件归属）", FAIL,
            "；".join(problems), ev2c)
    elif not settle and not attrib:
        add(results, "2c.player-kills", "玩家击杀口径（结算 vs 事件归属）", WARN,
            "找不到任何 kills 数字（结算与事件归属都没有）", ev2c)
    elif s_val is not None and a_val == 0:
        add(results, "2c.player-kills", "玩家击杀口径（结算 vs 事件归属）", INFO,
            "该局没拿到位流击杀者归属字段（ownKillsByEvents=0），只与结算核对：kills=%d" % s_val, ev2c)
    elif s_val is not None and a_val == s_val:
        add(results, "2c.player-kills", "玩家击杀口径（结算 vs 事件归属）", PASS,
            "四源一致：事件归属 %d = 结算 %d；%s" % (a_val, s_val, truth_txt), ev2c)
    elif s_val is None and a_val is not None:
        add(results, "2c.player-kills", "玩家击杀口径（结算 vs 事件归属）", WARN,
            "只有事件归属 %d、没有结算数字可对" % a_val, ev2c)
    else:
        add(results, "2c.player-kills", "玩家击杀口径（结算 vs 事件归属）", WARN,
            "事件归属 %s 与结算 %s 不等（需人确认口径）"
            % (a_val, s_val), ev2c)


# ---------------------------------------------------------------- 检查 3：结构

OWN_KEYS_V4 = ["t0", "hz", "n", "x", "y", "z", "ias", "mach", "hdg", "roll",
               "pitch", "g", "aoa", "ammo", "fuel"]
UNIT_KEYS_V4 = ["key", "side", "domain", "vehicle", "player", "hasAlt", "t0", "hz",
                "n", "x", "y", "z", "hdg"]
EVENT_KEYS_V4 = ["t", "kind", "text", "composed", "actor", "actorVehicle", "target",
                 "targetVehicle", "entityId", "victimEntityId", "isPlayerActor",
                 "isPlayerTarget", "replayTick", "x", "y", "z"]
META_KEYS_V4 = ["source", "replayFile", "replayHeader", "player", "matchResults",
                "replayTicks", "window", "grid", "counts", "devValidation",
                "provenance", "limits"]


def check_window(cfg, md, results):
    """3b：按回放时钟独立复核 window 覆盖率与 replayTicks 一致性。"""
    meta = md.get("meta") or {}
    win = meta.get("window") or {}
    rt = meta.get("replayTicks") or {}
    origin3b, axis_note3b, _bad3b = derive_axis_origin(md)
    hdr = load_json_file(cfg.get("header_path")) or {}
    clock = hdr.get("clock") or {}
    own = md.get("own") or {}
    units = md.get("units") or []
    ev = []
    problems = []
    try:
        t0 = float(win.get("t0", 0.0))
        t1 = float(win.get("t1", 0.0))
    except Exception:
        add(results, "3b.window", "窗口/回放时钟一致性（独立复算）", FAIL, "window.t0/t1 不是数字")
        return
    if t1 <= t0:
        problems.append("window 不合法：t0=%s t1=%s" % (t0, t1))
    dur = t1 - t0
    ev.append("声明窗口 [%.3f, %.3f] s（mission 轴），时长 %.3f s；轴原点 = %.3f s（%s），"
              "折算 raw 秒 [%.3f, %.3f]" % (t0, t1, dur, origin3b, axis_note3b,
                                            t0 + origin3b, t1 + origin3b))
    try:
        n = int(own.get("n") or 0)
        hz = float(own.get("hz") or 0)
        ot0 = float(own.get("t0") or 0)
        if n > 1 and hz > 0:
            cov = max(0.0, min(t1, ot0 + (n - 1) / hz) - max(t0, ot0)) / max(1e-9, dur)
            ev.append("own 覆盖窗口 %.1f%%（own.t0=%.3f hz=%g n=%d）" % (cov * 100, ot0, hz, n))
            if cov < 0.95:
                problems.append("own 只覆盖窗口 %.1f%%" % (cov * 100))
    except Exception:
        problems.append("own 采样参数不可用")
    try:
        spans = []
        for u in units:
            if not isinstance(u, dict):
                continue
            un = int(u.get("n") or 0)
            uhz = float(u.get("hz") or 0)
            ut0 = float(u.get("t0") or 0)
            if un > 1 and uhz > 0:
                spans.append((ut0, ut0 + (un - 1) / uhz))
        if spans:
            cover = np.zeros(int(max(1, round(dur * 2))) + 1, dtype=bool)
            grid = t0 + np.arange(cover.size) / 2.0
            for a, b in spans:
                cover |= (grid >= a) & (grid <= b)
            frac = float(cover.mean())
            ev.append("units 并集覆盖窗口 %.1f%%（%d 条航迹）" % (frac * 100, len(spans)))
            if frac < 0.8:
                problems.append("units 并集只覆盖窗口 %.1f%%" % (frac * 100))
    except Exception as exc:
        ev.append("units 覆盖计算异常：%s" % exc)
    if clock:
        _actual = {k: clock[k] for k in sorted(clock.keys()) if k != "evidence"}
        ev.append("header.clock 实际键（%d 个）：%s"
                  % (len(clock), json.dumps(_actual, ensure_ascii=False)[:400]))
        _dep = clock.get("deprecatedKeyNames")
        if isinstance(_dep, dict) and _dep:
            _rows = ["%s → %s → %s" % (o, n, json.dumps(clock.get(n), ensure_ascii=False)[:36])
                     for o, n in list(_dep.items())[:10]]
            ev.append("旧名 → 新名 → 实际值（来自 clock.deprecatedKeyNames 机读映射，%d 条）：%s"
                      % (len(_dep), "; ".join(_rows)[:420]))
        else:
            ev.append("header.clock 未提供 deprecatedKeyNames；旧键名不再逐个 lookup（避免把 null 误读成残留）")
        spt = rt.get("secPerTick")
        t_last = rt.get("tickLast")
        c_spt = clock.get("secondsPerTick")
        c_last = clock.get("endMarkerTick")
        e_sec_marker = clock.get("endMarkerSec")
        if isinstance(spt, (int, float)) and isinstance(t_last, (int, float)):
            t_mine = t_last * spt
            ev.append("mission-data 时间轴：tickLast %.0f × secPerTick %g = %.3f s（EndMarker 是最后一个包，"
                      "末条航迹早 0.107 s 属正常）" % (t_last, spt, t_mine))
            if isinstance(e_sec_marker, (int, float)) and abs(t_mine - e_sec_marker) > 1.0:
                problems.append("mission-data tickLast×secPerTick=%.3f 与 EndMarkerSec=%.3f 差超过 1 s"
                                % (t_mine, e_sec_marker))
        if isinstance(c_spt, (int, float)) and isinstance(c_last, (int, float)):
            ev.append("header.clock 时间轴：endMarkerTick %.0f × secondsPerTick %g = %.3f s"
                      % (c_last, c_spt, c_last * c_spt))
        if isinstance(spt, (int, float)) and isinstance(c_spt, (int, float)) and spt != c_spt:
            ev.append("注意：两套 tick 编号单位不同（secPerTick %g vs %g，比值 %.4f）——"
                      "不在此处判问题，见 2f.tick-unit" % (spt, c_spt, c_spt / spt))
        e_sec = clock.get("endMarkerSec")
        if isinstance(e_sec, (int, float)):
            t1_raw = t1 + origin3b
            ev.append("window.t1=%.3f（mission）+ 原点 %.3f = %.3f raw s vs EndMarkerSec=%.3f（差 %.3f s）"
                      % (t1, origin3b, t1_raw, e_sec, t1_raw - e_sec))
            if abs(t1_raw - e_sec) > 1.0:
                problems.append("window.t1 折算后与回放结束时刻差 %.2f s" % (t1_raw - e_sec))
    else:
        ev.append("header.clock 不可用，只做窗口内自检")
    ent = load_json_file(cfg.get("entities_path"))
    if isinstance(ent, dict) and isinstance(ent.get("killPackets"), list) and ent["killPackets"]:
        secs = [p.get("sec") for p in ent["killPackets"] if isinstance(p.get("sec"), (int, float))]
        if secs:
            out = [s for s in secs if s < t0 + origin3b - 1 or s > t1 + origin3b + 1]
            ev.append("killPackets sec 范围 %.3f..%.3f（raw 轴），窗口 raw 范围 [%.3f, %.3f]，落在窗口外 %d 个"
                      % (min(secs), max(secs), t0 + origin3b, t1 + origin3b, len(out)))
            if out:
                problems.append("有 %d 个击杀包 sec 落在窗口外" % len(out))
    if problems:
        add(results, "3b.window", "窗口/回放时钟一致性（独立复算）", WARN,
            "%d 处需要注意：%s" % (len(problems), problems[:6]), ev)
    else:
        add(results, "3b.window", "窗口/回放时钟一致性（独立复算）", PASS,
            "窗口覆盖与回放时钟自洽", ev)


def fit_replay_to_8111(tracks, raw, mapping):
    """用本机航迹与 8111 平面位置拟合 replay→8111 的 2D 相似变换（允许镜像）。"""
    if not tracks or not raw or not mapping or not mapping.get("ok"):
        return None
    own_u = None
    for u in tracks.get("units") or []:
        eid = u.get("entityId")
        if eid == tracks.get("playerEntityId") or eid == mapping.get("entity"):
            own_u = u
            break
    if own_u is None:
        return None
    a = ent_array(own_u)
    if a is None or a.shape[0] < 10:
        return None
    trep = (a[:, 0] - float(mapping["tick_lo"])) * float(mapping["sec_per_tick"])
    pxy = raw.get("player_xy") or {}
    rr = np.asarray(pxy.get("rel") or [], dtype=float)
    rx = np.asarray(pxy.get("x") or [], dtype=float)
    ry = np.asarray(pxy.get("y") or [], dtype=float)
    q = rr - float(mapping["offset_s"])
    ok = (q >= trep[0]) & (q <= trep[-1]) & np.isfinite(rx) & np.isfinite(ry)
    if int(ok.sum()) < 10:
        return None
    P2 = np.column_stack([np.interp(q[ok], trep, a[:, 1]), np.interp(q[ok], trep, a[:, 2])])
    Q2 = np.column_stack([rx[ok], ry[ok]])
    return procrustes_fit(P2, Q2, allow_reflection=True)


def check_ground_delivered(cfg, md, raw, tracks, mapping, results):
    """7.ground：从交付文件独立复算 184 个地面静态点（不采信透传数字）。"""
    units = [u for u in (md.get("units") or [])
             if isinstance(u, dict) and (u.get("domain") == "ground" or u.get("static") is True)]
    if not units:
        add(results, "7.ground", "地面层独立复算（交付文件 vs 8111）", SKIP, "交付 units 里没有地面静态点")
        return
    meta = md.get("meta") or {}
    dv = meta.get("devValidation") or {}
    pts = []
    bad = 0
    for u in units:
        xs, ys, zs = _colf(u, "x"), _colf(u, "y"), _colf(u, "z")
        if xs is None or ys is None or not np.isfinite(xs[0]) or not np.isfinite(ys[0]):
            bad += 1
            continue
        pts.append([xs[0], ys[0], (zs[0] if zs is not None and np.isfinite(zs[0]) else 0.0)])
    ev = ["交付地面静态点 %d 条（坐标可用的 %d，缺/非有限 %d）；static=%d，n=1 的 %d"
          % (len(units), len(pts), bad,
             sum(1 for u in units if u.get("static") is True),
             sum(1 for u in units if u.get("n") == 1))]
    problems = []
    P = np.asarray(pts, dtype=float) if pts else None
    # (1) 与源产物 ground.json 的合并保真
    g = load_json_file(cfg.get("ground_path"))
    if P is not None and isinstance(g, dict) and isinstance(g.get("units"), list):
        G = np.asarray([[u.get("x"), u.get("y")] for u in g["units"]
                        if isinstance(u, dict) and isinstance(u.get("x"), (int, float))
                        and isinstance(u.get("y"), (int, float))], dtype=float)
        if G.size:
            d = np.sqrt(((P[:, None, :2] - G[None, :, :]) ** 2).sum(-1)).min(1)
            ev.append("与源 ground.json 的最近距离：max %.3f m（中位 %.3f m）→ 合并%s"
                      % (float(d.max()), float(np.median(d)),
                         "无坐标改动" if float(d.max()) < 0.5 else "有位移，需检查"))
            if float(d.max()) >= 0.5:
                problems.append("与 ground.json 有 %.2f m 的坐标差" % float(d.max()))
    # (2) 与 8111 地面接触点独立复算
    author_med = dv.get("groundNearestContactMedianM")
    author_frac = dv.get("groundFracWithin100M")
    author_ctrl = dv.get("controlWrongAxisMedianM")
    sim = fit_replay_to_8111(tracks, raw, mapping)
    if P is None:
        add(results, "7.ground", "地面层独立复算（交付文件 vs 8111）", WARN, "没有可用坐标", ev)
        return
    if sim is None or not raw:
        ev.append("无法拟合 replay→8111 变换或没有 8111 数据，跳过最近邻复算")
        add(results, "7.ground", "地面层独立复算（交付文件 vs 8111）", WARN,
            "无法独立复算最近邻（缺变换/8111）", ev)
        return
    contacts = np.asarray([[c[4], c[5]] for c in (raw.get("contacts") or [])
                           if c[1] == "ground_model"], dtype=float)
    if not contacts.size:
        add(results, "7.ground", "地面层独立复算（交付文件 vs 8111）", WARN, "8111 没有地面接触点", ev)
        return

    def _nn(points2d):
        w = sim["scale"] * (points2d @ sim["R"].T) + sim["t"]
        return np.sqrt(((w[:, None, :] - contacts[None, :, :]) ** 2).sum(-1)).min(1)

    d_ok = _nn(P[:, :2])
    flip = P[:, :2].copy()
    flip[:, 1] = -flip[:, 1]
    d_bad = _nn(flip)
    med, p90 = float(np.median(d_ok)), float(np.percentile(d_ok, 90))
    frac = float((d_ok <= 100.0).mean())
    ctrl = float(np.median(d_bad))
    ev.append("我独立复算（8111 地面接触点 %d 个，开发期）：最近距离中位 **%.2f m**、p90 %.1f m、≤100 m 占 **%.1f%%**；"
              "错轴对照（北向取反）中位 **%.1f m**" % (contacts.shape[0], med, p90, frac * 100, ctrl))
    ev.append("交付文件透传的 devValidation：中位 %s / ≤100 m %s / 错轴对照 %s"
              % (author_med, author_frac, author_ctrl))
    verdicts = []
    def _cmp_num(label, mine, theirs, unit="", tol_abs=2.0, tol_rel=0.5):
        if not isinstance(theirs, (int, float)):
            ev.append("%s：作者未给，我复算 %s%s" % (label, round(float(mine), 3), unit))
            return None
        ratio = float(mine) / float(theirs) if float(theirs) else float("inf")
        ok = float(mine) <= float(theirs) * 1.05 or min(ratio, 1.0 / ratio) >= 0.5
        ev.append("%s：我 %.3f%s vs 作者 %s%s（比值 %.2f）→ %s"
                  % (label, float(mine), unit, theirs, unit, ratio,
                     "一致" if ok else "差得远"))
        return ok
    v = _cmp_num("地面最近邻中位距离", med, author_med, " m")
    if v is not None:
        verdicts.append(v)
    v = _cmp_num("≤100 m 比例", frac, author_frac if isinstance(author_frac, (int, float)) else None)
    if v is not None:
        verdicts.append(v)
    v = _cmp_num("错轴对照中位", ctrl, author_ctrl, " m")
    if v is not None:
        verdicts.append(v)
    if med > 200.0:
        problems.append("独立复算的最近邻中位 %.1f m 偏大" % med)
    if ctrl <= med * 5:
        problems.append("错轴对照（%.1f m）与正轴（%.1f m）差距不足，轴向结论存疑" % (ctrl, med))
    ev.append("被验文件指纹：%s / %s"
              % (file_fingerprint(cfg.get("mission_path") or ""),
                 file_fingerprint(cfg.get("tracks_path") or "")))
    if problems or (verdicts and not all(verdicts)):
        detail = ("我独立复算：中位 %.2f m / ≤100 m %.1f%% / 错轴对照 %.1f m；"
                  "透传：中位 %s / ≤100 m %s"
                  % (med, frac * 100, ctrl, author_med, author_frac))
        if isinstance(author_med, (int, float)) and float(author_med) > 0 \
                and med / float(author_med) > 5:
            detail += ("；中位差 %.0f× 的原因是我的回放→8111 变换由**本机航迹**拟合（本身 rms 2–35 m），"
                       "无法复现 0.03 m 的绝对精度，故**不采信该数字、也不据此判 FAIL**；"
                       "覆盖比例与错轴对照两项可复现" % (med / float(author_med)))
        if problems:
            detail += "；" + "；".join(problems[:2])
        add(results, "7.ground", "地面层独立复算（交付文件 vs 8111）", WARN, detail, ev)
    else:
        add(results, "7.ground", "地面层独立复算（交付文件 vs 8111）", PASS,
            "%d 个地面点：我独立复算中位 %.2f m / ≤100 m %.1f%% / 错轴对照 %.1f m，与透传数字一致"
            % (len(P), med, frac * 100, ctrl), ev)


def check_projectiles(cfg, tracks, results):
    """3e：弹药发射点独立复算（只用回放自身数据，不碰 8111）。"""
    d = load_json_file(cfg.get("projectile_path"))
    if not isinstance(d, dict) or not isinstance(d.get("units"), list) or not d["units"]:
        add(results, "3e.projectile-launches", "弹药发射点独立复算（发射点须贴发射机）", SKIP,
            "文件不存在或无 units：%s" % cfg.get("projectile_path"))
        return
    units = [u for u in d["units"] if isinstance(u, dict)]
    spt = None
    for cand in (d.get("secPerTick"), (tracks or {}).get("secPerTick")):
        if isinstance(cand, (int, float)) and cand:
            spt = float(cand)
            break
    if spt is None:
        add(results, "3e.projectile-launches", "弹药发射点独立复算（发射点须贴发射机）", WARN,
            "拿不到 secPerTick，无法换时间")
        return
    span = (tracks or {}).get("replaySpanSec")
    ev = ["projectile_launches.json：%d 条；%s；坐标=%s；%s"
          % (len(units), d.get("tickUnit"), json.dumps(d.get("coordinateSystem"), ensure_ascii=False)[:80],
             json.dumps(d.get("positionRule"), ensure_ascii=False)[:140]),
          "作者 devValidation：%s" % json.dumps(d.get("devValidation"), ensure_ascii=False)[:300],
          "被验文件指纹：%s" % file_fingerprint(cfg.get("projectile_path") or "")]
    problems = []
    # 模板计数
    tmpl = {}
    for u in units:
        tmpl[u.get("template")] = tmpl.get(u.get("template"), 0) + 1
    by = d.get("byTemplate")
    ev.append("模板计数（我复算）：%s；作者 byTemplate：%s"
              % (json.dumps(tmpl, ensure_ascii=False), json.dumps(by, ensure_ascii=False)))
    if isinstance(by, dict) and {k: int(v) for k, v in tmpl.items()} != {k: int(v) for k, v in by.items()}:
        problems.append("模板计数与 byTemplate 不一致")
    if isinstance(d.get("count"), int) and d["count"] != len(units):
        problems.append("count=%s 与实际 %d 条不符" % (d.get("count"), len(units)))
    # 时间窗
    ticks = [u.get("spawnTickRaw") for u in units if isinstance(u.get("spawnTickRaw"), (int, float))]
    if ticks:
        secs = [t * spt for t in ticks]
        ev.append("spawnTickRaw×secPerTick：%.3f..%.3f s（raw 轴；n=%d）" % (min(secs), max(secs), len(secs)))
        tk0 = (tracks or {}).get("tick0")
        tk1 = (tracks or {}).get("tickLast")
        if isinstance(tk0, (int, float)) and isinstance(tk1, (int, float)):
            ev.append("回放 tick 范围 [%s, %s]（= raw 秒 %.3f..%.3f）"
                      % (tk0, tk1, tk0 * spt, tk1 * spt))
            outn = sum(1 for t in ticks if t < tk0 - 1 / spt or t > tk1 + 1 / spt)
            if outn:
                problems.append("有 %d 个发射点 tick 落在回放 [tick0, tickLast] 之外" % outn)
        elif span is not None:
            ev.append("缺少 tick0/tickLast，退回用跨度 %.1f s 粗判（raw 秒需减去轴原点）" % float(span))
    # quaternion 模长
    badq = 0
    qn = 0
    for u in units:
        q = u.get("quaternion")
        if isinstance(q, list) and len(q) == 4 and all(isinstance(x, (int, float)) for x in q):
            qn += 1
            if abs(sum(float(x) * float(x) for x in q) ** 0.5 - 1.0) > 0.02:
                badq += 1
    if qn:
        ev.append("quaternion 模长检查：%d 条中越界（|q|-1|>0.02）%d 条" % (qn, badq))
        if badq:
            problems.append("有 %d 条 quaternion 模长不在 [0.98, 1.02]" % badq)
    # 与飞机航迹的最近邻（同刻 ±0.6 s 内**全部**候选，不取数组相邻几条）
    arrs = []
    for u in (tracks or {}).get("units") or []:
        a = ent_array(u)
        if a is not None and a.shape[0]:
            arrs.append(a)
    if not arrs:
        add(results, "3e.projectile-launches", "弹药发射点独立复算（发射点须贴发射机）", WARN,
            "tracks.json 不可用，无法做最近邻", ev)
        return
    S = np.vstack(arrs)
    order = np.argsort(S[:, 0])
    S = S[order]
    win = 0.6 / spt
    P = np.asarray([[u.get("x"), u.get("y"), u.get("z")] for u in units
                    if all(isinstance(u.get(k), (int, float)) for k in ("x", "y", "z"))], dtype=float)
    T = np.asarray([u.get("spawnTickRaw") for u in units
                    if all(isinstance(u.get(k), (int, float)) for k in ("x", "y", "z"))], dtype=float)
    if P.shape[0] == 0:
        add(results, "3e.projectile-launches", "弹药发射点独立复算（发射点须贴发射机）", WARN,
            "没有可用坐标", ev)
        return
    lo = np.searchsorted(S[:, 0], T - win, side="left")
    hi = np.searchsorted(S[:, 0], T + win, side="right")

    def _nearest(pts):
        out = np.full(pts.shape[0], np.inf)
        for i in range(pts.shape[0]):
            seg = S[lo[i]:hi[i], 1:4]
            if seg.shape[0] == 0:
                continue
            out[i] = np.sqrt(((seg - pts[i]) ** 2).sum(1)).min()
        return out

    d_ok = _nearest(P)
    d_swap = _nearest(P[:, [0, 2, 1]])
    flip = P.copy()
    flip[:, 1] = -flip[:, 1]
    d_flip = _nearest(flip)
    med = float(np.nanmedian(d_ok))
    p90 = float(np.nanpercentile(d_ok, 90))
    p99 = float(np.nanpercentile(d_ok, 99))
    mx = float(np.nanmax(d_ok))
    frac100 = float(np.mean(d_ok <= 100.0))
    n_gt1000 = int(np.nansum(d_ok > 1000.0))
    # 有效性单独挡：全 0 填充与近原点（量级检查永远挡不住 0）
    zero_fill = int(np.sum((np.abs(P[:, 0]) < 1e-6) & (np.abs(P[:, 1]) < 1e-6) & (np.abs(P[:, 2]) < 1e-6)))
    near_origin = int(np.sum(np.sqrt((P ** 2).sum(1)) < 100.0))
    ev.append("有效性检查：全 0 填充 %d 条、离原点 <100 m 的 %d 条" % (zero_fill, near_origin))
    ev.append("工具注意事项：**量级检查 ≠ 有效性检查** —— 全 0 在量级判据里永远合法，"
              "所以这里单独挡 0/近原点；判据必须先能失败（wrpl-probe 的修前 2 条离群就是 (0,-0,-0) 填充）。")
    ev.append("我独立复算（同刻 ±0.6 s **全部**航迹样本，共 %d 个候选点）："
              "最近飞机中位 **%.2f m**、p90 %.1f m、p99 %.1f m、max **%.1f m**、"
              "≤100 m 占 **%.4f**、>1000 m 的 %d 条"
              % (S.shape[0], med, p90, p99, mx, frac100, n_gt1000))
    ev.append("工具注意事项：最近邻必须取**时间窗内全部候选**；只取时间上相邻的几条会得到 15 km 的假否定"
              "（wrpl-probe 踩过并留痕）。")
    ev.append("对照组：y/z 互换中位 **%.0f m**（≤100 m 占 %.3f）；北向取反中位 **%.0f m**（≤100 m 占 %.3f）"
              % (float(np.nanmedian(d_swap)), float(np.mean(d_swap <= 100.0)),
                 float(np.nanmedian(d_flip)), float(np.mean(d_flip <= 100.0))))
    ev.append("作者自报：中位 %.2f m / p90 %.1f / ≤100 m %.4f"
              % (float(d.get("devValidation", {}).get("medianDistToNearestAircraftM") or -1),
                 float(d.get("devValidation", {}).get("p90DistM") or -1),
                 float(d.get("devValidation", {}).get("within100m") or -1)))
    ev.append("注意：这是**创建时刻的单点**、不是航迹；弹药无 uid 组件 → 不可能有逐帧弹道"
              "（REPLAY-DECODE.md §14.1/§14.3）。")
    if med > 50.0:
        problems.append("最近飞机中位 %.1f m > 50 m" % med)
    if frac100 < 0.99:
        problems.append("≤100 m 占比 %.4f < 0.99" % frac100)
    if mx > 200.0:
        problems.append("max %.1f m > 200 m" % mx)
    if n_gt1000:
        problems.append("有 %d 条最近飞机距离 >1000 m" % n_gt1000)
    if zero_fill:
        problems.append("有 %d 条 (0,0,0) 全零填充位置" % zero_fill)
    if near_origin:
        problems.append("有 %d 条离原点 <100 m（疑似填充/单位错）" % near_origin)
    if not (float(np.nanmedian(d_swap)) > med * 10 and float(np.nanmedian(d_flip)) > med * 10):
        problems.append("对照组未退化到 km 级（轴向结论存疑）")
    if problems:
        add(results, "3e.projectile-launches", "弹药发射点独立复算（发射点须贴发射机）", WARN,
            "；".join(problems[:3]), ev)
    else:
        add(results, "3e.projectile-launches", "弹药发射点独立复算（发射点须贴发射机）", PASS,
            "%d 条发射点：最近飞机中位 %.2f m、≤100 m %.4f；两个对照组 %.0f / %.0f m（米级 vs 公里级对比成立）"
            % (len(units), med, frac100, float(np.nanmedian(d_swap)), float(np.nanmedian(d_flip))), ev)


def check_sides_delivered(cfg, md, raw, tracks, mapping, results):
    """7.sides：交付 units 的 side 消白核对 + 8111 颜色族两种极性一致率。"""
    units = [u for u in (md.get("units") or []) if isinstance(u, dict)]
    if not units:
        add(results, "7.sides", "阵营消白核对（空中 unknown 应为 0）", SKIP, "交付 units 为空")
        return
    air = [u for u in units if u.get("domain") == "air"]
    grd = [u for u in units if u.get("domain") == "ground"]
    cnt_air = {}
    for u in air:
        cnt_air[u.get("side")] = cnt_air.get(u.get("side"), 0) + 1
    cnt_grd = {}
    for u in grd:
        cnt_grd[u.get("side")] = cnt_grd.get(u.get("side"), 0) + 1
    meta = md.get("meta") or {}
    side_src = meta.get("sideSource") or meta.get("side_source")
    sf = load_json_file(cfg.get("side_final_path"))
    ev = ["空中 %d 条 side 分布：%s" % (len(air), json.dumps(cnt_air, ensure_ascii=False)),
          "地面 %d 条 side 分布：%s（静态点可为 unknown，属另一类）"
          % (len(grd), json.dumps(cnt_grd, ensure_ascii=False)),
          "meta 里的 side 来源字段：%s" % (side_src or "（未见 sideSource，改看 provenance/limits）")]
    if isinstance(sf, dict):
        ev.append("side_final.json：%s；被验文件指纹：%s"
                  % (json.dumps({k: v for k, v in sf.items() if k != "sides"}, ensure_ascii=False)[:200],
                     file_fingerprint(cfg.get("side_final_path") or "")))
    problems = []
    n_unk_air = cnt_air.get("unknown", 0) + cnt_air.get(None, 0)
    n_unk_all = sum(1 for u in units if u.get("side") in (None, "unknown"))
    ev.append("全部 %d 条 units 里 side 为空/unknown 的 %d 条（含地面）；"
              "sideInferred=true 的 %d 条"
              % (len(units), n_unk_all,
                 sum(1 for u in units if u.get("sideInferred") is True)))
    if n_unk_air:
        problems.append("空中仍有 %d 个 unknown（消白要求 0）" % n_unk_air)
    if n_unk_all:
        problems.append("仍有 %d 条 units 的 side 为空/unknown（期望 236/236 全覆盖）" % n_unk_all)
    bad_enum = [s for s in list(cnt_air) + list(cnt_grd)
                if s not in ("ally", "enemy", "squad", "unknown", None)]
    if bad_enum:
        problems.append("side 出现契约外取值：%s" % bad_enum)
    # 8111 颜色族两种极性一致率（只用空中单位；地面点不在 tracks 里）
    votes = {}
    sim = fit_replay_to_8111(tracks, raw, mapping) if raw else None
    if sim is not None and raw:
        try:
            origin, _note, _bad = derive_axis_origin(md)
            M = float(mapping["offset_s"]) - float(mapping["tick_lo"]) * float(mapping["sec_per_tick"]) + origin
            groups = {}
            for c in raw.get("contacts") or []:
                groups.setdefault(round(c[0] * 2.0) / 2.0, []).append((c[4], c[5], c[3]))
            arrs = []
            for u in air:
                n_u = int(u.get("n") or 0)
                hz_u = float(u.get("hz") or 0)
                if n_u < 2 or hz_u <= 0:
                    continue
                xs, ys = _colf(u, "x"), _colf(u, "y")
                if xs is None or ys is None:
                    continue
                arrs.append((u, float(u.get("t0") or 0.0) + np.arange(n_u) / hz_u, xs, ys))
            for rel in sorted(groups):
                g = groups[rel]
                if not g:
                    continue
                cs = np.asarray([(p[0], p[1]) for p in g], dtype=float)
                tq = rel - M
                for u, tt, xs, ys in arrs:
                    if tq < tt[0] or tq > tt[-1]:
                        continue
                    m = np.isfinite(xs) & np.isfinite(ys)
                    if int(m.sum()) < 2:
                        continue
                    p = np.array([np.interp(tq, tt[m], xs[m]), np.interp(tq, tt[m], ys[m])])
                    w = sim["scale"] * (sim["R"] @ p) + sim["t"]
                    j = int(np.argmin(np.hypot(cs[:, 0] - w[0], cs[:, 1] - w[1])))
                    fam = color_family(g[j][2])
                    votes.setdefault(u.get("key"), {}).setdefault(fam, 0)
                    votes[u.get("key")][fam] += 1
        except Exception as exc:
            ev.append("颜色族投票异常：%s" % exc)
    pairs = []
    for u in air:
        v = votes.get(u.get("key"))
        s = u.get("side")
        if not v or s in (None, "unknown"):
            continue
        fam = max(v.items(), key=lambda kv: kv[1])[0]
        if fam in ("unknown", "self"):
            continue
        pairs.append((u.get("key"), s, fam, v))
    inv = {"ally": "enemy", "enemy": "ally", "squad": "squad"}
    agree = sum(1 for p in pairs if p[1] == p[2])
    agree_inv = sum(1 for p in pairs if inv.get(p[1]) == p[2])
    n = len(pairs)
    if n:
        ev.append("8111 颜色族交叉核对（仅空中、可核对的 %d 条）：as-is 一致 %d/%d（%.0f%%）、"
                  "取反 %d/%d（%.0f%%）" % (n, agree, n, 100.0 * agree / n, agree_inv, n,
                                            100.0 * agree_inv / n))
        for p in pairs[:6]:
            ev.append("  %s：声明 %s，颜色族 %s（票 %s）" % (p[0], p[1], p[2],
                                                          json.dumps(p[3], ensure_ascii=False)))
        ev.append("说明：8111 颜色族**不足以独立闭合阵营方向**（两种极性历史上都只有 53% 量级）；"
                  "这里给出的是当前交付 side 与该颜色族的**一致率**，方向本身仍需以纯回放推断（side_final）为准。")
    else:
        ev.append("没有可交叉核对的空中单位（缺少颜色票）")
    if problems:
        add(results, "7.sides", "阵营消白核对（空中 unknown 应为 0）", WARN, "；".join(problems[:3]), ev)
    else:
        add(results, "7.sides", "阵营消白核对（空中 unknown 应为 0）", PASS,
            "空中 %d 条全部有侧别（unknown 0）；与 8111 颜色族 as-is 一致率 %s"
            % (len(air), ("%.0f%%（n=%d）" % (100.0 * agree / n, n)) if n else "不可算"), ev)


def _src_tick_ranges(tracks):
    """按 eid_key 归并 tracks 各段的 tick 范围。"""
    out = {}
    for u in (tracks or {}).get("units") or []:
        a = ent_array(u)
        if a is None or not a.shape[0]:
            continue
        out.setdefault(eid_key(u.get("entityId")), []).append((float(a[0, 0]), float(a[-1, 0])))
    return out


def check_death(cfg, md, tracks, results):
    """3g：deathTickRaw 与回放 killPositions 的 victimUid/tick 一一对应。"""
    units = [u for u in (md.get("units") or [])
             if isinstance(u, dict) and isinstance(u.get("deathTickRaw"), (int, float))]
    kp = [k for k in ((tracks or {}).get("killPositions") or []) if isinstance(k, dict)]
    if not units:
        add(results, "3g.death", "deathTickRaw 与 killPositions 一一对应", SKIP,
            "交付 units 里没有 deathTickRaw")
        return
    ev = ["交付带 deathTickRaw 的单位 %d 个；tracks.killPositions %d 条" % (len(units), len(kp))]
    problems = []
    by_uid = {}
    for k in kp:
        if isinstance(k.get("tickRaw"), (int, float)):
            by_uid.setdefault(eid_key(k.get("victimUid")), []).append(float(k["tickRaw"]))
    matched = 0
    bad = []
    for u in units:
        uid = eid_key(u.get("uid") if u.get("uid") is not None else u.get("entityId"))
        dt = float(u["deathTickRaw"])
        cands = by_uid.get(uid) or []
        hit = None
        for tk in cands:
            if abs(tk - dt) <= 2:
                hit = tk
                break
        if hit is None:
            for tk in cands:
                if abs(tk - dt) <= 0.25 / float(((md.get("meta") or {}).get("replayTicks") or {})
                                                .get("secPerTick") or 3.90625e-06):
                    hit = tk
                    break
        if hit is None:
            bad.append({"key": u.get("key"), "uid": uid, "deathTickRaw": dt,
                        "victimTicks": cands[:3]})
        else:
            matched += 1
    unmatched_kp = [k for k in kp if not any(
        abs(float(k.get("tickRaw") or 0) - float(u["deathTickRaw"])) <= 2 for u in units)]
    ev.append("按 victimUid+tick 对齐：命中 %d/%d；对不上 %d 个" % (matched, len(units), len(bad)))
    ev.append("killPositions 侧未被任何 death 引用 %d 条（多为无 FM 航迹的受击者或本机）"
              % len(unmatched_kp))
    if bad:
        ev.append("对不上的示例：%s" % json.dumps(bad[:4], ensure_ascii=False))
    if unmatched_kp:
        ev.append("未引用示例：%s" % json.dumps(
            [{"tickRaw": k.get("tickRaw"), "victimUid": k.get("victimUid")} for k in unmatched_kp[:4]],
            ensure_ascii=False))
    if bad:
        problems.append("%d 个 deathTickRaw 在 killPositions 里找不到同 uid 同 tick 的记录" % len(bad))
    ev.append("被验文件指纹：%s" % file_fingerprint(cfg.get("mission_path") or ""))
    if problems:
        add(results, "3g.death", "deathTickRaw 与 killPositions 一一对应", WARN, "；".join(problems), ev)
    else:
        add(results, "3g.death", "deathTickRaw 与 killPositions 一一对应", PASS,
            "%d 个 deathTickRaw 全部对上 killPositions 的 victimUid+tick" % matched, ev)


def check_units_10hz(cfg, md, tracks, results):
    """7b：空中段全 10 Hz、包线 0 违规、且**上采样没有把 null 断点连起来**。"""
    air = [u for u in (md.get("units") or []) if isinstance(u, dict) and u.get("domain") == "air"]
    if not air:
        add(results, "7b.units-10hz", "空中段 10 Hz 上采样与 null 断点", SKIP, "没有空中单位")
        return
    meta = md.get("meta") or {}
    rt = meta.get("replayTicks") or {}
    spt = float(rt.get("secPerTick") or 3.90625e-06)
    origin, _note, _bad = derive_axis_origin(md)
    ev = ["空中 %d 段，hz 集合 %s，总点数 %d"
          % (len(air), sorted({u.get("hz") for u in air}), sum(int(u.get("n") or 0) for u in air))]
    problems = []
    bad_hz = [u.get("key") for u in air if u.get("hz") != 10]
    if bad_hz:
        problems.append("有 %d 段不是 10 Hz：%s" % (len(bad_hz), bad_hz[:5]))
    arrs = []
    nulls = 0
    for u in air:
        n_u = int(u.get("n") or 0)
        hz_u = float(u.get("hz") or 0)
        if n_u < 2 or hz_u <= 0:
            continue
        xs, ys, zs = _colf(u, "x"), _colf(u, "y"), _colf(u, "z")
        if xs is None or ys is None:
            continue
        nulls += int(np.isnan(xs).sum())
        arrs.append((u.get("key"), float(u.get("t0") or 0.0) + np.arange(n_u) / hz_u,
                     xs, ys, zs if zs is not None else np.zeros(n_u)))
    env = envelope_stats(arrs)
    ev.append("上采样后包线：超 600 m/s %d 步、单步 >5000 m %d 处、最大 %.0f m/s（共 %d 步）"
              % (env["violations"], env["teleports"], env["max_speed"], env["steps"]))
    ev.append("交付空中段的 null 样本 %d 个" % nulls)
    if env["violations"]:
        problems.append("上采样后仍有 %d 步 >600 m/s" % env["violations"])
    # null 断点是否被连线：交付样本落在源航段 tick 范围之外却非 null
    ranges = _src_tick_ranges(tracks)
    bridged = []
    checked = 0
    for u in air:
        rs = None
        for _kv in (u.get("uid"), u.get("entityId")):
            if _kv is None:
                continue
            if ranges.get(eid_key(_kv)):
                rs = ranges[eid_key(_kv)]
                break
        if not rs:
            continue
        n_u = int(u.get("n") or 0)
        hz_u = float(u.get("hz") or 1.0)
        xs = _colf(u, "x")
        if xs is None:
            continue
        tol = 0.0
        for a, b in rs:
            tol = max(tol, (b - a))
        tol = max(2.0 / 4.0 / spt, 0.5 / spt)   # 半个源采样间隔再放大
        t0u = float(u.get("t0") or 0.0)
        for i in range(n_u):
            if not np.isfinite(xs[i]):
                continue
            raw = (t0u + i / hz_u + origin) / spt
            inside = any(a - tol <= raw <= b + tol for a, b in rs)
            checked += 1
            if not inside:
                bridged.append({"key": u.get("key"), "i": i, "rawTick": int(raw)})
    ev.append("断点检查：核对 %d 个非 null 样本，落在源航段范围外（疑似把断档连成直线）的 %d 个"
              % (checked, len(bridged)))
    # 点数口径和解：逐段 n vs 源跨度×10+1（含端点）
    recon = []
    for u in air:
        rs = None
        for _kv in (u.get("uid"), u.get("entityId")):
            if _kv is not None and ranges.get(eid_key(_kv)):
                rs = ranges[eid_key(_kv)]
                break
        if not rs:
            continue
        span = sum((b - a) for a, b in rs) * spt
        recon.append((u.get("key"), int(u.get("n") or 0), round(span, 3),
                      int(round(span * 10)) + 1, len(rs)))
    diff = [r[1] - r[3] for r in recon]
    ev.append('点数口径和解：%d 段；交付 n 与『源跨度×10+1（含端点）』的差：min %s / max %s / 合计 %s；'
              "多段 uid 的段数分布 %s"
              % (len(recon), min(diff) if diff else None, max(diff) if diff else None,
                 sum(diff) if diff else None,
                 json.dumps({str(k): sum(1 for r in recon if r[4] == k) for k in sorted({r[4] for r in recon})})))
    ev.append("说明：差为 0 的段=源航段连续、交付按 10 Hz 含两端点取样；差为负的段=交付数组里有 null "
              "（或该 uid 由多段拼成、本单元只覆盖其中一段），不是桥接。")
    if bridged:
        ev.append("越界样本示例：%s" % json.dumps(bridged[:5], ensure_ascii=False))
        problems.append("%d 个样本落在源航段 tick 范围外仍非 null（上采样把 null 断点连起来了）"
                        % len(bridged))
    # own 段同样检查（own 有两段，接缝处应为 null）
    own = md.get("own") or {}
    if isinstance(own.get("n"), int) and own["n"] > 2:
        oz = _colf(own, "z")
        if oz is not None:
            ev.append("本机 own：n=%d，null %d 个（两段接缝处应为 null）"
                      % (own["n"], int(np.isnan(oz).sum())))
            if int(np.isnan(oz).sum()) == 0 and len(((tracks or {}).get("devValidation") or {})
                                                    .get("ownSegmentIds") or []) > 1:
                problems.append("own 有两段却没有任何 null，接缝可能被连成直线")
    ev.append("被验文件指纹：%s" % file_fingerprint(cfg.get("mission_path") or ""))
    if problems:
        add(results, "7b.units-10hz", "空中段 10 Hz 上采样与 null 断点", WARN, "；".join(problems[:3]), ev)
    else:
        add(results, "7b.units-10hz", "空中段 10 Hz 上采样与 null 断点", PASS,
            "%d 段全 10 Hz、%d 点、包线 0 违规、断点未被连线" % (len(air), env["steps"]), ev)


def check_display_names(cfg, md, results, text):
    """7c：显示名零非 ASCII + 无回退泄漏（按 lead 加严口径）。"""
    units = [u for u in (md.get("units") or []) if isinstance(u, dict)]
    if not units:
        add(results, "7c.display-names", "型号显示名（零非 ASCII / 无回退泄漏）", SKIP, "没有 units")
        return

    def _nonascii(s):
        return [ch for ch in str(s) if ord(ch) > 127]

    def _name(u):
        v = u.get("vehicleName")
        return v if isinstance(v, str) else ""

    hits = {"units[].vehicleName": 0, "shots[].templateName": 0,
            "airfields[].name": 0, "runways[].name": 0}
    samples = []
    for u in units:
        if _nonascii(_name(u)):
            hits["units[].vehicleName"] += 1
            samples.append(("unit", u.get("key"), _name(u)))
    for s in md.get("shots") or []:
        if isinstance(s, dict) and _nonascii(s.get("templateName")):
            hits["shots[].templateName"] += 1
            if len(samples) < 6:
                samples.append(("shot", s.get("template"), s.get("templateName")))
    for a in md.get("airfields") or []:
        if isinstance(a, dict) and _nonascii(a.get("name")):
            hits["airfields[].name"] += 1
            samples.append(("airfield", a.get("key"), a.get("name")))
    for r in md.get("runways") or []:
        if isinstance(r, dict) and _nonascii(r.get("name")):
            hits["runways[].name"] += 1
            samples.append(("runway", r.get("key"), r.get("name")))
    ev = ["非 ASCII 显示名计数：%s" % json.dumps(hits, ensure_ascii=False)]
    if samples:
        ev.append("非 ASCII 示例：%s" % json.dumps(samples[:6], ensure_ascii=False))
    problems = []
    if sum(hits.values()):
        n_bad = sum(hits.values())
        which = "、".join("%s %d 条" % (k, v) for k, v in hits.items() if v)
        problems.append("有 %d 处显示名含非 ASCII（%s）" % (n_bad, which))
    # map[vehicle] ?? vehicle 的可观测推论
    named = [u for u in units if _name(u)]
    grp = {}
    for u in named:
        grp.setdefault(u.get("vehicle"), set()).add(_name(u))
    inconsistent = [(v, sorted(s)) for v, s in grp.items() if len(s) > 1]
    ev.append("同一 vehicle 的显示名一致：%s；共 %d 个 vehicle 有名字"
              % ("是" if not inconsistent else "否 %s" % json.dumps(inconsistent[:3], ensure_ascii=False),
                 len(grp)))
    if inconsistent:
        problems.append("%d 个 vehicle 出现了多个显示名（映射不确定）" % len(inconsistent))
    leaks = [{"key": u.get("key"), "vehicleName": _name(u), "template": u.get("template")}
             for u in named if _name(u) == u.get("template")]
    ev.append("显示名 == template 的回退泄漏：%d 个" % len(leaks))
    if leaks:
        problems.append("%d 个显示名等于 template" % len(leaks))
    odd_empty = [u.get("key") for u in units
                 if u.get("vehicle") and not _name(u) and u.get("hidden") is not True]
    ev.append("有 vehicle 但名字为空、且不是 hidden 的单位：%d 个" % len(odd_empty))
    if odd_empty:
        problems.append("%d 个单位有 vehicle 却没有显示名且未标 hidden" % len(odd_empty))
    grd = [u for u in units if u.get("domain") == "ground"]
    air = [u for u in units if u.get("domain") == "air"]
    counts = (md.get("meta") or {}).get("counts") or {}
    ev.append("覆盖：地面 %d/%d、空中 %d/%d 有名字（counts.unitsWithDisplayName=%s）"
              % (sum(1 for u in grd if _name(u)), len(grd),
                 sum(1 for u in air if _name(u)), len(air), counts.get("unitsWithDisplayName")))
    if isinstance(counts.get("unitsWithDisplayName"), int) and counts["unitsWithDisplayName"] != len(named):
        problems.append("counts.unitsWithDisplayName=%s 与实际有名单位 %d 不符"
                        % (counts["unitsWithDisplayName"], len(named)))
    air_hits = []
    if text:
        for m in re.finditer(r'"AIR"|"GND"|>AIR<|>GND<', text):
            air_hits.append(text[max(0, m.start() - 30):m.end() + 10])
            if len(air_hits) >= 3:
                break
    ev.append("全文件 \"AIR\"/\"GND\" 字样：%d 处" % len(air_hits))
    if air_hits:
        problems.append("文件里仍出现 AIR/GND 回退字样 %d 处" % len(air_hits))
    ev.append("被验文件指纹：%s" % file_fingerprint(cfg.get("mission_path") or ""))
    if problems:
        add(results, "7c.display-names", "型号显示名（零非 ASCII / 无回退泄漏）", WARN,
            "；".join(problems[:3]), ev)
    else:
        add(results, "7c.display-names", "型号显示名（零非 ASCII / 无回退泄漏）", PASS,
            "四个来源全部零非 ASCII；%d/%d 有名字；无 template 回退泄漏、无 AIR/GND"
            % (len(named), len(units)), ev)


def check_hidden(cfg, md, results, texts):
    """7e：hidden 集合的条数/构成/空名/limits 理由 + 消费侧是否真的过滤。"""
    units = [u for u in (md.get("units") or []) if isinstance(u, dict)]
    if not units:
        add(results, "7e.hidden", "hidden 集合（21 条占位/雷达）与其消费", SKIP, "没有 units")
        return
    meta = md.get("meta") or {}
    counts = meta.get("counts") or {}
    hid = [u for u in units if u.get("hidden") is True]
    by_veh = {}
    for u in hid:
        by_veh[u.get("vehicle")] = by_veh.get(u.get("vehicle"), 0) + 1
    ev = ["hidden 单位 %d 个；counts.unitsHidden=%s" % (len(hid), counts.get("unitsHidden")),
          "构成：%s" % json.dumps(by_veh, ensure_ascii=False),
          "期望构成：dummy_immortal 9 / unit_airfield_flight_radar_char 8 / unit_airfield_main_radar_char 4"]
    problems = []
    if isinstance(counts.get("unitsHidden"), int) and counts["unitsHidden"] != len(hid):
        problems.append("counts.unitsHidden=%s 与实际 %d 不符" % (counts["unitsHidden"], len(hid)))
    expect = {"structures/dummy_immortal": 9,
              "structures/unit_airfield_flight_radar_char": 8,
              "structures/unit_airfield_main_radar_char": 4}
    if len(hid) == 21:
        if dict(by_veh) != expect:
            problems.append("hidden 构成与 9/8/4 不符：%s" % json.dumps(by_veh, ensure_ascii=False))
    else:
        ev.append("（构成 9/8/4 的硬判据只在恰好 21 条时启用；本样本 %d 条走一致性判据）" % len(hid))
    named = [u.get("key") for u in hid if (u.get("vehicleName") or "") != ""]
    ev.append("hidden 单位里 vehicleName 非空的：%d 个 %s" % (len(named), named[:5]))
    if named:
        problems.append("%d 个 hidden 单位的 vehicleName 不是空串" % len(named))
    lim = " ".join(str(x) for x in (meta.get("limits") or []))
    has_note = ("hidden" in lim.lower()) or ("隐藏" in lim) or ("占位" in lim) or ("雷达" in lim)
    ev.append("limits 是否写明隐藏理由（hidden/隐藏/占位/雷达）：%s" % has_note)
    if not has_note:
        problems.append("limits 没有交代隐藏理由")
    # 消费侧：渲染/UI 代码有没有读 hidden 标记
    sc = texts.get("scene3d.js") or ""
    mj = texts.get("mission.js") or ""
    reads = []
    dom_re = re.compile(r"(cv|el|panel|helpEl|wrap|cornerEl|btn|node|elem)\s*\.\s*hidden"
                        r"|\.hidden\s*=(?!=)|classList|setAttribute\(\s*['\"]hidden")
    for name, txt in (("scene3d.js", sc), ("mission.js", mj)):
        for m in re.finditer(r"\bhidden\b", txt):
            seg = txt[max(0, m.start() - 70):m.end() + 40].replace("\n", " ")
            if dom_re.search(seg):
                continue     # DOM 的 .hidden 属性/赋值不算消费数据标记
            reads.append("%s: …%s…" % (name, seg.strip()))
    ev.append("消费侧读 hidden 标记的代码位置：%d 处%s"
              % (len(reads), ("：" + json.dumps(reads[:3], ensure_ascii=False)) if reads else ""))
    if not reads:
        problems.append("渲染/UI 代码没有读 units[].hidden（21 个隐藏单位可能仍会被画出来）")
    ev.append("说明：本条只做数据层与消费侧**静态**核对；浏览器侧由 lead 终验。"
              "数据层已把 hidden 与 counts.unitsHidden 对齐。")
    ev.append("被验文件指纹：%s" % file_fingerprint(cfg.get("mission_path") or ""))
    if problems:
        add(results, "7e.hidden", "hidden 集合（21 条占位/雷达）与其消费", WARN, "；".join(problems[:3]), ev)
    else:
        add(results, "7e.hidden", "hidden 集合（21 条占位/雷达）与其消费", PASS,
            "21 条（9/8/4）全部 hidden+空名、limits 有理由、消费侧已读该标记", ev)


def check_runways(cfg, md, results):
    """runways[]：要么空数组 + 明确结论，要么给出线段与自证数字。"""
    meta = md.get("meta") or {}
    counts = meta.get("counts") or {}
    rw = md.get("runways")
    af = md.get("airfields")
    ev = ["runways 类型 %s，长度 %s；counts.runways=%s"
          % (type(rw).__name__, len(rw) if isinstance(rw, list) else "n/a", counts.get("runways")),
          "airfields 长度 %s；counts.airfields=%s"
          % (len(af) if isinstance(af, list) else "n/a", counts.get("airfields"))]
    problems = []
    if isinstance(counts.get("runways"), int) and isinstance(rw, list) and counts["runways"] != len(rw):
        problems.append("counts.runways=%s 与实际 %d 不符" % (counts["runways"], len(rw)))
    if not isinstance(rw, list):
        problems.append("runways 不是数组")
    elif len(rw) == 0:
        note = " ".join(str(x) for x in (meta.get("limits") or [])) + " " + \
               " ".join(str(x) for x in (meta.get("provenance") or []))
        has = ("跑道" in note) or ("runway" in note.lower()) or ("PCA" in note.upper())
        ev.append("空数组的说明检索（limits+provenance 里提到 跑道/runway/PCA）：%s" % has)
        if not has:
            problems.append("runways 是空数组，但 limits/provenance 没有交代原因")
    else:
        bad = 0
        heads = []
        for r in rw:
            if not isinstance(r, dict):
                bad += 1
                continue
            need = ("x1", "y1", "z1", "x2", "y2", "z2", "lengthM", "headingDeg")
            if any(not isinstance(r.get(k), (int, float)) or not np.isfinite(r[k]) for k in need):
                bad += 1
                ev.append("%s 缺字段或非有限" % r.get("key"))
                continue
            dx, dy = r["x2"] - r["x1"], r["y2"] - r["y1"]
            dist = math.hypot(dx, dy)
            hdg = math.degrees(math.atan2(dx, dy)) % 360.0
            dz = abs(r["z2"] - r["z1"])
            heads.append((r.get("key"), float(r["headingDeg"]), hdg, dist, dz, r))
            ev.append("%s：两端距离 **%.1f m**（报 %.1f，差 %+.2f）；反算航向 **%.2f°**（报 %.2f，差 %.2f）；"
                      "两端 z 差 **%.2f m**（自证阈值 ≤8 m）"
                      % (r.get("key"), dist, r["lengthM"], dist - r["lengthM"], hdg,
                         r["headingDeg"], abs(((hdg - r["headingDeg"] + 180) % 360) - 180), dz))
            if abs(dist - r["lengthM"]) > 2.0:
                problems.append("%s 两端距离与 lengthM 差 %.2f m" % (r.get("key"), dist - r["lengthM"]))
            if abs(((hdg - r["headingDeg"] + 180) % 360) - 180) > 0.2:
                problems.append("%s 反算航向与 headingDeg 差 >0.2°" % r.get("key"))
            if dz > 8.0:
                problems.append("%s 两端 z 差 %.2f m >8 m" % (r.get("key"), dz))
            if abs(dist - 3000.0) > 5.0:
                ev.append("  提示：%s 长度不是 3000 m（模型名给了 3000，差 %.1f m）" % (r.get("key"), dist - 3000.0))
        # 两两航向：每条跑道找最接近反向的伙伴
        for i, (ki, hi, _h, _d, _z, _r) in enumerate(heads):
            best = None
            for j, (kj, hj, _h2, _d2, _z2, _r2) in enumerate(heads):
                if i == j:
                    continue
                diff = abs(((hi - hj + 180) % 360) - 180)     # 0=同向, 180=反向
                err = abs(diff - 180)
                if best is None or err < best[0]:
                    best = (err, kj, diff)
            if best is not None:
                ev.append("%s 的最接近反向伙伴 %s：夹角 %.2f°（离 180° 差 %.2f°）"
                          % (ki, best[1], best[2], best[0]))
                if best[0] > 15.0:
                    problems.append("%s 找不到 15° 以内的反向伙伴（差 %.2f°）" % (ki, best[0]))
        # 机场结构数（主/备）
        afs = [a for a in (md.get("airfields") or []) if isinstance(a, dict)]
        st = sorted(int(a.get("structures") or 0) for a in afs)
        ev.append("airfields 结构数：%s（自证应为 13/13/3/3）"
                  % json.dumps([(a.get("key"), a.get("structures")) for a in afs], ensure_ascii=False))
        if st != [3, 3, 13, 13]:
            problems.append("airfields 结构数不是 13/13/3/3：%s" % st)
        # 机场点到最近跑道线段的垂距（独立交叉核对）
        for a in afs:
            best = None
            for ki, _hi, _h, _d, _z, r in heads:
                px, py = float(a["x"]), float(a["y"])
                x1, y1, x2, y2 = float(r["x1"]), float(r["y1"]), float(r["x2"]), float(r["y2"])
                vx, vy = x2 - x1, y2 - y1
                L2 = vx * vx + vy * vy
                t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * vx + (py - y1) * vy) / L2))
                cx, cy = x1 + t * vx, y1 + t * vy
                dd = math.hypot(px - cx, py - cy)
                if best is None or dd < best[0]:
                    best = (dd, ki, t)
            if best is not None:
                ev.append("%s → 最近跑道 %s 的垂距 %.0f m（线上投影 t=%.2f）"
                          % (a.get("key"), best[1], best[0], best[2]))
        if bad:
            problems.append("%d 条 runway 数据非法" % bad)
    ev.append("被验文件指纹：%s" % file_fingerprint(cfg.get("mission_path") or ""))
    if problems:
        add(results, "runways", "runways[]：空数组需有结论，或有线段+自证", WARN, "；".join(problems[:3]), ev)
    else:
        add(results, "runways", "runways[]：空数组需有结论，或有线段+自证", PASS,
            ("空数组且 limits/provenance 有交代" if isinstance(rw, list) and not rw
             else "给出 %d 条线段且数据合法" % len(rw)), ev)


def check_vehicle_category(cfg, md, results):
    """7f：units[].vehicleCategory 与 meta.vehicleCategories 的独立复算 + 语义交叉核对。"""
    units = [u for u in (md.get("units") or []) if isinstance(u, dict)]
    if not units:
        add(results, "7f.vehicle-category", "单位类别层（236/236 且语义自洽）", SKIP, "没有 units")
        return
    meta = md.get("meta") or {}
    mc = meta.get("vehicleCategories") or {}
    ev = ["meta.vehicleCategories：source=%s / field=%s / keys=%s / vocabulary=%s / "
          "unitsWithCategory=%s / unitsUncategorized=%s / uncategorizedValues=%s"
          % (str(mc.get("source"))[:110], mc.get("field"), mc.get("keys"),
             json.dumps(mc.get("vocabulary"), ensure_ascii=False), mc.get("unitsWithCategory"),
             mc.get("unitsUncategorized"), json.dumps(mc.get("uncategorizedValues"), ensure_ascii=False))]
    problems = []
    cats = {}
    for u in units:
        c = u.get("vehicleCategory")
        if not isinstance(c, str) or not c.strip():
            problems.append("有单位的 vehicleCategory 为空：%s" % u.get("key"))
            continue
        cats[c] = cats.get(c, 0) + 1
    ev.append("我独立算的分布（%d 条）：%s" % (sum(cats.values()), json.dumps(cats, ensure_ascii=False)))
    if sum(cats.values()) != len(units):
        problems.append("只有 %d/%d 条有类别" % (sum(cats.values()), len(units)))
    nonascii = [k for k in cats if any(ord(ch) > 127 for ch in k)]
    fallback = [k for k in cats if k.upper() in ("UNKNOWN", "NONE", "OTHER", "NULL", "N/A")]
    ev.append("非 ASCII 类别 %s；兜底字样 %s" % (nonascii or "无", fallback or "无"))
    if nonascii:
        problems.append("类别出现非 ASCII：%s" % nonascii)
    if fallback:
        problems.append("类别出现兜底字样：%s" % fallback)
    vocab = mc.get("vocabulary")
    if isinstance(vocab, list) and vocab:
        extra = sorted(set(cats) - set(vocab))
        ev.append("交付里的类别是否都在 meta 词表内：%s" % ("是" if not extra else "否，多出 %s" % extra))
        if extra:
            problems.append("有类别不在 meta.vocabulary 里：%s" % extra)
    else:
        problems.append("meta.vehicleCategories.vocabulary 缺失")
    if isinstance(mc.get("unitsWithCategory"), int) and mc["unitsWithCategory"] != len(units):
        problems.append("meta.vehicleCategories.unitsWithCategory=%s ≠ %d"
                        % (mc["unitsWithCategory"], len(units)))
    if isinstance(mc.get("unitsUncategorized"), int) and mc["unitsUncategorized"] != 0:
        problems.append("unitsUncategorized=%s（应 0）" % mc["unitsUncategorized"])
    if mc.get("uncategorizedValues"):
        problems.append("uncategorizedValues 非空：%s" % mc["uncategorizedValues"])
    if not isinstance(mc.get("keys"), int):
        problems.append("meta.vehicleCategories.keys 不是数字")
    if mc.get("field") != "units[].vehicleCategory":
        problems.append("meta.vehicleCategories.field=%r 与契约不一致" % mc.get("field"))
    ground_like = {"TANK", "SPAA", "ARTILLERY", "TRUCK", "RADAR", "BUNKER",
                   "FORTIFICATION", "IFV", "APC"}
    air_like = {"FIGHTER", "ATTACKER", "BOMBER", "HELI"}
    bad = []
    for u in units:
        c = u.get("vehicleCategory")
        veh = str(u.get("vehicle") or "").lower()
        dom = u.get("domain")
        if not isinstance(c, str):
            continue
        if dom == "air" and c not in air_like:
            bad.append({"key": u.get("key"), "domain": "air", "cat": c})
        if dom == "ground" and c in air_like:
            bad.append({"key": u.get("key"), "domain": "ground", "cat": c})
        if "radar" in veh and c != "RADAR":
            bad.append({"key": u.get("key"), "vehicle": veh, "cat": c, "why": "内部名含 radar"})
        if ("artillery" in veh or "art_noground" in veh) and c != "ARTILLERY":
            bad.append({"key": u.get("key"), "vehicle": veh, "cat": c, "why": "内部名含 artillery"})
        if "bunker" in veh and c != "BUNKER":
            bad.append({"key": u.get("key"), "vehicle": veh, "cat": c, "why": "内部名含 bunker"})
    ev.append("语义交叉核对（域/内部名 vs 类别）：%d 处不一致" % len(bad))
    if bad:
        ev.append("不一致示例：%s" % json.dumps(bad[:5], ensure_ascii=False))
        problems.append("%d 处类别与域/内部名矛盾" % len(bad))
    hid = [u for u in units if u.get("hidden") is True]
    hid_cats = {}
    for u in hid:
        hid_cats[u.get("vehicleCategory")] = hid_cats.get(u.get("vehicleCategory"), 0) + 1
    ev.append("hidden %d 条的类别分布：%s（vehicleName 仍应为空串）"
              % (len(hid), json.dumps(hid_cats, ensure_ascii=False)))
    if hid and any(not u.get("vehicleCategory") for u in hid):
        problems.append("有 hidden 单位没有类别")
    named_hidden = [u.get("key") for u in hid if (u.get("vehicleName") or "") != ""]
    if named_hidden:
        problems.append("%d 个 hidden 单位的 vehicleName 非空" % len(named_hidden))
    tp = os.path.join(os.path.dirname(cfg.get("mission_path") or ""),
                      "build", "wrpl", "vehicle_categories.json")
    if os.path.exists(tp):
        ev.append("类别源表（磁盘字节为准）：%s" % file_fingerprint(tp))
    else:
        ev.append("类别源表不在 %s（只核交付文件里的词表）" % tp)
    ev.append("被验文件指纹：%s" % file_fingerprint(cfg.get("mission_path") or ""))
    if problems:
        add(results, "7f.vehicle-category", "单位类别层（236/236 且语义自洽）", WARN,
            "；".join(problems[:3]), ev)
    else:
        add(results, "7f.vehicle-category", "单位类别层（236/236 且语义自洽）", PASS,
            "%d/%d 有类别、%d 个词、零非 ASCII、无兜底字样；语义交叉核对 0 处矛盾"
            % (sum(cats.values()), len(units), len(cats)), ev)


def check_squad(cfg, md, tracks, results):
    """meta.squad：作者 uid 与 ownEntityId 对齐 + 成员 uid 的 kill/death 指纹独立复核。"""
    meta = md.get("meta") or {}
    sq = meta.get("squad")
    if not isinstance(sq, dict):
        add(results, "meta.squad", "meta.squad 与回放内 kill/death 指纹自洽", SKIP,
            "交付文件没有 meta.squad")
        return
    members = [m for m in (sq.get("members") or []) if isinstance(m, dict)]
    own_uid = eid_key((tracks or {}).get("ownEntityId"))
    ev = ["schema=%s squadId=%s members=%d；tracks.ownEntityId=%s"
          % (sq.get("schema"), sq.get("squadId"), len(members), own_uid)]
    problems = []
    if len(members) != 2:
        problems.append("members 不是 2 个")
    kills = {}
    deaths = {}
    kp = (tracks or {}).get("killPositions") or []
    for k in kp:
        if isinstance(k, dict):
            if isinstance(k.get("killerUid"), int):
                kills[k["killerUid"]] = kills.get(k["killerUid"], 0) + 1
            if isinstance(k.get("victimUid"), int):
                deaths[k["victimUid"]] = deaths.get(k["victimUid"], 0) + 1
    veh_uids = {}
    for u in md.get("units") or []:
        if isinstance(u, dict) and u.get("vehicle") and u.get("uid") is not None:
            try:
                veh_uids.setdefault(u["vehicle"], set()).add(int(u["uid"]))
            except Exception:
                pass
    ev.append("我独立复算：killPositions %d 条 → 有击杀的 uid %d 个、有死亡的 uid %d 个；"
              "机型→uid 候选来自交付 units 的 vehicle 字段" % (len(kp), len(kills), len(deaths)))
    for m in members:
        uid = m.get("uid")
        veh = m.get("vehicle")
        cands = sorted(veh_uids.get(veh) or [])
        dev = m.get("uidEvidence") or {}
        if not dev:
            # 作者成员按 own-uid 判据、没有候选指纹：只核 uid 与 ownEntityId
            ev.append("成员 %s：没有 uidEvidence（uidSource=%s）→ 按 own-uid 判据，只核 uid 对齐"
                      % (m.get("name"), m.get("uidSource")))
            if m.get("isAuthor") is not True:
                problems.append("%s：非作者成员却没有 uidEvidence" % m.get("name"))
            if eid_key(uid) != own_uid:
                problems.append("%s：uid %s ≠ ownEntityId %s" % (m.get("name"), uid, own_uid))
            continue
        want = dev.get("step2_fingerprint_wanted") or {"kills": m.get("kills"),
                                                       "deaths": m.get("deaths")}
        ev.append("成员 %s：声明 uid=%s / vehicle=%s；我的机型候选 %s；想要的指纹 %s"
                  % (m.get("name"), uid, veh, cands, json.dumps(want, ensure_ascii=False)))
        s1 = sorted(int(x) for x in (dev.get("step1_vehicleCandidates") or []))
        if s1 and s1 != cands:
            problems.append("%s：step1 候选 %s ≠ 我算的 %s" % (m.get("name"), s1, cands))
        mine = {str(u): {"kills": kills.get(u, 0), "deaths": deaths.get(u, 0)} for u in cands}
        ev.append("  我的 (kills,deaths) 指纹：%s" % json.dumps(mine, ensure_ascii=False))
        match = [u for u in cands
                 if kills.get(u, 0) == want.get("kills") and deaths.get(u, 0) == want.get("deaths")]
        ev.append("  按指纹过滤后剩 %s（要求唯一且 == 声明 uid %s）" % (match, uid))
        if len(match) != 1:
            problems.append("%s：指纹过滤后剩 %d 个候选（应唯一）" % (m.get("name"), len(match)))
        else:
            try:
                if uid is None or int(match[0]) != int(uid):
                    problems.append("%s：指纹唯一解 %s ≠ 声明 %s" % (m.get("name"), match[0], uid))
            except Exception:
                problems.append("%s：声明 uid 不是整数" % m.get("name"))
        dpc = dev.get("fingerprintPerCandidate") or {}
        if dpc:
            mism = []
            for k, v in dpc.items():
                if isinstance(v, dict) and {kk: int(vv) for kk, vv in v.items()} != mine.get(str(k)):
                    mism.append(k)
            ev.append("  与作者 fingerprintPerCandidate 不一致的候选：%s" % (mism or "无"))
            if mism:
                problems.append("%s：作者候选指纹与我的复算不一致 %s" % (m.get("name"), mism))
    author = [m for m in members if m.get("isAuthor") is True]
    if not author:
        problems.append("没有 isAuthor 成员")
    else:
        a_uid = eid_key(author[0].get("uid"))
        ev.append("作者成员 %s：uid=%s（归一化 %s）vs tracks.ownEntityId 归一化 %s → %s"
                  % (author[0].get("name"), author[0].get("uid"), a_uid, own_uid,
                     "一致" if (own_uid and a_uid == own_uid) else "不一致"))
        if not (own_uid and a_uid == own_uid):
            problems.append("authorUid %s ≠ tracks.ownEntityId %s" % (author[0].get("uid"), own_uid))
    pls = [p for p in ((meta.get("matchResults") or {}).get("players") or []) if isinstance(p, dict)]
    hk = [p.get("humanKills") for p in pls if isinstance(p.get("humanKills"), (int, float))]
    has_ai = any("aiKills" in p for p in pls)
    ak = [p.get("aiKills") for p in pls if isinstance(p.get("aiKills"), (int, float))]
    ev.append("结算表 humanKills 取值集合 %s（n=%d）；aiKills 字段%s"
              % (sorted(set(hk)), len(hk), ("取值集合 %s" % sorted(set(ak))) if has_ai
                 else "不在 matchResults 里（在 header.json 的 players 里）"))
    if hk and any(x != 0 for x in hk):
        problems.append("humanKills 并非全场 0")
    if ak and any(x != 0 for x in ak):
        problems.append("aiKills 并非全场 0")
    ev.append("被验文件指纹：%s" % file_fingerprint(cfg.get("mission_path") or ""))
    if problems:
        add(results, "meta.squad", "meta.squad 与回放内 kill/death 指纹自洽", WARN, "；".join(problems[:3]), ev)
    else:
        add(results, "meta.squad", "meta.squad 与回放内 kill/death 指纹自洽", PASS,
            "%d 名成员的 uid 都被 (kills,deaths) 指纹唯一确定并与声明一致；authorUid 与 ownEntityId 对齐"
            % len(members), ev)


def check_terrain(cfg, md, results):
    """terrain：空则须有交代；非空则须结构合规、地图一致、几何数值干净。"""
    meta = md.get("meta") or {}
    lvl = str(((meta.get("replayHeader") or {}).get("level") or ""))
    lvl_base = lvl.split("/")[-1].rsplit(".", 1)[0].lower()
    tr = md.get("terrain")
    limits = [str(x) for x in (meta.get("limits") or [])]
    prov = [str(x) for x in (meta.get("provenance") or [])]
    both = " ".join(limits + prov)
    ev = ["本局关卡（meta.replayHeader.level）= %r → 归一化 %r" % (lvl, lvl_base),
          "terrain 类型：%s" % ("null" if tr is None else type(tr).__name__)]
    problems = []
    warn_only = []
    if tr is None:
        keys = [k for k in ("HM2", "hm2", "lmap", "等高", "地形", "contour", "terrain") if k in both]
        ev.append("limits/provenance 里与地形相关关键词命中：%s" % (keys or "无"))
        if not keys:
            problems.append("terrain 为 null，但 limits/provenance 没有交代原因")
    else:
        if not isinstance(tr, dict):
            problems.append("terrain 不是对象")
            tr = {}
        for k in ("meta", "contours", "water", "coastlines"):
            if k not in tr:
                problems.append("terrain 缺契约 §9.2 字段 %s" % k)
        tm = tr.get("meta") or {}
        ev.append("terrain.meta = %s" % json.dumps(tm, ensure_ascii=False)[:300])
        if tm.get("crs") != "replay-world-meters":
            problems.append("terrain.meta.crs=%r，期望 'replay-world-meters'" % tm.get("crs"))
        tmap = str(tm.get("map") or "")
        tmap_base = tmap.split("/")[-1].rsplit(".", 1)[0].lower()
        ok_map = bool(lvl_base and tmap_base and (lvl_base in tmap_base or tmap_base in lvl_base))
        ev.append("地形地图名 %r → 归一化 %r，与本局关卡比对：%s"
                  % (tmap, tmap_base, "一致" if ok_map else "不一致"))
        if not ok_map:
            problems.append("terrain.meta.map=%r 与本局关卡 %r 不一致（可能是别的地图的等高线）"
                            % (tmap, lvl))
        cs = tr.get("contours") or []
        wt = tr.get("water") or []
        cl = tr.get("coastlines") or []
        cnt = (meta.get("counts") or {}).get("terrainContours")
        ev.append("层数：contours %d / water %d / coastlines %d；counts.terrainContours=%s"
                  % (len(cs), len(wt), len(cl), cnt))
        if isinstance(cnt, int) and cnt != len(cs):
            problems.append("counts.terrainContours=%s ≠ contours.length=%d" % (cnt, len(cs)))
        # 等高线层序与间隔
        elevs = []
        bad_nested = 0
        npts = 0
        b = tm.get("bounds") or {}
        minx, maxx = b.get("minX"), b.get("maxX")
        miny, maxy = b.get("minY"), b.get("maxY")
        outb = 0
        for c in cs:
            if isinstance(c, dict) and isinstance(c.get("elev"), (int, float)):
                elevs.append(float(c["elev"]))
            for line in (c.get("lines") if isinstance(c, dict) else []) or []:
                arr = np.asarray(line, dtype=float)
                if arr.size < 2:
                    continue
                npts += arr.size // 2
                if not np.all(np.isfinite(arr)):
                    bad_nested += 1
                    continue
                xs, ys = arr[0::2], arr[1::2]
                if None not in (minx, maxx, miny, maxy):
                    outb += int(((xs < minx) | (xs > maxx) | (ys < miny) | (ys > maxy)).sum())
        def _flat_bad(coll):
            bad = 0
            n = 0
            for item in coll:
                arr = np.asarray(item, dtype=float)
                n += arr.size // 2
                if not np.all(np.isfinite(arr)):
                    bad += 1
            return bad, n
        bad_w, n_w = _flat_bad(wt)
        bad_c, n_c = _flat_bad(cl)
        ev.append("等高线：%d 层、%d 点；elev %.0f..%.0f；非有限数组 %d；越界点 %d"
                  % (len(elevs), npts, min(elevs) if elevs else 0, max(elevs) if elevs else 0,
                     bad_nested, outb))
        ev.append("水体 %d 条/%d 点；海岸线 %d 条/%d 点；非有限数组 %d" % (len(wt), n_w, len(cl), n_c, bad_w + bad_c))
        if not elevs:
            problems.append("terrain.contours 没有可读的 elev")
        else:
            if any(b2 <= a2 for a2, b2 in zip(elevs, elevs[1:])):
                problems.append("等高线 elev 不是严格递增")
            step_exp = tm.get("contourIntervalM")
            if isinstance(step_exp, (int, float)) and len(elevs) > 1:
                steps = {round(b2 - a2, 3) for a2, b2 in zip(elevs, elevs[1:])}
                if steps != {round(float(step_exp), 3)}:
                    problems.append("等高线间隔 %s 与 contourIntervalM=%s 不符" % (sorted(steps)[:4], step_exp))
        if bad_nested or bad_w or bad_c:
            problems.append("地形几何里有非有限坐标（等高线 %d / 水体 %d / 海岸线 %d 条）"
                            % (bad_nested, bad_w, bad_c))
        if outb:
            problems.append("有 %d 个等高线点越出 terrain.meta.bounds" % outb)
        # 交付文本自相矛盾：terrain 非空但 limits 还写着 null
        stale = [x for x in limits if "terrain" in x.lower() and "null" in x]
        ev.append("limits 里仍写着 terrain=null 的旧句：%d 条" % len(stale))
        if stale:
            warn_only.append("terrain 已填充，但 limits 仍保留 %d 条「terrain 为 null」的旧说明（文本与数据矛盾）"
                             % len(stale))
    ev.append("被验文件指纹：%s" % file_fingerprint(cfg.get("mission_path") or ""))
    if problems or warn_only:
        add(results, "terrain", "terrain 层：空需交代 / 非空需合规且不串图",
            WARN, "；".join((problems + warn_only)[:3]), ev)
    else:
        add(results, "terrain", "terrain 层：空需交代 / 非空需合规且不串图", PASS,
            ("terrain=null，且 limits 里写明了原因" if tr is None
             else "terrain 结构合规、crs 正确、地图名一致；%d 层/%d 水体/%d 海岸线坐标全部有限且不越界"
                  % (len(tr.get("contours") or []), len(tr.get("water") or []), len(tr.get("coastlines") or []))),
            ev)


def check_shots(cfg, md, results):
    """3f：顶层 shots[]（弹药发射点）的结构与合并保真。"""
    shots = md.get("shots")
    if not isinstance(shots, list) or not shots:
        add(results, "3f.shots", "shots[] 结构与合并保真（1734 条发射点）", SKIP,
            "交付文件没有 shots 数组（或为空）")
        return
    meta = md.get("meta") or {}
    counts = meta.get("counts") or {}
    rt = meta.get("replayTicks") or {}
    ev = ["shots 条数 %d；counts.shots=%s" % (len(shots), counts.get("shots"))]
    problems = []
    if isinstance(counts.get("shots"), int) and counts["shots"] != len(shots):
        problems.append("counts.shots=%s 与实际 %d 不符" % (counts["shots"], len(shots)))
    tmpl = {}
    sides = {}
    yaw_null = 0
    shooter_null = 0
    side_null = 0
    bad_coord = 0
    ticks = []
    ts = []
    keys = set()
    for i, sh in enumerate(shots):
        if not isinstance(sh, dict):
            problems.append("shots[%d] 不是对象" % i)
            continue
        keys |= set(sh.keys())
        tmpl[sh.get("template")] = tmpl.get(sh.get("template"), 0) + 1
        s = sh.get("side")
        sides[s] = sides.get(s, 0) + 1
        if sh.get("yaw") is None:
            yaw_null += 1
        if sh.get("shooterUid") in (None, ""):
            shooter_null += 1
        if s is None:
            side_null += 1
        if not all(isinstance(sh.get(k), (int, float)) and np.isfinite(sh.get(k))
                   for k in ("x", "y", "z")):
            bad_coord += 1
        if isinstance(sh.get("tickRaw"), (int, float)):
            ticks.append(float(sh["tickRaw"]))
        if isinstance(sh.get("t"), (int, float)):
            ts.append(float(sh["t"]))
    ev.append("字段集合：%s" % sorted(keys))
    ev.append("模板计数：%s" % json.dumps(tmpl, ensure_ascii=False))
    expect_tmpl = {"rocket": 1589, "bomb": 95, "rocket+arh_shell": 29, "payload": 17, "jettisoned": 4}
    if tmpl != expect_tmpl:
        problems.append("模板计数与 1589/95/29/17/4 不符：%s" % json.dumps(tmpl, ensure_ascii=False))
    non_null_sides = [k for k in sides if k is not None]
    bad_enum = [k for k in non_null_sides if k not in ("ally", "enemy", "squad", "unknown")]
    if bad_enum:
        problems.append("shots[].side 出现契约外取值：%s" % bad_enum)
    ev.append("side 分布：%s（null %d）" % (json.dumps({str(k): v for k, v in sides.items()}, ensure_ascii=False),
                                        side_null))
    if shot_mono := (ts and any(b < a - 1e-9 for a, b in zip(ts, ts[1:]))):
        problems.append("shots 的 t 非单调")
    ev.append("t 范围 %s..%s，%s单调" % (round(min(ts), 3) if ts else None,
                                       round(max(ts), 3) if ts else None,
                                       "非" if shot_mono else ""))
    if ticks and isinstance(rt.get("tick0"), (int, float)) and isinstance(rt.get("tickLast"), (int, float)):
        outn = sum(1 for t in ticks if t < rt["tick0"] - 1 or t > rt["tickLast"] + 1)
        ev.append("tickRaw 范围 %.0f..%.0f；回放 [%s, %s]；越界 %d 条"
                  % (min(ticks), max(ticks), rt["tick0"], rt["tickLast"], outn))
        if outn:
            problems.append("%d 条 shots 的 tickRaw 落在回放范围外" % outn)
    ev.append("yaw 为 null 的 %d/%d（作者口径：两种四元数分量顺序与发射机航向中位差 137.6°/103.5°，"
              "不可验证就留 null —— **不是数据丢失**）；shooterUid 为空的 %d 条"
              % (yaw_null, len(shots), shooter_null))
    if bad_coord:
        problems.append("有 %d 条 shots 坐标非有限" % bad_coord)
    # 与源文件合并保真
    d = load_json_file(cfg.get("projectile_path"))
    if isinstance(d, dict) and isinstance(d.get("units"), list):
        # 同一 (tickRaw, template) 可能是多发齐射 → 用候选列表（不能用 dict，否则键重复会把配对错位）
        src_by_key = {}
        for u in d["units"]:
            if isinstance(u, dict) and isinstance(u.get("spawnTickRaw"), (int, float)):
                src_by_key.setdefault((int(u["spawnTickRaw"]), u.get("template")), []).append(u)
        n_dup_keys = sum(1 for v in src_by_key.values() if len(v) > 1)
        maxd = 0.0
        matched = 0
        unmatched = 0
        for sh in shots:
            if not isinstance(sh, dict):
                continue
            k = (int(sh["tickRaw"]) if isinstance(sh.get("tickRaw"), (int, float)) else None,
                 sh.get("template"))
            cands = src_by_key.get(k) or []
            if not cands:
                unmatched += 1
                continue
            p = [float(sh.get(c)) for c in ("x", "y", "z")]
            best_j = min(range(len(cands)),
                         key=lambda j: sum((p[i] - float(cands[j][c])) ** 2
                                           for i, c in enumerate(("x", "y", "z"))))
            q = cands.pop(best_j)
            dd = float(np.sqrt(sum((p[i] - float(q[c])) ** 2 for i, c in enumerate(("x", "y", "z")))))
            maxd = max(maxd, dd)
            matched += 1
        ev.append("与源 projectile_launches.json 按 (tickRaw, template) 取最近候选配对："
                  "匹配 %d/%d、未匹配 %d；重复键（齐射）%d 组；坐标最大差 **%.3f m**"
                  % (matched, len(shots), unmatched, n_dup_keys, maxd))
        if unmatched:
            problems.append("有 %d 条 shots 在源文件里找不到同 (tickRaw, template) 的点" % unmatched)
        # 交付坐标按整数米写出（源文件是小数）：三轴各半米舍入 → 理论最大 √3×0.5 ≈ 0.87 m
        if matched and maxd >= 1.5:
            problems.append("与源文件坐标最大差 %.3f m（超出整数米舍入范围，疑似改动了坐标）" % maxd)
        elif matched:
            ev.append("说明：交付坐标是整数米、源文件是小数，最大差 %.3f m 属舍入（理论 ≤0.87 m），非坐标改动"
                      % maxd)
    ev.append("被验文件指纹：%s" % file_fingerprint(cfg.get("mission_path") or ""))
    if problems:
        add(results, "3f.shots", "shots[] 结构与合并保真（1734 条发射点）", WARN,
            "；".join(problems[:3]), ev)
    else:
        add(results, "3f.shots", "shots[] 结构与合并保真（1734 条发射点）", PASS,
            "%d 条 shots：模板计数/单调/tick 范围/counts 全部自洽，与源文件坐标一致" % len(shots), ev)


def check_scope(cfg, md, tracks, results):
    """3c：交付范围与解码产物一致性（实体数、是否有未说明的实体类别）。"""
    if not tracks:
        add(results, "3c.entity-scope", "交付范围与解码产物一致（实体数/未说明类别）", SKIP,
            "tracks.json 不存在，无法核对范围")
        return
    md_units = md.get("units") or []
    tr_units = tracks.get("units") or []
    dv = tracks.get("devValidation") or {}
    own_seg = dv.get("ownSegmentIds") or []
    counts = (md.get("meta") or {}).get("counts") or {}
    ev = ["tracks.json units %d 段；ownSegmentIds %s（%d 段）"
          % (len(tr_units), json.dumps(own_seg, ensure_ascii=False), len(own_seg)),
          "mission-data units %d 条（counts.airTracks=%s / groundTracks=%s）"
          % (len(md_units), counts.get("airTracks"), counts.get("groundTracks"))]
    problems = []
    air_n = sum(1 for u in md_units if isinstance(u, dict) and u.get("domain") == "air")
    grd_n = sum(1 for u in md_units if isinstance(u, dict) and u.get("domain") == "ground")
    if own_seg:
        expected_air = len(tr_units) - len(own_seg)
        ev.append("tracks %d 段 − own %d 段 = %d（空中），交付 air %d + ground %d = %d 条"
                  % (len(tr_units), len(own_seg), expected_air, air_n, grd_n, len(md_units)))
        if expected_air != air_n:
            problems.append("空中实体数对不上：期望 %d，实际 %d" % (expected_air, air_n))
    _g = load_json_file(cfg.get("ground_path"))
    if isinstance(_g, dict) and isinstance(_g.get("units"), list):
        ev.append("ground.json 源产物 %d 条 → 交付 ground %d 条" % (len(_g["units"]), grd_n))
        if len(_g["units"]) != grd_n:
            problems.append("地面实体数：源 %d，交付 %d" % (len(_g["units"]), grd_n))
    domains = {}
    for u in md_units:
        if isinstance(u, dict):
            domains[u.get("domain")] = domains.get(u.get("domain"), 0) + 1
    ev.append("交付 units 的 domain 分布：%s" % json.dumps(domains, ensure_ascii=False))
    if isinstance(counts.get("airTracks"), int) and counts["airTracks"] != domains.get("air", 0):
        problems.append("counts.airTracks=%s 与实际 air 条数 %s 不符"
                        % (counts.get("airTracks"), domains.get("air", 0)))
    for _k, _dom in (("groundUnits", "ground"), ("groundTracks", "ground")):
        if isinstance(counts.get(_k), int) and counts[_k] != domains.get(_dom, 0):
            problems.append("counts.%s=%s 与实际 %s 条数 %s 不符"
                            % (_k, counts[_k], _dom, domains.get(_dom, 0)))
    tr_keys = sorted(tracks.keys()) if isinstance(tracks, dict) else []
    suspicious = [k for k in tr_keys if any(w in str(k).lower() for w in ("missile", "rocket", "bomb"))]
    ev.append("tracks.json 顶层键：%s" % tr_keys[:14])
    ev.append("疑似武器/弹药类顶层字段：%s" % (suspicious or "无"))
    if suspicious:
        problems.append("tracks.json 出现 %s，需与 REPLAY-DECODE.md 的范围说明一致" % suspicious)
    for _key, _cnt in (("airfields", "airfields"), ("runways", "runways")):
        _v = md.get(_key)
        if isinstance(_v, list):
            ev.append("%s 层：%d 条（counts.%s=%s）—— 不在 units 里，属独立图层"
                      % (_key, len(_v), _cnt, counts.get(_cnt)))
    ev.append("范围说明：wrpl-probe REPLAY-DECODE.md §12 记录 ptype=2 里无导弹（44 个 uid 全是飞机；"
              "1589 个 rocket 实体逐帧位置未解出），故 tracks.json 不写 missiles 字段。"
              "我无法独立验证“回放里确实没有导弹”，只核交付范围与该说明一致。")
    if problems:
        add(results, "3c.entity-scope", "交付范围与解码产物一致（实体数/未说明类别）", WARN,
            "；".join(problems), ev)
    else:
        add(results, "3c.entity-scope", "交付范围与解码产物一致（实体数/未说明类别）", PASS,
            "实体数与 domain 分布自洽；无未说明的武器类字段", ev)


def check_ground(cfg, raw, tracks, mapping, results):
    """3d：可选产物 ground.json（静态地面点位）的独立核对。"""
    g = load_json_file(cfg.get("ground_path"))
    if not isinstance(g, dict) or not isinstance(g.get("units"), list) or not g["units"]:
        add(results, "3d.ground-points", "ground.json 静态点位（可选产物）", SKIP,
            "文件不存在或无 units：%s" % cfg.get("ground_path"))
        return
    units = [u for u in g["units"] if isinstance(u, dict)]
    spt = g.get("secPerTick") or (tracks or {}).get("secPerTick")
    span = (tracks or {}).get("replaySpanSec")
    ev = ["ground.json：%d 条；secPerTick=%s tickUnit=%s axisMap=%s source=%s"
          % (len(units), spt, g.get("tickUnit"), json.dumps(g.get("axisMap"), ensure_ascii=False),
             str(g.get("source"))[:120]),
          "devValidation（作者原样透传）：%s" % json.dumps(g.get("devValidation"), ensure_ascii=False)[:300]]
    classes = {}
    for u in units:
        classes[u.get("class")] = classes.get(u.get("class"), 0) + 1
    ev.append("分类计数：%s" % json.dumps(classes, ensure_ascii=False))
    problems = []
    # ① 时间窗
    ticks = [u.get("spawnTickRaw") for u in units if isinstance(u.get("spawnTickRaw"), (int, float))]
    if ticks and spt:
        secs = [t * float(spt) for t in ticks]
        ev.append("spawnTickRaw×secPerTick：%.3f..%.3f s（n=%d）" % (min(secs), max(secs), len(secs)))
        if span is not None and (min(secs) < -1 or max(secs) > float(span) + 1):
            problems.append("有 %d 个点落在对局窗口 [0, %.1f] s 之外"
                            % (sum(1 for s in secs if s < -1 or s > float(span) + 1), float(span)))
    # ② 坐标有限 + 与 tracks 同一 extent
    pts = []
    for u in units:
        if all(isinstance(u.get(k), (int, float)) and np.isfinite(u.get(k)) for k in ("x", "y", "z")):
            pts.append([u["x"], u["y"], u["z"]])
    if not pts:
        problems.append("没有可用坐标")
        P = None
    else:
        P = np.asarray(pts, dtype=float)
        ev.append("点位范围：x %.0f..%.0f  y %.0f..%.0f  z %.1f..%.1f"
                  % (P[:, 0].min(), P[:, 0].max(), P[:, 1].min(), P[:, 1].max(),
                     P[:, 2].min(), P[:, 2].max()))
        tpts = []
        for u in (tracks or {}).get("units") or []:
            a = ent_array(u)
            if a is not None and a.shape[0]:
                tpts.append(a[:, 1:4])
        if tpts:
            T = np.vstack(tpts)
            ev.append("tracks 航迹范围：x %.0f..%.0f  y %.0f..%.0f  z %.1f..%.1f"
                      % (T[:, 0].min(), T[:, 0].max(), T[:, 1].min(), T[:, 1].max(),
                         T[:, 2].min(), T[:, 2].max()))
            outside = int(((P[:, 0] < T[:, 0].min() - 3000) | (P[:, 0] > T[:, 0].max() + 3000)
                           | (P[:, 1] < T[:, 1].min() - 3000) | (P[:, 1] > T[:, 1].max() + 3000)).sum())
            ev.append("落在航迹包围盒外（放宽 3 km）的点：%d/%d → %s"
                      % (outside, len(P), "轴向/框架一致" if outside == 0 else "需检查轴向"))
            if outside:
                problems.append("%d 个点位在航迹包围盒外，可能坐标轴不同向" % outside)
    # ③ 与 8111 地面接触点对照（开发期）
    if P is not None and raw and mapping and mapping.get("ok"):
        try:
            own_u = None
            for u in (tracks or {}).get("units") or []:
                if u.get("entityId") == (tracks or {}).get("playerEntityId") or \
                        u.get("entityId") == mapping.get("entity"):
                    own_u = u
                    break
            sim = None
            if own_u is not None:
                a = ent_array(own_u)
                trep = (a[:, 0] - float(mapping["tick_lo"])) * float(mapping["sec_per_tick"])
                pxy = raw.get("player_xy") or {}
                rr = np.asarray(pxy.get("rel") or [], dtype=float)
                rx = np.asarray(pxy.get("x") or [], dtype=float)
                ry = np.asarray(pxy.get("y") or [], dtype=float)
                q = rr - float(mapping["offset_s"])
                ok = (q >= trep[0]) & (q <= trep[-1]) & np.isfinite(rx) & np.isfinite(ry)
                if int(ok.sum()) >= 10:
                    P2 = np.column_stack([np.interp(q[ok], trep, a[:, 1]),
                                          np.interp(q[ok], trep, a[:, 2])])
                    Q2 = np.column_stack([rx[ok], ry[ok]])
                    sim = procrustes_fit(P2, Q2, allow_reflection=True)
            if sim is not None:
                ground_contacts = np.asarray([[c[4], c[5]] for c in (raw.get("contacts") or [])
                                              if c[1] == "ground_model"], dtype=float)
                if ground_contacts.size:
                    def _nn_dist(points):
                        w = sim["scale"] * (points @ sim["R"].T) + sim["t"]
                        d = np.sqrt(((w[:, None, :] - ground_contacts[None, :, :]) ** 2).sum(-1)).min(1)
                        return d
                    d_ok = _nn_dist(P[:, :2])
                    flip = P[:, :2].copy()
                    flip[:, 1] = -flip[:, 1]
                    d_bad = _nn_dist(flip)
                    ev.append("与 8111 地面接触点（%d 个，开发期）最近距离："
                              "中位 %.1f m、p90 %.1f m、≤100 m 占 %.1f%%；"
                              "对照（北向取反）中位 %.1f m"
                              % (ground_contacts.shape[0], float(np.median(d_ok)),
                                 float(np.percentile(d_ok, 90)),
                                 100.0 * float((d_ok <= 100).mean()), float(np.median(d_bad))))
                    ev.append("（8111 只用于开发期验证；作者自报口径为中位 0.03 m / ≤100 m 占 66.3% / 取反中位 3632 m）")
                    if float(np.median(d_ok)) > 200.0:
                        problems.append("与 8111 地面接触点的中位距离 %.1f m 偏大" % float(np.median(d_ok)))
        except Exception as exc:
            ev.append("8111 对照异常：%s" % exc)
    ev.append("被验文件指纹：%s" % file_fingerprint(cfg.get("ground_path") or ""))
    ev.append("注意：ground.json 是静态点位、无速度，**未计入 4d/4d2 的速度包线统计**（避免误报瞬移）。")
    if problems:
        add(results, "3d.ground-points", "ground.json 静态点位（可选产物）", WARN,
            "；".join(problems[:3]), ev)
    else:
        add(results, "3d.ground-points", "ground.json 静态点位（可选产物）", PASS,
            "轴向/时间窗与 tracks 一致；%d 条静态点位未被混入航迹统计" % len(units), ev)


def check_structure(cfg, md, results):
    problems = []
    warns = []
    ev = []
    for k in ("meta", "own", "units", "events", "names"):
        if k not in md:
            problems.append("顶层缺 %s" % k)
    meta = md.get("meta") if isinstance(md.get("meta"), dict) else {}
    for k in META_KEYS_V4:
        if k not in meta:
            problems.append("meta 缺 %s" % k)
    pl = meta.get("player") or {}
    for k in ("name", "vehicle", "callsign"):
        if k not in pl:
            warns.append("meta.player 缺 %s" % k)
    win = meta.get("window") or {}
    if not isinstance(win, dict) or "t0" not in win or "t1" not in win:
        problems.append("meta.window 缺 t0/t1")
    else:
        try:
            if not float(win["t1"]) > float(win["t0"]):
                problems.append("meta.window t1<=t0")
            dur = win.get("duration")
            if isinstance(dur, (int, float)) and abs(dur - (float(win["t1"]) - float(win["t0"]))) > 0.05:
                warns.append("meta.window.duration 与 t1-t0 不一致（%s vs %.3f）"
                             % (dur, float(win["t1"]) - float(win["t0"])))
        except Exception:
            problems.append("meta.window t0/t1 不是数字")
    grid = meta.get("grid") or {}
    if not isinstance(grid, dict) or not grid.get("stepX") or not grid.get("stepY"):
        problems.append("meta.grid 缺 stepX/stepY")
    rt = meta.get("replayTicks") or {}
    for k in ("tick0", "tickLast", "secPerTick"):
        if k not in rt:
            warns.append("meta.replayTicks 缺 %s" % k)

    own = md.get("own")
    if not isinstance(own, dict):
        problems.append("own 不是对象")
    else:
        for k in OWN_KEYS_V4:
            if k not in own:
                problems.append("own 缺 %s" % k)
        n = own.get("n")
        if isinstance(n, int) and n > 0:
            for k in ("x", "y", "z", "ias", "mach", "hdg", "roll", "pitch", "g",
                      "aoa", "ammo", "fuel"):
                v = own.get(k)
                if isinstance(v, list) and len(v) != n:
                    problems.append("own.%s 长度 %d != n=%d" % (k, len(v), n))
        if not own.get("hz") or float(own.get("hz") or 0) <= 0:
            problems.append("own.hz 不合法")
        ev.append("own: n=%s hz=%s t0=%s" % (own.get("n"), own.get("hz"), own.get("t0")))

    units = md.get("units")
    if not isinstance(units, list):
        problems.append("units 不是数组")
        units = []
    z_ranges = []
    for i, u in enumerate(units):
        if not isinstance(u, dict):
            problems.append("units[%d] 不是对象" % i)
            continue
        _static = u.get("static") is True or (u.get("domain") == "ground" and u.get("n") == 1)
        _req = UNIT_KEYS_V4 if not _static else [k for k in UNIT_KEYS_V4 if k != "hdg"]
        for k in _req:
            if k not in u:
                problems.append("units[%d] 缺 %s" % (i, k))
        if _static and "hdg" not in u:
            warns.append("units[%d] 是静态点位、无 hdg（允许）" % i)
        if u.get("key") == "Player":
            problems.append("units[%d] 把 Player 混进了 units" % i)
        n = u.get("n")
        for k in ("x", "y", "z", "hdg"):
            v = u.get(k)
            if isinstance(v, list) and isinstance(n, int) and len(v) != n:
                problems.append("units[%d].%s 长度 %d != n=%s" % (i, k, len(v), n))
        if u.get("domain") not in ("air", "ground"):
            warns.append("units[%d].domain=%r" % (i, u.get("domain")))
        if u.get("side") not in ("ally", "enemy", "squad", "unknown"):
            warns.append("units[%d].side=%r" % (i, u.get("side")))
        for k in u:
            if k != "hasAlt" and "alt" in str(k).lower():
                problems.append("units[%d] 出现高度字段 %r（平面通道污染）" % (i, k))
        z = u.get("z")
        if u.get("hasAlt") is False:
            if isinstance(z, list) and any(x is not None for x in z):
                problems.append("units[%d] hasAlt=false 却带 z 数值（8111 补高度）" % i)
        if isinstance(z, list) and z:
            vals = [x for x in z if isinstance(x, (int, float))]
            if vals:
                z_ranges.append((u.get("key"), min(vals), max(vals)))
                if min(vals) < -1200 or max(vals) > 30000:
                    problems.append("units[%d].z 超出合理高度 %.0f..%.0f" % (i, min(vals), max(vals)))
            x = u.get("x")
            if isinstance(x, list) and len(x) == len(z):
                same = sum(1 for a, b in zip(x, z) if isinstance(a, (int, float))
                           and isinstance(b, (int, float)) and abs(a - b) < 1e-6)
                if len(z) >= 4 and same > 0.9 * len(z):
                    problems.append("units[%d].z 与 x 几乎相同（疑似通道串位）" % i)

    events = md.get("events")
    if not isinstance(events, list):
        problems.append("events 不是数组")
        events = []
    ts = []
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            problems.append("events[%d] 不是对象" % i)
            continue
        for k in EVENT_KEYS_V4:
            if k not in e:
                problems.append("events[%d] 缺 %s" % (i, k))
        if not isinstance(e.get("t"), (int, float)):
            problems.append("events[%d].t 不是数字" % i)
        else:
            ts.append(float(e["t"]))
        if e.get("kind") not in ("kill", "damage", "award", "disconnect", "lastblow", "chat"):
            warns.append("events[%d].kind=%r 不在契约 §2.4 枚举内" % (i, e.get("kind")))
    for a, b in zip(ts, ts[1:]):
        if b < a - 1e-9:
            problems.append("events 时间轴非单调：%.3f → %.3f" % (a, b))
            break
    if not isinstance(md.get("names"), dict):
        problems.append("names 不是对象")

    counts = meta.get("counts") or {}
    if isinstance(counts, dict):
        pairs = [("ownSamples", (own or {}).get("n")), ("trackCount", len(units)),
                 ("events", len(events)),
                 ("airTracks", sum(1 for u in units if isinstance(u, dict) and u.get("domain") == "air")),
                 ("groundTracks", sum(1 for u in units if isinstance(u, dict) and u.get("domain") == "ground")),
                 ("kills", sum(1 for e in events if isinstance(e, dict)
                                and e.get("kind") == "kill")),
                 ("ownKills", sum(1 for e in events if isinstance(e, dict)
                                  and e.get("isPlayerActor") and e.get("kind") == "kill")),
                 ("ownKillsByEvents", sum(1 for e in events if isinstance(e, dict)
                                          and e.get("isPlayerActor") and e.get("kind") == "kill"))]
        for k, real in pairs:
            if k in counts and counts[k] != real:
                problems.append("meta.counts.%s=%s 与实际 %s 不符" % (k, counts[k], real))
    else:
        problems.append("meta.counts 不是对象")

    ev.append("units=%d（air=%d ground=%d），events=%d，names=%d"
              % (len(units),
                 sum(1 for u in units if isinstance(u, dict) and u.get("domain") == "air"),
                 sum(1 for u in units if isinstance(u, dict) and u.get("domain") == "ground"),
                 len(events), len(md.get("names") or {})))
    ev.append("events 时间轴 %s 单调" % ("是" if not any(b < a - 1e-9 for a, b in zip(ts, ts[1:])) else "否"))
    if z_ranges:
        ev.append("units z 范围（前 5）：%s" % z_ranges[:5])
    if warns:
        ev.append("提醒：%s" % warns[:12])
    if problems:
        add(results, "3.structure", "结构符合契约 §2、时间轴单调、平面通道干净", FAIL,
            "%d 处问题" % len(problems), ev + problems[:40])
    else:
        add(results, "3.structure", "结构符合契约 §2、时间轴单调、平面通道干净", PASS,
            "字段齐全、时间轴单调、units 无 height 混入", ev)


# ---------------------------------------------------------------- 检查 4：回放解码独立复算

def _colf(d, key):
    v = d.get(key) if isinstance(d, dict) else None
    if not isinstance(v, list) or not v:
        return None
    return np.asarray([np.nan if x is None else float(x) for x in v], dtype=float)


def eid_key(v):
    """把 uid-368 / 368 / "0x170" 这类实体号归一到可比字符串。"""
    if v is None:
        return ""
    s = str(v).strip().lower()
    if s.startswith("uid-"):
        s = s[4:]
    if "#" in s:          # 断轨后的段后缀：uid-368#2 → 368
        s = s.split("#", 1)[0]
    if s.startswith("0x"):
        try:
            return str(int(s, 16))
        except Exception:
            return s
    return s


def color_family(hexcol):
    """8111 地图颜色 → 侧别族：红=敌、蓝=友、绿=小队、琥珀=自己。"""
    try:
        h = str(hexcol).lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except Exception:
        return "unknown"
    if r > 200 and g > 150 and b < 100:
        return "self"
    if r > 140 and g < 100 and b < 100:
        return "enemy"
    if b > 140 and r < 100:
        return "ally"
    if g > 140 and r < 140:
        return "squad"
    return "unknown"


def association_stats(raw, units, own_id, tick_lo, sec, off, sim, radius=2000.0):
    groups = {}
    for c in raw.get("contacts") or []:
        key = round(c[0] * 2.0) / 2.0
        groups.setdefault(key, []).append((c[4], c[5], c[3]))
    arrs = []
    for u in units:
        if u.get("entityId") == own_id:
            continue
        a = ent_array(u)
        if a is None or a.shape[0] < 2:
            continue
        arrs.append((u, (a[:, 0] - tick_lo) * sec, a))
    matched = total = 0
    frames = frames_hit = 0
    n_contacts = []
    n_units = []
    votes = {}
    for rel in sorted(groups):
        g = groups[rel]
        if not g:
            continue
        cs = np.asarray([(p[0], p[1]) for p in g], dtype=float)
        cols = [p[2] for p in g]
        tq = rel - off
        frames += 1
        n_contacts.append(cs.shape[0])
        hit = 0
        live = 0
        for u, t_rep, a in arrs:
            if tq < t_rep[0] or tq > t_rep[-1]:
                continue
            p = np.array([np.interp(tq, t_rep, a[:, 1]), np.interp(tq, t_rep, a[:, 2])])
            w = sim["scale"] * (sim["R"] @ p) + sim["t"]
            d = np.hypot(cs[:, 0] - w[0], cs[:, 1] - w[1])
            total += 1
            live += 1
            j = int(np.argmin(d))
            if d[j] <= radius:
                matched += 1
                hit += 1
                fam = color_family(cols[j]) if j < len(cols) else "unknown"
                votes.setdefault(u.get("key") or u.get("entityId"), {}).setdefault(fam, 0)
                votes[u.get("key") or u.get("entityId")][fam] += 1
        frames_hit += 1 if hit else 0
        n_units.append(live)
    rate = (matched / total) if total else 0.0
    return {"rate": rate, "matched": matched, "total": total, "frames": frames,
            "frames_hit": frames_hit,
            "mean_contacts": float(np.mean(n_contacts)) if n_contacts else 0.0,
            "mean_units_alive": float(np.mean(n_units)) if n_units else 0.0,
            "radius": radius, "votes": votes}


def envelope_stats(arrs):
    viol = 0
    n_steps = 0
    max_v = 0.0
    tele = 0
    worst = None
    offenders = []
    for name, t, x, y, z in arrs:
        dt = np.diff(t)
        if dt.size == 0:
            continue
        d = np.sqrt(np.diff(x) ** 2 + np.diff(y) ** 2 + np.diff(z) ** 2)
        good = dt > 1e-9
        v = d[good] / dt[good]
        tc = t[1:][good]
        dg = d[good]
        dtg = dt[good]
        fin = np.isfinite(v) & np.isfinite(tc) & np.isfinite(dg)
        n_steps += int(fin.sum())
        if int(fin.sum()):
            vv = v[fin]
            tcf = tc[fin]
            dgf = dg[fin]
            dtf = dtg[fin]
            idx = np.where(vv > 600)[0]
            viol += int(idx.size)
            m = float(np.nanmax(vv))
            if m > max_v:
                max_v = m
                worst = name
            tele += int((dgf > 5000).sum())
            for k in idx:
                offenders.append((float(vv[k]), name, float(tcf[k]), float(dtf[k]), float(dgf[k])))
    offenders.sort(reverse=True)
    return {"violations": viol, "max_speed": max_v, "steps": n_steps,
            "teleports": tele, "worst": worst, "offenders": offenders[:8]}


def check_tracks(cfg, md, raw, tracks, mapping, results):
    if not tracks:
        for rid, ttl in (("4b.units-assoc", "他机与 8111 平面接触的关联率（独立复算）"),
                         ("4c.own-accuracy", "回放本机航迹精度（独立复算）"),
                         ("4d.speed-envelope", "速度包线自检（独立复算）"),
                         ("4e.devvalidation", "与作者 devValidation 数字对照")):
            add(results, rid, ttl, SKIP, "tracks.json 不存在")
        return None
    meta = md.get("meta") or {}
    if mapping is None or not mapping.get("ok"):
        add(results, "4c.own-accuracy", "回放本机航迹精度（独立复算）", SKIP,
            "tick→抓取时间映射没拟合出来，无法与 8111 真值对照")
        return None
    sec = float(mapping["sec_per_tick"])
    off = float(mapping["offset_s"])
    tick_lo = float(mapping["tick_lo"])
    units = tracks.get("units") or []
    pid = tracks.get("playerEntityId")
    own_u = None
    how = ""
    if pid is not None:
        for u in units:
            if u.get("entityId") == pid:
                own_u, how = u, "tracks.playerEntityId=%s" % pid
                break
    if own_u is None:
        for u in units:
            if u.get("entityId") == mapping.get("entity"):
                own_u, how = u, "剖面相关选出的实体 %s（playerEntityId 缺失或对不上）" % mapping.get("entity")
                break
    if own_u is None:
        add(results, "4c.own-accuracy", "回放本机航迹精度（独立复算）", SKIP, "找不到本机实体")
        return None
    if pid is not None and mapping.get("entity") != pid:
        add(results, "4a2.entity-pick", "本机实体判定交叉核对", WARN,
            "playerEntityId=%s，但剖面相关选出的最佳实体是 %s" % (pid, mapping.get("entity")),
            ["两者不一致时以 playerEntityId 为准做误差复算，但这条不一致本身需要解释"])

    # own 可能被"按 600 m/s 断轨"切成多段（devValidation.ownSegmentIds），逐段取样本再汇总
    dv_tr0 = tracks.get("devValidation") or {}
    seg_ids = dv_tr0.get("ownSegmentIds")
    own_units = []
    if isinstance(seg_ids, list) and seg_ids:
        for sid in seg_ids:
            for u2 in units:
                if u2.get("entityId") == sid:
                    own_units.append(u2)
    if not own_units:
        own_units = [own_u]
    pxy = raw.get("player_xy") or {}
    ref_rel = np.asarray(pxy.get("rel") or [], dtype=float)
    rx = np.asarray(pxy.get("x") or [], dtype=float)
    ry = np.asarray(pxy.get("y") or [], dtype=float)
    ow = raw.get("own") or {}
    own_rel = np.asarray(ow.get("rel") or [], dtype=float)
    zraw = np.asarray([v if v is not None else np.nan for v in (ow.get("z_m") or [])], dtype=float)
    def _pool(off_use):
        qs, Qs, Ps, Pzs, Qzs = [], [], [], [], []
        for u2 in own_units:
            a2 = ent_array(u2)
            if a2 is None or a2.shape[0] < 3:
                continue
            t2 = (a2[:, 0] - tick_lo) * sec
            q2 = ref_rel - off_use
            ok2 = (q2 >= t2[0]) & (q2 <= t2[-1]) & np.isfinite(rx) & np.isfinite(ry)
            if int(ok2.sum()) < 3:
                continue
            q2 = q2[ok2]
            qs.append(q2)
            Qs.append(np.column_stack([rx[ok2], ry[ok2]]))
            Ps.append(np.column_stack([np.interp(q2, t2, a2[:, 1]), np.interp(q2, t2, a2[:, 2])]))
            Pzs.append(np.interp(q2, t2, a2[:, 3]))
            Qzs.append(np.interp(q2 + off_use, own_rel, zraw) if own_rel.size
                       else np.full(q2.shape, np.nan))
        if not qs:
            return None
        return (np.concatenate(qs), np.vstack(Qs), np.vstack(Ps),
                np.concatenate(Pzs), np.concatenate(Qzs))

    pooled = _pool(off)
    if pooled is None:
        add(results, "4c.own-accuracy", "回放本机航迹精度（独立复算）", SKIP, "own 段与 8111 没有重叠样本")
        return None
    q, Q, P, Pz, Qz = pooled

    def _rms_at(off_use):
        pl = _pool(off_use)
        if pl is None:
            return None
        s0 = procrustes_fit(pl[2], pl[1], allow_reflection=True)
        if s0 is None:
            return None
        d0 = np.hypot(*(apply_sim(s0, pl[2]) - pl[1]).T)
        return float(np.sqrt(np.mean(d0 ** 2)))

    off_refined = float(off)
    r_base = _rms_at(off)
    best_r = r_base
    for d in np.arange(-0.12, 0.1201, 0.01):
        r = _rms_at(float(off) + float(d))
        if r is not None and best_r is not None and r < best_r - 1e-9:
            best_r, off_refined = r, float(off) + float(d)
    if off_refined != float(off):
        pooled = _pool(off_refined)
        q, Q, P, Pz, Qz = pooled
    Pz_shift = off_refined - float(off)
    if q.size < 10:
        add(results, "4c.own-accuracy", "回放本机航迹精度（独立复算）", SKIP,
            "重叠样本只有 %d 个" % q.size)
        return None

    sim = procrustes_fit(P, Q, allow_reflection=True)
    sim2 = procrustes_fit(P, Q, allow_reflection=False)
    def rms(d):
        return float(np.sqrt(np.mean(d ** 2)))
    d_sim = np.hypot(*(apply_sim(sim, P) - Q).T)
    d_rot = np.hypot(*(apply_sim(sim2, P) - Q).T) if sim2 else d_sim
    tt = Q.mean(0) - P.mean(0)
    d_tr = np.hypot(*(P + tt - Q).T)
    d_id = np.hypot(*(P - Q).T)
    dz = float(np.nanmean(Qz - Pz)) if np.isfinite(Qz).any() and np.isfinite(Pz).any() else 0.0
    d3 = np.sqrt(d_sim ** 2 + (Pz + dz - Qz) ** 2)
    acc = {"n": int(q.size), "sim": (float(d_sim.max()), rms(d_sim)),
           "p99": float(np.percentile(d_sim, 99)),
           "rot_only": (float(d_rot.max()), rms(d_rot)),
           "trans_only": (float(d_tr.max()), rms(d_tr)),
           "identity": (float(d_id.max()), rms(d_id)),
           "sim3d": (float(np.nanmax(d3)), float(np.nanmean(d3 ** 2) ** 0.5)),
           "alt": (float(np.nanmax(np.abs(Pz + dz - Qz))),
                   float(np.sqrt(np.nanmean((Pz + dz - Qz) ** 2)))),
           "p50": float(np.percentile(d_sim, 50)),
           "p90": float(np.percentile(d_sim, 90)),
           "n_over50": int((d_sim > 50).sum()),
           "max_excl_over50": (float(d_sim[d_sim <= 50.0].max())
                               if bool((d_sim <= 50.0).any()) else None),
           "scale": sim["scale"] if sim else None, "det": sim["det"] if sim else None,
           "dz": dz,
           "rot_deg": math.degrees(math.atan2(sim["R"][1, 0], sim["R"][0, 0])) if sim else None,
           "entity": own_u.get("entityId"), "how": how}
    ev4 = ["本机实体：%s（%s；ownSegmentIds=%s 共 %d 段），样本 %d"
           % (acc["entity"], how, json.dumps(seg_ids, ensure_ascii=False) if seg_ids else "无",
              len(own_units), acc["n"]),
           "offset：4a 映射 %.2f s → 按平面残差微调后 %.2f s（±0.12 s 内选最小 rms；"
           "与作者自检同为'最优对齐'口径）" % (off, off_refined),
           "平移+旋转+缩放拟合后：max=%.1f m p99=%.1f m rms=%.1f m（scale=%.6f det=%+.3f 旋转=%.2f°）"
           % (acc["sim"][0], acc["p99"], acc["sim"][1], acc["scale"] or 1.0,
              acc["det"] or 1.0, acc["rot_deg"] or 0.0),
           "仅平移：max=%.1f m rms=%.1f m；仅旋转：max=%.1f m rms=%.1f m；不做任何拟合：max=%.1f m rms=%.1f m"
           % (acc["trans_only"][0], acc["trans_only"][1], acc["rot_only"][0], acc["rot_only"][1],
              acc["identity"][0], acc["identity"][1]),
           "三维（计入 z，z 平移 %.1f m 后）：max=%.1f m rms=%.1f m" % (dz, acc["sim3d"][0], acc["sim3d"][1]),
           "误差分布：p50=%.2f m p90=%.2f m p99=%.2f m max=%.2f m；>50 m 的样本 %d 个；"
           "剔除 >50 m 后的 max=%.2f m"
           % (acc["p50"], acc["p90"], acc["p99"], acc["sim"][0], acc["n_over50"],
              acc["max_excl_over50"] if acc["max_excl_over50"] is not None else float("nan")),
           "时间映射来自 4a：offset=%.2f s rate=%.1f" % (off, mapping["rate"]),
           "被验文件指纹：%s / %s" % (file_fingerprint(cfg.get("tracks_path") or ""),
                                    file_fingerprint(cfg.get("mission_path") or ""))]
    m1, r1 = acc["sim"]
    p99 = acc["p99"]
    # 判定口径（lead 裁定）：rms≤15 m 且 p99≤50 m 视为达标；max 单独列出并注明离群
    if r1 <= 15.0 and p99 <= 50.0:
        st = PASS
        det = ("达标：rms=%.1f m ≤15、p99=%.1f m ≤50；单点 max=%.1f m >50 的标称目标，属离群点"
               "（作者自报 max=130.3 m，同样只有离群点超标）" % (r1, p99, m1))
    elif r1 <= 15.0:
        st = WARN
        det = "rms 达标（%.1f m）但 p99=%.1f m 超过 50 m；max=%.1f m" % (r1, p99, m1)
    elif r1 <= 60.0:
        st = WARN
        det = "本机航迹略超目标（max=%.1f m，p99=%.1f m，rms=%.1f m；目标 rms≤15、p99≤50）" % (m1, p99, r1)
    else:
        st = FAIL
        det = "本机航迹与 8111 真值对不上（max=%.1f m，rms=%.1f m；目标 rms≤15、p99≤50）" % (m1, r1)
    add(results, "4c.own-accuracy", "回放本机航迹精度（独立复算）", st, det, ev4)

    # 4b 关联率
    assoc = association_stats(raw, units, own_u.get("entityId"), tick_lo, sec, off, sim)
    ev4b = ["关联半径 %.0f m；%d 个实体时刻中 %d 个在 8111 接触点附近 → 关联率 %.1f%%"
            % (assoc["radius"], assoc["total"], assoc["matched"], assoc["rate"] * 100),
            "有命中的接触帧 %d/%d；每帧平均 8111 接触 %.1f 个，同时存活解码实体 %.1f 个"
            % (assoc["frames_hit"], assoc["frames"], assoc["mean_contacts"], assoc["mean_units_alive"])]
    add(results, "4b.units-assoc", "他机与 8111 平面接触的关联率（独立复算）",
        PASS if assoc["rate"] >= 0.5 else (WARN if assoc["rate"] >= 0.2 else FAIL),
        "关联率 %.1f%%（此值取决于 8111 何时可见敌机，只作数量级核对）" % (assoc["rate"] * 100), ev4b)

    # 4g：side 是否被猜补
    # tracks.json 的 units 没有 side 字段（None），交付产物的 side 在 mission-data.js 里；
    # 按 entityId 把两者对起来，再用 8111 接触点颜色族做交叉核对（as-is 与取反两种极性都算）。
    md_side = {}
    md_side_counts = {}
    for u in (md.get("units") or []):
        if isinstance(u, dict):
            s = u.get("side")
            md_side_counts[s] = md_side_counts.get(s, 0) + 1
            if u.get("entityId"):
                md_side[eid_key(u.get("entityId"))] = s
    enum_bad = [s for s in md_side_counts if s not in ("ally", "enemy", "squad", "unknown")]
    votes = assoc.get("votes") or {}
    pairs = []
    undeclared = 0
    for u in units:
        key = u.get("key") or u.get("entityId")
        v = votes.get(key)
        declared = md_side.get(eid_key(u.get("entityId"))) or u.get("side")
        if not v:
            continue
        fam, cnt = max(v.items(), key=lambda kv: kv[1])
        if declared in (None, "unknown") or fam in ("unknown", "self"):
            undeclared += 1
            continue
        pairs.append((key, declared, fam, cnt, v))
    agree_as_is = sum(1 for p in pairs if p[1] == p[2])
    inv = {"ally": "enemy", "enemy": "ally", "squad": "squad"}
    agree_inv = sum(1 for p in pairs if inv.get(p[1]) == p[2])
    n = len(pairs)
    ev4g = ["交付 units 的 side 分布：%s（共 %d 条）" % (json.dumps(md_side_counts, ensure_ascii=False),
                                                    sum(md_side_counts.values())),
            "枚举检查：越界值 %s" % (enum_bad or "无"),
            "可交叉核对（有颜色票且已声明侧别）%d 个实体、未声明/无法判 %d 个" % (n, undeclared),
            "as-is 极性一致 %d/%d（%.0f%%）；取反极性一致 %d/%d（%.0f%%）"
            % (agree_as_is, n, 100.0 * agree_as_is / max(1, n), agree_inv, n,
               100.0 * agree_inv / max(1, n))]
    ev4g += ["%s：声明 %s，8111 颜色族 %s（票 %s）" % (p[0], p[1], p[2], json.dumps(p[4], ensure_ascii=False))
             for p in pairs[:6]]
    best = max(agree_as_is, agree_inv) / max(1, n)
    if enum_bad:
        add(results, "4g.side-vs-8111", "units 的 side 未被猜补（与 8111 颜色交叉核对）", FAIL,
            "side 出现契约外取值：%s" % enum_bad, ev4g)
    elif n == 0:
        add(results, "4g.side-vs-8111", "units 的 side 未被猜补（与 8111 颜色交叉核对）", SKIP,
            "没有可交叉核对的实体（未声明 %d 个）" % undeclared, ev4g)
    elif best < 0.7:
        add(results, "4g.side-vs-8111", "units 的 side 未被猜补（与 8111 颜色交叉核对）", INFO,
            "8111 颜色族无法判定阵营方向（两种极性最高只有 %.0f%% 一致），本项**无法独立验证 side 正确性**；"
            "只确认：枚举合法、%d/%d 未用 8111 颜色猜补（unknown/未声明）"
            % (best * 100, undeclared, undeclared + n), ev4g)
    else:
        add(results, "4g.side-vs-8111", "units 的 side 未被猜补（与 8111 颜色交叉核对）", PASS,
            "按%s极性一致率 %.0f%%（n=%d）；未声明 %d 个" % ("as-is" if agree_as_is >= agree_inv else "取反",
                                                        best * 100, n, undeclared), ev4g)

    # 4d 速度包线
    arrs = []
    for u in units:
        aa = ent_array(u)
        if aa is None or aa.shape[0] < 2:
            continue
        tt2 = (aa[:, 0] - tick_lo) * sec
        arrs.append((u.get("entityId"), tt2, aa[:, 1], aa[:, 2], aa[:, 3]))
    env = envelope_stats(arrs)
    off_lines = ["%s t=%.1f s dt=%.3f s 位移=%.0f m → %.0f m/s"
                 % (o[1], o[2], o[3], o[4], o[0]) for o in env.get("offenders", [])[:8]]
    # 4d：交付产物（mission-data.js 里 ship 出去的 units + own）本身有没有瞬移
    arrs_md = []
    for u in (md.get("units") or []):
        if not isinstance(u, dict):
            continue
        n_u = int(u.get("n") or 0)
        hz_u = float(u.get("hz") or 0)
        if n_u < 2 or hz_u <= 0:
            continue
        xs_u, ys_u, zs_u = _colf(u, "x"), _colf(u, "y"), _colf(u, "z")
        if xs_u is None or ys_u is None:
            continue
        tt_u = float(u.get("t0") or 0.0) + np.arange(n_u) / hz_u
        zz_u = zs_u if zs_u is not None else np.zeros(n_u)
        arrs_md.append((u.get("key"), tt_u, xs_u, ys_u, zz_u))
    own_md = md.get("own") or {}
    if int(own_md.get("n") or 0) > 2:
        ox, oy, oz = _colf(own_md, "x"), _colf(own_md, "y"), _colf(own_md, "z")
        if ox is not None and oy is not None:
            tt_o = float(own_md.get("t0") or 0.0) + np.arange(int(own_md["n"])) / float(own_md.get("hz") or 1)
            arrs_md.append(("own",
                            tt_o, ox, oy, oz if oz is not None else np.zeros(len(ox))))
    env_md = envelope_stats(arrs_md) if arrs_md else None
    if env_md is None:
        add(results, "4d.speed-envelope", "交付产物速度包线（无 >600 m/s、无瞬移）", SKIP,
            "mission-data.js 里没有可算的 units/own")
    else:
        off_md = ["%s t=%.1f s dt=%.3f s 位移=%.0f m → %.0f m/s"
                  % (o[1], o[2], o[3], o[4], o[0]) for o in env_md.get("offenders", [])[:8]]
        add(results, "4d.speed-envelope", "交付产物速度包线（无 >600 m/s、无瞬移）",
            PASS if (env_md["violations"] == 0 and env_md["teleports"] == 0) else FAIL,
            "交付 %d 条航迹共 %d 步：超 600 m/s %d 步，单步 >5000 m %d 处；最大 %.0f m/s（%s）"
            % (len(arrs_md), env_md["steps"], env_md["violations"], env_md["teleports"],
               env_md["max_speed"], env_md["worst"]),
            ["判定阈值 600 m/s 取自契约 §5.2；违规步明细（前 8）："] + off_md)
    add(results, "4d2.tracks-envelope", "tracks.json 原始样本速度包线（解码产物质量）",
        WARN if env["violations"] else PASS,
        "tracks.json 原始 %d 步：超 600 m/s %d 步（占 %.3f%%），单步 >5000 m %d 处；最大 %.0f m/s（%s）"
        % (env["steps"], env["violations"], 100.0 * env["violations"] / max(1, env["steps"]),
           env["teleports"], env["max_speed"], env["worst"]),
        ["违约步明细（按速度降序，前 8）："] + off_lines
        + ["说明：交付产物已按接缝断开，故 4d 只看交付产物；这条记录解码原始样本里的跳变，供 wrpl-probe 追因"])

    # 4e 与作者 devValidation 对照
    dv1 = tracks.get("devValidation") or {}
    dv2 = meta.get("devValidation") or {}
    cmp_lines = []
    verdicts = []
    def cmp_num(label, mine, theirs, tol_abs, tol_rel, unit=""):
        """数量级对比：绝对值受采样口径影响，故主判"比值"是否落在同一量级。
        返回 True/False/None（None=不判定）。"""
        if theirs is None:
            cmp_lines.append("%s：作者未给，复算 %s" % (label, mine))
            return None
        if mine is None or not isinstance(theirs, (int, float)):
            cmp_lines.append("%s：无法比较（mine=%s theirs=%s）" % (label, mine, theirs))
            return None
        if float(theirs) == 0.0 and float(mine) == 0.0:
            cmp_lines.append("%s：两者都是 0 → 一致" % label)
            return True
        ratio = (float(mine) / float(theirs)) if float(theirs) != 0 else float("inf")
        r2 = min(ratio, 1.0 / ratio) if ratio > 0 else 0.0
        # 复算值优于作者（mine ≤ theirs）永远算"一致"；只有明显更差才提示
        ok = (float(mine) <= float(theirs) * 1.05) or (r2 >= 0.5)
        cmp_lines.append("%s：复算 %s%s vs 作者 %s%s（比值 %.2f）→ %s"
                         % (label, round(float(mine), 2), unit, theirs, unit, ratio,
                            ("复算不差于作者" if float(mine) <= float(theirs) * 1.05
                             else ("同一量级" if ok else "复算明显更差"))))
        return ok
    if dv2:
        v = cmp_num("ownMaxErrM", acc["sim"][0], dv2.get("ownMaxErrM"), 2.0, 0.15, " m")
        v = cmp_num("ownP99ErrM", acc["p99"], dv2.get("ownP99ErrM"), 2.0, 0.15, " m")
        if v is not None:
            verdicts.append(v)
        v = cmp_num("ownRmsErrM", acc["sim"][1], dv2.get("ownRmsErrM"), 2.0, 0.15, " m")
        v = cmp_num("ownAltRmsVs8111M", (acc.get("alt") or (None, None))[1],
                    dv2.get("ownAltRmsVs8111M"), 2.0, 0.15, " m")
        if v is not None:
            verdicts.append(v)
        cmp_num("ownSamplesCompared", acc["n"], dv2.get("ownSamplesCompared"), 2.0, 0.02)
        cmp_lines.append("说明：ownSamplesCompared 口径可能与作者不同（作者可能算自己的航迹点数，"
                         "这里算的是与 8111 真值对得上的样本数），只报告不判定")
        # 作者侧新增的"分口径"字段（tracks.devValidation 优先）——这些才是能逐一核对的
        dv_extra = {}
        for src_name, dvx in (("tracks.devValidation", dv1), ("meta.devValidation", dv2)):
            if isinstance(dvx, dict):
                for k in ("ownP99ErrM", "ownMaxErrMExclSpawn", "ownRmsErrMExclSpawn",
                          "ownSamplesOver50M"):
                    if isinstance(dvx.get(k), (int, float)) and k not in dv_extra:
                        dv_extra[k] = (dvx[k], src_name)
        if "ownP99ErrM" in dv_extra:
            cmp_lines.append("作者分口径：%s" % json.dumps(
                {k: {"值": v[0], "来源": v[1]} for k, v in dv_extra.items()}, ensure_ascii=False))
            v = cmp_num("ownP99ErrM", acc["p99"], dv_extra["ownP99ErrM"][0], 2.0, 0.15, " m")
            if v is not None:
                verdicts.append(v)
            if "ownSamplesOver50M" in dv_extra:
                v = cmp_num("ownSamplesOver50M", acc["n_over50"],
                            dv_extra["ownSamplesOver50M"][0], 2.0, 0.0)
                if v is not None:
                    verdicts.append(v)
            if acc.get("max_excl_over50") is not None:
                base = dv_extra.get("ownMaxErrMExclSpawn")
                if base is not None:
                    # 口径不同，不判定：作者是"剔除出生/重生过渡样本后的 max"（16.7 m），
                    # 我的是"我这边 >50 m 的样本为 0 时的 max"（35.0 m），两者不是同一集合。
                    cmp_lines.append(
                        "ownMaxErrMExclSpawn（作者 %.2f m，剔除出生/重生过渡样本）vs 我的 max %.2f m"
                        "（我这边 >50 m 的样本 %d 个）→ 口径不同（采样点集合不同），只报告不判定"
                        % (base[0], acc["sim"][0], acc["n_over50"]))
                base2 = dv_extra.get("ownRmsErrMExclSpawn")
                if base2 is not None:
                    v = cmp_num("ownRmsErrMExclSpawn（我：整段 rms）",
                                acc["sim"][1], base2[0], 2.0, 0.25, " m")
                    if v is not None:
                        verdicts.append(v)
        v = cmp_num("unitAssocRate", assoc["rate"], dv2.get("unitAssocRate"), 0.05, 0.15)
        if v is not None:
            verdicts.append(v)
        v = cmp_num("speedViolations", env["violations"], dv2.get("speedViolations"), 0.5, 0.0)
        if v is not None:
            verdicts.append(v)
    else:
        cmp_lines.append("mission-data.js 的 meta.devValidation 缺失")
    if dv1:
        cmp_lines.append("tracks.devValidation：%s" % json.dumps(dv1, ensure_ascii=False))
        for lab, mine, key in (("ownMaxErrM", acc["sim"][0], "ownMaxErrM"),
                               ("ownRmsErrM", acc["sim"][1], "ownRmsErrM"),
                               ("unitAssocRate", assoc["rate"], "unitAssocRate"),
                               ("speedViolations", env["violations"], "speedViolations")):
            if key in dv1 and isinstance(dv1[key], (int, float)):
                cmp_lines.append("  tracks.%s：作者 %s，复算 %s" % (key, dv1[key], round(float(mine), 2)))
    else:
        cmp_lines.append("tracks.json 没有 devValidation")
    cmp_lines.append("本机实体：%s（%s）" % (acc["entity"], how))
    if not verdicts:
        add(results, "4e.devvalidation", "与作者 devValidation 数字对照", WARN,
            "没有可对照的作者数字", cmp_lines + ["采样口径说明：作者用其 4 Hz 航迹点对插值后的 8111 位置，"
                                                 "我用 8111 平面位置帧（5 Hz）对插值后的回放航迹，样本数与离群点覆盖不同"])
    elif all(verdicts):
        add(results, "4e.devvalidation", "与作者 devValidation 数字对照", PASS,
            "%d/%d 项同量级（作者数字可复现）" % (sum(verdicts), len(verdicts)), cmp_lines)
    else:
        add(results, "4e.devvalidation", "与作者 devValidation 数字对照", FAIL,
            "%d/%d 项与作者数字差得远（>2×）" % (sum(1 for v in verdicts if not v), len(verdicts)),
            cmp_lines)
    return acc


# ---------------------------------------------------------------- 检查 5 / 6：不编造性与回放独立性

def find_numeric_arrays(obj, path=""):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += find_numeric_arrays(v, "%s.%s" % (path, k))
    elif isinstance(obj, list):
        nums = [x for x in obj if isinstance(x, (int, float)) and not isinstance(x, bool)]
        if len(obj) >= 8 and len(nums) == len(obj):
            out.append((path or "$", len(obj)))
        else:
            for i, v in enumerate(obj):
                out += find_numeric_arrays(v, "%s[%d]" % (path, i))
    return out


def check_nonfabrication(cfg, md, raw, results, texts):
    meta = md.get("meta") or {}
    units = md.get("units") or []
    # 5a：units 里不能凭空出现高度
    bad = []
    no_alt = 0
    for i, u in enumerate(units):
        if not isinstance(u, dict):
            continue
        for k in u:
            if k != "hasAlt" and "alt" in str(k).lower():
                bad.append("units[%d].%s" % (i, k))
        if u.get("hasAlt") is False:
            z = u.get("z")
            if isinstance(z, list) and any(x is not None for x in z):
                bad.append("units[%d] hasAlt=false 却有 z 数值" % i)
            no_alt += 1
    if not units:
        add(results, "5a.units-alt", "units 不含凭空高度（hasAlt 与 z 自洽）", SKIP,
            "units 为空数组（几何未落地），无法判定；等 tracks 合并后再跑")
    elif bad:
        add(results, "5a.units-alt", "units 不含凭空高度（hasAlt 与 z 自洽）", FAIL,
            "发现 %d 处：%s" % (len(bad), bad[:8]))
    else:
        add(results, "5a.units-alt", "units 不含凭空高度（hasAlt 与 z 自洽）", PASS,
            "没有 height 字段混入；hasAlt=false 的 %d 个实体 z 均为空" % no_alt)

    # 5b：devValidation 不带坐标数组
    dv = meta.get("devValidation")
    if dv is None:
        add(results, "5b.devval-coords", "meta.devValidation 只有数字、不带坐标", FAIL,
            "meta.devValidation 缺失")
    else:
        arrs = find_numeric_arrays(dv, "devValidation")
        add(results, "5b.devval-coords", "meta.devValidation 只有数字、不带坐标",
            FAIL if arrs else PASS,
            ("发现坐标型数组：%s" % arrs[:6]) if arrs else
            "键：%s" % json.dumps(sorted(dv.keys()), ensure_ascii=False))

    # 5c：provenance / limits
    prov = meta.get("provenance")
    lim = meta.get("limits")
    okc = isinstance(prov, list) and len(prov) > 0 and isinstance(lim, list) and len(lim) > 0
    add(results, "5c.provenance", "provenance / limits 交代来源与局限",
        PASS if okc else FAIL,
        "provenance %d 条，limits %d 条" % (len(prov) if isinstance(prov, list) else -1,
                                            len(lim) if isinstance(lim, list) else -1),
        ["provenance: %s" % json.dumps(prov, ensure_ascii=False)[:300],
         "limits: %s" % json.dumps(lim, ensure_ascii=False)[:300]])

    # 5d：未知项显示 --
    has_dash = any("--" in (texts.get(k) or "") for k in ("mission.js", "scene3d.js"))
    add(results, "5d.unknown-dash", "界面未知项显示 `--`",
        PASS if has_dash else FAIL,
        "mission.js / scene3d.js 里%s `--` 字面量"
        % ("有" if has_dash else "没有"),
        ["静态检查，运行时是否真的用 `--` 顶替 null 需要 task-7 浏览器实测"])

    # 5e：角落说明交代来源与局限
    mjs = texts.get("mission.js") or ""
    src_ok = ("source" in mjs or "来源" in mjs)
    lim_ok = ("limits" in mjs or "局限" in mjs)
    add(results, "5e.corner-source", "角落/帮助说明交代来源与局限",
        PASS if (src_ok and lim_ok) else WARN,
        "mission.js 提到来源：%s，提到局限：%s" % (src_ok, lim_ok),
        ["scene3d.js 里也可能承担这段文案"])

    # 5f：composed 标注是否完整（回放原文 vs 拼合）
    evs = md.get("events") or []
    n_true = sum(1 for e in evs if e.get("composed") is True)
    n_false = sum(1 for e in evs if e.get("composed") is False)
    n_other = len(evs) - n_true - n_false
    hud_texts = {clean_msg(h.get("msg")) for h in (raw.get("hudmsg") or [])}
    verbatim = sum(1 for e in evs if clean_msg(str(e.get("text") or "")) in hud_texts)
    claims_original = [e for e in evs if e.get("composed") is False
                       and clean_msg(str(e.get("text") or "")) not in hud_texts]
    ev5f = ["events %d 条：composed=true %d，composed=false %d，非布尔 %d"
            % (len(evs), n_true, n_false, n_other),
            "文本与 8111 hudmsg 逐字相同的 %d 条（composed=true 的若逐字相同，说明其实是原文）"
            % verbatim,
            "标注 composed=false 但在 hudmsg 里找不到同文本的 %d 条（回放原文未必进过 8111 流水，"
            "不一定是错，只提示）" % len(claims_original)]
    # 5g：own 各通道的 null / 0 值统计（不得用 0 顶替未知）
    own = md.get("own") or {}
    chans = ("x", "y", "z", "ias", "mach", "hdg", "roll", "pitch", "g", "aoa", "ammo", "fuel")
    stats = []
    suspicious = []
    lim_text = " ".join(str(x) for x in (meta.get("limits") or []))
    for c in chans:
        v = own.get(c)
        if not isinstance(v, list) or not v:
            stats.append("%s：空/缺失" % c)
            continue
        nn = sum(1 for x in v if x is None)
        zz = sum(1 for x in v if isinstance(x, (int, float)) and float(x) == 0.0)
        uniq = len({x for x in v if x is not None})
        stats.append("%s：n=%d null=%d 0值=%d 不同值=%d" % (c, len(v), nn, zz, uniq))
        if nn == 0 and zz == len(v):
            suspicious.append("%s 整列都是 0（既无 null 也无变化）：疑似用 0 顶替未知" % c)
        elif nn == 0 and uniq <= 2 and c in ("ias", "mach", "g", "aoa", "roll", "pitch"):
            suspicious.append("%s 只有 %d 个不同值且无 null：疑似常数填充" % (c, uniq))
    if suspicious:
        add(results, "5g.missing-values", "缺值用 null/`--` 而不是 0 或估算值", WARN,
            "；".join(suspicious[:4]) + ("；limits 里提到相关字段：%s" % lim_text[:120] if lim_text else ""),
            stats)
    else:
        add(results, "5g.missing-values", "缺值用 null/`--` 而不是 0 或估算值", PASS,
            "own 通道没有整列常数/整列 0 的填充痕迹", stats)

    if not evs:
        add(results, "5f.events-composed", "composed 标注完整、拼合事件有标记", SKIP, "没有 events")
    elif n_other > 0:
        add(results, "5f.events-composed", "composed 标注完整、拼合事件有标记", FAIL,
            "有 %d 条事件没有布尔 composed 标注" % n_other, ev5f)
    else:
        add(results, "5f.events-composed", "composed 标注完整、拼合事件有标记", PASS,
            "全部 %d 条都有布尔 composed（true %d / false %d）" % (len(evs), n_true, n_false), ev5f)


def check_replay_independence(cfg, md, results, texts):
    meta = md.get("meta") or {}
    # 6a：生成脚本不读 8111
    b = texts.get("build")
    if b is None:
        add(results, "6a.build-no-8111", "build_ace_mission.py 不读 8111", SKIP,
            "找不到 %s" % cfg.get("build_script"))
    else:
        hits = [line.strip() for line in b.splitlines()
                if "rb-session" in line or "data/raw" in line or "data\\raw" in line]
        add(results, "6a.build-no-8111", "build_ace_mission.py 不读 8111",
            FAIL if hits else PASS,
            ("命中 %d 行：%s" % (len(hits), hits[:6])) if hits else "没有 rb-session / data/raw 字样",
            ["grep 目标：%s" % cfg.get("build_script")])

    # 6b：meta.source
    add(results, "6b.source", "meta.source == \"wrpl\"",
        PASS if meta.get("source") == "wrpl" else FAIL,
        "meta.source=%r" % meta.get("source"))

    # 6c：每个 units 的 z 存在且非常数
    units = md.get("units") or []
    air_const = []
    ground_const = []
    missing = []
    for u in units:
        if not isinstance(u, dict):
            continue
        z = u.get("z")
        vals = [x for x in z if isinstance(x, (int, float))] if isinstance(z, list) else []
        if not vals:
            missing.append(u.get("key"))
            continue
        rngv = max(vals) - min(vals)
        if rngv < 5.0:
            (air_const if u.get("domain") == "air" else ground_const).append(
                (u.get("key"), round(rngv, 2)))
    okz = (not missing) and (not air_const)
    detail = ("缺失 z 的实体 %d 个 %s；空中实体 z 近似常数 %d 个 %s；地面实体 z 常数 %d 个（地面可以）"
              % (len(missing), missing[:5], len(air_const), air_const[:5], len(ground_const)))
    if not units:
        add(results, "6c.units-z", "每个 units 的 z 存在且空中实体 z 非常数（证明来自回放）", SKIP,
            "units 为空数组（几何未落地），无法判定；这是 v5 §5.1 的硬检查，几何到位后必须 PASS")
    else:
        add(results, "6c.units-z", "每个 units 的 z 存在且空中实体 z 非常数（证明来自回放）",
            PASS if okz else FAIL, detail,
            ["8111 没有他机高度，z 有真实起伏是来自回放的强证据；容差 5 m"])

    # 6d/6e：index.html 静态检查
    h = texts.get("index.html")
    if h is None:
        add(results, "6d.index-no-dev", "index.html 不引用 dev/**", SKIP, "找不到 index.html")
        add(results, "6e.index-static", "index.html 无外部资源/无 ES module", SKIP, "找不到 index.html")
        return
    devrefs = re.findall(r'["\'][^"\']*dev/[^"\']*["\']', h)
    add(results, "6d.index-no-dev", "index.html 不引用 dev/**",
        FAIL if devrefs else PASS,
        ("引用了：%s" % devrefs[:6]) if devrefs else "没有 dev/ 引用")

    ext = re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)["\']', h)
    ext_remote = [u for u in ext if re.match(r'(?i)^(https?:)?//', u)]
    module = bool(re.search(r'type\s*=\s*["\']module["\']', h))
    net = [k for k in ("fetch(", "XMLHttpRequest", "importScripts", "WebSocket") if k in h]
    order = [u for u in ext if u.endswith(".js")]
    want_order = ["mission-data.js", "scene3d.js", "mission.js"]
    order_ok = [u for u in order if u in want_order] == want_order
    bad = list(ext_remote) + (["type=module"] if module else []) + net + ([] if order_ok else ["脚本顺序 %s" % order])
    add(results, "6e.index-static", "index.html 无外部资源/无 ES module/脚本顺序正确",
        FAIL if bad else PASS,
        "外部资源 %s；module=%s；网络调用 %s；脚本顺序 %s（期望 %s）"
        % (ext_remote, module, net, order, want_order),
        ["静态检查，实际 0 请求由 task-7 浏览器实测确认"])


# ---------------------------------------------------------------- 汇总

def make_cfg(args):
    # 工具位于 <repo>/ace-combat/tools/：ROOT=ace-combat，repo=仓库根。
    # 沙箱下只有根目录与 ace-combat/** 可写，所以中间产物一律落到 ace-combat/build/verify/。
    ace = ROOT
    repo = os.path.dirname(ace)
    if os.path.basename(ace) != "ace-combat":
        ace = os.path.join(repo, "ace-combat")
    return {
        "root": repo,
        "ace": ace,
        "raw_dir": os.path.join(repo, "data", "raw", "rb-session"),
        "cache_dir": os.path.join(ace, "build", "verify", "cache"),
        "contact_hz": 2.0,
        "rebuild_cache": bool(getattr(args, "rebuild_cache", False)),
        "mission_path": os.path.join(ace, "mission-data.js"),
        "tracks_path": os.path.join(ace, "build", "wrpl", "tracks.json"),
        "header_path": os.path.join(ace, "build", "wrpl", "header.json"),
        "entities_path": os.path.join(ace, "build", "wrpl", "entities.json"),
        "kill_events_path": os.path.join(ace, "build", "wrpl", "kill_events.json"),
        "ground_path": os.path.join(ace, "build", "wrpl", "ground.json"),
        "projectile_path": os.path.join(ace, "build", "wrpl", "projectile_launches.json"),
        "side_final_path": os.path.join(ace, "build", "wrpl", "side_final.json"),
        "payload_bin": os.path.join(repo, "data", "_scratch", "payload.bin"),
        "build_script": os.path.join(ace, "tools", "build_ace_mission.py"),
        "index_html": os.path.join(ace, "index.html"),
        "mission_js": os.path.join(ace, "mission.js"),
        "scene_js": os.path.join(ace, "scene3d.js"),
        "data_notes": os.path.join(ace, "docs", "DATA-NOTES.md"),
        "out": os.path.join(ace, "build", "verify", "report.json"),
        "md_out": os.path.join(ace, "build", "verify", "report.md"),
        "seed": int(getattr(args, "seed", 20261018)),
        "checks": getattr(args, "checks", None),
    }


def run_all(cfg, results, rng):
    raw = load_raw(cfg, results)
    if raw:
        try:
            raw_facts(cfg, raw, results)
        except Exception as exc:
            add(results, "0.raw-facts", "8111 原始真值基线（独立复算）", WARN,
                "计算异常：%s" % exc, [traceback.format_exc()[-500:]])
    mission = None
    if os.path.exists(cfg["mission_path"]):
        try:
            mission, _text = parse_js_global(cfg["mission_path"])
            cfg["_mission_text"] = _text
            _m = mission.get("meta") or {}
            add(results, "0.mission-load", "mission-data.js 可解析", PASS,
                "顶层键：%s" % list(mission.keys()),
                ["文件 %s（%.2f MB）" % (cfg["mission_path"],
                                        os.path.getsize(cfg["mission_path"]) / 1048576.0),
                 "meta.source=%s window=%s" % (_m.get("source"),
                                               json.dumps(_m.get("window"), ensure_ascii=False)),
                 "meta.replayTicks=%s" % json.dumps(_m.get("replayTicks"), ensure_ascii=False),
                 "meta.counts=%s" % json.dumps(_m.get("counts"), ensure_ascii=False),
                 "meta.player=%s" % json.dumps(_m.get("player"), ensure_ascii=False),
                 "meta.devValidation=%s" % json.dumps(_m.get("devValidation"), ensure_ascii=False)[:300],
                 "被验文件指纹：%s" % file_fingerprint(cfg["mission_path"])])
        except Exception as exc:
            add(results, "0.mission-load", "mission-data.js 可解析", FAIL,
                "解析失败：%s" % exc, [traceback.format_exc()[-600:]])
    else:
        add(results, "0.mission-load", "mission-data.js 可解析", SKIP,
            "文件不存在：%s" % cfg["mission_path"])

    tracks = None
    if os.path.exists(cfg["tracks_path"]):
        try:
            tracks = load_tracks(cfg["tracks_path"])
            add(results, "0.tracks-load", "tracks.json 可解析", PASS,
                "units=%d，playerEntityId=%s，secPerTick=%s，tick0=%s"
                % (len(tracks.get("units") or []), tracks.get("playerEntityId"),
                   tracks.get("secPerTick"), tracks.get("tick0")),
                ["devValidation=%s" % json.dumps(tracks.get("devValidation") or {}, ensure_ascii=False)[:300],
                 "结构规范化：%s" % (tracks.get("_normalize_notes") or "无需规范化"),
                 "顶层键：%s" % list(tracks.keys())[:20],
                 "被验文件指纹：%s" % file_fingerprint(cfg["tracks_path"])])
        except Exception as exc:
            add(results, "0.tracks-load", "tracks.json 可解析", FAIL, str(exc))
    else:
        add(results, "0.tracks-load", "tracks.json 可解析", SKIP,
            "文件不存在：%s" % cfg["tracks_path"])

    mapping = None
    if raw and tracks:
        try:
            mapping = fit_time_mapping(tracks, raw, results)
        except Exception as exc:
            add(results, "4a.time-map", "tick→抓取时间映射（独立拟合）", FAIL,
                "拟合异常：%s" % exc, [traceback.format_exc()[-600:]])

    sel = cfg.get("checks")
    def want(prefix):
        return not sel or any(str(c) == prefix or str(c) in prefix for c in sel)

    if mission is not None:
        if want("1"):
            try:
                check_own(cfg, mission, raw, mapping, results, rng)
            except Exception as exc:
                add(results, "1.own-truth", "本机 z/ias/hdg/g 与 8111 真值", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
        if want("2") and raw:
            try:
                check_events(cfg, mission, raw, mapping, results)
            except Exception as exc:
                add(results, "2a.events-vs-kills", "事件数与回放击杀包一致", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
        if want("3"):
            try:
                check_structure(cfg, mission, results)
            except Exception as exc:
                add(results, "3.structure", "结构检查", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
            try:
                check_death(cfg, mission, tracks, results)
            except Exception as exc:
                add(results, "3g.death", "deathTickRaw 与 killPositions 一一对应", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
            try:
                check_units_10hz(cfg, mission, tracks, results)
            except Exception as exc:
                add(results, "7b.units-10hz", "空中段 10 Hz 上采样与 null 断点", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
            try:
                check_display_names(cfg, mission, results, cfg.get("_mission_text"))
            except Exception as exc:
                add(results, "7c.display-names", "型号显示名覆盖与回退泄漏", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
            try:
                check_terrain(cfg, mission, results)
            except Exception as exc:
                add(results, "terrain", "terrain 层：空需交代 / 非空需合规且不串图", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
            try:
                check_runways(cfg, mission, results)
            except Exception as exc:
                add(results, "runways", "runways[]：空数组需有结论，或有线段+自证", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
            try:
                check_shots(cfg, mission, results)
            except Exception as exc:
                add(results, "3f.shots", "shots[] 结构与合并保真（1734 条发射点）", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
            try:
                check_window(cfg, mission, results)
            except Exception as exc:
                add(results, "3b.window", "窗口/回放时钟一致性（独立复算）", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
            try:
                check_ground(cfg, raw, tracks, mapping, results)
            except Exception as exc:
                add(results, "3d.ground-points", "ground.json 静态点位（可选产物）", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
            try:
                check_projectiles(cfg, tracks, results)
            except Exception as exc:
                add(results, "3e.projectile-launches", "弹药发射点独立复算（发射点须贴发射机）", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
            try:
                check_sides_delivered(cfg, mission, raw, tracks, mapping, results)
            except Exception as exc:
                add(results, "7.sides", "阵营消白核对（空中 unknown 应为 0）", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
            try:
                check_ground_delivered(cfg, mission, raw, tracks, mapping, results)
            except Exception as exc:
                add(results, "7.ground", "地面层独立复算（交付文件 vs 8111）", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
            except Exception as exc:
                add(results, "3d.ground-points", "ground.json 静态点位（可选产物）", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
            try:
                check_scope(cfg, mission, tracks, results)
            except Exception as exc:
                add(results, "3c.entity-scope", "交付范围与解码产物一致（实体数/未说明类别）", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
        texts = {"mission.js": read_text(cfg["mission_js"]),
                 "scene3d.js": read_text(cfg["scene_js"]),
                 "index.html": read_text(cfg["index_html"]),
                 "build": read_text(cfg["build_script"]),
                 "data-notes": read_text(cfg["data_notes"])}
        try:
            check_vehicle_category(cfg, mission, results)
        except Exception as exc:
            add(results, "7f.vehicle-category", "单位类别层（236/236 且语义自洽）", FAIL,
                "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
        try:
            check_squad(cfg, mission, tracks, results)
        except Exception as exc:
            add(results, "meta.squad", "meta.squad 与回放内 kill/death 指纹自洽", FAIL,
                "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
        try:
            check_hidden(cfg, mission, results, texts)
        except Exception as exc:
            add(results, "7e.hidden", "hidden 集合（21 条占位/雷达）与其消费", FAIL,
                "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
        if want("5"):
            try:
                check_nonfabrication(cfg, mission, raw, results, texts)
            except Exception as exc:
                add(results, "5a.units-alt", "不编造性检查", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
        if want("6"):
            try:
                check_replay_independence(cfg, mission, results, texts)
            except Exception as exc:
                add(results, "6b.source", "回放独立性检查", FAIL,
                    "检查异常：%s" % exc, [traceback.format_exc()[-600:]])
    if want("4") and raw and tracks:
        try:
            check_tracks(cfg, mission or {}, raw, tracks, mapping, results)
        except Exception as exc:
            add(results, "4c.own-accuracy", "回放本机航迹精度（独立复算）", FAIL,
                "检查异常：%s" % exc, [traceback.format_exc()[-800:]])
    return mission, tracks, mapping


def _fallback_dir(why):
    import tempfile
    d = tempfile.mkdtemp(prefix="ace-verify-")
    print("[提示] 工作区不可写（%s），产物落到临时目录：%s" % (why, d))
    return d


def write_report(cfg, results):
    import datetime
    payload = {"generated": datetime.datetime.now().isoformat(timespec="seconds"),
               "root": cfg["root"], "seed": cfg["seed"],
               "mission_path": cfg["mission_path"], "tracks_path": cfg["tracks_path"],
               "items": results}

    def _dump(out_path, md_path):
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("# tools/check_ace_mission.py 复算结果\n\n")
            f.write("生成时间：%s；随机种子 %s\n\n" % (payload["generated"], cfg["seed"]))
            for r in results:
                f.write("### [%s] %s — %s\n\n- 结论：%s\n" % (r["status"], r["id"], r["title"], r["detail"]))
                for e in r["evidence"][:8]:
                    f.write("  - %s\n" % e)
                f.write("\n")

    try:
        os.makedirs(os.path.dirname(cfg["out"]), exist_ok=True)
        _dump(cfg["out"], cfg["md_out"])
        return cfg["out"], cfg["md_out"]
    except PermissionError:
        d = _fallback_dir("报告目录 %s" % os.path.dirname(cfg["out"]))
        out = os.path.join(d, "report.json")
        md = os.path.join(d, "report.md")
        _dump(out, md)
        return out, md


def print_summary(results):
    n = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0, "INFO": 0}
    print("\n=== 独立复算结果（verifier：tools/check_ace_mission.py）===")
    for r in results:
        n[r["status"]] = n.get(r["status"], 0) + 1
        print("[%s] %-28s %s" % (r["status"], r["id"], r["title"]))
        print("        %s" % r["detail"])
    print("\n汇总：PASS %d / FAIL %d / WARN %d / SKIP %d" % (n["PASS"], n["FAIL"], n["WARN"], n["SKIP"]))
    return n


# ---------------------------------------------------------------- 自测（自造小样本）

SELFTEST_RATE = 100.0
SELFTEST_SEC = 1.0 / SELFTEST_RATE
SELFTEST_OFF = 10.0
SELFTEST_N = 451
PLAYER_NAME = "=LyRe= 八幡 海鈴"
TR_OFF = (1000.0, -500.0, 0.0)


def _synth_paths(t):
    return (1000.0 + 250.0 * t + 300.0 * math.sin(0.35 * t),
            -500.0 + 40.0 * t + 120.0 * math.sin(0.23 * t),
            3000.0 + 300.0 * math.sin(0.21 * t))


def _synth_u1(t):
    x, y, _ = _synth_paths(t)
    return (x + 2000.0 - 150.0 * t, y + 800.0, 2500.0 + 200.0 * math.sin(0.30 * t))


def _synth_u2(t):
    x, y, _ = _synth_paths(t)
    return (x - 3000.0 + 100.0 * t, y - 1200.0, 4000.0 + 150.0 * math.sin(0.25 * t))


def _synth_g1(t):
    return (5000.0 + 20.0 * t, 3000.0, 30.0)


def _selftest_own(t):
    x, y, z = _synth_paths(t)
    vx = 250.0 + 105.0 * math.cos(0.35 * t)
    vy = 40.0 + 27.6 * math.cos(0.23 * t)
    return x, y, z, math.degrees(math.atan2(vx, vy)) % 360.0


def build_selftest_raw(raw_dir):
    os.makedirs(raw_dir, exist_ok=True)

    def wline(f, rel, data):
        f.write(json.dumps({"t": 1792272850.0 + rel, "rel": round(rel, 3), "ok": True,
                            "data": data}, ensure_ascii=False) + "\n")

    with open(os.path.join(raw_dir, "map_info.jsonl"), "w", encoding="utf-8") as f:
        for rel in np.arange(0, 60.01, 1.0):
            wline(f, rel, {"grid_size": [52719.4, 55385.3], "grid_steps": [5500.0, 5500.0],
                           "grid_zero": [6494.3, 19547.5], "map_max": [65536.0, 65536.0],
                           "map_min": [-65536.0, -65536.0], "valid": True})
    with open(os.path.join(raw_dir, "state.jsonl"), "w", encoding="utf-8") as fs, \
            open(os.path.join(raw_dir, "indicators.jsonl"), "w", encoding="utf-8") as fi:
        for rel in np.arange(0, 60.0001, 0.1):
            t = rel - SELFTEST_OFF
            x, y, z, hdg = _selftest_own(t)
            ias = 480.0 + 40.0 * math.sin(0.4 * t)
            ny = 1.0 + 0.3 * math.sin(0.45 * t)
            wline(fs, rel, {"valid": True, "H, m": round(z), "IAS, km/h": round(ias),
                            "TAS, km/h": round(ias * 1.1), "M": round(ias / 1200.0, 3),
                            "Ny": round(ny, 3), "AoA, deg": 5.0, "Vy, m/s": 1.0,
                            "Mfuel, kg": 3000.0, "throttle 1, %": 85})
            wline(fi, rel, {"valid": True, "type": "f_15a", "army": "air", "compass": hdg,
                            "altitude_hour": z / 0.3048, "g_meter": round(ny, 3),
                            "bank": 0.0, "aviahorizon_roll": 0.0, "aviahorizon_pitch": 0.0,
                            "ammo_counter1": 343, "fuel": 3000.0, "mach": round(ias / 1200.0, 3)})

    def norm(xw, yn):
        return ((xw + 65536.0) / 131072.0, (65536.0 - yn) / 131072.0)

    with open(os.path.join(raw_dir, "map_obj.jsonl"), "w", encoding="utf-8") as f:
        for rel in np.arange(0, 60.0001, 0.2):
            t = rel - SELFTEST_OFF
            x, y, z, hdg = _selftest_own(t)
            nx, nyv = norm(x, y)
            items = [{"type": "aircraft", "color": "#faC81E", "icon": "Player", "x": nx,
                      "y": nyv, "dx": 0.0, "dy": 0.0}]
            for fn, icon, color in ((_synth_u1, "Fighter", "#f00C00"),
                                    (_synth_u2, "Fighter", "#174DFF"),
                                    (_synth_g1, "MediumTank", "#f00C00")):
                ux, uy, _uz = fn(t)
                a, b = norm(ux, uy)
                items.append({"type": "aircraft" if icon != "MediumTank" else "ground_model",
                              "color": color, "icon": icon, "x": a, "y": b})
            wline(f, rel, items)
    rows = [(5.0, 0, 1, "AAA (F-15A) 击毁了 BBB (米格-29)"),
            (12.0, 1, 2, "AAA (F-15A) 击毁了 BBB (米格-29)"),
            (20.0, 2, 10, PLAYER_NAME + " (F-15A) 击落了 CCC (F-20A)"),
            (20.5, 2, 10, PLAYER_NAME + " (F-15A) 击落了 CCC (F-20A)"),
            (30.0, 3, 20, "DDD 已掉线。")]
    with open(os.path.join(raw_dir, "hudmsg.jsonl"), "w", encoding="utf-8") as f:
        for rel, i, gt, msg in rows:
            wline(f, rel, {"events": [], "damage": [{"id": i, "msg": msg, "sender": "",
                                                     "enemy": False, "mode": "", "time": gt}]})
    with open(os.path.join(raw_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"started_at": "2026-10-18 05:34:10", "host": "127.0.0.1", "port": 8111,
                   "hz": 10.0}, f, ensure_ascii=False)


def build_selftest_tracks(path, noise=0.0, rng=None):
    rng = rng or random.Random(7)
    units = []
    specs = [("0xown", _synth_paths, "f_15a", "ally", "air"),
             ("0xu1", _synth_u1, "mig_29", "enemy", "air"),
             ("0xu2", _synth_u2, "fa_18a", "ally", "air"),
             ("0xg1", _synth_g1, "t_72", "enemy", "ground")]
    for eid, fn, veh, side, dom in specs:
        samples = []
        tmax = (SELFTEST_N - 1) / 10.0
        k = 0
        while k / SELFTEST_RATE <= tmax + 1e-9:
            t = k / SELFTEST_RATE
            x, y, z = fn(t)
            nx = 0.0 if noise <= 0 else rng.gauss(0, noise)
            ny = 0.0 if noise <= 0 else rng.gauss(0, noise)
            samples.append([k, round(x + TR_OFF[0] + nx, 2), round(y + TR_OFF[1] + ny, 2),
                            round(z + TR_OFF[2], 2)])
            k += 10
        units.append({"entityId": eid, "vehicle": veh, "player": None, "side": side,
                      "domain": dom, "samples": samples})
    obj = {"secPerTick": SELFTEST_SEC, "tick0": 0, "tickLast": 4500,
           "ownEntityId": "uid-368",
           "killPositions": [{"tickRaw": 1000, "victimUid": 900, "killerUid": 101,
                              "killerVehicle": "mig_29", "weapon": "r_60",
                              "victimPos": [0, 0, 0], "hasVictimPos": True}],
           "scale": {"toMeters": 1.0, "source": "packet_units", "note": "selftest"},
           "playerEntityId": "0xown", "units": units,
           "devValidation": {"ownMaxErrM": 0.0, "ownRmsErrM": 0.0,
                             "samples": SELFTEST_N, "unitAssocRate": 1.0,
                             "speedViolations": 0}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    return obj


def build_selftest_mission(path, own_from="replay", opts=None):
    opts = opts or {}
    ts = [i / 10.0 for i in range(SELFTEST_N)]
    own = {"t0": 0.0, "hz": 10.0, "n": SELFTEST_N}
    xs, ys, zs, hds, iass, nys, machs = [], [], [], [], [], [], []
    for t in ts:
        x, y, z, hdg = _selftest_own(t)
        if own_from == "8111":
            xs.append(round(x)); ys.append(round(y)); zs.append(round(z, 1))
            hds.append(round(hdg, 2))
            iass.append(round(480.0 + 40.0 * math.sin(0.4 * t), 1))
            nys.append(round(1.0 + 0.3 * math.sin(0.45 * t), 3))
            machs.append(round((480.0 + 40.0 * math.sin(0.4 * t)) / 1200.0, 3))
        else:
            xs.append(round(x + TR_OFF[0])); ys.append(round(y + TR_OFF[1]))
            zs.append(round(z + TR_OFF[2], 1))
            hds.append(round((hdg + 1.5) % 360.0, 2))  # 模拟解码航向偏置
            iass.append(None); nys.append(None); machs.append(None)
    zshift = float(opts.get("own_z_shift", 0.0))
    if zshift:
        zs = [round(v + zshift, 1) for v in zs]
    own.update({"x": xs, "y": ys, "z": zs, "ias": iass, "mach": machs, "hdg": hds,
                "roll": [0.0] * SELFTEST_N, "pitch": [0.0] * SELFTEST_N, "g": nys,
                "aoa": [5.0] * SELFTEST_N, "ammo": [343] * SELFTEST_N,
                "fuel": [3000.0] * SELFTEST_N})
    for k in opts.get("drop_own_keys", []):
        own.pop(k, None)

    units = []
    for key, fn, side, dom, veh in (("eny-ftr-01", _synth_u1, "enemy", "air", "mig_29"),
                                    ("ally-ftr-01", _synth_u2, "ally", "air", "fa_18a"),
                                    ("eny-gnd-01", _synth_g1, "enemy", "ground", "t_72")):
        ux, uy, uz, uh = [], [], [], []
        for t in ts:
            x, y, z = fn(t)
            x2, y2, _ = fn(t + 0.1)
            ux.append(round(x + TR_OFF[0], 1)); uy.append(round(y + TR_OFF[1], 1))
            uz.append(round(z + TR_OFF[2], 1))
            uh.append(round(math.degrees(math.atan2(x2 - x, y2 - y)) % 360.0, 1))
        units.append({"key": key, "side": side, "domain": dom, "vehicle": veh,
                      "vehicleName": {"mig_29": "MiG-29", "fa_18a": "F/A-18A",
                                      "t_72": "T-72"}.get(veh, veh),
                      "template": veh, "player": None, "hasAlt": True, "t0": 0.0, "hz": 10.0,
                      "n": SELFTEST_N, "x": ux, "y": uy, "z": uz, "hdg": uh,
                      "uid": {"eny-ftr-01": 900, "ally-ftr-01": 901, "eny-gnd-01": 902}[key],
                      "entityId": {"eny-ftr-01": "0xu1", "ally-ftr-01": "0xu2",
                                   "eny-gnd-01": "0xg1"}[key]})
    hid_name = "不该有名字" if opts.get("bad_hidden") else ""
    units.append({"key": "gnd-hidden-01", "side": "enemy", "domain": "ground",
                  "vehicle": "structures/dummy_immortal", "vehicleName": hid_name,
                  "template": "fortification", "player": None, "hasAlt": True, "static": True,
                  "hidden": True, "t0": 0.0, "hz": 1.0, "n": 1,
                  "x": [1234.0], "y": [-5678.0], "z": [42.0],
                  "hdg": [0.0], "uid": 903, "entityId": "0xh1"})
    if opts.get("const_air_z"):
        units[0]["z"] = [5000.0] * SELFTEST_N
    units[0]["deathTickRaw"] = int(opts.get("death_tick", 1000))
    if opts.get("bad_display_fallback"):
        units[0]["vehicleName"] = units[0]["template"]
    if opts.get("bad_upsample_bridge"):
        units[0]["t0"] = -20.0     # 起点越出源航段（源 tick 0..4500）→ 200 个样本落在范围外

    def ev(t, kind, text, actor, target, player_actor, tick, x, y, z):
        return {"t": t, "kind": kind, "text": text, "composed": True, "actor": actor,
                "actorVehicle": None, "target": target, "targetVehicle": None,
                "entityId": None, "victimEntityId": None, "isPlayerActor": player_actor,
                "isPlayerTarget": False, "replayTick": tick, "x": x, "y": y, "z": z}
    events = [ev(2.0, "kill", "AAA (F-15A) 击毁了 BBB (米格-29)", "AAA", "BBB", False,
                 200, 1000.0, 500.0, 2500.0),
              ev(10.0, "kill", PLAYER_NAME + " (F-15A) 击落了 CCC (F-20A)", PLAYER_NAME,
                 "CCC", True, 1000, 1200.0, 600.0, 2600.0),
              ev(20.0, "disconnect", "DDD 已掉线。", "DDD", None, False, 2000, None, None, None)]
    if opts.get("drop_event") == "kill":
        events = [events[1], events[2]]
    for f in opts.get("drop_event_keys", []):
        for e in events:
            e.pop(f, None)
    kinds = {}
    for e in events:
        kinds[e.get("kind")] = kinds.get(e.get("kind"), 0) + 1

    meta = {
        "source": opts.get("source", "wrpl"),
        "replayFile": "2026.10.18 05.45.06.wrpl",
        "replayHeader": {"version": 1},
        "player": {"name": PLAYER_NAME, "vehicle": "F-15A", "callsign": "LYRE", "kills": 1},
        "matchResults": {"players": [{"userId": 1, "name": PLAYER_NAME, "clanTag": "=LyRe=",
                                      "kills": 1, "humanKills": 0, "groundKills": 0,
                                      "navalKills": 0, "timePlayed": 600}], "raw": None},
        "replayTicks": {"tick0": 0, "tickLast": 4500, "secPerTick": SELFTEST_SEC,
                        "packets": 100, "killPackets": 2},
        "window": {"t0": 0.0, "t1": 45.0, "duration": 45.0},
        "grid": {"stepX": 5500, "stepY": 5500},
        "counts": {"ownSamples": SELFTEST_N, "trackCount": len(units),
                   "airTracks": sum(1 for u in units if u["domain"] == "air"),
                   "groundTracks": sum(1 for u in units if u["domain"] == "ground"),
                   "events": len(events), "kills": kinds.get("kill", 0),
                   "ownKills": 1, "ownKillsByEvents": 1, "ownKillsFromResults": 1},
        "devValidation": {"ownMaxErrM": 0.0, "ownRmsErrM": 0.0,
                          "ownSamplesCompared": SELFTEST_N, "unitAssocRate": 1.0,
                          "speedViolations": 0, "note": "selftest 合成样本"},
        "provenance": ["selftest 自造样本"],
        "limits": ["selftest 自造样本，仅用于验证检查逻辑"],
    }
    if opts.get("devval_coords"):
        meta["devValidation"]["x"] = list(range(20))
    shots = []
    for i, tk in enumerate((200, 400, 700)):
        shots.append({"t": round(tk / 100.0, 3), "tickRaw": tk,
                      "template": ["rocket", "bomb", "rocket"][i],
                      "templateName": (["火箭弹", "炸弹", "火箭弹"][i]
                                       if opts.get("bad_ascii_name")
                                       else ["Rocket", "Bomb", "Rocket"][i]),
                      "x": 100 + i, "y": 200 + i, "z": 300 + i, "yaw": None,
                      "side": ["ally", "enemy", None][i],
                      "shooterUid": [900, 901, None][i], "entityId": "0x%04x" % (0x400 + i)})
    runways = [] if not opts.get("runways_nonempty") else [
        {"airfield": "A", "start": [-1000.0, -2000.0], "end": [1000.0, 2000.0],
         "lengthM": 4472.0, "pcaRatio": 0.98}]
    limits = ["selftest 自造样本，仅用于验证检查逻辑"]
    if not opts.get("bad_runways"):
        limits.append("跑道：地面出生点 PCA 在时间盒内未收敛，runways 留空数组（不猜线段）")
    meta["counts"]["shots"] = len(shots)
    meta["counts"]["runways"] = len(runways)
    meta["counts"]["airfields"] = 1
    meta["counts"]["unitsWithSide"] = len(units)
    meta["counts"]["unitsWithDisplayName"] = sum(
        1 for u in units if isinstance(u.get("vehicleName"), str) and u["vehicleName"])
    meta["counts"]["unitsHidden"] = sum(1 for u in units if u.get("hidden") is True)
    meta["counts"]["unitsWithDeath"] = 1
    meta["limits"] = limits
    # squad fixture：作者=本机（tracks.ownEntityId=0xown），队友 uid 900（vehicle mig_29）
    # 回放内指纹：killPositions victimUid=900 → deaths=1；killerUid=101 → kills 与成员无关
    _squad_uid = 902 if opts.get("bad_squad_uid") else 900
    _author_uid = "0xzzz" if opts.get("bad_squad_author") else "0xown"
    meta["squad"] = {
        "schema": "wrpl-squad/selftest", "squadId": 2002, "members": [
            {"userId": "1", "name": "=LyRe= 八幡 海鈴", "clanTag": "=LyRe=", "team": 2, "slot": 6,
             "vehicle": None, "uid": (368 if _author_uid == "0xown" else _author_uid),
             "uidSource": "own-uid (tracks.ownEntityId)",
             "kills": 2, "deaths": 0, "isAuthor": True},
            {"userId": "2", "name": "Mo Amaris", "clanTag": "PL15E", "team": 2, "slot": 6,
             "vehicle": "mig_29", "uid": _squad_uid,
             "uidSource": "vehicle-candidates + kills + deaths fingerprint (all replay-internal)",
             "kills": 0, "deaths": 1, "isAuthor": False,
             "uidEvidence": {"step1_vehicleCandidates": [900],
                             "step2_fingerprint_wanted": {"kills": 0, "deaths": 1},
                             "step2_fingerprint_match": [900],
                             "fingerprintPerCandidate": {"900": {"kills": 0, "deaths": 1}}}}]}
    _cats = {"eny-ftr-01": "FIGHTER", "ally-ftr-01": "FIGHTER", "eny-gnd-01": "TANK",
             "gnd-hidden-01": "FORTIFICATION"}
    for _u in units:
        _c = _cats.get(_u.get("key"))
        _u["vehicleCategory"] = ("FIGHTER" if opts.get("bad_category") and _u.get("key") == "eny-gnd-01"
                                 else _c)
    meta["vehicleCategories"] = {
        "source": "build/wrpl/vehicle_categories.json（datamine selftest；显示约定，非回放数据）",
        "field": "units[].vehicleCategory", "keys": 2,
        "vocabulary": ["FIGHTER", "TANK", "FORTIFICATION"],
        "unitsWithCategory": sum(1 for u in units if u.get("vehicleCategory")),
        "unitsUncategorized": 0, "uncategorizedValues": []}
    obj_terrain = None
    if not opts.get("bad_terrain_no_note"):
        meta["limits"] = list(meta["limits"]) + [
            "terrain 为 null：本局关卡属老格式、没有 HM2 高度栅格，地形只在 lmap 里"]
    if not opts.get("bad_hidden_note"):
        meta["limits"] = list(meta["limits"]) + [
            "hidden:true 的单位界面不显示（不可摧毁占位物 dummy_immortal / 机场雷达），vehicleName 为空串"]
    if opts.get("bad_terrain_map"):
        obj_terrain = {"meta": {"crs": "replay-world-meters", "map": "levels/some_other_map.bin"},
                       "contours": [], "water": [], "coastlines": []}
    obj = {"meta": meta, "own": own, "units": units, "events": events,
           "shots": shots, "runways": runways,
           "terrain": obj_terrain,
           "airfields": [{"name": "A", "x": 0.0, "y": 0.0}],
           "names": {"eny-ftr-01": {"name": "BBB", "vehicle": "米格-29"}}}
    with open(path, "w", encoding="utf-8") as f:
        f.write("window.ACE_MISSION = " + json.dumps(obj, ensure_ascii=False) + ";\n")
    return obj


def build_selftest_texts(d, opts=None):
    opts = opts or {}
    os.makedirs(d, exist_ok=True)
    dash = "--" if not opts.get("mission_js_no_dash") else "未"
    with open(os.path.join(d, "mission.js"), "w", encoding="utf-8") as f:
        f.write("// selftest 合成 mission.js\nvar CORNER = '%s';\n"
                "// 数据来源：meta.source；局限：见 meta.limits\n" % dash)
    with open(os.path.join(d, "scene3d.js"), "w", encoding="utf-8") as f:
        f.write("// selftest 合成 scene3d.js\nvar UNKNOWN = '%s';\n"
                "function pickUnits(units) { var out = []; for (var i = 0; i < units.length; i++) {"
                " if (units[i].hidden) continue; out.push(units[i]); } return out; }\n"
                % ("未" if opts.get("mission_js_no_dash") else "--"))
    dev = '<script src="dev/fixture-data.js"></script>' if opts.get("index_dev_ref") else ""
    with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
        f.write('<!doctype html>\n<html lang="zh">\n<head>\n'
                '<link rel="stylesheet" href="ace.css">\n<link rel="stylesheet" href="mission.css">\n'
                "</head>\n<body>\n<div id=\"stage\"><canvas id=\"scene\"></canvas>"
                '<div id="corner"></div><div id="controls"></div><div id="help"></div></div>\n'
                + dev + "\n" + '<script src="mission-data.js"></script>\n'
                '<script src="scene3d.js"></script>\n<script src="mission.js"></script>\n'
                "</body>\n</html>\n")
    b = "# selftest 合成生成脚本\n"
    if opts.get("build_reads_8111"):
        b += 'RAW = "data/raw/rb-session"\n'
    with open(os.path.join(d, "build_ace_mission.py"), "w", encoding="utf-8") as f:
        f.write(b)
    with open(os.path.join(d, "DATA-NOTES.md"), "w", encoding="utf-8") as f:
        f.write("# selftest\n")


def build_selftest_entities(path, opts=None):
    """自测用 entities.json：killPackets 与 mission 样本里的两个击杀事件对齐。"""
    opts = opts or {}
    ticks = [200, 1000]
    if opts.get("bad_tick"):
        ticks[0] = ticks[0] + 799
    packets = []
    for i, tk in enumerate(ticks):
        packets.append({"tick": tk, "sec": round(tk / SELFTEST_RATE, 3), "controlByte": 49,
                        "damageType": 48, "strOffset": 14,
                        "vehicle": "mig_29" if i == 0 else "f_20a",
                        "prefixBytes": "f03100feff071c000000", "unitId32": 28 + i})
    obj = {"schema": "wrpl-entities/selftest", "replayFile": "2026.10.18 05.45.06.wrpl",
           "killPackets": packets,
           "awards": [{"award": "first_blood", "count": 1, "firstSec": 119.745,
                       "lastSec": 119.745, "firstTicks": [200]}]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    return obj


def build_selftest_kill_events(path, opts=None):
    """自测用 kill_events.json：位流路径的字段，应与 entities.json 的 tick/vehicle 一致。"""
    opts = opts or {}
    ticks = [200, 1000]
    vehs = ["mig_29", "f_20a"]
    if opts.get("bad_tick"):
        ticks[0] = ticks[0] + 799
    kills = []
    for i, tk in enumerate(ticks):
        kills.append({"tick": tk, "tsMs": tk // 256, "sec": round(tk / SELFTEST_RATE, 3),
                      "msgId": "0xF058",
                      "fields": {"KillerUid": 368 if i == 1 else 101, "PlayerVehicle": vehs[i],
                                 "VictimUid": 900 + i, "PlayerWeapon": "us_aim9l_sidewinder"}})
    obj = {"schema": "wrpl-kill-events/selftest", "replayFile": "2026.10.18 05.45.06.wrpl",
           "generatedAtUtc": "2026-10-18T21:00:00Z", "kills": kills,
           "selfCheck": {"vehicleMatchesPrefixScan": True, "itemsCompared": len(kills)}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    return obj


SELFTEST_SCENARIOS = [
    {"name": "ok_wrpl", "opts": {}, "expect": {
        "0.mission-load": PASS, "1.own-truth": PASS, "2a.events-vs-kills": PASS,
        "2b.events-vs-hudmsg": PASS, "2c.player-kills": PASS, "2a2.kill-events": PASS,
        "3.structure": PASS,
        "3b.window": PASS,
        "4a.time-map": PASS, "4b.units-assoc": PASS, "4c.own-accuracy": PASS,
        "4d.speed-envelope": PASS, "4e.devvalidation": PASS, "5a.units-alt": PASS,
        "5b.devval-coords": PASS, "5c.provenance": PASS, "5d.unknown-dash": PASS,
        "6a.build-no-8111": PASS, "6b.source": PASS, "6c.units-z": PASS,
        "6d.index-no-dev": PASS, "6e.index-static": PASS}},
    {"name": "ok_8111_channel", "opts": {"own_from": "8111"},
     "expect": {"1.own-truth": PASS}},
    {"name": "bad_own_z", "opts": {"own_z_shift": 120.0},
     "expect": {"1.own-truth": FAIL}},
    {"name": "bad_events_count", "opts": {"drop_event": "kill"},
     "expect": {"2a.events-vs-kills": FAIL, "2b.events-vs-hudmsg": WARN}},
    {"name": "bad_const_air_z", "opts": {"const_air_z": True},
     "expect": {"6c.units-z": FAIL}},
    {"name": "bad_source", "opts": {"source": "8111"}, "expect": {"6b.source": FAIL}},
    {"name": "bad_devval_coords", "opts": {"devval_coords": True},
     "expect": {"5b.devval-coords": FAIL}},
    {"name": "bad_struct_missing", "opts": {"drop_own_keys": ["aoa", "fuel"],
                                            "drop_event_keys": ["replayTick"]},
     "expect": {"3.structure": FAIL}},
    {"name": "bad_no_dash", "opts": {"mission_js_no_dash": True},
     "expect": {"5d.unknown-dash": FAIL}},
    {"name": "bad_build_reads_8111", "opts": {"build_reads_8111": True},
     "expect": {"6a.build-no-8111": FAIL}},
    {"name": "bad_index_dev_ref", "opts": {"index_dev_ref": True},
     "expect": {"6d.index-no-dev": FAIL}},
    {"name": "bad_tick_mismatch", "opts": {"bad_tick": True},
     "expect": {"2a.events-vs-kills": WARN}},
    {"name": "ok_extra_layers", "opts": {}, "expect": {
        "3g.death": PASS, "7b.units-10hz": PASS, "7c.display-names": PASS,
        "runways": PASS, "3f.shots": WARN, "terrain": PASS,
        "7c.display-names": PASS, "7e.hidden": PASS, "meta.squad": PASS,
        "7f.vehicle-category": PASS}},
    {"name": "bad_upsample_bridge", "opts": {"bad_upsample_bridge": True},
     "expect": {"7b.units-10hz": WARN}},
    {"name": "bad_display_fallback", "opts": {"bad_display_fallback": True},
     "expect": {"7c.display-names": WARN}},
    {"name": "bad_death_mismatch", "opts": {"death_tick": 999999},
     "expect": {"3g.death": WARN}},
    {"name": "bad_runways_empty", "opts": {"bad_runways": True},
     "expect": {"runways": WARN}},
    {"name": "bad_category", "opts": {"bad_category": True},
     "expect": {"7f.vehicle-category": WARN}},
    {"name": "bad_squad_uid", "opts": {"bad_squad_uid": True},
     "expect": {"meta.squad": WARN}},
    {"name": "bad_squad_author", "opts": {"bad_squad_author": True},
     "expect": {"meta.squad": WARN}},
    {"name": "bad_ascii_name", "opts": {"bad_ascii_name": True},
     "expect": {"7c.display-names": WARN}},
    {"name": "bad_hidden_named", "opts": {"bad_hidden": True},
     "expect": {"7e.hidden": WARN}},
    {"name": "bad_terrain_no_note", "opts": {"bad_terrain_no_note": True},
     "expect": {"terrain": WARN}},
    {"name": "bad_terrain_map", "opts": {"bad_terrain_map": True},
     "expect": {"terrain": WARN}},
    {"name": "bad_tracks_noise", "opts": {"track_noise": 300.0},
     "expect": {"4c.own-accuracy": FAIL, "4e.devvalidation": FAIL}},
]


def run_selftest():
    import shutil
    base = os.path.join(ROOT, "build", "verify", "selftest")
    if os.path.isdir(base):
        shutil.rmtree(base, ignore_errors=True)
    raw_dir = os.path.join(base, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    print("=== 自测：用自造小样本验证检查逻辑本身 ===")
    build_selftest_raw(raw_dir)
    print("合成 8111 小样本：%s" % raw_dir)
    total = bad = 0
    for sc in SELFTEST_SCENARIOS:
        d = os.path.join(base, sc["name"])
        os.makedirs(d, exist_ok=True)
        opts = sc.get("opts") or {}
        build_selftest_tracks(os.path.join(d, "tracks.json"),
                              noise=float(opts.get("track_noise", 0.0)),
                              rng=random.Random(7))
        build_selftest_mission(os.path.join(d, "mission-data.js"),
                               own_from=opts.get("own_from", "replay"), opts=opts)
        build_selftest_texts(d, opts)
        build_selftest_entities(os.path.join(d, "entities.json"), opts)
        build_selftest_kill_events(os.path.join(d, "kill_events.json"), opts)
        with open(os.path.join(d, "header.json"), "w", encoding="utf-8") as f:
            json.dump({"matchResults": {"players": [{"userId": 1, "name": PLAYER_NAME,
                                                     "clanTag": "=LyRe=", "kills": 1,
                                                     "humanKills": 1, "groundKills": 0,
                                                     "navalKills": 0, "timePlayed": 600}]}},
                      f, ensure_ascii=False)
        cfg = {"root": ROOT, "raw_dir": raw_dir, "cache_dir": os.path.join(d, "cache"),
               "contact_hz": 2.0, "rebuild_cache": True,
               "mission_path": os.path.join(d, "mission-data.js"),
               "tracks_path": os.path.join(d, "tracks.json"),
               "header_path": os.path.join(d, "header.json"),
               "entities_path": os.path.join(d, "entities.json"),
               "kill_events_path": os.path.join(d, "kill_events.json"),
               "payload_bin": os.path.join(d, "no-payload.bin"),
               "build_script": os.path.join(d, "build_ace_mission.py"),
               "index_html": os.path.join(d, "index.html"),
               "mission_js": os.path.join(d, "mission.js"),
               "scene_js": os.path.join(d, "scene3d.js"),
               "data_notes": os.path.join(d, "DATA-NOTES.md"),
               "out": os.path.join(d, "report.json"), "md_out": os.path.join(d, "report.md"),
               "seed": 20261018, "checks": None}
        results = []
        try:
            run_all(cfg, results, random.Random(20261018))
        except Exception:
            print("[自测异常] %s：%s" % (sc["name"], traceback.format_exc()[-1500:]))
            bad += 1
            continue
        ok = True
        print("[run] %s" % sc["name"])
        for rid, want in sc["expect"].items():
            got = status_of(results, rid)
            good = (got == want)
            total += 1
            if not good:
                bad += 1
                ok = False
            r = get_of(results, rid)
            print("    %s %-26s 期望 %-4s 实际 %-4s | %s"
                  % ("OK  " if good else "MISS", rid, want, got,
                     (r or {}).get("detail", "")[:90]))
        print("    -> %s" % ("通过" if ok else "失败"))
    print("\n自测汇总：%d/%d 条预期命中；%s"
          % (total - bad, total, "通过" if bad == 0 else "有 %d 条不符" % bad))
    return 0 if bad == 0 else 1


# ---------------------------------------------------------------- 入口

def main(argv=None):
    ap = argparse.ArgumentParser(description="ACE-COMBAT 独立复算/验收工具（verifier）")
    ap.add_argument("--selftest", action="store_true", help="用自造小样本验证检查逻辑")
    ap.add_argument("--rebuild-cache", action="store_true", help="重建 8111 原始数据缓存")
    ap.add_argument("--seed", type=int, default=20261018, help="随机取时刻的种子")
    ap.add_argument("--checks", default=None, help="逗号分隔，只跑这些检查（1,2,3,4,5,6）")
    ap.add_argument("--mission", default=None, help="mission-data.js 路径（默认 web/ace-combat/）")
    ap.add_argument("--tracks", default=None, help="tracks.json 路径")
    args = ap.parse_args(argv)
    if args.selftest:
        return run_selftest()
    cfg = make_cfg(args)
    if args.mission:
        cfg["mission_path"] = os.path.abspath(args.mission)
    if args.tracks:
        cfg["tracks_path"] = os.path.abspath(args.tracks)
    if args.checks:
        cfg["checks"] = [c.strip() for c in args.checks.split(",") if c.strip()]
    results = []
    run_all(cfg, results, random.Random(cfg["seed"]))
    n = print_summary(results)
    out, md = write_report(cfg, results)
    print("\n报告：%s\n      %s" % (out, md))
    return 1 if n["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())



