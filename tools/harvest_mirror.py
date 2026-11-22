"""Find the manifest a mirror player actually asks for -- and how big the picture is.

    python tools/harvest_mirror.py <event-page> [--mains 3] [--timeout 30] [--json]

The NBA hub's harvest.py showed the shape of this family: drive a real browser down
event -> /main/N -> embed, watch the network for the media playlist, then feed *that*
URL to our own player. The playlist usually answers a plain GET with no Referer
(`/main/secure/<token>/<epoch>/nfl-<id>.m3u8`), which is what makes it usable outside
the browser at all.

This version answers the question the label cannot: it downloads the top rendition's
first segment and decodes it, so "1080p" means the bytes really are 1080p.
"""
import argparse
import io
import json
import re
import sys
import time

import av
import urllib.request

AD_HOSTS = (
    "subduepaler.cyou", "doubleclick.net", "googlesyndication.com", "adservice.google.com",
    "googletagmanager.com", "google-analytics.com", "histats.com", "dtscout.com",
    "sharethis.com", "crwdcntrl.net", "lijit.com", "intentiq.com", "mrktmtrcs.net",
    "popads.net", "propellerads.com", "onclicksuper.com", "adcash.com", "tynt.com",
    "cloudflareinsights.com", "reliedhounder.com", "dtscdn.com", "pxdrop",
)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
M3U8 = re.compile(r"\.m3u8(\?|$)", re.I)


def plain_get(url, timeout=15, referer=None):
    hd = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        hd["Referer"] = referer
    req = urllib.request.Request(url, headers=hd)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def decode_size(ts):
    with av.open(io.BytesIO(ts), format="mpegts") as c:
        for st in c.streams:
            if st.type != "video":
                continue
            for f in c.decode(st):
                return f.width, f.height
    return None


def join_url(base_playlist, rel):
    """Resolve a variant URI that may be absolute, root-relative or sibling-relative."""
    if rel.startswith("http"):
        return rel
    m = re.match(r"^(https?://[^/]+)", base_playlist)
    host = m.group(1) if m else ""
    if rel.startswith("/"):
        return host + rel
    return base_playlist.rsplit("/", 1)[0] + "/" + rel


def ladder(url, referer=None):
    """Master or media playlist -> (status, renditions, real size of the top rung)."""
    try:
        status, body = plain_get(url, referer=referer)
    except Exception as e:
        return {"url": url, "error": str(e)[:120]}
    text = body.decode("utf-8", "replace")
    if not text.lstrip().upper().startswith("#EXTM3U"):
        return {"url": url, "error": "not a playlist (%d bytes)" % len(body)}
    res = re.findall(r"RESOLUTION=(\d+x\d+)", text)
    segs = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    out = {"url": url, "bytes": len(body), "declared": res, "files": len(segs)}
    vurl = url
    if res and segs:                      # a master: descend to the top rung
        top = join_url(url, segs[0])
        out["top"] = top
        try:
            _, media = plain_get(top, referer=referer)
            out["top_playlist_bytes"] = len(media)
            segs = [l.strip() for l in media.decode("utf-8", "replace").splitlines()
                    if l.strip() and not l.startswith("#")]
            vurl = top.rsplit("/", 1)[0]
        except Exception as e:
            out["top_error"] = str(e)[:90]
            segs = []
    if segs:
        seg = join_url(vurl + "/x", segs[0])
        try:
            _, ts = plain_get(seg, timeout=30, referer=referer)
            out["segment_kb"] = len(ts) // 1024
            out["real"] = decode_size(ts)
        except Exception as e:
            out["segment_error"] = str(e)[:90]
    return out


def capture(page_url, mains, timeout, headless):
    from playwright.sync_api import sync_playwright
    found = []
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="chrome", headless=headless, args=[
            "--autoplay-policy=no-user-gesture-required", "--mute-audio",
            "--window-position=-2400,-2400",
        ])
        ctx = br.new_context(viewport={"width": 1280, "height": 800},
                             user_agent=UA)
        page = ctx.new_page()
        page.on("request", lambda r: found.append((r.url, r.headers.get("referer", "")))
                if M3U8.search(r.url) else None)

        def route(r):
            return r.abort() if any(h in r.request.url.lower() for h in AD_HOSTS) else r.continue_()
        page.route("**/*", route)
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=timeout * 1000)
        except Exception as e:
            print("goto: %s" % e, file=sys.stderr)
        page.wait_for_timeout(3000)

        targets = []
        if "/main/" not in page.url:
            try:
                targets = page.eval_on_selector_all("a[href*='/main/']", "els => els.map(e => e.href)")
            except Exception:
                targets = []
        if not targets:
            targets = [page.url]
        for t in targets[:mains]:
            try:
                page.goto(t, wait_until="domcontentloaded", timeout=timeout * 1000)
            except Exception:
                continue
            deadline = time.time() + timeout
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
            if found:
                break
        br.close()
    seen, uniq = set(), []
    for u, ref in reversed(found):
        if u not in seen:
            seen.add(u)
            uniq.append((u, ref))
    return uniq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("page")
    ap.add_argument("--mains", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cands = capture(args.page, args.mains, args.timeout, args.headless)
    print("captured %d unique manifest request(s)" % len(cands), file=sys.stderr)
    report = []
    for url, ref in cands[:6]:
        r = ladder(url)
        r["referer_seen"] = ref
        r["plain_no_referer"] = "error" not in r
        report.append(r)
        if not args.json:
            print("\n%s" % url)
            print("  plain GET (no referer): %s" % ("ok" if r.get("bytes") else r.get("error")))
            print("  declared: %s  files listed: %s  segment: %s KB  REAL: %s" % (
                r.get("declared"), r.get("files"), r.get("segment_kb"), r.get("real")))
    if args.json:
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
