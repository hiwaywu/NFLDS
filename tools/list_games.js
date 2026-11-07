(async () => {
  const list = (typeof games !== 'undefined') ? games : [];
  return JSON.stringify({
    curDate: (typeof curDate !== 'undefined' ? curDate : '?'),
    games: list.map(g => ({ n: g.name, s: g.state, u: (g.url || '').slice(-40), st: g.stream }))
  });
})()
