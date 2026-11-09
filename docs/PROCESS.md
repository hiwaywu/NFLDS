# PROCESS — how NFLDS was made, and what is football-specific

This is a port of NBADS (the NBA hub) to American football. The engine — schedule scraping, the
wrapper-chain harvester, the buffering relay, the `/play` proxy, the ad gates, fullscreen, the
Android shell — is the same code, so the interesting part is what did **not** survive a rename.

## 1. What was copied, and what was renamed

```text
NBADS/                          NFLDS/
  nba/hub/hub.py        ->        nfl/hub/hub.py            (ESPN + sources + ports changed)
  nba/hub/index.html    ->        nfl/hub/index.html        (branding, storage key, labels)
  nba/hub/{hls,shaka}.js->        unchanged (vendor bundles)
  nba/{relay.py,feeds.txt}->      nfl/...                   (channels replaced)
  pc/NBAPC.cs           ->        pc/NFLPC.cs               (mutex, ports, window title)
  android/src/com/nbahub/app/* -> android/src/com/nflds/app/*  (package com.nflds.app)
  tools/*               ->        tools/*                   (paths, package name, ports)
```

Mechanical pass, in a fixed order so compound names survive first:
`com.nbahub.app` -> `com.nflds.app`, `NBAHub`/`NBAHUB`/`nbahub` -> `NFLHub`/`NFLHUB`/`nflhub`,
`basketball/nba` -> `football/nfl`, `nba\`/`nba/` -> `nfl\`/`nfl/`, `lpnba` -> `lpnfl`,
`NBA TV` -> `NFL TV`, then `\bNBA\b` -> `NFL`, `\bnba\b` -> `nfl`. The vendor bundles
(`hls.min.js`, `shaka.js`) are excluded from every substitution, and the word-boundary forms keep
English words like "unbalanced" intact.

**Ports were moved on purpose**: hub `8795` (was 8790), relay pool `8960-9019` (was 8900-8959), so
both apps can run side by side on the same PC without stealing each other's relays or answering
each other's `/api/ping`.

## 2. Football-specific work

### 2.1 The chat iframe comes first

On the NBA pages the player iframe was the one worth following. On football pages the first
`<iframe>` is the **chat widget**:

```text
foxiy.st/.../main/1
   <iframe src="https://trendy48.site/embed/...">   <- the player
   <iframe src="https://chat.21aims.shop/">         <- the furniture
```

"The first frame wins" walked into a chat room and reported `no stream` while the picture sat one
frame over. Both walkers now **score** candidates (`_frame_score` in Python, `frameScore` in Java):

| Signal | Score |
|---|---|
| ad host (`_adish`) | -99 (never followed) |
| chat / discord / telegram / twitter / youtube / `21aims` | -10 |
| `/embed`, `/player`, `play.php`, `/live/` | +6 |
| `.m3u8` / `.mpd` | +5 |
| `/main/` | +4 |
| absolute http(s) | +1 |

Best score wins; anything at or below -10 is refused, so a page with only chat frames returns
"names no iframe and no stream" instead of chasing it.

### 2.2 Nicknames are shared with college football

`methstreams.st/NFL` is **football**, not the NFL: on a Saturday it lists 25 college games
(`Alabama vs LSU`). The NFL slate only appears on game day. Two consequences:

- the app must join the mirror's list to ESPN's **NFL** fixtures by name, not by URL count;
- the matcher requires **both** nicknames to appear on the right sides
  (`away_split[-1] in away_txt and home_split[-1] in home_txt`). A one-sided hit is useless here:
  "Falcons" is Air Force, "Cardinals" is Ball State, "Panthers" is Georgia State. Measured on
  2026-11-07 (a Saturday): `/api/games?date=2026-11-08` returned **13 NFL games, 0 with a stream
  listed** — which is the honest answer, not a bug.

### 2.3 The channels

The NBA file had four NBA TV feeds tied to that league's FAST channel ids. Probed replacements
(2026-11-07), each verified by parsing the master playlist and fetching the first segment:

| Name | URL | Result |
|---|---|---|
| NFL Channel (Samsung TV Plus / Akamai) | `https://pb-we3ltka9xobj6.akamaized.net/master.m3u8` | master, 4 renditions to 1920x1080, 24 segments, first segment 763 KB, no key |
| NFL Channel (Pluto via `jmp2.uk`) | `https://jmp2.uk/plu-5ced7d5df64be98e07ed47b6.m3u8` | redirects to Pluto's stitcher with a JWT, 5 renditions to ~3.3 Mbps, **AES-128** (`#EXT-X-KEY`), first segment 388 KB |
| NFL Network (aynaott) | `https://tvsen6.aynaott.com/nfl/index.m3u8` | 768x432, 2 renditions, first segment 926 KB |

The Pluto feed is the reason `relay.py` keeps `#EXT-X-KEY` and proxies the key instead of stripping
it; the relay never needed a change for it.

The candidate list came from the public `iptv-org` index (`channels.json` + `streams.json`), filtered
to ids containing `nfl` and then tested one by one — the index lists 31 NFL-named channels and only
3 had a live, playable URL. RedZone and Sunday Ticket are in the index but were **not** live.

### 2.4 The inline-playlist family (2026-11-08, live NFL Sunday)

The NBA port learned the chain `event → /main/N → embed → manifest`. On the NFL slate one family
does not work that way at all, and every assumption above breaks on it at once:

```text
foxiy.st/event/.../main/1
  <iframe id="streamIframe" src="https://trendy48.site/embed/3zw64nbs-8718">
    -> trendy48.site/top/chargers
       -> topstreamer.site/iframe/nfl/chargers       <- the player, no manifest anywhere
```

What that last page serves (85 KB, only to a browser-ish request):

```js
var key = 'chargers';
var globalurl = 'chargers.m3u8';                       // a dud: 404 on its own host
var masterinf = {"video5_chargers.m3u8": {"inf": "#EXT-X-STREAM-INF:BANDWIDTH=10000000,
                 RESOLUTION=1920x1080,..."}, ...};     // the ladder
var payload = {"name": "chargers", "renditions": {
                 "video5_chargers": "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:2230\n...
                 https://o300801.mp.lura.live/live/ephemeral/.../segment_...c.ts?Expires=...&Signature=..."}};
```

- **No playlist request exists.** The site's own shaka player is fed a `blob:` built from those
  strings, so the app's invisible-engine sniffer — which waits for a `.m3u8` on the wire — caught
  nothing, on any of three candidate pages (`sniffStream` → `null` after ~6 s each).
- **`globalurl` is a name, not a URL.** It joined to `.../iframe/nfl/chargers.m3u8`, the relay asked
  for it, got 404, and the page fell through to "that feed would not start — falling back to the
  player page". That was the reported symptom: the app left its own player.
- **The page is flaky cold.** The same URL answers ~18 KB with no `payload` and ~85 KB with the whole
  ladder a second later, so a single fetch is a coin toss.

The fix is three small pieces, in the shape of the provider's own player:

| Piece | Where | What it does |
|---|---|---|
| `inline_from(html)` / `inlineIn(text)` | `hub.py`, `index.html`, `LocalHub.java` | brace-matched, string-aware JSON extraction of `masterinf` + `payload.renditions`; ranks by `BANDWIDTH` |
| `/api/inline` + `/inline/<id>/{index,videoN_*}.m3u8` | all three | remembers the page and *is* the playlist: a master with real `#EXT-X-STREAM-INF` lines plus one media playlist per rung, re-read from the page per request (3 s cache, 3 tries for the shell) |
| `isPlaylist(url)` | all three | a name a page invented must answer `#EXTM3U` before the walk believes it; a dud is a trace line, and the walk continues to the next frame instead of stopping |

The relay needed no change: variant URIs are relative to the master it fetched, and the segment URIs
inside the media playlist are absolute, so `urllib.parse.urljoin` keeps them and the relay proxies
them with the player page as `Referer` — which is what the CDN signatures are for.

Measured on a live game (Texans @ Chargers, 2026-11-08):

```text
PC    --app window : 1920x1080, ADVANCE 6.00s/6s, audio 171815 bytes, blob:http://127.0.0.1:8801
phone (Pixel 7)    : 1920x1080, ADVANCE 6.00s/6s, audio +94775 in 6s, relay variant
                     http://127.0.0.1:8795/inline/ad739aa4a3617e31/video5_chargers.m3u8 @ 10 Mbps
```

Offline proof, no network and no browser: `python tools/verify_inline_family.py` → 13/13 (ladder
found and ranked, master/variant pair answer, the dud `globalurl` rejected, the 18 KB shell not
mistaken for a ladder).

### 2.5 Everything from the NBA app that carried over unchanged

- the licence POST proxy (`do_POST` on `/ext`), the `Referer`/`Origin` rewrite, frame rehosting,
  the popup gate and the "a tap is not a licence to leave" navigation rule;
- the harvest walk itself (6 hops, loop guard, `&sessionId=` append, `masterinf` capture) and
  `/api/clearkey` + shaka for a CENC feed with a published key;
- the fullscreen fade (chrome out of the way, touch to bring it back, platform progress row off);
- `NFL TV` in our own player, `low delay`, DVR window, the diagnostics panel.

## 3. Verification

Both harnesses are the NBA ones with the paths/package renamed; the fixture is sport-agnostic.

```text
python tools/fixture_stream.py          the offline mirror: XFO, referer gate, licence POST, frames
python tools/verify_inline_family.py   13/13   inline ladder, master/variant pair, dud name, shell
python tools/verify_inapp_game.py      29/29   PC, real --app Chrome window over CDP
python tools/verify_android_inapp.py   14/14   Pixel 7 / Android 15 emulator, releases/NFLHub.apk
python tools/live_probe.py             one live game: advance, audio bytes, source, relay stats
```

Live checks on this build:

```text
GET /api/ping      -> {"ok":true,"app":"nflhub","version":"1.3","lan":"http://127.0.0.1:8795/"}
GET /api/games?date=2026-11-08 -> 13 NFL games from ESPN, 4 in progress, each with a stream listed
GET /api/harvest?u=<live event> -> ok:true, 1920x1080 manifest, drm:false
POST /api/inline {page, referer} -> {"ok":true,"id":"ad739…","m3u8":"http://127.0.0.1:8801/inline/ad739…/index.m3u8"}
GET /inline/<id>/index.m3u8    -> 200 master, 1080p rung first
GET /inline/<id>/video5_….m3u8 -> 200 media playlist, absolute signed luma.live segments
```

## 4. Known gaps

- **The mirrors are the only real dependency.** On 2026-11-08 the plain-GET walk still hit
  `trendy48.site` → 403 (Cloudflare JA3, §3.5 of the NBA SOP) on the PC, so the PC used Playwright;
  the phone reached the same pages through the WebView bridge, which is why the inline family is
  resolvable there without a browser. A mirror family that hides both the playlist *and* its page
  from a plain fetch would need the engine again.
- The `bundled` channel list can outlive a provider's id. `feeds.txt` is refreshed from `main`, so
  fixing it means editing that file, not shipping an APK.
- `NFLHub.exe` is 45 KB (the NBA one was 25 KB) because the icon carries seven sizes.
