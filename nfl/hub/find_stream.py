#!/usr/bin/env python3
"""
find_stream.py -- find the real video URL behind a stream page.

    python find_stream.py --url <player page> [--timeout 25]

Loads the page (and any player iframes it spawns), watches the network, and
returns the HLS master playlist if one appears. That URL plays anywhere --
VLC, ffmpeg, our relay -- no site chrome, no ads, no iframe needed.

    {"ok": true, "master": "https://.../index.m3u8?...", "all": [...]}
"""
import argparse
import json
import re
import sys
import time

try:
    from playwright.sync_api import sync_playwright
except Exception as e:
    print(json.dumps({"ok": False, "reason": f"playwright missing: {e}"}))
    sys.exit(0)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

MASTER_HINT = re.compile(r"/index\.m3u8", re.I)


def pick(urls):
    """Prefer the untouched master playlist over per-variant children."""
    masters = [u for u in urls if MASTER_HINT.search(u)]
    if masters:
        return masters[0]
    variants = [u for u in urls if re.search(r"\.m3u8", u, re.I)]
    if variants:
        return sorted(variants, key=len, reverse=True)[0]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--timeout", type=int, default=25)
    args = ap.parse_args()

    hits = []
    out = {"ok": False, "url": args.url, "master": None, "all": []}
    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch(headless=True, args=[
                "--autoplay-policy=no-user-gesture-required", "--mute-audio"])
            ctx = br.new_context(user_agent=UA, ignore_https_errors=True,
                                 viewport={"width": 1280, "height": 720})
            page = ctx.new_page()

            def on_req(r):
                u = r.url
                if ".m3u8" in u or ".mpd" in u:
                    if u not in hits:
                        hits.append(u)

            page.on("request", on_req)
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                out["reason"] = f"goto: {e}"

            deadline = time.time() + args.timeout
            while time.time() < deadline:
                page.wait_for_timeout(1200)
                if pick(hits):
                    break
                # nudge lazy players
                try:
                    for sel in ("video", "[class*='play']", "button"):
                        for el in page.query_selector_all(sel)[:2]:
                            el.click(timeout=400, force=True)
                except Exception:
                    pass

            out["all"] = hits[:12]
            m = pick(hits)
            if m:
                out["ok"] = True
                out["master"] = m
            else:
                out["reason"] = "no playlist saw any traffic"
            br.close()
    except Exception as e:
        out["reason"] = f"error: {e}"

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
