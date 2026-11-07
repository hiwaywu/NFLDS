(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  let g = null;
  try {
    const j = await fetch('/api/games?date=2026-11-06&_=' + Date.now()).then(r => r.json());
    g = (j.games || []).find(x => x.state === 'in' && /Lakers/.test(x.name)) || (j.games || []).find(x => x.state === 'in');
  } catch(e){}
  if (!g) return JSON.stringify({err: 'no live game'});
  watchGame(g);
  await wait(32000);
  const v2 = document.getElementById('v');
  const a0 = v2 ? (v2.webkitAudioDecodedByteCount || 0) : -1;
  await wait(10000);
  const v3 = document.getElementById('v');
  // what the relay says about audio
  let st = null;
  try { st = await fetch('/api/relaystats?_=' + Date.now()).then(r => r.json()); } catch(e){}
  return JSON.stringify({
    game: g.name,
    w: v3 ? v3.videoWidth : 0,
    t: v3 ? +(v3.currentTime||0).toFixed(1) : 0,
    muted: v3 ? v3.muted : null,
    audioBytesBefore: a0,
    audioBytesAfter: v3 ? (v3.webkitAudioDecodedByteCount || 0) : -1,
    relayAudioNote: st ? st.audio : '?',
    snd: (document.getElementById('snd')||{}).textContent || ''
  });
})()
