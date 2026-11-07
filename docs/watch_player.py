"""Click the player and report what actually plays, from where.

    python watch_player.py [page-url]
"""
import json
import re
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else (
    "https://methstreams.st/event/m-atlanta-hawks-vs-washington-wizards-1105")

MEDIA = re.compile(r"\.(m3u8|mp4|ts|key)(\?|$)|/manifest|/hls/", re.I)


def main():
    reqs = []
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="chrome", headless=False,
                                args=["--window-position=-2400,-2400",
                                      "--autoplay-policy=no-user-gesture-required"])
        page = br.new_context(viewport={"width": 1280, "height": 800}).new_page()
        page.on("request", lambda r: reqs.append({
            "url": r.url, "type": r.resource_type,
            "referer": r.headers.get("referer", "")})
            if MEDIA.search(r.url) else None)
        page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)

        # click everything that could be a play control, in every frame
        for _ in range(3):
            for f in page.frames:
                try:
                    f.evaluate("""() => {
                        document.querySelectorAll('video').forEach(v => {
                            v.muted = true; try { v.play(); } catch(e){}
                            v.click();
                        });
                        document.querySelectorAll('button,.btn,[class*=play],#play').forEach(b => {
                            try { b.click(); } catch(e){}
                        });
                    }""")
                except Exception:
                    pass
            page.wait_for_timeout(1500)

        page.wait_for_timeout(12000)

        frames = []
        for f in page.frames:
            try:
                info = f.evaluate("""() => {
                    const v = document.querySelector('video');
                    return {
                        url: location.href.slice(0, 160),
                        video: v ? {
                            src: (v.currentSrc || v.src || '').slice(0, 300),
                            readyState: v.readyState, duration: v.duration,
                            paused: v.paused, t: v.currentTime,
                            w: v.videoWidth, h: v.videoHeight
                        } : null,
                        sources: [...document.querySelectorAll('video source, source')]
                            .map(s => (s.src || '').slice(0, 300))
                    };
                }""")
                if info.get("video") or info.get("sources"):
                    frames.append(info)
            except Exception:
                pass
        try:
            page.screenshot(path=r"C:\AI STUFF\DeepSeek\NBADS-main\docs\player_shot.png")
        except Exception:
            pass
        br.close()

    print(json.dumps({
        "players": frames,
        "media_requests": reqs[-25:],
    }, indent=2)[:5000])


if __name__ == "__main__":
    sys.exit(main())
