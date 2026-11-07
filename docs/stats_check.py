"""Why is the buffer pill empty? Check the relay stats fetch from the page."""
import json
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790/"

with sync_playwright() as pw:
    br = pw.chromium.launch(headless=True, args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"])
    page = br.new_context(viewport={"width": 1440, "height": 900}).new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    page.evaluate("() => playChannel(channels[0])")
    page.wait_for_timeout(6000)
    out = page.evaluate("""async () => {
        const r = {relayBase: relayBase, pill: document.getElementById('pillBuf').textContent,
                   pillVisible: document.getElementById('pillBuf').style.display,
                   nowTitle: document.getElementById('nowTitle').textContent};
        try {
            const raw = await fetch(relayBase + '/stats', {cache: 'no-store'});
            r.http = raw.status;
            r.body = (await raw.text()).slice(0, 220);
        } catch (e) { r.error = String(e); }
        return r;
    }""")
    print(json.dumps(out, indent=2, default=str))
    br.close()
