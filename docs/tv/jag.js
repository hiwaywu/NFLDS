(async function(){
  const g = games.filter(function(x){ return /Jaguars/.test(x.name); })[0];
  if (!g) return 'no jaguars game; live: ' + games.filter(function(x){return x.state==='in';}).map(function(x){return x.name;}).join(' | ');
  if (g.state !== 'in') return 'jaguars not live: ' + g.state;
  watchGame(g);
  return 'started ' + g.name;
})()
