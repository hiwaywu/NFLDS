"""Rewind / live / disk-DVR behaviour on the live channel.

    python dvr_check.py
"""
import json
import os
import shutil
import subprocess
import time

from playwright.sync_api import sync_playwright

DVR = r"C:\AI STUFF\NFLHub\dvr"

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=True,
                            args=["--mute-audio", "--autoplay-policy=no-user-gesture-required"])
    page = br.new_context(viewport={"width": 1000, "height": 700}).new_page()
    page.goto("http://127.0.0.1:8790/", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    page.evaluate("() => playChannel(channels[0])")
    for _ in range(40):
        page.wait_for_timeout(1000)
        if page.evaluate("() => v.readyState >= 2 && v.currentTime > 1"):
            break
    page.wait_for_timeout(20000)            # let the RAM ring fill and spill

    state = lambda: page.evaluate("""async () => {
        let r = null;
        try { r = await fetch(relayBase + '/stats?_=' + Date.now()).then(x => x.json()); } catch(e){}
        return {
            t: Math.round(v.currentTime * 10) / 10,
            latency: (hls && typeof hls.latency === 'number') ? Math.round(hls.latency * 10) / 10 : null,
            buffered_window_s: (v.buffered.length ? Math.round((v.buffered.end(0) - v.buffered.start(0)) * 10) / 10 : null),
            pill: (document.getElementById('pillBuf') || {}).textContent,
            dvr_hold: dvrHold,
            dvr_s: r ? r.dvr_seconds_stored : null,
            dvr_MB: r ? r.dvr_MB : null
        };
    }""")

    before = state()
    page.click("#rw30")
    page.wait_for_timeout(2500)
    after30 = state()

    page.click("#rw2m")
    page.wait_for_timeout(2500)
    after2m = state()

    page.click("#live")
    page.wait_for_timeout(3000)
    backlive = state()

    print(json.dumps({
        "before": before,
        "after rw30": after30,
        "after rw2m": after2m,
        "after live": backlive,
        "delta rw30 (s)": round(after30["t"] - before["t"], 1) if after30["t"] and before["t"] else None,
    }, indent=2))
    page.screenshot(path=r"C:\AI STUFF\DeepSeek\NBADS-main\docs\dvr_controls.png")
    br.close()

# disk footprint, then prove it is wiped when the relay goes away
def dvr_usage():
    if not os.path.isdir(DVR):
        return 0, 0
    files = [os.path.join(dp, f) for dp, _, fs in os.walk(DVR) for f in fs]
    return len(files), sum(os.path.getsize(f) for f in files)

n, b = dvr_usage()
print(json.dumps({"dvr_files": n, "dvr_MB": round(b / 1e6, 1), "dvr_dir": DVR}))

out = subprocess.run(["powershell", "-Command",
                      "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
                      "Where-Object { $_.CommandLine -match 'relay\\.py' } | "
                      "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }"],
                     capture_output=True, text=True)
print("relays killed:", out.stdout.strip() or "(none)")
time.sleep(4)
print("dvr dir after stop:", os.path.isdir(DVR), "files:", len(os.listdir(DVR)) if os.path.isdir(DVR) else 0)
