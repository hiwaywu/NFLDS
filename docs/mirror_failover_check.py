"""Does a dead mirror get replaced automatically?

    python mirror_failover_check.py

Assumes the live feeds.txt temporarily starts with a broken entry.
"""
import json

from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=True,
                            args=["--mute-audio", "--autoplay-policy=no-user-gesture-required"])
    page = br.new_context(viewport={"width": 1000, "height": 800}).new_page()
    page.goto("http://127.0.0.1:8790/", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)

    print("channels:", page.evaluate("() => channels.map(c => c.name)"))

    # click the broken mirror, the way a user would
    page.evaluate("() => playChannel(channels[0])")

    seen = []
    for _ in range(24):                       # up to ~24 s
        page.wait_for_timeout(1000)
        st = page.evaluate("""() => ({
            title: (document.getElementById('nowTitle') || {}).textContent,
            playing: v.readyState >= 2 && v.currentTime > 0.2,
            t: Math.round(v.currentTime),
            mirror: (mirrorIdx + 1) + '/' + channels.length,
            tried: mirrorTried.size,
            reason: mirrorReason,
            toast: (document.getElementById('toast') || {}).textContent || ''
        })""")
        seen.append(st)
        if st["playing"]:
            break

    last = seen[-1]
    print(json.dumps({
        "final_title": last["title"],
        "playing": last["playing"],
        "currentTime": last["t"],
        "mirror_pos": last["mirror"],
        "tried": last["tried"],
        "reason": last["reason"],
        "transcript": [(s["title"], s["reason"], s["toast"][:60]) for s in seen[:6]],
    }, indent=2))
    page.screenshot(path=r"C:\AI STUFF\DeepSeek\NBADS-main\docs\mirror_failover.png")
    br.close()
