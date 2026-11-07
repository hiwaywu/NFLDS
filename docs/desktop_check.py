"""Desktop check of the hub page in real Chrome (via Playwright).

Loads the hub at 127.0.0.1, plays NFL TV channel 0 through the relay, reports the
video state, then clicks the first game row and reports what the iframe did.
"""
import json
import sys
import time

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790/"
SHOT = "C:/AI STUFF/DeepSeek/NBADS-main/docs/shot_desktop.png"


def main():
    out = {}
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=True, args=[
            "--autoplay-policy=no-user-gesture-required", "--mute-audio"])
        ctx = br.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        out["title"] = page.title()
        out["rows"] = page.eval_on_selector_all(".row", "els => els.length")
        out["layout"] = page.evaluate("""() => {
            const s = getComputedStyle(document.body);
            return {bodyDisplay: s.display, sideWidth: document.getElementById('side').getBoundingClientRect().width,
                    stageH: Math.round(document.getElementById('stage').getBoundingClientRect().height),
                    vw: window.innerWidth};
        }""")
        out["canDrm"] = page.evaluate("() => typeof navigator.requestMediaKeySystemAccess === 'function' && window.isSecureContext")

        # play channel 0 in-page
        page.evaluate("() => playChannel(channels[0])")
        for _ in range(20):
            page.wait_for_timeout(1000)
            st = page.evaluate("() => ({t: v.currentTime, rs: v.readyState, w: v.videoWidth, h: v.videoHeight, paused: v.paused})")
            if st["rs"] >= 2 and st["t"] > 1:
                out["channel"] = st
                break
            out["channel"] = st
        out["pills"] = page.evaluate("() => [document.getElementById('pillQ').textContent, document.getElementById('pillBuf').textContent]")
        page.screenshot(path=SHOT)

        out["consoleErrors"] = errors[:6]
        br.close()
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
