"""Click a game row on whatever page the hub serves and report what happens.

    python click_game.py [game-name-substring]
"""
import json
import sys

from playwright.sync_api import sync_playwright

WANT = sys.argv[1] if len(sys.argv) > 1 else ""

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=True,
                            args=["--mute-audio", "--autoplay-policy=no-user-gesture-required"])
    p = br.new_context(viewport={"width": 1200, "height": 800}).new_page()
    p.goto("http://127.0.0.1:8790/", wait_until="domcontentloaded", timeout=30000)

    # their page fills the list asynchronously; wait for it instead of guessing
    for _ in range(40):
        p.wait_for_timeout(500)
        if p.evaluate("() => (typeof games !== 'undefined') && games.length > 0"):
            break

    picked = p.evaluate("""(want) => {
        const g = (want ? games.find(x => x.name.includes(want)) : null)
               || games.find(x => x.stream && x.state === 'in')
               || games.find(x => x.stream);
        if (!g) return null;
        watchGame(g);
        return g.name;
    }""", WANT)
    print("picked:", picked)

    for _ in range(90):
        p.wait_for_timeout(1000)
        st = p.evaluate("() => ({rs: v.readyState, t: v.currentTime, title: document.getElementById('nowTitle').textContent})")
        if st["rs"] >= 2 and st["t"] > 1:
            break
    p.wait_for_timeout(2500)

    out = p.evaluate("""async () => {
        let s = null;
        try { s = await fetch(relayBase + '/stats?_=' + Date.now()).then(r => r.json()); } catch(e){}
        return {
            title: document.getElementById('nowTitle').textContent,
            readyState: v.readyState, t: Math.round(v.currentTime * 10) / 10,
            size: [v.videoWidth, v.videoHeight],
            iframeMode: document.body.classList.contains('iframeMode'),
            relay: s ? {segs: s.segments_cached, mbps: Math.round(s.upstream_Mbps),
                        ads_skipped: s.ads_skipped, src: (s.source || '').slice(0, 80)} : null
        };
    }""")
    print(json.dumps(out, indent=2))
    p.screenshot(path=r"C:\AI STUFF\DeepSeek\NBADS-main\docs\merged_click.png")
    br.close()
