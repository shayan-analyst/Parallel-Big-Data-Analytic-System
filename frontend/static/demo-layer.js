/* Demo layer: active ONLY when the UI is opened from a URL (GitHub Pages etc.).
   On localhost / 127.0.0.1 (RUN.bat or the portable app) this file does nothing at all.
   Override for testing: ?demo=1 forces demo mode, ?demo=0 forces normal mode. */
(function () {
  'use strict';
  var FX = {"templates": [{"kind": "sales", "label": "Sales Dataset", "description": "Transactions with products, categories, regions, quantities and revenue.", "columns": [{"name": "Transaction_ID", "type": "int64", "role": "identifier"}, {"name": "Transaction_Date", "type": "date", "role": "date"}, {"name": "Product_ID", "type": "int32", "role": "identifier"}, {"name": "Product", "type": "string", "role": "category"}, {"name": "Category", "type": "string", "role": "category"}, {"name": "Region", "type": "string", "role": "category"}, {"name": "Quantity", "type": "int32", "role": "numeric"}, {"name": "Unit_Price", "type": "float64", "role": "numeric"}, {"name": "Revenue", "type": "float64", "role": "numeric"}], "numeric_columns": ["Quantity", "Unit_Price", "Revenue"], "category_columns": ["Product", "Category", "Region"], "date_columns": ["Transaction_Date"]}, {"kind": "finance", "label": "Finance Dataset", "description": "Synthetic financial transactions with account, type, category, amount and balance.", "columns": [{"name": "Transaction_ID", "type": "int64", "role": "identifier"}, {"name": "Transaction_Date", "type": "date", "role": "date"}, {"name": "Account_ID", "type": "int32", "role": "identifier"}, {"name": "Transaction_Type", "type": "string", "role": "category"}, {"name": "Category", "type": "string", "role": "category"}, {"name": "Amount", "type": "float64", "role": "numeric"}, {"name": "Balance", "type": "float64", "role": "numeric"}, {"name": "Region", "type": "string", "role": "category"}], "numeric_columns": ["Amount", "Balance"], "category_columns": ["Transaction_Type", "Category", "Region"], "date_columns": ["Transaction_Date"]}, {"kind": "products", "label": "Product Inventory Dataset", "description": "Warehouse inventory, stock, unit costs and selling prices.", "columns": [{"name": "Product_ID", "type": "int64", "role": "identifier"}, {"name": "Product_Name", "type": "string", "role": "category"}, {"name": "Category", "type": "string", "role": "category"}, {"name": "Warehouse", "type": "string", "role": "category"}, {"name": "Stock_Quantity", "type": "int32", "role": "numeric"}, {"name": "Unit_Cost", "type": "float64", "role": "numeric"}, {"name": "Selling_Price", "type": "float64", "role": "numeric"}, {"name": "Inventory_Value", "type": "float64", "role": "numeric"}], "numeric_columns": ["Stock_Quantity", "Unit_Cost", "Selling_Price", "Inventory_Value"], "category_columns": ["Product_Name", "Category", "Warehouse"], "date_columns": []}, {"kind": "students", "label": "Student Performance Dataset", "description": "Student marks, departments, courses, semesters and attendance.", "columns": [{"name": "Student_ID", "type": "int64", "role": "identifier"}, {"name": "Department", "type": "string", "role": "category"}, {"name": "Semester", "type": "int32", "role": "numeric"}, {"name": "Course_ID", "type": "int32", "role": "identifier"}, {"name": "Marks", "type": "float64", "role": "numeric"}, {"name": "Attendance_Percentage", "type": "float64", "role": "numeric"}, {"name": "Credits", "type": "int32", "role": "numeric"}, {"name": "Academic_Year", "type": "string", "role": "category"}], "numeric_columns": ["Semester", "Marks", "Attendance_Percentage", "Credits"], "category_columns": ["Department", "Academic_Year"], "date_columns": []}, {"kind": "employees", "label": "Employee Dataset", "description": "Employees, departments, designations, experience and salary.", "columns": [{"name": "Employee_ID", "type": "int64", "role": "identifier"}, {"name": "Department", "type": "string", "role": "category"}, {"name": "Designation", "type": "string", "role": "category"}, {"name": "Joining_Date", "type": "date", "role": "date"}, {"name": "Experience_Years", "type": "int32", "role": "numeric"}, {"name": "Salary", "type": "float64", "role": "numeric"}, {"name": "Region", "type": "string", "role": "category"}], "numeric_columns": ["Experience_Years", "Salary"], "category_columns": ["Department", "Designation", "Region"], "date_columns": ["Joining_Date"]}, {"kind": "attendance", "label": "Attendance Dataset", "description": "Employee attendance records, working hours, shifts and status.", "columns": [{"name": "Attendance_ID", "type": "int64", "role": "identifier"}, {"name": "Employee_ID", "type": "int32", "role": "identifier"}, {"name": "Department", "type": "string", "role": "category"}, {"name": "Attendance_Date", "type": "date", "role": "date"}, {"name": "Attendance_Status", "type": "string", "role": "category"}, {"name": "Working_Hours", "type": "float64", "role": "numeric"}, {"name": "Shift", "type": "string", "role": "category"}, {"name": "Region", "type": "string", "role": "category"}], "numeric_columns": ["Working_Hours"], "category_columns": ["Department", "Attendance_Status", "Shift", "Region"], "date_columns": ["Attendance_Date"]}], "options": {"minimum_rows": 100000, "minimum_benchmark_rows": 100000, "maximum_rows": 75000000, "generation_row_presets": [100000, 1000000, 5000000, 10000000, 25000000, 50000000, 75000000], "recommended_demo_row_presets": [100000, 1000000, 5000000, 10000000, 25000000, 50000000, 75000000], "generation_chunk_sizes": [10000, 25000, 50000, 100000], "recommended_demo_sequence": [100000, 1000000, 5000000, 10000000, 25000000, 50000000, 75000000], "notes": "Generation and benchmarking are available from 100K records."}};
  var q = new URLSearchParams(location.search), h = location.hostname;
  var isLocal = /^(localhost|127(\.\d+){3}|\[::1\]|::1)$/.test(h);
  var demo = q.get('demo') === '1' ? true : q.get('demo') === '0' ? false : !isLocal;
  if (!demo) return;
  window.PBDA_DEMO = true;

  var FILE = 'Parallel_Big_Data_Analytics_Windows_x64.zip';
  function downloadUrl() {
    if (window.PBDA_DOWNLOAD_URL) return window.PBDA_DOWNLOAD_URL;
    var m = h.match(/^([^.]+)\.github\.io$/i);
    if (!m) return '#';
    var seg = location.pathname.split('/').filter(Boolean)[0];
    var repo = seg && !/\.html?$/i.test(seg) ? seg : m[1] + '.github.io';
    return 'https://github.com/' + m[1] + '/' + repo + '/releases/latest/download/' + FILE;
  }

  /* ---- 1. Fake "no engine" API so the page loads cleanly (no error toasts) ---- */
  var realFetch = window.fetch.bind(window);
  var MSG = 'Demo version: execution is available only in the downloadable Windows app.';
  function reply(body, status) {
    return new Response(JSON.stringify(body), { status: status || 200, headers: { 'Content-Type': 'application/json' } });
  }
  window.fetch = function (u, o) {
    var s = typeof u === 'string' ? u : (u && u.url) || '';
    var i = s.split('?')[0].indexOf('/api/');
    if (i < 0) return realFetch(u, o);
    var p = s.split('?')[0].slice(i);
    var get = ((o && o.method) || 'GET').toUpperCase() === 'GET';
    if (get) {
      if (p === '/api/health') return Promise.resolve(reply({
        status: 'ok', application: 'Parallel Big Data Analytics', version: 'demo',
        engine: { compiled: false, openmp: false, engine: 'Not connected (demo)' }, engine_error: null,
        hardware: { hostname: 'Demo (no local engine)', cpu_name: 'Not connected' },
        pyarrow_available: false, kernel_capabilities: {}, benchmark_limits: { max_threads: 4 }, local_execution: false }));
      if (p === '/api/datasets/templates') return Promise.resolve(reply({ templates: FX.templates }));
      if (p === '/api/datasets/generation-options') return Promise.resolve(reply(FX.options));
      if (p === '/api/datasets') return Promise.resolve(reply({ datasets: [] }));
      if (p === '/api/history') return Promise.resolve(reply({ experiments: [] }));
      if (p === '/api/datasets/generation/active') return Promise.resolve(reply({ status: 'idle', latest: {}, progress: 0 }));
      if (p === '/api/benchmarks/active') return Promise.resolve(reply({ status: 'idle' }));
    }
    return Promise.resolve(reply({ detail: MSG }, 503));
  };

  /* ---- 2. UI layer ---- */
  var LOCKED = ['generateBtn', 'startBenchmark', 'downloadDatasetBtn', 'exportJsonBtn', 'exportCsvBtn'];
  var css = '.pbda-demo-bar{position:fixed;left:0;right:0;bottom:0;z-index:9998;display:flex;gap:14px;align-items:center;justify-content:center;flex-wrap:wrap;padding:10px 16px;background:#1b1f2a;color:#f3f4f6;border-top:2px solid #f5a524;font:600 14px/1.3 system-ui,sans-serif}'
    + '.pbda-demo-bar a,.pbda-demo-modal a.pbda-dl{background:#f5a524;color:#1b1f2a;text-decoration:none;padding:8px 16px;border-radius:8px;font-weight:800}'
    + 'body.pbda-demo{padding-bottom:64px}'
    + '.demo-locked{opacity:.5!important;cursor:not-allowed!important;filter:grayscale(.5)}'
    + '.connection .status-dot{background:#f5a524!important;box-shadow:0 0 0 3px rgba(245,165,36,.25)!important}'
    + '.pbda-demo-ov{position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;padding:16px}'
    + '.pbda-demo-modal{max-width:440px;background:#1b1f2a;color:#f3f4f6;border:1px solid #f5a524;border-radius:14px;padding:24px;font:500 15px/1.5 system-ui,sans-serif}'
    + '.pbda-demo-modal h3{margin:0 0 8px;font-size:18px}.pbda-demo-modal .row{display:flex;gap:10px;margin-top:18px;justify-content:flex-end}'
    + '.pbda-demo-modal button{background:transparent;color:#f3f4f6;border:1px solid #6b7280;border-radius:8px;padding:8px 16px;cursor:pointer;font-weight:700}';

  function lock(id, text, asHtml) {
    var el = document.getElementById(id); if (!el) return;
    var cur = function () { return asHtml ? el.innerHTML : el.textContent; };
    var set = function () { if (cur() !== text) { if (asHtml) el.innerHTML = text; else el.textContent = text; } };
    set();
    new MutationObserver(set).observe(el, { childList: true, characterData: true, subtree: true });
  }
  function closeModal() { var o = document.getElementById('pbdaDemoOv'); if (o) o.remove(); }
  function showModal() {
    if (document.getElementById('pbdaDemoOv')) return;
    var o = document.createElement('div'); o.className = 'pbda-demo-ov'; o.id = 'pbdaDemoOv';
    o.innerHTML = '<div class="pbda-demo-modal" role="dialog" aria-modal="true"><h3>Demo version</h3>'
      + '<p>Bhai, aap demo version par ho. Execution ke liye app download karke apne PC par chalayein.</p>'
      + '<div class="row"><button type="button" id="pbdaDemoClose">Close</button><a class="pbda-dl" href="' + downloadUrl() + '">Download .exe (Windows)</a></div></div>';
    o.addEventListener('click', function (e) { if (e.target === o) closeModal(); });
    document.body.appendChild(o);
    document.getElementById('pbdaDemoClose').addEventListener('click', closeModal);
  }

  /* Capture phase: runs before app.js handlers, so nothing is ever executed in demo mode. */
  document.addEventListener('click', function (e) {
    var t = e.target && e.target.closest && e.target.closest(LOCKED.map(function (i) { return '#' + i; }).join(','));
    if (!t) return;
    e.preventDefault(); e.stopImmediatePropagation(); showModal();
  }, true);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeModal(); });

  function init() {
    var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);
    document.body.classList.add('pbda-demo');
    var bar = document.createElement('div'); bar.className = 'pbda-demo-bar';
    bar.innerHTML = '<span>Demo version &mdash; the real parallel engine runs only in the downloadable Windows app (no install needed).</span><a href="' + downloadUrl() + '">Download .exe (Windows)</a>';
    document.body.appendChild(bar);
    lock('connectionText', 'DEMO VERSION · ENGINE NOT CONNECTED');
    lock('engineNote', 'Demo version: download the Windows app and run RUN.bat to connect the native OpenMP engine.');
    lock('engineWarning', 'Engine not connected (demo version). Download the Windows app to run benchmarks.');
    lock('engineStatus', '<span class="status-dot"></span>Engine not connected (demo)', true);
    LOCKED.forEach(function (id) {
      var b = document.getElementById(id); if (!b) return;
      var fix = function () { b.classList.add('demo-locked'); if (b.disabled) b.removeAttribute('disabled'); };
      fix(); new MutationObserver(fix).observe(b, { attributes: true, attributeFilter: ['disabled', 'class'] });
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
