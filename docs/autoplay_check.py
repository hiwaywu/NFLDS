"""Click a game on the PC page and check the proxied player auto-starts.

    python autoplay_check.py [game-name-substring]
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
    p.wait_for_timeout(6000)

    src = p.evaluate("() => (document.getElementById('pf') || {}).src || ''")
    print("frame src:", src[:120])

    state = {}
    for _ in range(20):
        p.wait_for_timeout(1000)
        try:
            state = p.evaluate("""() => {
                const f = document.getElementById('pf');
                const d = f && f.contentDocument;
                if (!d) return {same_origin: false};
                const v = d.querySelector('video');
                return {
                    same_origin: true,
                    autoplay: d.documentElement.getAttribute('data-nbaautoplay'),
                    videos: d.querySelectorAll('video').length,
                    title: (d.title || '').slice(0, 60),
                    playing: v ? {rs: v.readyState, muted: v.muted, t: Math.round(v.currentTime)} : null
                };
            }""")
        except Exception as e:
            state = {"error": str(e)[:120]}
        if state.get("autoplay") == "playing":
            break
    print(json.dumps(state, indent=2))
    p.screenshot(path=r"C:\AI STUFF\DeepSeek\NBADS-main\docs\autoplay_proof.png")
    br.close()
