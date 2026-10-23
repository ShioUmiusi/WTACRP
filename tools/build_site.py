"""Assemble the GitHub Pages site into ``dist/``.

Only the uploader/tool page is published. It lives at ``web/index.html`` in the
repository, so paths starting with ``../`` are rebased to the site root here.
The demo page and its pre-generated data stay in the repository for local use
and are deliberately not deployed.

    python tools/build_site.py --data ../WT-MAP-ANALYZE --out dist
"""
import argparse
import pathlib
import shutil
import sys

import pack_terrain

ROOT = pathlib.Path(__file__).resolve().parents[1]
# files copied to the site root, keeping their names
ROOT_FILES = ["ace.css", "mission.css", "mission.js", "scene3d3-loader.js",
              "web/wrpl-parse.js", "web/wrpl-worker.js", "web/airfields.js"]
# (source dir, destination dir)
DIRS = [("three3d", "three3d"), ("tables", "tables")]


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

    for name in ROOT_FILES:
        src = ROOT / name
        shutil.copy2(src, out / src.name)
    for src_name, dst_name in DIRS:
        shutil.copytree(ROOT / src_name, out / dst_name)
    (out / "three3d").mkdir(exist_ok=True)

    # tool page as site root, with ../ references rebased to /
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    (out / "index.html").write_text(html.replace('"../', '"./'), encoding="utf-8")

    # three.js and fzstd both go under vendor/
    vendor = out / "vendor"
    vendor.mkdir(exist_ok=True)
    shutil.copytree(ROOT / "vendor" / "three", vendor / "three")
    shutil.copytree(ROOT / "web" / "vendor", vendor, dirs_exist_ok=True)

    pack_terrain.main(["--data", str(args.data), "--out", str(out / "terrain")])
    # 机场目录是仓库自带的小表（不依赖地形数据集），单独拷进站点
    af = ROOT / "terrain" / "airfields.json"
    if af.exists():
        (out / "terrain").mkdir(parents=True, exist_ok=True)
        shutil.copy2(af, out / "terrain" / "airfields.json")
    (out / ".nojekyll").write_text("")
    print("site -> %s (tool page only; demo page not deployed)" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
