# NFLDS — NFL stream hub + Android app

A local hub that lists **tonight's NFL games from the real schedule** (ESPN), finds a stream for the
one you click, and plays it **inside the app window** — no browser hand-off, no popup, no ad wall.
The phone app carries the hub itself: no PC needed.

This is a port of [NBADS](../NBADS) (the NBA version) to American football. Same engine, same
tricks, football-specific differences — see [docs/PROCESS.md](docs/PROCESS.md) for the port map.

| Piece | Runs on | What it does |
|---|---|---|
| `nfl/` + `pc/` | Windows (Python 3.11+ or the portable bundle) | schedule sync, feed harvesting, HLS relay, browser UI, `NFLHub.exe` launcher |
| `android/` | any Android 7+ phone | standalone app: hub + relay + WebView inside one 385 KB APK |

---

## Quick start (PC)

```
nfl\hub\NFLHub.exe
```

Double-click. It starts the hub on **8795** if it is not already running, waits until it answers,
and opens a **chromeless Chromium window** (`--app`, a private profile) showing the list. That
window *is* the app: no tabs, no address bar, its own icon in the taskbar.

Needs, in the same tree: `hub.py`, `index.html`, `hls.min.js`, `shaka.js`, `probe_play.py`,
`find_stream.py`, `harvest.py` in `nfl\hub\`; `relay.py`, `feeds.txt` in `nfl\`; Python 3.11+
with `requests`; Chrome/Chromium/Firefox/Brave; .NET 4.x. The portable bundle
(`releases/NFLHub-portable.zip`) carries its own Python, so double-clicking `START.bat` is enough.

```text
NFLHub.exe                0.0.0.0:8795 (the phone can connect), app window
NFLHub.exe --local        bind 127.0.0.1 only
NFLHub.exe --port 8796    another port
NFLHub.exe --size 1920x1080
NFLHub.exe --browser firefox
```

Close the window and everything stops: relays and the hub are shut down through a loopback-only
`/api/shutdown`. Log of every decision: `%LOCALAPPDATA%\NFLHubPC\launcher.log`.

## Quick start (Android)

Install `releases/NFLHub.apk` and open it. Standalone — no PC, works on mobile data too. The hub
runs inside the app on `127.0.0.1:8795` (loopback only: nothing on the wifi can reach it).

```powershell
adb install --no-incremental releases\NFLHub.apk
adb shell am start -n com.nflds.app/.MainActivity --es chan 0     # straight into NFL TV
adb shell am start -n com.nflds.app/.MainActivity --es url https://host/main/1   # any page
```

## How a game gets on screen

1. **The list** comes from ESPN's NFL scoreboard (`football/nfl`), joined with whatever the mirrors
   list under `/NFL` (`methstreams.st`, `crackstreams.st`) by team name, with the site's UTC-date
   quirk handled: a 1 PM Sunday kick-off is numbered with the same date and matched against the
   ESPN fixture, not against the URL.
2. **The stream** is resolved by walking the wrapper chain the "Main N" link hides — plain HTTPS
   GETs, no page JS, so no ad layer ever loads:
   `event → /main/N → wrapper → wrapper → manifest` (`globalurl`, `DIRECT_STREAM_URL`, a rotator
   `"value"`, or a hard-coded `.m3u8`/`.mpd`).
   Football pages put their **chat widget in the first iframe**, so frames are *scored* before they
   are followed (player-ish first, chat/social last, ad hosts never) — otherwise the walk reports
   "no stream" while the player sits one frame over. A manifest a page *names* is fetched before it
   is believed: `globalurl = 'chargers.m3u8'` joins to a 404 on the page's own host, and accepting it
   is how a live game "resolved" and then could not start.
3. **The picture** is our own `<video>` fed by the local relay (`/api/play` → 8960-9019), which pulls
   the feed with the `Referer`/`Origin` its CDN expects (`&r=<player page>`). A feed that is
   Widevine-packaged but ships its clear key in the open (`masterinf = {"kid": "key"}`) plays through
   `org.w3.clearkey` + `/api/clearkey` (JWK set) with no licence server; a real Widevine feed falls
   back to the site's own player page proxied through `/play`, with its licence POST forwarded.
4. **The inline family** hands out no manifest URL at all: the player page carries the whole ladder
   inside itself (`masterinf` = the variant `#EXT-X-STREAM-INF` lines, `payload.renditions` = complete
   media playlists whose segment URLs are absolute and signed) and its own shaka player is fed from
   those strings. There is no playlist request to sniff, and a cold hit gets an ~18 KB shell before
   the ~85 KB payload, so the hub re-reads the page and *is* the playlist: `/api/inline` remembers it,
   `/inline/<id>/index.m3u8` answers a real master, `/inline/<id>/video5_….m3u8` the rendition — the
   relay proxies the segments with the player page as `Referer`. Measured on a live Sunday game:
   1920x1080, audio at ~16 KB/s, in the app, no browser, no player page.
5. **The fallback** — when the chain yields nothing — is the site's own player page, served by the
   hub with the site's `Referer`/`Origin`, every nested frame rehosted, and `window.open()` turned
   into the picture in the same frame. That is the path that made games playable in-app at all.

## Best stream, in that order

Four decisions decide what you actually watch, and each one is measured rather than assumed:

| Decision | Rule | Measured on this build |
|---|---|---|
| which source lists the game | `GAME_SOURCES` order — methstreams, then crackstreams; the first that lists the matchup wins | both list 25 events for a Sunday slate |
| which player link | the game's `Main 1 → Main 4` in the site's own order; a chain that names no manifest is skipped | live college chain resolved on `Main 1`, hop 2 |
| which rendition | the relay sorts the master by **height, then bandwidth** and takes the top; a master with no `RESOLUTION` lines is picked by bandwidth | NFL Channel: 360p / 720p / 1080p@5.1M / 1080p@8.1M → **1920x1080 @ 8.46 Mbps, 29.97 fps**, 40 MB in the first 5 s |
| which channel | `feeds.txt` order (best on top); the page walks to the next one when a relay fails, and the pill names what is playing | 1080p Akamai → 720p Pluto (top rung 3321 k) → 480p aynaaott (768x432 @ 1.29 Mbps) |

Two failure rules keep "best" from meaning "broken": a harvested manifest that will not start falls
through to the site's player page instead of leaving a spinner, and a feed that redirects is resolved
against the **URL that answered** — a shortener in front of a playlist is normal, and joining the
variants against the URL you asked for sends them to the wrong host (that one cost the Pluto channel
a `414 URI Too Long` until it was fixed).

## NFL TV

Three always-on channels, verified 2026-11-07 (master playlist parsed, first segment 200):

| Feed | Quality | Note |
|---|---|---|
| Samsung TV Plus / Akamai | up to 1920x1080 | best picture; no key |
| Pluto (via `jmp2.uk`) | up to 720p | AES-128 segments — the relay keeps `#EXT-X-KEY` |
| backup relay | 480p | spare |

`feeds.txt` is refreshed from `raw.githubusercontent.com/hiwaywu/NFLDS/main/nfl/feeds.txt` on every
start (and on `reload list`), so a dead mirror is fixed by editing that file on `main`.

## What the window does

- **Games** — click a row marked `live`. The feed is resolved and played in the app window.
- **NFL TV** — always-on channels in our own player: 1080p where the feed has it, buffered locally.
- **latency** pill from hls.js (`14.2s behind live`), `low delay` (~8s), `frozen?`, `reload stream`,
  `live` jump.
- **fullscreen** — the picture is letterboxed to the stream's ratio. In fullscreen nothing stays on
  screen: the exit button and the control bar fade after ~2.6s and come back on a touch, and the
  phone's own progress row is switched off for the duration.
- Keys on the PC: `f` fullscreen, `m` mute, `r` reload the feed, Esc leaves fullscreen.
- **long-press the `nfl hub` title** — diagnostics: secure context, hls.js, Widevine/EME, relay
  state, video readyState. First thing to look at when a stream is black.

## Limits, stated plainly

- **Game streams are Widevine/PlayReady DRM.** Decryption is the browser's CDM, or the provider's
  own published clear key; the hub never extracts a key. On a phone whose WebView ships no Widevine
  key system, a real Widevine feed cannot be decoded in-app — the app says so and the browser chip
  (named after the browser that will open) is the way out.
- **The mirrors are the fragile dependency.** They post NFL games on the day (the football section
  shows college games on a Saturday), so before kick-off a Sunday slate lists every game with no
  stream yet — `has a feed (opens at kickoff)` means exactly that.
- **The inline family pages answer a cold hit with a shell.** The first request to a
  `.../iframe/...` player page can come back ~18 KB with no payload at all; the resolver asks again
  (up to three times, 0.5 s apart) and only then gives up. A feed that rotates its signing after that
  is served fresh on every playlist request, so the picture keeps moving while the page keeps
  cooperating.
- Nicknames are shared with college football (Falcons, Cardinals, Panthers...), so a match must hit
  **both** teams; the matcher requires both, and a one-sided hit is ignored.
- Browsing the PC hub from another device needs ports `8795` and `8960-9019` reachable; a
  guest/client-isolated wifi will not let you.

## Verification

The engine is proven offline with a fixture that reproduces every gate:

```text
python tools/fixture_stream.py         # a mirror with XFO, a referer gate, a POST licence,
                                       # a nested player, a frame-buster, window.open, an ad
                                       # popup, an ad overlay and a wrapper chain
python tools/verify_inline_family.py   # 13 checks: the inline ladder, the master/variant pair,
                                       # the dud globalurl and the ~18 KB shell
python tools/verify_inapp_game.py      # 29 checks against the real --app window (CDP)
python tools/verify_android_inapp.py   # 14 checks on a USB device or emulator, this APK
python tools/live_probe.py             # what a live game actually does (advance, audio, source)
```

Last measured (2026-11-08, live NFL slate): **29/29** on the PC window and **14/14** on a Pixel 7 /
Android 15 emulator with the APK from `releases/`; `tools/verify_inline_family.py` **13/13**. On the
same slate, the inline family played **1920x1080 with audio in both places** —
`tools/live_probe.py` reported `ADVANCE 6.00s over 6s`, `videoWidth 1920`, audio decoded at
~16 KB/s, source `blob:http://127.0.0.1:…` (our own player, not a site page); on the phone the relay
named `inline/…/video5_chargers.m3u8` at 10 Mbps. `python tools/build_portable.py --zip` rebuilds
the portable bundle.

## Layout

```text
nfl/            hub, relay, probes, CLI launchers, feeds.txt
  hub/          server, UI, hls.min.js, shaka.js, probes, NFLHUB.bat, FIREWALL.bat, NFLHub.exe
pc/             NFLPC.cs (the launcher), build-pc.ps1, app.ico
tools/          deploy.ps1, build_portable.py, fixture_stream.py, live_probe.py, the harnesses
android/        manifest, src/ (LocalHub, Relay, Games, Http, MainActivity), res/, build.ps1
releases/       NFLHub.apk (389 KB, standalone), NFLHub-portable.zip
docs/           PROCESS.md (the port map), screenshots, helper scripts
```
