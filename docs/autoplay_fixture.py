"""Prove the /play proxy auto-starts a player, using a local fixture.

    python autoplay_fixture.py

Runs a two-file origin (a page with a <video> and a poster overlay + the clip),
fetches it through the hub's /play proxy, and checks that the injected script
mutes, clicks and plays without any user gesture.
"""
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
CLIP = os.path.join(HERE, "white1080.mp4")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>fixture player</title>
<style>body{background:#111;margin:0}#ov{position:absolute;inset:0;display:flex;
align-items:center;justify-content:center;background:rgba(0,0,0,.6);color:#fff;
font:20px sans-serif}#ov.hide{display:none}</style></head><body>
<video src="/clip.mp4" playsinline preload="auto"></video>
<div id="ov" class="overlay play">click to play</div>
<script>
  document.querySelector('#ov').addEventListener('click', function () {
    this.classList.add('hide');
    var v = document.querySelector('video');
    v.play().catch(function(){});
  });
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/page"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/clip.mp4"):
            with open(CLIP, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    target = "http://127.0.0.1:%d/page.html" % srv.server_port
    proxied = "http://127.0.0.1:8790/play?u=" + urllib.parse.quote(target, safe="")
    print("fixture:", target)

    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="chrome", headless=True,
                                args=["--mute-audio", "--autoplay-policy=document-user-activation-required"])
        page = br.new_context(viewport={"width": 900, "height": 600}).new_page()
        page.goto(proxied, wait_until="domcontentloaded", timeout=30000)
        state = {}
        for _ in range(20):
            page.wait_for_timeout(700)
            state = page.evaluate("""() => {
                const v = document.querySelector('video');
                return {
                    autoplay: document.documentElement.getAttribute('data-nbaautoplay'),
                    overlay_hidden: !!(document.getElementById('ov') || {}).classList &&
                                     document.getElementById('ov').classList.contains('hide'),
                    playing: v ? {paused: v.paused, muted: v.muted,
                                  t: Math.round(v.currentTime * 10) / 10,
                                  rs: v.readyState} : null
                };
            }""")
            if state.get("playing") and not state["playing"]["paused"] and state["playing"]["t"] > 0.2:
                break
        print(json.dumps(state, indent=2))
        ok = bool(state.get("playing") and not state["playing"]["paused"]
                  and state["playing"]["t"] > 0.2)
        print("PASS" if ok else "FAIL")
        br.close()
    srv.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
