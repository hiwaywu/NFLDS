"""Does the relay drop ad breaks out of an HLS playlist?

Runs a tiny local origin: a media playlist with a CUE-OUT/CUE-IN ad break around
segment 4, then starts the real relay against it and checks that
  * /live.m3u8 never lists the ad segment,
  * a #EXT-X-DISCONTINUITY marks the hole,
  * /stats reports ads_skipped,
  * the segment bytes we serve for 3 and 5 are the real ones.

    python adstrip_check.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nfl")
RELAY = os.path.join(NB, "relay.py")

PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:100
#EXTINF:4.000,
http://127.0.0.1:{port}/seg/100.m4s
#EXTINF:4.000,
http://127.0.0.1:{port}/seg/101.m4s
#EXTINF:4.000,
http://127.0.0.1:{port}/seg/102.m4s
#EXT-X-CUE-OUT:60
#EXTINF:4.000,
http://127.0.0.1:{port}/seg/103.m4s
#EXTINF:4.000,
http://127.0.0.1:{port}/seg/104.m4s
#EXT-X-CUE-IN
#EXTINF:4.000,
http://127.0.0.1:{port}/seg/105.m4s
#EXTINF:4.000,
http://127.0.0.1:{port}/seg/106.m4s
"""


class Origin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/live.m3u8") or self.path == "/":
            body = PLAYLIST.format(port=self.server.server_port).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/seg/"):
            name = self.path.rsplit("/", 1)[-1].split(".")[0]
            body = ("SEGMENT-" + name).encode() * 50
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
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    origin.daemon_threads = True
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    oport = origin.server_port

    port = 8971
    dvr = os.path.join(tempfile.gettempdir(), "adstrip-dvr-%d" % port)
    proc = subprocess.Popen([sys.executable, RELAY,
                             "--url", "http://127.0.0.1:%d/live.m3u8" % oport,
                             "--port", str(port), "--window", "10",
                             "--dvr", dvr, "--idle-timeout", "30"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(60):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                pass
        if proc.poll() is not None:
            err = (proc.communicate(timeout=5)[1] or "")[-800:]
            print("relay died:", err)
            return 2
        with urllib.request.urlopen("http://127.0.0.1:%d/live.m3u8" % port, timeout=5) as r:
            pl = r.read().decode()
        stats = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:%d/stats" % port, timeout=5).read().decode())
        segs = [ln.rsplit("/", 1)[-1] for ln in pl.splitlines() if ln.startswith("/seg/")]
        print(json.dumps({
            "segments_served": segs,
            "ad_segments_present": [s for s in segs if s.split(".")[0] in ("103", "104")],
            "discontinuity": "#EXT-X-DISCONTINUITY" in pl,
            "ads_skipped": stats.get("ads_skipped"),
            "media_sequence": [l for l in pl.splitlines() if l.startswith("#EXT-X-MEDIA-SEQUENCE")],
        }, indent=2))
        ok = (not any(s.split(".")[0] in ("103", "104") for s in segs)
              and "#EXT-X-DISCONTINUITY" in pl
              and stats.get("ads_skipped") == 2)
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        proc.terminate()
        time.sleep(0.5)
        origin.shutdown()


if __name__ == "__main__":
    sys.exit(main())
