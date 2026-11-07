"""How fast does it start, and how far behind live does it settle?

    python lag_check.py [url] [seconds]

Measures, in real Chrome:
  * click -> first frame (time until currentTime advances)
  * steady-state latency behind the live edge (hls.latency)
  * stalls (video `waiting` events) over the sample window
  * relay buffer and pull rate at the end
"""
import json
import sys
import time

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790/"
SAMPLE = int(sys.argv[2]) if len(sys.argv) > 2 else 45
LOW = (sys.argv[3].lower() == "low") if len(sys.argv) > 3 else False

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=True,
                            args=["--mute-audio", "--autoplay-policy=no-user-gesture-required"])
    page = br.new_context(viewport={"width": 1440, "height": 900}).new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)

    t0 = time.time()
    page.evaluate("""(low) => {
        if (low) lowMode = true;
        window.__t0 = Date.now();
        playChannel(channels[0]);
    }""", LOW)
    first = None
    while time.time() - t0 < 40:
        page.wait_for_timeout(200)
        st = page.evaluate("() => ({t: v.currentTime, rs: v.readyState})")
        if st["rs"] >= 2 and st["t"] > 0.2:
            first = round((time.time() - t0) * 1000)
            break

    samples = []
    stalls0 = page.evaluate("() => window.stallCount || 0")
    for _ in range(SAMPLE // 3):
        page.wait_for_timeout(3000)
        samples.append(page.evaluate("""() => {
            let fwd = null;
            try {
                if (hls && hls.mainForwardBufferInfo) fwd = hls.mainForwardBufferInfo().len;
                else if (hls && hls.bufferController && hls.bufferController.getMainForwardBufferInfo)
                    fwd = hls.bufferController.getMainForwardBufferInfo().len;
            } catch (e) {}
            return {
                lat: (hls && typeof hls.latency === 'number') ? hls.latency : null,
                fwd: fwd,
                t: v.currentTime,
                stalls: window.stallCount || 0
            };
        }"""))

    tail = page.evaluate("""async () => {
        let s = null;
        try { s = await fetch(relayBase + '/stats?_=' + Date.now()).then(r => r.json()); } catch(e) {}
        return {relay: s && {segments: s.segments_cached, buffered_s: s.buffered_seconds,
                             pull_Mbps: Math.round(s.upstream_Mbps)}, port: relayPort};
    }""")
    br.close()

lats = [s["lat"] for s in samples if s["lat"]]
fwd = [s["fwd"] for s in samples if s["fwd"] is not None]
print(json.dumps({
    "click_to_first_frame_ms": first,
    "latency_samples_s": [round(x, 1) for x in lats],
    "latency_avg_s": round(sum(lats) / len(lats), 1) if lats else None,
    "forward_buffer_avg_s": round(sum(fwd) / len(fwd), 1) if fwd else None,
    "stalls_total": (samples[-1]["stalls"] - stalls0) if samples else None,
    "relay": tail["relay"],
    "relay_port": tail["port"],
}, indent=2))
