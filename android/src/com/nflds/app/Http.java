package com.nflds.app;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Map;

/**
 * Outbound HTTP for the in-app hub. HttpURLConnection keeps a keep-alive pool
 * per host on its own, which is what gives the relay several parallel sockets
 * to the CDN (the "more bandwidth" part of relay.py).
 */
final class Http {

    static final String UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            + "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

    private Http() { }

    /** Body plus the URL we actually ended up at (after redirects). */
    static final class Resp {
        final byte[] body;
        final String finalUrl;
        final String type;
        final int code;

        Resp(byte[] body, String finalUrl, String type, int code) {
            this.body = body;
            this.finalUrl = finalUrl;
            this.type = type;
            this.code = code;
        }

        String text() {
            try {
                return new String(body, "UTF-8");
            } catch (Exception e) {
                return new String(body);
            }
        }
    }

    static Resp get(String url, int timeoutMs, Map<String, String> headers) throws IOException {
        String cur = url;
        // follow redirects by hand: HttpURLConnection will not hop http <-> https
        for (int hop = 0; hop < 6; hop++) {
            HttpURLConnection c = (HttpURLConnection) new URL(cur).openConnection();
            c.setConnectTimeout(Math.min(timeoutMs, 10000));
            c.setReadTimeout(timeoutMs);
            c.setInstanceFollowRedirects(false);
            c.setUseCaches(false);
            c.setRequestProperty("User-Agent", UA);
            c.setRequestProperty("Accept", "*/*");
            c.setRequestProperty("Accept-Language", "en-US,en;q=0.9");
            if (headers != null) {
                for (Map.Entry<String, String> h : headers.entrySet()) {
                    c.setRequestProperty(h.getKey(), h.getValue());
                }
            }
            int code = c.getResponseCode();
            if (code >= 300 && code < 400) {
                String loc = c.getHeaderField("Location");
                drain(c);
                if (loc == null) throw new IOException("redirect without Location");
                cur = new URL(new URL(cur), loc).toString();
                continue;
            }
            if (code >= 400) {
                drain(c);
                throw new IOException("HTTP " + code);
            }
            InputStream in = c.getInputStream();
            try {
                return new Resp(readAll(in), cur, c.getContentType(), code);
            } finally {
                try { in.close(); } catch (IOException ignored) { }
            }
        }
        throw new IOException("too many redirects");
    }

    static String text(String url, int timeoutMs) throws IOException {
        return get(url, timeoutMs, null).text();
    }

    static byte[] readAll(InputStream in) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream(256 * 1024);
        byte[] buf = new byte[64 * 1024];
        int n;
        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        return out.toByteArray();
    }

    private static void drain(HttpURLConnection c) {
        try {
            InputStream e = c.getErrorStream();
            if (e != null) { readAll(e); e.close(); }
        } catch (Exception ignored) { }
    }

    /** urljoin(base, ref) */
    static String join(String base, String ref) {
        try {
            return new URL(new URL(base), ref).toString();
        } catch (Exception e) {
            return ref;
        }
    }
}
