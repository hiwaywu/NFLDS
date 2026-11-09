"""Live probe: drive the real hub page in a real Chrome --app window and report
what the in-app player actually does for one live game.

    python tools/live_probe.py --hub http://127.0.0.1:8796 --game 0

Nothing here is a test fixture: it clicks the same watchGame() path a user does,
then reads <video> state, the console, and the relay stats. Playwright is only the
CDP transport; the window is a normal app window, so hls.js/EME/decoders behave.

Exit code 0 when the picture actually advances.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CDP = 9344


def launch(hub, port):
    profile = os.path.join(os.environ.get("TEMP", "."), "nflprobe_profile")
    args = [CHROME, "--app=%s/" % hub.rstrip("/"), "--window-size=1280,820",
            "--remote-debugging-port=%d" % port,
            "--user-data-dir=" + profile,
            "--no-first-run", "--no-default-browser-check",
            "--disable-features=Translate,MediaRouter"]
    return subprocess.Popen(args)


STATE_JS = r"""(() => {
  const v = document.querySelector('#v');
  const t = document.querySelector('#toast');
  const pf = document.querySelector('#pf');
  return {
    src: (v.currentSrc || v.src || '').slice(0, 200),
    readyState: v.readyState,
    w: v.videoWidth, h: v.videoHeight,
    ct: +v.currentTime.toFixed(3),
    paused: v.paused, muted: v.muted, volume: v.volume,
    err: v.error ? (v.error.code + ':' + (v.error.message || '')) : null,
    buf: v.buffered.length ? [+v.buffered.start(0).toFixed(2), +v.buffered.end(v.buffered.length - 1).toFixed(2)] : null,
    bodyClass: document.body.className,
    toast: t ? t.textContent : '',
    title: (document.querySelector('#nowTitle') || {}).textContent || '',
    pillQ: (document.querySelector('#pillQ') || {}).textContent || '',
    pillSrc: (document.querySelector('#pillSrc') || {}).textContent || '',
    pfSrc: pf && pf.getAttribute ? (pf.getAttribute('src') || '') : '',
    audioBytes: v.webkitAudioDecodedByteCount,
    videoBytes: v.webkitVideoDecodedByteCount
  };
})()"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", default="http://127.0.0.1:8796")
    ap.add_argument("--game", default="0", help="index, slug substring, or event URL")
    ap.add_argument("--port", type=int, default=CDP)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--wait", type=int, default=90)
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    proc = None
    try:
        code = _run(args, sync_playwright)
        return code
    finally:
        if proc is not None and not args.keep:
            proc.terminate()


def _ensure_chrome(args):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/json/version" % args.port, timeout=3)
        return None
    except Exception:
        pass
    proc = launch(args.hub, args.port)
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/json/version" % args.port, timeout=2)
            return proc
        except Exception:
            time.sleep(0.5)
    return proc


def _run(args, sync_playwright):
    logs = []
    _ensure_chrome(args)
    with sync_playwright() as pw:
        br = pw.chromium.connect_over_cdp("http://127.0.0.1:%d" % args.port)
        pages = [p for c in br.contexts for p in c.pages]
        if not pages:
            print("no page in the app window")
            return 2
        page = pages[0]
        page.on("console", lambda m: logs.append("console[%s] %s" % (m.type, m.text[:400])))
        page.on("pageerror", lambda e: logs.append("pageerror %s" % str(e)[:400]))

        games = json.loads(page.evaluate(
            "JSON.stringify(games.map(g => ({name:g.name,url:g.url,state:g.state,stream:g.stream,time:g.time})))"))
        print("games on the page:", len(games))
        arg = args.game
        if arg.isdigit():
            cand = [g for g in games if g.get("stream") and g.get("state") == "in"]
            if not cand:
                cand = [g for g in games if g.get("stream")] or games
            pick = cand[int(arg) % len(cand)] if cand else None
        else:
            pick = next((g for g in games
                         if arg in (g.get("url") or "") or arg in (g.get("name") or "")), None)
        if not pick:
            print("no game to probe")
            return 2
        print("probing:", json.dumps(pick))

        page.evaluate("g => { watchGame(g); return true; }", pick)

        def snap():
            return json.loads(page.evaluate("JSON.stringify(" + STATE_JS + ")"))

        t0 = time.time()
        while time.time() - t0 < args.wait:
            s = snap()
            if s["ct"] > 0.5 and s["w"]:
                break
            time.sleep(1.0)
        a = snap()
        time.sleep(6.0)
        b = snap()

        print("\n--- now ---\n" + json.dumps(b, indent=1))
        advanced = b["ct"] - a["ct"]
        print("\nADVANCE: %.2fs over 6s (w=%s h=%s readyState=%s audioBytes=%s)" %
              (advanced, b["w"], b["h"], b["readyState"], b["audioBytes"]))
        print("TOAST:", b["toast"])
        print("SRC:", b["src"])
        if logs:
            print("\n--- console (%d) ---" % len(logs))
            for line in logs[-40:]:
                print(line)
        ok = bool(b["w"]) and advanced > 3.0
        print("\nRESULT:", "PLAYING IN APP" if ok else "NOT PLAYING IN APP")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
