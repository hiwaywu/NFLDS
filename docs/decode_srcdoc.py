"""Dump the player srcdoc, then statically decode what it hides."""
import base64
import json
import re
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else (
    "https://methstreams.st/event/m-atlanta-hawks-vs-washington-wizards-1105")
OUT = r"C:\AI STUFF\DeepSeek\NBADS-main\docs\player_srcdoc.html"

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=False,
                            args=["--window-position=-2400,-2400",
                                  "--autoplay-policy=no-user-gesture-required", "--mute-audio"])
    page = br.new_context(viewport={"width": 1280, "height": 800}).new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(6000)
    shells = page.eval_on_selector_all(
        "iframe", "els => els.map(e => e.getAttribute('srcdoc') || '')")
    br.close()

biggest = max(shells, key=len) if shells else ""
with open(OUT, "w", encoding="utf-8", errors="replace") as fh:
    fh.write(biggest)
print("srcdoc bytes:", len(biggest), "->", OUT)

low = biggest.lower()
for kw in ("atob(", "eval(", "jwplayer", "sources", "m3u8", "unpack", "function(p,a,c,k,e,d)"):
    print("  has %-28s %s" % (kw, kw in low))

# every decent base64 blob: decode and look for anything media-ish
blobs = re.findall(r"[A-Za-z0-9+/]{80,}={0,2}", biggest)
print("base64 candidates:", len(blobs))
seen = set()
for b in blobs[:40]:
    try:
        raw = base64.b64decode(b + "=" * (-len(b) % 4))
    except Exception:
        continue
    try:
        txt = raw.decode("utf-8")
    except Exception:
        continue
    if txt in seen:
        continue
    seen.add(txt)
    hits = re.findall(r"https?://[^\s\"'<>\\]+", txt)
    if hits or "m3u8" in txt.lower():
        print("---- decoded blob (%d bytes) ----" % len(txt))
        for h in sorted(set(hits))[:10]:
            print("   ", h[:180])
        idx = txt.lower().find("m3u8")
        if idx >= 0:
            print("    ...", txt[max(0, idx - 160):idx + 160].replace("\n", " "))
