"""Follow a game page down to the real HLS manifest.

    python harvest.py <event-or-player-url> [--timeout 30]

The chain on these mirrors is: event page -> "/main/1" player page -> an embed that
ad-wraps the player -> the embed finally XHRs the media playlist:

    https://tims-001.sytes.net/main/secure/<token>/<epoch>/NFL-401909923.m3u8

That manifest answers 200 with plain (unencrypted) HLS; the segments are signed CDN
URLs. So "cracking" it is: drive a real browser until the player asks for its
playlist, then hand *that* URL to our own relay and play it in our own <video>.

stdout: one JSON object {"ok": bool, "m3u8": url, "referer": url, "source": page}
"""
import argparse
import json
import re
import sys
import time
import urllib.request

BLOCK_HOSTS = (
    "subduepaler.cyou", "doubleclick.net", "googlesyndication.com", "adservice.google.com",
    "googletagmanager.com", "google-analytics.com", "histats.com", "dtscout.com",
    "sharethis.com", "crwdcntrl.net", "lijit.com", "intentiq.com", "mrktmtrcs.net",
    "popads.net", "propellerads.com", "onclicksuper.com", "adcash.com", "tynt.com",
    "cloudflareinsights.com", "reliedhounder.com", "dtscdn.com", "pxdrop",
)


LEAGUE_PASS = "lpnfl.akamaized.net"
GAME_ID = re.compile(r"/(g\d{10}[a-z]{3}\d{6,8}[a-z]{2,3})", re.I)


def describe(url, body):
    """What kind of stream is this? Game Pass, DRM system, PSSH, license URL."""
    info = {"league_pass": LEAGUE_PASS in (url or ""), "game_id": None,
            "widevine": False, "sample_aes": False, "key_uri": None, "license": None}
    m = GAME_ID.search(url or "")
    if m:
        info["game_id"] = m.group(1)
    body = body or ""
    low = body.lower()
    if "hls-wvpr" in (url or "").lower() or "widevine" in low:
        info["widevine"] = True
    if "method=sample-aes" in low:
        info["sample_aes"] = True
    for line in body.splitlines():
        if line.startswith("#EXT-X-KEY") or line.startswith("#EXT-X-SESSION-KEY"):
            kv = line.split(":", 1)[1] if ":" in line else ""
            params = dict(p.strip().split("=", 1) for p in kv.split(",") if "=" in p)
            uri = (params.get("URI") or "").strip('"')
            info["key_uri"] = uri or info["key_uri"]
            if uri.startswith("skd://") or "widevine" in (params.get("KEYFORMAT") or "").lower():
                info["widevine"] = True
                info["license"] = uri
    return info

def is_ad_host(url):
    low = url.lower()
    return any(h in low for h in BLOCK_HOSTS)
M3U8 = re.compile(r"\.m3u8(\?|$)", re.I)
KEY = re.compile(r"\.key(\?|$)|/key/", re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--shot", default=None)
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    found = []          # (url, referer), newest last
    keys = []
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="chrome", headless=False, args=[
            "--window-position=-2400,-2400",
            "--autoplay-policy=no-user-gesture-required",
            "--mute-audio",
        ])
        ctx = br.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()

        def on_req(r):
            u = r.url
            ref = r.headers.get("referer", "")
            if M3U8.search(u):
                found.append((u, ref))
            elif KEY.search(u):
                keys.append(u)

        # zero ads while cracking: abort ad/tracker hosts outright. The pre-roll
        # bumper is one of them, and the manifest does not need any of it.
        def _route(route):
            if is_ad_host(route.request.url):
                return route.abort()
            return route.continue_()
        try:
            page.route("**/*", _route)
        except Exception:
            pass
        page.on("request", on_req)
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
        except Exception:
            pass
        page.wait_for_timeout(3500)

        # event pages link the actual player; follow it if we are not there yet
        if "/main/" not in page.url:
            try:
                nxt = page.eval_on_selector_all(
                    "a[href*='/main/']", "els => els.map(e => e.href)")
                if nxt:
                    page.goto(nxt[0], wait_until="domcontentloaded", timeout=args.timeout * 1000)
                    page.wait_for_timeout(3000)
            except Exception:
                pass

        # poke every player we can see, repeatedly: the ad layer has to finish
        deadline = time.time() + args.timeout
        while time.time() < deadline and not found:
            for f in page.frames:
                try:
                    f.evaluate("""() => {
                        document.querySelectorAll('video').forEach(v => {
                            v.muted = true; try { v.play(); } catch(e){}
                            v.click();
                        });
                        document.querySelectorAll('button,[class*=play],#play,[onclick]').forEach(b => {
                            try { b.click(); } catch(e){}
                        });
                    }""")
                except Exception:
                    pass
            page.wait_for_timeout(1200)

        if args.shot:
            try:
                page.screenshot(path=args.shot)
            except Exception:
                pass
        src = page.url
        br.close()

    # the token rotates; keep the last few, newest first
    uniq = []
    for u, ref in reversed(found):
        if not any(u == seen for seen, _ in uniq):
            uniq.append((u, ref))

    # Not every mirror is playable by us. NFL's own CDN hands out
    # ".../hls-wvpr/..." manifests whose segments are Widevine (SAMPLE-AES): the
    # playlist downloads fine, the picture does not decode without a CDM. Check
    # each candidate and prefer one that is plain.
    def looks_drm(url, body):
        blob = (url + " " + (body or "")).lower()
        return ("hls-wvpr" in url.lower() or "widevine" in blob
                or "sample-aes" in blob or "com.widevine" in blob)

    best = None
    best_body = ""
    checked = []
    for url, ref in uniq[:5]:
        body = ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=8) as r:
                body = r.read(200000).decode("utf-8", "replace")
        except Exception as e:
            checked.append({"url": url[:90], "error": str(e)[:80]})
            continue
        drm = looks_drm(url, body)
        checked.append({"url": url[:90], "drm": drm, "bytes": len(body)})
        if not drm:
            best = (url, ref)
            best_body = body
            break
        if best is None:
            best = (url, ref)          # all DRM so far: keep the first as fallback
            best_body = body
    all_drm = bool(checked) and all(c.get("drm") for c in checked if "drm" in c)
    stream_info = describe(best[0] if best else "", best_body)
    print(json.dumps({
        "ok": bool(best and best[0]),
        "m3u8": best[0] if best else None,
        "referer": (best[1] if best else "") or "",
        "stream": stream_info,
        "source": src,
        "drm": all_drm,
        "checked": checked,
        "alternates": [u for u, _ in uniq[1:5]],
        "keys": keys[:4],
        "seen": len(found),
    }, indent=2))


if __name__ == "__main__":
    sys.exit(main())
