"""Phone fullscreen aspect test over CDP (no Playwright: WebView devtools).

    python phone_fs_check.py

Puts a known 16:9 white picture into the app's <video>, presses the page's own
fullscreen button, screenshots with adb and measures the letterbox. On a
1080x2400 screen a 16:9 picture must be 1080x608, not 1080x2400.
"""
import asyncio
import base64
import json
import os
import subprocess
import urllib.request

import websockets
from PIL import Image

HOST = "127.0.0.1:9222"
HERE = os.path.dirname(os.path.abspath(__file__))
SHOT_LOCAL = os.path.join(HERE, "phone_fs.png")
SHOT_DEV = "/sdcard/phone_fs.png"

with open(os.path.join(HERE, "white180.b64")) as fh:
    B64 = fh.read().strip()


def targets():
    with urllib.request.urlopen(f"http://{HOST}/json/list", timeout=8) as r:
        return json.loads(r.read().decode())


async def call(ws_url, method, params=None):
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                return msg


async def evaluate(ws, expr):
    r = await call(ws, "Runtime.evaluate",
                   {"expression": expr, "awaitPromise": True, "returnByValue": True, "userGesture": True})
    res = r.get("result", {}).get("result", {})
    return res.get("value", res)


async def main():
    pages = [t for t in targets() if t.get("type") == "page"]
    if not pages:
        print("no page target; is the app running?")
        return
    p = pages[0]
    print("target:", p.get("url"))
    ws = p["webSocketDebuggerUrl"]

    await evaluate(ws, "window.__W=" + json.dumps(B64) + "; true")
    injected = await evaluate(ws, """(() => {
        try { if (window.hls) { hls.destroy(); hls = null; } } catch(e){}
        document.getElementById('idle').style.display='none';
        document.getElementById('fsexit').style.display='none';
        document.body.classList.remove('iframeMode');
        document.body.classList.add('playing');
        v.controls = false; v.loop = true; v.muted = true;
        v.src = 'data:video/mp4;base64,' + window.__W;
        return v.play().then(() => 'playing').catch(e => 'play: ' + e);
    })()""")
    print("inject:", injected)
    await asyncio.sleep(1.5)
    before = await evaluate(ws, "JSON.stringify({rs: v.readyState, size: [v.videoWidth, v.videoHeight], fit: getComputedStyle(v).objectFit})")
    print("before:", before)

    clicked = await evaluate(ws, "(() => { document.getElementById('fs').click(); return 'clicked'; })()")
    print("click:", clicked)
    await asyncio.sleep(2.0)
    after = await evaluate(ws, """JSON.stringify({
        fsEl: document.fullscreenElement ? (document.fullscreenElement.id || document.fullscreenElement.tagName) : null,
        wkEl: document.webkitFullscreenElement ? (document.webkitFullscreenElement.id || document.webkitFullscreenElement.tagName) : null,
        displaying: !!(v && v.webkitDisplayingFullscreen),
        fit: getComputedStyle(v).objectFit,
        rect: [Math.round(v.getBoundingClientRect().width), Math.round(v.getBoundingClientRect().height)],
        exitBtn: getComputedStyle(document.getElementById('fsexit')).display,
        btn: document.getElementById('fs').textContent
    })""")
    print("fullscreen:", after)

    subprocess.run(["adb", "shell", "screencap", "-p", SHOT_DEV], check=False)
    subprocess.run(["adb", "pull", SHOT_DEV, SHOT_LOCAL], check=False, capture_output=True)

    im = Image.open(SHOT_LOCAL).convert("RGB")
    w, h = im.size
    px = im.load()
    rows = [sum(1 for x in range(0, w, 6) if px[x, y] == (255, 255, 255)) for y in range(h)]
    first = next((y for y, n in enumerate(rows) if n > w // 12), None)
    last = next((y for y in range(h - 1, -1, -1) if rows[y] > w // 12), None)
    print(json.dumps({
        "screen": [w, h],
        "picture": {"first": first, "last": last,
                    "height": (last - first + 1) if first is not None else 0,
                    "aspect": round(w / (last - first + 1), 3) if first is not None else None,
                    "expected_16_9_height": round(w * 9 / 16, 1)},
    }, indent=2))


asyncio.run(main())
