"""What resolution is each rendition of a live inline feed really?

    python tools/probe_renditions.py <inline-master-url> <referer>

The inline family labels its ladder with RESOLUTION lines that are not always the
truth: a rung declared 1920x1080 @ 10 Mbps can hand over a segment that decodes at
1280x720. This downloads the first media segment of every rendition and asks a real
decoder (PyAV) for the frame size, so "is there a 1080p?" is answered by the bytes.
"""
import io
import re
import sys
import urllib.request

import av


def get(url, referer):
    req = urllib.request.Request(url, headers={
        "Referer": referer,
        "Origin": re.match(r"^(https?://[^/]+)", referer).group(1),
        "User-Agent": "Mozilla/5.0 (Linux; Android 9) AppleWebKit/537.36 Chrome/75 Safari/537.36",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def measure(ts_bytes):
    """(width, height, codec, kbps) of the first video stream in a segment."""
    with av.open(io.BytesIO(ts_bytes), format="mpegts") as c:
        for st in c.streams:
            if st.type != "video":
                continue
            for frame in c.decode(st):
                return frame.width, frame.height, st.codec_context.name, len(ts_bytes) * 8 // 1000
    return None


def main():
    master, referer = sys.argv[1], sys.argv[2]
    text = get(master, referer).decode("utf-8", "replace")
    base = master.rsplit("/", 1)[0]
    variants, inf = [], None
    for line in [l.strip() for l in text.splitlines() if l.strip()]:
        if line.startswith("#EXT-X-STREAM-INF"):
            inf = line
        elif not line.startswith("#"):
            variants.append((inf or "", line))
            inf = None
    print("master: %d rendition(s)" % len(variants))
    best = None
    for inf, uri in variants:
        url = uri if uri.startswith("http") else base + "/" + uri
        res = re.search(r"RESOLUTION=(\d+x\d+)", inf)
        bw = re.search(r"BANDWIDTH=(\d+)", inf)
        try:
            media = get(url, referer).decode("utf-8", "replace")
            segs = [l.strip() for l in media.splitlines() if l.strip() and not l.startswith("#")]
            if not segs:
                print("  %-26s no segments" % uri)
                continue
            seg_url = segs[0] if segs[0].startswith("http") else url.rsplit("/", 1)[0] + "/" + segs[0]
            got = measure(get(seg_url, referer))
        except Exception as e:
            print("  %-26s failed: %s" % (uri, e))
            continue
        if not got:
            print("  %-26s no video frame in the first segment" % uri)
            continue
        w, h, codec, kb = got
        print("  %-26s declared=%-9s bw=%-9s real=%dx%d %s  first-segment=%d KB" % (
            uri, res.group(1) if res else "?", bw.group(1) if bw else "?", w, h, codec, kb))
        if best is None or h * w > best[0] * best[1]:
            best = (w, h, uri)
    if best:
        print("best real rendition: %dx%d  (%s)" % (best[0], best[1], best[2]))


if __name__ == "__main__":
    main()
