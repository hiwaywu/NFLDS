"""Does a game page really play inside the ANDROID app, or does it still need a browser?

    python tools/verify_android_inapp.py             # needs a booted emulator/device
    python tools/verify_android_inapp.py --shot out.png

What it drives, on the real APK:

  1. installs releases/NFLHub.apk (uninstalling an old signature first, if needed)
  2. puts a gated fixture player page in front of the app's WebView with the
     `--es url` deep link (tools/fixture_stream.py, reachable as 10.0.2.2)
  3. the page opens its media window with window.open() -- the case that used to be
     swallowed and left a black screen
  4. the page beacons its own <video> state back to the fixture, because a phone's
     WebView cannot be inspected from a PC without CDP

Checks: the app really loaded the page (its own UA), the popup was kept in-app, the
main WebView navigated to it, the clip decoded and advanced on the device, and a
gesture-less navigation to an unknown host was refused (the ad-hijack shape).
"""
import argparse
import json
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


def adb(*args, **kw):
    cmd = [ADB] + list(args)
    timeout = kw.pop("timeout", 60)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def wait(cond, seconds=60, step=1.0):
    end = time.time() + seconds
    while time.time() < end:
        got = cond()
        if got:
            return got
        time.sleep(step)
    return None


def logcat(since=None):
    rc, out = adb("logcat", "-d", "-v", "brief", timeout=60)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", default=os.path.join(ROOT, "docs", "android_inapp_popup.png"))
    ap.add_argument("--keep-installed", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(ADB):
        print("adb not found at", ADB)
        return 2
    if not os.path.isfile(APK):
        print("apk not found at", APK)
        return 2

    results = []

    def check(name, ok, detail):
        results.append({"check": name, "ok": bool(ok), "detail": detail})
        print("%s  %-56s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)

    # ---------------------------------------------------------------- device
    rc, out = adb("devices")
    if "device" not in out.replace("List of devices attached", ""):
        print("no device/emulator attached:\n" + out)
        return 3
    booted = wait(lambda: "1" in adb("shell", "getprop", "sys.boot_completed")[1], 300, 2.0)
    check("device: booted", bool(booted), adb("shell", "getprop", "ro.product.model")[1].strip())

    fx = fixture_stream.serve(0, "0.0.0.0")
    fxport = fx.server_port
    BASE = "http://10.0.2.2:%d" % fxport
    print("fixture for the emulator:", BASE, flush=True)

    try:
        # -------------------------------------------------------------- install
        rc, out = adb("install", "-r", "-d", APK, timeout=240)
        if rc != 0 and ("INSTALL_FAILED_UPDATE_INCOMPATIBLE" in out
                        or "signatures do not match" in out
                        or "INSTALL_FAILED_ALREADY_EXISTS" in out):
            adb("uninstall", PKG)
            rc, out = adb("install", "-r", "-d", APK, timeout=240)
        check("apk: installed on the device", rc == 0, out.strip().splitlines()[-1][:120])
        if rc != 0:
            return 4

        adb("shell", "pm", "clear", PKG)
        adb("logcat", "-c")

        # ------------------------------------------- the popup-using player page
        before = len(fixture_stream.STATE["main"])
        adb("shell", "am", "start", "-n", PKG + "/.MainActivity",
            "--es", "url", BASE + "/main/2")
        loaded = wait(lambda: len(fixture_stream.STATE["main"]) > before, 45, 0.5)
        check("app: the page reached the device", bool(loaded),
              json.dumps((fixture_stream.STATE["main"][-1:] or [{}])[0])[:220])
        ua = (fixture_stream.STATE["main"][-1]["ua"] if loaded else "")
        check("app: the request came from the app's WebView", "NFLHubApp" in ua, ua[:120])

        def popup_report():
            for row in fixture_stream.STATE["report"]:
                if row.get("path") == "/popup" and float(row.get("t") or 0) > 0.4:
                    return row
            return None

        row = wait(popup_report, 25, 1.0)
        if not row:
            # one retry: the popup handoff is a set of callbacks, and a cold WebView
            # on an emulator can be slow enough for the first one to be dropped
            adb("shell", "am", "start", "-n", PKG + "/.MainActivity",
                "--es", "url", BASE + "/main/2")
            row = wait(popup_report, 40, 1.0)
        check("app: the popup page decoded video on the device", bool(row),
              json.dumps(row or {})[:220])

        logs = logcat()
        kept = [l for l in logs.splitlines() if "popup kept in-app" in l]
        check("app: window.open() was kept in-app", bool(kept),
              (kept[-1].strip() if kept else "no 'popup kept in-app' in logcat")[:200])
        navigated = [l for l in logs.splitlines()
                     if "page finished" in l and "/popup" in l]
        check("app: the main WebView became that window", bool(navigated),
              (navigated[-1].strip() if navigated else "main WebView never showed /popup")[:200])
        dropped = [l for l in logs.splitlines() if "ad blocked" in l]
        check("app: the fixture's own playback was not interrupted by the ad block",
              not [l for l in dropped if "10.0.2.2" in l], "%d ad hosts blocked" % len(dropped))

        # ------------------------------------------- an ad's window must be dropped
        def embed_reports():
            return [r for r in fixture_stream.STATE["report"] if r.get("path") == "/embed/player"]

        before_embed = len(embed_reports())
        adb("logcat", "-c")
        adb("shell", "am", "start", "-n", PKG + "/.MainActivity",
            "--es", "url", BASE + "/adwin")
        wait(lambda: len(embed_reports()) > before_embed, 45, 1.0)
        time.sleep(2)
        logs = logcat()
        dropped_popup = [l for l in logs.splitlines() if "popup dropped" in l]
        on_ad_page = [l for l in logs.splitlines()
                      if "page finished" in l and "ads.example.net" in l]
        check("ad: an ad's window.open() was dropped", bool(dropped_popup),
              (dropped_popup[-1].strip() if dropped_popup else "no 'popup dropped' in logcat")[:170])
        check("ad: the app stayed on the player", not on_ad_page,
              (on_ad_page[-1].strip() if on_ad_page else "no ads.example.net page load")[:170])
        check("ad: the player still decoded video while the ad fired",
              len(embed_reports()) > before_embed,
              json.dumps((embed_reports()[-1:] or [{}])[0])[:200])

        # --------------------------------- a tap on an ad overlay must not leave the app
        before_tap = len(embed_reports())
        adb("logcat", "-c")
        adb("shell", "am", "start", "-n", PKG + "/.MainActivity",
            "--es", "url", BASE + "/tapad")
        wait(lambda: len(embed_reports()) > before_tap, 45, 1.0)
        adb("shell", "input", "tap", "540", "1200")
        tap_blocked = wait(lambda: next((l.strip() for l in logcat().splitlines()
                                         if "nav blocked (not the player)" in l), None), 20, 1.0)
        stray_tap = [l for l in logcat().splitlines()
                     if "page finished" in l and "ads.example.net" in l]
        check("ad: a tap on an ad overlay did not leave the player",
              bool(tap_blocked) and not stray_tap,
              (tap_blocked[:150] if tap_blocked else "no 'nav blocked (not the player)' in logcat"))

        # ---------------------------------------------------- the ad-hijack shape
        adb("logcat", "-c")
        adb("shell", "am", "start", "-n", PKG + "/.MainActivity",
            "--es", "url", BASE + "/adtest")

        def nav_blocked_line():
            for line in logcat().splitlines():
                if "nav blocked" in line:
                    return line.strip()
            return None

        blocked = wait(nav_blocked_line, 30, 1.0)

        def left_the_fixture():
            logs2 = logcat()
            for line in logs2.splitlines():
                if "page finished" in line and "example.com" in line:
                    return line
            return None
        stray = left_the_fixture()
        check("app: a gesture-less jump to an unknown host is refused",
              bool(blocked) and not stray,
              (blocked[:140]) if blocked else "no 'nav blocked' in logcat")

        # ------------------------------------------------------------- screenshot
        if args.shot:
            # a *new* playback, so the frame on screen is a moving picture and not
            # the player's poster button
            baseline = len([r for r in fixture_stream.STATE["report"]
                            if r.get("path") == "/popup"])
            adb("shell", "am", "start", "-n", PKG + "/.MainActivity",
                "--es", "url", BASE + "/main/2")
            wait(lambda: len([r for r in fixture_stream.STATE["report"]
                              if r.get("path") == "/popup"]) > baseline, 40, 1.0)
            time.sleep(3)
            with open(args.shot, "wb") as fh:
                p = subprocess.run([ADB, "exec-out", "screencap", "-p"],
                                   capture_output=True, timeout=60)
                fh.write(p.stdout)
            check("app: screenshot captured", os.path.getsize(args.shot) > 20000,
                  "%s (%d bytes)" % (args.shot, os.path.getsize(args.shot)))

        if not args.keep_installed:
            pass                      # leave it: an uninstall would drop the app's data
    finally:
        fx.shutdown()

    passed = sum(1 for r in results if r["ok"])
    print("\n%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
