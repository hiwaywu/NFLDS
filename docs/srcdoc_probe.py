"""Read the srcdoc player shell and look for a media URL inside it."""
import json
import re
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else (
    "https://methstreams.st/event/m-atlanta-hawks-vs-washington-wizards-1105")

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=False,
                            args=["--window-position=-2400,-2400",
                                  "--autoplay-policy=no-user-gesture-required", "--mute-audio"])
    page = br.new_context(viewport={"width": 1280, "height": 800}).new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(6000)

    shells = page.eval_on_selector_all(
        "iframe",
        "els => els.map(e => ({src: e.src || '', srcdoc: e.getAttribute('srcdoc') || ''}))")
    inline = page.eval_on_selector_all(
        "script:not([src])", "els => els.map(e => e.textContent || '').join('\\n')")
    try:
        shot = r"C:\AI STUFF\DeepSeek\NBADS-main\docs\game_page.png"
        page.screenshot(path=shot)
    except Exception:
        pass
    br.close()

for i, f in enumerate(shells):
    s = f["srcdoc"] or ""
    print("iframe %d  srcdoc=%d  src=%s" % (i, len(s), f["src"][:80]))
    urls = sorted(set(re.findall(r"https?://[^\s\"'<>]+", s)))
    for u in urls[:15]:
        print("    ", u[:170])
    low = s.lower()
    for kw in ("m3u8", "jwplayer", "hls", "sources", "file:", "embed"):
        idx = low.find(kw)
        if idx >= 0:
            print("     [%s] %s" % (kw, s[max(0, idx - 100):idx + 200].replace("\n", " ")))

print("inline script bytes:", len(inline))
for u in sorted(set(re.findall(r"https?://[^\s\"'<>]+", inline)))[:20]:
    print("  inline:", u[:150])
for kw in ("m3u8", "jwplayer", "hls(", "sources", "srcdoc", "atob(", "eval("):
    idx = inline.lower().find(kw)
    if idx >= 0:
        print("  [%s] %s" % (kw, inline[max(0, idx - 120):idx + 240].replace("\n", " ")))
