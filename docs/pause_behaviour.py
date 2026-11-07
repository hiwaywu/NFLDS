"""What does pausing actually do to the buffers?

    python pause_behaviour.py [pause_seconds]

Plays NFL TV, pauses, watches both buffers (relay ring + hls.js forward buffer),
then resumes and reports whether it continues where it stopped or jumps to live.
"""
import json
import sys

from playwright.sync_api import sync_playwright

PAUSE = int(sys.argv[1]) if len(sys.argv) > 1 else 30

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=True,
                            args=["--mute-audio", "--autoplay-policy=no-user-gesture-required"])
    page = br.new_context(viewport={"width": 1000, "height": 700}).new_page()
    page.goto("http://127.0.0.1:8790/", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    page.evaluate("() => playChannel(channels[0])")

    for _ in range(30):
        page.wait_for_timeout(1000)
        if page.evaluate("() => v.readyState >= 2 && v.currentTime > 1"):
            break

    def snap(label):
        return page.evaluate("""async (label) => {
            let relay = null;
            try { relay = await fetch(relayBase + '/stats?_=' + Date.now()).then(r => r.json()); } catch(e){}
            let fwd = null;
            try { if (hls && hls.mainForwardBufferInfo) fwd = hls.mainForwardBufferInfo().len; } catch(e){}
            let buf = null;
            try {
                if (v.buffered.length) buf = v.buffered.end(v.buffered.length - 1) - v.currentTime;
            } catch(e){}
            return {
                label,
                t: Math.round(v.currentTime * 10) / 10,
                paused: v.paused,
                latency: (hls && typeof hls.latency === 'number') ? Math.round(hls.latency * 10) / 10 : null,
                player_ahead_s: buf === null ? null : Math.round(buf * 10) / 10,
                hls_forward_s: fwd === null ? null : Math.round(fwd * 10) / 10,
                relay_segments: relay ? relay.segments_cached : null,
                relay_buffer_s: relay ? Math.round(relay.buffered_seconds) : null,
                relay_MB: relay ? Math.round(relay.buffered_MB * 10) / 10 : null
            };
        }""", label)

    rows = [snap("before pause")]
    page.evaluate("() => v.pause()")
    for i in range(6):
        page.wait_for_timeout(int(PAUSE * 1000 / 6))
        rows.append(snap("paused +%ds" % int(PAUSE * (i + 1) / 6)))

    page.evaluate("() => v.play()")
    page.wait_for_timeout(1500)
    rows.append(snap("1.5 s after resume"))
    page.wait_for_timeout(6000)
    rows.append(snap("7.5 s after resume"))
    page.wait_for_timeout(12000)
    rows.append(snap("20 s after resume"))

    print(json.dumps(rows, indent=2))
    br.close()
