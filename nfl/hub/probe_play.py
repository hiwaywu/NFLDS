#!/usr/bin/env python3
"""
probe_play.py -- does this stream page actually play, right now?

    python probe_play.py --url <page> [--timeout 10]

The player usually lives inside an iframe, so every frame is inspected, not
just the top document. Reports the frame that is really showing moving video.

Two details that matter downstream:

  * `frame` is the nearest ancestor of the playing video that has a navigable
    http(s) URL. The video itself is often inside `about:srcdoc` -- a document
    that exists only inside its parent, so embedding or navigating to it gives
    a blank box. The parent iframe host is the thing worth embedding.
  * candidate videos are ranked by area and by "is this live" (duration
    Infinity / no end), so a 480x480 ad does not win over the real player.

    {"ok":true,"reason":"playing","w":1920,"h":1080,"currentTime":3.4,
     "frame":"https://host/iframe/nfl/clippers","video_frame":"about:srcdoc"}
"""
import argparse
import json
import sys
import time

try:
    from playwright.sync_api import sync_playwright
except Exception as e:
    print(json.dumps({"ok": False, "reason": f"playwright missing: {e}"}))
    sys.exit(0)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

JS_STATE = """() => {
  const vs = Array.from(document.querySelectorAll('video'));
  return vs.map(x => ({rs: x.readyState, t: x.currentTime,
                       w: x.videoWidth, h: x.videoHeight,
                       paused: x.paused, d: x.duration,
                       src: (x.currentSrc || '').slice(0, 90)}));
}"""

REFUSAL = ("not available here", "plays only on the sites")


def openable(frame):
    """Walk up to the nearest frame a browser can actually navigate to."""
    cur = frame
    while cur is not None:
        u = (cur.url or "")
        if u.startswith("http"):
            return u
        try:
            cur = cur.parent_frame
        except Exception:
            return ""
    return ""


def rank(v):
    live = 1 if (v.get("d") is None or v.get("d") == float("inf")) else 0
    return (v.get("rs", 0) >= 2, v.get("t", 0) > 0.3, live,
            (v.get("w", 0) or 0) * (v.get("h", 0) or 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--timeout", type=int, default=10)
    args = ap.parse_args()

    out = {"ok": False, "reason": "not started", "url": args.url}
    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch(headless=True, args=[
                "--autoplay-policy=no-user-gesture-required", "--mute-audio"])
            ctx = br.new_context(user_agent=UA, ignore_https_errors=True,
                                 viewport={"width": 1280, "height": 720})
            page = ctx.new_page()
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=25000)
            except Exception as e:
                out["reason"] = f"goto: {e}"
                br.close()
                print(json.dumps(out))
                return 0

            deadline = time.time() + args.timeout
            while time.time() < deadline:
                page.wait_for_timeout(1200)
                best = None
                for f in page.frames:
                    try:
                        states = f.evaluate(JS_STATE)
                    except Exception:
                        continue
                    for st in states or []:
                        if st.get("rs", 0) < 2 or st.get("t", 0) <= 0.3:
                            continue
                        cand = dict(st)
                        cand["_frame"] = f
                        if best is None or rank(cand) > rank(best):
                            best = cand
                if best is not None:
                    target = openable(best["_frame"])
                    out.update({
                        "ok": True, "reason": "playing",
                        "currentTime": round(best.get("t", 0), 2),
                        "w": best.get("w", 0), "h": best.get("h", 0),
                        "paused": best.get("paused"),
                        "duration": best.get("d") if best.get("d") != float("inf") else "live",
                        "frame": target[:150],
                        "video_frame": (best["_frame"].url or "")[:120],
                    })
                    br.close()
                    print(json.dumps(out))
                    return 0
                try:
                    body = (page.inner_text("body") or "").lower()
                    if any(k in body for k in REFUSAL):
                        out["reason"] = "site refused: not available here"
                        br.close()
                        print(json.dumps(out))
                        return 0
                except Exception:
                    pass

            out["reason"] = "no video started in time"
            br.close()
    except Exception as e:
        out["reason"] = f"error: {e}"

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
