/* Parallel Lab — UI effects (theme, guided journey, live chip-network background).
   Pure presentation: never touches data/API logic in app.js. */
'use strict';
(function () {
  const $ = (id) => document.getElementById(id);
  const root = document.documentElement;
  const reduceMotion = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------------------------------------------------------------- theme */
  function currentTheme() { return root.getAttribute('data-theme') === 'light' ? 'light' : 'dark'; }
  function applyTheme(t) {
    root.setAttribute('data-theme', t);
    try { localStorage.setItem('pbda-theme', t); } catch (e) { /* storage may be blocked */ }
    const m = document.querySelector('meta[name="theme-color"]');
    if (m) m.setAttribute('content', t === 'light' ? '#f3f7ff' : '#1c2a49');
    window.dispatchEvent(new Event('themechange'));
  }
  $('themeToggle')?.addEventListener('click', () => {
    applyTheme(currentTheme() === 'dark' ? 'light' : 'dark');
    try { if (typeof renderResult === 'function') renderResult(); } catch (e) { /* charts redraw on next refresh */ }   // repaint canvas charts in the new colours
  });
  applyTheme(currentTheme());

  /* ------------------------------------------------------- guided journey */
  const journey = {
    overview: { say: 'Start here', next: 'generator', label: 'Create a dataset →' },
    generator: { say: 'Next', next: 'analytics', label: 'Set up the benchmark →' },
    analytics: { say: 'Next', next: 'live', label: 'Watch it run →' },
    live: { say: 'Next', next: 'results', label: 'See the results →' },
    results: { say: 'Want another try?', next: 'generator', label: 'New dataset →' },
    summary: { say: 'Done reading?', next: 'overview', label: 'Back to overview →' },
  };
  function goto(page) { document.querySelector(`.nav-item[data-page="${page}"]`)?.click(); window.scrollTo({ top: 0, behavior: 'smooth' }); }
  function renderHint() {
    const active = document.querySelector('.page.active');
    const page = active ? active.id.replace('page-', '') : 'overview';
    const j = journey[page]; const el = $('journeyHint');
    if (!j || !el) return;
    el.innerHTML = `<span>${j.say}:</span> <button type="button">${j.label}</button>`;
    el.querySelector('button').addEventListener('click', () => goto(j.next));
  }
  document.querySelectorAll('.page').forEach((p) => new MutationObserver(renderHint).observe(p, { attributes: true, attributeFilter: ['class'] }));
  renderHint();
  $('brandHome')?.addEventListener('click', () => goto('overview'));
  $('howBtn')?.addEventListener('click', () => $('how')?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
  document.addEventListener('click', (e) => {            // close the mobile menu when tapping outside
    const sb = $('sidebar');
    if (sb && sb.classList.contains('open') && !e.target.closest('#sidebar') && !e.target.closest('#mobileMenu')) sb.classList.remove('open');
  });

  /* ------------------------------------------------ cursor glow + spotlight */
  const glow = $('cursorGlow');
  const mouse = { x: -9999, y: -9999, active: false };
  window.addEventListener('pointermove', (e) => {
    mouse.x = e.clientX; mouse.y = e.clientY; mouse.active = true;
    if (busy) return;
    if (glow && !reduceMotion) glow.style.transform = `translate3d(${e.clientX}px,${e.clientY}px,0)`;
    const card = e.target.closest && e.target.closest('.metric-card,.how-step');
    if (card) {
      const r = card.getBoundingClientRect();
      card.style.setProperty('--mx', `${e.clientX - r.left}px`);
      card.style.setProperty('--my', `${e.clientY - r.top}px`);
    }
  }, { passive: true });
  document.addEventListener('pointerleave', () => { mouse.active = false; });
  window.addEventListener('blur', () => { mouse.active = false; });

  /* ------------------------------------------- live chip-network background
     • Small microchips drift around the page (evenly spread, smooth inertia).
     • When two chips get close they JOIN with a chain-link line (a ring pulses at the join).
     • When they drift apart the link stretches, fades and BREAKS; each chip then links to its
       nearest neighbours — so the web of nodes keeps re-forming by itself.
     • The mouse acts as a hub: nearby chips light up, get pulled in and connect to the cursor. */
  const cv = $('bgfx'); if (!cv) return;
  const ctx = cv.getContext('2d');
  let W = 0, H = 0, dpr = 1, chips = [], links = new Map(), pulses = [], t = 0, last = 0, running = true;

  const PALETTES = {
    dark: [[150, 175, 255], [98, 205, 255], [94, 234, 203], [196, 173, 255]],
    light: [[44, 89, 245], [10, 127, 194], [11, 143, 112], [106, 59, 224]],
  };
  const pal = () => PALETTES[currentTheme()];
  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
  const mix = (a, b, k) => [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k].map(Math.round);

  /* Layout: every chip owns a "home" cell of a jittered grid, so the whole screen is always evenly covered
     (no blank areas, no clumps). A chip drifts slowly around its home on its own lazy orbit (spring + damping,
     so motion is smooth and has inertia). Nearby chips join with a link; links fade in, stretch and snap as the
     chips drift, so the network keeps re-forming. The cursor attracts nearby chips and links to them. */
  let cw = 200, chh = 200, sim = 0;
  const CONNECT = () => Math.max(cw, chh) * 1.32;               // distance at which chips join
  const BREAK = () => CONNECT() * 1.22;                          // distance at which a link snaps (hysteresis)
  const MAXDEG = 3;
  const MOUSE_R = 230;

  function layout() {
    const target = Math.round(Math.max(24, Math.min(54, (W * H) / 30000)));
    const cols = Math.max(3, Math.round(Math.sqrt(target * W / H)));
    const rows = Math.max(3, Math.round(target / cols));
    cw = W / cols; chh = H / rows;
    const old = chips; chips = []; links.clear(); pulses.length = 0;
    for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
      const ax = (c + 0.5 + (Math.random() - 0.5) * 0.45) * cw, ay = (r + 0.5 + (Math.random() - 0.5) * 0.45) * chh;
      const prev = old[chips.length];
      chips.push(makeChip(ax, ay, prev));
    }
  }
  function resize() {
    dpr = 1;   // 1:1 canvas pixels — soft background art does not need retina resolution, and it keeps CPU use low
    W = window.innerWidth; H = window.innerHeight;
    cv.width = Math.floor(W * dpr); cv.height = Math.floor(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    layout();
  }
  function makeChip(ax, ay, prev) {
    return {
      ax, ay, x: prev ? Math.min(W, Math.max(0, prev.x)) : ax, y: prev ? Math.min(H, Math.max(0, prev.y)) : ay, vx: 0, vy: 0,
      rx: cw * (0.28 + Math.random() * 0.2), ry: chh * (0.28 + Math.random() * 0.2),          // size of its lazy orbit
      f1: 0.00005 + Math.random() * 0.00007, f2: 0.00006 + Math.random() * 0.00007,           // orbit speed (period ≈ 45–120 s)
      p1: Math.random() * 6.28, p2: Math.random() * 6.28,
      s: 7 + Math.random() * 6, rot: Math.random() * Math.PI, vr: (Math.random() - .5) * 0.0025,
      c: Math.floor(Math.random() * 4), deg: 0, heat: 0, ph: Math.random() * 6.28,
    };
  }
  const key = (i, j) => (i < j ? i * 4096 + j : j * 4096 + i);

  function step(dt) {
    const n = chips.length, C = CONNECT(), B = BREAK();
    sim += dt * 16.67;
    chips.forEach((p) => { p.deg = 0; });

    // --- movement: spring toward a slowly moving orbit point, cursor attraction, soft separation
    for (let i = 0; i < n; i++) {
      const p = chips[i];
      let hot = 0, fx = 0, fy = 0;
      if (mouse.active) {
        const dx = mouse.x - p.x, dy = mouse.y - p.y, d = Math.hypot(dx, dy);
        if (d < MOUSE_R && d > 1) { hot = 1 - d / MOUSE_R; const pull = hot * hot * 0.10 + hot * 0.02; fx += dx / d * pull * (d > 46 ? 1 : d / 46); fy += dy / d * pull * (d > 46 ? 1 : d / 46); }
      }
      const tx = p.ax + Math.cos(sim * p.f1 + p.p1) * p.rx, ty = p.ay + Math.sin(sim * p.f2 + p.p2) * p.ry;
      const k = 0.0016 * (1 - hot * 0.75);                       // weaker leash while the cursor is pulling
      fx += (tx - p.x) * k; fy += (ty - p.y) * k;
      // a stiffer leash far from home, so a chip that followed the cursor always drifts back
      const hx = p.x - p.ax, hy = p.y - p.ay, hd = Math.hypot(hx, hy), maxD = Math.max(cw, chh) * 0.95;
      if (hd > maxD) { fx -= hx / hd * (hd - maxD) * 0.01; fy -= hy / hd * (hd - maxD) * 0.01; }
      for (let j = i + 1; j < n; j++) {
        const q = chips[j]; const dx = q.x - p.x, dy = q.y - p.y; const d2 = dx * dx + dy * dy; const min = 54;
        if (d2 < min * min && d2 > 0.01) { const d = Math.sqrt(d2), f = (min - d) / min * 0.02; fx -= dx / d * f; fy -= dy / d * f; q.vx += dx / d * f * dt; q.vy += dy / d * f * dt; }
      }
      p.vx += fx * dt; p.vy += fy * dt;
      const damp = Math.pow(0.955, dt); p.vx *= damp; p.vy *= damp;       // inertia: smooth starts and stops
      const sp = Math.hypot(p.vx, p.vy), cap = 0.9 + hot * 1.4;
      if (sp > cap) { p.vx *= cap / sp; p.vy *= cap / sp; }
      p.heat += (hot - p.heat) * 0.12;
      p.x += p.vx * dt; p.y += p.vy * dt; p.rot += (p.vr + p.vx * 0.0006) * dt;
    }

    // --- link formation / breaking (nearest neighbours join; far ones let go)
    const want = new Set();
    for (let i = 0; i < n; i++) {
      const p = chips[i]; const near = [];
      for (let j = 0; j < n; j++) { if (j === i) continue; const q = chips[j]; const d = Math.hypot(q.x - p.x, q.y - p.y); if (d < C) near.push([d, j]); }
      near.sort((a, b) => a[0] - b[0]);
      for (let k = 0; k < Math.min(MAXDEG, near.length); k++) want.add(key(i, near[k][1]));
    }
    links.forEach((L, k) => {
      const a = chips[L.i], b = chips[L.j];
      if (!a || !b) { links.delete(k); return; }
      const d = Math.hypot(a.x - b.x, a.y - b.y); L.d = d;
      const keep = d < B && (want.has(k) || d < C * 0.9);
      L.target = keep ? 1 : 0;
      L.s += (L.target - L.s) * (keep ? 0.07 : 0.045) * dt;
      if (!keep && L.s < 0.02) links.delete(k);
    });
    want.forEach((k) => {
      if (links.has(k)) return;
      const i = Math.floor(k / 4096), j = k % 4096, a = chips[i], b = chips[j];
      if (!a || !b) return;
      links.set(k, { i, j, s: 0, target: 1, d: 0, off: Math.random() * 20, pk: Math.random() });
      pulses.push({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, r: 4, a: 0.7, c: a.c });     // "join" ping
    });
    links.forEach((L) => { if (L.target) { chips[L.i].deg++; chips[L.j].deg++; } });
    for (let k = pulses.length - 1; k >= 0; k--) { const p = pulses[k]; p.r += 0.7 * dt; p.a -= 0.014 * dt; if (p.a <= 0) pulses.splice(k, 1); }
  }

  function drawChip(p, tt) {
    const c = pal()[p.c]; const a = 0.34 + Math.min(1, p.deg / 3) * 0.2 + p.heat * 0.45; const s = p.s * (1 + p.heat * 0.35);
    ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.rot);
    // node ring appears when the chip is joined to others
    if (p.deg > 0 || p.heat > 0.05) {
      ctx.beginPath(); ctx.arc(0, 0, s * 1.55 + Math.sin(tt * 0.002 + p.ph) * 1.2, 0, 6.2832);
      ctx.strokeStyle = rgba(c, 0.18 + p.heat * 0.5 + p.deg * 0.05); ctx.lineWidth = 1; ctx.stroke();
    }
    // pins
    ctx.strokeStyle = rgba(c, a); ctx.lineWidth = 1.2; ctx.beginPath();
    for (let k = -1; k <= 1; k++) {
      const o = k * s * 0.45;
      ctx.moveTo(o, -s * 0.5); ctx.lineTo(o, -s * 0.5 - 3.2); ctx.moveTo(o, s * 0.5); ctx.lineTo(o, s * 0.5 + 3.2);
      ctx.moveTo(-s * 0.5, o); ctx.lineTo(-s * 0.5 - 3.2, o); ctx.moveTo(s * 0.5, o); ctx.lineTo(s * 0.5 + 3.2, o);
    }
    ctx.stroke();
    // package
    const r = 2.5;
    ctx.beginPath(); ctx.roundRect ? ctx.roundRect(-s * 0.5, -s * 0.5, s, s, r) : ctx.rect(-s * 0.5, -s * 0.5, s, s);
    ctx.fillStyle = rgba(c, 0.08 + p.heat * 0.18); ctx.fill(); ctx.strokeStyle = rgba(c, a + 0.1); ctx.lineWidth = 1.3; ctx.stroke();
    // die
    const d = s * 0.42;
    ctx.fillStyle = rgba(c, a * 0.9 + (p.deg > 0 ? 0.1 : 0)); ctx.fillRect(-d / 2, -d / 2, d, d);
    ctx.restore();
  }

  function drawLink(L, tt) {
    const a = chips[L.i], b = chips[L.j]; if (!a || !b) return;
    const c = mix(pal()[a.c], pal()[b.c], 0.5);
    const stretch = Math.max(0, (L.d - CONNECT() * 0.6) / (BREAK() - CONNECT() * 0.6));   // 0 close … 1 about to snap
    const alpha = L.s * (0.5 - stretch * 0.28) + Math.max(a.heat, b.heat) * 0.25 * L.s;
    if (alpha < 0.02) return;
    // chain: dashed line whose dashes flow along the link
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
    ctx.strokeStyle = rgba(c, alpha); ctx.lineWidth = 1.3 - stretch * 0.5; ctx.stroke();
    // data packet travelling along an established link
    if (L.s > 0.6 && L.i % 2 === 0) {
      const u = (L.pk + tt * 0.00012 * (1 + (L.i % 3))) % 1;
      const px = a.x + (b.x - a.x) * u, py = a.y + (b.y - a.y) * u;
      ctx.beginPath(); ctx.arc(px, py, 2, 0, 6.2832); ctx.fillStyle = rgba(c, Math.min(1, alpha * 2)); ctx.fill();
    }
  }

  function drawMouse(tt) {
    if (!mouse.active) return;
    const base = [...pal()[1]];
    const near = [];
    chips.forEach((p) => { const d = Math.hypot(p.x - mouse.x, p.y - mouse.y); if (d < MOUSE_R) near.push([d, p]); });
    near.sort((x, y) => x[0] - y[0]);
    near.slice(0, 6).forEach(([d, p], idx) => {
      const k = 1 - d / MOUSE_R;
      ctx.beginPath(); ctx.moveTo(mouse.x, mouse.y); ctx.lineTo(p.x, p.y);
      ctx.strokeStyle = rgba(pal()[p.c], 0.25 + k * 0.6); ctx.lineWidth = 1 + k * 1.4;
      ctx.setLineDash([6, 4]); ctx.lineDashOffset = -tt * 0.04; ctx.stroke(); ctx.setLineDash([]);
    });
    // hub marker
    ctx.beginPath(); ctx.arc(mouse.x, mouse.y, 9 + Math.sin(tt * 0.006) * 1.5, 0, 6.2832);
    ctx.strokeStyle = rgba(base, 0.75); ctx.lineWidth = 1.5; ctx.stroke();
    ctx.beginPath(); ctx.arc(mouse.x, mouse.y, 3, 0, 6.2832); ctx.fillStyle = rgba(base, 0.95); ctx.fill();
  }

  function frame(now) {
    if (!running) return;
    if (now - last < 40) { requestAnimationFrame(frame); return; }          // cap at ~30 fps to stay light
    const dt = Math.min(2.2, (now - last) / 16.67 || 1); last = now; t = now;
    step(dt);
    ctx.clearRect(0, 0, W, H);
    links.forEach((L) => drawLink(L, t));
    pulses.forEach((p) => { ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, 6.2832); ctx.strokeStyle = rgba(pal()[p.c], p.a); ctx.lineWidth = 1.5; ctx.stroke(); });
    chips.forEach((p) => drawChip(p, t));
    drawMouse(t);
    requestAnimationFrame(frame);
  }

  /* Busy mode: stop ALL animation while a benchmark or dataset generation is running so the browser
     never steals CPU from the compute threads (this is what keeps parallel-vs-sequential timings honest). */
  let busy = false;
  function isBusy() {
    try {
      if (typeof state === 'undefined') return false;
      return !!((state.benchmarkId && !state.benchmarkTerminal) || state.benchmarkStarting ||
                (state.generationId && !state.generationTerminal) || state.generationStarting ||
                $('liveNavPulse')?.classList.contains('show'));
    } catch (e) { return false; }
  }
  function syncBusy() {
    const b = isBusy();
    if (b === busy) return;
    busy = b; document.body.classList.toggle('is-busy', b);
    if (b) { running = false; }                                   // loop exits on its next frame; last frame stays on screen
    else if (!reduceMotion && !document.hidden && !running) { running = true; last = performance.now(); requestAnimationFrame(frame); }
  }
  setInterval(syncBusy, 250);

  function start() {
    resize();
    if (reduceMotion) {            // one calm still frame, no animation loop
      for (let i = 0; i < 90; i++) step(1);
      ctx.clearRect(0, 0, W, H); links.forEach((L) => drawLink(L, 0)); chips.forEach((p) => drawChip(p, 0));
      return;
    }
    for (let i = 0; i < 120; i++) step(1);      // warm-up so the page opens with a network already formed
    last = performance.now(); requestAnimationFrame(frame);
  }
  let rz; window.addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(resize, 150); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) running = false;
    else if (!running && !reduceMotion && !busy) { running = true; last = performance.now(); requestAnimationFrame(frame); }
  });
  start();
})();
