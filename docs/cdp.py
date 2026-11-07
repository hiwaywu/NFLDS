"""Drive the Android WebView directly over CDP (raw websocket, no Playwright).

    python cdp.py pages
    python cdp.py eval "<javascript expression>"
    python cdp.py shot out.png
"""
import asyncio
import base64
import json
import sys
import urllib.request

import websockets

HOST = "127.0.0.1:9222"


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


def pick(pages, want=None):
    for p in pages:
        if want and want in p.get("url", ""):
            return p
    return pages[0]


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "pages"
    pages = [t for t in targets() if t.get("type") == "page"]
    if mode == "pages":
        for p in pages:
            print(p.get("id"), p.get("url"), p.get("webSocketDebuggerUrl"))
        return
    want = sys.argv[3] if len(sys.argv) > 3 and mode == "eval" and sys.argv[3].startswith("http") else None
    p = pick(pages, want)
    print("target:", p.get("url"))
    if mode == "eval":
        r = await call(p["webSocketDebuggerUrl"], "Runtime.evaluate",
                       {"expression": sys.argv[2], "awaitPromise": True,
                        "returnByValue": True, "userGesture": True})
        res = r.get("result", {}).get("result", {})
        print(json.dumps(res.get("value", res), indent=2, default=str))
        if r.get("result", {}).get("exceptionDetails"):
            print("EXCEPTION:", json.dumps(r["result"]["exceptionDetails"])[:800])
    elif mode == "evalfile":
        with open(sys.argv[2], "r", encoding="utf-8") as fh:
            expr = fh.read()
        r = await call(p["webSocketDebuggerUrl"], "Runtime.evaluate",
                       {"expression": expr, "awaitPromise": True,
                        "returnByValue": True, "userGesture": True})
        res = r.get("result", {}).get("result", {})
        print(json.dumps(res.get("value", res), indent=2, default=str))
        if r.get("result", {}).get("exceptionDetails"):
            print("EXCEPTION:", json.dumps(r["result"]["exceptionDetails"])[:800])
    elif mode == "shot":
        r = await call(p["webSocketDebuggerUrl"], "Page.captureScreenshot", {"format": "png"})
        data = r["result"]["data"]
        with open(sys.argv[2], "wb") as fh:
            fh.write(base64.b64decode(data))
        print("saved", sys.argv[2])


asyncio.run(main())
