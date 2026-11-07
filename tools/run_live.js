(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  // the live game lives on yesterday's slate in this view: ask the hub for it
  let g = null;
  try {
    const j = await fetch('/api/games?date=2026-11-06&_=' + Date.now()).then(r => r.json());
    g = (j.games || []).find(x => x.state === 'in' && /Lakers/.test(x.name));
    if (!g) g = (j.games || []).find(x => x.state === 'in');
  } catch(e){}
  if (!g) return JSON.stringify({err: 'no live game'});
  watchGame(g);
  await wait(50000);
  const v2 = document.getElementById('v');
  return JSON.stringify({
    game: g.name,
    title: (document.getElementById('nowTitle')||{}).textContent || '',
    src: (document.getElementById('pillSrc')||{}).textContent || '',
    w: v2 ? v2.videoWidth : 0, h: v2 ? v2.videoHeight : 0,
    t: v2 ? +(v2.currentTime||0).toFixed(1) : 0,
    ready: v2 ? v2.readyState : 0,
    idle: ((document.getElementById('idle')||{}).innerText || '').slice(0, 80)
  });
})()
