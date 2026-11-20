/* Parallel Lab — LiveChart: smooth, time-based real-time line charts (presentation only).
   It draws samples that app.js already receives; it never calls the API itself.

   Why the old charts broke
   • They re-sized their own <canvas> on every telemetry sample (bitmap = CSS width × devicePixelRatio).
     The bitmap size fed back into the page layout, so the chart — and the page — grew a little each
     second until the browser gave up and showed the grey "sad face" canvas.
   • Here every chart lives in a fixed-size CSS box (.lc-box). The canvas is absolutely positioned inside it,
     the bitmap is derived FROM the box, and nothing in the layout can ever depend on the bitmap again.

   Smoothness
   • The curve scrolls continuously by TIME (requestAnimationFrame) and is interpolated with a monotone cubic
     spline, so motion is fluid even though the OS is sampled only every 1.5 s (3 s while a benchmark runs).
   • No grid / graph-paper lines: just the gradient area, a soft glow, a live pulsing head and a hover read-out.

   Cost control (so benchmark timing is never disturbed)
   • One shared animation loop. Charts that are off-screen, on a hidden page or in a hidden tab are skipped.
   • 60 fps when idle; 20 fps and a cheaper "lite" style (no glow / pulse) while a benchmark or generation runs. */
'use strict';
(function () {
  const reduceMotion = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  const WINDOW_MS = 75000;          // visible time span
  const KEEP_MS = WINDOW_MS * 1.4;  // samples kept in memory
  const charts = new Set();
  let theme = {};
  let looping = false, lastFrame = 0, busy = false, busyCheckedAt = 0;

  function readTheme() {
    const s = getComputedStyle(document.documentElement);
    const g = (n, d) => (s.getPropertyValue(n).trim() || d);
    theme = {
      '--max': g('--max', '#c4adff'), '--base': g('--base', '#62cdff'), '--combine': g('--combine', '#5eeacb'),
      '--divide': g('--divide', '#ffcf5c'), fg: g('--fg', '#e8eeff'), fg3: g('--fg3', '#8798b1'),
      panel: g('--panel-solid', '#22325a'),
    };
  }
  function rgb(color) {
    const c = (color || '').trim();
    let m = /^#([0-9a-f]{3})$/i.exec(c);
    if (m) return m[1].split('').map((x) => parseInt(x + x, 16));
    m = /^#([0-9a-f]{6})$/i.exec(c);
    if (m) return [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16));
    m = /rgba?\(([^)]+)\)/i.exec(c);
    if (m) return m[1].split(',').slice(0, 3).map((x) => parseFloat(x));
    return [138, 166, 255];
  }
  const rgba = (color, a) => { const [r, g, b] = rgb(color); return `rgba(${r},${g},${b},${a})`; };
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const sign = (x) => (x < 0 ? -1 : 1);

  /* d3-style monotone-X tangents: smooth curve that never overshoots the data (stays inside 0..100 %). */
  function tangents(xs, ys) {
    const n = xs.length, t = new Array(n).fill(0);
    if (n < 2) return t;
    const d = [];
    for (let i = 0; i < n - 1; i++) d.push((ys[i + 1] - ys[i]) / ((xs[i + 1] - xs[i]) || 1e-6));
    t[0] = d[0]; t[n - 1] = d[n - 2];
    for (let i = 1; i < n - 1; i++) {
      if (d[i - 1] * d[i] <= 0) { t[i] = 0; continue; }
      const h0 = xs[i] - xs[i - 1], h1 = xs[i + 1] - xs[i];
      const p = (d[i - 1] * h1 + d[i] * h0) / (h0 + h1);
      t[i] = (sign(d[i - 1]) + sign(d[i])) * Math.min(Math.abs(d[i - 1]), Math.abs(d[i]), 0.5 * Math.abs(p)) || 0;
    }
    return t;
  }

  class Chart {
    constructor(canvasId, opts) {
      this.canvas = document.getElementById(canvasId);
      if (!this.canvas) return;
      this.ctx = this.canvas.getContext('2d');
      this.series = opts.series.map((s) => ({ name: s.name, color: s.color, fill: s.fill !== false, width: s.width || 2.6 }));
      this.min = opts.min ?? 0; this.max = opts.max ?? 100;
      this.format = opts.format || ((v) => `${v.toFixed(1)}%`);
      this.t = []; this.v = this.series.map(() => []);
      this.interval = 0; this.w = 0; this.h = 0; this.dpr = 1; this.visible = false;
      this.hoverX = null; this.grad = new Map();
      this.pad = { l: 2, r: 12, t: 14, b: 4 };
      // The observed element is the fixed-size BOX, never the canvas bitmap.
      this.box = this.canvas.parentElement || this.canvas;
      new ResizeObserver(() => this.resize()).observe(this.box);
      new IntersectionObserver((entries) => {
        this.visible = entries.some((e) => e.isIntersecting);
        if (this.visible) { this.resize(); ensureLoop(); this.draw(performance.now()); }
      }, { threshold: 0 }).observe(this.box);
      this.canvas.addEventListener('pointermove', (e) => {
        const r = this.canvas.getBoundingClientRect(); this.hoverX = e.clientX - r.left; if (reduceMotion) this.draw(performance.now());
      }, { passive: true });
      this.canvas.addEventListener('pointerleave', () => { this.hoverX = null; if (reduceMotion) this.draw(performance.now()); });
      charts.add(this);
    }

    resize() {
      const r = this.box.getBoundingClientRect();
      const w = Math.max(0, Math.round(r.width)), h = Math.max(0, Math.round(r.height));
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      if (w === this.w && h === this.h && dpr === this.dpr) return;
      this.w = w; this.h = h; this.dpr = dpr; this.grad.clear();
      this.canvas.width = Math.max(1, Math.round(w * dpr));
      this.canvas.height = Math.max(1, Math.round(h * dpr));
      if (this.visible) this.draw(performance.now());
    }

    push(values) {
      const now = performance.now();
      const n = this.t.length;
      // Two samples closer than 0.4 s (e.g. an extra refresh on page switch) just refresh the newest point.
      if (n && now - this.t[n - 1] < 400) { this.series.forEach((_, i) => { this.v[i][n - 1] = Number(values[i]) || 0; }); return; }
      if (n) { const dt = now - this.t[n - 1]; this.interval = this.interval ? this.interval * 0.7 + dt * 0.3 : dt; }
      this.t.push(now);
      this.series.forEach((_, i) => this.v[i].push(Number(values[i]) || 0));
      while (this.t.length > 2 && now - this.t[0] > KEEP_MS) { this.t.shift(); this.v.forEach((a) => a.shift()); }
      if (reduceMotion && this.visible) this.draw(now);
    }

    /** Average / peak / min of the last `ms` milliseconds for one series. */
    stats(index, ms = 30000) {
      const now = performance.now(); let sum = 0, cnt = 0, max = -Infinity, min = Infinity;
      for (let i = this.t.length - 1; i >= 0 && now - this.t[i] <= ms; i--) {
        const x = this.v[index][i]; sum += x; cnt++; if (x > max) max = x; if (x < min) min = x;
      }
      return cnt ? { avg: sum / cnt, max, min } : null;
    }

    yOf(v) { const { t, b } = this.pad; return t + (1 - (clamp(v, this.min, this.max) - this.min) / ((this.max - this.min) || 1)) * (this.h - t - b); }

    gradient(color) {
      const key = `${color}|${this.h}`;
      let g = this.grad.get(key);
      if (!g) {
        g = this.ctx.createLinearGradient(0, this.pad.t, 0, this.h);
        g.addColorStop(0, rgba(color, 0.42)); g.addColorStop(0.55, rgba(color, 0.12)); g.addColorStop(1, rgba(color, 0));
        this.grad.set(key, g);
      }
      return g;
    }

    draw(now) {
      if (!this.ctx || !this.w || !this.h) return;
      const c = this.ctx, W = this.w, H = this.h, { l, r } = this.pad;
      c.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      c.clearRect(0, 0, W, H);
      const n = this.t.length;
      const headX = W - r, plotW = Math.max(1, headX - l);
      if (!n) {
        c.fillStyle = theme.fg3; c.font = '600 12px Figtree, system-ui, sans-serif'; c.textBaseline = 'middle'; c.textAlign = 'center';
        c.fillText('Waiting for the first live sample…', W / 2, H / 2); return;
      }
      const lite = busy;
      const lastT = this.t[n - 1];
      // Render slightly behind real time so the head is always interpolated between two real samples.
      const delay = reduceMotion ? 0 : clamp((this.interval || 1500) * 1.3, 600, 6000);
      const disp = reduceMotion ? lastT : now - delay;
      const stale = disp - lastT;                         // > 0 : no newer sample than the display time
      // The visible span grows with the data (min 15 s) until it reaches the full window, so a fresh chart is never mostly empty.
      const win = clamp(disp - this.t[0], 15000, WINDOW_MS);
      this.win = win;
      const xOf = (tm) => headX - ((disp - tm) / win) * plotW;

      let first = 0;
      while (first < n - 1 && xOf(this.t[first + 1]) < l - 30) first++;
      let last = n - 1;
      for (let i = first; i < n; i++) { if (xOf(this.t[i]) >= headX) { last = i; break; } }
      const xs = [], idx = [];
      for (let i = first; i <= last; i++) { xs.push(xOf(this.t[i])); idx.push(i); }

      const stalledTooLong = stale > Math.max(9000, (this.interval || 1500) * 4);
      const headPoints = [];
      this.series.forEach((s, si) => {
        const ys = idx.map((i) => this.yOf(this.v[si][i]));
        const tan = tangents(xs, ys);
        const col = theme[s.color] || s.color;
        const p = new Path2D();
        p.moveTo(xs[0], ys[0]);
        for (let i = 0; i < xs.length - 1; i++) {
          const dx = xs[i + 1] - xs[i];
          p.bezierCurveTo(xs[i] + dx / 3, ys[i] + tan[i] * dx / 3, xs[i + 1] - dx / 3, ys[i + 1] - tan[i + 1] * dx / 3, xs[i + 1], ys[i + 1]);
        }
        c.save();
        c.beginPath(); c.rect(0, 0, headX + 0.5, H); c.clip();
        if (s.fill) {
          const area = new Path2D(p);
          area.lineTo(xs[xs.length - 1], H); area.lineTo(xs[0], H); area.closePath();
          c.fillStyle = this.gradient(col); c.fill(area);
        }
        c.lineJoin = 'round'; c.lineCap = 'round';
        if (!lite) { c.strokeStyle = rgba(col, 0.18); c.lineWidth = s.width + 6; c.stroke(p); }   // soft glow (cheap: no shadowBlur)
        c.strokeStyle = col; c.lineWidth = s.width; c.stroke(p);
        c.restore();

        // Head position on the curve (Hermite evaluation), or the newest point when data has stalled.
        let hx = headX, hy = ys[ys.length - 1];
        if (xs.length > 1 && xs[xs.length - 1] >= headX && stale <= 0) {
          const k = xs.length - 2, x0 = xs[k], x1 = xs[k + 1], dx = x1 - x0, u = clamp((headX - x0) / dx, 0, 1);
          const u2 = u * u, u3 = u2 * u;
          hy = (2 * u3 - 3 * u2 + 1) * ys[k] + (u3 - 2 * u2 + u) * dx * tan[k] + (-2 * u3 + 3 * u2) * ys[k + 1] + (u3 - u2) * dx * tan[k + 1];
        } else if (stale > 0) { hx = Math.min(headX, xs[xs.length - 1]); }
        headPoints.push({ x: hx, y: hy, col, dim: stalledTooLong });
      });

      // Live dot at the head of every line (pulse ring only when idle).
      const pulse = reduceMotion || lite ? 0 : (now % 1800) / 1800;
      headPoints.forEach((hp) => {
        if (!reduceMotion && !lite && !hp.dim) {
          c.beginPath(); c.arc(hp.x, hp.y, 4 + pulse * 10, 0, 6.2832);
          c.fillStyle = rgba(hp.col, 0.3 * (1 - pulse)); c.fill();
        }
        c.beginPath(); c.arc(hp.x, hp.y, 4.4, 0, 6.2832);
        c.fillStyle = hp.dim ? theme.fg3 : hp.col; c.fill();
        c.lineWidth = 2; c.strokeStyle = theme.panel; c.stroke();
      });

      if (this.hoverX !== null && this.hoverX >= l && this.hoverX <= headX) this.drawHover(c, disp, plotW, headX);
    }

    drawHover(c, disp, plotW, headX) {
      const tm = disp - ((headX - this.hoverX) / plotW) * (this.win || WINDOW_MS);
      const n = this.t.length;
      if (n < 2 || tm < this.t[0] || tm > this.t[n - 1]) return;
      let k = 1; while (k < n - 1 && this.t[k] < tm) k++;
      const f = clamp((tm - this.t[k - 1]) / ((this.t[k] - this.t[k - 1]) || 1), 0, 1);
      const rows = this.series.map((s, si) => ({ s, v: this.v[si][k - 1] + (this.v[si][k] - this.v[si][k - 1]) * f }));
      c.save();
      c.strokeStyle = rgba(theme.fg3, 0.55); c.lineWidth = 1; c.setLineDash([3, 4]);
      c.beginPath(); c.moveTo(this.hoverX, this.pad.t); c.lineTo(this.hoverX, this.h - this.pad.b); c.stroke(); c.setLineDash([]);
      rows.forEach(({ s, v }) => {
        const col = theme[s.color] || s.color;
        c.beginPath(); c.arc(this.hoverX, this.yOf(v), 4, 0, 6.2832); c.fillStyle = col; c.fill(); c.lineWidth = 2; c.strokeStyle = theme.panel; c.stroke();
      });
      c.font = '700 12px Figtree, system-ui, sans-serif'; c.textBaseline = 'middle';
      const label = rows.map(({ s, v }) => `${rows.length > 1 ? s.name + ' ' : ''}${this.format(v)}`).join('   ');
      const tw = c.measureText(label).width + 18, bx = clamp(this.hoverX - tw / 2, 2, this.w - tw - 2), by = 4;
      c.fillStyle = rgba(theme.panel, 0.95); c.strokeStyle = rgba(theme.fg3, 0.4);
      c.beginPath(); c.roundRect ? c.roundRect(bx, by, tw, 24, 8) : c.rect(bx, by, tw, 24); c.fill(); c.stroke();
      c.fillStyle = theme.fg; c.textAlign = 'left'; c.fillText(label, bx + 9, by + 12.5);
      c.restore();
    }
  }

  function anyVisible() { for (const ch of charts) if (ch.visible && ch.w) return true; return false; }

  function frame(ts) {
    if (document.hidden || !anyVisible()) { looping = false; return; }
    if (ts - busyCheckedAt > 500) {
      busyCheckedAt = ts;
      try { busy = !!(api.busyProbe && api.busyProbe()); } catch (e) { busy = false; }
    }
    const interval = busy ? 1000 / 20 : 1000 / 60;
    if (ts - lastFrame >= interval - 2) {
      lastFrame = ts;
      const now = performance.now();
      charts.forEach((ch) => { if (ch.visible) ch.draw(now); });
    }
    requestAnimationFrame(frame);
  }
  function ensureLoop() {
    if (reduceMotion || looping || document.hidden) return;
    looping = true; requestAnimationFrame(frame);
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) ensureLoop(); });
  window.addEventListener('themechange', () => { readTheme(); charts.forEach((ch) => { ch.grad.clear(); if (ch.visible) ch.draw(performance.now()); }); });
  readTheme();

  const api = {
    busyProbe: null,
    create: (id, opts) => new Chart(id, opts),
    refreshTheme: readTheme,
  };
  window.LiveChart = api;
})();
