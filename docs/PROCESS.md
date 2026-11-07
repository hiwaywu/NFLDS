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

### 2.4 Everything from the NBA app that carried over unchanged

- the licence POST proxy (`do_POST` on `/ext`), the `Referer`/`Origin` rewrite, frame rehosting,
  the popup gate and the "a tap is not a licence to leave" navigation rule;
- the harvest walk itself (6 hops, loop guard, `&sessionId=` append, `masterinf` capture) and
  `/api/clearkey` + shaka for a CENC feed with a published key;
- the fullscreen fade (chrome out of the way, touch to bring it back, platform progress row off);
- `NFL TV` in our own player, `low delay`, DVR window, the diagnostics panel.

## 3. Verification

Both harnesses are the NBA ones with the paths/package renamed; the fixture is sport-agnostic.

```text
python tools/verify_inapp_game.py       29/29   PC, real --app Chrome window over CDP
python tools/verify_android_inapp.py    14/14   Pixel 7 / Android 15 emulator, releases/NFLHub.apk
```

Live checks on this build:

```text
GET /api/ping      -> {"ok":true,"app":"nflhub","version":"1.0","lan":"http://127.0.0.1:8795/"}
GET /api/games?date=2026-11-08 -> 13 NFL games from ESPN, both football sources ok (25 events each)
GET /api/harvest?u=<live college event> -> ok:false with a per-hop trace naming the chat frame it
                                           refused, i.e. the ranking doing its job
```

## 4. Known gaps

- **No live NFL game has been harvested yet** in this build: the mirror's football section was
  college on the day it was written. The chain, the ranking, the relay and both shells are proven
  against the fixture and against a live college-football chain (`foxiy.st` -> `trendy48.site`), but
  the first NFL Sunday is the real test. When it fails, `/api/harvest` returns `chains[]` with a
  trace per hop — read the last line, it names the host and the reason.
- The `bundled` channel list can outlive a provider's id. `feeds.txt` is refreshed from `main`, so
  fixing it means editing that file, not shipping an APK.
- `NFLHub.exe` is 45 KB (the NBA one was 25 KB) because the icon carries seven sizes.
