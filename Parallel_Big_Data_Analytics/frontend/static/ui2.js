/* Parallel Lab — UI layer 2 (presentation only; never touches benchmark data or timing).
   1. Wires the live CPU / RAM charts (static/livecharts.js) to the telemetry app.js already receives.
   2. "Your computer" hardware profile on the Overview page (details cached by the backend, live usage on top).
   3. Summary page: report, techniques, annotated core code, scheduling demo.
   Load policy: while a benchmark or dataset generation is running nothing here polls or animates the page
   except the two small live charts (20 fps, lite style) — so the measured execution time is not affected. */
'use strict';
(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);
  const pct = (v) => `${(num(v) ?? 0).toFixed(1)}%`;
  function bytes(b) {
    b = num(b); if (b === null) return '—';
    const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return `${b >= 100 || i === 0 ? b.toFixed(0) : b.toFixed(1)} ${u[i]}`;
  }
  const rate = (b) => `${bytes(b)}/s`;
  function isBusy() {
    try {
      return typeof state !== 'undefined' && !!((state.benchmarkId && !state.benchmarkTerminal) || state.benchmarkStarting ||
        (state.generationId && !state.generationTerminal) || state.generationStarting);
    } catch (e) { return false; }
  }
  const currentPage = () => { try { return state.page; } catch (e) { return 'overview'; } };

  /* ====================================================================== 1. live charts */
  const charts = {};
  function initCharts() {
    if (!window.LiveChart) return;
    LiveChart.busyProbe = isBusy;
    charts.ovCpu = LiveChart.create('overviewCpuChart', { series: [{ name: 'CPU', color: '--max' }] });
    charts.ovRam = LiveChart.create('overviewRamChart', { series: [{ name: 'RAM', color: '--base' }] });
    charts.liveCpu = LiveChart.create('liveCpuChart', { series: [{ name: 'System', color: '--max' }, { name: 'App', color: '--combine', fill: false, width: 2.2 }] });
    charts.liveRam = LiveChart.create('liveRamChart', { series: [{ name: 'RAM', color: '--base' }] });
  }
  function chipStats(prefix, chart) {
    const s = chart && chart.stats(0, 30000); if (!s) return;
    $(`${prefix}Avg`).textContent = pct(s.avg); $(`${prefix}Peak`).textContent = pct(s.max); $(`${prefix}Low`).textContent = pct(s.min);
  }
  function onTelemetry(e) {
    const { live: d, appCpuShare } = e.detail || {};
    if (!d) return;
    const cpu = d.cpu || {}, mem = d.memory || {};
    charts.ovCpu?.push([cpu.total_percent]); charts.ovRam?.push([mem.percent]);
    charts.liveCpu?.push([cpu.total_percent, appCpuShare]); charts.liveRam?.push([mem.percent]);
    if (currentPage() === 'overview') {
      $('ovCpuNow').textContent = pct(cpu.total_percent); $('ovRamNow').textContent = pct(mem.percent);
      chipStats('ovCpuS', charts.ovCpu); chipStats('ovRamS', charts.ovRam);
    }
    latest.live = d; if (currentPage() === 'overview') updateHwLive();
  }
  window.addEventListener('pbda:telemetry', onTelemetry);

  /* ====================================================================== 2. hardware profile */
  const latest = { live: null, extra: null };
  let hw = null, hwBuilt = '', hwRetry = null, extraTimer = null, hwLoading = false;
  const ICON = {
    cpu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></svg>',
    mem: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="18" height="10" rx="2"/><path d="M7 10v2M11 10v2M15 10v2M7 16v4M12 16v4M17 16v4"/></svg>',
    sto: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg>',
    gpu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="7" width="18" height="10" rx="2"/><circle cx="11" cy="12" r="2.6"/><path d="M20 10h2M20 14h2M6 17v3M10 17v3"/></svg>',
    sys: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
  };
  const row = (k, v) => (v === null || v === undefined || v === '' ? '' : `<div class="pc-row"><span>${esc(k)}</span><strong>${v}</strong></div>`);
  const tag = (t) => (t ? `<span class="pc-tag">${esc(t)}</span>` : '');
  const meter = (id, label) => `<div class="pc-meter"><div class="pc-meter-top"><span>${esc(label)}</span><b id="${id}T">—</b></div><div class="pc-bar"><i id="${id}B"></i></div></div>`;
  function setMeter(id, text, p) {
    const t = $(`${id}T`), b = $(`${id}B`);
    if (t && t.textContent !== text) t.textContent = text;
    if (b) b.style.width = `${Math.max(0, Math.min(100, num(p) ?? 0))}%`;
  }
  const setText = (id, text) => { const el = $(id); if (el && el.textContent !== text) el.textContent = text; };
  const archLabel = (a) => (/64/.test(a || '') ? '64-bit' : /86|32/.test(a || '') ? '32-bit' : (a || '—'));

  function cpuCard(c, os) {
    const bits = archLabel(os.architecture) !== '—' ? archLabel(os.architecture) : (c.address_width ? `${c.address_width}-bit` : (c.bits ? `${c.bits}-bit` : null));
    const gen = [c.generation, c.codename].filter(Boolean).join(' · ');
    const base = c.base_mhz || c.max_mhz;
    return `<article class="pc-card cpu"><div class="pc-top"><span class="pc-ico">${ICON.cpu}</span><span class="pc-label">PROCESSOR</span></div>
<h3 class="pc-title">${esc(c.name || 'Unknown CPU')}</h3>${gen ? `<p class="pc-sub">${esc(gen)}</p>` : ''}
<div class="pc-tags">${tag(bits)}${tag(c.architecture)}${tag(`${c.physical_cores ?? '—'} cores · ${c.logical_processors ?? '—'} threads`)}</div>
<div class="pc-rows">${row('Vendor', esc(c.vendor || c.vendor_id))}${row('Generation', esc(c.generation))}${row('Codename', esc(c.codename))}${row('Base speed', base ? `${(base / 1000).toFixed(2)} GHz` : null)}
${row('Current speed', '<span id="pcCpuMhz">—</span>')}${row('L2 cache', c.l2_kb ? bytes(c.l2_kb * 1024) : null)}${row('L3 cache', c.l3_kb ? bytes(c.l3_kb * 1024) : null)}${row('Socket', esc(c.socket))}
${row('Virtualization', c.virtualization === true ? 'Enabled' : c.virtualization === false ? 'Disabled' : null)}${row('Family / Model / Stepping', c.family != null ? `${c.family} / ${c.model} / ${c.stepping}` : null)}</div>
${meter('pcCpu', 'CPU usage now')}</article>`;
  }
  function memCard(m) {
    const mods = m.modules || [];
    const installed = mods.reduce((a, x) => a + (num(x.capacity_bytes) || 0), 0) || num(m.total_bytes);
    const gb = installed ? Math.round(installed / 1024 ** 3) : null;
    const type = m.type || null;
    const slots = m.slots_total ? `${m.slots_used ?? mods.length} of ${m.slots_total} slots used` : (mods.length ? `${mods.length} module${mods.length > 1 ? 's' : ''}` : null);
    const list = mods.map((x) => row(x.slot, `${bytes(x.capacity_bytes)}${x.type ? ` · ${esc(x.type)}` : ''}${x.speed_mts ? ` · ${x.speed_mts} MT/s` : ''}${x.manufacturer ? ` · ${esc(x.manufacturer)}` : ''}`)).join('');
    return `<article class="pc-card mem"><div class="pc-top"><span class="pc-ico">${ICON.mem}</span><span class="pc-label">MEMORY (RAM)</span></div>
<h3 class="pc-title">${gb ? `${gb} GB` : bytes(m.total_bytes)} ${esc(type || 'RAM')}</h3><p class="pc-sub">${[m.speed_mts ? `${m.speed_mts} MT/s` : null, slots].filter(Boolean).map(esc).join(' · ') || 'Memory type not reported by the system'}</p>
<div class="pc-tags">${tag(type || 'Type not reported')}${tag(mods[0]?.form_factor)}${tag(m.layout_note)}</div>
<div class="pc-rows">${row('Installed', gb ? `${gb} GB` : null)}${row('Usable by Windows', bytes(m.total_bytes))}${row('In use now', '<span id="pcMemUsed">—</span>')}${row('Available', '<span id="pcMemAvail">—</span>')}${row('Used by this app', '<span id="pcMemApp">—</span>')}${list}</div>
${meter('pcMem', 'Memory usage now')}</article>`;
  }
  function stoCard(disks) {
    const list = (disks || []).map((d) => `<div class="pc-device"><div class="pc-device-name">${esc(d.model)}</div><div class="pc-tags" style="margin:6px 0 4px">${tag(d.type)}${tag(d.description)}${tag(bytes(d.size_bytes))}${tag(d.interface && d.interface !== '—' ? d.interface : null)}${tag(d.health)}</div></div>`).join('');
    const head = disks && disks.length ? `${disks.length} physical drive${disks.length > 1 ? 's' : ''}` : 'Drive details not reported';
    return `<article class="pc-card sto"><div class="pc-top"><span class="pc-ico">${ICON.sto}</span><span class="pc-label">STORAGE</span></div>
<h3 class="pc-title">${esc(head)}</h3><p class="pc-sub">Type (SSD / HDD / NVMe M.2), size and live usage of each volume</p>${list}
<div class="pc-sep">VOLUMES · USED / TOTAL</div><div id="pcVolumes"><div class="empty-small">Reading volumes…</div></div>
<div class="pc-rows" style="margin-top:8px">${row('Disk read now', '<span id="pcDiskR">—</span>')}${row('Disk write now', '<span id="pcDiskW">—</span>')}</div></article>`;
  }
  function gpuCard(gpus) {
    const list = (gpus || []).map((g, i) => {
      const integrated = g.dedicated_bytes != null && g.dedicated_bytes < 1024 ** 3 && g.shared_bytes;
      return `<div class="pc-device"><div class="pc-device-name">${esc(g.name)}</div>
<div class="pc-tags" style="margin:6px 0 6px">${tag(g.vendor)}${tag(integrated ? 'Integrated' : (g.dedicated_bytes ? 'Dedicated' : null))}${tag(g.dedicated_bytes ? `${bytes(g.dedicated_bytes)} VRAM` : null)}</div>
<div class="pc-rows">${row('Dedicated video memory', g.dedicated_bytes ? bytes(g.dedicated_bytes) : null)}${row('Shared (system RAM)', g.shared_bytes ? bytes(g.shared_bytes) : null)}${row('Driver', esc(g.driver))}${row('Display', g.resolution ? `${esc(g.resolution)}${g.refresh_hz ? ` @ ${g.refresh_hz} Hz` : ''}` : null)}
${row('GPU load now', `<span id="pcGpu${i}U">—</span>`)}${row('Video memory in use', `<span id="pcGpu${i}M">—</span>`)}${row('Temperature / Power', `<span id="pcGpu${i}X">—</span>`)}</div>${meter(`pcGpu${i}`, 'GPU usage now')}</div>`;
    }).join('');
    const none = '<div class="empty-small" id="pcNoGpu">No graphics adapter reported yet.</div>';
    return `<article class="pc-card gpu"><div class="pc-top"><span class="pc-ico">${ICON.gpu}</span><span class="pc-label">GRAPHICS (GPU)</span></div>
<h3 class="pc-title">${gpus && gpus.length ? esc(gpus.map((g) => g.name).join(' + ')) : 'Graphics card'}</h3><div class="pc-devices">${list || none}</div>
<div class="pc-note">GPU values come from Windows performance counters or NVIDIA NVML. The OpenMP engine itself runs on the CPU.</div></article>`;
  }
  function sysCard(os, machine) {
    const ver = [os.feature_version ? `Version ${os.feature_version}` : null, os.build ? `Build ${os.build}` : (os.version ? `Build ${os.version}` : null)].filter(Boolean).join(' · ');
    return `<article class="pc-card sys"><div class="pc-top"><span class="pc-ico">${ICON.sys}</span><span class="pc-label">SYSTEM &amp; WINDOWS</span></div>
<h3 class="pc-title">${esc(os.name || 'Operating system')}</h3><p class="pc-sub">${esc(ver || os.version || '')}</p>
<div class="pc-tags">${tag(archLabel(os.architecture))}${tag(machine.manufacturer)}</div>
<div class="pc-rows">${row('Computer name', esc(machine.hostname))}${row('Manufacturer', esc(machine.manufacturer))}${row('Model', esc(machine.model))}${row('System type', esc(machine.system_type))}${row('OS architecture', esc(os.architecture || null))}${row('OS version', esc(os.version))}${row('BIOS / firmware', esc(machine.bios))}</div></article>`;
  }

  function renderHw() {
    const grid = $('pcGrid'); if (!grid || !hw) return;
    const key = JSON.stringify([hw.status, hw.cpu, hw.memory, hw.storage_devices, hw.gpus, hw.os, hw.machine]);
    const st = $('pcStatus');
    if (st) st.textContent = hw.status === 'ready' ? 'DETECTED' : hw.status === 'deferred' ? 'PAUSED · WORKLOAD RUNNING' : 'BASIC INFO';
    // The 4th Overview card shows the CPU model; use the real marketing name ("11th Gen Intel Core i5-…") instead of the raw "Intel64 Family 6 Model 140" string.
    const cpuLabel = $('ovCpuModel'); if (cpuLabel && hw.cpu && hw.cpu.name) cpuLabel.textContent = hw.cpu.name;
    if (key === hwBuilt) return;
    hwBuilt = key;
    grid.innerHTML = cpuCard(hw.cpu || {}, hw.os || {}) + memCard(hw.memory || {}) + stoCard(hw.storage_devices) + gpuCard(hw.gpus) + sysCard(hw.os || {}, hw.machine || {})
      + (hw.message ? `<div class="pc-note" style="grid-column:1/-1">${esc(hw.message)}</div>` : '');
    volKey = ''; updateHwLive();
  }

  let volKey = '';
  function updateHwLive() {
    if (!hw || !$('pcCpuT')) return;
    const d = latest.live, x = latest.extra;
    if (d) {
      const cpu = d.cpu || {}, mem = d.memory || {};
      setMeter('pcCpu', pct(cpu.total_percent), cpu.total_percent);
      setText('pcCpuMhz', cpu.frequency_mhz ? `${(cpu.frequency_mhz / 1000).toFixed(2)} GHz` : '—');
      setMeter('pcMem', `${bytes(mem.used_bytes)} / ${bytes(mem.total_bytes)} · ${pct(mem.percent)}`, mem.percent);
      setText('pcMemUsed', bytes(mem.used_bytes)); setText('pcMemAvail', bytes(mem.available_bytes)); setText('pcMemApp', bytes(mem.process_rss_bytes));
      setText('pcDiskR', rate(d.disk_io?.read_bytes_per_sec)); setText('pcDiskW', rate(d.disk_io?.write_bytes_per_sec));
    }
    const vols = (d && d.storage && d.storage.length ? d.storage : (x && x.volumes) || []);
    if (vols.length) {
      const k = vols.map((v) => v.mountpoint).join('|');
      if (k !== volKey) {
        volKey = k;
        $('pcVolumes').innerHTML = vols.map((v, i) => meter(`pcVol${i}`, `${v.mountpoint} · ${v.filesystem || ''}`)).join('');
      }
      vols.forEach((v, i) => setMeter(`pcVol${i}`, `${bytes(v.used_bytes)} / ${bytes(v.total_bytes)} · ${pct(v.percent)}`, v.percent));
    }
    const live = (x && !x.paused && x.gpus) || [];
    (hw.gpus || []).forEach((g, i) => {
      const lg = live.find((q) => q.name && g.name && (q.name.toLowerCase() === g.name.toLowerCase() || g.name.toLowerCase().includes(q.name.toLowerCase()) || q.name.toLowerCase().includes(g.name.toLowerCase()))) || (live.length === 1 && (hw.gpus || []).length === 1 ? live[0] : null);
      if (!lg) return;
      setMeter(`pcGpu${i}`, lg.util_percent != null ? pct(lg.util_percent) : '—', lg.util_percent);
      setText(`pcGpu${i}U`, lg.util_percent != null ? pct(lg.util_percent) : 'Not available');
      const used = lg.dedicated_used_bytes, tot = lg.dedicated_total_bytes || g.dedicated_bytes;
      setText(`pcGpu${i}M`, used != null ? `${bytes(used)}${tot ? ` / ${bytes(tot)}` : ''}${lg.shared_used_bytes != null ? ` · shared ${bytes(lg.shared_used_bytes)}` : ''}` : 'Not available');
      setText(`pcGpu${i}X`, `${lg.temperature_c != null ? `${lg.temperature_c} °C` : '—'} / ${lg.power_watts != null ? `${lg.power_watts} W` : '—'}`);
    });
  }

  async function loadHw() {
    if (hwLoading || typeof api !== 'function') return;
    hwLoading = true;
    try { hw = await api('/api/hardware/details'); renderHw(); }
    catch (e) { const st = $('pcStatus'); if (st) st.textContent = 'UNAVAILABLE'; $('pcGrid').innerHTML = `<div class="empty-small">Hardware details could not be read: ${esc(e.message || e)}</div>`; hwBuilt = ''; }
    finally { hwLoading = false; }
    clearTimeout(hwRetry);
    if (!hw || hw.status !== 'ready') hwRetry = setTimeout(() => { if (currentPage() === 'overview' && !isBusy()) loadHw(); else scheduleRetryLater(); }, 20000);
  }
  function scheduleRetryLater() { clearTimeout(hwRetry); hwRetry = setTimeout(() => { if (currentPage() === 'overview' && !isBusy()) loadHw(); else scheduleRetryLater(); }, 8000); }

  async function pollExtra() {
    clearTimeout(extraTimer);
    if (currentPage() !== 'overview' || document.hidden || isBusy() || !hw) return;   // only while the Overview is on screen and nothing heavy runs
    try { const x = await api('/api/hardware/live-extra'); if (x && !x.paused) { latest.extra = x; updateHwLive(); } } catch (e) { /* GPU telemetry is optional */ }
    extraTimer = setTimeout(pollExtra, 2500);
  }
  function startExtra() { clearTimeout(extraTimer); extraTimer = setTimeout(pollExtra, 300); }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) startExtra(); });

  /* ====================================================================== 3. summary page */
  const TECH = [
    { g: 'parallel', t: 'OpenMP parallel loop', tag: 'prange', d: 'OpenMP is a shared-memory standard that splits the iterations of a loop across CPU threads. One block of data is processed by one thread while other threads work on other blocks at the same time.', u: 'prange(...) inside every parallel kernel; compiled with the C compiler’s OpenMP switch.' },
    { g: 'parallel', t: 'Native Cython kernels', tag: 'nogil', d: 'Cython turns typed Python-like code into C. Inside a nogil section Python’s Global Interpreter Lock is released, so threads execute truly in parallel at C speed.', u: 'openmp_kernels.pyx — sequential and parallel versions come from the same source.' },
    { g: 'parallel', t: 'Block decomposition', tag: '131,072 rows', d: 'The array is cut into equal blocks. A block is the smallest unit of work a thread can take, which keeps loads balanced and memory access sequential.', u: 'BLOCK_ROWS in every kernel; n_chunks = ceil(n / BLOCK_ROWS).' },
    { g: 'parallel', t: 'Static · dynamic · guided scheduling', tag: 'schedule', d: 'Scheduling decides how blocks are handed to threads: fixed equal shares (static), first-come first-served (dynamic) or big-to-small shares (guided).', u: 'Chosen in Benchmark Setup under “How work is distributed”.' },
    { g: 'parallel', t: 'Thread-private partials + merge', tag: 'reduction', d: 'Each thread accumulates into its own variables and writes once into its own slot. After the loop the few partial results are merged, so no lock is needed while computing.', u: 'p[tid, …] partial rows, merged after the prange loop.' },
    { g: 'parallel', t: 'Cache-line alignment', tag: '64 B', d: 'Per-thread result rows are padded and aligned to 64 bytes so two threads never write to the same cache line. This prevents “false sharing”, a hidden slowdown.', u: '_aligned_zeros() allocates every partial-results array.' },
    { g: 'measure', t: 'Fair benchmark protocol', tag: 'warm-up · repeats', d: 'Both methods get a warm-up run, the same prepared data, several repetitions and alternating order, so caches, page faults and heat do not favour one side.', u: 'benchmark.py — order alternates every repetition; 1–7 repeats.' },
    { g: 'measure', t: 'Median timing', tag: 'median', d: 'The median of the repeats is reported instead of the average. One unusually slow run (a background task, for example) cannot distort the headline.', u: 'Speed-up cards use median compute time; spread is checked and explained.' },
    { g: 'measure', t: 'Result parity check', tag: 'verified', d: 'A result is marked verified only if the sequential and parallel answers agree within a tight numeric tolerance in every repetition, including the warm-up.', u: '_results_equal(…, tolerance=1e-9) for all runs.' },
    { g: 'measure', t: 'Speed-up, efficiency, time saved', tag: 'metrics', d: 'Speed-up compares median times, efficiency relates it to the number of threads actually observed, and time saved is the share of single-thread time that was removed.', u: 'Results page cards and the plain-language explanation.' },
    { g: 'system', t: 'Chunked CSV preparation', tag: 'PyArrow', d: 'Large CSV files are read in chunks and converted to typed arrays. This preparation is timed separately so file reading is never mixed into the compute comparison.', u: 'datasets.py / benchmark.py preparation phase (PyArrow when installed).' },
    { g: 'system', t: 'Real telemetry from the OS', tag: 'psutil · NVML', d: 'CPU, memory, disk, network, process and GPU figures are sampled from the operating system — they are measurements of this machine, not simulations.', u: 'Live Monitor and the “Your computer” panel.' },
  ];
  const CODE = {
    cython: {
      caption: 'Simplified excerpt of numeric_stats_parallel (sum only; the real kernel also tracks count, sum of squares, min and max).',
      lines: [
        ['cdef Py_ssize_t BLOCK_ROWS = 131072', 'Work unit: 131,072 rows per block.'],
        ['n_chunks = (n + BLOCK_ROWS - 1) // BLOCK_ROWS', 'How many blocks cover all rows (round up).'],
        ['p = _aligned_zeros((threads, 8), float64)', 'One 64-byte partial-result row per thread.'],
        ['for chunk in prange(n_chunks, nogil=True,', 'OpenMP parallel loop. nogil lets threads run truly together.'],
        ['                    num_threads=threads,', 'How many CPU threads you selected.'],
        ['                    schedule="static"):', 'How blocks are handed out: static, dynamic or guided.'],
        ['    tid = threadid()', 'Which thread am I? (0 … threads − 1)'],
        ['    start = chunk * BLOCK_ROWS', 'First row of this block.'],
        ['    end = min(start + BLOCK_ROWS, n)', 'Last row — the final block may be shorter.'],
        ['    local_sum = 0.0', 'Accumulate in a private local variable: fast, nothing shared.'],
        ['    for i in range(start, end):', 'Plain C loop over the block.'],
        ['        local_sum = local_sum + x[i]', 'The real work: add every value.'],
        ['    p[tid, 1] += local_sum', 'One write into this thread’s own slot — no lock needed.'],
        ['# loop finished → one thread merges', 'Back on a single thread; this part is tiny.'],
        ['for t in range(threads): total += p[t, 1]', 'Combine the per-thread partial sums into the final answer.'],
      ],
      cards: [
        { n: '0', h: 'Split into blocks', p: 'Rows are cut into 131,072-row blocks. Each block is one unit of work, so data can be shared fairly.', r: [1, 2] },
        { n: '1', h: 'Threads claim blocks', p: 'prange hands blocks to OpenMP threads using the chosen schedule; nogil releases Python’s lock so they really run at once.', r: [4, 9] },
        { n: '2', h: 'Private partial results', p: 'Each thread adds into its own local variable and writes once to its own aligned row — no locks, no false sharing.', r: [3, 3, 10, 13] },
        { n: '3', h: 'Merge once', p: 'After the loop the few partial rows are added together. The answer equals the 1-thread kernel, and that equality is verified.', r: [14, 15] },
      ],
      why: ['Why align each row to 64 bytes?', 'If two threads update counters on the same 64-byte cache line, that line bounces between cores (false sharing) and 4 threads can end up slower than 1. Padding each thread’s row keeps their writes apart.'],
    },
    python: {
      caption: 'Simplified outline of the benchmark protocol in benchmark.py.',
      lines: [
        ['warm_seq, warm_par = run(seq), run(par)', 'One untimed warm-up per method loads caches and pages.'],
        ['assert results_equal(warm_seq, warm_par)', 'Even the warm-up answers must match.'],
        ['for rep in range(repetitions):', 'Repeat the test 1–7 times.'],
        ['    order = [seq, par] if rep % 2 else [par, seq]', 'Alternate who goes first to cancel cache and heat bias.'],
        ['    for mode in order:', 'Run both methods in that order.'],
        ['        wall, kernel = run(mode, prepared_arrays)', 'Same prepared arrays; wall time and native time recorded.'],
        ['speedup = median(seq_wall) / median(par_wall)', 'Median, not mean: robust against one unlucky run.'],
        ['efficiency = native_speedup / observed_threads * 100', 'How well the threads were actually used.'],
        ['verified = results_equal(seq, par, tol=1e-9)', 'Marked verified only if every repetition matches.'],
      ],
      cards: [
        { n: '0', h: 'Warm up', p: 'Each method runs once, untimed, so the first timed run is not paying for cold caches. The answers are already compared here.', r: [1, 2] },
        { n: '1', h: 'Alternate and repeat', p: 'Sequential and parallel swap places every repetition, on the same prepared data, so neither side gets a systematic advantage.', r: [3, 6] },
        { n: '2', h: 'Median and verify', p: 'Medians give speed-up and efficiency; the parity check decides whether the whole experiment may be called verified.', r: [7, 9] },
      ],
      why: ['Why the median?', 'A single slow run — a background update, a cold cache — can distort an average. The median ignores such outliers, so the headline number reflects typical behaviour.'],
    },
  };
  const SCHED = [
    { h: 'Static', p: 'Blocks are split into equal contiguous shares before the loop starts. Lowest overhead; best when every block costs about the same.', s: [0,0,0,0,1,1,1,1,2,2,2,2,3,3,3,3] },
    { h: 'Dynamic', p: 'Whichever thread is free takes the next block. Balances uneven work, but every hand-out has a small coordination cost.', s: [0,1,2,3,1,0,3,2,0,2,3,1,3,0,1,2] },
    { h: 'Guided', p: 'Threads take large shares first and smaller ones later. Fewer hand-outs than dynamic, still balances the tail.', s: [0,0,0,0,0,1,1,1,1,2,2,2,3,3,0,1] },
  ];

  const TK = /(#.*$)|("[^"]*"|'[^']*')|\b(\d[\d_]*(?:\.\d+)?(?:e-?\d+)?)\b|\b(cdef|def|for|in|range|if|else|nogil|return|assert|prange|and|or|not)\b|\b([A-Za-z_]\w*)(?=\()/g;
  function highlight(line) {
    let out = '', last = 0, m; TK.lastIndex = 0;
    while ((m = TK.exec(line))) {
      out += esc(line.slice(last, m.index));
      const cls = m[1] ? 'c' : m[2] ? 's' : m[3] ? 'n' : m[4] ? 'k' : 'f';
      out += `<span class="tk-${cls}">${esc(m[0])}</span>`; last = TK.lastIndex;
    }
    return out + esc(line.slice(last));
  }
  let tab = 'cython';
  function renderCode() {
    const c = CODE[tab];
    $('codeCaption').textContent = c.caption;
    $('codeRows').innerHTML = c.lines.map((l, i) => `<div class="code-row${l[0].trim().startsWith('#') ? ' cm' : ''}" data-i="${i + 1}"><span class="ln">${i + 1}</span><code>${highlight(l[0])}</code><span class="nt">${esc(l[1])}</span></div>`).join('');
    $('codeSide').innerHTML = c.cards.map((k, i) => `<div class="step-card" data-r="${k.r.join(',')}"><span class="sn">${esc(k.n)}</span><div><h4>${esc(k.h)}</h4><p>${esc(k.p)}</p></div></div>`).join('')
      + `<div class="why-box"><strong>${esc(c.why[0])}</strong><p>${esc(c.why[1])}</p></div>`;
    $('codeSide').querySelectorAll('.step-card').forEach((card) => {
      const r = card.dataset.r.split(',').map(Number);
      const spans = []; for (let i = 0; i < r.length; i += 2) spans.push([r[i], r[i + 1] ?? r[i]]);
      const set = (on) => $('codeRows').querySelectorAll('.code-row').forEach((row) => {
        const n = Number(row.dataset.i); row.classList.toggle('hl', on && spans.some(([a, b]) => n >= a && n <= b));
      });
      card.addEventListener('mouseenter', () => set(true)); card.addEventListener('mouseleave', () => set(false));
    });
  }
  function renderTech(filter) {
    $('techGrid').innerHTML = TECH.filter((t) => filter === 'all' || t.g === filter).map((t, i) => `<article class="tech-card" data-g="${t.g}" style="animation-delay:${i * 40}ms"><h3>${esc(t.t)} <i>${esc(t.tag)}</i></h3><p class="tech-def">${esc(t.d)}</p><p class="tech-use"><b>Used here:</b> ${esc(t.u)}</p></article>`).join('');
  }
  function renderSched() {
    $('schedGrid').innerHTML = SCHED.map((s) => `<article class="sched-card"><h3>${s.h}</h3><p>${esc(s.p)}</p><div class="strip">${s.s.map((t, i) => `<i data-t="${t}" style="--i:${i}"></i>`).join('')}</div></article>`).join('');
  }
  function animateCounters(root) {
    root.querySelectorAll('[data-count]').forEach((el) => {
      const target = Number(el.dataset.count), suffix = el.dataset.suffix || '';
      if (matchMedia('(prefers-reduced-motion: reduce)').matches) { el.textContent = target + suffix; return; }
      const t0 = performance.now(), dur = 1100;
      const step = (now) => { const k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 3); el.textContent = Math.round(target * e) + suffix; if (k < 1) requestAnimationFrame(step); };
      requestAnimationFrame(step);
    });
  }
  function initSummary() {
    if (!$('page-summary')) return;
    renderTech('all'); renderCode(); renderSched();
    $('techFilter').addEventListener('click', (e) => {
      const b = e.target.closest('.chip'); if (!b) return;
      $('techFilter').querySelectorAll('.chip').forEach((x) => x.classList.toggle('active', x === b)); renderTech(b.dataset.f);
    });
    $('codeTabs').addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b || b.dataset.tab === tab) return;
      tab = b.dataset.tab; $('codeTabs').querySelectorAll('button').forEach((x) => x.classList.toggle('active', x === b)); renderCode();
    });
    $('codeCopy').addEventListener('click', async () => {
      const text = CODE[tab].lines.map((l) => l[0]).join('\n'), btn = $('codeCopy');
      try { await navigator.clipboard.writeText(text); } catch (e) {
        const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); } catch (_) { /* ignore */ } ta.remove();
      }
      btn.textContent = 'Copied ✓'; setTimeout(() => { btn.textContent = 'Copy'; }, 1400);
    });
    // Reveal-on-scroll (one cheap observer; nothing runs while the page is hidden).
    const io = new IntersectionObserver((entries) => entries.forEach((en) => {
      if (en.isIntersecting && !en.target.classList.contains('in')) { en.target.classList.add('in'); animateCounters(en.target); }
    }), { threshold: 0.12 });
    document.querySelectorAll('#page-summary .reveal').forEach((el) => io.observe(el));
    // Replay the transitions each time the page is opened again.
    window.addEventListener('pbda:page', (e) => {
      if (e.detail.page !== 'summary') {
        document.querySelectorAll('#page-summary .reveal.in').forEach((el) => el.classList.remove('in'));
        document.querySelectorAll('#page-summary [data-count]').forEach((el) => { el.textContent = '0'; });
      }
    });
  }

  /* ====================================================================== boot */
  window.addEventListener('pbda:page', (e) => {
    if (e.detail.page === 'overview') {
      if (!hw || hw.status !== 'ready') { if (!isBusy()) loadHw(); }
      startExtra(); updateHwLive();
    } else clearTimeout(extraTimer);
  });
  initCharts();
  initSummary();
  loadHw().then(startExtra);
})();
