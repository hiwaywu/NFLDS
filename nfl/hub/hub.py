#!/usr/bin/env python3
"""
nfl hub -- a local page that lists the games by date and plays the best stream.

    python hub.py [--port 8795] [--open]

Sections in the UI:
  * GAMES  -- scraped from the stream sites' NFL pages, filtered by the date you
              pick (defaults to today; rolls forward automatically when a date
              has no games). Click one -> it opens that game's best player feed
              in a dedicated app window.
  * NFL TV -- the verified always-on channels. Click one -> the local relay
              picks the highest rendition, buffers ~40 s, and hls.js plays it
              right here at full quality.
"""
import argparse
import collections
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
NFL_DIR = os.path.dirname(ROOT)
RELAY = os.path.join(NFL_DIR, "relay.py")
FEEDS = os.path.join(NFL_DIR, "feeds.txt")
LOG_PATH = os.path.join(ROOT, "hub.err.log")

# Where the relay listens. 127.0.0.1 keeps it desktop-only; when the hub is
# bound to 0.0.0.0 (the phone case) the relay has to listen on the LAN too, or
# the phone is handed http://127.0.0.1:89xx/ and talks to itself.
RELAY_HOST = "127.0.0.1"
HUB_VERSION = "1.0"


def log_exc(where, exc):
    """pythonw has no console: an unhandled handler error has to leave a trace."""
    try:
        import traceback
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {where}: {exc!r}\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=fh)
    except Exception:
        pass


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

GAME_SOURCES = [
    "https://methstreams.st/NFL",
    "https://crackstreams.st/NFL",
]

# ------------------------------------------------------------------ helpers
def fetch(url, timeout=20, headers=None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "*/*")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=timeout)


BROWSER_FILE = os.path.join(ROOT, "browser.txt")

# Order matters: Chrome first, Firefox second. Edge is deliberately NOT in this
# list -- it is the Windows default browser and it kept being the thing that
# opened. It stays available, but only if asked for by name: --browser edge or
# a browser.txt containing "edge".
BROWSER_CANDIDATES = [
    ("chrome",   r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    ("chrome",   r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    ("chrome",   r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ("chromium", r"%ProgramFiles%\Chromium\Application\chrome.exe"),
    ("firefox",  r"%ProgramFiles%\Mozilla Firefox\firefox.exe"),
    ("firefox",  r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe"),
    ("brave",    r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
]

BROWSER_NAMES = {
    "chrome": r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    "firefox": r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
    "edge": r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    "brave": r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
}


def _expand(p):
    return os.path.expandvars(os.path.expanduser(p or ""))


def browser_override():
    """browser.txt next to hub.py: a name (chrome/firefox/edge/brave) or a path."""
    try:
        with open(BROWSER_FILE, encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                low = s.lower()
                if low in BROWSER_NAMES:
                    s = BROWSER_NAMES[low]
                return _expand(s) if os.path.isfile(_expand(s)) else None
    except Exception:
        pass
    return None


def find_browser():
    """(kind, exe) for the browser the hub should use, or (None, None)."""
    forced = browser_override()
    if forced:
        name = os.path.basename(forced).lower()
        kind = ("firefox" if "firefox" in name else
                "brave" if "brave" in name else
                "edge" if "msedge" in name else "chrome")
        return kind, forced
    for kind, pat in BROWSER_CANDIDATES:
        exe = _expand(pat)
        if os.path.isfile(exe):
            return kind, exe
    return None, None


def browser_argv(url, profile_dir):
    """Command line that opens `url` as an app window where the browser allows it."""
    kind, exe = find_browser()
    if not exe:
        return None
    if kind == "firefox":
        # Firefox has no --app mode; a private profile + a new window is as close
        # as it gets, and -no-remote keeps it out of the user's own session.
        return [exe, "-no-remote", "-new-instance", "-profile", profile_dir,
                "-new-window", url]
    return [exe, f"--app={url}",
            "--window-size=1280,820",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-features=Translate,DisableLoadExtensionCommandLineSwitch",
            "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={profile_dir}"]


def find_python():
    exe = sys.executable
    return exe if exe and os.path.isfile(exe) else (shutil.which("python") or "python")


def lan_ip():
    """This machine's LAN address -- the one a phone on the same wifi can use."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def request_hostname(handler, default=""):
    """The host the client actually used to reach us.

    The relay runs on its own port, so the URL we hand back has to keep the
    client's host and swap the port. Returning 127.0.0.1 here is what made a
    phone's "play" button do nothing at all.
    """
    raw = (handler.headers.get("Host") or "").strip()
    if raw:
        if raw.startswith("["):                    # [fe80::1]:8790
            return raw.split("]")[0] + "]"
        if raw.count(":") == 1:
            return raw.rsplit(":", 1)[0]
        return raw
    return default or lan_ip() or "127.0.0.1"


def port_in_use(p):
    """Something already answers on this port?

    A bind() test is not enough on Windows: HTTPServer sets SO_REUSEADDR, so a
    second process can bind a port another one is serving on, and then whatever
    we start answers... nothing, while /health talks to the stranger. That is how
    a dead stream briefly "worked" by playing somebody else's relay.
    """
    s = socket.socket()
    s.settimeout(0.2)
    try:
        s.connect(("127.0.0.1", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


def free_port(start=8960, end=9020):
    with RELAY_LOCK:
        busy = {p for (_proc, p) in RELAYS.values()}
    for p in range(start, end):
        if p in busy or port_in_use(p):
            continue
        s = socket.socket()
        try:
            s.bind(("0.0.0.0", p))       # same address the relay will bind
            return p
        except OSError:
            continue
        finally:
            s.close()
    return 0


# ------------------------------------------------------------------ games
DATE_CACHE = {}
CACHE_TTL = 240
SOURCE_HEALTH = {}

# The stream sites stamp each event with the UTC date, which is one day ahead
# of the local game date for every evening tip-off. ESPN's schedule is the
# source of truth for "which night is this game on", so we join the streams to
# it by team name instead of trusting the URL.
ESPN_URL = ("https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
            "scoreboard?dates={}")


def espn_day(yyyymmdd):
    rows = []
    try:
        raw = fetch(ESPN_URL.format(yyyymmdd), timeout=15).read().decode("utf-8", "replace")
        data = json.loads(raw)
    except Exception:
        return rows
    for ev in data.get("events", []):
        try:
            comp = ev["competitions"][0]
        except (KeyError, IndexError):
            continue
        away = home = None
        for c in comp.get("competitors", []):
            t = c.get("team", {})
            if c.get("homeAway") == "away":
                away = t
            else:
                home = t
        if not away or not home:
            continue
        try:
            status = (comp.get("status") or {}).get("type", {}).get("state", "")
            detail = (comp.get("status") or {}).get("type", {}).get("detail", "")
        except Exception:
            status, detail = "", ""
        rows.append({
            "date": f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}",
            "away": away.get("displayName", ""),
            "home": home.get("displayName", ""),
            "start_utc": ev.get("date", ""),
            "state": status,
            "detail": detail,
        })
    return rows


SCHED = {}
SCHED_TTL = 300


def _safe_espn_day(day):
    try:
        return espn_day(day)
    except Exception:
        return []


def schedule_window():
    """ESPN's fixtures for a two-week window.

    It used to be -2..+3 days, which meant picking a date five days out showed
    "no games listed" even though the NFL schedule is public. The stream sites
    only post a few days ahead, so those later days come back as fixtures with
    stream=False -- the UI says "no stream listed yet" instead of pretending the
    night is empty.
    """
    now = time.time()
    hit = SCHED.get("w")
    if hit and now - hit[0] < SCHED_TTL:
        return hit[1]
    import datetime as _dt
    base = _dt.date.today()
    days = [(base + _dt.timedelta(days=off)).strftime("%Y%m%d")
            for off in range(-3, 11)]
    rows = []
    # 14 tiny JSON documents: fetch them together, ~0.3 s instead of ~3 s
    with ThreadPoolExecutor(max_workers=6) as pool:
        for part in pool.map(_safe_espn_day, days):
            rows.extend(part)
    SCHED["w"] = (now, rows)
    return rows


def local_time(iso):
    if not iso:
        return ""
    import datetime as _dt
    try:
        s = iso.replace("Z", "+00:00")
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.astimezone().strftime("%-I:%M %p")
    except Exception:
        try:
            d = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
            return d.strftime("%I:%M %p").lstrip("0")
        except Exception:
            return ""


def slug_match(url):
    """'m-okc-thunder-vs-cleveland-cavaliers-1103' -> ('okc thunder', 'cleveland cavaliers')"""
    slug = url.rstrip("/").split("/event/")[-1]
    slug = re.sub(r"-\d{4}$", "", slug)
    slug = re.sub(r"^m-", "", slug)
    if "-vs-" not in slug:
        return None, None
    a, h = slug.split("-vs-", 1)
    return a.replace("-", " "), h.replace("-", " ")


def match_espn(away_txt, home_txt, rows):
    for r in rows:
        try:
            al = r["away"].split()[-1].lower()
            hl = r["home"].split()[-1].lower()
        except IndexError:
            continue
        if al and hl and al in away_txt and hl in home_txt:
            return r
    return None



def date_of(url):
    m = re.search(r"-(\d{2})(\d{2})$", url.rstrip("/"))
    if not m:
        return ""
    mon, day = int(m.group(1)), int(m.group(2))
    if not (1 <= mon <= 12 and 1 <= day <= 31):
        return ""
    year = time.localtime().tm_year
    try:
        return time.strftime("%Y-%m-%d", time.strptime(f"{year}-{mon:02d}-{day:02d}", "%Y-%m-%d"))
    except ValueError:
        return ""


def pretty(name):
    return re.sub(r"\s+", " ", name).strip()


def scrape_source(url):
    html = fetch(url).read().decode("utf-8", "replace")
    out = []
    for m in re.finditer(r'(?s)<script type="application/ld\+json">(.*?)</script>', html):
        blob = m.group(1)
        if "ItemList" not in blob:
            continue
        try:
            obj = json.loads(blob)
        except Exception:
            continue
        for it in obj.get("itemListElement", []):
            u = it.get("url", "")
            if not u:
                continue
            out.append({
                "name": pretty(it.get("name", "")),
                "url": u,
                "date": date_of(u),
                "src": urllib.parse.urlparse(url).netloc,
            })
    return out


def all_games():
    now = time.time()
    cached = DATE_CACHE.get("__all__")
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]
    rows = schedule_window()
    games, seen = [], set()
    matched = set()
    foreign = 0
    for src in GAME_SOURCES:
        try:
            scraped = scrape_source(src)
            SOURCE_HEALTH[src] = {"ok": True, "found": len(scraped), "at": time.strftime("%H:%M:%S")}
        except Exception as exc:
            SOURCE_HEALTH[src] = {"ok": False, "found": 0, "error": str(exc)[:80],
                                  "at": time.strftime("%H:%M:%S")}
            continue
        for g in scraped:
            away_txt, home_txt = slug_match(g["url"])
            hit = match_espn(away_txt or "", home_txt or "", rows) if away_txt else None
            if hit:
                g["date"] = hit["date"]
                g["time"] = local_time(hit["start_utc"])
                g["name"] = f"{hit['away']} vs {hit['home']}"
                g["verified"] = True
                g["utc"] = hit.get("start_utc", "")
                g["state"] = hit.get("state", "")
                g["detail"] = hit.get("detail", "")
                g["stream"] = True
                matched.add((hit["date"], hit["away"], hit["home"]))
            else:
                # The mirror's football section is football, not the NFL: on a Saturday
                # it lists college games (Alabama vs LSU, Air Force vs Army). An event
                # that matched no ESPN NFL fixture is another competition, so it is not
                # listed at all -- an NFL app showing college games is worse than one
                # that says "no stream listed yet".
                foreign += 1
                continue
            key = (g["name"], g["date"])
            if key in seen:
                continue
            seen.add(key)
            games.append(g)
        SOURCE_HEALTH.setdefault(src, {})["foreign"] = foreign    # events of other sports

    # keep the list honest: every scheduled game shows up, stream or not
    for r in rows:
        key = (r["date"], r["away"], r["home"])
        if key in matched:
            continue
        if any(g["name"] == f"{r['away']} vs {r['home']}" and g["date"] == r["date"] for g in games):
            continue
        games.append({
            "name": f"{r['away']} vs {r['home']}",
            "url": "",
            "date": r["date"],
            "time": local_time(r["start_utc"]),
            "utc": r.get("start_utc", ""),
            "state": r.get("state", ""),
            "detail": r.get("detail", ""),
            "src": "espn",
            "verified": True,
            "stream": False,
        })
    DATE_CACHE["__all__"] = (now, games)
    return games


DAY_CACHE = {}
DAY_TTL = 600


def day_rows(date):
    """ESPN fixtures for one exact day, fetched on demand.

    The warm window (schedule_window) covers today -3..+10 so the common case is
    instant, but the league schedule runs all season. Walking further with the
    date arrow used to end at the window edge and look like the schedule simply
    stopped; now any date in the season is one small request away, cached.
    """
    hit = DAY_CACHE.get(date)
    if hit and time.time() - hit[0] < DAY_TTL:
        return hit[1]
    try:
        import datetime as _dt
        ymd = _dt.date.fromisoformat(date).strftime("%Y%m%d")
    except Exception:
        return []
    rows = _safe_espn_day(ymd)
    if len(DAY_CACHE) > 240:                     # walking the season all night
        for k in sorted(DAY_CACHE, key=lambda k: DAY_CACHE[k][0])[:80]:
            DAY_CACHE.pop(k, None)
    DAY_CACHE[date] = (time.time(), rows)
    return rows


def _fixture(r):
    return {
        "name": f"{r['away']} vs {r['home']}",
        "url": "",
        "date": r["date"],
        "time": local_time(r["start_utc"]),
        "utc": r.get("start_utc", ""),
        "state": r.get("state", ""),
        "detail": r.get("detail", ""),
        "src": "espn",
        "verified": True,
        "stream": False,
    }


def games_for(date):
    gs = [g for g in all_games() if g["date"] == date]
    if not gs:
        # outside the warm window: ask ESPN about that day directly
        gs = [_fixture(r) for r in day_rows(date) if r.get("date") == date]
    gs.sort(key=lambda g: (g.get("utc") or "9999", g["name"]))
    return gs


# ------------------------------------------------------------------ channels
def channels():
    out = []
    if not os.path.isfile(FEEDS):
        return out
    for line in open(FEEDS, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        name, url = [p.strip() for p in line.rsplit("|", 1)]
        if url.startswith("http"):
            out.append({"name": name, "url": url})
    return out


# ------------------------------------------------------------------ health
# What we can actually measure from outside the player frame: whether the
# upstream playlist is still advancing and how fast segments arrive. A frozen
# picture caused by the site's own ad splice cannot be sensed by any code we
# run, so the player also exposes a one-key "frozen" report.
HEALTH = {}
HEALTH_LOCK = threading.Lock()
MASTERS = {}


def _find_master(url):
    if not os.path.isfile(FINDER):
        return None
    try:
        r = subprocess.run([find_python(), FINDER, "--url", url, "--timeout", "18"],
                           capture_output=True, timeout=60, creationflags=0x08000000)
        for line in reversed((r.stdout or b"").decode("utf-8", "replace").splitlines()):
            if line.startswith("{"):
                data = json.loads(line)
                if data.get("ok") and data.get("master"):
                    return data["master"]
    except Exception:
        pass
    return None


def master_for(page):
    """Resolve and remember the raw playlist behind a game page (cached)."""
    if page in MASTERS:
        return MASTERS[page]
    m = _find_master(page)
    if not m:
        for link in (feed_links(page) or [])[:2]:
            m = _find_master(link)
            if m:
                break
    if m:
        MASTERS[page] = m
    return m


def playlist_state(url):
    """(media sequence, segment count) of a live HLS playlist, or None."""
    try:
        txt = fetch(url, timeout=10).read().decode("utf-8", "replace")
    except Exception:
        return None
    seq = None
    n = 0
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(s.split(":", 1)[1])
            except ValueError:
                pass
        elif s and not s.startswith("#"):
            n += 1
    return (seq, n) if seq is not None else None


def health_of(page):
    url = master_for(page)
    if not url:
        return {"ok": False, "reason": "no playlist found"}
    now = time.time()
    st = playlist_state(url)
    with HEALTH_LOCK:
        h = HEALTH.setdefault(page, {"seq": None, "since": now, "stalls": 0, "freezes": 0})
        if st:
            seq, n = st
            if h["seq"] is None or seq != h["seq"]:
                h["seq"] = seq
                h["since"] = now
            stalled = now - h["since"]
        else:
            stalled = now - h["since"]
        h["stalled"] = round(stalled, 1)
        h["lagging"] = stalled > 15
        if h["lagging"]:
            h["stalls"] += 1
        return {"ok": True, "lagging": h["lagging"], "stalled_seconds": h["stalled"],
                "freezes": h["freezes"], "playlist": url[:80]}

PROFILE_DIR = os.path.join(ROOT, "browserprofile")
CLEAN_DIR = os.path.join(ROOT, "clean")
PROBE = os.path.join(ROOT, "probe_play.py")
HARVEST = os.path.join(ROOT, "harvest.py")
HARVEST_CACHE = {}
HARVEST_TTL = 120          # manifest tokens rotate: never hold one for long
FINDER = os.path.join(ROOT, "find_stream.py")
GAME_CACHE = {}
GAME_TTL = 900

# ---------------------------------------------------------------- chain walk
# The Android app resolves a game to its manifest with plain HTTPS GETs and no
# browser at all; this is the same walk, on the PC. The "Main N" link is a wrapper
# chain: a page that embeds an iframe, an iframe that builds another one in JS, and
# finally a page that names the manifest (a globalurl constant, DIRECT_STREAM_URL, a
# rotator entry, or a hard-coded .m3u8/.mpd). Some providers also leave the CDM's
# clear-key pair in the open next to it (masterinf = {"kid": "key"}).
#
# Why bother when Playwright works: no page JS runs, so none of the ad layers ever
# load, and there is no browser to launch (the portable bundle has no Playwright).
HARVEST_KEYS = {}          # last harvested clear-key pair: kid hex -> key hex

P_GLOBALURL = re.compile(r"globalurl\s*=\s*['\"]([^'\"\r\n]+)['\"]", re.I)
P_DIRECTURL = re.compile(r"DIRECT_STREAM_URL\s*=\s*['\"]([^'\"\r\n]+)['\"]", re.I)
P_VALUE = re.compile(r'"value"\s*:\s*"([^"]+)"', re.I)
P_MANIFEST = re.compile(r"(https?://[^\"'\\\s<>]+?\.(?:m3u8|mpd)[^\"'\\\s<>]*)", re.I)
P_IFRAME = re.compile(r"<iframe[^>]*?\bsrc\s*=\s*['\"]([^'\"]+)['\"]", re.I)
P_JS_SRC = re.compile(r"\.src\s*=\s*['\"](https?://[^'\"\r\n]+)['\"]")
P_MASTERINF = re.compile(r"masterinf\s*=\s*\{([^}]{10,600})\}", re.I)
P_KEYKV = re.compile(r'"?([0-9a-fA-F]{16,64})"?\s*:\s*"?([0-9a-fA-F]{16,64})"?')

ADISH = ("doubleclick", "googlesyndication", "adservice", "popads", "adcash",
         "propellerads", "histats.com", "onclick", "hilltopads", "exoclick")


def _adish(u):
    low = (u or "").lower()
    return any(a in low for a in ADISH)


def _host(u):
    try:
        return urllib.parse.urlparse(u).hostname or ""
    except Exception:
        return ""


def _keys_in(text):
    m = P_MASTERINF.search(text or "")
    if not m:
        return None
    out = {}
    for kid, key in P_KEYKV.findall(m.group(1)):
        out[kid.lower()] = key.lower()
    return out or None


def _manifest_in(text):
    """The manifest a wrapper page names, in the order the players try them."""
    for pat in (P_GLOBALURL, P_DIRECTURL):
        m = pat.search(text or "")
        if m:
            return m.group(1)
    m = P_MANIFEST.search(text or "")
    return m.group(1) if m else None


def _next_hop(text):
    """The next player URL a wrapper hands the picture to -- the *best* one.

    Football pages put their chat widget in the first <iframe>, so "the first frame
    wins" walks into a chat room and reports "no stream" while the player sits one
    frame over. Frames are therefore scored before they are followed: player-ish
    URLs first, chat/social frames last, ads never.
    """
    cands = []
    for m in P_IFRAME.finditer(text or ""):
        cands.append(m.group(1))
    for m in P_VALUE.finditer(text or ""):
        if "http" in (m.group(1) or ""):
            cands.append(m.group(1))
    for m in P_JS_SRC.finditer(text or ""):
        ctx = (text[max(0, m.start(1) - 260):m.start(1) + 260]).lower()
        if "iframe" in ctx:
            cands.append(m.group(1))

    best, best_score = None, -99
    for u in cands:
        if not u or u.startswith(("javascript:", "#")):
            continue
        score = _frame_score(u)
        if score > best_score:
            best, best_score = u, score
    if best is None or best_score <= -10:
        return None
    return best                    # relative values are joined by the caller


CHATTY = ("chat", "discord", "telegram", "t.me/", "whatsapp", "instagram", "tiktok",
          "facebook.com", "twitter.com", "x.com/", "youtube.com", "disqus", "livechat",
          "21aims")


def _frame_score(u):
    low = (u or "").lower()
    score = 0
    if _adish(low):
        return -99
    if any(c in low for c in CHATTY):
        score -= 10
    if "/embed" in low or "/player" in low or "play.php" in low or "/live/" in low:
        score += 6
    if "/main/" in low:
        score += 4
    if low.endswith(".m3u8") or low.endswith(".mpd") or ".m3u8?" in low or ".mpd?" in low:
        score += 5
    if low.startswith("http"):
        score += 1                 # a relative path still works, an absolute one is better
    return score


def _session_id(url):
    """The provider's own player appends sessionId=<guid> to *its* manifest family.

    Two rules, both learned the hard way: only that family (the Akamai "GP" feeds
    answer 400 without it, everything else ignores or breaks on an unknown param),
    and a correct separator -- `&sessionId=` on a URL with no query string turns the
    path into `index.m3u8&sessionId=...` and the CDN answers 404, which reads exactly
    like "the chain found nothing".
    """
    if "sessionid=" in url.lower() or not _wants_session(url):
        return url
    low = url.lower()
    if not ("akamaized" in low or "/gp/" in low or "lpnfl" in low):
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + "sessionId=" + str(uuid.uuid4())


def _wants_session(url):
    low = (url or "").lower()
    return "akamaized" in low or "/gp/" in low or "lpnfl" in low


def b64url_hex(hexstr):
    """A hex key as unpadded base64url -- the only shape a JWK accepts."""
    import base64
    raw = bytes.fromhex(hexstr)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def harvest_http(page):
    """Walk the chain with plain GETs. Returns the /api/harvest payload."""
    if not page:
        return {"ok": False, "error": "missing u"}
    links = feed_links(page) or [page]
    tried, traces = [], []
    for main in links[:4]:
        tried.append(main)
        hit = _follow_chain(main, page)
        if hit and hit.get("ok"):
            hit["page"] = page
            hit["tried"] = tried
            hit["http"] = True
            return hit
        if hit:
            traces.append(hit)
    return {"ok": False, "tried": tried, "chains": traces, "http": True}


def _follow_chain(first, event_page):
    referer = event_page or first
    seen, keys, trace = set(), None, []
    cur = first
    reason = "ran out of hops"
    for hop in range(6):
        if not cur:
            break
        if cur in seen:
            reason = "loop at hop %d" % hop
            break
        seen.add(cur)
        try:
            r = fetch(cur, timeout=20, headers={"Referer": referer})
            raw = r.read().decode("utf-8", "replace")
            final = r.geturl() or cur
        except Exception as exc:
            reason = "hop %d (%s) failed: %s" % (hop, _host(cur), exc)
            trace.append(reason)
            return {"ok": False, "error": reason, "trace": trace}
        keys = _keys_in(raw) or keys
        found = _manifest_in(raw)
        if found:
            found = urllib.parse.urljoin(final, found)
            found = _session_id(found)
            if keys:
                HARVEST_KEYS.clear()
                HARVEST_KEYS.update(keys)
            return {"ok": True, "m3u8": found, "via": _host(found),
                    "hops": len(seen), "referer": final,
                    "keys": dict(keys or {})}
        trace.append("hop %d: %s (%d bytes), no stream named" % (hop, _host(cur), len(raw)))
        nxt = _next_hop(raw)
        if not nxt:
            reason = "hop %d (%s) names no iframe and no stream" % (hop, _host(cur))
            break
        cur = nxt if nxt.startswith("http") else urllib.parse.urljoin(final, nxt)
    return {"ok": False, "error": reason, "trace": trace}


def harvest_payload(page, fresh=False):
    """The best answer for /api/harvest: the GET walk first, Playwright second."""
    hit = HARVEST_CACHE.get(page)
    if hit and not fresh and time.time() - hit[0] < HARVEST_TTL:
        return hit[1]
    payload = None
    try:
        payload = harvest_http(page)
    except Exception as exc:
        payload = {"ok": False, "error": str(exc)[:200], "http": True}
    if not payload.get("ok") and os.path.isfile(HARVEST):
        # the browser is still worth trying: it can see a modal, a cookie wall, a
        # player that only builds its URL on a click
        try:
            proc = subprocess.run([find_python(), HARVEST, page], cwd=ROOT,
                                  capture_output=True, text=True, timeout=120)
            pw = json.loads((proc.stdout or "{}").strip() or "{}")
            if pw.get("ok"):
                pw["keys"] = dict(HARVEST_KEYS)
                pw["http"] = False
                payload = pw
            else:
                payload.setdefault("playwright", pw.get("error") or pw.get("reason") or "no feed")
        except Exception as exc:
            payload.setdefault("playwright", str(exc)[:160])
    HARVEST_CACHE[page] = (time.time(), payload)
    return payload



def feed_links(page):
    """All 'Main N' player links on an event page, in order."""
    out = []
    try:
        html = fetch(page).read().decode("utf-8", "replace")
    except Exception:
        return out
    for m in re.finditer(r'https?://[^"\'<>\s]+?/main/(\d+)', html):
        u = m.group(0)
        if u not in out:
            out.append(u)
    out.sort(key=lambda u: int(re.search(r"/main/(\d+)", u).group(1)))
    return out


def probe(url):
    if not os.path.isfile(PROBE):
        return {"ok": False, "reason": "probe missing"}
    try:
        r = subprocess.run([find_python(), PROBE, "--url", url, "--timeout", "10"],
                           capture_output=True, timeout=60,
                           creationflags=0x08000000)
        line = (r.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        for s in reversed(line):
            if s.startswith("{"):
                return json.loads(s)
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    return {"ok": False, "reason": "no output"}


def resolve_game(page):
    """Try each feed until one actually starts playing."""
    now = time.time()
    hit = GAME_CACHE.get(page)
    if hit:
        ok = bool((hit[1].get("probe") or {}).get("ok"))
        if now - hit[0] < (GAME_TTL if ok else 45):
            return hit[1]
    links = feed_links(page) or [page]
    tried = []
    winner = None
    for link in links[:4]:
        st = probe(link)
        tried.append({"url": link, "ok": st.get("ok"), "reason": st.get("reason")})
        if st.get("ok"):
            winner = {"url": link, "probe": st, "tried": tried}
            break
    if not winner:
        winner = {"url": links[0], "probe": {"ok": False}, "tried": tried}
    GAME_CACHE[page] = (now, winner)
    return winner


def playable_target(res):
    """The URL a browser can navigate to for this game.

    probe_play reports the frame the picture was seen in, and that frame is
    often `about:srcdoc` -- a document that exists only inside its parent, so
    pointing an iframe or a tab at it yields a blank screen. Fall back to the
    feed page itself, which is the thing that was playing.
    """
    probe_info = res.get("probe") or {}
    frame = (probe_info.get("frame") or "").strip()
    if not re.match(r"^https?://", frame, re.I):
        frame = ""
    return frame or (res.get("url") or "")


SHELL_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>nfl</title>
<style>
  html,body{margin:0;padding:0;background:#000;overflow:hidden;height:100%}
  #wrap{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;background:#000}
  iframe{width:min(100%,calc(100vh * 16 / 9));height:min(100%,calc(100vw * 9 / 16));border:0;background:#000;display:block}
  #hint{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);color:#3a3a3a;
        font:13px -apple-system,"Segoe UI",sans-serif;z-index:0}
  #bar{position:fixed;top:0;left:0;right:0;height:0;z-index:9;overflow:visible}
  #reload{position:fixed;top:10px;right:12px;z-index:10;
          background:rgba(20,22,26,.86);border:1px solid #2c3038;color:#c9cdd6;
          border-radius:7px;padding:6px 11px;font:12px -apple-system,"Segoe UI",sans-serif;
          cursor:pointer;opacity:.25;transition:opacity .2s}
  #reload:hover{opacity:1;color:#fff;border-color:#3ddc84}
</style></head>
<body><div id="hint">loading stream...</div>
<div id="wrap"><iframe id="f" src="__SRC__" allow="autoplay; fullscreen; encrypted-media"
        sandbox="allow-scripts allow-same-origin allow-forms allow-presentation allow-orientation-lock"
        allowfullscreen referrerpolicy="no-referrer-when-downgrade"></iframe></div>
<button id="reload" title="stream froze? reload it">&#8635; reload stream</button>
<script>
  var f = document.getElementById('f');
  var base = f.src;
  document.getElementById('reload').addEventListener('click', function () {
    document.getElementById('hint').style.display = 'block';
    f.src = base + (base.indexOf('?') >= 0 ? '&' : '?') + '_r=' + Date.now();
  });
  f.addEventListener('load', function () {
    document.getElementById('hint').style.display = 'none';
  });
  setTimeout(function(){ document.getElementById('hint').style.display='none'; }, 8000);
</script>
</body></html>"""


def shell_page(src, referer=""):
    inner = "/play?u=" + urllib.parse.quote(src, safe="")
    if referer:
        inner += "&r=" + urllib.parse.quote(referer, safe="")
    return SHELL_HTML.replace("__SRC__", _attr(inner))


# ------------------------------------------------------------------ page wrapper
# The game pages cannot be embedded (X-Frame-Options) and their chrome is
# noise: nav bars, a chat panel, "clear your cache" banners. So we serve the
# page ourselves, inject CSS that hides their furniture, and patch fetch/XHR so
# the player keeps talking to their servers through us. Same origin, no CORS,
# no ads on the parts we control.
HIDE_CSS = """
<style id="nbaclean">
  header, nav, .navbar, .site-header, .topbar, .site-nav, .menu, .nav-links,
  aside, #chat, .chat, .chat-box, .chat-panel, .live-chat, [id*="chat"],
  [class*="chat-panel"], [class*="chatbox"],
  footer, .site-footer,
  .event-mirror-banner, .notice, .warning, .alert, [class*="banner"] {
    display: none !important;
  }
  html, body { background: #000 !important; margin: 0 !important; }
  body > * { max-width: 100% !important; }
  iframe[src*="chat"], iframe[title*="chat"], iframe[title*="Chat"] { display:none !important; }
</style>
"""

PATCH_JS = """
<script id="nbacleanjs">
(function () {
  // Runs inside every document the hub serves from a stream site: the page
  // itself, each nested player frame, and each srcdoc frame. Four jobs:
  //
  //   1. keep every subresource inside the proxy, so the request leaves with the
  //      site's own Referer/Origin instead of the hub's
  //   2. defuse the frame-busters ("if (top !== self) top.location = ..."), which
  //      is what made the site refuse to stay in an app window
  //   3. answer document.referrer with the upstream page, not the hub
  //   4. let window.open() play the picture here instead of in a popup a sandboxed
  //      frame is not allowed to create
  var ORIGIN = location.origin;
  var ME = (window.__nbaSelf || location.href);
  var UPREF = (window.__nbaRef || '');
  var REAL_TOP = window.top;
  var P = ORIGIN + '/play?u=';
  var E = ORIGIN + '/ext?u=';

  function isMine(u) { return !!u && u.indexOf(ORIGIN) === 0; }
  function abs(u) { try { return new URL(u, document.baseURI).href; } catch (e) { return ''; } }
  function wrap(u) {
    try {
      if (!u || typeof u !== 'string') return u;
      if (/^(data|blob|about|javascript):/i.test(u)) return u;
      if (isMine(u)) return u;
      return E + encodeURIComponent(new URL(u, document.baseURI).href)
             + '&r=' + encodeURIComponent(UPREF || ME);
    } catch (e) { return u; }
  }
  window.__nbaWrap = wrap;
  window.__nbaPlayUrl = function (u) {
    return P + encodeURIComponent(u) + '&r=' + encodeURIComponent(UPREF || ME);
  };

  // ---- 1. subresources -------------------------------------------------
  var _fetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      if (typeof input === 'string') input = wrap(input);
      else if (input && input.url) input = new Request(wrap(input.url), input);
    } catch (e) {}
    return _fetch.call(this, input, init);
  };
  var _open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u) {
    var a = Array.prototype.slice.call(arguments, 2);
    return _open.apply(this, [m, wrap(u)].concat(a));
  };
  if (navigator.serviceWorker) {
    try { navigator.serviceWorker.register = function () { return Promise.reject('blocked'); }; } catch (e) {}
  }

  // ---- 2. frame-busters ------------------------------------------------
  // A player page that notices it is framed navigates the top window away, or
  // blanks itself. Both checks are "am I the top document"; make that true.
  // defineProperty first (the spec makes top [Replaceable]), then a plain
  // assignment for the engines where the getter refuses to be shadowed.
  var shimmed = false;
  try {
    Object.defineProperty(window, 'top', {
      configurable: true, get: function () { return window.self; }
    });
    shimmed = (window.top === window.self);
  } catch (e) {}
  if (!shimmed) {
    try { window.top = window.self; shimmed = (window.top === window.self); } catch (e) {}
  }
  try { document.documentElement.setAttribute('data-nfl-top', shimmed ? 'self' : 'real'); } catch (e) {}
  try {
    Object.defineProperty(window, 'frameElement', {
      configurable: true, get: function () { return null; }
    });
  } catch (e) {}

  // ---- 3. document.referrer --------------------------------------------
  if (UPREF) {
    try {
      Object.defineProperty(document, 'referrer', {
        configurable: true, get: function () { return UPREF; }
      });
    } catch (e) {}
  }

  // ---- 4. window.open ---------------------------------------------------
  var fakeWin = null;
  function fakeWindow() {
    var w = {
      closed: false, name: '', opener: window.self,
      focus: function () {}, blur: function () {}, close: function () { w.closed = true; },
      postMessage: function (m, o) { try { REAL_TOP.postMessage(m, '*'); } catch (e) {} }
    };
    w.self = w; w.window = w;
    w.location = {
      assign: function (u) { go(u); }, replace: function (u) { go(u); }, set href(u) { go(u); },
      toString: function () { return 'about:blank'; }
    };
    w.document = { write: function () {}, writeln: function () {}, close: function () {} };
    return w;
  }
  function go(u) {
    var a = abs(u);
    if (!a || /^(data|blob|about|javascript):/i.test(a)) return;
    if (REAL_TOP && REAL_TOP !== window) {
      try {
        REAL_TOP.postMessage({ nfl_popup: a, nfl_ref: UPREF || ME }, '*');
        return;
      } catch (e) {}
    }
    location.replace(window.__nbaPlayUrl ? window.__nbaPlayUrl(a) : a);
  }
  window.open = function (u) {
    if (!fakeWin) fakeWin = fakeWindow();
    if (u) go(u);
    return fakeWin;
  };

  // ---- 5. tell the hub page when this frame is touched -------------------
  // When the player frame itself is the fullscreened element, pointer events never
  // reach the hub page, so its "hide the chrome, show it on touch" logic goes deaf.
  // One throttled message per gesture is enough to wake it.
  var lastWake = 0;
  function wake() {
    var now = Date.now();
    if (now - lastWake < 250) return;
    lastWake = now;
    try { if (REAL_TOP && REAL_TOP !== window) REAL_TOP.postMessage({ nfl_wake: 1 }, '*'); } catch (e) {}
  }
  ['pointerdown', 'pointermove', 'touchstart', 'keydown', 'wheel'].forEach(function (ev) {
    try { document.addEventListener(ev, wake, { passive: true }); } catch (e) {}
  });

  // ---- 6. keep nested frames in the proxy ------------------------------
  // Their own <iframe> is the thing the player hosts refuse to render for a
  // foreign parent. Served through /play it is a same-origin page of ours, with
  // the site's Referer on the way out.
  var REHOSTED = '__nbaRehosted';
  function rehost(el) {
    if (!el || el.nodeType !== 1) return;
    var tag = (el.tagName || '').toLowerCase();
    if (tag !== 'iframe' && tag !== 'frame') return;
    var sd = el.getAttribute('srcdoc');
    if (sd !== null && sd !== undefined && el.getAttribute(REHOSTED) !== '1') {
      el.setAttribute(REHOSTED, '1');
      var seed = '<base href="' + document.baseURI + '"><script>window.__nbaRef='
        + JSON.stringify(UPREF) + ';window.__nbaSelf=window.location.href;<\\/script>';
      el.setAttribute('srcdoc', seed + sd);
      return;
    }
    var s = el.getAttribute('src');
    if (!s || isMine(s) || el.getAttribute(REHOSTED) === '1') return;
    if (s.indexOf('/play?u=') >= 0 || s.indexOf('/ext?u=') >= 0) return;   // already ours
    var a = abs(s);
    if (!a || !/^https?:/i.test(a)) return;
    el.setAttribute(REHOSTED, '1');
    el.setAttribute('src', window.__nbaPlayUrl(a));
  }
  function rehostAll(root) {
    try {
      var list = (root || document).querySelectorAll('iframe, frame');
      for (var i = 0; i < list.length; i++) rehost(list[i]);
    } catch (e) {}
  }
  try {
    new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        var added = muts[i].addedNodes || [];
        for (var j = 0; j < added.length; j++) {
          var n = added[j];
          if (n.nodeType === 1) {
            rehost(n);
            if (n.querySelectorAll) rehostAll(n);
          }
        }
      }
    }).observe(document.documentElement, { childList: true, subtree: true });
  } catch (e) {}
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { rehostAll(); });
  } else { rehostAll(); }
})();
</script>
"""


AUTOPLAY_JS = """
<script id="nbaautoplay">
(function () {
  // The click that opened this page is the gesture, but it does not travel into a
  // new document, so the player sits on its poster. Mute it, press its own play
  // control, and call play() again over the first seconds until the picture
  // actually moves. The video element itself is never clicked twice: on most of
  // these players a click on the picture is a pause toggle, which is how an
  // "autostart" ended up leaving a black frame on screen.
  var tries = 0, kicked = false, clickedVideo = 0;
  var WANT = ['.play', '.play-button', '.btn-play', '[class*="play-button"]',
              '[id*="play-button"]', '[aria-label*="lay"]', '.poster', '.overlay',
              '[class*="poster"]', '[id*="play"]', '[class*="play"]'];
  function vid() { return document.querySelector('video'); }
  function kick() {
    tries++;
    var v = vid();
    if (v) {
      try { v.muted = true; } catch (e) {}
      try { var pr = v.play(); if (pr && pr.catch) pr.catch(function () {}); } catch (e) {}
    }
    if (v && v.paused && clickedVideo < 2) {
      clickedVideo++;
      try { v.click(); } catch (e) {}
    }
    for (var i = 0; i < WANT.length; i++) {
      var els;
      try { els = document.querySelectorAll(WANT[i]); } catch (e) { continue; }
      for (var j = 0; j < els.length && j < 3; j++) {
        var el = els[j];
        if (!el || el.disabled || el === v) continue;
        try { el.click(); } catch (e) {}
      }
    }
    // EME players reset the element after a key error: nudge it again
    if (v && !v.paused) {
      kicked = true;
      try { document.documentElement.setAttribute('data-nbaautoplay', 'playing'); } catch (e) {}
      try {
        var b = document.createElement('div');
        b.textContent = 'muted - tap the picture for sound';
        b.style.cssText = 'position:fixed;left:12px;top:12px;z-index:2147483647;'
          + 'background:rgba(20,22,26,.9);border:1px solid #2c3038;color:#c9cdd6;'
          + 'border-radius:8px;padding:8px 12px;font:13px -apple-system,Segoe UI,sans-serif;'
          + 'pointer-events:none';
        (document.body || document.documentElement).appendChild(b);
        setTimeout(function () { b.remove(); }, 6000);
      } catch (e) {}
      try {
        document.addEventListener('click', function once() {
          try { var vv = vid(); if (vv) { vv.muted = false; vv.play(); } } catch (e) {}
          document.removeEventListener('click', once);
        });
      } catch (e) {}
    }
    if (tries < 24 && !kicked) setTimeout(kick, 800);
  }
  try { document.documentElement.setAttribute('data-nbaautoplay', 'armed'); } catch (e) {}
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { setTimeout(kick, 350); });
  } else { setTimeout(kick, 350); }
})();
</script>
"""


def _attr(u):
    return (u or "").replace("&", "&amp;").replace('"', "&quot;")


FRAME_SRC = re.compile(r'(?is)(<(?:i?frame)\b[^>]*?\bsrc\s*=\s*)(["\'])(.*?)\2')


def rehost_frames(raw, doc_url, hub_origin=""):
    """Point every nested player frame at /play instead of at the site.

    The sites refuse to render their player for a foreign parent -- either with
    X-Frame-Options / frame-ancestors (which only our own response can waive) or
    with a Referer check that answers "This stream is not available here". A
    frame served by /play is a page of ours: no XFO applies, and the request we
    make for it carries the site's Referer. That is the difference between a
    black box and a picture.

    The replacement is absolute on purpose: the page carries a <base> pointing at
    the site, so a root-relative "/play?u=..." would resolve to the SITE's /play
    and 404 there.
    """
    prefix = (hub_origin or "").rstrip("/")

    def repl(m):
        src = (m.group(3) or "").strip()
        if not src or src.startswith(("data:", "blob:", "about:", "javascript:")):
            return m.group(0)
        if "/play?u=" in src or "/ext?u=" in src:
            return m.group(0)
        try:
            absolute = urllib.parse.urljoin(doc_url, src)
        except Exception:
            return m.group(0)
        if not absolute.startswith(("http://", "https://")):
            return m.group(0)
        proxied = (prefix + "/play?u=" + urllib.parse.quote(absolute, safe="") +
                   "&r=" + urllib.parse.quote(doc_url, safe=""))
        return m.group(1) + m.group(2) + _attr(proxied) + m.group(2)

    return FRAME_SRC.sub(repl, raw)


def proxy_page(url, referer=None, hub_origin=""):
    """The site's page, served as ours: our frame rules, their content.

    `referer` is the upstream page this document is embedded in. It becomes the
    Referer of the upstream fetch, the value document.referrer answers with
    inside the page, and the parent of any frame the page itself creates.
    `hub_origin` (e.g. http://127.0.0.1:8795) makes every rewritten frame URL
    absolute, because the page's own <base> would otherwise swallow it.
    """
    ref = referer if (referer and not our_own(referer)) else ""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", PROXY_UA)
    req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    req.add_header("Accept-Language", "en-US,en;q=0.9")
    req.add_header("Accept-Encoding", "identity")
    req.add_header("Referer", ref or url)
    org = scheme_host(ref or url)
    if org:
        req.add_header("Origin", org)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode("utf-8", "replace")
        final = r.geturl() or url

    # never let their CSP or frame rules apply to our copy
    raw = re.sub(r'(?is)<meta[^>]+http-equiv=["\']?(content-security-policy)["\']?[^>]*>', "", raw)
    raw = rehost_frames(raw, final, hub_origin)

    seed = ("<script>window.__nbaRef=%s;window.__nbaSelf=%s;</script>"
            % (json.dumps(ref or final), json.dumps(final)))
    inject = (f'<base href="{_attr(final)}">' + seed + HIDE_CSS + PATCH_JS + AUTOPLAY_JS)

    m = re.search(r"(?is)<head[^>]*>", raw)
    if m:
        raw = raw[:m.end()] + inject + raw[m.end():]
    else:
        raw = inject + raw
    return raw


# ------------------------------------------------------------------ relay pool
RELAYS = {}
RELAY_LOCK = threading.Lock()


def stop_relay(url):
    with RELAY_LOCK:
        item = RELAYS.pop(url, None)
    if not item:
        return
    proc, port = item
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
    except Exception:
        pass
    # terminate() does not run the child's cleanup, so the download-ahead folder
    # is removed here: "kept while you watch, gone when you stop" has to hold
    # even when the relay is killed.
    dvr = os.path.join(NFL_DIR, "dvr", str(port))
    shutil.rmtree(dvr, ignore_errors=True)


def sweep_dvr():
    """Remove DVR folders left behind by a crash or a previous session."""
    root = os.path.join(NFL_DIR, "dvr")
    if not os.path.isdir(root):
        return
    live = {str(p) for (_proc, p) in list(RELAYS.values())}
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        if name in live:
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass


def _stop_everything():
    """Retire every relay, then exit. Used by /api/shutdown (PC launcher)."""
    time.sleep(0.4)                      # let the HTTP reply reach the client
    for k in list(RELAYS.keys()):
        stop_relay(k)
    time.sleep(0.3)
    os._exit(0)


def start_relay(url, low=False, referer=None):
    key = url + ("#low" if low else "") + (("#" + referer) if referer else "")
    with RELAY_LOCK:
        item = RELAYS.get(key)
        if item:
            proc, port = item
            if proc.poll() is None:
                try:
                    r = fetch(f"http://127.0.0.1:{port}/health", timeout=3)
                    if r.status == 200:
                        return port
                except Exception:
                    pass
    stop_relay(key)
    port = free_port()
    if not port:
        return 0
    # one player at a time: retire the previous feed so we do not pile up
    for other in list(RELAYS.keys()):
        if other != key:
            stop_relay(other)
    window = "5" if low else "10"
    prefetch = "3" if low else "6"
    # download-ahead tier: keeps the minutes the RAM ring drops, so pausing or
    # rewinding does not depend on the source still having them. Wiped when the
    # relay exits, and swept on start if a previous run died.
    dvr_dir = os.path.join(NFL_DIR, "dvr", str(port))
    dvr_min = os.environ.get("NFLHUB_DVR_MIN", "15" if not low else "8")
    cmd = [find_python(), RELAY, "--url", url, "--port", str(port),
           "--host", RELAY_HOST,
           "--window", window, "--prefetch", prefetch, "--idle-timeout", "900",
           "--dvr", dvr_dir, "--dvr-minutes", dvr_min]
    if referer:
        cmd += ["--referer", referer]
    creation = 0x08000000  # CREATE_NO_WINDOW
    proc = subprocess.Popen(cmd, cwd=NFL_DIR, creationflags=creation,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with RELAY_LOCK:
        RELAYS[key] = (proc, port)
    # Return as soon as the relay is *listening*, not when its buffer is full.
    # Waiting for /health==200 held the click for 6-10 s; the page can start
    # hls.js against a relay that answers 503 and retries the manifest itself.
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            return 0
        try:
            r = fetch(f"http://127.0.0.1:{port}/health", timeout=2)
            if r.status == 200 and not relay_is_ours(port, url):
                # another relay owns this port and the page would play IT
                LogRelay("port %d belongs to a different relay, refusing" % port)
                proc.terminate()
                return 0
            return port if r.status in (200, 503) else port
        except urllib.error.HTTPError as exc:
            if exc.code == 503:          # warming: the socket is up, that is enough
                return port
        except Exception:
            pass
        time.sleep(0.2)
    return 0


def relay_is_ours(port, url):
    """Identity check: the relay on `port` must be serving `url`."""
    try:
        st = json.loads(fetch(f"http://127.0.0.1:{port}/stats", timeout=2).read())
    except Exception:
        return True                      # cannot tell; do not break a working start
    src = (st.get("source") or st.get("url") or "").strip()
    if not src:
        return True
    return src == url


def LogRelay(msg):
    print(f"[hub] {msg}", flush=True)


# --------------------------------------------------------- upstream identity
# The player pages and their licence servers do not only check the frame: they
# check who is asking. Three strings carry that identity, and the proxy has to
# rewrite every one of them or the request is refused even though it arrives:
#
#   Referer  the page the request came from (their own site, never 127.0.0.1)
#   Origin   the site the licence token was issued to
#   method   POST -- a licence challenge is a body upload, not a GET
#
# A request from the hub therefore leaves with the upstream site's own
# identity, which is the whole trick: the hub is the player's parent, not a
# third-party embedder.
PROXY_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# Headers we copy from the player's request straight through to upstream, so a
# licence POST keeps its content type, its range and its conditional GETs.
COPY_UP = ("Range", "If-Range", "If-None-Match", "If-Modified-Since", "Content-Type")
# Headers we copy back from upstream: the player needs the real status and the
# real body, and a 206 for a ranged media request is not a 200.
COPY_DOWN = ("Content-Type", "Content-Range", "Accept-Ranges", "Content-Disposition",
             "ETag", "Last-Modified")


def scheme_host(url):
    """'https://player.example' -- the Origin a browser would have sent."""
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return ""


def our_own(url):
    """True for a URL that points at the hub's own proxy, not at a stream site.

    The referral the browser sends from inside the app window looks like
    http://127.0.0.1:8795/play?u=... -- forwarding that upstream is exactly what
    the player hosts reject, so it is replaced before the request leaves.
    """
    u = (url or "").strip()
    if not u:
        return True
    if "/play?u=" in u or "/ext?u=" in u or "/watch?u=" in u:
        return True
    try:
        p = urllib.parse.urlparse(u)
    except Exception:
        return False
    if p.hostname not in ("127.0.0.1", "localhost", "0.0.0.0", "::1", "[::1]"):
        return False
    return (p.path in ("", "/")) or p.path.startswith("/api/") or p.path == "/index.html"


def strip_cookie_flags(raw):
    """A Set-Cookie the browser will store for 127.0.0.1.

    Domain= and Secure= are dropped: the cookie was issued to the site's host,
    and the browser is looking at the hub. The name=value and Path stay, which is
    all a Cloudflare clearance or a session id needs.
    """
    parts = [p.strip() for p in (raw or "").split(";") if p.strip()]
    if not parts:
        return ""
    keep = [parts[0]]
    for p in parts[1:]:
        low = p.lower()
        if low.startswith(("domain=", "secure", "samesite=none", "partitioned", "priority=")):
            continue
        keep.append(p)
    return "; ".join(keep)


# ------------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    server_version = "nflhub/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        for k, v in (extra or {}).items():
            if isinstance(v, (list, tuple)):
                for one in v:                       # Set-Cookie repeats
                    self.send_header(k, one)
            else:
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    # A Widevine licence request is a POST with an opaque binary body. Without
    # these three the proxied player could not acquire a key at all: BaseHTTPRequest
    # answers 501 to any method it has no do_* for, so the licence came back
    # "unsupported method" and the decoder stayed black while the page looked fine.
    def do_POST(self):
        try:
            return self._route()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:
            log_exc("POST " + self.path, exc)
            try:
                self._send(502, '{"ok":false,"error":"upstream proxy error"}')
            except Exception:
                pass

    def do_PUT(self):
        return self.do_POST()

    def do_PATCH(self):
        return self.do_POST()

    def do_DELETE(self):
        return self.do_POST()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,HEAD,POST,PUT,PATCH,OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _static(self, name, ctype):
        for base in (ROOT, NFL_DIR, os.path.dirname(NFL_DIR)):
            path = os.path.join(base, name)
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    data = fh.read()
                return self._send(200, data, ctype, {"Cache-Control": "no-cache"})
        return self._send(404, "not found", "text/plain")

    def do_GET(self):
        try:
            return self._route()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:
            log_exc("GET " + self.path, exc)
            try:
                self._send(500, '{"ok":false,"error":"hub handler error"}')
            except Exception:
                pass

    def _route(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if u.path == "/hls.min.js":
            return self._static("hls.min.js", "application/javascript")
        if u.path == "/shaka.js":
            # the page loads it with defer: a 404 only disables the Shaka path, which
            # is why it has to be served here and not only in the APK's assets
            return self._static("shaka.js", "application/javascript")

        if u.path == "/api/ping":
            # the app scans the LAN for this: it is the only way to find the PC
            # without the user typing an IP that DHCP may have changed
            host = request_hostname(self)
            port = self.server.server_port
            kind, exe = find_browser()
            return self._send(200, json.dumps({
                "ok": True, "app": "nflhub", "version": HUB_VERSION,
                "lan": f"http://{host}:{port}/", "hub": f"http://{host}:{port}",
                "relay_host": RELAY_HOST, "browser": kind or "", "browser_exe": exe or "",
                "time": time.strftime("%H:%M:%S"),
            }))

        if u.path == "/api/shutdown":
            # local helper only: the PC launcher owns the lifecycle and calls this
            # when the app window closes. Anything off this machine is refused,
            # otherwise a phone (or any page that guessed the port) could stop the hub.
            peer = (self.client_address or ("",))[0]
            if peer not in ("127.0.0.1", "::1", "localhost"):
                return self._send(403, '{"ok":false,"error":"localhost only"}')
            self._send(200, json.dumps({"ok": True, "stopping": True,
                                        "relays": len(RELAYS)}))
            threading.Thread(target=_stop_everything, daemon=True).start()
            return

        if u.path == "/api/games":
            date = (qs.get("date") or [""])[0]
            if not date:
                date = time.strftime("%Y-%m-%d")
            try:
                gs = games_for(date)
                return self._send(200, json.dumps({"date": date, "games": gs,
                                                   "sources": SOURCE_HEALTH,
                                                   "checked_at": time.strftime("%H:%M:%S")}))
            except Exception as exc:
                return self._send(200, json.dumps({"date": date, "games": [], "error": str(exc)}))

        if u.path == "/api/dates":
            try:
                gs = all_games()
                ds = {g["date"] for g in gs if g.get("date")}
                for r in schedule_window():
                    if r.get("date"):
                        ds.add(r["date"])
                # how many of that day's games actually have a stream listed:
                # the sources post a couple of days ahead, the schedule is known
                # for the whole season
                streams = {}
                for g in gs:
                    if g.get("stream"):
                        streams[g["date"]] = streams.get(g["date"], 0) + 1
                import datetime as _dt
                t = _dt.date.today()
                sy = t.year if t.month >= 9 else t.year - 1
                return self._send(200, json.dumps({
                    "dates": sorted(ds),
                    "streams": streams,
                    "today": t.isoformat(),
                    "season": {"start": _dt.date(sy, 10, 1).isoformat(),
                               "end": _dt.date(sy + 1, 4, 30).isoformat()},
                }))
            except Exception as exc:
                return self._send(200, json.dumps({"dates": [], "error": str(exc)}))

        if u.path == "/api/next":
            # "no games on this date" needs a way out: holidays exist (Nov 26 is
            # Thanksgiving, ESPN really returns 0 events), so point at the next
            # night that does have games instead of leaving the user at a dead end
            date = (qs.get("date") or [""])[0] or time.strftime("%Y-%m-%d")
            step = -1 if (qs.get("dir") or ["next"])[0] == "prev" else 1
            try:
                import datetime as _dt
                d = _dt.date.fromisoformat(date)
                tried = []
                for _ in range(20):
                    d = d + _dt.timedelta(days=step)
                    iso = d.isoformat()
                    tried.append(iso)
                    n = len(games_for(iso))
                    if n:
                        return self._send(200, json.dumps({"date": iso, "games": n,
                                                           "tried": tried}))
                return self._send(200, json.dumps({"date": None, "tried": tried}))
            except Exception as exc:
                return self._send(200, json.dumps({"date": None, "error": str(exc)}))

        if u.path == "/api/harvest":
            # The GET walk first: no browser, no page JS, no ad layer. Playwright is
            # kept as the fallback for a player that only builds its URL on a click.
            page = (qs.get("u") or [""])[0]
            if not page:
                return self._send(400, '{"ok":false,"error":"missing u"}')
            fresh = (qs.get("fresh") or ["0"])[0] == "1"
            return self._send(200, json.dumps(harvest_payload(page, fresh)))

        if u.path == "/api/clearkey":
            # What hls.js asks when a harvested feed is SAMPLE-AES with a key the
            # provider published next to the manifest. A JWK set, no license server.
            arr = []
            for kid, key in list(HARVEST_KEYS.items()):
                try:
                    arr.append({"kty": "oct", "kid": b64url_hex(kid), "k": b64url_hex(key)})
                except Exception:
                    pass
            return self._send(200, json.dumps({"keys": arr}))

        if u.path == "/api/keys" and self.command == "POST":
            # parity with the phone: the page may hand back a pair it found itself
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")
                ks = body.get("keys") or {}
                if isinstance(ks, dict):
                    HARVEST_KEYS.clear()
                    HARVEST_KEYS.update({str(k).lower(): str(v).lower() for k, v in ks.items()})
                return self._send(200, json.dumps({"ok": True, "keys": len(HARVEST_KEYS)}))
            except Exception as exc:
                return self._send(200, json.dumps({"ok": False, "error": str(exc)[:160]}))

        if u.path == "/api/harvest_settle":
            # private: what the last harvest left behind, for the diagnostics panel
            return self._send(200, json.dumps({"keys": len(HARVEST_KEYS),
                                               "cache": len(HARVEST_CACHE)}))

        if u.path == "/api/lp":
            # Game Pass ladder for one game: what renditions exist and what
            # protects them. The manifest and the segments are fetchable (checked:
            # a 702 KB 1080p fragment in 75 ms); the payload is SAMPLE-AES-CTR with
            # a Widevine PSSH + PlayReady header, so decoding needs a licence.
            page = (qs.get("u") or [""])[0]
            if not page:
                return self._send(400, '{"ok":false,"error":"missing u"}')
            try:
                h = HARVEST_CACHE.get(page)
                if not h or time.time() - h[0] >= HARVEST_TTL:
                    proc = subprocess.run([find_python(), HARVEST, page], cwd=ROOT,
                                          capture_output=True, text=True, timeout=120)
                    payload = json.loads((proc.stdout or "{}").strip() or "{}")
                    HARVEST_CACHE[page] = (time.time(), payload)
                    h = (time.time(), payload)
                hp = h[1]
                out = {"ok": bool(hp.get("ok")), "m3u8": hp.get("m3u8"),
                       "stream": hp.get("stream", {}), "renditions": [],
                       "protection": None, "playable": False}
                master = next((a for a in (hp.get("alternates") or [])
                               if "/index.m3u8" in a), None) or hp.get("m3u8")
                if master:
                    try:
                        txt = fetch(master, timeout=15).read().decode("utf-8", "replace")
                    except Exception:
                        txt = ""
                    for line in txt.splitlines():
                        if line.startswith("#EXT-X-STREAM-INF"):
                            kv = {}
                            for part in line.split(":", 1)[1].split(","):
                                if "=" in part:
                                    a, b = part.split("=", 1)
                                    kv[a.strip()] = b.strip().strip('"')
                            res = kv.get("RESOLUTION", "")
                            w = hgt = 0
                            if "x" in res:
                                try:
                                    w, hgt = [int(x) for x in res.split("x")]
                                except Exception:
                                    pass
                            bw = int(float(kv.get("BANDWIDTH", "0") or 0))
                            out["renditions"].append({"w": w, "h": hgt,
                                                      "mbps": round(bw / 1e6, 2),
                                                      "codecs": kv.get("CODECS", "")})
                        elif line.startswith("#EXT-X-SESSION-KEY") or line.startswith("#EXT-X-KEY"):
                            if not out["protection"]:
                                out["protection"] = line.split(":", 1)[1].split(",")[0]
                            if "SAMPLE-AES-CTR" in line or "widevine" in line.lower():
                                out["playable"] = False
                    out["renditions"].sort(key=lambda r: -r["h"])
                    out["best"] = out["renditions"][0] if out["renditions"] else None
                    out["playable"] = bool(out["renditions"]) and not out["protection"]
                return self._send(200, json.dumps(out))
            except Exception as exc:
                return self._send(200, json.dumps({"ok": False, "error": str(exc)[:200]}))

        if u.path == "/api/channels":
            return self._send(200, json.dumps({"channels": channels()}))

        if u.path == "/api/play":
            target = (qs.get("u") or [""])[0]
            low = (qs.get("low") or ["0"])[0] == "1"
            # r=<player page>: a harvested feed is not in feeds.txt, so the Referer
            # its CDN expects cannot come from the list; it comes per request.
            ref = (qs.get("r") or [""])[0]
            if not target:
                return self._send(400, '{"ok":false,"error":"missing u"}')
            port = start_relay(target, low=low, referer=ref or None)
            if not port:
                return self._send(200, json.dumps({"ok": False, "error": "relay failed"}))
            info = {}
            try:
                info = json.loads(fetch(f"http://127.0.0.1:{port}/stats", timeout=4).read())
            except Exception:
                pass
            host = request_hostname(self)
            return self._send(200, json.dumps({
                "ok": True,
                "url": f"http://{host}:{port}/live.m3u8",   # host the client used, relay port
                "local": f"http://127.0.0.1:{port}/live.m3u8",
                "port": port,
                "stats": info,
            }))

        if u.path == "/api/relaystats":
            # fallback for clients that cannot reach the relay port directly
            port = (qs.get("p") or [""])[0]
            if not port.isdigit():
                return self._send(400, '{"ok":false}')
            try:
                return self._send(200, fetch(f"http://127.0.0.1:{port}/stats", timeout=4).read(),
                                  "application/json")
            except Exception as exc:
                return self._send(200, json.dumps({"ok": False, "error": str(exc)}))

        if u.path == "/api/health":
            page = (qs.get("u") or [""])[0]
            if not page:
                return self._send(400, '{"ok":false}')
            return self._send(200, json.dumps(health_of(page)))

        if u.path == "/api/freeze":
            page = (qs.get("u") or [""])[0]
            with HEALTH_LOCK:
                h = HEALTH.setdefault(page, {"seq": None, "since": time.time(),
                                             "stalls": 0, "freezes": 0})
                h["freezes"] += 1
                n = h["freezes"]
            return self._send(200, json.dumps({"ok": True, "freezes": n}))

        if u.path == "/api/candidates":
            page = (qs.get("u") or [""])[0]
            if not page:
                return self._send(400, '{"ok":false}')
            out = []
            for link in (feed_links(page) or [page])[:3]:
                st = probe(link)
                out.append({"url": link, "ok": bool(st.get("ok")),
                            "h": st.get("h", 0), "w": st.get("w", 0),
                            "frame": st.get("frame", "")})
            out.sort(key=lambda c: (not c["ok"], -c["h"]))
            return self._send(200, json.dumps({"ok": True, "candidates": out}))
        if u.path == "/api/resolve":
            page = (qs.get("u") or [""])[0]
            if not page:
                return self._send(400, '{"ok":false,"error":"missing u"}')
            res = resolve_game(page)
            probe_info = res.get("probe") or {}
            target = playable_target(res)
            return self._send(200, json.dumps({
                "ok": bool(probe_info.get("ok")),
                "frame": target,
                "open": target,
                "source": res["url"],
                "event": page,
                "probe": probe_info,
            }))
        if u.path == "/watch":
            target = (qs.get("u") or [""])[0]
            if not target:
                return self._send(400, "missing u", "text/plain")
            return self._send(200, shell_page(target, target), "text/html; charset=utf-8")

        if u.path == "/play":
            target = (qs.get("u") or [""])[0]
            if not target:
                return self._send(400, "missing u", "text/plain")
            ref = (qs.get("r") or [""])[0]
            origin = "http://" + (self.headers.get("Host") or
                                  "127.0.0.1:%d" % self.server.server_port)
            try:
                html = proxy_page(target, ref, origin)
            except Exception as exc:
                return self._send(502, f"could not load that page: {exc}", "text/plain")
            return self._send(200, html, "text/html; charset=utf-8")

        if u.path == "/ext":
            target = (qs.get("u") or [""])[0]
            if not target:
                return self._send(400, "missing u", "text/plain")
            return self._passthrough(target, (qs.get("r") or [""])[0])

        if u.path == "/api/watch":
            page = (qs.get("u") or [""])[0]
            if not page:
                return self._send(400, '{"ok":false,"error":"missing u"}')
            res = resolve_game(page)
            target = playable_target(res)
            # if we found the player host, show ONLY that, inside our own shell:
            # no site nav, no chat column, no ad overlays
            open_url = target
            if target and target != res["url"]:
                open_url = (f"http://127.0.0.1:{self.server.server_port}/watch?u=" +
                            urllib.parse.quote(target, safe=""))
            args = browser_argv(open_url, PROFILE_DIR)
            if not args:
                return self._send(200, json.dumps({"ok": False, "url": target,
                                                   "error": "no browser found (see browser.txt)"}))
            try:
                subprocess.Popen(args, close_fds=True)
            except Exception as exc:
                return self._send(200, json.dumps({"ok": False, "url": target, "error": str(exc)}))
            return self._send(200, json.dumps({"ok": True, "url": open_url,
                                               "browser": args[0],
                                               "source": res["url"], "frame": target,
                                               "probe": res.get("probe", {}),
                                               "tried": res.get("tried", [])}))

        if u.path == "/api/open":
            target = (qs.get("u") or [""])[0]
            if not target:
                return self._send(400, '{"ok":false,"error":"missing u"}')
            args = browser_argv(target, PROFILE_DIR)
            if args:
                try:
                    subprocess.Popen(args, close_fds=True)
                except Exception as exc:
                    return self._send(200, json.dumps({"ok": False, "error": str(exc)}))
                return self._send(200, json.dumps({"ok": True, "browser": args[0]}))
            return self._send(200, '{"ok":false,"error":"no browser found"}')

        if u.path == "/api/open":
            target = (qs.get("u") or [""])[0]
            if target:
                webbrowser.open(target)
            return self._send(200, '{"ok":true}')

        return self._send(404, "not found", "text/plain")

    # ------------------------------------------------------------ upstream
    def _body(self):
        """The request body, exactly as the browser sent it (binary safe).

        A licence challenge is a few hundred bytes of opaque bytes; a segment
        upload could be megabytes. Chunked bodies are decoded too, because
        fetch() sends one whenever the caller passes a stream.
        """
        if (self.headers.get("Transfer-Encoding") or "").lower().find("chunked") >= 0:
            out = bytearray()
            while True:
                line = self.rfile.readline(65536).strip()
                try:
                    n = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if n <= 0:
                    self.rfile.readline(65536)          # trailing CRLF
                    break
                out += self.rfile.read(n)
                self.rfile.read(2)
            return bytes(out) or None
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return None
        data = self.rfile.read(n)
        return data or None

    def _passthrough(self, target, referer=None):
        """One upstream request, method and body included.

        This is the piece that made the app window black while Firefox played:
        only GET was ever forwarded, so the Widevine licence POST died here with
        a 501 and the player never got a key. Everything downstream of that --
        the ranged media requests, the ad-token refreshes, the playlist reloads
        -- is the same function, so it has to be a real proxy, not a fetcher.
        """
        method = (getattr(self, "command", "GET") or "GET").upper()
        data = self._body() if method in ("POST", "PUT", "PATCH", "DELETE") else None
        try:
            req = urllib.request.Request(target, data=data, method=method)
        except Exception as exc:
            return self._send(400, f"bad upstream url: {exc}", "text/plain")

        req.add_header("User-Agent", PROXY_UA)
        req.add_header("Accept", self.headers.get("Accept") or "*/*")
        req.add_header("Accept-Language", self.headers.get("Accept-Language") or "en-US,en;q=0.9")
        req.add_header("Accept-Encoding", "identity")
        for h in COPY_UP:
            val = self.headers.get(h)
            if val:
                req.add_header(h, val)

        # Referer / Origin: never ours. The page that would have made this
        # request is the upstream one, and that is the string to send.
        ref = (referer or "").strip()
        if not ref or our_own(ref):
            ref = (self.headers.get("Referer") or "").strip()
        if not ref or our_own(ref):
            ref = target if method == "GET" else (scheme_host(target) + "/")
        req.add_header("Referer", ref)

        org = (self.headers.get("Origin") or "").strip()
        if not org or our_own(org):
            # A same-origin GET has no Origin at all; inventing one is a good way to
            # be turned away by a CDN. Only uploads need to name the site.
            org = scheme_host(target) if method in ("POST", "PUT", "PATCH") else ""
        if org:
            req.add_header("Origin", org)

        ck = self.headers.get("Cookie")
        if ck:
            req.add_header("Cookie", ck)

        try:
            r = urllib.request.urlopen(req, timeout=45)
            status = r.status
        except urllib.error.HTTPError as exc:      # 4xx/5xx still has a body to relay
            r = exc
            status = exc.code
        except Exception as exc:
            return self._send(502, f"upstream: {exc}", "text/plain")

        try:
            body = r.read()
        except Exception:
            body = b""
        final = r.geturl()
        ctype = r.headers.get("Content-Type") or "application/octet-stream"

        extra = {"Cache-Control": "no-store"}
        for h in COPY_DOWN:
            if h == "Content-Type":
                continue
            val = r.headers.get(h)
            if val:
                extra[h] = val
        cookies = []
        try:
            for c in (r.headers.get_all("Set-Cookie") or []):
                c = strip_cookie_flags(c)
                if c:
                    cookies.append(c)
        except Exception:
            pass
        if cookies:
            extra["Set-Cookie"] = cookies
        for h in ("Access-Control-Allow-Origin", "Access-Control-Allow-Headers",
                  "Access-Control-Allow-Methods", "Access-Control-Expose-Headers"):
            val = r.headers.get(h)
            if val:
                extra[h] = val
        loc = r.headers.get("Location")
        if loc and status in (301, 302, 303, 307, 308):
            # keep redirects inside the proxy, or the player silently leaves it
            extra["Location"] = "/ext?u=" + urllib.parse.quote(
                urllib.parse.urljoin(final, loc), safe="")

        if ("mpegurl" in ctype.lower() or body[:7] == b"#EXTM3U") and method != "HEAD":
            text = body.decode("utf-8", "replace")
            out = []
            for line in text.splitlines():
                s = line.strip()
                if not s:
                    out.append(line)
                    continue
                if s.startswith("#"):
                    if 'URI="' in s:
                        head, _, rest = s.partition('URI="')
                        uri, _, tail = rest.partition('"')
                        absu = urllib.parse.urljoin(final, uri)
                        out.append(head + 'URI="/ext?u=' +
                                   urllib.parse.quote(absu, safe="") +
                                   '&r=' + urllib.parse.quote(final, safe="") + '"' + tail)
                    else:
                        out.append(s)
                    continue
                absu = urllib.parse.urljoin(final, s)
                out.append("/ext?u=" + urllib.parse.quote(absu, safe="") +
                           "&r=" + urllib.parse.quote(final, safe=""))
            return self._send(200, "\n".join(out) + "\n",
                              "application/vnd.apple.mpegurl", extra)

        return self._send(status, body, ctype, extra)


def main():
    global RELAY_HOST
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8795)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = reachable from your phone")
    args = ap.parse_args()

    RELAY_HOST = "0.0.0.0" if args.host in ("0.0.0.0", "::", "") else "127.0.0.1"

    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"port {args.port} busy: {exc}", file=sys.stderr)
        sys.exit(1)
    url = f"http://127.0.0.1:{args.port}/"
    lan = lan_ip()
    print(f"nfl hub at {url}")
    if lan and args.host == "0.0.0.0":
        print(f"phone url  http://{lan}:{args.port}/")
    print(f"relay binds {RELAY_HOST} (ports 8960-9019)")
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for k in list(RELAYS.keys()):
            stop_relay(k)


if __name__ == "__main__":
    main()






