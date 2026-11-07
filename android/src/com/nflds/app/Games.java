package com.nflds.app;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Calendar;
import java.util.Collections;
import java.util.Comparator;
import java.util.Date;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TimeZone;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The game list, same rules as hub.py:
 *  - ESPN's scoreboard is the source of truth for which night a game is on
 *    (the stream sites stamp events with the UTC date, one day ahead of every
 *    evening tip-off)
 *  - the stream sites' ItemList JSON-LD gives the event pages, joined to the
 *    schedule by team nickname
 *  - every scheduled game is listed, stream or not, so an empty night is real
 */
final class Games {

    static final String[] SOURCES = {
            "https://methstreams.st/NFL",
            "https://crackstreams.st/NFL",
    };
    private static final String ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=";

    private final ExecutorService pool = Executors.newFixedThreadPool(6);
    private final Map<String, Object[]> dayCache = new HashMap<String, Object[]>();   // ymd -> {time, rows}
    private Object[] allCache = null;                                                // {time, games}
    final Map<String, JSONObject> sourceHealth = Collections.synchronizedMap(new HashMap<String, JSONObject>());

    // ------------------------------------------------------------ dates

    static String iso(Calendar c) {
        return String.format(Locale.US, "%04d-%02d-%02d",
                c.get(Calendar.YEAR), c.get(Calendar.MONTH) + 1, c.get(Calendar.DAY_OF_MONTH));
    }

    static String todayIso() { return iso(Calendar.getInstance()); }

    static Calendar parseIso(String d) {
        String[] p = d.split("-");
        Calendar c = Calendar.getInstance();
        c.clear();
        c.set(Integer.parseInt(p[0]), Integer.parseInt(p[1]) - 1, Integer.parseInt(p[2]), 12, 0, 0);
        return c;
    }

    static String shift(String d, int days) {
        Calendar c = parseIso(d);
        c.add(Calendar.DAY_OF_MONTH, days);
        return iso(c);
    }

    static String localTime(String utc) {
        if (utc == null || utc.length() == 0) return "";
        String[] fmts = {"yyyy-MM-dd'T'HH:mm'Z'", "yyyy-MM-dd'T'HH:mm:ss'Z'", "yyyy-MM-dd'T'HH:mm:ss.SSS'Z'"};
        for (String f : fmts) {
            try {
                SimpleDateFormat in = new SimpleDateFormat(f, Locale.US);
                in.setTimeZone(TimeZone.getTimeZone("UTC"));
                Date d = in.parse(utc);
                return new SimpleDateFormat("h:mm a", Locale.US).format(d);
            } catch (Exception ignored) { }
        }
        return "";
    }

    // ------------------------------------------------------------- ESPN

    /** ESPN fixtures for one day, cached 5 min (10 min outside the warm window). */
    List<JSONObject> espnDay(String isoDate) {
        synchronized (dayCache) {
            Object[] hit = dayCache.get(isoDate);
            if (hit != null && System.currentTimeMillis() - (Long) hit[0] < 300000) {
                @SuppressWarnings("unchecked") List<JSONObject> rows = (List<JSONObject>) hit[1];
                return rows;
            }
        }
        List<JSONObject> rows = new ArrayList<JSONObject>();
        boolean ok = false;
        try {
            String ymd = isoDate.replace("-", "");
            JSONObject data = new JSONObject(Http.text(ESPN + ymd, 15000));
            JSONArray evs = data.optJSONArray("events");
            ok = true;
            for (int i = 0; evs != null && i < evs.length(); i++) {
                JSONObject ev = evs.getJSONObject(i);
                JSONArray comps = ev.optJSONArray("competitions");
                if (comps == null || comps.length() == 0) continue;
                JSONObject comp = comps.getJSONObject(0);
                JSONArray cs = comp.optJSONArray("competitors");
                JSONObject away = null, home = null;
                for (int k = 0; cs != null && k < cs.length(); k++) {
                    JSONObject c = cs.getJSONObject(k);
                    JSONObject t = c.optJSONObject("team");
                    if (t == null) continue;
                    if ("away".equals(c.optString("homeAway"))) away = t; else home = t;
                }
                if (away == null || home == null) continue;
                JSONObject type = comp.optJSONObject("status") == null ? null
                        : comp.optJSONObject("status").optJSONObject("type");
                JSONObject r = new JSONObject();
                r.put("date", isoDate);
                r.put("away", away.optString("displayName"));
                r.put("home", home.optString("displayName"));
                r.put("start_utc", ev.optString("date"));
                r.put("state", type == null ? "" : type.optString("state"));
                r.put("detail", type == null ? "" : type.optString("detail"));
                rows.add(r);
            }
        } catch (Exception ignored) { }
        if (ok) {
            synchronized (dayCache) {
                if (dayCache.size() > 240) dayCache.clear();
                dayCache.put(isoDate, new Object[]{System.currentTimeMillis(), rows});
            }
        }
        return rows;
    }

    /** today -3 .. +10, fetched in parallel. */
    List<JSONObject> window() {
        String t = todayIso();
        List<Future<List<JSONObject>>> fs = new ArrayList<Future<List<JSONObject>>>();
        for (int off = -3; off <= 10; off++) {
            final String d = shift(t, off);
            fs.add(pool.submit(new java.util.concurrent.Callable<List<JSONObject>>() {
                public List<JSONObject> call() { return espnDay(d); }
            }));
        }
        List<JSONObject> out = new ArrayList<JSONObject>();
        for (Future<List<JSONObject>> f : fs) {
            try { out.addAll(f.get()); } catch (Exception ignored) { }
        }
        return out;
    }

    // ------------------------------------------------------- stream sites

    private static final Pattern LD = Pattern.compile("(?s)<script type=\"application/ld\\+json\">(.*?)</script>");

    private static List<JSONObject> scrape(String src) throws Exception {
        String html = Http.text(src, 20000);
        List<JSONObject> out = new ArrayList<JSONObject>();
        Matcher m = LD.matcher(html);
        String host = new java.net.URL(src).getHost();
        while (m.find()) {
            String blob = m.group(1);
            if (!blob.contains("ItemList")) continue;
            JSONObject obj;
            try { obj = new JSONObject(blob.trim()); } catch (Exception e) { continue; }
            JSONArray items = obj.optJSONArray("itemListElement");
            for (int i = 0; items != null && i < items.length(); i++) {
                JSONObject it = items.optJSONObject(i);
                if (it == null) continue;
                String u = it.optString("url");
                if (u.length() == 0) continue;
                JSONObject g = new JSONObject();
                g.put("name", it.optString("name").replaceAll("\\s+", " ").trim());
                g.put("url", u);
                g.put("date", dateOf(u));
                g.put("src", host);
                out.add(g);
            }
        }
        return out;
    }

    /** '.../event/m-okc-thunder-vs-cleveland-cavaliers-1103' -> {away, home} */
    private static String[] slugMatch(String url) {
        String s = url.replaceAll("/+$", "");
        int e = s.indexOf("/event/");
        if (e >= 0) s = s.substring(e + 7);
        s = s.replaceAll("-\\d{4}$", "").replaceAll("^m-", "");
        int v = s.indexOf("-vs-");
        if (v < 0) return null;
        return new String[]{s.substring(0, v).replace('-', ' '), s.substring(v + 4).replace('-', ' ')};
    }

    private static String dateOf(String url) {
        Matcher m = Pattern.compile("-(\\d{2})(\\d{2})$").matcher(url.replaceAll("/+$", ""));
        if (!m.find()) return "";
        int mon = Integer.parseInt(m.group(1)), day = Integer.parseInt(m.group(2));
        if (mon < 1 || mon > 12 || day < 1 || day > 31) return "";
        return String.format(Locale.US, "%04d-%02d-%02d", Calendar.getInstance().get(Calendar.YEAR), mon, day);
    }

    private static String nick(String team) {
        String[] p = team.trim().split("\\s+");
        return p.length == 0 ? "" : p[p.length - 1].toLowerCase(Locale.US);
    }

    private static JSONObject fixture(JSONObject r) throws Exception {
        JSONObject g = new JSONObject();
        g.put("name", r.optString("away") + " vs " + r.optString("home"));
        g.put("url", "");
        g.put("date", r.optString("date"));
        g.put("time", localTime(r.optString("start_utc")));
        g.put("utc", r.optString("start_utc"));
        g.put("state", r.optString("state"));
        g.put("detail", r.optString("detail"));
        g.put("src", "espn");
        g.put("verified", true);
        g.put("stream", false);
        return g;
    }

    /** Every game in the warm window, streams joined to fixtures. Cached 4 min. */
    synchronized List<JSONObject> all() {
        if (allCache != null && System.currentTimeMillis() - (Long) allCache[0] < 240000) {
            @SuppressWarnings("unchecked") List<JSONObject> g = (List<JSONObject>) allCache[1];
            return g;
        }
        // schedule and both sources at once: ~1 s instead of ~4
        Future<List<JSONObject>> fw = pool.submit(new java.util.concurrent.Callable<List<JSONObject>>() {
            public List<JSONObject> call() { return window(); }
        });
        List<Future<List<JSONObject>>> fsrc = new ArrayList<Future<List<JSONObject>>>();
        for (final String src : SOURCES) {
            fsrc.add(pool.submit(new java.util.concurrent.Callable<List<JSONObject>>() {
                public List<JSONObject> call() throws Exception { return scrape(src); }
            }));
        }
        List<JSONObject> rows;
        try { rows = fw.get(); } catch (Exception e) { rows = new ArrayList<JSONObject>(); }
        List<JSONObject> games = new ArrayList<JSONObject>();
        Set<String> seen = new HashSet<String>();
        Set<String> matched = new HashSet<String>();
        int foreign = 0;                       // events from another competition (college)
        String now = new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date());
        for (int i = 0; i < SOURCES.length; i++) {
            List<JSONObject> scraped;
            JSONObject h = new JSONObject();
            try {
                scraped = fsrc.get(i).get();
                h.put("ok", true).put("found", scraped.size()).put("at", now);
            } catch (Exception e) {
                try {
                    String msg = String.valueOf(e.getCause() != null ? e.getCause().getMessage() : e.getMessage());
                    h.put("ok", false).put("found", 0).put("at", now)
                            .put("error", msg.length() > 80 ? msg.substring(0, 80) : msg);
                } catch (Exception ignored) { }
                sourceHealth.put(SOURCES[i], h);
                continue;
            }
            sourceHealth.put(SOURCES[i], h);
            for (JSONObject g : scraped) {
                try {
                    String[] am = slugMatch(g.optString("url"));
                    JSONObject hit = null;
                    if (am != null) {
                        for (JSONObject r : rows) {
                            String al = nick(r.optString("away")), hl = nick(r.optString("home"));
                            if (al.length() > 0 && hl.length() > 0 && am[0].contains(al) && am[1].contains(hl)) {
                                hit = r;
                                break;
                            }
                        }
                    }
                    if (hit != null) {
                        g.put("date", hit.optString("date"));
                        g.put("time", localTime(hit.optString("start_utc")));
                        g.put("name", hit.optString("away") + " vs " + hit.optString("home"));
                        g.put("verified", true);
                        g.put("utc", hit.optString("start_utc"));
                        g.put("state", hit.optString("state"));
                        g.put("detail", hit.optString("detail"));
                        g.put("stream", true);
                        matched.add(hit.optString("date") + "|" + hit.optString("away") + "|" + hit.optString("home"));
                    } else {
                        // This list is football, not the NFL: on a Saturday it is
                        // college games (Alabama vs LSU, Air Force vs Army). An event
                        // that matched no NFL fixture is another sport's game, so it
                        // is not listed at all -- an NFL app that shows college games
                        // is worse than one that says "no stream listed yet".
                        foreign++;
                        continue;
                    }
                    String key = g.optString("name") + "|" + g.optString("date");
                    if (seen.add(key)) games.add(g);
                } catch (Exception ignored) { }
            }
            try { h.put("foreign", foreign); } catch (Exception ignored) { }
        }
        for (JSONObject r : rows) {
            String key = r.optString("date") + "|" + r.optString("away") + "|" + r.optString("home");
            if (matched.contains(key)) continue;
            String nm = r.optString("away") + " vs " + r.optString("home");
            if (seen.contains(nm + "|" + r.optString("date"))) continue;
            try { games.add(fixture(r)); } catch (Exception ignored) { }
        }
        allCache = new Object[]{System.currentTimeMillis(), games};
        return games;
    }

    synchronized void invalidate() { allCache = null; }

    List<JSONObject> forDate(String date) {
        List<JSONObject> out = new ArrayList<JSONObject>();
        for (JSONObject g : all()) if (date.equals(g.optString("date"))) out.add(g);
        if (out.isEmpty()) {
            // outside the warm window: ask ESPN about that day directly
            for (JSONObject r : espnDay(date)) {
                try { out.add(fixture(r)); } catch (Exception ignored) { }
            }
        }
        Collections.sort(out, new Comparator<JSONObject>() {
            public int compare(JSONObject a, JSONObject b) {
                String ua = a.optString("utc", ""), ub = b.optString("utc", "");
                if (ua.length() == 0) ua = "9999";
                if (ub.length() == 0) ub = "9999";
                int c = ua.compareTo(ub);
                return c != 0 ? c : a.optString("name").compareTo(b.optString("name"));
            }
        });
        return out;
    }

    // -------------------------------------------------------- game pages

    private static final Pattern MAIN = Pattern.compile("https?://[^\"'<>\\s]+?/main/(\\d+)");
    private static final Pattern MAIN_REL = Pattern.compile(
            "[\"'](/[^\"'\\s]{0,200}/main/(\\d+))[\"']");
    private static final Pattern MAIN_SRC = Pattern.compile(
            "data-e=[\"']([^\"']+)[\"'][^>]*", Pattern.CASE_INSENSITIVE);

    /** All 'Main N' player links on an event page, in order. The skins differ:
     *  some print absolute URLs, some print /main/N relative to the event, and
     *  some carry the player page in a data-e= attribute instead of an href. */
    static List<String> feedLinks(String page) {
        final List<String> out = new ArrayList<String>();
        try {
            String html = Http.text(page, 15000);
            Matcher m = MAIN.matcher(html);
            while (m.find()) if (!out.contains(m.group(0))) out.add(m.group(0));
            if (out.isEmpty()) {
                m = MAIN_REL.matcher(html);
                while (m.find()) {
                    String abs = Http.join(page, m.group(1));
                    if (!out.contains(abs)) out.add(abs);
                }
            }
            if (out.isEmpty()) {
                // href gone entirely: the schedule rows carry data-e="<main page>"
                m = MAIN_SRC.matcher(html);
                while (m.find()) {
                    String raw = m.group(1).replace("&amp;", "&");
                    if (raw.indexOf("/main/") < 0) continue;
                    String abs = raw.startsWith("http") ? raw : Http.join(page, raw);
                    if (!out.contains(abs)) out.add(abs);
                }
            }
        } catch (Exception ignored) { }
        Collections.sort(out, new Comparator<String>() {
            public int compare(String a, String b) { return num(a) - num(b); }
        });
        return out;
    }

    private static int num(String u) {
        Matcher m = Pattern.compile("/main/(\\d+)").matcher(u);
        return m.find() ? Integer.parseInt(m.group(1)) : 0;
    }
}
