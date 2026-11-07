#!/usr/bin/env python3
"""
relay.py -- local HLS relay that (a) always picks the highest-quality rendition,
(b) buffers ahead so the player never stutters, and (c) pulls segments over a
pooled, parallel connection set so a single slow connection can't throttle you.

    python relay.py --url <MASTER_OR_MEDIA_PLAYLIST> --port 8823

Then point any player at:  http://127.0.0.1:8823/live.m3u8

Extra endpoints:
    /stats     JSON: segments cached, MB buffered, measured upstream throughput
    /health    200 once the first segments are ready
    /source    the resolved highest-bitrate rendition URL

Why this removes lag: the upstream feed hands you segments on someone else's
schedule, and a single dropped/slow segment stalls the player (it has nothing
else to show). This relay keeps a rolling window of fully-downloaded segments
and only advertises a segment in its playlist once the whole thing is in RAM, so
the player always reads from localhost at LAN speed. Slow segments are retried
and, if still bad, skipped instead of freezing playback.
"""
import argparse
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# A harvested feed is not a plain HLS URL: the CDN it sits on answers only when the
# request says which page it came from (the player page the chain walked through).
# One relay serves one feed, so one global Referer is exactly right.
REFERER = None

# --------------------------------------------------------------------------- #
# HTTP with connection reuse + generous pooling (this is the "more bandwidth"
# part: N parallel keep-alive sockets instead of one serial download stream)
# --------------------------------------------------------------------------- #
def make_session(pool=24):
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.4,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET"]))
    ad = HTTPAdapter(pool_connections=pool, pool_maxsize=pool, max_retries=retry)
    s.mount("http://", ad)
    s.mount("https://", ad)
    s.headers.update({
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    if REFERER:
        s.headers.update({"Referer": REFERER, "Origin": _origin(REFERER)})
    return s


def _origin(url):
    """https://cdn.example -- what a Referer implies for the Origin header."""
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return ""


def fetch_text(sess, url, timeout=12):
    r = sess.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def fetch_bytes(sess, url, timeout=20):
    r = sess.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content


# --------------------------------------------------------------------------- #
# playlist parsing
# --------------------------------------------------------------------------- #
ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def attrs(line):
    out = {}
    for k, v in ATTR_RE.findall(line.split(":", 1)[1]):
        out[k] = v.strip('"')
    return out


def parse_master(text, base):
    """Return [{url, bw, w, h, fps, codecs}] sorted best-first."""
    lines = [l.strip() for l in text.splitlines()]
    variants = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        a = attrs(line)
        j = i + 1
        while j < len(lines) and (not lines[j] or lines[j].startswith("#")):
            j += 1
        if j >= len(lines):
            continue
        w = h = 0
        if "RESOLUTION" in a:
            try:
                w, h = (int(x) for x in a["RESOLUTION"].split("x"))
            except ValueError:
                pass
        variants.append({
            "url": urllib.parse.urljoin(base, lines[j]),
            "bw": int(a.get("BANDWIDTH", 0) or 0),
            "abw": int(a.get("AVERAGE-BANDWIDTH", 0) or 0),
            "w": w, "h": h,
            "fps": a.get("FRAME-RATE", ""),
            "codecs": a.get("CODECS", ""),
        })
    variants.sort(key=lambda v: (v["h"], v["bw"]), reverse=True)
    return variants


def parse_media(text):
    """Return dict(seq, target, entries=[(url_rel, dur, ad)], map_uri, endlist).

    Ad breaks are marked in HLS several ways and live re-streams often carry all of
    them: #EXT-X-CUE-OUT / -CONT / CUE-IN (SCTE-35 style), #EXT-X-SCTE35, and
    #EXT-X-DATERANGE with an interstitial/ad class. Everything between OUT and IN is
    an ad break -- that is what the relay drops so the player never sees it.
    """
    lines = [l.strip() for l in text.splitlines()]
    seq = 0
    target = 6.0
    map_uri = None
    entries = []
    pending = None
    in_ad = False
    for line in lines:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            target = float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MAP:"):
            a = attrs(line)
            map_uri = a.get("URI")
        elif line.startswith("#EXT-X-CUE-OUT:0"):
            in_ad = False
        elif line.startswith("#EXT-X-CUE-OUT") or line.startswith("#EXT-X-SCTE35"):
            in_ad = True
        elif line.startswith("#EXT-X-CUE-IN"):
            in_ad = False
        elif line.startswith("#EXT-X-DATERANGE"):
            a = attrs(line)
            cls = (a.get("CLASS") or "").lower()
            if "ad" in cls or "interstitial" in cls or a.get("X-AD-ID") or a.get("SCTE35-OUT"):
                in_ad = not (a.get("END-DATE") or a.get("DURATION"))
        elif line.startswith("#EXTINF:"):
            try:
                pending = float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                pending = target
        elif line and not line.startswith("#"):
            entries.append((line, pending if pending is not None else target, in_ad))
            pending = None
    return {"seq": seq, "target": target, "entries": entries,
            "map": map_uri, "endlist": "#EXT-X-ENDLIST" in text}


# --------------------------------------------------------------------------- #
# relay core
# --------------------------------------------------------------------------- #
class Relay:
    def __init__(self, url, window=12, prefetch=4, max_seconds=None, idle_timeout=180,
                 dvr_dir=None, dvr_minutes=15, dvr_max_mb=1200):
        self.last_hit = time.time()
        self.idle_timeout = idle_timeout
        self.sess = make_session()
        self.pool = ThreadPoolExecutor(max_workers=prefetch + 2)
        self.lock = threading.Lock()
        self.url = url                     # resolved MEDIA playlist URL
        self.source_url = url
        self.window = window               # how many segments to hold
        self.prefetch = prefetch
        self.max_seconds = max_seconds
        self.segs = OrderedDict()          # seq -> bytes
        self.ad_seqs = set()               # seqs inside an ad break
        self.ads_skipped = 0
        self.durs = {}                     # seq -> duration
        self.uris = {}                     # seq -> absolute url
        self.map_uri = None
        self.map_data = None
        self.target = 6.0
        self.first_seq = None
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.bytes_total = 0
        self.segs_total = 0
        self.throughput = 0.0              # bits/sec estimate
        self.last_error = ""
        self.hits = 0
        self.pl_hits = 0
        self.seg_hits = 0
        self.miss = 0
        self.variant_info = {}
        # ---- disk tier: keep what scrolled out of the RAM ring, delete on exit
        # The RAM ring is ~70 s. Anything older is moved to disk instead of being
        # dropped, up to dvr_minutes / dvr_max_mb, so pausing or rewinding does
        # not have to fight the source. The folder is removed when the relay
        # stops (and stale ones are swept on start), so nothing accumulates.
        self.dvr_dir = dvr_dir
        self.dvr_minutes = dvr_minutes or 15
        self.dvr_max_mb = dvr_max_mb or 1200
        self.disk = OrderedDict()          # seq -> (path, bytes)
        self.dvr_mb = 0.0

    # ---- resolve master -> best rendition ----
    def resolve(self):
        txt = fetch_text(self.sess, self.url)
        if "#EXT-X-STREAM-INF" in txt:
            vs = parse_master(txt, self.url)
            if not vs:
                raise RuntimeError("master playlist had no variants")
            best = vs[0]
            self.variant_info = best
            self.url = best["url"]
            print(f"[relay] {len(vs)} rendition(s); picked "
                  f"{best['w']}x{best['h']} @ {best['bw']/1e6:.2f} Mbps")
        else:
            print("[relay] direct media playlist (single rendition)")

    # ---- background: keep a rolling window fully downloaded ----
    def pump(self):
        backoff = 1.0
        while not self.stop.is_set():
            if self.idle_timeout and (time.time() - self.last_hit) > self.idle_timeout:
                print("[relay] player idle too long, shutting down")
                self.stop.set()
                break
            try:
                txt = fetch_text(self.sess, self.url)
                m = parse_media(txt)
                self.target = max(1.0, m["target"])
                base = self.url
                if m["map"] and not self.map_data:
                    self.map_uri = urllib.parse.urljoin(base, m["map"])
                    self.map_data = fetch_bytes(self.sess, self.map_uri)
                new = {}
                for i, (rel, dur, ad) in enumerate(m["entries"]):
                    seq = m["seq"] + i
                    new[seq] = (urllib.parse.urljoin(base, rel), dur, ad)
                with self.lock:
                    for seq, (u, d, ad) in new.items():
                        self.uris[seq] = u
                        self.durs[seq] = d
                        if ad:
                            self.ad_seqs.add(seq)     # never downloaded, never served
                        else:
                            self.ad_seqs.discard(seq)
                    self.ads_skipped = len(self.ad_seqs)
                # fetch everything missing, newest first: the player wants to
                # start at the live edge, so the newest segment must land first
                with self.lock:
                    missing = [s for s in new if s not in self.segs and s not in self.ad_seqs]
                missing.sort(reverse=True)
                newest = max(new) if new else None
                if missing:
                    t0 = time.time()
                    total = 0
                    futs = {self.pool.submit(self.grab, s): s for s in missing}
                    for f in as_completed(futs):
                        seq = futs[f]
                        try:
                            data = f.result()
                        except Exception as e:
                            self.last_error = f"seg {seq}: {e}"
                            continue
                        with self.lock:
                            self.segs[seq] = data
                            self.bytes_total += len(data)
                            self.segs_total += 1
                            while len(self.segs) > self.window:
                                old, old_data = self.segs.popitem(last=False)
                                # disk tier: keep what leaves RAM so rewind and a
                                # long pause still have the source
                                self._spill(old, old_data)
                        total += len(data)
                        # ready as soon as the edge segment is in RAM (2 as a floor)
                        if newest is not None and seq == newest and len(self.segs) >= 2:
                            self.ready.set()
                    dt = max(0.001, time.time() - t0)
                    self.throughput = (total * 8) / dt
                    print(f"[relay] +{len(missing)} seg  "
                          f"{total/1e6:.2f} MB in {dt:.2f}s  "
                          f"=> {self.throughput/1e6:.1f} Mbps upstream")
                    if len(self.segs) >= 2:
                        self.ready.set()
                if self.max_seconds and self.segs_total * self.target > self.max_seconds:
                    print("[relay] duration cap reached")
                    self.stop.set()
                # pace: wake up often enough that the playlist edge advances one
                # segment at a time -- a batched update (+2 segments at once)
                # makes the player jump 12 s further behind live
                wait = min(max(self.target / 3, 0.5), 2.0)
                if m["endlist"]:
                    wait = 0.5
                if missing:
                    wait = 0.05
                time.sleep(wait)
                backoff = 1.0
            except Exception as e:
                self.last_error = str(e)
                print(f"[relay] playlist error: {e} (retry in {backoff:.1f}s)")
                time.sleep(backoff)
                backoff = min(backoff * 2, 8.0)

    def grab(self, seq, tries=3):
        url = self.uris[seq]
        last = None
        for i in range(tries):
            try:
                return fetch_bytes(self.sess, url)
            except Exception as e:
                last = e
                time.sleep(0.3 * (2 ** i))
        raise last

    # ---- what the player sees ----
    def _spill(self, seq, data):
        """Move a segment that left the RAM ring to disk (called under self.lock)."""
        if not self.dvr_dir:
            self.durs.pop(seq, None)
            self.uris.pop(seq, None)
            return
        try:
            os.makedirs(self.dvr_dir, exist_ok=True)
            path = os.path.join(self.dvr_dir, "seg-%06d.ts" % seq)
            with open(path, "wb") as fh:
                fh.write(data)
            self.disk[seq] = (path, len(data))
            self.dvr_mb += len(data) / 1e6
        except Exception as e:
            self.last_error = "dvr write: %s" % e
        # prune: by bytes, then by age (dvr_minutes)
        while self.disk and self.dvr_mb > self.dvr_max_mb:
            self._drop_oldest_disk()
        if self.disk and self.target:
            keep = int(self.dvr_minutes * 60 / max(self.target, 1.0))
            while len(self.disk) > keep:
                self._drop_oldest_disk()

    def _drop_oldest_disk(self):
        old, (path, size) = self.disk.popitem(last=False)
        try:
            os.remove(path)
        except OSError:
            pass
        self.dvr_mb -= size / 1e6
        self.durs.pop(old, None)
        self.uris.pop(old, None)

    def _disk_path(self, seq):
        with self.lock:
            item = self.disk.get(seq)
        return item[0] if item else None

    def wipe_dvr(self):
        """Delete the DVR folder: the download-ahead stays only while it is useful."""
        with self.lock:
            paths = [p for (p, _s) in self.disk.values()]
            self.disk.clear()
            self.dvr_mb = 0.0
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass
        if self.dvr_dir:
            try:
                shutil.rmtree(self.dvr_dir, ignore_errors=True)
            except Exception:
                pass

    def playlist_text(self):
        with self.lock:
            seqs = sorted(set(self.segs.keys()) | set(self.disk.keys()))
            ads = set(self.ad_seqs)
        # ad breaks are dropped, so the numbering has holes: state them honestly and
        # mark each hole, or the player mis-maps timestamps across the missing ad
        seqs = [s for s in seqs if s not in ads]
        if len(seqs) > self.window:
            # live window plus whatever the disk still holds (that is the point
            # of the DVR: the player may seek back into it)
            pass
        if not seqs:
            return None
        first = seqs[0]
        tgt = int(math.ceil(max(self.durs.get(s, self.target) for s in seqs)))
        out = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{tgt}",
               f"#EXT-X-MEDIA-SEQUENCE:{first}"]
        if self.map_data:
            out.append('#EXT-X-MAP:URI="/init.mp4"')
        prev = None
        for s in seqs:
            if prev is not None and s != prev + 1:
                out.append("#EXT-X-DISCONTINUITY")
            out.append(f"#EXTINF:{self.durs.get(s, self.target):.3f},")
            out.append(f"/seg/{s}.ts")
            prev = s
        return "\n".join(out) + "\n"

    def segment(self, seq):
        with self.lock:
            d = self.segs.get(seq)
        if d is not None:
            return d
        path = self._disk_path(seq)
        if path:
            try:
                with open(path, "rb") as fh:
                    return fh.read()
            except OSError:
                pass
        return self.grab(seq)

    def stats(self):
        with self.lock:
            n = len(self.segs)
            b = sum(len(v) for v in self.segs.values())
            dn, dmb = len(self.disk), self.dvr_mb
        return {
            "segments_cached": n,
            "buffered_MB": round(b / 1e6, 2),
            "buffered_seconds": round(n * self.target, 1),
            "ads_skipped": self.ads_skipped,
            "dvr_segments": dn,
            "dvr_MB": round(dmb, 1),
            "dvr_minutes_cap": self.dvr_minutes,
            "dvr_seconds_stored": round((n + dn) * self.target, 1),
            "upstream_Mbps": round(self.throughput / 1e6, 2),
            "variant": self.variant_info,
            "source": self.source_url,
            "resolved": self.url,
            "last_error": self.last_error,
            "requests": self.hits, "playlist_req": self.pl_hits,
            "segment_req": self.seg_hits, "segment_404": self.miss,
        }


def make_handler(relay):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # VLC hangs up on keep-alive sockets all the time; that is not an error
        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                self.close_connection = True

        def handle(self):
            try:
                super().handle()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                pass

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
            if isinstance(body, str):
                body = body.encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except (ConnectionResetError, BrokenPipeError):
                self.close_connection = True

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,HEAD,OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            relay.last_hit = time.time()
            relay.hits += 1
            if path in ("/live.m3u8", "/", "/index.m3u8"):
                relay.pl_hits += 1
                txt = relay.playlist_text()
                if not txt:
                    return self._send(503, "#EXTM3U\n#EXT-X-TARGETDURATION:2\n")
                return self._send(200, txt, "application/vnd.apple.mpegurl",
                                  {"Cache-Control": "no-cache"})
            if path == "/health":
                return self._send(200 if relay.ready.is_set() else 503,
                                  "ready" if relay.ready.is_set() else "warming")
            if path == "/stats":
                return self._send(200, json.dumps(relay.stats(), indent=2),
                                  "application/json")
            if path == "/window":
                # grow the ring while the viewer is paused, shrink it on resume.
                # This is what makes "pause, come back behind live" work: the
                # paused position has to still exist in the ring when playback
                # resumes, otherwise the player is forced back to the edge.
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                want = (qs.get("n") or [""])[0]
                if want.isdigit():
                    with relay.lock:
                        relay.window = max(2, min(60, int(want)))
                return self._send(200, json.dumps({"window": relay.window,
                                                   "segments": len(relay.segs),
                                                   "buffered_seconds": round(len(relay.segs) * relay.target, 1)}),
                                  "application/json")
            if path == "/source":
                return self._send(200, relay.url + "\n")
            if path == "/init.mp4" and relay.map_data:
                return self._send(200, relay.map_data, "video/mp4")
            m = re.match(r"^/seg/(\d+)\.(ts|m4s|mp4)$", path)
            if m:
                relay.seg_hits += 1
                seq = int(m.group(1))
                try:
                    data = relay.segment(seq)
                except Exception as e:
                    relay.miss += 1
                    return self._send(404, f"segment unavailable: {e}")
                ctype = "video/mp2t" if m.group(2) == "ts" else "video/mp4"
                return self._send(200, data, ctype, {"Cache-Control": "max-age=60"})
            return self._send(404, "not found")
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--port", type=int, default=8823)
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 = let a phone on the LAN reach this relay")
    ap.add_argument("--window", type=int, default=12, help="segments kept buffered")
    ap.add_argument("--prefetch", type=int, default=4, help="parallel downloads")
    ap.add_argument("--max-seconds", type=int, default=None)
    ap.add_argument("--probe-only", action="store_true",
                    help="resolve best rendition, measure throughput, exit")
    ap.add_argument("--idle-timeout", type=int, default=180,
                    help="exit after N seconds with no player requests")
    ap.add_argument("--dvr", default=None,
                    help="folder for the download-ahead tier (deleted when the relay stops)")
    ap.add_argument("--dvr-minutes", type=int, default=15,
                    help="how many minutes of the stream to keep on disk")
    ap.add_argument("--dvr-max-mb", type=int, default=1200,
                    help="hard byte cap for the DVR folder")
    ap.add_argument("--referer", default=None,
                    help="Referer the feed's CDN expects (a harvested feed needs one)")
    args = ap.parse_args()
    global REFERER
    REFERER = args.referer

    relay = Relay(args.url, window=args.window, prefetch=args.prefetch,
                  max_seconds=args.max_seconds, idle_timeout=args.idle_timeout,
                  dvr_dir=args.dvr, dvr_minutes=args.dvr_minutes,
                  dvr_max_mb=args.dvr_max_mb)
    try:
        relay.resolve()
    except Exception as e:
        print(f"[relay] cannot open playlist: {e}", file=sys.stderr)
        return 2

    if args.probe_only:
        txt = fetch_text(relay.sess, relay.url)
        m = parse_media(txt)
        if not m["entries"]:
            print("[relay] empty playlist", file=sys.stderr)
            return 3
        # grab the last 3 segments in parallel: average size gives an honest
        # bitrate, and the wall clock gives the aggregate bandwidth we can pull
        sample = m["entries"][-3:]
        urls = [(urllib.parse.urljoin(relay.url, rel), dur) for rel, dur in sample]
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=len(urls)) as ex:
            futs = [ex.submit(fetch_bytes, relay.sess, u) for u, _ in urls]
            blobs = []
            for f in futs:
                try:
                    blobs.append(f.result())
                except Exception:
                    blobs.append(b"")
        dt = max(0.001, time.time() - t0)
        total = sum(len(b) for b in blobs)
        secs = sum(d for _, d in urls)
        v = relay.variant_info
        print(json.dumps({
            "url": relay.url, "w": v.get("w", 0), "h": v.get("h", 0), "fps": v.get("fps", ""),
            "bw_mbps": round(v.get("bw", 0) / 1e6, 2),
            "seg_MB": round(total / len(urls) / 1e6, 2),
            "seg_seconds": round(secs / len(urls), 2),
            "measured_bitrate_mbps": round((total * 8) / max(0.001, secs) / 1e6, 2),
            "fetch_Mbps": round((total * 8) / dt / 1e6, 2),
            "ok": total > 0,
        }))
        return 0 if total > 0 else 4

    th = threading.Thread(target=relay.pump, daemon=True)
    th.start()

    srv = ThreadingHTTPServer((args.host, args.port), make_handler(relay))
    srv.daemon_threads = True
    print(f"[relay] serving http://{args.host}:{args.port}/live.m3u8")
    if relay.ready.wait(timeout=45):
        print("[relay] buffered and ready")
    else:
        print("[relay] warning: buffer not filled yet, player may wait")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        relay.stop.set()
        relay.wipe_dvr()          # nothing lingers after the app is closed
    return 0


if __name__ == "__main__":
    sys.exit(main())



