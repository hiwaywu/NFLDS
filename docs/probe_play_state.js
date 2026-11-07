(async () => {
  // sample twice so "playing" means the clock advanced, not just buffered
  const snap = () => ({
    url: location.href,
    t: v.currentTime,
    rs: v.readyState,
    w: v.videoWidth, h: v.videoHeight,
    paused: v.paused, muted: v.muted,
    err: (window.hls && hls.media && hls.media.error) ? hls.media.error.code : null,
    levels: (window.hls && hls.levels && hls.levels.length) || 0,
    title: (document.getElementById('nowTitle') || {}).textContent,
    pillQ: (document.getElementById('pillQ') || {}).textContent,
    pillBuf: (document.getElementById('pillBuf') || {}).textContent,
    buffered: v.buffered && v.buffered.length ? Math.round(v.buffered.end(v.buffered.length - 1) - v.currentTime) : null
  });
  const a = snap();
  await new Promise(r => setTimeout(r, 3000));
  const b = snap();
  return {
    url: b.url,
    title: b.title,
    resolution: (b.w || 0) + 'x' + (b.h || 0),
    readyState: b.rs,
    t1: Math.round(a.t * 10) / 10,
    t2: Math.round(b.t * 10) / 10,
    advancing: b.t > a.t + 0.5,
    paused: b.paused,
    muted: b.muted,
    bufferedAheadSec: b.buffered,
    levels: b.levels,
    mediaError: b.err,
    pillQ: b.pillQ,
    pillBuf: b.pillBuf
  };
})()
