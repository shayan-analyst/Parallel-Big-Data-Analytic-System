# UI update notes

Only the presentation layer changed. Benchmark, kernels, dataset generation, resources, store and monitor code are byte-identical to the previous package.

- frontend/static/livecharts.js  – fixed-size, time-based smooth CPU/RAM charts (no grid lines, no layout growth)
- frontend/static/ui2.css / ui2.js – themed scrollbars, hardware profile cards, Summary page behaviour
- frontend/index.html, app.js, fx.js – side-by-side CPU/RAM charts, Summary tab, Start benchmark -> Live Monitor opens at the top
- app/services/hardware_info.py + 2 read-only endpoints in app/main.py (/api/hardware/details, /api/hardware/live-extra):
  cached hardware inventory and GPU/volume usage. Never collected or served while a benchmark or generation is running.
- tests: test_frontend_contract.py updated, test_hardware_info.py added.

## Small update (header/footer/background)
- Header: "Parallel and Distributed Computing"; footer: "Powered by Xypex Studio" (version removed).
- Page-background graph-paper lines removed.
- Chip network rewritten in fx.js: evenly spread, smooth inertial motion, better connectivity, cursor attraction.
- Frontend files only (index.html, styles.css, fx.js).
