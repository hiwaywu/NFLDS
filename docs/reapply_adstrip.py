"""Re-apply the HLS ad-break stripping to relay.py (it was lost in a sync).

Idempotent: each edit checks whether it is already present.
"""
import io
import sys

PATH = r"C:\AI STUFF\DeepSeek\NBADS-main\nfl\relay.py"
src = io.open(PATH, encoding="utf-8").read()
orig = src
changed = []

# 1. parse_media: mark ad entries -------------------------------------------------
if "ad flag" not in src and "entries.append((line, pending if pending is not None else target, in_ad))" not in src:
    old_doc = '''def parse_media(text):
    """Return dict(seq, target, entries=[(url_rel, dur)], map_uri, endlist)."""'''
    new_doc = '''def parse_media(text):
    """Return dict(seq, target, entries=[(url_rel, dur, ad)], map_uri, endlist).

    Ad breaks are marked in HLS several ways and live re-streams often carry all of
    them: #EXT-X-CUE-OUT / -CONT / CUE-IN (SCTE-35 style), #EXT-X-SCTE35, and
    #EXT-X-DATERANGE with an interstitial/ad class. Everything between OUT and IN is
    an ad break -- that is what the relay drops so the player never sees it.
    """'''
    assert old_doc in src, "parse_media docstring not found"
    src = src.replace(old_doc, new_doc)
    src = src.replace("    entries = []\n    pending = None\n    for line in lines:",
                      "    entries = []\n    pending = None\n    in_ad = False\n    for line in lines:")
    src = src.replace('        elif line.startswith("#EXTINF:"):',
                      '        elif line.startswith("#EXT-X-CUE-OUT:0"):\n'
                      '            in_ad = False\n'
                      '        elif line.startswith("#EXT-X-CUE-OUT") or line.startswith("#EXT-X-SCTE35"):\n'
                      '            in_ad = True\n'
                      '        elif line.startswith("#EXT-X-CUE-IN"):\n'
                      '            in_ad = False\n'
                      '        elif line.startswith("#EXT-X-DATERANGE"):\n'
                      '            a = attrs(line)\n'
                      '            cls = (a.get("CLASS") or "").lower()\n'
                      '            if "ad" in cls or "interstitial" in cls or a.get("X-AD-ID") or a.get("SCTE35-OUT"):\n'
                      '                in_ad = not (a.get("END-DATE") or a.get("DURATION"))\n'
                      '        elif line.startswith("#EXTINF:"):')
    src = src.replace("            entries.append((line, pending if pending is not None else target))",
                      "            entries.append((line, pending if pending is not None else target, in_ad))")
    changed.append("parse_media marks ad entries")

# 2. state fields -----------------------------------------------------------------
if "self.ad_seqs" not in src:
    src = src.replace("        self.segs = OrderedDict()          # seq -> bytes",
                      "        self.segs = OrderedDict()          # seq -> bytes\n"
                      "        self.ad_seqs = set()               # seqs inside an ad break\n"
                      "        self.ads_skipped = 0")
    changed.append("ad bookkeeping fields")

# 3. consumer: unpack 3-tuples, never download or serve ad seqs --------------------
if "for i, (rel, dur) in enumerate(m[\"entries\"])" in src:
    src = src.replace('''                for i, (rel, dur) in enumerate(m["entries"]):
                    seq = m["seq"] + i
                    new[seq] = (urllib.parse.urljoin(base, rel), dur)
                with self.lock:
                    for seq, (u, d) in new.items():
                        self.uris[seq] = u
                        self.durs[seq] = d''',
'''                for i, (rel, dur, ad) in enumerate(m["entries"]):
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
                    self.ads_skipped = len(self.ad_seqs)''')
    src = src.replace("                    missing = [s for s in new if s not in self.segs]",
                      "                    missing = [s for s in new if s not in self.segs and s not in self.ad_seqs]")
    changed.append("consumer skips ad seqs")

# 4. playlist: drop them and mark the hole with a discontinuity -------------------
if "ads = set(self.ad_seqs)" not in src:
    src = src.replace('''        with self.lock:
            seqs = sorted(set(self.segs.keys()) | set(self.disk.keys()))''',
'''        with self.lock:
            seqs = sorted(set(self.segs.keys()) | set(self.disk.keys()))
            ads = set(self.ad_seqs)
        # ad breaks are dropped, so the numbering has holes: state them honestly and
        # mark each hole, or the player mis-maps timestamps across the missing ad
        seqs = [s for s in seqs if s not in ads]''')
    src = src.replace('''        for s in seqs:
            out.append(f"#EXTINF:{self.durs.get(s, self.target):.3f},")
            out.append(f"/seg/{s}.ts")''',
'''        prev = None
        for s in seqs:
            if prev is not None and s != prev + 1:
                out.append("#EXT-X-DISCONTINUITY")
            out.append(f"#EXTINF:{self.durs.get(s, self.target):.3f},")
            out.append(f"/seg/{s}.ts")
            prev = s''')
    changed.append("playlist drops ad seqs + discontinuity")

# 5. stats counter ---------------------------------------------------------------
if '"ads_skipped"' not in src:
    src = src.replace('            "dvr_segments": dn,',
                      '            "ads_skipped": self.ads_skipped,\n            "dvr_segments": dn,')
    changed.append("stats: ads_skipped")

if src != orig:
    io.open(PATH, "w", encoding="utf-8", newline="\n").write(src)
print("edits applied:", changed or "none (already present)")
