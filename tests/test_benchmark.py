import time
from concurrent.futures import ThreadPoolExecutor

from app.services import benchmark, datasets, store


import pytest


@pytest.fixture(autouse=True)
def _use_deterministic_test_cpu_capacity(monkeypatch):
    monkeypatch.setattr(benchmark.psutil, "cpu_count", lambda logical=True: 8)


def test_benchmark_all_operations(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "DATA_ROOT", tmp_path / "generated")
    monkeypatch.setattr(datasets, "META_ROOT", tmp_path / "metadata")
    monkeypatch.setattr(benchmark, "WORKSPACE", tmp_path / "results")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "history" / "experiments.db")
    store.initialize()
    # Force the standard-library parser and cross its 16,384-row reader boundary.
    # This validates experiment-wide category codes across multiple input blocks.
    monkeypatch.setattr(benchmark, "pacsv", None)
    ds = datasets.generate_dataset("sales", 70_000, seed=15, batch_size=10_000)
    configs = [
        {"dataset_id": ds["dataset_id"], "operation": "numeric_stats", "column": "Revenue", "threads": 4, "schedule": "static", "repetitions": 1},
        {"dataset_id": ds["dataset_id"], "operation": "filter_gt", "column": "Revenue", "threshold": 50000, "threads": 4, "schedule": "dynamic", "repetitions": 1},
        {"dataset_id": ds["dataset_id"], "operation": "frequency", "group_column": "Category", "threads": 4, "schedule": "guided", "repetitions": 1},
        {"dataset_id": ds["dataset_id"], "operation": "group_sum", "column": "Revenue", "group_column": "Category", "threads": 4, "schedule": "static", "repetitions": 1},
        {"dataset_id": ds["dataset_id"], "operation": "top_k_groups", "column": "Revenue", "group_column": "Product", "top_k": 3, "threads": 4, "schedule": "dynamic", "repetitions": 1},
        {"dataset_id": ds["dataset_id"], "operation": "date_frequency", "date_column": "Transaction_Date", "date_granularity": "month", "threads": 4, "schedule": "guided", "repetitions": 1},
    ]
    for config in configs:
        jid = benchmark.create_job(config)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(benchmark._worker, jid)
            future.result(timeout=30)
        job = benchmark.get_job(jid)
        assert job["status"] == "completed", job.get("error")
        assert job["result"]["verified"] is True
        assert job["result"]["sequential"]["median_kernel_seconds"] >= 0
        assert job["result"]["parallel"]["median_kernel_seconds"] >= 0
        if config["operation"] == "top_k_groups":
            assert len(job["result"]["result"]["top_groups"]) <= 3
        if config["operation"] == "date_frequency":
            assert all(len(period) == 7 for period in job["result"]["result"]["frequencies"])


def test_worker_thread_events_are_delivered_on_subscriber_loop():
    import asyncio

    async def scenario():
        job_id = "thread-safe-test"
        queue = benchmark.subscribe(job_id)
        payload = {"type": "phase_progress", "progress": 42.0}
        try:
            await asyncio.to_thread(benchmark._emit, job_id, payload)
            delivered = await asyncio.wait_for(queue.get(), timeout=2)
            assert delivered["type"] == payload["type"]
            assert delivered["progress"] == payload["progress"]
            assert isinstance(delivered.get("timestamp"), float)
        finally:
            benchmark.unsubscribe(job_id, queue)

    asyncio.run(scenario())


def test_benchmark_refuses_to_fall_back_when_native_engine_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "DATA_ROOT", tmp_path / "generated")
    monkeypatch.setattr(datasets, "META_ROOT", tmp_path / "metadata")
    item = datasets.generate_dataset("sales", 20, seed=5, batch_size=10_000)
    monkeypatch.setattr(benchmark, "kernels", None)
    config = {
        "dataset_id": item["dataset_id"], "operation": "numeric_stats",
        "column": "Revenue", "threads": 2, "schedule": "static", "repetitions": 1,
    }
    try:
        benchmark.create_job(config)
    except RuntimeError as exc:
        assert "compiled OpenMP engine is unavailable" in str(exc)
    else:
        raise AssertionError("Benchmark must refuse to run without the compiled OpenMP extension.")


def test_benchmark_reads_and_prepares_csv_once_for_both_engines(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "DATA_ROOT", tmp_path / "generated")
    monkeypatch.setattr(datasets, "META_ROOT", tmp_path / "metadata")
    monkeypatch.setattr(benchmark, "WORKSPACE", tmp_path / "results")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "history" / "experiments.db")
    store.initialize()
    ds = datasets.generate_dataset("sales", 20_000, seed=12, batch_size=10_000)
    original_reader = benchmark._read_arrow_batches
    reads = {"count": 0}

    def counted_reader(*args, **kwargs):
        reads["count"] += 1
        yield from original_reader(*args, **kwargs)

    monkeypatch.setattr(benchmark, "_read_arrow_batches", counted_reader)
    config = {
        "dataset_id": ds["dataset_id"], "operation": "numeric_stats",
        "column": "Revenue", "threads": 4, "schedule": "static", "repetitions": 2,
    }
    jid = benchmark.create_job(config)
    benchmark._worker(jid)
    job = benchmark.get_job(jid)
    assert job["status"] == "completed", job.get("error")
    assert reads["count"] == 1, "The source CSV must be parsed only once per experiment."
    assert job["result"]["methodology"]["same_prepared_arrays"] is True
    assert job["result"]["verified"] is True
    assert job["result"]["preparation_seconds"] >= 0
    assert len(job["result"]["sequential"]["runs"]) == 2
    assert len(job["result"]["parallel"]["runs"]) == 2


def test_benchmark_active_job_exposes_live_progress_snapshot():
    job_id = "active-snapshot-test"
    config = {"dataset_id": "test"}
    with benchmark._job_lock:
        benchmark._jobs[job_id] = {
            "job_id": job_id, "status": "running", "config": config,
            "progress": 32.0, "latest": {}, "events": [], "result": None,
            "error": None, "event_sequence": 0,
            "progress_state": {"type": "phase_progress", "progress": 32.0,
                               "records_processed": 320, "total_records": 1000},
            "cancel_event": __import__("threading").Event(),
        }
    try:
        active = benchmark.get_active_job()
        assert active is not None
        assert active["job_id"] == job_id
        assert active["progress_state"]["records_processed"] == 320
    finally:
        with benchmark._job_lock:
            benchmark._jobs.pop(job_id, None)


def test_preparation_uses_and_releases_memory_map_under_low_memory(tmp_path, monkeypatch):
    from types import SimpleNamespace

    class LargeFakeBatch:
        num_rows = 400_000

    workspace = tmp_path / "benchmark-workspace"
    monkeypatch.setattr(benchmark, "WORKSPACE", workspace)
    monkeypatch.setattr(benchmark.psutil, "virtual_memory", lambda: SimpleNamespace(available=4 * 1024 * 1024))
    monkeypatch.setattr(benchmark, "_read_arrow_batches", lambda *_args, **_kwargs: iter([LargeFakeBatch()]))
    monkeypatch.setattr(
        benchmark, "_to_float_array",
        lambda batch, _column: __import__("numpy").arange(batch.num_rows, dtype="float64"),
    )

    prepared, _segments, temp_dir, _elapsed, storage_mode = benchmark._prepare_dataset(
        "memory-map-test",
        {"operation": "numeric_stats", "column": "Revenue"},
        {"rows": 400_000, "path": "unused.csv"},
        __import__("threading").Event(),
    )
    assert storage_mode == "memory-mapped arrays"
    assert isinstance(prepared["numeric"], __import__("numpy").memmap)
    folder = temp_dir.name
    benchmark._release_prepared_arrays(prepared)
    temp_dir.cleanup()
    assert not __import__("pathlib").Path(folder).exists()


def test_category_codes_are_global_across_reader_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "WORKSPACE", tmp_path / "results")
    batches = [
        benchmark._DictBatch({"Category": ["A", "B", "A"]}),
        benchmark._DictBatch({"Category": ["B", "C", "A"]}),
    ]
    monkeypatch.setattr(benchmark, "_read_arrow_batches", lambda *_args, **_kwargs: iter(batches))
    prepared, segments, temp_dir, _elapsed, _storage = benchmark._prepare_dataset(
        "global-codes-test",
        {"operation": "frequency", "group_column": "Category"},
        {"rows": 6, "path": "unused.csv"},
        __import__("threading").Event(),
    )
    try:
        assert segments == [{"start": 0, "end": 6, "labels": ["A", "B", "C"]}]
        assert prepared["codes"].tolist() == [0, 1, 0, 1, 2, 0]
    finally:
        benchmark._release_prepared_arrays(prepared)
        if temp_dir is not None:
            temp_dir.cleanup()


def test_benchmark_configuration_validation_rejects_invalid_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "DATA_ROOT", tmp_path / "generated")
    monkeypatch.setattr(datasets, "META_ROOT", tmp_path / "metadata")
    item = datasets.generate_dataset("sales", 20, seed=8, batch_size=10_000)
    base = {
        "dataset_id": item["dataset_id"], "operation": "numeric_stats",
        "column": "Revenue", "threads": 2, "schedule": "static", "repetitions": 1,
    }
    invalid_cases = [
        {**base, "column": "Nonexistent_Column"},
        {**base, "threads": 257},
        {**base, "repetitions": 0},
        {**base, "schedule": "unknown"},
        {**base, "operation": "frequency", "group_column": "Missing_Category"},
        {**base, "operation": "date_frequency", "date_column": "Transaction_Date", "date_granularity": "decade"},
    ]
    for config in invalid_cases:
        try:
            benchmark.create_job(config)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Invalid benchmark configuration was accepted: {config}")



def test_comprehensive_analytics_pipeline_matches_sequential_and_parallel(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "DATA_ROOT", tmp_path / "generated")
    monkeypatch.setattr(datasets, "META_ROOT", tmp_path / "metadata")
    monkeypatch.setattr(benchmark, "WORKSPACE", tmp_path / "results")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "history" / "experiments.db")
    store.initialize()
    ds = datasets.generate_dataset("sales", 85_000, seed=23, batch_size=25_000)
    config = {
        "dataset_id": ds["dataset_id"], "operation": "comprehensive_analytics",
        "column": "Revenue", "group_column": "Region", "threshold": 100_000.0,
        "threads": 4, "schedule": "static", "repetitions": 3,
    }
    job_id = benchmark.create_job(config)
    benchmark._worker(job_id)
    job = benchmark.get_job(job_id)
    assert job["status"] == "completed", job.get("error")
    result = job["result"]
    assert result["verified"] is True
    assert result["result"]["record_count"] == 85_000
    assert sum(group["count"] for group in result["result"]["groups"].values()) == 85_000
    assert result["result"]["records_above_threshold"] <= 85_000
    assert result["warmup"]["runs_per_mode"] == 1
    assert result["warmup"]["timings_excluded_from_medians"] is True
    assert len(result["sequential"]["runs"]) == 3
    assert len(result["parallel"]["runs"]) == 3
    assert result["sequential"]["display_metrics"]["summary_statistic"] == "median"
    assert result["analysis"]["outcome"] in {"parallel_faster", "single_worker_faster", "similar"}


def test_summary_cards_use_medians_not_the_last_repetition():
    runs = [
        {"repetition": 1, "wall_seconds": 1.0, "kernel_seconds": 0.8},
        {"repetition": 2, "wall_seconds": 0.4, "kernel_seconds": 0.3},
        {"repetition": 3, "wall_seconds": 0.7, "kernel_seconds": 0.5},
    ]
    summary = benchmark._summarize_runs(runs, 1_000_000)
    assert summary["median_wall_seconds"] == 0.7
    assert summary["median_kernel_seconds"] == 0.5
    assert summary["display_metrics"]["compute_seconds"] == 0.7
    assert summary["display_metrics"]["calculation_seconds"] == 0.5
    assert summary["representative_repetition"] == 3



def test_performance_analysis_explains_small_frequency_workload_and_schedule():
    seq = benchmark._summarize_runs([
        {"repetition": 1, "wall_seconds": 0.20, "kernel_seconds": 0.18},
        {"repetition": 2, "wall_seconds": 0.21, "kernel_seconds": 0.19},
        {"repetition": 3, "wall_seconds": 0.19, "kernel_seconds": 0.17},
    ], 10_000_000)
    par = benchmark._summarize_runs([
        {"repetition": 1, "wall_seconds": 0.28, "kernel_seconds": 0.25},
        {"repetition": 2, "wall_seconds": 0.27, "kernel_seconds": 0.24},
        {"repetition": 3, "wall_seconds": 0.29, "kernel_seconds": 0.26},
    ], 10_000_000)
    analysis = benchmark._build_performance_analysis(
        {"operation": "frequency", "schedule": "dynamic"}, 10_000_000,
        seq, par, seq["median_wall_seconds"] / par["median_wall_seconds"],
        seq["median_kernel_seconds"] / par["median_kernel_seconds"],
        True, True, "RAM arrays",
    )
    assert analysis["outcome"] == "single_worker_faster"
    assert any("Category Frequency" in reason for reason in analysis["reasons"])
    assert any("Dynamic" in reason for reason in analysis["reasons"])
    assert analysis["recommended_next_test"]


def test_calculation_time_measures_native_kernel_only(monkeypatch):
    import threading
    import time
    import numpy as np

    original_merge = benchmark._merge_result

    def delayed_kernel(values):
        time.sleep(0.015)
        return (len(values), 45.0, 285.0, 0.0, 9.0)

    def delayed_merge(*args, **kwargs):
        time.sleep(0.045)
        return original_merge(*args, **kwargs)

    monkeypatch.setattr(benchmark.kernels, "numeric_stats_seq", delayed_kernel)
    monkeypatch.setattr(benchmark, "_merge_result", delayed_merge)
    run = benchmark._run_phase(
        "timer-test", "sequential",
        {"operation": "numeric_stats", "column": "Revenue", "threads": 4},
        {"numeric": np.arange(10, dtype=np.float64)},
        [{"start": 0, "end": 10, "labels": None}],
        1, 10, threading.Event(), report_progress=False,
    )

    assert run["kernel_seconds"] < 0.035
    assert run["wall_seconds"] - run["kernel_seconds"] >= 0.035


def test_verification_checks_every_measured_repetition():
    matching_seq = [{"result": {"count": 10}}, {"result": {"count": 10}}, {"result": {"count": 10}}]
    matching_par = [{"result": {"count": 10}}, {"result": {"count": 10}}, {"result": {"count": 10}}]
    mismatched_seq = [{"result": {"count": 10}}, {"result": {"count": 9}}, {"result": {"count": 10}}]

    assert benchmark._verify_repetitions(matching_seq, matching_par) is True
    assert benchmark._verify_repetitions(mismatched_seq, matching_par) is False
    assert benchmark._verify_repetitions([], matching_par) is False


def test_analysis_does_not_guess_a_worker_or_dataset_size_recommendation():
    seq = benchmark._summarize_runs([
        {"repetition": 1, "wall_seconds": 0.020, "kernel_seconds": 0.018},
        {"repetition": 2, "wall_seconds": 0.021, "kernel_seconds": 0.019},
        {"repetition": 3, "wall_seconds": 0.019, "kernel_seconds": 0.017},
    ], 1_000_000)
    par = benchmark._summarize_runs([
        {"repetition": 1, "wall_seconds": 0.031, "kernel_seconds": 0.027},
        {"repetition": 2, "wall_seconds": 0.030, "kernel_seconds": 0.026},
        {"repetition": 3, "wall_seconds": 0.032, "kernel_seconds": 0.028},
    ], 1_000_000)
    analysis = benchmark._build_performance_analysis(
        {"operation": "frequency", "schedule": "static", "threads": 8},
        1_000_000, seq, par,
        seq["median_wall_seconds"] / par["median_wall_seconds"],
        seq["median_kernel_seconds"] / par["median_kernel_seconds"],
        True, True, "RAM arrays",
    )
    assert analysis["outcome"] == "single_worker_faster"
    assert analysis["recommended_configuration"] is None
    assert "No faster thread configuration has been verified" in analysis["recommended_next_test"]
    assert "coordination" in analysis["audience_explanation"]
    assert len(analysis["audience_explanation"]) < 220


def test_recommendation_probe_only_suggests_a_worker_setting_it_measured(monkeypatch):
    import threading

    monkeypatch.setattr(benchmark.psutil, "cpu_count", lambda logical=True: 8)
    seq_runs = [
        {"mode": "sequential", "repetition": i, "wall_seconds": 1.0 + (i - 2) * 0.01,
         "kernel_seconds": 0.8 + (i - 2) * 0.01, "result": {"count": 100}}
        for i in range(1, 4)
    ]
    seq_summary = benchmark._summarize_runs(seq_runs, 100_000)
    par_runs = [
        {"mode": "parallel", "repetition": i, "wall_seconds": 1.30 + (i - 2) * 0.01,
         "kernel_seconds": 1.10 + (i - 2) * 0.01, "result": {"count": 100}}
        for i in range(1, 4)
    ]
    par_summary = benchmark._summarize_runs(par_runs, 100_000)
    values = {4: [0.78, 0.80, 0.82], 6: [0.90, 0.91, 0.89], 8: [1.10, 1.11, 1.09]}

    def fake_run(_job_id, mode, cfg, _prepared, _segments, repetition, _rows, _cancel,
                 *, report_progress=True, run_type="measured"):
        if run_type == "recommendation_warmup":
            seconds = 0.9
        elif mode == "sequential":
            seconds = 1.0 + (repetition - 2) * 0.01
        else:
            seconds = values[cfg["threads"]][repetition - 1]
        return {
            "mode": mode, "repetition": repetition, "thread_count": cfg["threads"],
            "observed_threads_max": cfg["threads"], "wall_seconds": seconds,
            "kernel_seconds": seconds * 0.8, "result": {"count": 100},
        }

    monkeypatch.setattr(benchmark, "_run_phase", fake_run)
    outcome = benchmark._probe_recommendation_options(
        "probe-test", {"threads": 2, "operation": "numeric_stats", "schedule": "static"},
        {"kind": "sales", "seed": 1},
        {"numeric": object()}, [{"start": 0, "end": 100_000, "labels": None}],
        100_000, threading.Event(), seq_runs, seq_summary, par_runs, par_summary,
        True, True, 3,
    )
    assert outcome["status"] == "verified"
    assert outcome["recommended_configuration"]["threads"] == 4
    assert outcome["recommended_configuration"]["dataset_rows"] == 100_000
    assert outcome["recommended_configuration"]["measured_runs"] == 3
    assert {item["threads"] for item in outcome["tested_candidates"]} == {4}
    assert all(item["source"] == "automatic_paired_probe" for item in outcome["tested_candidates"])
    assert all("median_sequential_compute_seconds" in item for item in outcome["tested_candidates"])
    assert all(item["median_sequential_compute_seconds"] > item["median_compute_seconds"] for item in outcome["tested_candidates"] if item["threads"] == 4)



def test_next_size_result_is_only_recommended_after_exact_probe_reports_winner(monkeypatch):
    import threading
    monkeypatch.setattr(benchmark, "ENABLE_NEXT_SIZE_PROBE", True)

    monkeypatch.setattr(benchmark.psutil, "cpu_count", lambda logical=True: 1)
    seq_runs = [
        {"mode": "sequential", "repetition": i, "wall_seconds": 1.0 + (i - 2) * 0.01,
         "kernel_seconds": 0.8, "result": {"count": 10}}
        for i in range(1, 4)
    ]
    par_runs = [
        {"mode": "parallel", "repetition": i, "wall_seconds": 1.3 + (i - 2) * 0.01,
         "kernel_seconds": 1.1, "observed_threads_max": 2, "result": {"count": 10}}
        for i in range(1, 4)
    ]
    seq_summary = benchmark._summarize_runs(seq_runs, 100_000)
    par_summary = benchmark._summarize_runs(par_runs, 100_000)
    verified_config = {
        "threads": 4, "dataset_rows": 1_000_000, "speedup_vs_single": 1.24,
        "measured_runs": 3, "operation": "numeric_stats", "schedule": "static",
        "automatically_generated_trial": True,
    }
    monkeypatch.setattr(
        benchmark, "_probe_next_dataset_size",
        lambda *_args, **_kwargs: {
            "status": "verified_next_size", "next_dataset_rows": 1_000_000,
            "recommended_configuration": verified_config, "message": "Verified 4 threads at 1M.",
        },
    )
    result = benchmark._probe_recommendation_options(
        "next-size-pass-through", {"threads": 2, "operation": "numeric_stats"},
        {"kind": "sales", "seed": 1}, {"numeric": object()}, [], 100_000,
        threading.Event(), seq_runs, seq_summary, par_runs, par_summary,
        True, True, 3,
    )
    assert result["status"] == "verified_next_size"
    assert result["recommended_configuration"] == verified_config


def test_optional_recommendation_error_does_not_fail_completed_benchmark(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "DATA_ROOT", tmp_path / "generated")
    monkeypatch.setattr(datasets, "META_ROOT", tmp_path / "metadata")
    monkeypatch.setattr(benchmark, "WORKSPACE", tmp_path / "results")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "history" / "experiments.db")
    monkeypatch.setattr(benchmark, "pacsv", None)
    store.initialize()
    ds = datasets.generate_dataset("sales", 100_000, seed=82, batch_size=10_000)
    config = {
        "dataset_id": ds["dataset_id"], "operation": "numeric_stats", "column": "Revenue",
        "threads": 2, "schedule": "static", "repetitions": 1,
    }
    monkeypatch.setattr(
        benchmark, "_probe_recommendation_options",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated optional probe failure")),
    )
    job_id = benchmark.create_job(config)
    benchmark._worker(job_id)
    job = benchmark.get_job(job_id)
    assert job["status"] == "completed", job.get("error")
    assert job["result"]["verified"] is True
    assert job["result"]["recommendation_validation"]["status"] == "skipped_probe_error"

def test_recommendation_probe_skips_unstable_baseline(monkeypatch):
    import threading
    calls = {"count": 0}

    def should_not_run(*_args, **_kwargs):
        calls["count"] += 1
        raise AssertionError("Unstable baselines must not trigger candidate probes.")

    monkeypatch.setattr(benchmark, "_run_phase", should_not_run)
    seq_runs = [{"result": {"count": 1}}] * 3
    seq_summary = {"median_wall_seconds": 1.0, "wall_time_spread_percent": 81.4}
    par_summary = {"median_wall_seconds": 2.0, "wall_time_spread_percent": 10.0}
    result = benchmark._probe_recommendation_options(
        "unstable", {"threads": 2}, {"kind": "sales"}, {}, [], 1_000_000, threading.Event(),
        seq_runs, seq_summary, [{"result": {"count": 1}}] * 3, par_summary,
        True, True, 3,
    )
    assert result["status"] == "skipped_unstable"
    assert calls["count"] == 0


def test_comprehensive_benchmark_works_with_legacy_windows_native_binary(tmp_path, monkeypatch):
    """An older .pyd can run via existing kernels instead of failing at dispatch."""
    import numpy as np

    monkeypatch.setattr(datasets, "DATA_ROOT", tmp_path / "generated")
    monkeypatch.setattr(datasets, "META_ROOT", tmp_path / "metadata")
    monkeypatch.setattr(benchmark, "WORKSPACE", tmp_path / "results")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "history" / "experiments.db")
    monkeypatch.setattr(benchmark, "pacsv", None)
    store.initialize()
    native = benchmark.kernels

    class LegacyNativeAdapter:
        def __getattr__(self, name):
            if name in {"comprehensive_analytics_seq", "comprehensive_analytics_parallel"}:
                raise AttributeError(name)
            return getattr(native, name)

    monkeypatch.setattr(benchmark, "kernels", LegacyNativeAdapter())
    ds = datasets.generate_dataset("sales", 70_000, seed=51, batch_size=10_000)
    config = {
        "dataset_id": ds["dataset_id"], "operation": "comprehensive_analytics",
        "column": "Revenue", "group_column": "Category", "threshold": 50_000,
        "threads": 4, "schedule": "static", "repetitions": 1,
    }
    jid = benchmark.create_job(config)
    benchmark._worker(jid)
    job = benchmark.get_job(jid)
    assert job["status"] == "completed", job.get("error")
    assert job["result"]["verified"] is True
    assert job["result"]["methodology"]["comprehensive_kernel_implementation"] == "legacy_three_kernel_compatibility"
    assert job["result"]["sequential"]["median_wall_seconds"] > 0
    assert job["result"]["parallel"]["median_wall_seconds"] > 0

def test_terminal_job_status_is_saved_as_progress_snapshot():
    job_id = "terminal-progress-snapshot-test"
    with benchmark._job_lock:
        benchmark._jobs[job_id] = {
            "job_id": job_id, "status": "failed", "config": {}, "progress": 0.0,
            "latest": {}, "events": [], "result": None, "error": None,
            "event_sequence": 0, "progress_state": {},
            "cancel_event": __import__("threading").Event(),
        }
    try:
        benchmark._emit(job_id, {"type": "job_status", "status": "failed", "phase": "FAILED", "progress": 0.0})
        snapshot = benchmark.get_job(job_id)["progress_state"]
        assert snapshot["status"] == "failed"
        assert snapshot["phase"] == "FAILED"
    finally:
        with benchmark._job_lock:
            benchmark._jobs.pop(job_id, None)


def test_next_size_recommendation_is_generated_and_benchmarked_before_suggesting(tmp_path, monkeypatch):
    import threading
    from pathlib import Path

    monkeypatch.setattr(benchmark.datasets, "RECOMMENDED_DEMO_ROW_PRESETS", [100_000, 1_000_000])
    monkeypatch.setattr(benchmark.psutil, "cpu_count", lambda logical=True: 8)
    generated = {"data_root": None, "meta_root": None, "rows": None}

    def fake_generate_dataset(**kwargs):
        generated["data_root"] = Path(kwargs["data_root"])
        generated["meta_root"] = Path(kwargs["meta_root"])
        generated["rows"] = kwargs["rows"]
        generated["data_root"].mkdir(parents=True, exist_ok=True)
        generated["meta_root"].mkdir(parents=True, exist_ok=True)
        fake_csv = generated["data_root"] / "trial.csv"
        fake_csv.write_text("Revenue,Region\\n10,North\\n", encoding="utf-8")
        return {
            "dataset_id": "temporary-trial", "kind": "sales", "rows": kwargs["rows"],
            "path": str(fake_csv), "numeric_columns": ["Revenue"],
            "category_columns": ["Region"], "date_columns": [],
            "seed": kwargs["seed"],
        }

    monkeypatch.setattr(benchmark.datasets, "generate_dataset", fake_generate_dataset)
    monkeypatch.setattr(
        benchmark, "_prepare_dataset",
        lambda *_args, **_kwargs: ({"numeric": object(), "codes": object()},
                                    [{"start": 0, "end": 1_000_000, "labels": ["North"]}],
                                    None, 0.1, "RAM arrays"),
    )

    def fake_run(_job_id, mode, cfg, _prepared, _segments, repetition, _rows, _cancel,
                 *, report_progress=True, run_type="measured"):
        if run_type == "recommendation_warmup":
            seconds = 0.5 if mode == "sequential" else 0.5
        elif mode == "sequential":
            seconds = 1.0 + (repetition - 2) * 0.01
        else:
            worker_times = {2: 0.80, 4: 0.70, 6: 0.90}
            seconds = worker_times[cfg["threads"]] + (repetition - 2) * 0.005
        return {
            "mode": mode, "repetition": repetition, "thread_count": cfg["threads"],
            "observed_threads_max": cfg["threads"] if mode == "parallel" else 1,
            "wall_seconds": seconds, "kernel_seconds": seconds * 0.8,
            "result": {"records": 1_000_000, "sum": 10.0},
        }

    monkeypatch.setattr(benchmark, "_run_phase", fake_run)
    result = benchmark._probe_next_dataset_size(
        "next-size-test",
        {"threads": 2, "operation": "comprehensive_analytics", "column": "Revenue",
         "group_column": "Region", "threshold": 0.0, "schedule": "static"},
        {"kind": "sales", "seed": 7, "batch_size": 25_000},
        100_000, threading.Event(), 3,
    )

    assert generated["rows"] == 1_000_000
    assert result["status"] == "verified_next_size"
    assert result["recommended_configuration"]["threads"] == 4
    assert result["recommended_configuration"]["dataset_rows"] == 1_000_000
    assert result["recommended_configuration"]["automatically_generated_trial"] is True
    assert len(result["tested_candidates"]) == 3
    assert result["temporary_dataset_removed"] is True
    assert not generated["data_root"].exists()
    assert not generated["meta_root"].exists()



def test_next_size_trial_resource_error_does_not_produce_a_guess(monkeypatch):
    import threading
    monkeypatch.setattr(benchmark, "ENABLE_NEXT_SIZE_PROBE", True)

    monkeypatch.setattr(benchmark.psutil, "cpu_count", lambda logical=True: 1)
    monkeypatch.setattr(
        benchmark, "_probe_next_dataset_size",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("Insufficient disk space estimate")),
    )
    seq_runs = [
        {"mode": "sequential", "repetition": i, "wall_seconds": 1.0 + (i - 2) * 0.01,
         "kernel_seconds": 0.8, "result": {"count": 100}}
        for i in range(1, 4)
    ]
    par_runs = [
        {"mode": "parallel", "repetition": i, "wall_seconds": 1.3 + (i - 2) * 0.01,
         "kernel_seconds": 1.1, "observed_threads_max": 2, "result": {"count": 100}}
        for i in range(1, 4)
    ]
    seq_summary = benchmark._summarize_runs(seq_runs, 100_000)
    par_summary = benchmark._summarize_runs(par_runs, 100_000)
    result = benchmark._probe_recommendation_options(
        "resources-test", {"threads": 2, "operation": "numeric_stats"},
        {"kind": "sales", "seed": 1}, {"numeric": object()}, [], 100_000,
        threading.Event(), seq_runs, seq_summary, par_runs, par_summary,
        True, True, 3,
    )
    assert result["status"] == "skipped_resources"
    assert result["recommended_configuration"] is None
    assert "Insufficient disk space" in result["message"]

def test_next_size_recommendation_respects_automatic_trial_row_limit(monkeypatch):
    import threading
    monkeypatch.setenv("PBDA_RECOMMENDATION_MAX_AUTO_ROWS", "500000")
    monkeypatch.setattr(benchmark.datasets, "RECOMMENDED_DEMO_ROW_PRESETS", [100_000, 1_000_000])
    result = benchmark._probe_next_dataset_size(
        "size-cap-test", {"threads": 2}, {"kind": "sales"},
        100_000, threading.Event(), 3,
    )
    assert result["status"] == "skipped_size_limit"
    assert result["next_dataset_rows"] == 1_000_000


def test_memory_safe_profile_bounds_prepared_arrays_and_chunks():
    limits = benchmark.resource_limits()
    assert limits["prepared_ram_limit_mb"] <= 2048
    assert limits["parser_block_size_bytes"] <= 8 * 1024 * 1024
    assert limits["compute_chunk_rows"] <= 8_388_608
    assert limits["automatic_next_size_probe"] is False
    assert limits["max_threads"] <= 4


def test_parallel_efficiency_summary_uses_observed_workers():
    runs = [
        {"repetition": 1, "wall_seconds": 0.5, "kernel_seconds": 0.4, "observed_threads_max": 2},
        {"repetition": 2, "wall_seconds": 0.5, "kernel_seconds": 0.4, "observed_threads_max": 3},
        {"repetition": 3, "wall_seconds": 0.5, "kernel_seconds": 0.4, "observed_threads_max": 2},
    ]
    summary = benchmark._summarize_runs(runs, 1_000_000)
    assert summary["median_observed_workers"] == 2
