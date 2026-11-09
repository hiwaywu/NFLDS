"""Offline proof for the inline-playlist family -- no network, no browser.

One mirror family never hands out a manifest URL. Its player page carries the whole
ladder inside itself:

    var masterinf = {"video5_chargers.m3u8": {"inf": "#EXT-X-STREAM-INF:BANDWIDTH=10000000,..."}}
    var payload   = {"name": "chargers", "renditions": {"video5_chargers": "#EXTM3U\n..."}}

and its own shaka player is fed from those strings, so a request sniffer sees no
m3u8, `globalurl` is a relative dud ('chargers.m3u8' -> 404) and the page answers a
cold hit with an ~18 KB shell before the real ~85 KB payload.

This checks the three pieces that make it playable in our own window:

  * `inline_from` finds the ladder in a page and ranks it by bandwidth,
  * `inline_master` / `inline_media` answer a real master + variant pair,
  * the pair is what the relay can consume (variant URIs are relative to the master).

    python tools/verify_inline_family.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "nfl", "hub"))

import hub  # noqa: E402


SEG = ("https://o300801.mp.lura.live/live/ephemeral/rgXVPz5RdkM2unCzKuPY0EMj1Nynx9kq/"
       "dmla-anvato09/%dk/YsRfH8ubOuy/175575/segment_17557514%sc.ts"
       "?Expires=1794186028&KeyName=cdnkey1&Signature=GtV2wS3Xz0dKsz05PhGEScUkR38")


def media(n):
    out = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:6",
           "#EXT-X-MEDIA-SEQUENCE:%d" % (2230 + n), "#EXT-X-INDEPENDENT-SEGMENTS"]
    for i in range(3):
        out.append("#EXTINF:6.000,")
        out.append(SEG % (n, "%x" % i))
    return "\n".join(out)


def page(ladder):
    """A page in the shape the provider serves, plus the traps it sets."""
    masterinf = {}
    renditions = {}
    for name, bw, res, kbps in ladder:
        key = name[:-len(".m3u8")]
        masterinf[name] = {
            "inf": ('#EXT-X-STREAM-INF:BANDWIDTH=%d,AVERAGE-BANDWIDTH=%d,'
                    'CODECS="avc1.64001f, mp4a.40.2",RESOLUTION=%s,CLOSED-CAPTIONS="cc"'
                    % (bw, int(bw * 0.8), res)),
            "subfolder": None, "backupsubfolder": "",
        }
        renditions[key] = media(kbps)
    return (
        "<!DOCTYPE html><html><head><script>\n"
        "var key = 'chargers';\n"
        "var globalurl= 'chargers.m3u8';\n"          # the dud, relative, 404s
        "var masterinf = %s;\n"
        "var payload = %s;\n"
        "</script></head><body><video id=\"video\"></video></body></html>"
        % (json.dumps(masterinf), json.dumps({"name": "chargers", "renditions": renditions})))


LADDER = [
    ("video0_chargers.m3u8", 500000, "416x234", 528),
    ("video1_chargers.m3u8", 1250000, "640x360", 1128),
    ("video2_chargers.m3u8", 4000000, "1280x720", 3328),
    ("video4_chargers.m3u8", 2500000, "1280x720", 2128),
    ("video5_chargers.m3u8", 10000000, "1920x1080", 8128),
]

checks = []


def check(name, ok, detail=""):
    checks.append((name, bool(ok), detail))
    print(("  ok   " if ok else "  FAIL ") + name + ("  -- " + detail if detail else ""))


def main():
    html = page(LADDER)
    vs = hub.inline_from(html)
    check("inline ladder found in the page", vs is not None,
          "%d renditions" % (len(vs) if vs else 0))
    if not vs:
        return 1
    check("ranked by bandwidth, 1080p first",
          vs[0]["file"] == "video5_chargers.m3u8" and vs[0]["bw"] == 10000000,
          "%s @ %d" % (vs[0]["file"], vs[0]["bw"]))
    check("every rendition carries a media playlist", all("#EXTM3U" in v["playlist"] for v in vs))
    check("the dud globalurl is not taken for a rendition",
          all(v["file"] != "chargers.m3u8" for v in vs))

    hub.INLINE.clear()
    hub.INLINE_CACHE.clear()
    sid = hub.inline_register("https://topstreamer.site/iframe/nfl/chargers",
                              "https://trendy48.site/top/chargers")
    hub.INLINE_CACHE[sid] = (hub.time.time(), vs)   # no network in this test

    master = hub.inline_master(sid)
    check("master answers #EXTM3U", master.startswith("#EXTM3U"))
    lines = [l for l in master.splitlines() if l and not l.startswith("#")]
    check("master names every variant, best first",
          lines == [v["file"] for v in vs], "%d variants" % len(lines))
    check("variant names are relative to the master (the relay joins them)",
          all("/" not in l for l in lines))
    check("master carries RESOLUTION so the relay picks the best rung",
          "RESOLUTION=1920x1080" in master)

    best = hub.inline_media(sid, "video5_chargers.m3u8")
    check("variant body is a playable media playlist",
          best.startswith("#EXTM3U") and ".ts?" in best,
          "%d segments" % best.count("#EXTINF"))
    check("variant segments are absolute (the relay proxies them with the referer)",
          "https://o300801.mp.lura.live/" in best)
    check("unknown variant answers nothing", hub.inline_media(sid, "nope.m3u8") is None)
    check("unknown id answers nothing", hub.inline_master("deadbeef") is None)

    # the shell: no payload, no masterinf -- the answer a cold hit gets
    shell = "<html><head><script>var key='chargers';var globalurl='chargers.m3u8';</script></head></html>"
    check("an ~18 KB shell is not mistaken for a ladder", hub.inline_from(shell) is None)

    bad = len([1 for _, ok, _ in checks if not ok])
    print("\n%d/%d checks passed" % (len(checks) - bad, len(checks)))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
