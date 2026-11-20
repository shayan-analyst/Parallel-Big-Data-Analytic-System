import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class IdCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"])


def test_frontend_ids_are_unique_and_all_literal_js_refs_exist():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    parser = IdCollector()
    parser.feed(html)
    ids = parser.ids
    assert len(ids) == len(set(ids)), "Duplicate HTML IDs make event/render targets ambiguous."
    referenced = set(re.findall(r"\$\('([^']+)'\)", js))
    missing = sorted(referenced - set(ids))
    assert not missing, f"JavaScript references missing HTML IDs: {missing}"


def test_live_monitor_and_generator_controls_are_wired():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "styles.css").read_text(encoding="utf-8")
    assert 'id="generatorBatchSize"' in html
    assert "generatorBatchSize" in js
    assert "/api/benchmarks/active" in js
    assert "preparation_progress" in js
    assert "phase_progress" in js
    assert "benchmarkActive?3000:1500" in js
    assert "'/api/hardware/live?light=true'" in js
    assert "$('startBenchmark').disabled=state.benchmarkStarting" in js
    assert "clientBenchmarkActive?'Open Live Monitor →'" in js
    assert "startBenchmarkPhaseTimer" in js
    assert "#page-live .live-chart-grid>.panel{height:270px" in css
    assert 'id="liveTelemetryStatus"' in html
    assert 'id="liveCpuSource"' in html and 'id="liveRamSource"' in html
    assert "LIVE FROM ${String(state.hardware?.hostname" in js
    assert "REFRESHING EVERY 1 SECOND" in js
    assert "#page-live #gpuPanel{max-height:260px" in css


def test_generator_startup_passes_chunk_size_and_progress_ui_is_truthful():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "styles.css").read_text(encoding="utf-8")
    assert "function showGenerationStarted(rows,batchSize)" in js
    assert "showGenerationStarted(rows,batchSize)" in js
    assert "Starting the first ${fmtNum(Number($('generatorBatchSize').value" not in js
    assert "state.generationCancelQueued" in js
    assert "requestGenerationCancel(r.generation_id)" in js
    assert "is-indeterminate" in js
    assert "@keyframes generationSweep" in css
    assert 'value="50000000"' in html
    assert "phase==='saved'" in js or "e.phase==='saved'" in js


def test_frontend_exposes_only_big_data_generation_sizes_and_explains_operations():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    generator_select = re.search(r'<select id="datasetRowsSelect">(.*?)</select>', html, re.S)
    assert generator_select, "Big Data record-count selector is missing."
    values = re.findall(r'value="(\d+)"', generator_select.group(1))
    assert values == ["100000", "1000000", "5000000", "10000000", "25000000", "50000000", "75000000"]
    assert "customRows" not in html and "customRows" not in js
    assert 'id="analyticsDataset"' in html
    assert "let MIN_BENCHMARK_ROWS = 100_000" in js
    assert "generation_row_presets" in js
    assert "formatApiErrorDetail" in js
    assert "state.datasets.filter(d=>Number(d.rows)>=MIN_BENCHMARK_ROWS)" in js
    assert 'value="comprehensive_analytics"' in html
    for operation in ("numeric_stats", "filter_gt", "frequency", "group_sum", "top_k_groups", "date_frequency"):
        assert f"{operation}:" in js, f"Missing plain-language description for {operation}."
    assert "Group By + Sum" in html
    assert "MEDIAN COMPUTE TIME" in js
    assert "applyMedianComparisonMetrics(e.result)" in js
    assert "showBenchmarkExplanation(e.result)" in js
    assert "const defaultThreads=availableThreads.includes(4)?4:(availableThreads.filter(x=>x<=core).pop()||availableThreads[0]||1)" in js
    assert "The explanation below shows why this outcome occurred" in js
    assert "recommendation_probe_start" in js
    assert "Tested and verified:" in js
    assert "CPU THREADS" in html
    assert "PARALLEL CPU THREADS" in html
    assert "Single-thread run" in html
    assert "Multi-thread run" in html
    assert "['verified','verified_next_size'].includes(validation.status)" in js
    assert "tested_candidates" in (ROOT / "app" / "services" / "benchmark.py").read_text(encoding="utf-8")
    assert 'id="livePlainSummary"' in html
    assert html.index('id="livePlainSummary"') > html.index('id="parWall"')
    assert "setLiveSummary('Comparison complete'" not in js



def test_generator_errors_are_structured_and_live_explanation_has_next_setup():
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "function formatApiErrorDetail(detail)" in js
    assert "item.loc.filter(part=>part!=='body').join('.')" in js
    assert 'id="generationStatusPill"' in html
    assert "recommended_configuration" in js
    assert "recommendation_probe_start" in js
    assert "Tested and verified:" in js
    assert "CPU THREADS" in html
    assert "PARALLEL CPU THREADS" in html
    assert "Single-thread run" in html
    assert "Multi-thread run" in html
    assert "['verified','verified_next_size'].includes(validation.status)" in js
    assert "tested_candidates" in (ROOT / "app" / "services" / "benchmark.py").read_text(encoding="utf-8")


def test_live_monitor_shows_local_os_cpu_and_memory_series():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="liveCpuChart"' in html and 'id="liveRamChart"' in html
    assert "System CPU average" in html
    assert "System RAM used" in html
    assert "benchmarkActive?3000:1500" in js
    assert "'/api/hardware/live?light=true'" in js
    assert "processRamHistory" in js
    assert "local operating system via psutil" in (ROOT / "app" / "services" / "monitor.py").read_text(encoding="utf-8")

def test_failed_benchmark_event_resets_compute_progress_and_keeps_error():
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert "records benchmarked" in js
    assert "finishBenchmarkUI(e.status,e.message" in js
    assert "No performance conclusion should be drawn from an incomplete run." in js


def test_ui2_fixed_size_charts_scroll_reset_summary_and_hardware_panel():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    ui = (ROOT / "frontend" / "static" / "ui2.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "ui2.css").read_text(encoding="utf-8")
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    # charts live in fixed CSS boxes; the canvas never drives layout and the old self-resizing drawer is gone
    for canvas in ("overviewCpuChart", "overviewRamChart", "liveCpuChart", "liveRamChart"):
        assert re.search(rf'<div class="lc-box"><canvas class="lc-canvas" id="{canvas}"', html), canvas
    assert "function drawLineChart" not in js
    assert ".lc-canvas{position:absolute;inset:0" in css
    # a page always opens at its top (Start benchmark -> Live Monitor)
    assert "window.scrollTo({top:0,left:0,behavior:'instant'})" in js
    # Summary page + hardware profile are present and read-only
    assert 'id="page-summary"' in html and 'data-page="summary"' in html
    assert 'id="pcProfile"' in html and "/api/hardware/details" in ui and "/api/hardware/live-extra" in ui
    assert "@app.get(\"/api/hardware/details\")" in main and "@app.get(\"/api/hardware/live-extra\")" in main
    # themed scrollbars
    assert "::-webkit-scrollbar-thumb" in css
