(function(){
  try {
    return JSON.stringify({
      title: (document.getElementById("nowTitle")||{}).textContent || "",
      src: (document.getElementById("pillSrc")||{}).textContent || "",
      q: (document.getElementById("pillQ")||{}).textContent || "",
      buf: (document.getElementById("pillBuf")||{}).textContent || "",
      errs: (window.__errs||[]).slice(0,6),
      hasShaka: typeof shaka !== "undefined",
      hasHls: !!window.hls,
      hasShakaP: !!window.shakaPlayer,
      w: ((document.getElementById("v")||{}).videoWidth||0),
      t: +((document.getElementById("v")||{}).currentTime||0).toFixed(1),
      ready: ((document.getElementById("v")||{}).readyState||0),
      url: location.href
    });
  } catch(e){ return "ERR " + e.message; }
})()
