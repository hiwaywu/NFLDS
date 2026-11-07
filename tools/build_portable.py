"""Build releases/NFLHub-portable.zip: the PC app plus its own interpreter.

    python tools/build_portable.py [--zip]

Layout (what the launcher expects: it looks for ..\\..\\python\\pythonw.exe):

    START.bat
    README-PORTABLE.txt
    python\\                     embeddable 3.13 + requests/urllib3
    nfl\\hub\\NFLHub.exe + hub.py, index.html, hls.min.js, probes
    nfl\\relay.py, feeds.txt
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "build-portable")
ZIP = os.path.join(ROOT, "releases", "NFLHub-portable.zip")
PY_URL = "https://www.python.org/ftp/python/3.13.7/python-3.13.7-embed-amd64.zip"
PY_PTH = "python313._pth"

START_BAT = """@echo off
rem NFL Hub -- portable. Double-click this: no Python install needed.
cd /d "%~dp0nfl\\hub"
start "" "NFLHub.exe"
if errorlevel 1 pause
"""

PORTABLE_README = """NFL Hub -- portable bundle
==========================

Double-click START.bat. That is the whole install.

What is in here
---------------
START.bat                 starts the app window
python\\                   its own Python 3.13 (embeddable) with requests/urllib3
nfl\\hub\\NFLHub.exe        the launcher (23 KB)
nfl\\hub\\hub.py ...        the server, the page, hls.js, the probes
nfl\\relay.py, feeds.txt   the buffering relay and the channel list

What it still wants from Windows
--------------------------------
- a browser: Chrome, Chromium, Firefox or Brave (the window IS that browser in
  --app mode; Firefox gets its own profile). Force one with browser.txt, or
  `NFLHub.exe --browser firefox`.
- .NET Framework 4.x -- part of Windows 10/11.

What changed in 2.4
-------------------
Games play inside the app window now. The hub proxies the site's own player page
with the site's Referer/Origin, forwards the Widevine licence POST, rehosts every
nested player frame and turns window.open() into the picture in the same frame.
Measured: 17/17 checks in `python tools\\verify_inapp_game.py` (repo).

Troubleshooting
---------------
- Nothing happens on double-click: look at
  %LOCALAPPDATA%\\NFLHubPC\\launcher.log -- it logs the browser it picked and
  every decision it made.
- Unzip somewhere writable (Desktop, Documents). Not Program Files: the window
  profile is written next to the exe.
- A stream stays black: long-press the "nfl hub" title in the page for
  diagnostics (secure context, hls.js, Widevine/EME, relay state).
- Close the window and everything stops: relays, the Python server, nothing left
  running.

add Playwright only if you care about headless game probing:
    python\\python.exe -m pip install playwright
"""


def run(*cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        sys.stderr.write((p.stdout or "") + (p.stderr or ""))
        raise SystemExit("failed: %s" % " ".join(cmd))
    return p.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", action="store_true", help="also write the zip and its size")
    args = ap.parse_args()

    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    # --- python ---------------------------------------------------------
    py = os.path.join(OUT, "python")
    with tempfile.TemporaryDirectory() as tmp:
        zpath = os.path.join(tmp, "py.zip")
        print("downloading", PY_URL)
        urllib.request.urlretrieve(PY_URL, zpath)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(py)
    pth = os.path.join(py, PY_PTH)
    if os.path.isfile(pth):
        with open(pth, "r", encoding="utf-8") as fh:
            lines = [l.rstrip("\n") for l in fh]
        if "Lib\\site-packages" not in lines:
            lines.append("Lib\\site-packages")
        if "import site" not in lines:
            lines.append("import site")
        with open(pth, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    site = os.path.join(py, "Lib", "site-packages")
    os.makedirs(site, exist_ok=True)
    print("installing requests + urllib3 into the bundle")
    run(sys.executable, "-m", "pip", "install", "--quiet", "--no-input",
        "--target", site, "requests", "urllib3")

    # --- the app --------------------------------------------------------
    hub_src = os.path.join(ROOT, "nfl", "hub")
    hub_dst = os.path.join(OUT, "nfl", "hub")
    os.makedirs(hub_dst)
    for name in ("NFLHub.exe", "hub.py", "index.html", "hls.min.js", "shaka.js",
                 "probe_play.py", "find_stream.py", "harvest.py", "NFLHUB.bat", "FIREWALL.bat"):
        src = os.path.join(hub_src, name)
        if os.path.isfile(src):
            shutil.copy2(src, hub_dst)
    clean = os.path.join(hub_src, "clean")
    if os.path.isdir(clean):
        shutil.copytree(clean, os.path.join(hub_dst, "clean"))
    for name in ("relay.py", "feeds.txt"):
        shutil.copy2(os.path.join(ROOT, "nfl", name), os.path.join(OUT, "nfl", name))
    with open(os.path.join(OUT, "START.bat"), "w", newline="\r\n") as fh:
        fh.write(START_BAT)
    with open(os.path.join(OUT, "README-PORTABLE.txt"), "w", newline="\r\n") as fh:
        fh.write(PORTABLE_README)

    # --- prove it runs without the system interpreter -------------------
    probe = subprocess.run([os.path.join(py, "python.exe"), "-c",
                            "import requests, urllib3, ssl; print('bundle python ok', requests.__version__)"],
                           capture_output=True, text=True)
    print(probe.stdout.strip() or probe.stderr.strip()[:300])

    total = sum(os.path.getsize(os.path.join(d, f))
                for d, _, fs in os.walk(OUT) for f in fs)
    print("bundle dir: %.1f MB" % (total / 1048576.0))

    if args.zip:
        os.makedirs(os.path.dirname(ZIP), exist_ok=True)
        if os.path.isfile(ZIP):
            os.remove(ZIP)
        with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
            for d, _, fs in os.walk(OUT):
                for f in fs:
                    full = os.path.join(d, f)
                    z.write(full, os.path.relpath(full, OUT))
        print("wrote %s (%.1f MB)" % (ZIP, os.path.getsize(ZIP) / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
