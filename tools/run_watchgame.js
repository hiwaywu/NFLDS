(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const list = (typeof games !== 'undefined') ? games : [];
  const g = list.find(x => /Nuggets/.test(x.name)) || list[0];
  if (!g) return JSON.stringify({err: 'no game'});
  watchGame(g);
  await wait(45000);
  const v2 = document.getElementById('v');
  return JSON.stringify({
    game: g.name,
    title: (document.getElementById('nowTitle')||{}).textContent || '',
    src: (document.getElementById('pillSrc')||{}).textContent || '',
    w: v2 ? v2.videoWidth : 0,
    t: v2 ? +(v2.currentTime||0).toFixed(1) : 0,
    ready: v2 ? v2.readyState : 0,
    idle: ((document.getElementById('idle')||{}).innerText || '').slice(0, 90),
    spinshown: (document.getElementById('spinner')||{}).className || ''
  });
})()
