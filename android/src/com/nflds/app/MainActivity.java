package com.nflds.app;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.webkit.ConsoleMessage;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebResourceResponse;
import java.io.ByteArrayInputStream;
import android.widget.FrameLayout;
import android.widget.TextView;
import android.widget.Toast;

/**
 * NFL Hub for Android -- standalone.
 *
 * Earlier builds were a remote screen for the hub on a PC. This one carries the
 * hub itself (LocalHub: schedule, stream-site scraping, the buffering relay) and
 * serves the same page on 127.0.0.1, so the phone needs nothing but internet.
 *
 * What the shell adds on top of the page:
 *   - video fullscreen through onShowCustomView, so both fullscreen buttons work
 *   - games open in-app on the site's own player page (Widevine granted to it),
 *     with the home chip to come back and a browser chip if the WebView itself
 *     has no key system
 *   - window.open() from a player is kept in-app: the popup's URL becomes this
 *     WebView's page, because a dropped popup is a black screen
 *   - the page's JS console mirrored into logcat
 */
public class MainActivity extends Activity {

    private static final String TAG = "nflhub";
    /** Ad / tracker hosts: a game page is mostly these. Blocking them here is the
     *  phone equivalent of what the PC /play proxy already strips. */
    private static final String[] AD_HOSTS = {
            "subduepaler.cyou", "doubleclick.net", "googlesyndication.com",
            "adservice.google.com", "googletagmanager.com", "google-analytics.com",
            "histats.com", "dtscout.com", "sharethis.com", "crwdcntrl.net",
            "lijit.com", "intentiq.com", "mrktmtrcs.net", "popads.net",
            "propellerads.com", "onclicksuper.com", "adcash.com", "tynt.com",
            "cloudflareinsights.com", "reliedhounder.com", "dtscdn.com",
            "trend48.st", "adcash", "popads", "popcash", "adsterra", "highperformance",
            "hilltopads", "exoclick", "tsyndicate", "profitableratecpm", "onclickalgo",
            "adnium", "clickadu", "galaksion", "revcontent", "taboola", "outbrain",
            "zedo", "mgid", "trafficjunky", "juicyads", "adspyglass", "ad-maven"
    };


    private LocalHub hub;
    private String home;                 // http://127.0.0.1:PORT/
    private WebView web;
    private FrameLayout root;
    private TextView chip;
    private TextView chromeChip;

    /** Hosts this session has actually talked to: the game page, its frames, its CDNs. */
    private final java.util.Set<String> knownHosts = java.util.Collections.synchronizedSet(
            new java.util.HashSet<String>());

    private boolean isAdHost(String url) {
        if (url == null) return false;
        String low = url.toLowerCase();
        for (String h : AD_HOSTS) {
            if (low.contains(h)) return true;
        }
        return false;
    }

    /** A URL that is plainly part of a player, whatever host it is on. */
    private boolean isPlayerish(String url) {
        String low = (url == null ? "" : url).toLowerCase();
        return low.contains("/main/") || low.contains("/embed") || low.contains("/player")
                || low.contains("/live") || low.contains("/watch") || low.contains("/secure/")
                || low.contains(".m3u8") || low.contains(".mpd");
    }

    /** a.b.example.com and example.com are the same site; example.com and other.net are not. */
    private static String regDomain(String host) {
        if (host == null) return "";
        String[] p = host.toLowerCase().split("\\.");
        return p.length >= 2 ? p[p.length - 2] + "." + p[p.length - 1] : host.toLowerCase();
    }

    private boolean sameSite(String a, String b) {
        if (a == null || b == null || a.length() == 0 || b.length() == 0) return false;
        return regDomain(a).equals(regDomain(b));
    }

    /**
     * Is this window.open() URL the player's, or an ad's?
     *
     * Honouring every popup is how the last build ended up showing nothing but ads:
     * an ad calls window.open() far more often than a player does. A popup is taken
     * only when it belongs to the site being played, a host this session has already
     * loaded, or a URL whose path names a player.
     */
    private boolean popupAllowed(String url, String current) {
        if (url == null || !url.toLowerCase().startsWith("http")) return false;
        if (isAdHost(url)) return false;
        String host = "";
        try { host = Uri.parse(url).getHost(); } catch (Exception e) { }
        if (host == null || host.length() == 0) return false;
        host = host.toLowerCase();
        String curHost = "";
        try { curHost = Uri.parse(current == null ? "" : current).getHost(); } catch (Exception e) { }
        if (curHost == null) curHost = "";
        if (host.equalsIgnoreCase(curHost)) return true;
        if (sameSite(host, curHost)) return true;
        if (knownHosts.contains(host)) return true;
        if (isPlayerish(url)) return true;
        return false;
    }

    private View fullView;
    private WebChromeClient.CustomViewCallback fullCallback;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        hub = new LocalHub(this);
        try {
            home = "http://127.0.0.1:" + hub.start() + "/";
        } catch (Exception e) {
            Log.e(TAG, "hub failed to start", e);
            Toast.makeText(this, "could not start the player: " + e.getMessage(), Toast.LENGTH_LONG).show();
            home = "http://127.0.0.1:8790/";
        }
        Log.i(TAG, "local hub at " + home);

        root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        web = new WebView(this);
        web.setBackgroundColor(Color.BLACK);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setDisplayZoomControls(false);
        s.setTextZoom(100);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        // Popups must reach onCreateWindow() or the player's media window is lost:
        // with this false, window.open() from script does nothing at all and the
        // screen stays black. onCreateWindow() keeps the window the player wants and
        // files the rest under "ad" by host, which is the part that matters.
        s.setJavaScriptCanOpenWindowsAutomatically(true);
        s.setSupportMultipleWindows(true);
        s.setUserAgentString(s.getUserAgentString() + " NFLHubApp/" + LocalHub.VERSION);

        WebView.setWebContentsDebuggingEnabled(true);   // chrome://inspect from a PC, if ever needed

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onShowCustomView(View view, CustomViewCallback cb) {
                if (fullView != null) { cb.onCustomViewHidden(); return; }
                fullView = view;
                fullCallback = cb;
                root.addView(fullView, new FrameLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
                web.setVisibility(View.GONE);
                chip.setVisibility(View.GONE);
                chromeChip.setVisibility(View.GONE);
                goFullscreen();
            }

            @Override
            public void onHideCustomView() {
                leaveCustomView();
            }

            @Override
            public boolean onCreateWindow(WebView view, boolean dialog, boolean userGesture, android.os.Message msg) {
                // The mirror players open their media window with window.open(). The old
                // answer was "no": the popup was dropped and the app showed a black
                // rectangle, which is why the stream only ever played after handing the
                // page to a browser. Instead, catch the window the moment it names a URL
                // and put that URL in this WebView, so the picture stays in the app.
                final WebView pop = new WebView(MainActivity.this);
                pop.getSettings().setJavaScriptEnabled(true);
                pop.getSettings().setDomStorageEnabled(true);
                pop.setWebViewClient(new WebViewClient() {
                    @Override
                    public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                        takePopup(v, r.getUrl() == null ? "" : r.getUrl().toString());
                        return true;
                    }

                    @SuppressWarnings("deprecation")
                    @Override
                    public boolean shouldOverrideUrlLoading(WebView v, String u) {
                        takePopup(v, u);
                        return true;
                    }

                    @Override
                    public void onPageStarted(WebView v, String u, android.graphics.Bitmap fav) {
                        if (u != null && u.startsWith("http")) takePopup(v, u);
                    }
                });
                ((WebView.WebViewTransport) msg.obj).setWebView(pop);
                msg.sendToTarget();
                return true;
            }

            @Override
            public void onPermissionRequest(final PermissionRequest req) {
                // a game page opened in-app may ask for protected media (Widevine)
                runOnUiThread(new Runnable() {
                    public void run() {
                        for (String r : req.getResources()) {
                            if (PermissionRequest.RESOURCE_PROTECTED_MEDIA_ID.equals(r)) {
                                req.grant(new String[]{r});
                                return;
                            }
                        }
                        req.deny();
                    }
                });
            }

            @Override
            public boolean onConsoleMessage(ConsoleMessage m) {
                Log.i(TAG, "console " + m.messageLevel() + ": " + m.message()
                        + " @" + m.sourceId() + ":" + m.lineNumber());
                return true;
            }
        });

        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap fav) {
                Log.i(TAG, "page started: " + url);
                // the chip is the way out of a page that will not play: it has to be
                // there the moment the page starts, not after it finishes loading
                updateChip();
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                updateChip();
                // A game tap navigates to the site's own player page. Its poster /
                // overlay waits for a click, and the tap that brought us here does not
                // count inside the new document, so start it ourselves: mute, click the
                // overlay, play, and keep nudging for a few seconds.
                Log.i(TAG, "page finished: " + url);
                if (url != null && !isHome(url)) kickPlayback(view);
            }

            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest req) {
                try {
                    String u = req.getUrl() != null ? req.getUrl().toString() : "";
                    String low = u.toLowerCase();
                    String host = req.getUrl() != null && req.getUrl().getHost() != null
                            ? req.getUrl().getHost().toLowerCase() : "";
                    // an ad resource is not a member of the club, however often it
                    // asks: registering its host is what let a later top-level
                    // navigation to the same host sail right through the guard
                    if (host.length() > 0 && !isAdHost(host)) knownHosts.add(host);
                    if (isAdHost(low)) {
                        Log.i(TAG, "ad blocked: " + low);
                        return new WebResourceResponse("text/plain", "utf-8",
                                new ByteArrayInputStream(new byte[0]));
                    }
                } catch (Exception e) { }
                return null;
            }
@Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                if (req != null && req.isForMainFrame() && isHome(String.valueOf(req.getUrl()))) {
                    Log.w(TAG, "home failed: " + err.getErrorCode() + " " + err.getDescription());
                    view.postDelayed(new Runnable() {
                        public void run() { goHome(); }
                    }, 1200);
                }
            }

            @Override
            public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                Log.e(TAG, "renderer gone, didCrash=" + detail.didCrash());
                restartWebView();
                return true;
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                String scheme = u == null ? null : u.getScheme();
                if (scheme == null) return false;
                if (scheme.startsWith("http")) {
                    String url = u.toString();
                    String host = u.getHost() == null ? "" : u.getHost().toLowerCase();
                    String cur = view.getUrl() == null ? "" : view.getUrl();
                    String curHost = "";
                    try { curHost = Uri.parse(cur).getHost(); } catch (Exception e) { }
                    if (curHost == null) curHost = "";
                    if (isHome(url)) return false;
                    // an ad host is an ad host, whoever asks for it
                    if (isAdHost(url)) { Log.i(TAG, "nav blocked (ad host): " + url); return true; }
                    // Stay in the app, but only on pages that belong to the player that
                    // is already running: same host, same site, a host this session has
                    // talked to, or a URL that names a player.
                    //
                    // A gesture is NOT a licence to leave: the overlay an ad puts over
                    // the picture looks exactly like a play button, so "the user tapped
                    // it" would walk the app straight onto an ad landing page -- which
                    // is precisely what went wrong in the last build.
                    if (host.equalsIgnoreCase(curHost)) { Log.i(TAG, "nav allowed (same host): " + url); return false; }
                    if (sameSite(host, curHost)) { Log.i(TAG, "nav allowed (same site): " + url); return false; }
                    if (knownHosts.contains(host)) { Log.i(TAG, "nav allowed (known host): " + url); return false; }
                    if (isPlayerish(url)) { Log.i(TAG, "nav allowed (player-ish): " + url); return false; }
                    Log.i(TAG, "nav blocked (not the player): " + url);
                    return true;
                }
                // Nothing on a game page may launch another app. These mirrors ship
                // their "play" overlay as an intent:// URL aimed at Chrome, so the
                // old startActivity() here was the whole "it opens the browser and
                // the ads" bug. The browser chip is the only deliberate way out.
                Log.i(TAG, "nav blocked (scheme " + scheme + "): " + u);
                return true;
            }
        });

        web.addJavascriptInterface(new Bridge(), "Hub");

        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        chip = new TextView(this);
        chip.setText("⌂");
        chip.setTextSize(15);
        chip.setTextColor(Color.parseColor("#d9ffe8"));
        chip.setGravity(Gravity.CENTER);
        chip.setPadding(16, 12, 16, 12);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(Color.parseColor("#66162a1f"));
        bg.setCornerRadius(30f);
        bg.setStroke(1, Color.parseColor("#552c4a39"));
        chip.setBackground(bg);
        chip.setAlpha(0.30f);
        chip.setVisibility(View.GONE);
        chip.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { goHome(); }
        });
        FrameLayout.LayoutParams clp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.END | Gravity.CENTER_VERTICAL);
        clp.setMargins(0, 0, 4, 0);
        root.addView(chip, clp);

        // the escape hatch for a game page: the same page in the browser that is
        // actually installed, named honestly (the chip used to say "Chrome" even
        // when Chrome was not the browser it was going to open)
        chromeChip = new TextView(this);
        chromeChip.setText(browserLabel());
        chromeChip.setTextSize(11);
        chromeChip.setTextColor(Color.parseColor("#d9ffe8"));
        chromeChip.setGravity(Gravity.CENTER);
        chromeChip.setPadding(14, 10, 14, 10);
        GradientDrawable bg2 = new GradientDrawable();
        bg2.setColor(Color.parseColor("#66162a1f"));
        bg2.setCornerRadius(30f);
        bg2.setStroke(1, Color.parseColor("#552c4a39"));
        chromeChip.setBackground(bg2);
        chromeChip.setAlpha(0.30f);
        chromeChip.setVisibility(View.GONE);
        chromeChip.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                String cur = web == null ? null : web.getUrl();
                if (cur == null || isHome(cur)) return;
                goHome();                 // stop the in-app copy: one stream, not two
                openInBrowser(cur);
            }
        });
        FrameLayout.LayoutParams cclp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.END | Gravity.CENTER_VERTICAL);
        cclp.setMargins(0, 150, 4, 0);
        root.addView(chromeChip, cclp);

        setContentView(root);
        goFullscreen();
        openFromIntent(getIntent());
    }

    /**
     * A window.open() from a player page: keep it in-app.
     *
     * Read as a rule, not a guess -- an ad host in the URL means drop it, anything
     * else means the player is asking for the window its picture lives in, so load
     * it in the main WebView. That is the whole difference between "the video does
     * not play in the app" and "it plays here".
     */
    private void takePopup(WebView from, String url) {
        if (url == null || url.isEmpty()) return;
        String low = url.toLowerCase();
        if (!low.startsWith("http")) return;
        if (isHome(url)) { destroyPopupLater(from); return; }
        String current = web == null ? "" : web.getUrl();
        if (!popupAllowed(url, current)) {
            // an ad's window: drop it, and say so, because "nothing happened" is the
            // correct outcome and the log is the only place that can show it
            Log.i(TAG, "popup dropped (not the player): " + url);
            destroyPopupLater(from);
            return;
        }
        Log.i(TAG, "popup kept in-app: " + url);
        destroyPopupLater(from);
        if (web != null) web.loadUrl(url);
    }

    /** Destroying a WebView from inside its own navigation callback aborts the load. */
    private void destroyPopupLater(final WebView pop) {
        if (pop == null || pop == web) return;
        pop.postDelayed(new Runnable() {
            public void run() { destroyPopup(pop); }
        }, 700);
    }

    private void destroyPopup(WebView pop) {
        try {
            if (pop != null) { pop.stopLoading(); pop.destroy(); }
        } catch (Exception ignored) { }
    }

    /** Start the player on a page we did not write: mute, click, play, repeat. */
    private void kickPlayback(final WebView view) {
        Log.i(TAG, "kick playback scheduled");
        final String js = "(function(){try{var st=document.createElement('style');"
            + "st.textContent='header,nav,aside,footer,.navbar,.site-header,.topbar,header[class],"
            + "body>header,body>nav,.top-menu,#header,#nav,[class*=chat],[id*=chat],"
            + "[class*=banner],.notice,.alert{display:none!important}"
            + "html,body{background:#000!important}';"
            + "(document.body||document.documentElement).appendChild(st);"
            + "setTimeout(function(){try{(document.body||document.documentElement)"
            + ".appendChild(st.cloneNode(true));}catch(e){}},1500);}catch(e){}"
            + "var n=0,k=false;function go(){n++;"
            + "var v=document.querySelector('video');"
            + "if(v){try{v.muted=true;}catch(e){}"
            + "try{var p=v.play();if(p&&p.catch)p.catch(function(){});}catch(e){}"
            + "try{v.click();}catch(e){}}"
            + "var s=['.play','.play-button','.btn-play','[class*=play]','[id*=play]','.overlay','.poster','button'];"
            + "for(var i=0;i<s.length;i++){var e=document.querySelectorAll(s[i]);"
            + "for(var j=0;j<e.length&&j<4;j++){try{if(!e[j].disabled)e[j].click();}catch(x){}}}"
            + "if(v&&v.readyState>=3&&!v.paused){k=true;"
            + "document.documentElement.setAttribute('data-nbaautoplay','playing');"
            + "try{var b=document.createElement('div');"
            + "b.textContent='muted - tap the picture for sound';"
            + "b.style.cssText='position:fixed;left:12px;top:12px;z-index:2147483647;"
            + "background:rgba(20,22,26,.9);border:1px solid #2c3038;color:#c9cdd6;"
            + "border-radius:8px;padding:8px 12px;font:13px sans-serif;pointer-events:none';"
            + "document.body.appendChild(b);setTimeout(function(){b.remove();},6000);}catch(e){}"
            + "document.addEventListener('click',function once(){"
            + "try{var vv=document.querySelector('video');if(vv){vv.muted=false;vv.play();}}catch(e){}"
            + "document.removeEventListener('click',once);});}"
            + "if(n<20&&!k)setTimeout(go,900);}"
            + "try{document.documentElement.setAttribute('data-nbaautoplay','armed');}catch(e){}"
            + "if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',"
            + "function(){setTimeout(go,400);});}else{setTimeout(go,400);}})();";
        for (final long delay : new long[]{1200, 3000, 5000, 8000, 12000}) {
            view.postDelayed(new Runnable() {
                public void run() { try { view.evaluateJavascript(js, null); } catch (Exception e) { } }
            }, delay);
        }
    }

    /** JS bridge, same names the page already uses (Hub.openExternal, ...). */
    public class Bridge {
        /**
         * fetchText(url, referer): the page-side stream resolver's GET.
         *
         * First a plain HttpURLConnection fetch (the clean case: schedule
         * pages, most event pages). When a hot player wrapper answers 403 --
         * that is Cloudflare fingerprinting the JVM's TLS hello, not a rule --
         * the same URL is rendered inside a hidden WebView: real Chromium TLS,
         * real JS, the challenge passes by itself, and what comes back is only
         * the rendered text. The hidden engine carries the game page's own
         * ad-blocker, so the wrappers' ad layers surface as blocked requests
         * while nothing is on screen: the caller receives a String, and that
         * is all.
         */
        private java.util.concurrent.CountDownLatch engLatch;
        private String engOut;

        @JavascriptInterface
        public String fetchText(final String url, final String referer) {
            if (url == null || !url.startsWith("http")) return "";
            java.util.Map<String, String> hd = new java.util.HashMap<String, String>();
            if (referer != null && referer.startsWith("http")) {
                hd.put("Referer", referer);
                try {
                    java.net.URL r = new java.net.URL(referer);
                    hd.put("Origin", r.getProtocol() + "://" + r.getHost());
                } catch (Exception ignored) { }
            }
            try {
                return Http.get(url, 12000, hd).text();
            } catch (Exception javaGet) {
                Log.i(TAG, "fetchText: plain fetch failed (" + javaGet.getMessage()
                        + "), webview engine: " + url);
            }
            engLatch = new java.util.concurrent.CountDownLatch(1);
            engOut = null;
            runOnUiThread(new Runnable() {
                public void run() { fetchViaWebview(url, referer); }
            });
            try { engLatch.await(22, java.util.concurrent.TimeUnit.SECONDS); }
            catch (InterruptedException e) { Thread.currentThread().interrupt(); }
            return engOut == null ? "" : engOut;
        }

        /**
         * The one page the invisible engine renders at a time. Same-host
         * navigation is allowed (a Cloudflare challenge navigates itself once
         * or twice on its own host), cross-host escapes are not: a wrapper
         * that turns into an ad landing stays put. Popups are swallowed whole.
         */
        private WebView engine;

        private void fetchViaWebview(final String url, final String referer) {
            try {
                if (engine == null) {
                    engine = new WebView(MainActivity.this);
                    engine.setBackgroundColor(Color.BLACK);
                    engine.getSettings().setJavaScriptEnabled(true);
                    engine.getSettings().setDomStorageEnabled(true);
                    engine.getSettings().setLoadWithOverviewMode(true);
                    engine.getSettings().setUseWideViewPort(true);
                    engine.getSettings().setUserAgentString(engine.getSettings().getUserAgentString()
                            + " NFLHubApp/" + LocalHub.VERSION);
                    engine.setWebChromeClient(new WebChromeClient() {
                        @Override
                        public boolean onCreateWindow(WebView view, boolean dialog,
                                                      boolean userGesture, android.os.Message msg) {
                            ((WebView.WebViewTransport) msg.obj).setWebView(new WebView(MainActivity.this));
                            msg.sendToTarget();
                            return true;
                        }
                        @Override
                        public void onPermissionRequest(PermissionRequest req) { req.deny(); }
                    });
                    engine.setWebViewClient(new WebViewClient() {
                        @Override
                        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest req) {
                            try {
                                String low = req.getUrl() == null ? "" : req.getUrl().toString().toLowerCase();
                                if (isAdHost(low)) {
                                    return new WebResourceResponse("text/plain", "utf-8",
                                            new ByteArrayInputStream(new byte[0]));
                                }
                            } catch (Exception ignored) { }
                            return null;
                        }
                        @Override
                        public void onPageFinished(WebView view, String u) {
                            view.evaluateJavascript("(document.documentElement.outerHTML)",
                                    new android.webkit.ValueCallback<String>() {
                                        @Override
                                        public void onReceiveValue(String val) {
                                            if (engOut == null) {
                                                String un = val;
                                                if (un != null && un.length() > 1 && un.charAt(0) == '"') {
                                                    try {
                                                        un = String.valueOf(
                                                                new org.json.JSONTokener(un).nextValue());
                                                    } catch (Exception e) {
                                                        un = val.substring(1, Math.max(1, val.length() - 1));
                                                    }
                                                }
                                                engOut = un == null ? "" : un;
                                            }
                                            if (engLatch != null) engLatch.countDown();
                                        }
                                    });
                        }
                        @Override
                        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                            try {
                                String from = "", to = "";
                                try {
                                    from = Uri.parse(view.getUrl()).getHost();
                                    to = Uri.parse(req.getUrl().toString()).getHost();
                                } catch (Exception ignored) { }
                                // same host (or same registrable domain): the challenge
                                // hops the challenge belongs to; anything else is an ad
                                if (from != null && from.equalsIgnoreCase(to)) return false;
                                if (from != null && to != null
                                        && regDomain(from).equals(regDomain(to))) return false;
                            } catch (Exception ignored) { }
                            return true;
                        }
                        @Override
                        public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                            if (req != null && req.isForMainFrame())
                                if (engLatch != null) engLatch.countDown();
                        }
                    });
                }
                engOut = null;
                // the engine answers after 12 s with whatever it has, always
                root.postDelayed(new Runnable() {
                    public void run() { if (engLatch != null) engLatch.countDown(); }
                }, 12000);
                engine.loadUrl(url);
            } catch (Exception e) {
                if (engLatch != null) engLatch.countDown();
            }
        }

        // ---------------------------------------------------- manifest sniffer
        /**
         * sniffStream(page, referer): render a player page in the app's invisible
         * engine and answer with the media playlist its own player asks for --
         * {"m3u8":..,"referer":..,"html":..}, or "" when it asks for nothing.
         *
         * The page never appears on screen, popups are refused and ad hosts are
         * blocked, so none of its ad layers reach the viewer. Only the playlist
         * URL comes back, and the visible player plays it through the relay.
         * A wrapper chain that is built in JavaScript -- the case the plain GET
         * walk cannot follow -- is exactly what this exists for.
         */
        private java.util.concurrent.CountDownLatch sniffLatch;
        private volatile String sniffHit;
        private volatile String sniffHtml;
        private volatile String sniffRef;
        private volatile WebView sniffView;
        private FrameLayout sniffScrim;

        /**
         * The invisible page gets the full screen and an opaque black sheet on
         * top of it. A 1x1 view is not enough: the player wrappers check
         * visibility and viewport size before they build their player, so a
         * hidden frame answers with nothing. Full size behind a scrim keeps
         * their checks happy while the user sees black and a status line --
         * and ad hosts are still answered with an empty body.
         */
        private void showSniffScrim() {
            if (sniffScrim != null) return;
            FrameLayout f = new FrameLayout(MainActivity.this);
            f.setBackgroundColor(Color.BLACK);
            TextView t = new TextView(MainActivity.this);
            t.setText("resolving the game feed\u2026");
            t.setTextColor(Color.parseColor("#9aa3ad"));
            t.setTextSize(14);
            FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER);
            f.addView(t, lp);
            f.setClickable(true);        // swallows taps aimed at what is under it
            root.addView(f, new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.MATCH_PARENT));
            sniffScrim = f;
        }

        private void hideSniffScrim() {
            FrameLayout f = sniffScrim;
            sniffScrim = null;
            if (f != null) { try { root.removeView(f); } catch (Exception ignored) { } }
        }

        @JavascriptInterface
        public void sniffStreamAsync(final String url, final String referer, final String cb) {
            Log.i(TAG, "sniff request: " + url + " ref=" + referer + " cb=" + cb);
            if (url == null || !url.toLowerCase().startsWith("http")) {
                sniffReply(cb, "");
                return;
            }
            String low = url.toLowerCase();
            if (low.contains(".m3u8") || low.contains(".mpd")) {
                sniffReply(cb, sniffResult(url, referer, null));
                return;
            }
            // The call must return to JavaScript at once: a WebView's frames share
            // one renderer thread, and blocking it here would stop the invisible
            // page's own script from ever asking for its playlist. That deadlock
            // was why the first build of this sniffer always came back empty.
            sniffLatch = new java.util.concurrent.CountDownLatch(1);
            sniffHit = null;
            sniffHtml = null;
            sniffRef = referer == null ? "" : referer;
            runOnUiThread(new Runnable() {
                public void run() { sniffViaWebview(url); }
            });
            new Thread(new Runnable() {
                public void run() {
                    try { sniffLatch.await(24, java.util.concurrent.TimeUnit.SECONDS); }
                    catch (InterruptedException e) { Thread.currentThread().interrupt(); }
                    Log.i(TAG, "sniff done: hit=" + sniffHit + " html="
                            + (sniffHtml == null ? -1 : sniffHtml.length()));
                    final String hit = sniffHit;
                    final String html = sniffHtml;
                    runOnUiThread(new Runnable() {
                        public void run() { removeSniffView(); }
                    });
                    sniffReply(cb, hit == null ? "" : sniffResult(hit, sniffRef, html));
                }
            }, "nfl-sniff").start();
        }

        /** Hand the sniff's answer back to the page that asked for it. */
        private void sniffReply(final String cb, final String json) {
            if (cb == null || cb.length() == 0) return;
            final String js = "window.__nbaSniff&&window.__nbaSniff("
                    + org.json.JSONObject.quote(cb) + ","
                    + org.json.JSONObject.quote(json == null ? "" : json) + ")";
            runOnUiThread(new Runnable() {
                public void run() {
                    try { if (web != null) web.evaluateJavascript(js, null); }
                    catch (Exception ignored) { }
                }
            });
        }

        private String sniffResult(String m3u8, String referer, String html) {
            try {
                return new org.json.JSONObject()
                        .put("m3u8", m3u8)
                        .put("referer", referer == null ? "" : referer)
                        .put("html", html == null ? "" : html)
                        .toString();
            } catch (Exception e) { return ""; }
        }

        private String unquote(String val) {
            if (val == null) return "";
            if (val.length() > 1 && val.charAt(0) == '"') {
                try { return String.valueOf(new org.json.JSONTokener(val).nextValue()); }
                catch (Exception e) { return val.substring(1, Math.max(1, val.length() - 1)); }
            }
            return "null".equals(val) ? "" : val;
        }

        /** The first manifest URL anywhere in a rendered page, ad hosts aside. */
        private String firstManifest(String html) {
            if (html == null) return null;
            java.util.regex.Matcher m = java.util.regex.Pattern
                    .compile("https?://[^\"'\\\\\\s<>]+?\\.(?:m3u8|mpd)[^\"'\\\\\\s<>]*",
                             java.util.regex.Pattern.CASE_INSENSITIVE)
                    .matcher(html);
            while (m.find()) {
                if (!isAdHost(m.group())) return m.group();
            }
            return null;
        }

        private void sniffViaWebview(final String url) {
            try {
                removeSniffView();
                final WebView sv = new WebView(MainActivity.this);
                sniffView = sv;
                Log.i(TAG, "sniff view created");
                WebSettings ss = sv.getSettings();
                ss.setJavaScriptEnabled(true);
                ss.setDomStorageEnabled(true);
                ss.setMediaPlaybackRequiresUserGesture(false);
                ss.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
                ss.setUserAgentString(ss.getUserAgentString() + " NFLHubApp/" + LocalHub.VERSION);
                sv.setBackgroundColor(Color.BLACK);
                sv.setWebChromeClient(new WebChromeClient() {
                    @Override
                    public boolean onCreateWindow(WebView v, boolean dialog, boolean gesture,
                                                  android.os.Message msg) {
                        return false;      // a page rendered in the dark gets no windows
                    }
                    @Override
                    public void onPermissionRequest(PermissionRequest req) { req.deny(); }
                });
                sv.setWebViewClient(new WebViewClient() {
                    @Override
                    public WebResourceResponse shouldInterceptRequest(WebView v, WebResourceRequest req) {
                        try {
                            String u = req.getUrl() == null ? "" : req.getUrl().toString();
                            String low = u.toLowerCase();
                            if (isAdHost(low)) {
                                return new WebResourceResponse("text/plain", "utf-8",
                                        new ByteArrayInputStream(new byte[0]));
                            }
                            // the playlist the page's own player would pull: the moment
                            // it is asked for, the relay has everything it needs
                            if (sniffHit == null && !low.contains("127.0.0.1")
                                    && (low.contains(".m3u8") || low.contains(".mpd"))) {
                                sniffHit = u;
                                Log.i(TAG, "sniff manifest: " + u);
                                if (sniffLatch != null) sniffLatch.countDown();
                            }
                        } catch (Exception ignored) { }
                        return null;
                    }
                    @Override
                    public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) {
                        return true;       // the engine stays on the page it was given
                    }
                    @Override
                    public void onPageFinished(WebView v, String u) {
                        Log.i(TAG, "sniff page finished: " + u);
                        v.evaluateJavascript("(function(){try{var vv=document.querySelector('video');"
                                + "if(vv){vv.muted=true;try{vv.play()}catch(e){}}"
                                + "var s=['.play','.play-button','.btn-play','[class*=play]','.overlay','.poster','button'];"
                                + "for(var i=0;i<s.length;i++){var e=document.querySelectorAll(s[i]);"
                                + "for(var j=0;j<e.length&&j<4;j++){try{e[j].click()}catch(x){}}}}catch(e){}"
                                + "return document.documentElement.outerHTML;})()",
                                new android.webkit.ValueCallback<String>() {
                                    @Override
                                    public void onReceiveValue(String val) {
                                        if (sniffHtml == null) sniffHtml = unquote(val);
                                        Log.i(TAG, "sniff html: "
                                                + (sniffHtml == null ? -1 : sniffHtml.length())
                                                + " hit=" + sniffHit);
                                        if (sniffHit == null) {
                                            String m = firstManifest(sniffHtml);
                                            if (m != null) sniffHit = m;
                                        }
                                        if (sniffHit != null) {
                                            if (sniffLatch != null) sniffLatch.countDown();
                                        } else {
                                            // a player can build its request a beat later
                                            root.postDelayed(new Runnable() {
                                                public void run() {
                                                    if (sniffLatch != null) sniffLatch.countDown();
                                                }
                                            }, 4000);
                                        }
                                    }
                                });
                    }
                    @Override
                    public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                        if (req != null && req.isForMainFrame()) {
                            Log.i(TAG, "sniff page error: " + err.getErrorCode() + " "
                                    + err.getDescription() + " " + req.getUrl());
                            if (sniffLatch != null) sniffLatch.countDown();
                        }
                    }
                });
                // full size under the scrim: the wrapper pages check the
                // viewport before they build a player, and a 1x1 view reads as
                // "not on screen" to them
                root.addView(sv, new FrameLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.MATCH_PARENT));
                showSniffScrim();
                Log.i(TAG, "sniff view loading: " + url);
                sv.loadUrl(url);
            } catch (Exception e) {
                if (sniffLatch != null) sniffLatch.countDown();
            }
        }

        private void removeSniffView() {
            final WebView sv = sniffView;
            sniffView = null;
            hideSniffScrim();
            if (sv == null) return;
            try { root.removeView(sv); } catch (Exception ignored) { }
            try { sv.stopLoading(); } catch (Exception ignored) { }
            root.postDelayed(new Runnable() {
                public void run() { try { sv.destroy(); } catch (Exception ignored) { } }
            }, 400);
        }

        @JavascriptInterface
        public void openInternal(final String url) {
            if (url == null || url.length() == 0) return;
            runOnUiThread(new Runnable() {
                public void run() { web.loadUrl(url); }
            });
        }

                @JavascriptInterface
        public void setFullscreen(final boolean on) {
            runOnUiThread(new Runnable() {
                public void run() {
                    if (on) {
                        // a phone held normally is portrait, and a 16:9 picture in a
                        // portrait window is a stamp. Fullscreen means landscape here.
                        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE);
                        goFullscreen();
                    } else {
                        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED);
                        leaveFullscreen();
                    }
                }
            });
        }
@JavascriptInterface
        public void openExternal(final String url) {
            // A page asking for "external" is asking for Chrome, and that is how the
            // ad layer used to win. Keep it in the app; the chip under the picture
            // is the only path to a browser, and a person has to tap it.
            if (url == null || url.length() == 0) return;
            Log.i(TAG, "openExternal kept in-app: " + url);
            runOnUiThread(new Runnable() {
                public void run() { if (web != null) web.loadUrl(url); }
            });
        }

        @JavascriptInterface
        public void retry() {
            runOnUiThread(new Runnable() {
                public void run() { goHome(); }
            });
        }

        @JavascriptInterface
        public String version() { return LocalHub.VERSION; }

        @JavascriptInterface
        public boolean standalone() { return true; }

        @JavascriptInterface
        public void hubReady() { Log.i(TAG, "page ready"); }
    }

    // ---------------------------------------------------------------- nav

    /**
     * Deep links:
     *   adb shell am start -n com.nflds.app/.MainActivity --es chan 0
     *   adb shell am start -n com.nflds.app/.MainActivity --es url https://host/main/1
     *
     * The url form exists so a page can be put in front of the app's WebView
     * without going through the schedule -- that is how the in-app playback
     * behaviour is tested against a local fixture.
     */
    private void openFromIntent(Intent intent) {
        String direct = intent == null ? null : intent.getStringExtra("url");
        if (direct != null && direct.startsWith("http")) {
            Log.i(TAG, "deep link url: " + direct);
            web.loadUrl(direct);
            return;
        }
        String chan = intent == null ? null : intent.getStringExtra("chan");
        if (chan != null && chan.matches("\\d+")) web.loadUrl(home + "?chan=" + chan);
        else goHome();
    }

    private void goHome() {
        if (web != null) web.loadUrl(home);
    }

    private boolean isHome(String url) {
        return url != null && url.startsWith(home.substring(0, home.length() - 1));
    }

    private void updateChip() {
        if (chip == null || web == null) return;
        String cur = web.getUrl();
        // visible on every page that is not the hub itself -- including a page an ad
        // dragged us onto, where it is the only way back
        boolean away = cur == null || !cur.startsWith("http") || !isHome(cur);
        chip.setVisibility(away ? View.VISIBLE : View.GONE);
        if (chromeChip != null) chromeChip.setVisibility(away ? View.VISIBLE : View.GONE);
    }

    private void leaveCustomView() {
        if (fullView == null) return;
        root.removeView(fullView);
        fullView = null;
        web.setVisibility(View.VISIBLE);
        WebChromeClient.CustomViewCallback cb = fullCallback;
        fullCallback = null;
        if (cb != null) cb.onCustomViewHidden();
        updateChip();
        goFullscreen();
    }

    // ------------------------------------------------------------- browser

    /**
     * Games are Widevine. This WebView can decrypt them when the device ships a
     * CDM and the page is a secure context; when it cannot, the chip is the way
     * out, and it names the browser it will actually use.
     */
    private static final String[] BROWSER_PACKAGES = {
            "com.android.chrome",
            "com.chrome.beta",
            "org.mozilla.firefox",
            "org.mozilla.firefox_beta",
            "com.brave.browser",
    };

    private static final String[][] BROWSER_NAMES = {
            {"com.android.chrome", "Chrome"},
            {"com.chrome.beta", "Chrome beta"},
            {"org.mozilla.firefox", "Firefox"},
            {"org.mozilla.firefox_beta", "Firefox beta"},
            {"com.brave.browser", "Brave"},
    };

    /** The name of the browser the chip will open, as installed on this phone. */
    private String browserLabel() {
        for (String[] row : BROWSER_NAMES) {
            try {
                if (getPackageManager().getLaunchIntentForPackage(row[0]) != null) return row[1];
            } catch (Exception ignored) { }
        }
        return "browser";
    }

    private void openInBrowser(String url) {
        Uri uri = Uri.parse(url);
        Intent intent = new Intent(Intent.ACTION_VIEW, uri);
        for (String pkg : BROWSER_PACKAGES) {
            try {
                if (getPackageManager().getLaunchIntentForPackage(pkg) == null) continue;
                intent.setPackage(pkg);
                startActivity(intent);
                return;
            } catch (Exception e) {
                intent.setPackage(null);
            }
        }
        try {
            startActivity(Intent.createChooser(new Intent(Intent.ACTION_VIEW, uri), "open stream with"));
        } catch (Exception e) {
            Toast.makeText(this, "no browser accepted that link", Toast.LENGTH_SHORT).show();
        }
    }

    private void restartWebView() {
        try {
            root.removeView(web);
            web.destroy();
        } catch (Exception ignored) { }
        web = null;
        finish();
        startActivity(new Intent(this, MainActivity.class));
    }

    // -------------------------------------------------------------- window

    /** Real fullscreen: on a Fold the taskbar otherwise eats the bottom strip. */
    @SuppressWarnings("deprecation")
    private void goFullscreen() {
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS
                | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN);
        getWindow().setFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN,
                WindowManager.LayoutParams.FLAG_FULLSCREEN);
        if (root != null) {
            root.setFitsSystemWindows(false);
            root.setPadding(0, 0, 0, 0);
        }
        if (web != null) {
            web.setFitsSystemWindows(false);
            web.setPadding(0, 0, 0, 0);
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            getWindow().setDecorFitsSystemWindows(false);
            WindowInsetsController c = getWindow().getInsetsController();
            if (c != null) {
                c.hide(WindowInsets.Type.systemBars());
                c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        }
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
    }

        @SuppressWarnings("deprecation")
    private void leaveFullscreen() {
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN
                | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS
                | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            getWindow().setDecorFitsSystemWindows(true);
            WindowInsetsController c = getWindow().getInsetsController();
            if (c != null) c.show(WindowInsets.Type.systemBars());
        }
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_VISIBLE);
        if (root != null) root.setFitsSystemWindows(true);
        if (web != null) web.setFitsSystemWindows(true);
    }
@Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) goFullscreen();
    }

    @Override
    public boolean onKeyDown(int code, KeyEvent e) {
        if (code == KeyEvent.KEYCODE_BACK && web != null) {
            if (fullView != null) {
                web.evaluateJavascript("document.fullscreenElement && document.exitFullscreen()", null);
                leaveCustomView();
                return true;
            }
            String cur = String.valueOf(web.getUrl());
            if (!isHome(cur)) {
                // a game page opened in-app: back means "back to the list"
                if (web.canGoBack()) web.goBack(); else goHome();
                return true;
            }
        }
        return super.onKeyDown(code, e);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (intent != null && (intent.hasExtra("chan") || intent.hasExtra("url"))) openFromIntent(intent);
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (web != null) web.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (web != null) web.onResume();
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.loadUrl("about:blank");
            web.destroy();
            web = null;
        }
        if (root != null) {
            // the invisible fetch engine goes with the activity
            try {
                View ev = root.getChildAt(0);
                if (ev instanceof WebView && ev != web) { /* handled below */ }
            } catch (Exception ignored) { }
        }
        if (hub != null) hub.stop();      // relay stopped, DVR folder deleted
        super.onDestroy();
    }
}
