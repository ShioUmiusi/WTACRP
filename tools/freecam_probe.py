"""回归检查：自由镜头必须能拖动相机（含"开局本机还没跟到玩家"的远景窗口）。

为什么需要浏览器：OrbitControls 的 pointermove/pointerup 挂在 document 上并调用
setPointerCapture，合成 PointerEvent 驱动不了它，只能真鼠标（Playwright）。

两个臂，缺一不可：

  gap      页面刚加载、本机航迹还是 null（本例 0–147.8 s）。此时渲染循环会进
           "战场全局远景"（CAMS.over）。**修好前**这里拖鼠标完全没反应
           （每帧被远景重定位覆盖）——这就是用户报的
           「如果一开始没有镜头跟随到玩家 自由镜头不起效」。
  control  seek 到 200 s（本机已在），远景窗口不生效。这个臂必须能动，
           否则说明探针本身坏了，gap 臂的"没动"没有意义。

判据：gap 臂 changed_px > 2000（画面必须变），control 臂同理；任一不满足退出码 1。

    python tools/freecam_probe.py --url http://127.0.0.1:8123/ --wrpl scratch/terrain-test/avg_soviet_range.wrpl
    python tools/build_site.py --data ../warthunder-maps --out dist   # 先构站
    python -m http.server 8123 --directory dist                        # 再服务
"""
import argparse
import pathlib
import sys

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE.parent / "scratch" / "freecam-probe"
MIN_CHANGED = 2000


def diff_px(p1, p2):
    from PIL import Image, ImageChops
    import numpy as np
    a = Image.open(p1).convert("RGB")
    b = Image.open(p2).convert("RGB")
    d = np.asarray(ImageChops.difference(a, b)).astype(int).sum(axis=2)
    return int((d > 12).sum())


def drag(page, dx=300, dy=72, steps=12):
    box = page.locator("#scene").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    for i in range(1, steps + 1):
        page.mouse.move(x + dx * i / steps, y + dy * i / steps)
    page.mouse.up()
    page.wait_for_timeout(900)          # OrbitControls 有阻尼，等它停


def arm(page, tag, t_sec=None):
    page.evaluate("window.ACEUI.pause()")
    if t_sec is not None:
        page.evaluate("s => window.ACEUI.seek(s)", t_sec)   # 注意：seek 会清掉 CAMS.over
    page.evaluate("window.ACEScene.setCamera('free')")
    page.wait_for_timeout(400)
    OUT.mkdir(parents=True, exist_ok=True)
    a, b = OUT / f"{tag}-a.png", OUT / f"{tag}-b.png"
    page.screenshot(path=str(a))
    drag(page)
    page.screenshot(path=str(b))
    changed = diff_px(a, b)
    print("  %-10s t=%-6s camMode=%-5s changed_px=%-7d %s"
          % (tag, page.evaluate("window.ACEScene.getStats().t"),
             page.evaluate("window.ACEScene.getStats().camMode"), changed,
             "OK" if changed > MIN_CHANGED else "FAIL（拖动无效果）"))
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8123/")
    ap.add_argument("--wrpl", default=str(HERE.parent / "scratch" / "terrain-test" / "avg_soviet_range.wrpl"))
    args = ap.parse_args()
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        page = br.new_page(viewport={"width": 1280, "height": 800})
        logs = []
        page.on("console", lambda m: logs.append(m.text))
        page.goto(args.url)
        page.set_input_files("#upload-file", args.wrpl)
        page.wait_for_function("window.ACEScene && window.ACEScene.getReady && window.ACEScene.getReady()",
                               timeout=180000)
        gap = arm(page, "gap")                      # 必须先跑：此时 CAMS.over 已经武装
        control = arm(page, "control", 200)         # seek 会清 CAMS.over，所以放第二个
        br.close()
    ok = gap > MIN_CHANGED and control > MIN_CHANGED
    print("free-camera probe: gap=%d control=%d -> %s" % (gap, control, "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
