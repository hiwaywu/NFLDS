"""Does a REAL stream play inside the ANDROID app, end to end?

    python tools/verify_android_live.py            # needs a booted device/emulator
    python tools/verify_android_live.py --shot docs/android_channel.png

Two live checks against `releases/NFLHub.apk` (no fixture video, no CDP):

  A. NFL TV:      adb ... --es chan 2  -> the hub relays the 480p backup channel and
                  the app's own <video> decodes it (the screenshot is not a black rectangle)
  B. game path:   adb ... --es url http://127.0.0.1:8795/?game=<fixture event>
                  -> the page runs watchGame -> /api/harvest -> a wrapper chain that
                     names a REAL manifest -> relay. Proof is the app's own log line
                     "relay started: <that manifest>".

The decoder is only exercised once (check A): a software-rendered emulator dies after a
few minutes of real video, and that is the emulator, not the app.
"""
import argparse
import io
import os
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import fixture_stream                                      # noqa: E402

SDK = os.environ.get("ANDROID_SDK") or os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk")
ADB = os.path.join(SDK, "platform-tools", "adb.exe")
APK = os.path.join(ROOT, "releases", "NFLHub.apk")
PKG = "com.nflds.app"
HUB = "http://127.0.0.1:8795"


def adb(*args, timeout=60, binary=False):
    """Never raise: a wedged adb must show up as empty output, not kill the run."""
    try:
        p = subprocess.run([ADB] + list(args), capture_output=True, timeout=timeout)
    except Exception:
        return b"" if binary else ""
    if binary:
        return p.stdout
    return (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")


def device_ok():
    return "device" in adb("devices", timeout=20).replace("List of devices attached", "")


def logcat(tail="400"):
    txt = adb("logcat", "-d", "-t", tail, "-v", "brief", timeout=25)
    if not txt:
        txt = adb("logcat", "-d", "-t", "150", "-v", "brief", timeout=25)
    return txt


def wait(cond, seconds=90, step=1.5):
    end = time.time() + seconds
    while time.time() < end:
        got = cond()
        if got:
            return got
        if not device_ok():
            return None
        time.sleep(step)
    return None


def picture_spread(png_bytes):
    """Standard deviation of the picture area: a black screen is flat, a stream is not."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("L")
    except Exception:
        return -1
    w, h = img.size
    crop = img.crop((int(w * 0.06), int(h * 0.12), int(w * 0.94), int(h * 0.42)))
    px = list(crop.getdata())
    if not px:
        return -1
    mean = sum(px) / float(len(px))
    return (sum((p - mean) ** 2 for p in px) / float(len(px))) ** 0.5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", default=os.path.join(ROOT, "docs", "android_channel.png"))
    ap.add_argument("--phase", choices=("all", "tv", "game"), default="all",
                    help="tv = NFL TV only, game = the harvest path only (no decode wait)")
    args = ap.parse_args()

    if not os.path.isfile(ADB):
        print("adb not found:", ADB)
        return 2
    if not os.path.isfile(APK):
        print("apk not found:", APK)
        return 2
    if not device_ok():
        print("no device/emulator attached")
        return 3
    wait(lambda: "1" in adb("shell", "getprop", "sys.boot_completed", timeout=20), 300, 2.0)

    results = []

    def check(name, ok, detail):
        results.append({"check": name, "ok": bool(ok), "detail": detail})
        print("%s  %-52s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)

    fx = fixture_stream.serve(0, "0.0.0.0")
    base = "http://10.0.2.2:%d" % fx.server_port
    try:
        out = adb("install", "-r", "-d", APK, timeout=240)
        if "Success" not in out:
            adb("uninstall", PKG)
            out = adb("install", "-r", "-d", APK, timeout=240)
        check("apk: installed", "Success" in out,
              (out.strip().splitlines()[-1] if out.strip() else "?")[:110])

        # ---------------------------------------------------------- A. NFL TV
        if args.phase in ("all", "tv"):
            adb("shell", "am", "force-stop", PKG)
            adb("logcat", "-c", timeout=20)
            adb("shell", "am", "start", "-n", PKG + "/.MainActivity", "--es", "chan", "2")
            relay = wait(lambda: next((l.strip() for l in logcat().splitlines()
                                       if "relay started:" in l), None), 150, 2.0)
            check("nfl tv: the app started a relay for the 480p channel", bool(relay),
                  (relay or "no 'relay started:' in logcat")[:170])
            time.sleep(14)
            shot = adb("exec-out", "screencap", "-p", timeout=90, binary=True)
            if shot:
                with open(args.shot, "wb") as fh:
                    fh.write(shot)
            spread = picture_spread(shot or b"")
            check("nfl tv: the picture is decoding in the app (not a black rectangle)",
                  spread > 6, "luminance sd %.1f  -> %s" % (spread, os.path.basename(args.shot)))

        # --------------------------------------------------- B. the game path
        if args.phase not in ("all", "game"):
            adb("shell", "am", "start", "-n", PKG + "/.MainActivity", timeout=40)
            raise SystemExit(0)
        adb("logcat", "-c", timeout=20)
        game = HUB + "/?game=" + urllib.parse.quote(base + "/chain2", safe="")
        adb("shell", "am", "start", "-n", PKG + "/.MainActivity", "--es", "url", game)
        relay2 = wait(lambda: next((l.strip() for l in logcat().splitlines()
                                    if "relay started:" in l
                                    and ("aynaott" in l or "akamaized" in l)), None), 180, 2.0)
        check("game: the page harvested the chain and relayed the real manifest", bool(relay2),
              (relay2 or "no relay line naming the real manifest")[:170])
        check("emulator: still alive at the end", device_ok(),
              "device answered" if device_ok() else "the emulator died (known: software decode)")
        adb("shell", "am", "start", "-n", PKG + "/.MainActivity", timeout=40)
    finally:
        fx.shutdown()

    passed = sum(1 for r in results if r["ok"])
    print("\n%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
