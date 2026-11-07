"""End-to-end fullscreen aspect test on the real hub page.

Swaps the live stream for a known 16:9 white video (data: URL, so no server
changes), presses the page's own fullscreen button, screenshots and measures the
picture box. Chromium at 1680x1050 (16:10) must show 945 px of picture with black
bars, i.e. object-fit:contain -- not 1050 px of stretched picture.
"""
import base64
import json
import os
import sys

from PIL import Image
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790/"
CHANNEL = sys.argv[2] if len(sys.argv) > 2 else "chrome"      # or "chromium"
W, H = 1680, 1050
SHOT = os.path.join(HERE, "fs_hub.png")

with open(os.path.join(HERE, "white180.b64"), "r") as fh:
    B64 = fh.read().strip()


def measure(path):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    px = im.load()
    rows = []
    for y in range(h):
        n = sum(1 for x in range(0, w, 4) if px[x, y] == (255, 255, 255))
        rows.append(n)
    first = next((y for y, n in enumerate(rows) if n > w // 12), None)
    last = next((y for y in range(h - 1, -1, -1) if rows[y] > w // 12), None)
    return {"size": [w, h], "first": first, "last": last,
            "picture_height": (last - first + 1) if first is not None else 0,
            "top_bar": first, "bottom_bar": (h - 1 - last) if last is not None else None,
            "aspect": round(w / (last - first + 1), 3) if first is not None else None,
            "expected_16_9_height": round(w * 9 / 16, 1)}


with sync_playwright() as pw:
    kwargs = {"headless": True, "args": ["--mute-audio", "--autoplay-policy=no-user-gesture-required"]}
    if CHANNEL == "chrome":
        kwargs["channel"] = "chrome"
    br = pw.chromium.launch(**kwargs)
    page = br.new_context(viewport={"width": W, "height": H}).new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)

    # put a known 16:9 picture into the player instead of the live stream
    page.evaluate("""(b64) => {
        if (window.hls) { try { hls.destroy(); } catch(e){} hls = null; }
        document.getElementById('idle').style.display = 'none';
        document.body.classList.remove('iframeMode');
        document.body.classList.add('playing');
        v.loop = true; v.muted = true; v.controls = false;   // controls would cover the picture
        v.src = 'data:video/mp4;base64,' + b64;
        return v.play().then(() => true).catch(e => String(e));
    }""", B64)
    page.wait_for_timeout(2000)
    before = page.evaluate("""() => ({rs: v.readyState, size: [v.videoWidth, v.videoHeight],
                                      fit: getComputedStyle(v).objectFit,
                                      rect: [v.getBoundingClientRect().width, v.getBoundingClientRect().height]})""")
    page.click("#fs")
    page.wait_for_timeout(2000)
    after = page.evaluate("""() => ({fsEl: document.fullscreenElement ? document.fullscreenElement.id || document.fullscreenElement.tagName : null,
                                     fit: getComputedStyle(v).objectFit,
                                     rect: [Math.round(v.getBoundingClientRect().width), Math.round(v.getBoundingClientRect().height)],
                                     exitBtn: getComputedStyle(document.getElementById('fsexit')).display,
                                     btn: document.getElementById('fs').textContent})""")
    page.screenshot(path=SHOT)
    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    restored = page.evaluate("() => ({fsEl: !!document.fullscreenElement, btn: document.getElementById('fs').textContent})")
    br.close()

print(json.dumps({"url": URL, "before": before, "fullscreen": after,
                  "after_escape": restored, "measured": measure(SHOT)}, indent=2))
