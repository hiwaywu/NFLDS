# Fire TV / Android TV

The TV build is the same app with a TV interface and a TV manifest. It installs **next to**
the phone app (package `com.nflds.tv`) and shows up on the Fire TV home screen.

```powershell
cd android
powershell -ExecutionPolicy Bypass -File build.ps1 -Tv      # -> android\NFLHub-TV.apk
adb connect <fire-tv-ip>:5555
adb install -r ..\releases\NFLHub-TV.apk
adb shell am start -n com.nflds.tv/com.nflds.app.MainActivity
```

Enable it on the stick first: **Settings → My Fire TV → About → click "Fire TV Stick" 7 times →
Developer Options → ADB debugging on**.

## What the TV build is

| Piece | Phone build | TV build |
|---|---|---|
| manifest | `android/AndroidManifest.xml` | `android/tv/AndroidManifest.xml` |
| launcher | `LAUNCHER` | `LAUNCHER` + `LEANBACK_LAUNCHER`, 320x180 `banner`, landscape |
| package | `com.nflds.app` | `com.nflds.tv` (renamed at link time) |
| interface | the phone page | `body.tv`: card browser + info bar (`?tv=1` forces it in a browser) |

`Hub.tv()` answers `true` on a leanback device (and on the `com.nflds.tv` package), and the
page builds the TV screen for it: a date bar, a row of game cards with team logos, a row of
NFL TV channels, and playback that takes the whole screen.

## The remote

| Key | Browse screen | Watching |
|---|---|---|
| ▲ ▼ | row up/down | hide / show the info bar |
| ◀ ▶ | card left/right (top row: change the day) | info-bar button |
| OK | **play the focused card, full screen** | show info bar (with sound when muted) |
| Play/Pause | — | pause / play |
| Rewind / Forward | — | ±10 s inside the buffer |
| Menu | diagnostics | close diagnostics |
| Back | leave the app | back to the browse screen |

Arrow keys are **not** left to the WebView: Fire OS's WebView eats them for its own focus
traversal (first run: OK did nothing at all). `MainActivity.dispatchKeyEvent` names every key
and calls the page's `tvKey()`, and the page's raw `keydown` handler steps aside once the shell
has said it is driving (`window.__nflTvShell`). On a game that opened the site's own player
page, the same keys become clicks on whatever the page centred (`window.tvShot`).

## What it took to keep a live game moving

Measured on a Fire TV Stick 4K (AFTR/raven, Fire OS 7.2.2.9 = Android 9, WebView Chromium 75,
armeabi-v7a, 1080p @ 320 dpi) and cross-checked on the `Television_4K` emulator (Android 16 TV,
4K). The feed path itself needed nothing: the resolver found the game, the inline family handed
over its ladder, and the decoder ran 1280x720 AVC at 30 fps / ~6 Mbps with audio.

Three things about a **television** did need fixing, and all three look like "the stream died":

1. **The feed's window arrives in bursts.** A rendition playlist carries ~78 s of media and
   jumps ~30 s forward at a time (measured: `#EXT-X-MEDIA-SEQUENCE` 612 → 617 in 8 s, then
   frozen at 617 for 20 s). Chasing that edge — hls.js live-sync inside 16 s plus the page's
   hard "snap to live" ceiling — is what made the picture hop from scene to scene. On a TV the
   playhead now sits 4 segments (~24 s) back, catches up at 1.05x, and the ceiling only fires
   past 60 s, so a burst lands ahead of the playhead instead of triggering a seek.
2. **A 4 s buffer blip was read as a dead mirror.** The page's stall rule (`frozen > 4 s and
   readyState < 3 → mirrorFail`) is right on a phone; on a Fire TV it abandoned a feed that was
   about to play, which surfaced as "game feed failed (picture stalled)". On a TV the rule is
   14 s and needs `readyState < 2`, and 5 buffering events in a minute (not 3) rotate a mirror.
3. **Nothing restarted a frozen picture.** A TV is left alone for an hour. A watchdog samples
   the playhead every 6 s: no movement → one nudge to the live edge (then 45 s of quiet, so it
   cannot shove twice in a row); a third consecutive stall → the stream is rebuilt.

## Measured

| Where | What | Result |
|---|---|---|
| Fire TV Stick 4K | live NFL game resolved in-app | `relay started … /inline/…/index.m3u8`, HW `avc` 1280x720, first frame rendered, audio focus taken |
| Fire TV Stick 4K | 2.5 min soak before the TV fixes | playhead advancing, but latency swinging 14 → 35 s with decoder flushes every 5 s |
| Television_4K emulator (Android 16 TV) | 193 s soak after the fixes | `currentTime` 35 → 203 s, `readyState` 4 throughout, **one** watchdog nudge, **zero** mirror failures |
| Television_4K emulator | picture + info bar | live Jets–Chargers (FOX NFL), full screen, "27.7 s behind live", Back → browse, Back → exit |

`docs/firetv_browse.png`, `firetv_playing.png`, `firetv_osd.png` are from the emulator; the Fire
TV Stick was verified through `dumpsys`/`MediaCodecLogger`/page stats, because `screencap` on
Amlogic cannot see the hardware video plane (its screenshots come back black even while the
picture is on screen).

## Checking it without a television

```powershell
# any browser: the TV interface, driven by the arrow keys and Enter
http://127.0.0.1:8795/?tv=1

# or the real thing on an emulator
emulator -avd Television_4K -no-window -gpu swiftshader_indirect
adb install -r releases\NFLHub-TV.apk
adb shell am start -n com.nflds.tv/com.nflds.app.MainActivity
adb shell input keyevent 23            # OK on the first card (live games sort first)
adb shell screencap -p /sdcard/tv.png
```

## Where the 1080p game feeds actually are

They are in the same place the NBA hub found them: the mirror's own signed ladder, not the
broadcast relay we were playing. On 2026-11-22, Jaguars–Giants on `methstreams.st` handed over

```text
https://tims-001.servehttp.com/main/secure/<sha256>/<epoch>/nfl-401873069.m3u8
#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=8780000,BANDWIDTH=10980000,RESOLUTION=1920x1080,
                   FRAME-RATE=59.940,CODECS="avc1.64002a,mp4a.40.2"
 -> nfl-401873069_1080p.m3u8      #EXT-X-MEDIA-SEQUENCE:20732, 6.006 s segments
```

measured with a decoder on the first segment: **1920x1080 h264 High@4.2, 59.94 fps, AAC 48 kHz
stereo, 6 MB per 6 s segment, `#EXT-X-KEY` absent (clear, no Widevine), no discontinuities**.
It answers a plain GET with **no Referer**. That is the broadcast feed (1080p59.94), the same
class of thing NBA Hub got from `tims-001.sytes.net/.../nba-<id>.m3u8`.

Availability is per game, not per site — the same night, Raiders–Broncos on the same mirror
only published

```text
.../nfl-401873075.m3u8 -> nfl-401873075_720p.m3u8   RESOLUTION=1280x720, 4.79 Mbps
```

so a resolver that takes the first link that yields *something* ends up on 720p while a 1080p
ladder sits on another `Main N` link of the same event page.

`tools/harvest_mirror.py` reproduces this: it drives Chrome down event → `/main/N` → embed with
ad hosts blocked, records every `.m3u8` the player asks for, fetches each one plainly, and decodes
the top rung's first segment.

```powershell
python tools\harvest_mirror.py https://methstreams.st/event/m-jacksonville-jaguars-vs-new-york-giants-1122 --mains 3
```

**What the app does not do yet, and what it would take:** prefer the candidate whose master
declares (and delivers) 1920x1080 instead of the first one that plays, and re-harvest when a
tokenised `/main/secure/...` URL starts answering 404 — those epochs rotate every few minutes, so
a cached manifest is good for one session only (the relay's current behaviour is to retry the same
404 until it gives up, which is exactly how a 1080p game "stopped working").

## 1080p, measured rather than labelled

The ladder's `RESOLUTION=` lines are not evidence. On a live game this mirror family
declared five rungs — 1920x1080 @ 10 Mbps at the top — and every one of the three "HD" rungs
decoded at **1280x720**:

```text
$ python tools/probe_renditions.py http://127.0.0.1:8899/inline/<id>/index.m3u8 <player page>
master: 5 rendition(s)
  video4_chargers.m3u8   declared=1920x1080 bw=10000000  real=1280x720 h264  first-segment=31383 KB
  video3_chargers.m3u8   declared=1280x720  bw=4000000   real=1280x720 h264  first-segment=20373 KB
  video2_chargers.m3u8   declared=1280x720  bw=2500000   real=1280x720 h264  first-segment=12877 KB
  video0_chargers.m3u8   declared=640x360   bw=1250000   real=640x360  h264  first-segment=7282 KB
  video1_chargers.m3u8   declared=416x234   bw=500000    real=416x234  h264  first-segment=3731 KB
best real rendition: 1280x720  (video4_chargers.m3u8)
```

(It needs `pyav`: `python -m pip install av`. Point the URL at the app's own hub through
`adb forward tcp:8899 tcp:8795` while a game is playing.)

So: a game on this family is **720p, at a 10 Mbps 720p rung** — the app already takes the best
rung that exists, and there is no setting that turns it into 1080p. What *is* real 1080p is
verified per stream instead of per label:

| Stream | Decoder says |
|---|---|
| NFL TV channel (Samsung TV Plus / Akamai) | **1920x1080** |
| Live game, inline family (top rung, 10 Mbps) | 1280x720 |
| Live game, `main/2` link (`tims-001.servehttp.com`, declared 1920x1080 @ 10.5 Mbps) | playlist answered 404 on re-fetch, so unusable as-is |

Because of that, the resolution pill in the info bar now prints `v.videoWidth x v.videoHeight` —
what the decoder produced — instead of the stream's own claim, so "is it really 1080p" is
answerable on the spot.

## Signing

Both APKs are signed with `android/debug.keystore` (store/key password `android`), which
`build.ps1` creates on first run and `.gitignore` keeps out of the repository. A build therefore
signs with whatever key that file holds: the copy in `releases/` and a fresh clone will not match
each other, and an APK signed with a different key cannot be installed over an existing one
(`INSTALL_FAILED_UPDATE_INCOMPATIBLE`) — uninstall first, or keep the same keystore. The TV
package (`com.nflds.tv`) is a different app, so it never collides with the phone build.

