"""Real Chrome: does the native video-fullscreen path stretch a 16:9 video on a
16:10 screen? Compares webkitEnterFullscreen() with requestFullscreen().

    python fs_native_vs_api.py
"""
import json
import os

from PIL import Image
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = "file:///" + os.path.join(HERE, "fs_test.html").replace("\\", "/")
W, H = 1680, 1050


def picture(path):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    px = im.load()
    white = [sum(1 for x in range(0, w, 4) if px[x, y] == (255, 255, 255)) for y in range(h)]
    first = next((y for y, n in enumerate(white) if n > w // 12), None)
    last = next((y for y in range(h - 1, -1, -1) if white[y] > w // 12), None)
    return {"size": [w, h], "first_white_row": first, "last_white_row": last,
            "white_height": (last - first + 1) if first is not None else 0,
            "aspect": round(w / (last - first + 1), 3) if first is not None else None,
            "expected_16_9": round(w * 9 / 16, 1)}


def run(page, how):
    page.reload()
    page.wait_for_timeout(1200)
    caps = page.evaluate("() => ({req: typeof document.getElementById('v').requestFullscreen,"
                         " wk: typeof document.getElementById('v').webkitEnterFullscreen})")
    if how == "api":
        ok = page.evaluate("""() => new Promise(res => {
            const v = document.getElementById('v');
            v.requestFullscreen().then(() => res(true)).catch(e => res(String(e)));
        })""")
    else:
        ok = page.evaluate("""() => { try { document.getElementById('v').webkitEnterFullscreen(); return 'called'; }
                                     catch(e) { return String(e); } }""")
    page.wait_for_timeout(2500)
    shot = os.path.join(HERE, "fs_native_%s.png" % how)
    page.screenshot(path=shot)
    state = page.evaluate("""() => ({fsEl: document.fullscreenElement ? document.fullscreenElement.tagName : null,
                                     wk: !!(document.getElementById('v').webkitDisplayingFullscreen)})""")
    page.keyboard.press("Escape")
    page.wait_for_timeout(800)
    return {"how": how, "capabilities": caps, "call": ok, "state": state, "measured": picture(shot)}


with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=True, args=["--mute-audio"])
    page = br.new_context(viewport={"width": W, "height": H}).new_page()
    page.goto(PAGE)
    out = {"chrome": br.version, "viewport": [W, H]}
    out["api"] = run(page, "api")
    out["native"] = run(page, "native")
    br.close()
    print(json.dumps(out, indent=2))
