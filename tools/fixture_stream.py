"""A stand-in for a mirror player site, complete with its protections.

    python tools/fixture_stream.py            # prints the port, serves until ^C
    python tools/fixture_stream.py 8901 0.0.0.0   # reachable from an emulator (10.0.2.2)

The real mirrors gate four things, and this reproduces all four so the hub -- or the
Android app's WebView -- can be proved to get past them without a live game:

  1. X-Frame-Options on the player page                -> refuses to be framed
  2. a Referer check                                    -> "This stream is not available here"
  3. a widevine-shaped licence request: POST + body      -> 501 without a real proxy
  4. window.open() for the media window                  -> popup, or nothing

Pages:
  /event            the event page the hub lists and resolves
  /main/1           player page: frame-buster + nested /embed/player
  /main/2           the same, but it asks for its window with window.open()
  /popup            what that window would have been
  /embed/player     referer-gated, XFO'd, posts a licence, then plays the clip
  /license          POST-only, binary body, checks Origin/Referer
  /report           the page beacons its own video state here (read by the harnesses)
  /clip.mp4         docs/white1080.mp4
"""
import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CLIP = os.path.join(os.path.dirname(HERE), "docs", "white1080.mp4")

STATE = {"license": [], "embed": [], "clip": 0, "main": [], "report": [], "hls": []}

MAIN = """<!doctype html><html><head><meta charset="utf-8"><title>main __N__</title>
<style>html,body{margin:0;background:#000;height:100%}iframe{position:absolute;inset:0;
width:100%;height:100%;border:0}</style></head><body>
<iframe src="/embed/player?n=__N__" allow="autoplay; fullscreen; encrypted-media"></iframe>
<script>
  // the classic frame-buster: navigate the top window away when framed
  if (window.top !== window.self) {
    try { window.top.location.href = '/busted'; } catch (e) {}
  }
  __POPUP__
</script></body></html>"""

BEACON = """<script id="fixturebeacon">
(function () {
  // Tell the fixture (and so the test harness) what this document's video is
  // really doing. A phone's WebView cannot be inspected from a PC without CDP,
  // so the page reports on itself -- the same trick in every direction.
  var sent = false;
  setInterval(function () {
    if (sent) return;
    var v = document.querySelector('video');
    if (!v) return;
    if (v.readyState >= 2 && v.currentTime > 0.4) {
      sent = true;
      var q = '/report?path=' + encodeURIComponent(location.pathname)
            + '&url=' + encodeURIComponent(location.href)
            + '&t=' + v.currentTime.toFixed(2) + '&rs=' + v.readyState
            + '&w=' + v.videoWidth + '&h=' + v.videoHeight
            + '&muted=' + (v.muted ? 1 : 0);
      try { fetch(q).catch(function () {}); } catch (e) {}
      try { var i = new Image(); i.src = q; } catch (e) {}
    }
  }, 400);
})();
</script>"""

EMBED = """<!doctype html><html><head><meta charset="utf-8"><title>player</title>
<style>html,body{margin:0;background:#000;height:100%}video{width:100%;height:100%;
object-fit:contain;display:block}</style></head><body>
<video src="/clip.mp4" playsinline preload="auto" muted loop></video>
""" + BEACON + """
<script>
  window.__license = 'pending';
  window.__referrer_seen = document.referrer;
  var chal = new Uint8Array([7,7,7,7,7,7,7,7]);
  fetch('/license', {method: 'POST',
                     headers: {'Content-Type': 'application/octet-stream'},
                     body: chal})
    .then(function (r) { return r.text().then(function (t) { return {s: r.status, t: t}; }); })
    .then(function (o) {
      window.__license = o.s + ' ' + o.t;
      var v = document.querySelector('video');
      if (o.s === 200) { v.muted = true; v.play().catch(function () {}); }
    })
    .catch(function (e) { window.__license = 'error ' + e; });
</script></body></html>"""

POPUP = """<!doctype html><html><head><meta charset="utf-8"><title>popup player</title>
<style>html,body{margin:0;background:#000;height:100%}video{width:100%;height:100%;
object-fit:contain;display:block}</style></head><body>
<video src="/clip.mp4" playsinline preload="auto" muted loop></video>
""" + BEACON + """
<script>
  window.__license = 'popup';
  var v = document.querySelector('video');
  v.play().catch(function () {});
</script></body></html>"""

EVENT = """<!doctype html><html><head><meta charset="utf-8"><title>event</title></head>
<body><h1>fixture event</h1>
<a href="/main/1">Main 1</a> <a href="/main/2">Main 2</a></body></html>"""

# A page whose only job is to walk the screen to an unknown host, with no user
# gesture -- the ad-hijack shape. The app is expected to refuse it.
ADTEST = """<!doctype html><html><head><meta charset="utf-8"><title>adtest</title></head>
<body><h1>adtest</h1><p>a script is about to try an unknown host</p>
<script>setTimeout(function () { location.href = 'http://example.com/'; }, 700);</script>
</body></html>"""

# The same player, plus an ad that asks for a window -- the shape that must NOT be
# honoured, because an ad calls window.open() far more often than a player does.
ADWIN = """<!doctype html><html><head><meta charset="utf-8"><title>player with an ad window</title>
<style>html,body{margin:0;background:#000;height:100%}iframe{position:absolute;inset:0;
width:100%;height:100%;border:0}</style></head><body>
<iframe src="/embed/player?n=3" allow="autoplay; fullscreen; encrypted-media"></iframe>
<script>
  setTimeout(function () { window.open('http://ads.example.net/landing?slot=1', '_blank'); }, 600);
  setTimeout(function () { window.open('http://ads.example.net/landing?slot=2', '_blank'); }, 1400);
</script></body></html>"""

# The player, with an ad wearing the costume of a play button: a full-screen link on
# top of the picture. A tap must not take the app to that landing page.
TAPAD = """<!doctype html><html><head><meta charset="utf-8"><title>player with an ad overlay</title>
<style>html,body{margin:0;background:#000;height:100%}
iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
a#ad{position:absolute;inset:0;z-index:9;display:block;background:rgba(0,0,0,0.01)}
a#ad span{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);
width:180px;height:180px;border-radius:90px;background:rgba(255,255,255,0.25)}</style></head>
<body><iframe src="/embed/player?n=4" allow="autoplay; fullscreen; encrypted-media"></iframe>
<a id="ad" href="http://ads.example.net/landing?tap=1"><span></span></a></body></html>"""

REFUSED = "<html><body>This stream is not available here</body></html>"

# ---- the wrapper chain the harvest has to walk -------------------------------
# event page -> wrap1 (a static iframe) -> wrap2 (names the manifest and leaks the
# clear key next to it). This is the shape the phone resolves without a browser, and
# the shape the PC now walks the same way.
KID_HEX = "00112233445566778899aabbccddeeff"
KEY_HEX = "0f0e0d0c0b0a09080706050403020100"

CHAIN = """<!doctype html><html><head><meta charset="utf-8"><title>chain event</title></head>
<body><h1>chain event</h1><a href="__MAIN7__">Main 7</a></body></html>"""

WRAP1 = """<!doctype html><html><head><meta charset="utf-8"><title>wrap 1</title></head>
<body><p>one wrapper</p>
<iframe src="/wrap2" allow="autoplay; fullscreen; encrypted-media"></iframe></body></html>"""

WRAP2 = """<!doctype html><html><head><meta charset="utf-8"><title>wrap 2</title></head>
<body><p>the page that names the manifest</p>
<script>
  var globalurl = '/live.m3u8';
  var masterinf = {"__KID__": "__KEY__"};
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fixture-stream/1.0"

    def log_message(self, *a):
        pass

    # -------------------------------------------------------------- helpers
    @property
    def origin(self):
        """The origin this request was addressed to.

        The same fixture is used from 127.0.0.1 (PC harness) and from 10.0.2.2
        (an emulator's view of the host), so the expected identity has to come
        from the request, not from a constant.
        """
        host = self.headers.get("Host") or ""
        if host:
            return "http://" + host
        return "http://127.0.0.1:%d" % self.server.server_port

    def _html(self, code, body, extra=None):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _text(self, code, body):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _ok_referer(self):
        ref = self.headers.get("Referer") or ""
        return ref.startswith(self.origin), ref

    # ---------------------------------------------------------------- routes
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        if path == "/event":
            return self._html(200, EVENT)
        if path == "/adtest":
            return self._html(200, ADTEST)
        if path == "/chain":
            return self._html(200, CHAIN.replace("__MAIN7__", self.origin + "/main/7"))
        if path == "/main/7":
            # the wrapper the event page links to: not a player, just another hop
            return self._html(200, WRAP1)
        if path == "/wrap1":
            return self._html(200, WRAP1)
        if path == "/wrap2":
            return self._html(200, WRAP2.replace("__KID__", KID_HEX).replace("__KEY__", KEY_HEX))
        if path == "/live.m3u8":
            ref = self.headers.get("Referer") or ""
            STATE["hls"].append({"path": path, "referer": ref,
                                 "ua": self.headers.get("User-Agent") or ""})
            if "/wrap2" not in ref:
                # the CDN answers only the page it belongs to -- the reason a
                # harvested feed needs the Referer the chain walked through
                return self._text(403, "no referer")
            body = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
                    "#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:4.0,\n/seg1.ts\n"
                    "#EXTINF:4.0,\n/seg2.ts\n#EXTINF:4.0,\n/seg3.ts\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        if path.startswith("/seg") and path.endswith(".ts"):
            blob = b"\x00" * 188
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(blob)
            return
        if path == "/adwin":
            return self._html(200, ADWIN, {"X-Frame-Options": "SAMEORIGIN"})
        if path == "/tapad":
            return self._html(200, TAPAD, {"X-Frame-Options": "SAMEORIGIN"})
        if path in ("/main/1", "/main/2"):
            n = path[-1]
            popup = ""
            if n == "2":
                popup = ("setTimeout(function () { window.open('/popup', '_blank'); }, 600);")
            STATE["main"].append({"path": path, "ref": self.headers.get("Referer") or "",
                                  "ua": self.headers.get("User-Agent") or ""})
            return self._html(200, MAIN.replace("__N__", n).replace("__POPUP__", popup),
                              {"X-Frame-Options": "SAMEORIGIN"})
        if path == "/popup":
            return self._html(200, POPUP)
        if path == "/embed/player":
            ok, ref = self._ok_referer()
            STATE["embed"].append({"ok": ok, "ref": ref})
            if not ok:
                # the sentence the real mirrors use, and the reason a foreign
                # parent sees a black box
                return self._html(403, REFUSED, {"X-Frame-Options": "SAMEORIGIN"})
            return self._html(200, EMBED, {"X-Frame-Options": "SAMEORIGIN"})
        if path == "/busted":
            return self._html(200, "<html><body>busted: the frame left the app</body></html>")
        if path == "/report":
            qs = urllib.parse.parse_qs(u.query)
            row = {k: (v[0] if v else "") for k, v in qs.items()}
            row["ua"] = self.headers.get("User-Agent") or ""
            STATE["report"].append(row)
            return self._text(200, "ok")
        if path == "/clip.mp4":
            STATE["clip"] += 1
            with open(CLIP, "rb") as fh:
                raw = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Accept-Ranges", "none")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)
            return
        if path == "/license":
            return self._text(405, "licence is a POST")
        return self._text(404, "not found")

    do_HEAD = do_GET

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/license":
            return self._text(404, "not found")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        body = self.rfile.read(n) if n else b""
        ok, ref = self._ok_referer()
        origin = self.headers.get("Origin") or ""
        seen = {
            "method": self.command, "bytes": len(body),
            "referer": ref, "referer_ok": ok,
            "origin": origin, "origin_ok": origin.startswith(self.origin),
            "ctype": self.headers.get("Content-Type") or "",
        }
        STATE["license"].append(seen)
        if len(body) != 8 or seen["ctype"] != "application/octet-stream":
            return self._text(400, "license refused: bad challenge %s" % json.dumps(seen))
        if not ok or not seen["origin_ok"]:
            return self._text(403, "license refused: wrong identity %s" % json.dumps(seen))
        # a real one would answer with a wrapped content key
        return self._text(200, "key=OK method=POST bytes=8 referer=site origin=site")


def serve(port=0, host="127.0.0.1"):
    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    h = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    s = serve(p, h)
    print("fixture on http://%s:%d/event" % (h if h != "0.0.0.0" else "127.0.0.1", s.server_port),
          flush=True)
    print("from an emulator: http://10.0.2.2:%d/event" % s.server_port, flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        s.shutdown()
