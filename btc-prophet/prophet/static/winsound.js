'use strict';
/* 2.5 win sound that browsers actually allow.
   Safari and Chrome only let a page play sound after the person has clicked or tapped it once.
   The first click or tap anywhere (or the "Enable sound" button) unlocks audio for this tab: the
   sound is downloaded and decoded once, and every later win plays it instantly. */
const WinSound = (() => {
  let ctx = null, buffer = null, unlocked = false, loading = null, current = null;
  const listeners = [];
  const notify = () => listeners.forEach(fn => { try { fn(unlocked); } catch (e) {} });
  async function load() {
    if (buffer || loading) return loading;
    loading = fetch('/api/win-sound?v=' + Date.now(), { credentials: 'same-origin' })
      .then(r => { if (!r.ok) throw Error('no sound file'); return r.arrayBuffer(); })
      .then(data => new Promise((ok, fail) => ctx.decodeAudioData(data, ok, fail)))
      .then(decoded => { buffer = decoded; })
      .catch(() => { buffer = null; })
      .finally(() => { loading = null; });
    return loading;
  }
  async function unlock() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return false;
      if (!ctx) ctx = new Ctx();
      if (ctx.state === 'suspended') await ctx.resume();
      const blip = ctx.createBuffer(1, 1, 22050), src = ctx.createBufferSource();   // silent unlock
      src.buffer = blip; src.connect(ctx.destination); src.start(0);
      await load();
      unlocked = ctx.state === 'running' && !!buffer;
    } catch (e) { unlocked = false; }
    notify();
    return unlocked;
  }
  function play(maxSeconds) {
    if (!unlocked || !ctx || !buffer) return Promise.reject(Error('locked'));
    stop();
    const src = ctx.createBufferSource();
    src.buffer = buffer; src.connect(ctx.destination); src.start(0);
    if (maxSeconds > 0) src.stop(ctx.currentTime + maxSeconds);
    current = src;
    return new Promise(done => { src.onended = () => { if (current === src) current = null; done(); }; });
  }
  function stop() { try { if (current) current.stop(); } catch (e) {} current = null; }
  for (const ev of ['pointerdown', 'keydown', 'touchstart']) {
    window.addEventListener(ev, () => { if (!unlocked) unlock(); }, { passive: true });
  }
  return { unlock, play, stop, isUnlocked: () => unlocked, onChange: fn => listeners.push(fn) };
})();
