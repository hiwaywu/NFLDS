package com.nflds.app;

import android.content.Context;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Calendar;
import java.util.Date;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * hub.py, inside the APK. Listens on 127.0.0.1 only -- nothing on the wifi can
 * reach it, and no PC is involved.
 *
 * It serves the very same index.html the PC uses (bundled as an asset) and the
 * /api/* calls that page makes, so the UI, mirror failover, DVR, latency pill
 * and diagnostics all behave exactly as on the desktop. The relay lives under
 * /relay/ on the same port; the page derives it from the URL /api/play hands
 * back, so it needs no changes for that either.
 *
 * Two things only a PC could do are replaced:
 *   - game probing (headless Chromium): the event page's first "Main N" link is
 *     opened in the phone's browser, which is where a Widevine feed has to play
 *     anyway
 *   - feeds.txt on disk: bundled, then refreshed from the repo on GitHub, so a
 *     dead mirror is fixed by editing one file -- no APK rebuild
 */
final class LocalHub {

    private static final String TAG = "nflhub.hub";
    static final String VERSION = "1.0";
    private static final String FEEDS_REMOTE =
            "https://raw.githubusercontent.com/hiwaywu/NFLDS/main/nfl/feeds.txt";

    /** What the hub needs from Android -- small enough to fake on a desktop JVM for tests. */
    interface Env {
        InputStream asset(String name) throws IOException;
        File filesDir();
        File cacheDir();
    }

    private final Env env;
    private final Games games = new Games();
    private final ExecutorService workers = Executors.newCachedThreadPool();
    private ServerSocket server;
    private volatile Relay relay;
    private volatile boolean relayLow;
    private final Object relayLock = new Object();
    /** Sound lives in its own playlist on some feeds (Game Pass does this);
     *  the video relay alone plays silent, so it gets a companion relay. */
    private volatile Relay relayAudio;
    private volatile String feedsText;
    private volatile String feedsFrom = "bundled";
    private final Map<String, Object[]> resolved = new HashMap<String, Object[]>();
    private final Map<String, Integer> freezes = new HashMap<String, Integer>();

    /** Clear-key pair the last harvest pulled out of a player page (kid hex -> key
     *  hex). Served to hls.js through /api/clearkey so EME can decrypt a Widevine-
     *  packaged feed that leaks a known key without cropping a video path. */
    private volatile Map<String, String> lastClearKeys = null;

    LocalHub(Context ctx) {
        final Context app = ctx.getApplicationContext();
        this.env = new Env() {
            public InputStream asset(String name) throws IOException { return app.getAssets().open(name); }
            public File filesDir() { return app.getFilesDir(); }
            public File cacheDir() { return app.getCacheDir(); }
        };
    }

    LocalHub(Env env) {
        this.env = env;
    }

    // ------------------------------------------------------------ lifecycle

    /** Bind and return the port (8795 if free, else any). */
    int start() throws IOException {
        InetAddress lo = InetAddress.getByName("127.0.0.1");
        try {
            server = new ServerSocket(8795, 64, lo);
        } catch (IOException busy) {
            server = new ServerSocket(0, 64, lo);
        }
        sweepDvr();
        loadFeeds();
        Thread t = new Thread(new Runnable() {
            public void run() { acceptLoop(); }
        }, "hub-accept");
        t.setDaemon(true);
        t.start();
        // warm the game list while the WebView is still starting
        workers.execute(new Runnable() {
            public void run() {
                try { games.all(); } catch (Exception ignored) { }
                refreshFeeds();
            }
        });
        return server.getLocalPort();
    }

    void stop() {
        stopRelay();
        try { if (server != null) server.close(); } catch (IOException ignored) { }
        workers.shutdownNow();
    }

    /** App went to the background for good: stop pulling video. */
    void stopRelay() {
        synchronized (relayLock) {
            if (relay != null) relay.stop();
            relay = null;
        }
    }

    private File dvrRoot() {
        return new File(env.cacheDir(), "dvr");
    }

    private void sweepDvr() {
        File r = dvrRoot();
        if (r.isDirectory()) Relay.deleteTree(r);
    }

    // --------------------------------------------------------------- feeds

    private void loadFeeds() {
        File cached = new File(env.filesDir(), "feeds.txt");
        try {
            if (cached.isFile() && cached.length() > 0) {
                FileInputStream in = new FileInputStream(cached);
                try { feedsText = new String(Http.readAll(in), "UTF-8"); } finally { in.close(); }
                feedsFrom = "github (cached)";
                return;
            }
        } catch (Exception ignored) { }
        try {
            InputStream in = env.asset("feeds.txt");
            try { feedsText = new String(Http.readAll(in), "UTF-8"); } finally { in.close(); }
            feedsFrom = "bundled";
        } catch (Exception e) {
            feedsText = "";
        }
    }

    private void refreshFeeds() {
        try {
            String t = Http.text(FEEDS_REMOTE + "?_=" + System.currentTimeMillis(), 10000);
            if (parseChannels(t).length() == 0) return;        // never replace a list with nothing
            feedsText = t;
            feedsFrom = "github";
            FileOutputStream o = new FileOutputStream(new File(env.filesDir(), "feeds.txt"));
            try { o.write(t.getBytes("UTF-8")); } finally { o.close(); }
        } catch (Exception e) {
            Log.i(TAG, "feeds refresh skipped: " + e.getMessage());
        }
    }

    /** NAME | URL [| referer] */
    private static JSONArray parseChannels(String text) {
        JSONArray out = new JSONArray();
        if (text == null) return out;
        for (String line : text.split("\r?\n")) {
            String s = line.trim();
            if (s.length() == 0 || s.startsWith("#") || !s.contains("|")) continue;
            String[] p = s.split("\\|");
            if (p.length < 2) continue;
            String name = p[0].trim(), url = p[1].trim();
            if (!url.startsWith("http")) continue;
            try {
                JSONObject c = new JSONObject().put("name", name).put("url", url);
                if (p.length > 2 && p[2].trim().startsWith("http")) c.put("referer", p[2].trim());
                out.put(c);
            } catch (Exception ignored) { }
        }
        return out;
    }

    private String refererFor(String url) {
        JSONArray cs = parseChannels(feedsText);
        for (int i = 0; i < cs.length(); i++) {
            JSONObject c = cs.optJSONObject(i);
            if (c != null && url.equals(c.optString("url"))) return c.optString("referer", null);
        }
        return null;
    }

    // ------------------------------------------------------------- server

    private void acceptLoop() {
        while (server != null && !server.isClosed()) {
            final Socket s;
            try {
                s = server.accept();
            } catch (IOException e) {
                break;
            }
            workers.execute(new Runnable() {
                public void run() { serve(s); }
            });
        }
    }

    private static final class Req {
        String method, path, query;
        Map<String, String> qs = new HashMap<String, String>();
        boolean keepAlive = true;
        byte[] body;

        String q(String k) {
            String v = qs.get(k);
            return v == null ? "" : v;
        }
    }

    private void serve(Socket s) {
        try {
            s.setSoTimeout(30000);
            s.setTcpNoDelay(true);
            InputStream in = new BufferedInputStream(s.getInputStream());
            OutputStream out = s.getOutputStream();
            // keep-alive: hls.js asks for a segment every few seconds, and the
            // WebView reuses sockets -- one connection per request is wasted work
            while (true) {
                Req r = readRequest(in);
                if (r == null) break;
                try {
                    route(r, out);
                } catch (IOException e) {
                    throw e;
                } catch (Exception e) {
                    Log.w(TAG, "handler " + r.path + ": " + e);
                    send(out, r, 500, "application/json", bytes("{\"ok\":false,\"error\":\"hub handler error\"}"), null);
                }
                out.flush();
                if (!r.keepAlive) break;
            }
        } catch (Exception ignored) {
        } finally {
            try { s.close(); } catch (IOException ignored) { }
        }
    }

    private static Req readRequest(InputStream in) throws IOException {
        String line = readLine(in);
        if (line == null || line.length() == 0) return null;
        String[] p = line.split(" ");
        if (p.length < 2) return null;
        Req r = new Req();
        r.method = p[0];
        String target = p[1];
        r.keepAlive = p.length < 3 || !"HTTP/1.0".equals(p[2]);
        int clen = 0;
        String h;
        while ((h = readLine(in)) != null && h.length() > 0) {
            int c = h.indexOf(':');
            if (c > 0 && h.substring(0, c).trim().equalsIgnoreCase("connection")) {
                r.keepAlive = !h.substring(c + 1).trim().equalsIgnoreCase("close");
            }
            if (c > 0 && h.substring(0, c).trim().equalsIgnoreCase("content-length")) {
                try { clen = Integer.parseInt(h.substring(c + 1).trim()); } catch (NumberFormatException ignored) { }
            }
        }
        if (clen > 0) {
            byte[] body = new byte[clen];
            int got = 0;
            while (got < clen) {
                int n = in.read(body, got, clen - got);
                if (n < 0) break;
                got += n;
            }
            r.body = body;
        }
        int q = target.indexOf('?');
        r.path = q >= 0 ? target.substring(0, q) : target;
        r.query = q >= 0 ? target.substring(q + 1) : "";
        for (String kv : r.query.split("&")) {
            if (kv.length() == 0) continue;
            int e = kv.indexOf('=');
            try {
                String k = URLDecoder.decode(e >= 0 ? kv.substring(0, e) : kv, "UTF-8");
                String v = e >= 0 ? URLDecoder.decode(kv.substring(e + 1), "UTF-8") : "";
                r.qs.put(k, v);
            } catch (Exception ignored) { }
        }
        return r;
    }

    private static String readLine(InputStream in) throws IOException {
        StringBuilder b = new StringBuilder();
        int c;
        while ((c = in.read()) >= 0) {
            if (c == '\n') break;
            if (c != '\r') b.append((char) c);
            if (b.length() > 16384) throw new IOException("header too long");
        }
        if (c < 0 && b.length() == 0) return null;
        return b.toString();
    }

    private static byte[] bytes(String s) {
        try { return s.getBytes("UTF-8"); } catch (Exception e) { return s.getBytes(); }
    }

    private static void send(OutputStream out, Req r, int code, String type, byte[] body,
                             Map<String, String> extra) throws IOException {
        StringBuilder h = new StringBuilder(256);
        h.append("HTTP/1.1 ").append(code).append(reason(code)).append("\r\n");
        h.append("Content-Type: ").append(type).append("\r\n");
        h.append("Content-Length: ").append(body.length).append("\r\n");
        h.append("Access-Control-Allow-Origin: *\r\n");
        h.append("Access-Control-Allow-Headers: *\r\n");
        if (extra == null || !extra.containsKey("Cache-Control")) h.append("Cache-Control: no-store\r\n");
        if (extra != null) for (Map.Entry<String, String> e : extra.entrySet())
            h.append(e.getKey()).append(": ").append(e.getValue()).append("\r\n");
        h.append(r.keepAlive ? "Connection: keep-alive\r\n" : "Connection: close\r\n");
        h.append("\r\n");
        out.write(bytes(h.toString()));
        if (!"HEAD".equals(r.method)) out.write(body);
    }

    private static String reason(int code) {
        switch (code) {
            case 200: return " OK";
            case 204: return " No Content";
            case 400: return " Bad Request";
            case 404: return " Not Found";
            case 500: return " Internal Server Error";
            case 502: return " Bad Gateway";
            case 503: return " Service Unavailable";
            default: return " Status";
        }
    }

    private static void json(OutputStream out, Req r, JSONObject j) throws IOException {
        send(out, r, 200, "application/json", bytes(j.toString()), null);
    }

    private void asset(OutputStream out, Req r, String name, String type) throws IOException {
        InputStream in;
        try {
            in = env.asset(name);
        } catch (IOException e) {
            send(out, r, 404, "text/plain", bytes("not found"), null);
            return;
        }
        byte[] b;
        try { b = Http.readAll(in); } finally { in.close(); }
        Map<String, String> x = new HashMap<String, String>();
        x.put("Cache-Control", "no-cache");
        send(out, r, 200, type, b, x);
    }

    // -------------------------------------------------------------- routes

    private void route(Req r, OutputStream out) throws Exception {
        if ("OPTIONS".equals(r.method)) {
            send(out, r, 204, "text/plain", new byte[0], null);
            return;
        }
        String p = r.path;
        if (p.equals("/") || p.equals("/index.html")) { asset(out, r, "index.html", "text/html; charset=utf-8"); return; }
        if (p.equals("/hls.min.js")) { asset(out, r, "hls.min.js", "application/javascript"); return; }
        if (p.equals("/shaka.js")) { asset(out, r, "shaka.js", "application/javascript"); return; }
        if (p.startsWith("/relay/")) { relayRoute(r, out, p.substring(7)); return; }

        if (p.equals("/api/ping")) {
            json(out, r, new JSONObject()
                    .put("ok", true).put("app", "nflhub").put("version", VERSION)
                    .put("mode", "standalone (on this phone)")
                    .put("hub", "http://127.0.0.1:" + server.getLocalPort())
                    .put("feeds", feedsFrom)
                    .put("time", new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date())));
            return;
        }
        if (p.equals("/api/games")) {
            String date = r.q("date");
            if (date.length() == 0) date = Games.todayIso();
            JSONObject j = new JSONObject().put("date", date);
            try {
                j.put("games", new JSONArray(games.forDate(date)));
            } catch (Exception e) {
                j.put("games", new JSONArray()).put("error", String.valueOf(e.getMessage()));
            }
            JSONObject src = new JSONObject();
            synchronized (games.sourceHealth) {
                for (Map.Entry<String, JSONObject> e : games.sourceHealth.entrySet()) src.put(e.getKey(), e.getValue());
            }
            j.put("sources", src);
            j.put("checked_at", new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date()));
            json(out, r, j);
            return;
        }
        if (p.equals("/api/dates")) {
            TreeSet<String> ds = new TreeSet<String>();
            JSONObject streams = new JSONObject();
            for (JSONObject g : games.all()) {
                String d = g.optString("date");
                if (d.length() == 0) continue;
                ds.add(d);
                if (g.optBoolean("stream")) streams.put(d, streams.optInt(d, 0) + 1);
            }
            Calendar t = Calendar.getInstance();
            int sy = t.get(Calendar.MONTH) >= Calendar.SEPTEMBER ? t.get(Calendar.YEAR) : t.get(Calendar.YEAR) - 1;
            json(out, r, new JSONObject()
                    .put("dates", new JSONArray(new ArrayList<String>(ds)))
                    .put("streams", streams)
                    .put("today", Games.todayIso())
                    .put("season", new JSONObject().put("start", sy + "-10-01").put("end", (sy + 1) + "-04-30")));
            return;
        }
        if (p.equals("/api/next")) {
            String date = r.q("date");
            if (date.length() == 0) date = Games.todayIso();
            int step = "prev".equals(r.q("dir")) ? -1 : 1;
            JSONArray tried = new JSONArray();
            String d = date;
            for (int i = 0; i < 20; i++) {
                d = Games.shift(d, step);
                tried.put(d);
                int n = games.forDate(d).size();
                if (n > 0) {
                    json(out, r, new JSONObject().put("date", d).put("games", n).put("tried", tried));
                    return;
                }
            }
            json(out, r, new JSONObject().put("date", JSONObject.NULL).put("tried", tried));
            return;
        }
        if (p.equals("/api/channels")) {
            json(out, r, new JSONObject().put("channels", parseChannels(feedsText)).put("from", feedsFrom));
            return;
        }
        if (p.equals("/api/play")) {
            String u = r.q("u");
            if (u.length() == 0) {
                send(out, r, 400, "application/json", bytes("{\"ok\":false,\"error\":\"missing u\"}"), null);
                return;
            }
            boolean low = "1".equals(r.q("low"));
            // r=<player page URL>: the Referer the harvested feed expects. Harvested
            // game feeds do not come from feeds.txt, so their referer is per-request.
            String ref = r.q("r");
            if (ref.length() == 0) ref = refererFor(u);
            Relay rl;
            try {
                rl = startRelay(u, low, ref);
            } catch (Exception e) {
                json(out, r, new JSONObject().put("ok", false).put("error", "relay failed: " + e.getMessage()));
                return;
            }
            String base = "http://127.0.0.1:" + server.getLocalPort() + "/relay";
            boolean hasAudio = relayAudio != null && !relayAudio.isStopped();
            json(out, r, new JSONObject()
                    .put("ok", true)
                    .put("url", base + "/live.m3u8")
                    .put("local", base + "/live.m3u8")
                    // master names the sound as its own rendition; without it the
                    // picture is silent when the feed keeps audio separate
                    .put("master", base + "/master.m3u8")
                    .put("audio", hasAudio)
                    .put("port", server.getLocalPort())
                    .put("stats", rl.stats()));
            return;
        }
        if (p.equals("/api/relaystats")) {
            Relay rl = relay;
            json(out, r, rl == null ? new JSONObject().put("ok", false) : rl.stats());
            return;
        }
        if (p.equals("/api/resolve") || p.equals("/api/candidates")) {
            String page = r.q("u");
            List<String> links = linksFor(page);
            if (p.equals("/api/candidates")) {
                JSONArray cs = new JSONArray();
                for (String l : links) {
                    String f = playerFrame(l);
                    cs.put(new JSONObject().put("url", l).put("ok", f != null).put("h", 0).put("frame", f == null ? "" : f));
                }
                json(out, r, new JSONObject().put("ok", true).put("candidates", cs));
                return;
            }
            // no headless browser on a phone: take the first "Main N" link whose
            // page names a player frame in plain HTML. The app opens that page
            // itself -- the player host refuses to play framed by anyone else.
            String main = links.isEmpty() ? page : links.get(0);
            String frame = null;
            for (String l : links) {
                frame = playerFrame(l);
                if (frame != null) { main = l; break; }
            }
            json(out, r, new JSONObject()
                    .put("ok", frame != null)
                    .put("frame", frame != null ? frame : main)
                    .put("open", frame != null ? frame : main)
                    .put("browser", main)
                    .put("source", main).put("event", page)
                    .put("links", new JSONArray(links))
                    .put("probe", new JSONObject().put("ok", frame != null).put("note", "frame read from the page, not probed")));
            return;
        }
        if (p.equals("/api/harvest")) {
            JSONObject j;
            try {
                j = harvestStream(r.q("u"));
            } catch (Exception e) {
                j = new JSONObject().put("ok", false)
                        .put("error", String.valueOf(e.getMessage()));
            }
            json(out, r, j);
            return;
        }
        if (p.equals("/api/clearkey")) {
            Map<String, String> ks = lastClearKeys;
            JSONArray arr = new JSONArray();
            if (ks != null) {
                for (Map.Entry<String, String> e : ks.entrySet()) {
                    try {
                        arr.put(new JSONObject()
                                .put("kty", "oct")
                                .put("kid", b64url(hexToBytes(e.getKey())))
                                .put("k", b64url(hexToBytes(e.getValue()))));
                    } catch (Exception ignored) { }
                }
            }
            json(out, r, new JSONObject().put("keys", arr));
            return;
        }
        if (p.equals("/api/keys") && "POST".equals(r.method)) {
            try {
                byte[] raw = r.body == null ? new byte[0] : r.body;
                org.json.JSONTokener tk = new org.json.JSONTokener(new String(raw, "UTF-8"));
                org.json.JSONArray arr = (org.json.JSONArray) tk.nextValue();
                Map<String, String> ks = new HashMap<String, String>();
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject k = arr.optJSONObject(i);
                    if (k == null) continue;
                    String kid = k.optString("kid"), key = k.optString("key");
                    if (kid.matches("[0-9a-fA-F]{16,64}") && key.matches("[0-9a-fA-F]{16,64}"))
                        ks.put(kid.toLowerCase(Locale.US), key.toLowerCase(Locale.US));
                }
                if (!ks.isEmpty()) {
                    lastClearKeys = ks;
                    json(out, r, new JSONObject().put("ok", true).put("keys", ks.size()));
                } else {
                    json(out, r, new JSONObject().put("ok", false));
                }
            } catch (Exception e) {
                json(out, r, new JSONObject().put("ok", false)
                        .put("error", String.valueOf(e.getMessage())));
            }
            return;
        }
        if (p.equals("/api/health")) {
            json(out, r, new JSONObject().put("ok", false).put("reason", "not measured on the phone"));
            return;
        }
        if (p.equals("/api/freeze")) {
            String page = r.q("u");
            int n;
            synchronized (freezes) {
                n = (freezes.containsKey(page) ? freezes.get(page) : 0) + 1;
                freezes.put(page, n);
            }
            json(out, r, new JSONObject().put("ok", true).put("freezes", n));
            return;
        }
        if (p.equals("/api/refresh")) {
            games.invalidate();
            refreshFeeds();
            json(out, r, new JSONObject().put("ok", true).put("feeds", feedsFrom));
            return;
        }
        send(out, r, 404, "text/plain", bytes("not found"), null);
    }

    private final Map<String, Object[]> frames = new HashMap<String, Object[]>();
    private static final Pattern STREAM_IFRAME = Pattern.compile(
            "<iframe[^>]*\\bid=[\"']streamIframe[\"'][^>]*>",
            Pattern.CASE_INSENSITIVE);
    private static final Pattern SRC = Pattern.compile("\\bsrc=[\"']([^\"']+)[\"']", Pattern.CASE_INSENSITIVE);

    /**
     * The player frame a "Main N" page embeds (iframe#streamIframe), read from
     * its plain HTML -- the same request a browser makes for that page, nothing
     * spoofed. Null when the page does not name one.
     */
    private String playerFrame(String mainPage) {
        synchronized (frames) {
            Object[] hit = frames.get(mainPage);
            if (hit != null && System.currentTimeMillis() - (Long) hit[0] < 600000) return (String) hit[1];
        }
        String found = null;
        try {
            Http.Resp resp = Http.get(mainPage, 15000, null);
            Matcher m = STREAM_IFRAME.matcher(resp.text());
            if (m.find()) {
                Matcher sm = SRC.matcher(m.group(0));
                if (sm.find()) {
                    String u = Http.join(resp.finalUrl, sm.group(1).replace("&amp;", "&"));
                    if (u.startsWith("https://")) found = u;     // EME needs a secure frame
                }
            }
        } catch (Exception e) {
            Log.i(TAG, "no player frame on " + mainPage + ": " + e.getMessage());
        }
        synchronized (frames) { frames.put(mainPage, new Object[]{System.currentTimeMillis(), found}); }
        return found;
    }

    private List<String> linksFor(String page) {
        if (page.length() == 0) return new ArrayList<String>();
        synchronized (resolved) {
            Object[] hit = resolved.get(page);
            if (hit != null && System.currentTimeMillis() - (Long) hit[0] < 600000) {
                @SuppressWarnings("unchecked") List<String> l = (List<String>) hit[1];
                return l;
            }
        }
        List<String> l = Games.feedLinks(page);
        if (!l.isEmpty()) synchronized (resolved) { resolved.put(page, new Object[]{System.currentTimeMillis(), l}); }
        return l;
    }

    // -------------------------------------------------------------- harvest
    //
    // The event page's "Main N" link is a wrapper chain, not a player: the page
    // embeds an iframe, the iframe is another wrapper that builds a second iframe
    // in JS, and the last page of the chain is the one that names the actual
    // manifest (a globalurl constant, a DIRECT_STREAM_URL constant, a rotator
    // entry, or a hard-coded .m3u8/.mpd URL). Some providers also leak the CDM's
    // clear-key pair next to the manifest (masterinf = {"kid": "key"}), which is
    // how HLS encrypted with a known key can be decrypted without a license
    // server at all.
    //
    // Each hop is one plain HTTPS GET a browser would make too. When the chain
    // lands on a manifest, /api/play hands it to the relay and the picture
    // appears in the app's own player -- no WebView navigation, so none of the
    // ad layers a player page drags along ever run.

    private static final Pattern P_GLOBALURL = Pattern.compile(
            "globalurl\\s*=\\s*['\"]([^'\"\\r\\n]+)['\"]", Pattern.CASE_INSENSITIVE);
    private static final Pattern P_DIRECT = Pattern.compile(
            "DIRECT_STREAM_URL\\s*=\\s*['\"]([^'\"\\r\\n]+)['\"]", Pattern.CASE_INSENSITIVE);
    private static final Pattern P_VALUE = Pattern.compile(
            "\"value\"\\s*:\\s*\"([^\"]+)\"", Pattern.CASE_INSENSITIVE);
    private static final Pattern P_M3U8 = Pattern.compile(
            "(https?://[^\"'\\\\\\s<>]+?\\.(?:m3u8|mpd)[^\"'\\\\\\s<>]*)");
    private static final Pattern P_IFRAME = Pattern.compile(
            "<iframe[^>]*?\\bsrc\\s*=\\s*['\"]([^'\"]+)['\"]", Pattern.CASE_INSENSITIVE);
    private static final Pattern P_JS_SRC = Pattern.compile(
            "\\.src\\s*=\\s*['\"](https?://[^'\"\\r\\n]+)['\"]");
    private static final Pattern P_MASTERINF = Pattern.compile(
            "masterinf\\s*=\\s*\\{([^}]{10,600})\\}", Pattern.CASE_INSENSITIVE);
    private static final Pattern P_KEYKV = Pattern.compile(
            "\"?([0-9a-fA-F]{16,64})\"?\\s*:\\s*\"?([0-9a-fA-F]{16,64})\"?");

    private static String hostOf(String u) {
        try { return new java.net.URL(u).getHost(); } catch (Exception e) { return ""; }
    }

    private static boolean isAdish(String u) {
        String low = (u == null ? "" : u.toLowerCase());
        return low.contains("doubleclick") || low.contains("googlesyndication")
                || low.contains("adservice") || low.contains("popads")
                || low.contains("adcash") || low.contains("propellerads")
                || low.contains("histats.com") || low.contains("onclick")
                || low.contains("hilltopads") || low.contains("exoclick");
    }

    private static String firstMatch(String text, Pattern p) {
        Matcher m = p.matcher(text);
        return m.find() ? m.group(1) : null;
    }

    /** masterinf = {"kid": "key"} from a wrapper page, when one is there. */
    private static Map<String, String> keysIn(String text) {
        Matcher m = P_MASTERINF.matcher(text);
        if (!m.find()) return null;
        Map<String, String> out = new HashMap<String, String>();
        Matcher km = P_KEYKV.matcher(m.group(1));
        while (km.find()) out.put(km.group(1).toLowerCase(Locale.US), km.group(2).toLowerCase(Locale.US));
        return out.size() > 0 ? out : null;
    }

    /**
     * The manifest URL a wrapper page's script carries, when it carries one.
     * DIRECT_STREAM_URL has no manifest extension (the "application1" style) --
     * it still answers with an HLS playlist, and the relay reparses it anyway.
     */
    private static String directStreamIn(String text) {
        String u = firstMatch(text, P_GLOBALURL);
        if (u != null) return u;
        u = firstMatch(text, P_DIRECT);
        if (u != null) return u;
        Matcher m = P_M3U8.matcher(text);
        if (m.find()) return m.group(1);
        return null;
    }

    /** Does this manifest family want sessionId=<guid> appended? */
    private static boolean wantsSession(String url) {
        String low = url == null ? "" : url.toLowerCase(Locale.US);
        return low.contains("akamaized") || low.contains("/gp/") || low.contains("lpnfl");
    }

    private static final String[] CHATTY = {"chat", "discord", "telegram", "t.me/",            "whatsapp", "instagram", "tiktok", "facebook.com", "twitter.com", "x.com/",
            "youtube.com", "disqus", "livechat", "21aims"};

    /**
     * How much a frame looks like the player rather than the furniture.
     *
     * Football pages put their chat widget in the first iframe; "the first frame
     * wins" walks into a chat room and reports "no stream" while the player sits one
     * frame over. Chat/social frames score below zero, player-ish URLs above it, and
     * an ad host is never followed at all.
     */
    private static int frameScore(String u) {
        String low = u == null ? "" : u.toLowerCase(Locale.US);
        if (isAdish(low)) return -99;
        int score = 0;
        for (String c : CHATTY) {
            if (low.contains(c)) { score -= 10; break; }
        }
        if (low.contains("/embed") || low.contains("/player") || low.contains("play.php")
                || low.contains("/live/")) score += 6;
        if (low.contains("/main/")) score += 4;
        if (low.contains(".m3u8") || low.contains(".mpd")) score += 5;
        if (low.startsWith("http")) score += 1;
        return score;
    }

    /**
     * The next player URL a wrapper page hands the picture to: a static iframe, a
     * rotator entry, or an iframe built in JS. The best-scoring candidate wins.
     * Relative values are returned as-is; the caller joins.
     */
    private static String nextHopIn(String text) {
        List<String> cands = new ArrayList<String>();
        Matcher m = P_IFRAME.matcher(text);
        while (m.find()) cands.add(m.group(1));
        m = P_VALUE.matcher(text);
        while (m.find()) {
            String v = m.group(1);
            if (v != null && v.contains("http")) cands.add(v);
        }
        m = P_JS_SRC.matcher(text);
        while (m.find()) {
            int at = m.start(1);
            String ctx = text.substring(Math.max(0, at - 260),
                    Math.min(text.length(), at + 260)).toLowerCase(Locale.US);
            if (ctx.contains("iframe")) cands.add(m.group(1));
        }
        String best = null;
        int bestScore = -99;
        for (String u : cands) {
            if (u == null || u.startsWith("javascript:") || u.startsWith("#")) continue;
            int s = frameScore(u);
            if (s > bestScore) { best = u; bestScore = s; }
        }
        if (best == null || bestScore <= -10) return null;
        return best;
    }

    private JSONObject harvestStream(String page) throws Exception {
        if (page == null || page.length() == 0)
            return new JSONObject().put("ok", false).put("error", "missing u");
        List<String> links;
        try { links = linksFor(page); } catch (Exception e) { links = new ArrayList<String>(); }
        if (links.isEmpty()) links = new ArrayList<String>(java.util.Arrays.asList(page));
        if (links.size() > 4) links = links.subList(0, 4);
        JSONArray tried = new JSONArray();
        JSONArray traces = new JSONArray();
        for (String main : links) {
            tried.put(main);
            JSONObject hit = followChain(main, page);
            if (hit != null && hit.optBoolean("ok")) {
                hit.put("page", page).put("tried", tried);
                return hit;
            }
            if (hit != null) traces.put(hit);
        }
        // keep every chain's per-hop trace on the failure: "why" beats "no"
        return new JSONObject()
                .put("ok", false)
                .put("tried", tried)
                .put("chains", traces);
    }

    /** Walk the wrapper chain until a manifest name shows up (5 hops max). */
    private JSONObject followChain(String cur, String eventPage) {
        String referer = (eventPage == null || eventPage.length() == 0) ? cur : eventPage;
        Set<String> seen = new HashSet<String>();
        Map<String, String> keys = null;
        JSONArray trace = new JSONArray();
        String reason = "ran out of hops";
        for (int hop = 0; hop < 6 && cur != null; hop++) {
            if (seen.contains(cur)) { reason = "loop at hop " + hop; break; }
            seen.add(cur);
            Map<String, String> hd = new HashMap<String, String>();
            if (referer.length() > 0) hd.put("Referer", referer);
            Http.Resp resp;
            try {
                resp = Http.get(cur, 15000, hd);
            } catch (Exception e) {
                reason = "hop " + hop + " (" + hostOf(cur) + ") failed: " + String.valueOf(e.getMessage());
                trace.put(reason);
                return failWith(reason, trace);
            }
            String text = resp.text();
            Map<String, String> k = keysIn(text);
            if (k != null) keys = k;
            String found = directStreamIn(text);
            if (found != null) {
                found = Http.join(resp.finalUrl, found);
                // The provider's own player appends sessionId=<guid> to *this* family
                // of manifests (the Akamai "GP" entitlement feeds answer 400 without
                // it). Two rules matter: only that family, and a correct separator --
                // appending "&sessionId=" to a URL with no query string turns the path
                // into "index.m3u8&sessionId=..." and the CDN answers 404, which looked
                // exactly like "the chain found nothing".
                if (!found.toLowerCase(Locale.US).contains("sessionid=") && wantsSession(found)) {
                    found = found + (found.indexOf('?') >= 0 ? "&" : "?")
                            + "sessionId=" + java.util.UUID.randomUUID();
                }
                if (keys != null) lastClearKeys = keys;
                try {
                    return new JSONObject()
                            .put("ok", true)
                            .put("m3u8", found)
                            .put("via", hostOf(found))
                            .put("hops", seen.size())
                            .put("referer", resp.finalUrl);
                } catch (Exception e) {
                    return null;
                }
            }
            trace.put("hop " + hop + ": " + hostOf(cur) + " (" + text.length() + " bytes), no stream named");
            String nxt = nextHopIn(text);
            if (nxt == null) { reason = "hop " + hop + " (" + hostOf(cur) + ") names no iframe and no stream"; break; }
            cur = nxt.startsWith("http") ? nxt : Http.join(resp.finalUrl, nxt);
        }
        return failWith(reason, trace);
    }

    private static JSONObject failWith(String reason, JSONArray trace) {
        try {
            return new JSONObject().put("ok", false).put("error", reason).put("trace", trace);
        } catch (Exception e) {
            return null;
        }
    }

    private static byte[] hexToBytes(String hex) {
        byte[] b = new byte[hex.length() / 2];
        for (int i = 0; i < b.length; i++)
            b[i] = (byte) Integer.parseInt(hex.substring(i * 2, i * 2 + 2), 16);
        return b;
    }

    /** JWK bytes are unpadded base64url; the harvested key material is lowercase hex. */
    private static String b64url(byte[] b) {
        String A = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
        StringBuilder sb = new StringBuilder(((b.length + 2) / 3) * 4);
        for (int i = 0; i < b.length; i += 3) {
            int n = Math.min(3, b.length - i);
            int v = ((b[i] & 0xff) << 16)
                    | ((n > 1 ? b[i + 1] & 0xff : 0) << 8)
                    | (n > 2 ? b[i + 2] & 0xff : 0);
            sb.append(A.charAt((v >> 18) & 63)).append(A.charAt((v >> 12) & 63));
            if (n > 1) sb.append(A.charAt((v >> 6) & 63));
            if (n > 2) sb.append(A.charAt(v & 63));
        }
        while (sb.charAt(sb.length() - 1) == '=') sb.setLength(sb.length() - 1);
        return sb.toString();
    }

    // --------------------------------------------------------------- relay

    /** One player at a time: a new channel retires the previous relay. */
    private Relay startRelay(String url, boolean low, String refererOverride) throws Exception {
        synchronized (relayLock) {
            Relay cur = relay;
            if (cur != null && !cur.isStopped() && cur.source.equals(url) && relayLow == low) {
                cur.lastHit = System.currentTimeMillis();
                return cur;
            }
            String ref = (refererOverride != null && refererOverride.length() > 0)
                    ? refererOverride : refererFor(url);
            Relay n = new Relay(url, ref, low, dvrRoot());
            // resolve first: a dead mirror throws here and the page fails over,
            // while whatever was playing keeps playing
            n.resolve();
            if (cur != null) cur.stop();
            relay = null;
            n.start();
            relay = n;
            relayLow = low;
            // one line the harness (and a bug report) can grep: which feed, which
            // referer, what the ladder resolved to
            String res = "";
            try {
                JSONObject st = n.stats();
                JSONObject v = st.optJSONObject("variant");
                if (v != null) res = v.optInt("w") + "x" + v.optInt("h");
            } catch (Exception ignored) { }
            Log.i(TAG, "relay started: " + url + "  ref=" + (ref == null ? "" : ref)
                    + "  " + res);
            // the picture's sound may live in its own playlist: relay that too,
            // under /relay/a/ so its segments cannot collide with the video's
            Relay oldAudio = relayAudio;
            relayAudio = null;
            if (n.audioUrl() != null) {
                try {
                    Relay an = new Relay(n.audioUrl(), ref, low, dvrRoot());
                    an.setPrefix("a/");
                    an.resolve();
                    an.start();
                    relayAudio = an;
                    Log.i(TAG, "audio relay up: " + n.audioUrl());
                } catch (Exception e) {
                    Log.i(TAG, "audio relay failed: " + e.getMessage());
                }
            }
            if (oldAudio != null) oldAudio.stop();
            return n;
        }
    }

    private static final Pattern SEG = Pattern.compile("^seg/(\\d+)\\.(ts|m4s|mp4)$");

    private void relayRoute(Req r, OutputStream out, String sub) throws Exception {
        Relay rl = relay;
        if (rl == null || rl.isStopped()) {
            send(out, r, 404, "text/plain", bytes("no relay running"), null);
            return;
        }
        rl.lastHit = System.currentTimeMillis();
        rl.hits++;
        // ---- the audio rendition, when the feed keeps its sound separately ----
        if (sub.equals("master.m3u8")) {
            // a master that names our own video playlist plus the audio one, so
            // the player can attach the sound the raw variant playlist lacks
            Relay au = relayAudio;
            StringBuilder b = new StringBuilder(512);
            b.append("#EXTM3U\n#EXT-X-VERSION:6\n");
            if (au != null && !au.isStopped()) {
                b.append("#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"aud\",NAME=\"main\",")
                 .append("DEFAULT=YES,AUTOSELECT=YES,URI=\"a.m3u8\"\n");
            }
            JSONObject v = rl.stats().optJSONObject("variant");
            long bw = v == null ? 0 : v.optLong("bw");
            int w = v == null ? 0 : v.optInt("w");
            int h = v == null ? 0 : v.optInt("h");
            String codecs = v == null ? "" : v.optString("codecs");
            b.append("#EXT-X-STREAM-INF:BANDWIDTH=").append(Math.max(bw, 1000000));
            if (w > 0 && h > 0) b.append(",RESOLUTION=").append(w).append('x').append(h);
            if (codecs != null && codecs.length() > 0) b.append(",CODECS=\"").append(codecs).append('"');
            if (au != null && !au.isStopped()) b.append(",AUDIO=\"aud\"");
            b.append("\nlive.m3u8\n");
            Map<String, String> x = new HashMap<String, String>();
            x.put("Cache-Control", "no-cache");
            send(out, r, 200, "application/vnd.apple.mpegurl", bytes(b.toString()), x);
            return;
        }
        if (sub.equals("a.m3u8") || sub.startsWith("a/")) {
            Relay au = relayAudio;
            if (au == null || au.isStopped()) {
                send(out, r, 404, "text/plain", bytes("no audio relay"), null);
                return;
            }
            au.lastHit = System.currentTimeMillis();
            if (sub.equals("a.m3u8")) {
                String pl = au.playlist();
                if (pl == null) {
                    send(out, r, 503, "application/vnd.apple.mpegurl",
                            bytes("#EXTM3U\n#EXT-X-TARGETDURATION:2\n"), null);
                    return;
                }
                Map<String, String> x = new HashMap<String, String>();
                x.put("Cache-Control", "no-cache");
                send(out, r, 200, "application/vnd.apple.mpegurl", bytes(pl), x);
                return;
            }
            String inner = sub.substring(2);          // "seg/N.ts" or "init.mp4"
            if (inner.equals("init.mp4")) {
                byte[] d = au.initData();
                if (d == null) send(out, r, 404, "text/plain", bytes("no init segment"), null);
                else send(out, r, 200, "audio/mp4", d, null);
                return;
            }
            Matcher am = SEG.matcher(inner);
            if (am.matches()) {
                byte[] d;
                try {
                    d = au.segment(Long.parseLong(am.group(1)));
                } catch (Exception e) {
                    send(out, r, 404, "text/plain", bytes("audio segment: " + e.getMessage()), null);
                    return;
                }
                Map<String, String> x = new HashMap<String, String>();
                x.put("Cache-Control", "max-age=60");
                send(out, r, 200, "ts".equals(am.group(2)) ? "video/mp2t" : "audio/mp4", d, x);
                return;
            }
            send(out, r, 404, "text/plain", bytes("not found"), null);
            return;
        }
        if (sub.equals("live.m3u8") || sub.equals("index.m3u8")) {
            rl.plHits++;
            String pl = rl.playlist();
            if (pl == null) {
                send(out, r, 503, "application/vnd.apple.mpegurl", bytes("#EXTM3U\n#EXT-X-TARGETDURATION:2\n"), null);
                return;
            }
            Map<String, String> x = new HashMap<String, String>();
            x.put("Cache-Control", "no-cache");
            send(out, r, 200, "application/vnd.apple.mpegurl", bytes(pl), x);
            return;
        }
        if (sub.equals("health")) {
            send(out, r, rl.ready() ? 200 : 503, "text/plain", bytes(rl.ready() ? "ready" : "warming"), null);
            return;
        }
        if (sub.equals("stats")) { json(out, r, rl.stats()); return; }
        if (sub.equals("window")) {
            String n = r.q("n");
            if (n.matches("\\d+")) rl.setWindow(Integer.parseInt(n));
            json(out, r, rl.windowInfo());
            return;
        }
        if (sub.equals("source")) { send(out, r, 200, "text/plain", bytes(rl.source + "\n"), null); return; }
        if (sub.equals("init.mp4")) {
            byte[] d = rl.initData();
            if (d == null) send(out, r, 404, "text/plain", bytes("no init segment"), null);
            else send(out, r, 200, "video/mp4", d, null);
            return;
        }
        if (sub.equals("key")) {
            try {
                send(out, r, 200, "application/octet-stream", rl.key(r.q("u")), null);
            } catch (Exception e) {
                send(out, r, 502, "text/plain", bytes("key: " + e.getMessage()), null);
            }
            return;
        }
        Matcher m = SEG.matcher(sub);
        if (m.matches()) {
            rl.segHits++;
            byte[] d;
            try {
                d = rl.segment(Long.parseLong(m.group(1)));
            } catch (Exception e) {
                rl.miss++;
                send(out, r, 404, "text/plain", bytes("segment unavailable: " + e.getMessage()), null);
                return;
            }
            Map<String, String> x = new HashMap<String, String>();
            x.put("Cache-Control", "max-age=60");
            send(out, r, 200, "ts".equals(m.group(2)) ? "video/mp2t" : "video/mp4", d, x);
            return;
        }
        send(out, r, 404, "text/plain", bytes("not found"), null);
    }
}
