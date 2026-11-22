"""Does a harvested mirror manifest answer for a given Referer -- and is it really 1080p?

    python tools/probe_manifest.py <manifest-url> [referer ...]

A mirror hands the player a signed playlist (`/main/secure/<sha>/<epoch>/nfl-<id>.m3u8`)
whose URL is only good with the identity the player page had: the right Referer, and
sometimes an Origin. The relay re-fetches it later with one Referer guess and gets a 404
that looks like "the feed is dead" while the ladder is fine.

Prints, per referer: HTTP status, size, rendition count, every RESOLUTION line, and the
frame size a real decoder produces from the first segment of the top rung.
"""
import io
import re
import sys
import urllib.error
import urllib.request

import av

UA = ("Mozilla/5.0 (Linux; Android 9; AFTKA Build/PS7647) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/75.0.3770.143 Mobile Safari/537.36")


def fetch(url, referer=None, timeout=25):
    hd = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        hd["Referer"] = referer
        m = re.match(r"^(https?://[^/]+)", referer)
        if m:
            hd["Origin"] = m.group(1)
    req = urllib.request.Request(url, headers=hd)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def measure(ts_bytes):
    with av.open(io.BytesIO(ts_bytes), format="mpegts") as c:
        for st in c.streams:
            if st.type != "video":
                continue
            for frame in c.decode(st):
                return frame.width, frame.height
    return None


def probe(url, referer):
    try:
        status, body = fetch(url, referer)
    except urllib.error.HTTPError as e:
        return "HTTP %s" % e.code
    except Exception as e:
        return "error %s" % e
    text = body.decode("utf-8", "replace")
    if not text.lstrip().upper().startswith("#EXTM3U"):
        return "200 but not a playlist (%d bytes)" % len(body)
    res = re.findall(r"RESOLUTION=(\d+x\d+)", text)
    variants = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    out = ["200  %d bytes  %d rendition(s)  %s" % (len(body), len(variants), res or "no RESOLUTION lines")]
    if variants:
        base = url.rsplit("/", 1)[0]
        top = variants[0]
        vurl = top if top.startswith("http") else base + "/" + top
        try:
            _, media = fetch(vurl, referer)
            segs = [l.strip() for l in media.decode("utf-8", "replace").splitlines()
                    if l.strip() and not l.startswith("#")]
            if segs:
                seg = segs[0] if segs[0].startswith("http") else vurl.rsplit("/", 1)[0] + "/" + segs[0]
                _, ts = fetch(seg, referer, 30)
                out.append("top rung %s -> segment %d KB -> decoded %s" % (
                    top, len(ts) // 1024, measure(ts)))
        except Exception as e:
            out.append("top rung fetch failed: %s" % e)
    return "   ".join(out)


def main():
    url = sys.argv[1]
    referers = sys.argv[2:] or [None]
    for ref in referers:
        print("referer: %s" % (ref or "(none)"))
        print("   " + probe(url, ref))


if __name__ == "__main__":
    main()
