# -*- coding: utf-8 -*-
"""Build the browser-side airfield catalogue from wrpl-acmi data.

Source: ``airfields_all.json`` from https://github.com/3794432/wrpl-acmi (dev branch,
AGPL-3.0; it ships that catalogue with the project).
Output: ``terrain/airfields.json`` — only "map -> mission -> runway segment (world
metres)" is kept, with the upstream commit recorded. The derived file is distributed
under AGPL-3.0 to match its source; no upstream code is copied.

    python tools/build_airfields_catalogue.py --src path/to/airfields_all.json
"""
import argparse
import json
import math
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", required=True,
                    help="airfields_all.json from wrpl-acmi")
    ap.add_argument("--dst", default=str(pathlib_path()),
                    help="output JSON (default: terrain/airfields.json)")
    args = ap.parse_args(argv)
    build(args.src, args.dst)
    return 0


def pathlib_path():
    import pathlib
    return pathlib.Path(__file__).resolve().parents[1] / "terrain" / "airfields.json"


def world(tm, v):
    return [sum(tm[r][c] * v[c] for c in range(3)) + tm[3][r] for r in range(3)]


def rnd(v):
    return round(float(v), 1)


def build(src, dst):
    cat = json.load(open(src, encoding="utf-8"))
    prefabs = cat["prefabs"]
    if isinstance(prefabs, list):
        prefabs = {p["id"]: p for p in prefabs}
    out = {"schema": 1, "source": "wrpl-acmi", "license": "AGPL-3.0",
           "commit": cat.get("commit"), "maps": {}}
    total = 0
    for m in cat["maps"]:
        map_id = m.get("map_id")
        missions = []
        for mi in m.get("missions") or []:
            rws = []
            seen = set()
            for p in mi.get("placements") or []:
                pre = prefabs.get(p.get("prefab"))
                tm = p.get("tm")
                if not pre or not tm or not pre.get("start") or not pre.get("end"):
                    continue
                s = world(tm, pre["start"])
                e = world(tm, pre["end"])
                length = math.dist(s, e)
                if length < 200:          # 不是跑道（模板里的小结构）
                    continue
                key = (p.get("name"), round(s[0]), round(s[2]), round(e[0]), round(e[2]))
                if key in seen:
                    continue
                seen.add(key)
                rank = p.get("rank_range")
                if not rank:
                    for c in p.get("conditions") or []:
                        if c.get("rank_range"):
                            rank = c["rank_range"]; break
                rws.append({
                    "name": p.get("name"), "team": p.get("team"),
                    "x1": rnd(s[0]), "y1": rnd(s[2]), "z1": rnd(s[1]),
                    "x2": rnd(e[0]), "y2": rnd(e[2]), "z2": rnd(e[1]),
                    "width": rnd(pre.get("width") or 0), "length": rnd(length),
                    "alt": rnd(p.get("ground_height")) if p.get("ground_height") is not None else rnd(s[1]),
                    "conditional": bool(p.get("conditional")),
                    "rank": rank,
                })
            for d in mi.get("direct_runways") or []:
                s, e = d.get("start"), d.get("end")
                if not s or not e:
                    continue
                length = math.dist(s, e)
                if length < 200:
                    continue
                key = (d.get("name"), round(s[0]), round(s[2]), round(e[0]), round(e[2]))
                if key in seen:
                    continue
                seen.add(key)
                rank = None
                for c in d.get("conditions") or []:
                    if c.get("rank_range"):
                        rank = c["rank_range"]; break
                rws.append({
                    "name": d.get("name"), "team": d.get("team"),
                    "x1": rnd(s[0]), "y1": rnd(s[2]), "z1": rnd(s[1]),
                    "x2": rnd(e[0]), "y2": rnd(e[2]), "z2": rnd(e[1]),
                    "width": rnd(d.get("width") or 0), "length": rnd(length),
                    "alt": rnd(d.get("ground_height")) if d.get("ground_height") is not None else rnd(s[1]),
                    "conditional": bool(d.get("conditional")),
                    "rank": rank,
                })
            if rws:
                missions.append({"mission": mi.get("mission"), "runways": rws})
                total += len(rws)
        if missions:
            out["maps"][map_id] = {"missions": missions}
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("maps", len(out["maps"]), "runways", total, "bytes", os.path.getsize(dst))


if __name__ == "__main__":
    sys.exit(main())
