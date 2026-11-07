"""Play a harvested game manifest in OUR player and prove it.

    python game_in_player.py <event-page-url>
"""
import json
import sys

from playwright.sync_api import sync_playwright

PAGE = sys.argv[1] if len(sys.argv) > 1 else (
    "https://methstreams.st/event/m-atlanta-hawks-vs-washington-wizards-1105")

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=True,
                            args=["--mute-audio", "--autoplay-policy=no-user-gesture-required"])
    page = br.new_context(viewport={"width": 1200, "height": 800}).new_page()
    page.goto("http://127.0.0.1:8790/", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    h = page.evaluate("""async (u) => {
        const r = await fetch('/api/harvest?u=' + encodeURIComponent(u) + '&fresh=1');
        return await r.json();
    }""", PAGE)
    print("harvest:", json.dumps({k: h.get(k) for k in ("ok", "m3u8", "referer")}))

    if not h.get("ok"):
        print("no manifest"); br.close(); sys.exit(1)

    page.evaluate("(m) => playChannel({name: 'game (harvested)', url: m})", h["m3u8"])
    for _ in range(30):
        page.wait_for_timeout(1000)
        st = page.evaluate("() => ({rs: v.readyState, t: v.currentTime, w: v.videoWidth, h: v.videoHeight})")
        if st["rs"] >= 2 and st["t"] > 1:
            break

    page.wait_for_timeout(4000)
    final = page.evaluate("""async () => {
        let s = null;
        try { s = await fetch(relayBase + '/stats?_=' + Date.now()).then(r => r.json()); } catch(e){}
        return {
            title: document.getElementById('nowTitle').textContent,
            readyState: v.readyState, t: Math.round(v.currentTime * 10) / 10,
            size: [v.videoWidth, v.videoHeight], paused: v.paused,
            latency: (hls && typeof hls.latency === 'number') ? Math.round(hls.latency * 10) / 10 : null,
            relay: s ? {segs: s.segments_cached, secs: s.buffered_seconds,
                        mbps: Math.round(s.upstream_Mbps), err: s.last_error} : null
        };
    }""")
    print(json.dumps(final, indent=2))
    page.screenshot(path=r"C:\AI STUFF\DeepSeek\NBADS-main\docs\game_in_player.png", full_page=False)
    br.close()
