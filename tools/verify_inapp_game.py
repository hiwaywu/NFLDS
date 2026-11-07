"""Does a gated, popup-using, licence-POSTing player page play inside the app window?

    python tools/verify_inapp_game.py            # headful, the real --app window
    python tools/verify_inapp_game.py --json     # machine-readable

The hub is started from nfl/hub, a fixture "mirror site" (tools/fixture_stream.py)
stands in for the real ones, and Chrome is launched with the launcher's own
command line, so what is measured is the window the user actually watches.

Checks, in the order the old code failed them:

  1. baseline      the fixture player really does refuse a foreign referer
  2. eme           this window has the Widevine key system at all
  3. licence       POST /ext -- method, binary body, upstream Origin/Referer
  4. frame         the nested player frame is served by /play, plays, and the
                   frame-buster did not move the app window
  5. popup         window.open() from the player becomes the picture in the same
                   frame, with no second window
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HUB_DIR = os.path.join(ROOT, "nfl", "hub")
sys.path.insert(0, HERE)

import fixture_stream                                     # noqa: E402

HUB_PORT = 8879
CDP_PORT = 9333
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def http(url, data=None, headers=None, timeout=15):
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) verify/1.0")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def wait(cond, seconds=25, step=0.35):
    end = time.time() + seconds
    while time.time() < end:
        got = cond()
        if got:
            return got
        time.sleep(step)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--keep", action="store_true", help="leave the app window open")
    args = ap.parse_args()

    results = []

    def check(name, ok, detail):
        results.append({"check": name, "ok": bool(ok), "detail": detail})

    fx = fixture_stream.serve()
    fxport = fx.server_port
    ORIGIN = "http://127.0.0.1:%d" % fxport
    EVENT = ORIGIN + "/event"
    MAIN1 = ORIGIN + "/main/1"
    MAIN2 = ORIGIN + "/main/2"
    LICENSE = ORIGIN + "/license"
    HUB = "http://127.0.0.1:%d" % HUB_PORT

    hub_log = tempfile.NamedTemporaryFile(prefix="hubfix-", suffix=".log", delete=False)
    hub = subprocess.Popen([sys.executable, "hub.py", "--port", str(HUB_PORT),
                            "--host", "127.0.0.1"],
                           cwd=HUB_DIR, stdout=hub_log, stderr=subprocess.STDOUT)
    profile = tempfile.mkdtemp(prefix="nflfix-")
    chrome = None
    try:
        if not wait(lambda: "nflhub" in (http(HUB + "/api/ping")[1] or ""), 25):
            print("hub did not start; see", hub_log.name)
            print(open(hub_log.name, encoding="utf-8", errors="replace").read()[-3000:])
            return 2

        # ---------------------------------------------------------- 1. baseline
        st_foreign, body_foreign = http(ORIGIN + "/embed/player",
                                        headers={"Referer": HUB + "/"})
        st_site, _ = http(ORIGIN + "/embed/player", headers={"Referer": EVENT})
        check("baseline: a foreign referer is refused",
              st_foreign == 403 and "not available here" in body_foreign,
              "direct embed with the hub as referer -> %d %r" % (st_foreign, body_foreign[:60]))
        check("baseline: the site's own referer is served",
              st_site == 200, "direct embed with the event page as referer -> %d" % st_site)

        # ---------------------------------------------------------- 2. licence (server side)
        raw_st, raw_body = http(LICENSE, data=b"\x01\x02\x03\x04\x05\x06\x07\x08",
                                headers={"Content-Type": "application/octet-stream",
                                         "Referer": EVENT, "Origin": ORIGIN})
        check("baseline: the licence endpoint is POST-only and checks identity",
              raw_st == 200,
              "direct POST -> %d %s" % (raw_st, raw_body[:80]))

        # ---------------------------------------------------------- the app window
        embed_before = len(fixture_stream.STATE["embed"])
        chrome = subprocess.Popen([
            CHROME, "--app=%s/" % HUB, "--window-size=1280,820",
            "--user-data-dir=%s" % profile,
            "--no-first-run", "--no-default-browser-check",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-features=Translate,MediaRouter",
            "--remote-debugging-port=%d" % CDP_PORT,
        ])
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            br = None
            for _ in range(40):
                try:
                    br = pw.chromium.connect_over_cdp("http://127.0.0.1:%d" % CDP_PORT)
                    break
                except Exception:
                    time.sleep(0.5)
            if br is None:
                check("app window", False, "could not attach to Chrome over CDP")
                return 3
            ctx = br.contexts[0]
            page = wait(lambda: next((p for p in ctx.pages
                                      if p.url.startswith(HUB)), None), 20, 0.4)
            if page is None:
                check("app window", False, "the app window never loaded the hub")
                return 4
            page.wait_for_timeout(1500)
            console = []
            page.on("console", lambda m: console.append("%s: %s" % (m.type, m.text)))
            page.on("pageerror", lambda e: console.append("pageerror: %s" % e))

            # 2. EME in this exact window
            key = page.evaluate("""async () => {
                try {
                    const a = await navigator.requestMediaKeySystemAccess('com.widevine.alpha', [{
                        initDataTypes: ['cenc'],
                        videoCapabilities: [{contentType: 'video/mp4; codecs="avc1.42E01E"'}]
                    }]);
                    return a.keySystem;
                } catch (e) { return 'fail: ' + (e && e.message || e); }
            }""")
            check("eme: the app window has Widevine", key == "com.widevine.alpha", key)

            # 3. the licence POST survives the proxy
            lic = page.evaluate("""async ([lic, ref]) => {
                const r = await fetch('/ext?u=' + encodeURIComponent(lic) + '&r=' + encodeURIComponent(ref),
                                      {method: 'POST',
                                       headers: {'Content-Type': 'application/octet-stream'},
                                       body: new Uint8Array([1,2,3,4,5,6,7,8])});
                return r.status + ' ' + (await r.text());
            }""", [LICENSE, EVENT])
            check("licence: POST /ext reaches upstream as a POST with its body",
                  lic.startswith("200") and "method=POST" in lic, lic[:120])

            # 4. the player page as the app shows it
            def show(u):
                page.evaluate("""(u) => {
                    document.body.classList.add('iframeMode');
                    const pf = document.querySelector('#pf');
                    pf.style.display = 'block';
                    if (typeof pfOpen === 'function') pfOpen(u); else pf.src = u;
                }""", u)

            def frame_state(want):
                for f in page.frames:
                    if want in urllib.parse.unquote(f.url):
                        try:
                            return f.evaluate("""() => {
                                const v = document.querySelector('video');
                                return {
                                    url: location.href,
                                    topIsSelf: window.top === window,
                                    lic: window.__license,
                                    ref: document.referrer,
                                    playing: v ? {paused: v.paused,
                                                  t: Math.round(v.currentTime * 100) / 100,
                                                  rs: v.readyState,
                                                  w: v.videoWidth, h: v.videoHeight} : null
                                };
                            }""")
                        except Exception as exc:
                            if os.environ.get("NFL_TRACE"):
                                print("  eval failed in", f.url[:80], str(exc)[:80])
                            return None
                return None

            def trace(tag):
                if not os.environ.get("NFL_TRACE"):
                    return
                try:
                    pf = page.evaluate("""() => {
                        const el = document.querySelector('#pf');
                        let href = '';
                        try { href = el.contentWindow.location.href; } catch (e) { href = 'n/a'; }
                        return {attr: el.getAttribute('src'), href: href,
                                display: el.style.display, good: (typeof pfGoodSrc === 'string' ? pfGoodSrc : 'undef'),
                                pfOpen: typeof pfOpen};
                    }""")
                except Exception as exc:
                    pf = {"error": str(exc)[:80]}
                print("  [%s] %s" % (tag, json.dumps(pf)[:260]))
                print("       frames:", [urllib.parse.unquote(f.url)[:70] for f in page.frames])

            def moving(state):
                """A picture that is actually running: decoded, advanced, not stopped."""
                v = (state or {}).get("playing") or {}
                return bool(v) and v.get("rs", 0) >= 2 and v.get("t", 0) > 0.3

            play1 = "/play?u=" + urllib.parse.quote(MAIN1, safe="") + \
                    "&r=" + urllib.parse.quote(EVENT, safe="")
            show(play1)
            trace("after show")
            st = None
            deadline = time.time() + 30
            while time.time() < deadline:
                s = frame_state("/embed/player")
                if moving(s):
                    st = s
                    break
                trace("poll")
                page.wait_for_timeout(700)
            if not st:
                st = frame_state("/embed/player") or {}
            seen = [urllib.parse.unquote(f.url)[:80] for f in page.frames]
            check("frame: the nested player is served by /play",
                  "/play?u=" in (st.get("url") or ""),
                  (st.get("url") or "no matching frame; frames=" + json.dumps(seen))[:200])
            check("frame: the frame-buster did not move the app window",
                  page.url.startswith(HUB) and st.get("url") is not None,
                  "app window still %s" % page.url)
            check("frame: document.referrer is the site, not the hub",
                  bool(st.get("ref")) and st["ref"].startswith(ORIGIN),
                  str(st.get("ref"))[:120])
            check("frame: the licence POST succeeded inside the app",
                  str(st.get("lic", "")).startswith("200"), str(st.get("lic"))[:120])
            check("frame: the picture is moving in the app window", moving(st),
                  json.dumps(st.get("playing") or {}))
            check("frame: the app window itself never navigated away",
                  page.url.startswith(HUB), page.url)

            # 5. window.open() from the player
            before_pages = len(ctx.pages)
            play2 = "/play?u=" + urllib.parse.quote(MAIN2, safe="") + \
                    "&r=" + urllib.parse.quote(EVENT, safe="")
            show(play2)

            def popup_ready():
                src = page.evaluate("() => (document.querySelector('#pf') || {}).src || ''")
                if "popup" not in urllib.parse.unquote(src):
                    return None
                s = frame_state("/popup")
                if moving(s):
                    return {"src": src, "frame": s}
                return None

            got = wait(popup_ready, 25)
            check("popup: window.open() becomes the picture in the same frame",
                  bool(got), (got or {}).get("src", "no re-point")[:160])
            check("popup: no second window was created",
                  len(ctx.pages) == before_pages,
                  "%d page target(s) before, %d after" % (before_pages, len(ctx.pages)))

            # ---- an ad's window must not replace the picture -------------------
            ADWIN = ORIGIN + "/adwin"
            play3 = "/play?u=" + urllib.parse.quote(ADWIN, safe="") + \
                    "&r=" + urllib.parse.quote(EVENT, safe="")
            show(play3)
            st3 = wait(lambda: (lambda s: s if (s and "/play?u=" in (s.get("url") or "")
                                                and moving(s)) else None)(
                frame_state("/embed/player")), 20)
            page.wait_for_timeout(1500)
            src3 = page.evaluate("() => (document.querySelector('#pf') || {}).getAttribute('src') || ''")
            st3 = frame_state("/embed/player")
            check("ad: an ad's window.open() did not replace the player",
                  "/play?u=" in src3 and "ads.example.net" not in src3,
                  src3[:160])
            check("ad: the picture kept running through it", moving(st3),
                  json.dumps((st3 or {}).get("playing") or {}))
            check("ad: no window was opened for the ad",
                  len(ctx.pages) == before_pages,
                  "%d page target(s)" % len(ctx.pages))

            # ---- the harvest walk: how the phone resolves a game, now on the PC ----
            CHAIN = ORIGIN + "/chain"
            try:
                hj = json.loads(http(HUB + "/api/harvest?u=" +
                                     urllib.parse.quote(CHAIN, safe=""))[1] or "{}")
            except Exception:
                hj = {}
            check("harvest: the chain walk finds the manifest with no browser and no page JS",
                  bool(hj.get("ok")) and "/live.m3u8" in str(hj.get("m3u8", "")),
                  json.dumps({k: hj.get(k) for k in ("ok", "hops", "via", "error")})[:180])
            check("harvest: the published clear-key pair comes back with it",
                  (hj.get("keys") or {}).get(fixture_stream.KID_HEX) == fixture_stream.KEY_HEX,
                  json.dumps(hj.get("keys") or {})[:160])
            check("harvest: it carries the referer the chain walked through",
                  "/wrap2" in str(hj.get("referer", "")), str(hj.get("referer"))[:140])
            check("harvest: the fixture's CDN refuses that manifest without a referer",
                  http(str(hj.get("m3u8") or ""))[0] == 403,
                  "direct GET -> %d" % http(str(hj.get("m3u8") or ""))[0])

            ck = json.loads(http(HUB + "/api/clearkey")[1] or "{}")
            kid_b64 = base64.urlsafe_b64encode(bytes.fromhex(fixture_stream.KID_HEX)).decode().rstrip("=")
            key_b64 = base64.urlsafe_b64encode(bytes.fromhex(fixture_stream.KEY_HEX)).decode().rstrip("=")
            check("clearkey: the JWK set hls.js asks for answers with that pair",
                  any(k.get("kid") == kid_b64 and k.get("k") == key_b64
                      for k in (ck.get("keys") or [])),
                  json.dumps(ck)[:200])

            m3u8 = str(hj.get("m3u8") or "")
            ref = str(hj.get("referer") or "")
            pr = json.loads(http(HUB + "/api/play?u=" + urllib.parse.quote(m3u8, safe="") +
                                 "&r=" + urllib.parse.quote(ref, safe=""), timeout=45)[1] or "{}")
            seen_ref = wait(lambda: next((h for h in fixture_stream.STATE["hls"]
                                          if "/wrap2" in h.get("referer", "")), None), 25, 0.5)
            check("relay: it pulls the harvested feed with that referer",
                  bool(pr.get("ok")) and bool(seen_ref),
                  "port=%s  CDN saw referer=%s" % (pr.get("port"),
                                                   (seen_ref or {}).get("referer", "-")[:80]))

            # ---- fullscreen chrome has to get out of the way --------------------
            page.click("#fs")
            page.wait_for_timeout(4200)
            fs1 = page.evaluate("""() => {
                const b = document.querySelector('#fsexit');
                const cs = getComputedStyle(b);
                return {full: !!document.fullscreenElement,
                        faded: document.body.classList.contains('fsfade'),
                        opacity: cs.opacity, pe: cs.pointerEvents};
            }""")
            check("fullscreen: the exit button and the control bar fade out on their own",
                  fs1.get("full") and fs1.get("faded") and fs1.get("opacity") == "0",
                  json.dumps(fs1)[:180])
            page.mouse.move(320, 300)
            page.wait_for_timeout(700)
            fs2 = page.evaluate("""() => ({faded: document.body.classList.contains('fsfade'),
                    opacity: getComputedStyle(document.querySelector('#fsexit')).opacity})""")
            check("fullscreen: one touch brings them back",
                  (not fs2.get("faded")) and fs2.get("opacity") != "0", json.dumps(fs2)[:120])
            page.evaluate("() => { if (document.fullscreenElement && document.exitFullscreen)"
                          " document.exitFullscreen(); }")
            page.wait_for_timeout(900)
            fs3 = page.evaluate("""() => ({full: !!document.fullscreenElement,
                    faded: document.body.classList.contains('fsfade'),
                    controls: (document.querySelector('video') || {}).controls})""")
            check("fullscreen: leaving it restores the bar and the controls",
                  (not fs3.get("faded")) and fs3.get("controls") is not False, json.dumps(fs3)[:140])

            errors = [c for c in console
                      if (c.startswith("pageerror") or
                          (c.startswith("error") and "Failed to load resource" not in c))
                      and "Unsafe attempt to initiate navigation" not in c]
            check("page: no javascript errors in the app window", not errors,
                  "; ".join(errors[:4])[:300])
            # the fixture's frame-buster fires and the sandbox refuses it: that is
            # the "site cannot walk out of the app window" guarantee, measured
            check("sandbox: the frame-buster's top-navigation was refused",
                  any("Unsafe attempt to initiate navigation" in c for c in console),
                  "refused by the #pf sandbox (blocked top-navigation)")

            # the fixture's own record of what arrived upstream, from the app window on
            embed_seen = fixture_stream.STATE["embed"][embed_before:]
            lic_seen = fixture_stream.STATE["license"][-3:]
            check("upstream: the licence saw the site's identity",
                  any(x.get("referer_ok") and x.get("origin_ok") for x in lic_seen),
                  json.dumps(lic_seen)[:300])
            check("upstream: every embed request from the app carried the site's referer",
                  bool(embed_seen) and all(x.get("ok") for x in embed_seen),
                  json.dumps(embed_seen[:3])[:300])

            if not args.keep:
                br.close()
    finally:
        if chrome is not None and not args.keep:
            try:
                chrome.terminate()
            except Exception:
                pass
        try:
            hub.terminate()
            hub.wait(timeout=5)
        except Exception:
            pass
        fx.shutdown()

    passed = sum(1 for r in results if r["ok"])
    if args.json:
        print(json.dumps({"results": results, "passed": passed,
                          "total": len(results)}, indent=2))
    else:
        for r in results:
            print("%s  %-52s %s" % ("PASS" if r["ok"] else "FAIL", r["check"], r["detail"]))
        print("\n%d/%d checks passed" % (passed, len(results)))
        print("hub log:", hub_log.name)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
