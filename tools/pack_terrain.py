"""Pack terrain assets (contours + land masks) for the static site.

Reads the dataset published by WT-MAP-ANALYZE and writes the filenames the
player fetches:

    <out>/<map>_contours.json   <- <data>/dataset/contours/<map>_contours.json
    <out>/<map>_landmask.json   <- <data>/dataset/landmask/<map>.json

Usage:

    python tools/pack_terrain.py --data ../WT-MAP-ANALYZE --out dist/terrain

The page only fetches the map it needs; the repository ships a few sample maps
in ``terrain/`` for local use, the full set is packed at build time.
"""
import argparse
import json
import pathlib
import shutil
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True,
                    help="WT-MAP-ANALYZE checkout (contains dataset/)")
    ap.add_argument("--out", default="dist/terrain")
    ap.add_argument("--maps", nargs="*", default=None, help="only these maps")
    args = ap.parse_args(argv)

    data = pathlib.Path(args.data)
    contours = data / "dataset" / "contours"
    masks = data / "dataset" / "landmask"
    if not contours.is_dir():
        sys.exit("no dataset/contours under %s" % data)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    maps = {p.name[:-len("_contours.json")] for p in contours.glob("*_contours.json")}
    maps |= {p.stem for p in masks.glob("*.json") if p.name != "index.json"}
    if args.maps:
        maps &= set(args.maps)

    n_c = n_m = 0
    for m in sorted(maps):
        src = contours / (m + "_contours.json")
        if src.exists():
            dst = out / (m + "_contours.json")
            shutil.copy2(src, dst)
            n_c += 1
            try:
                meta = json.loads(dst.read_text(encoding="utf-8")).get("meta") or {}
            except Exception:
                meta = {}
            if meta.get("crs") != "replay-world-meters" or (meta.get("map") or "") != m:
                print("  warning: %s has an unexpected meta (crs=%s map=%s)"
                      % (m, meta.get("crs"), meta.get("map")))
        src = masks / (m + ".json")
        if src.exists():
            shutil.copy2(src, out / (m + "_landmask.json"))
            n_m += 1

    print("terrain: %d contours, %d masks -> %s" % (n_c, n_m, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
