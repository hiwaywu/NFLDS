// Runs at document_start in every frame of the stream/player pages.
// Two jobs: kill popups, and hide everything that is not the video.
(function () {
  window.__nbaClean = true;

  // --- 1. popups: the player's ad layer calls window.open on click ----------
  try {
    var noop = function () { return null; };
    window.open = noop;
    if (window.top !== window.self) {
      // also stop this frame from navigating the whole window away
      window.open = noop;
    }
  } catch (e) {}

  // block clicks that only exist to launch ads
  document.addEventListener(
    "click",
    function (ev) {
      try {
        var a = ev.target && ev.target.closest ? ev.target.closest("a[href]") : null;
        if (!a) return;
        var h = a.href || "";
        if (/^javascript:/i.test(h) || a.target === "_blank") {
          ev.preventDefault();
          ev.stopPropagation();
        }
      } catch (e) {}
    },
    true
  );

  // --- 2. cosmetics ---------------------------------------------------------
  function clean() {
    document.querySelectorAll("div, p, section, span").forEach(function (el) {
      try {
        var t = (el.textContent || "").trim().toUpperCase();
        if (t.length > 12 && t.length < 220 &&
            (t.indexOf("BLACK SCREEN") === 0 ||
             (t.indexOf("CLEAR CACHE") !== -1 && t.indexOf("VPN") !== -1) ||
             t.indexOf("INSTALL ") === 0 && t.indexOf("EXTENSION") !== -1)) {
          el.style.setProperty("display", "none", "important");
        }
      } catch (e) {}
    });
    document.documentElement.style.overflowX = "hidden";
    if (document.body) document.body.style.background = "#000";
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", clean);
  } else {
    clean();
  }
  setTimeout(clean, 1200);
  setTimeout(clean, 4000);
})();
