"""Assemble the static site into ``dist/`` for GitHub Pages.

The repository ships the application and a few sample maps; the full terrain
set comes from WT-MAP-ANALYZE at build time:

    python tools/build_site.py --data ../WT-MAP-ANALYZE --out dist
"""
import argparse
import pathlib
import shutil
import sys

import pack_terrain

ROOT = pathlib.Path(__file__).resolve().parents[1]
FILES = [
    "index.html", "mission.js", "mission.css", "ace.css",
    "scene3d3-loader.js", "mission-data.js", "attitude-data.js",
]
DIRS = ["web", "vendor", "tables"]
CNAME = "wtacrp.seaslug.moe"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True,
                    help="WT-MAP-ANALYZE checkout (contains dataset/)")
    ap.add_argument("--out", default="dist")
    args = ap.parse_args(argv)

    out = pathlib.Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for name in FILES:
        shutil.copy2(ROOT / name, out / name)
    for name in DIRS:
        shutil.copytree(ROOT / name, out / name,
                        ignore=shutil.ignore_patterns("dev"))
    (out / "three3d").mkdir()
    shutil.copy2(ROOT / "three3d" / "scene.js", out / "three3d" / "scene.js")
    (out / "terrain").mkdir()
    for p in (ROOT / "terrain").glob("air_skyscraper_city*"):
        shutil.copy2(p, out / "terrain" / p.name)

    pack_terrain.main(["--data", str(args.data), "--out", str(out / "terrain")])

    (out / ".nojekyll").write_text("")
    (out / "CNAME").write_text(CNAME + "\n")
    print("site -> %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
