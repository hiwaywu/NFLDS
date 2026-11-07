package com.nflds.app;

import android.util.Log;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.net.URLEncoder;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.CompletionService;
import java.util.concurrent.ExecutorCompletionService;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The HLS relay from nfl/relay.py, running inside the app.
 *
 *  (a) always picks the highest-quality rendition of a master playlist
 *  (b) keeps a rolling window of fully downloaded segments in RAM, and only
 *      advertises a segment once all of it is here -- the player reads from
 *      localhost and cannot stutter on a slow CDN segment
 *  (c) pulls missing segments in parallel, newest first, so the live edge lands
 *      first and one slow socket cannot throttle the stream
 *  (d) a disk tier in the app cache keeps what scrolls out of RAM (pause and
 *      rewind work without racing the source); wiped when the relay stops
 *
 * Two things the Python relay dropped and this one keeps:
 *  - #EXT-X-DISCONTINUITY (ad splices): without it the player tries to join
 *    two unrelated timelines and the picture freezes or jumps
 *  - #EXT-X-KEY (AES-128): served through key?u=... so CORS cannot block it
 *
 * Served by LocalHub under /relay/: live.m3u8, seg/N.ts, init.mp4, key, stats,
 * health, window, source.
 */
final class Relay {

    private static final String TAG = "nflhub.relay";

    final String source;
    private final String referer;
    private volatile String url;                 // resolved MEDIA playlist
    private volatile int window;
    private final int prefetch;
    private final File dvrDir;
    private final int dvrMinutes;
    private final long dvrMaxBytes;
    private final long idleMs;

    private final LinkedHashMap<Long, byte[]> segs = new LinkedHashMap<Long, byte[]>();
    private final LinkedHashMap<Long, File> disk = new LinkedHashMap<Long, File>();
    private final Map<Long, Double> durs = new HashMap<Long, Double>();
    private final Map<Long, String> uris = new HashMap<Long, String>();
    private final Map<Long, String> keys = new HashMap<Long, String>();     // seq -> rewritten KEY line
    private final Set<Long> discs = new HashSet<Long>();                     // seq starts a new timeline
    private final Map<Long, Long> dseq = new HashMap<Long, Long>();          // seq -> discontinuity number
    private final Map<String, byte[]> keyCache = Collections.synchronizedMap(new HashMap<String, byte[]>());

    private volatile byte[] mapData;
    private volatile String segExt = "ts";
    /** Path prefix this relay answers on ("a/" for the audio rendition), so two
     *  relays can live under /relay/ without their segment names colliding. */
    private volatile String prefix = "";
    /** The variant's separate audio playlist, when the master declares one.
     *  Without it a picture plays silent: the video variant carries no audio. */
    private volatile String audioUrl;    private volatile double target = 6.0;
    private volatile boolean ready = false;
    private volatile boolean stopped = false;
    private long floor = -1;                     // never fetch behind where we joined
    volatile long lastHit = System.currentTimeMillis();

    private long bytesTotal = 0, segsTotal = 0, dvrBytes = 0;
    private volatile double throughput = 0;      // bits/s, last batch
    private volatile String lastError = "";
    private volatile JSONObject variant = new JSONObject();
    private volatile String audioNote = "";
    private volatile int renditions = 0;
    int hits = 0, plHits = 0, segHits = 0, miss = 0;

    private final ExecutorService pool;
    private Thread pumpThread;

    Relay(String source, String referer, boolean low, File dvrRoot) {
        this.source = source;
        this.url = source;
        this.referer = referer;
        this.window = low ? 5 : 10;
        this.prefetch = low ? 3 : 6;
        this.dvrMinutes = low ? 5 : 10;              // a phone, not a PC: smaller disk tier
        this.dvrMaxBytes = 600L * 1024 * 1024;
        this.idleMs = 180 * 1000L;                   // nobody asked for 3 min: stop pulling data
        this.dvrDir = dvrRoot == null ? null : new File(dvrRoot, "r" + Long.toHexString(System.nanoTime()));
        this.pool = Executors.newFixedThreadPool(prefetch + 1);
    }

    private Map<String, String> hdrs() {
        if (referer == null || referer.length() == 0) return null;
        Map<String, String> h = new HashMap<String, String>();
        h.put("Referer", referer);
        try {
            java.net.URL r = new java.net.URL(referer);
            h.put("Origin", r.getProtocol() + "://" + r.getHost());
        } catch (Exception ignored) { }
        return h;
    }

    // ------------------------------------------------------------ playlists

    private static final Pattern ATTR = Pattern.compile("([A-Z0-9-]+)=(\"[^\"]*\"|[^,]*)");

    static Map<String, String> attrs(String line) {
        Map<String, String> out = new HashMap<String, String>();
        int c = line.indexOf(':');
        if (c < 0) return out;
        Matcher m = ATTR.matcher(line.substring(c + 1));
        while (m.find()) {
            String v = m.group(2);
            if (v.startsWith("\"") && v.endsWith("\"") && v.length() >= 2) v = v.substring(1, v.length() - 1);
            out.put(m.group(1), v);
        }
        return out;
    }

    /** Master -> best rendition (height, then bandwidth). */
    void resolve() throws Exception {
        Http.Resp r = Http.get(url, 12000, hdrs());
        String txt = r.text();
        if (!txt.contains("#EXTM3U")) throw new Exception("not an HLS playlist");
        if (!txt.contains("#EXT-X-STREAM-INF")) {
            url = r.finalUrl;
            renditions = 1;
            return;
        }
        String[] lines = txt.split("\r?\n");
        Map<String, Boolean> audioGroups = new HashMap<String, Boolean>();
        Map<String, String> audioUris = new HashMap<String, String>();
        for (String l : lines) {
            l = l.trim();
            if (l.startsWith("#EXT-X-MEDIA:")) {
                Map<String, String> a = attrs(l);
                if ("AUDIO".equals(a.get("TYPE")) && a.containsKey("URI")) {
                    String g = a.get("GROUP-ID");
                    audioGroups.put(g, true);
                    // DEFAULT=YES wins; otherwise the first one listed
                    if (!audioUris.containsKey(g) || "YES".equalsIgnoreCase(a.get("DEFAULT")))
                        audioUris.put(g, Http.join(r.finalUrl, a.get("URI")));
                }
            }
        }
        String bestUrl = null;
        int bestH = -1;
        long bestBw = -1;
        Map<String, String> bestA = null;
        for (int i = 0; i < lines.length; i++) {
            String l = lines[i].trim();
            if (!l.startsWith("#EXT-X-STREAM-INF")) continue;
            Map<String, String> a = attrs(l);
            int j = i + 1;
            while (j < lines.length && (lines[j].trim().length() == 0 || lines[j].trim().startsWith("#"))) j++;
            if (j >= lines.length) continue;
            renditions++;
            int h = 0;
            String res = a.get("RESOLUTION");
            if (res != null && res.contains("x")) {
                try { h = Integer.parseInt(res.split("x")[1].trim()); } catch (Exception ignored) { }
            }
            long bw = 0;
            try { bw = Long.parseLong(a.containsKey("BANDWIDTH") ? a.get("BANDWIDTH") : "0"); } catch (Exception ignored) { }
            if (h > bestH || (h == bestH && bw > bestBw)) {
                bestH = h;
                bestBw = bw;
                bestUrl = Http.join(r.finalUrl, lines[j].trim());
                bestA = a;
            }
        }
        if (bestUrl == null) throw new Exception("master playlist had no variants");
        url = bestUrl;
        JSONObject v = new JSONObject();
        int w = 0;
        String res = bestA.get("RESOLUTION");
        if (res != null && res.contains("x")) {
            try { w = Integer.parseInt(res.split("x")[0].trim()); } catch (Exception ignored) { }
        }
        v.put("url", bestUrl);
        v.put("bw", bestBw);
        v.put("w", w);
        v.put("h", Math.max(bestH, 0));
        v.put("fps", bestA.containsKey("FRAME-RATE") ? bestA.get("FRAME-RATE") : "");
        v.put("codecs", bestA.containsKey("CODECS") ? bestA.get("CODECS") : "");
        variant = v;
        String ag = bestA.get("AUDIO");
        if (ag != null && audioGroups.containsKey(ag)) {
            // the sound is its own playlist: keep the URL so the hub can relay
            // it alongside the picture instead of leaving a silent feed
            audioUrl = audioUris.get(ag);
            audioNote = audioUrl == null
                    ? "separate audio rendition (" + ag + ") - not carried"
                    : "separate audio rendition (" + ag + ") - relayed";
        }
        Log.i(TAG, renditions + " rendition(s); picked " + w + "x" + bestH + " @ " + bestBw
                + (audioUrl == null ? "" : "  audio: yes"));
    }

    // ---------------------------------------------------------------- pump

    void start() {
        pumpThread = new Thread(new Runnable() {
            public void run() { pump(); }
        }, "relay-pump");
        pumpThread.setDaemon(true);
        pumpThread.start();
    }

    private void pump() {
        long backoff = 1000;
        while (!stopped) {
            if (System.currentTimeMillis() - lastHit > idleMs) {
                Log.i(TAG, "player idle, stopping");
                stop();
                break;
            }
            try {
                Http.Resp r = Http.get(url, 12000, hdrs());
                String base = r.finalUrl;
                String txt = r.text();
                String[] lines = txt.split("\r?\n");
                long mseq = 0, dsn = 0;
                double tgt = target;
                String mapUri = null;
                String curKey = null;
                boolean pendingDisc = false;
                Double pendingDur = null;
                boolean endlist = txt.contains("#EXT-X-ENDLIST");
                List<Long> newSeqs = new ArrayList<Long>();
                Map<Long, String> nu = new HashMap<Long, String>();
                Map<Long, Double> nd = new HashMap<Long, Double>();
                Map<Long, String> nk = new HashMap<Long, String>();
                Map<Long, Long> nds = new HashMap<Long, Long>();
                Set<Long> ndisc = new HashSet<Long>();
                long idx = 0;
                for (String raw : lines) {
                    String l = raw.trim();
                    if (l.length() == 0) continue;
                    if (l.startsWith("#EXT-X-MEDIA-SEQUENCE:")) {
                        mseq = Long.parseLong(l.substring(22).trim());
                    } else if (l.startsWith("#EXT-X-DISCONTINUITY-SEQUENCE:")) {
                        dsn = Long.parseLong(l.substring(30).trim());
                    } else if (l.startsWith("#EXT-X-TARGETDURATION:")) {
                        try { tgt = Double.parseDouble(l.substring(22).trim()); } catch (Exception ignored) { }
                    } else if (l.startsWith("#EXT-X-MAP:")) {
                        mapUri = attrs(l).get("URI");
                    } else if (l.startsWith("#EXT-X-KEY:")) {
                        curKey = rewriteKey(l, base);
                    } else if (l.equals("#EXT-X-DISCONTINUITY")) {
                        pendingDisc = true;
                    } else if (l.startsWith("#EXTINF:")) {
                        try {
                            String d = l.substring(8);
                            int c = d.indexOf(',');
                            pendingDur = Double.parseDouble((c >= 0 ? d.substring(0, c) : d).trim());
                        } catch (Exception e) {
                            pendingDur = null;
                        }
                    } else if (!l.startsWith("#")) {
                        long seq = mseq + idx;
                        idx++;
                        if (pendingDisc) { dsn++; ndisc.add(seq); }
                        nu.put(seq, Http.join(base, l));
                        nd.put(seq, pendingDur != null ? pendingDur : tgt);
                        nds.put(seq, dsn);
                        if (curKey != null) nk.put(seq, curKey);
                        newSeqs.add(seq);
                        pendingDisc = false;
                        pendingDur = null;
                    }
                }
                target = Math.max(1.0, tgt);
                if (mapUri != null && mapData == null) {
                    mapData = Http.get(Http.join(base, mapUri), 20000, hdrs()).body;
                    segExt = "m4s";
                } else if (mapUri == null && !newSeqs.isEmpty()) {
                    String u0 = nu.get(newSeqs.get(0)).toLowerCase();
                    if (u0.contains(".m4s") || u0.contains(".mp4")) segExt = "m4s";
                }
                List<Long> missing = new ArrayList<Long>();
                synchronized (this) {
                    for (Long s : newSeqs) {
                        if (!uris.containsKey(s)) {
                            uris.put(s, nu.get(s));
                            durs.put(s, nd.get(s));
                            dseq.put(s, nds.get(s));
                            if (nk.containsKey(s)) keys.put(s, nk.get(s));
                            if (ndisc.contains(s)) discs.add(s);
                        }
                        if (s >= floor && !segs.containsKey(s) && !disk.containsKey(s)) missing.add(s);
                    }
                }
                // newest first: the player starts at the live edge, so that
                // segment has to land first
                Collections.sort(missing, Collections.<Long>reverseOrder());
                // first start: there is no point downloading the whole upstream
                // window (some relays publish 5 minutes) -- the ring keeps `window`
                // first start: join with ~30 s, not the whole upstream window
                // (some relays publish 5 minutes); the ring grows forward from here
                int join = Math.min(window, 5);
                if (floor < 0 && !missing.isEmpty()) {
                    if (missing.size() > join) missing = new ArrayList<Long>(missing.subList(0, join));
                    floor = missing.get(missing.size() - 1);
                }
                if (!missing.isEmpty()) {
                    long t0 = System.currentTimeMillis();
                    long batch = 0;
                    CompletionService<Object[]> cs = new ExecutorCompletionService<Object[]>(pool);
                    for (final Long s : missing) {
                        cs.submit(new java.util.concurrent.Callable<Object[]>() {
                            public Object[] call() throws Exception { return new Object[]{s, grab(s)}; }
                        });
                    }
                    for (int i = 0; i < missing.size(); i++) {
                        try {
                            Future<Object[]> f = cs.take();
                            Object[] res = f.get();
                            long s = (Long) res[0];
                            byte[] data = (byte[]) res[1];
                            synchronized (this) {
                                segs.put(s, data);
                                sortSegs();
                                bytesTotal += data.length;
                                segsTotal++;
                                while (segs.size() > window) {
                                    Iterator<Map.Entry<Long, byte[]>> it = segs.entrySet().iterator();
                                    Map.Entry<Long, byte[]> old = it.next();
                                    it.remove();
                                    spill(old.getKey(), old.getValue());
                                }
                            }
                            batch += data.length;
                        } catch (Exception e) {
                            lastError = "segment: " + e.getMessage();
                        }
                    }
                    double dt = Math.max(1, System.currentTimeMillis() - t0) / 1000.0;
                    throughput = batch * 8 / dt;
                    // advertise only once the joining batch is complete: segments
                    // land newest-first, and a playlist published mid-batch would
                    // start earlier on every refresh -- hls.js reads that as time
                    // running backwards and the cold start fails
                    synchronized (this) { if (!segs.isEmpty()) ready = true; }
                }
                forgetOld(newSeqs.isEmpty() ? -1 : newSeqs.get(0));
                long wait = (long) (Math.min(Math.max(target / 3, 0.5), 2.0) * 1000);
                if (endlist) wait = 500;
                if (!missing.isEmpty()) wait = 50;
                Thread.sleep(wait);
                backoff = 1000;
            } catch (InterruptedException ie) {
                break;
            } catch (Exception e) {
                lastError = String.valueOf(e.getMessage());
                Log.w(TAG, "playlist error: " + e + " (retry in " + backoff + " ms)");
                try { Thread.sleep(backoff); } catch (InterruptedException ie) { break; }
                backoff = Math.min(backoff * 2, 8000);
            }
        }
    }

    /** segs is insertion-ordered; parallel downloads land out of order. */
    private void sortSegs() {
        if (segs.size() < 2) return;
        TreeSet<Long> order = new TreeSet<Long>(segs.keySet());
        boolean sorted = true;
        Iterator<Long> a = segs.keySet().iterator();
        for (Long s : order) {
            if (!s.equals(a.next())) { sorted = false; break; }
        }
        if (sorted) return;
        LinkedHashMap<Long, byte[]> copy = new LinkedHashMap<Long, byte[]>();
        for (Long s : order) copy.put(s, segs.get(s));
        segs.clear();
        segs.putAll(copy);
    }

    /** Drop bookkeeping for sequences that are neither held nor still upstream. */
    private synchronized void forgetOld(long upstreamFirst) {
        if (upstreamFirst < 0) return;
        Iterator<Long> it = uris.keySet().iterator();
        while (it.hasNext()) {
            Long s = it.next();
            if (s < upstreamFirst && !segs.containsKey(s) && !disk.containsKey(s)) {
                it.remove();
                durs.remove(s);
                keys.remove(s);
                discs.remove(s);
                dseq.remove(s);
            }
        }
    }

    private String rewriteKey(String line, String base) {
        Map<String, String> a = attrs(line);
        String m = a.get("METHOD");
        String uri = a.get("URI");
        if (uri == null || !"AES-128".equals(m)) return line;
        String abs = Http.join(base, uri);
        try {
            return line.replace("URI=\"" + uri + "\"", "URI=\"key?u=" + URLEncoder.encode(abs, "UTF-8") + "\"");
        } catch (Exception e) {
            return line;
        }
    }

    byte[] key(String u) throws Exception {
        byte[] k = keyCache.get(u);
        if (k != null) return k;
        k = Http.get(u, 12000, hdrs()).body;
        keyCache.put(u, k);
        return k;
    }

    private byte[] grab(long seq) throws Exception {
        String u;
        synchronized (this) { u = uris.get(seq); }
        if (u == null) throw new Exception("unknown segment " + seq);
        Exception last = null;
        for (int i = 0; i < 3; i++) {
            try {
                return Http.get(u, 20000, hdrs()).body;
            } catch (Exception e) {
                last = e;
                try { Thread.sleep(300L << i); } catch (InterruptedException ie) { throw e; }
            }
        }
        throw last;
    }

    // ------------------------------------------------------------ disk tier

    private void spill(long seq, byte[] data) {          // under lock
        if (dvrDir == null || stopped) { forgetSeq(seq); return; }
        try {
            if (!dvrDir.isDirectory() && !dvrDir.mkdirs()) throw new Exception("mkdir");
            // never fill the phone: keep 500 MB free for everything else
            if (dvrDir.getUsableSpace() < 500L * 1024 * 1024) { forgetSeq(seq); return; }
            File f = new File(dvrDir, "seg-" + seq + ".bin");
            FileOutputStream o = new FileOutputStream(f);
            try { o.write(data); } finally { o.close(); }
            disk.put(seq, f);
            dvrBytes += data.length;
        } catch (Exception e) {
            lastError = "dvr write: " + e.getMessage();
            forgetSeq(seq);
        }
        while (!disk.isEmpty() && dvrBytes > dvrMaxBytes) dropOldestDisk();
        int keep = (int) (dvrMinutes * 60 / Math.max(target, 1.0));
        while (disk.size() > keep) dropOldestDisk();
    }

    private void forgetSeq(long seq) {
        durs.remove(seq);
        uris.remove(seq);
        keys.remove(seq);
        discs.remove(seq);
        dseq.remove(seq);
    }

    private void dropOldestDisk() {
        Iterator<Map.Entry<Long, File>> it = disk.entrySet().iterator();
        Map.Entry<Long, File> e = it.next();
        it.remove();
        dvrBytes -= e.getValue().length();
        //noinspection ResultOfMethodCallIgnored
        e.getValue().delete();
        forgetSeq(e.getKey());
    }

    void stop() {
        stopped = true;
        try { pool.shutdownNow(); } catch (Exception ignored) { }
        if (pumpThread != null) pumpThread.interrupt();
        synchronized (this) {
            for (File f : disk.values()) //noinspection ResultOfMethodCallIgnored
                f.delete();
            disk.clear();
            dvrBytes = 0;
            segs.clear();
        }
        if (dvrDir != null) deleteTree(dvrDir);
    }

    boolean isStopped() { return stopped; }

    static void deleteTree(File f) {
        File[] kids = f.listFiles();
        if (kids != null) for (File k : kids) deleteTree(k);
        //noinspection ResultOfMethodCallIgnored
        f.delete();
    }

    // --------------------------------------------------------- player side

    boolean ready() { return ready; }

    String segExt() { return segExt; }
    void setPrefix(String p) { prefix = p == null ? "" : p; }
    String audioUrl() { return audioUrl; }

    byte[] initData() { return mapData; }

    synchronized void setWindow(int n) {
        window = Math.max(2, Math.min(60, n));
    }

    synchronized JSONObject windowInfo() throws Exception {
        JSONObject j = new JSONObject();
        j.put("window", window);
        j.put("segments", segs.size());
        j.put("buffered_seconds", Math.round(segs.size() * target * 10) / 10.0);
        return j;
    }

    synchronized String playlist() {
        if (!ready) return null;
        TreeSet<Long> all = new TreeSet<Long>(segs.keySet());
        all.addAll(disk.keySet());
        if (all.isEmpty()) return null;
        long first = all.first();
        double mx = 0;
        for (Long s : all) mx = Math.max(mx, durs.containsKey(s) ? durs.get(s) : target);
        StringBuilder b = new StringBuilder(4096);
        b.append("#EXTM3U\n#EXT-X-VERSION:").append(mapData != null ? 6 : 3).append('\n');
        b.append("#EXT-X-TARGETDURATION:").append((int) Math.ceil(mx)).append('\n');
        b.append("#EXT-X-MEDIA-SEQUENCE:").append(first).append('\n');
        Long d0 = dseq.get(first);
        if (d0 != null && d0 > 0) b.append("#EXT-X-DISCONTINUITY-SEQUENCE:").append(d0).append('\n');
        if (mapData != null) b.append("#EXT-X-MAP:URI=\"").append(prefix).append("init.mp4\"\n");
        String lastKey = null;
        long prev = -1;
        for (Long s : all) {
            // a hole in the sequence (a segment that never downloaded) is a
            // timeline break for the player too
            if ((prev >= 0 && s != prev + 1) || (s != first && discs.contains(s))) {
                b.append("#EXT-X-DISCONTINUITY\n");
            }
            String k = keys.get(s);
            if (k != null && !k.equals(lastKey)) { b.append(k).append('\n'); lastKey = k; }
            double d = durs.containsKey(s) ? durs.get(s) : target;
            b.append("#EXTINF:").append(String.format(java.util.Locale.US, "%.3f", d)).append(",\n");
            b.append(prefix).append("seg/").append(s).append('.').append(segExt).append('\n');
            prev = s;
        }
        return b.toString();
    }

    byte[] segment(long seq) throws Exception {
        File f;
        synchronized (this) {
            byte[] d = segs.get(seq);
            if (d != null) return d;
            f = disk.get(seq);
        }
        if (f != null) {
            FileInputStream in = new FileInputStream(f);
            try { return Http.readAll(in); } finally { in.close(); }
        }
        return grab(seq);
    }

    synchronized JSONObject stats() throws Exception {
        long b = 0;
        for (byte[] d : segs.values()) b += d.length;
        JSONObject j = new JSONObject();
        j.put("segments_cached", segs.size());
        j.put("buffered_MB", Math.round(b / 1e4) / 100.0);
        j.put("buffered_seconds", Math.round(segs.size() * target * 10) / 10.0);
        j.put("dvr_segments", disk.size());
        j.put("dvr_MB", Math.round(dvrBytes / 1e5) / 10.0);
        j.put("dvr_minutes_cap", dvrMinutes);
        j.put("dvr_seconds_stored", Math.round((segs.size() + disk.size()) * target * 10) / 10.0);
        j.put("upstream_Mbps", Math.round(throughput / 1e4) / 100.0);
        j.put("variant", variant);
        j.put("renditions", renditions);
        j.put("source", source);
        j.put("resolved", url);
        j.put("audio", audioNote);
        j.put("discontinuities", discs.size());
        j.put("last_error", lastError);
        j.put("requests", hits);
        j.put("playlist_req", plHits);
        j.put("segment_req", segHits);
        j.put("segment_404", miss);
        j.put("segments_total", segsTotal);
        j.put("MB_total", Math.round(bytesTotal / 1e5) / 10.0);
        j.put("ready", ready);
        return j;
    }
}
