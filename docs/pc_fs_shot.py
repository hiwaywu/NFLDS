"""Screenshot of the hub page in fullscreen with a live channel, new code path."""
import json
import os
import sys

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8790/"
OUT = r"C:\AI STUFF\DeepSeek\NBADS-main\docs\pc_fullscreen.png"

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=True, args=["--mute-audio", "--autoplay-policy=no-user-gesture-required"])
    page = br.new_context(viewport={"width": 1680, "height": 1050}).new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    page.evaluate("() => playChannel(channels[0])")
    for _ in range(25):
        page.wait_for_timeout(1000)
        if page.evaluate("() => v.readyState >= 2 && v.currentTime > 1"):
            break
    page.click("#fs")
    page.wait_for_timeout(2500)
    info = page.evaluate("""() => ({
        fs: document.fullscreenElement ? document.fullscreenElement.id : null,
        fit: getComputedStyle(v).objectFit,
        box: [Math.round(v.getBoundingClientRect().width), Math.round(v.getBoundingClientRect().height)],
        src: [v.videoWidth, v.videoHeight],
        exit: getComputedStyle(document.getElementById('fsexit')).display
    })""")
    page.screenshot(path=OUT)
    print(json.dumps(info))
    br.close()
