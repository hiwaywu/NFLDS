(function(){
  if (String(location.href).indexOf('127.0.0.1:8795') < 0) return 'WRONG-TARGET';
  var vv = document.querySelector('video') || {};
  var g = function(id){ var e = document.getElementById(id); return e ? e.textContent : ''; };
  return JSON.stringify({vw:vv.videoWidth||0, vh:vv.videoHeight||0, rs:vv.readyState,
    q:g('pillQ'), lat:g('pillLat'), src:g('pillSrc'),
    last:(typeof lastChannel!=='undefined'&&lastChannel?String(lastChannel.url).slice(0,60):'')});
})()
