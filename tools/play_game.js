(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const g = (typeof games !== 'undefined') ? games.find(x => /(Chiefs|Eagles|Cowboys|Bills|49ers)/.test(x.name)) : null;
  if (!g) return JSON.stringify({err: 'games not loaded'});
  gameNow = g;
  const h = await harvestChain(g);
  if (!h || !h.ok) return JSON.stringify({step: 'harvest', res: h});
  // POST the keys so /api/clearkey is warm too
  try {
    await fetch('/api/keys', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(Object.keys(h.keys||{}).map(kid => ({kid: kid, key: (h.keys||{})[kid]}))) });
  } catch(e){}
  // try shaka DIRECTLY against the source manifest (what the provider does)
  window.__errs = [];
  try {
    if (hls){ hls.destroy(); hls = null; }
    if (shakaPlayer){ shakaPlayer.destroy(); shakaPlayer = null; }
  } catch(e){}
  const boot = () => new Promise(resolve => {
    const t0 = Date.now();
    const go = () => {
      shaka.polyfill.installAll();
      const p = new shaka.Player(document.getElementById('v'));
      p.configure({ drm: { clearKeys: h.keys || {} } });
      p.addEventListener('error', e => window.__errs.push('shaka ' + (e.detail && e.detail.message)));
      shakaPlayer = p;
      p.load(h.m3u8, 0, null, null).then(() => {
        const v = document.getElementById('v');
        v.muted = true; v.play().catch(()=>{});
        resolve('shaka loaded');
      }).catch(err => resolve('shaka load failed: ' + (err && (err.message || err.code))));
    };
    if (typeof shaka !== 'undefined') go(); else setTimeout(go, 250);
  });
  const r = await boot();
  await wait(25000);
  const v = document.getElementById('v');
  return JSON.stringify({
    via: h.via, keys: !!h.keys, result: r, errs: (window.__errs||[]).slice(0,6),
    w: v.videoWidth, h2: v.videoHeight, t: +(v.currentTime||0).toFixed(1),
    ready: v.readyState, paused: v.paused,
    buffered: v.buffered.length ? +(v.buffered.end(0)).toFixed(1) : 0
  }, null, 1);
})()

