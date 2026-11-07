#!/usr/bin/env python3
"""
sniff.py -- drive a real (headless) browser at the stream sites and pull the
actual video playlist out of the network traffic.

    python sniff.py --url https://site.example/NFL
    python sniff.py --auto --sites-max 3 --follow -vs- --require "lakers|celtics"
    python sniff.py --url URL --follow nfl --max 4

Why a browser: those sites build the player in JavaScript, so a plain HTTP
fetch only ever returns the page shell -- no iframe, no .m3u8. Chromium runs
the page and we listen to everything it downloads.

Output: JSON on stdout:
    {"streams": [{"url": ..., "type": "request", "page": ...}], "pages_opened": n}
"""
import argparse
import json
import os
import re
import sys
import time

try:
    from playwright.sync_api import sync_playwright
except Exception as e:  # pragma: no cover
    print(json.dumps({"error": f"playwright missing: {e}", "streams": []}))
    sys.exit(0)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

STREAM_RE = re.compile(r"https?://[^\s\"'<>\\]+?\.(m3u8|mpd)(\?[^\s\"'<>\\]*)?")
JUNK = re.compile(r"(doubleclick|googlesyndication|popads|onclickads|propellerads|"
                  r"adsterra|hilltopads|juicyads|exoclick|trafficjunky|mgid|"
                  r"push-notification|service-worker|analytics)")


def attach(page, hits):
    """Record every request the page makes that looks like a stream."""
    def on_request(req):
        u = req.url
        if STREAM_RE.search(u):
            hits.append({"url": u, "type": "request", "page": page.url})

    page.on("request", on_request)


def grab(url, follow, max_pages, wait_ms, verbose=False, require=None):
    hits = []
    opened = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required",
                  "--mute-audio", "--disable-notifications",
                  "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(user_agent=UA, ignore_https_errors=True,
                                  viewport={"width": 1280, "height": 720})
        ctx.route("**/*",
                  lambda route: route.abort()
                  if JUNK.search(route.request.url) else route.continue_())

        page = ctx.new_page()
        attach(page, hits)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            opened += 1
        except Exception as e:
            if verbose:
                print(f"[sniff] goto failed {url}: {e}", file=sys.stderr)

        page.wait_for_timeout(wait_ms)

        # many sites only start pulling the stream after a click
        for sel in ("button:has-text('Play')", "button:has-text('play')",
                    ".play-button", "[class*='play']", "video"):
            try:
                for el in page.query_selector_all(sel)[:2]:
                    el.click(timeout=700, force=True)
            except Exception:
                pass
        page.wait_for_timeout(wait_ms // 2)

        # follow event links (only when asked to)
        if follow or require:
            try:
                hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            except Exception:
                hrefs = []
            host = re.sub(r"^https?://", "", url).split("/")[0]
            wanted = []
            for h in hrefs:
                if not h or host not in h:
                    continue
                if follow and follow.lower() not in h.lower():
                    continue
                if require:
                    try:
                        if not re.search(require, h, re.I):
                            continue
                    except re.error:
                        pass
                if h not in wanted:
                    wanted.append(h)
            for h in wanted[: max(0, max_pages - 1)]:
                p2 = ctx.new_page()
                attach(p2, hits)
                try:
                    p2.goto(h, wait_until="domcontentloaded", timeout=25000)
                    opened += 1
                    p2.wait_for_timeout(wait_ms)
                except Exception:
                    pass

        browser.close()

    seen, out = set(), []
    for h in hits:
        if h["url"] in seen:
            continue
        seen.add(h["url"])
        out.append(h)
    out.sort(key=lambda h: (0 if ".m3u8" in h["url"] else 1))
    return {"streams": out, "pages_opened": opened}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--auto", action="store_true", help="use sites.txt")
    ap.add_argument("--sites-file",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "sites.txt"))
    ap.add_argument("--follow", default=None, help="only follow links containing this text")
    ap.add_argument("--require", default=None, help="regex a followed link must match")
    ap.add_argument("--max", type=int, default=3, help="max pages per site")
    ap.add_argument("--sites-max", type=int, default=99, help="how many sites.txt lines to use")
    ap.add_argument("--wait", type=int, default=6000, help="ms to wait per page")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    urls = []
    if args.url:
        urls = [args.url]
    elif args.auto and os.path.exists(args.sites_file):
        for line in open(args.sites_file, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line.startswith("http"):
                urls.append(line)
    urls = urls[: max(1, args.sites_max)]
    if not urls:
        print(json.dumps({"error": "no url", "streams": []}))
        return 1

    all_hits, pages = [], 0
    for u in urls:
        try:
            r = grab(u, args.follow, args.max, args.wait, args.verbose, args.require)
            all_hits += r["streams"]
            pages += r["pages_opened"]
        except Exception as e:
            if args.verbose:
                print(f"[sniff] {u}: {e}", file=sys.stderr)

    seen, out = set(), []
    for h in all_hits:
        if h["url"] in seen:
            continue
        seen.add(h["url"])
        out.append(h)
    out.sort(key=lambda h: (0 if ".m3u8" in h["url"] else 1))
    print(json.dumps({"streams": out, "pages_opened": pages}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
