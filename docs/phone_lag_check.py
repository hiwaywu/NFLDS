"""Phone latency check over CDP: click to first frame, then seconds behind live.

    python phone_lag_check.py [samples]

Requires: adb forward tcp:9222 localabstract:webview_devtools_remote_<pid>
"""
import asyncio
import json
import sys
import urllib.request

import websockets

HOST = "127.0.0.1:9222"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 12


def target():
    with urllib.request.urlopen(f"http://{HOST}/json/list", timeout=8) as r:
        pages = [t for t in json.loads(r.read().decode()) if t.get("type") == "page"]
    return pages[0]


async def evaluate(ws, expr, await_promise=False):
    async with websockets.connect(ws, max_size=32 * 1024 * 1024) as sock:
        await sock.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": expr, "awaitPromise": await_promise,
            "returnByValue": True, "userGesture": True}}))
        while True:
            msg = json.loads(await sock.recv())
            if msg.get("id") == 1:
                res = msg.get("result", {}).get("result", {})
                return res.get("value", res)


async def main():
    p = target()
    ws = p["webSocketDebuggerUrl"]
    print("target:", p.get("url"))
    await evaluate(ws, "(() => { window.__t0 = Date.now(); window.__first = null; v.addEventListener('timeupdate', () => { if (window.__first === null && v.currentTime > 0.2) window.__first = Date.now() - window.__t0; }); playChannel(channels[0]); return 'started'; })()")
    samples = []
    for i in range(N):
        await asyncio.sleep(4)
        s = await evaluate(ws, """JSON.stringify((() => {
            let fwd = null;
            try { if (hls && hls.mainForwardBufferInfo) fwd = hls.mainForwardBufferInfo().len; } catch(e){}
            return {
                first: window.__first,
                t: Math.round(v.currentTime),
                rs: v.readyState,
                lat: (hls && typeof hls.latency === 'number') ? Math.round(hls.latency * 10) / 10 : null,
                stalls: window.stallCount || 0,
                pill: (document.getElementById('pillLat') || {}).textContent
            };
        })())""")
        samples.append(json.loads(s))
    lats = [x["lat"] for x in samples if x["lat"]]
    print(json.dumps({
        "click_to_first_frame_ms": samples[0]["first"] if samples else None,
        "latency_samples_s": lats,
        "latency_avg_s": round(sum(lats) / len(lats), 1) if lats else None,
        "stalls": samples[-1]["stalls"] if samples else None,
        "pill_text": samples[-1]["pill"] if samples else None,
        "readyState": samples[-1]["rs"] if samples else None,
    }, indent=2))


asyncio.run(main())
