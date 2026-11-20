"""Resource planner, CPU-interference meter and honest-verdict analysis."""
from types import SimpleNamespace
from pathlib import Path

from app.services import benchmark, resources

MB = 1024 * 1024


def _plan(monkeypatch, available_mb, rows=25_000_000, op="comprehensive_analytics", threads=4):
    monkeypatch.setattr(resources.psutil, "virtual_memory",
                        lambda: SimpleNamespace(available=available_mb * MB, total=8192 * MB))
    return resources.plan_run(
        rows=rows, operation=op, threads=threads, workspace=Path("workspace/results"),
        ram_cap_bytes=512 * MB, compute_chunk_rows=4_194_304, max_threads=4, cpu_sample_seconds=0.05,
    )


def test_bytes_per_row():
    assert resources.bytes_per_row("numeric_stats") == 8
    assert resources.bytes_per_row("frequency") == 4
    assert resources.bytes_per_row("comprehensive_analytics") == 12


def test_25m_rows_fit_in_ram_when_enough_memory_is_free(monkeypatch):
    plan = _plan(monkeypatch, 3000)
    assert plan["can_run"] and plan["storage_mode"] == "RAM arrays"


def test_25m_rows_fall_back_to_memory_map_and_explain_why(monkeypatch):
    plan = _plan(monkeypatch, 500)
    assert plan["can_run"] and plan["storage_mode"] == "memory-mapped arrays"
    text = " ".join(m["text"] for m in plan["messages"])
    assert "MB of free RAM is needed" in text and "500 MB" in text


def test_run_is_blocked_with_exact_numbers_when_ram_is_too_low(monkeypatch):
    plan = _plan(monkeypatch, 100)
    assert not plan["can_run"] and plan["status"] == "blocked"
    assert "only 100 MB is available" in plan["blocked_message"]
    assert "free at least" in plan["blocked_message"]


def test_blocked_when_memory_map_needs_more_disk_than_free(monkeypatch):
    monkeypatch.setattr(resources, "disk_free_bytes", lambda _path: 10 * MB)
    plan = _plan(monkeypatch, 500)
    assert not plan["can_run"]
    assert "disk space" in plan["blocked_message"]


def test_combine_windows_flags_too_short_measurements():
    window = {"wall_seconds": 0.05, "process_cpu_seconds": 0.05, "system_busy_cpu_seconds": 0.05,
              "logical_processors": 4}
    assert resources.combine_windows([window])["measurable"] is False
    assert resources.combine_windows([window] * 10)["measurable"] is True
    assert resources.combine_windows([]) is None


def _summary(wall, cores_used, background, logical=4, measurable=True):
    return {
        "median_wall_seconds": wall, "median_kernel_seconds": wall * 0.9,
        "wall_time_spread_percent": 5.0, "runs": [{}, {}, {}],
        "cpu_utilization": {"measurable": measurable, "process_cores_used": cores_used,
                            "background_cores": background, "logical_processors": logical,
                            "system_busy_percent": 50.0},
    }


def _analyse(seq, par, threads=4):
    return benchmark._build_performance_analysis(
        {"operation": "comprehensive_analytics", "threads": threads, "schedule": "static"}, 25_000_000,
        seq, par, seq["median_wall_seconds"] / par["median_wall_seconds"],
        seq["median_kernel_seconds"] / par["median_kernel_seconds"], True, True, "RAM arrays",
    )


def test_busy_machine_makes_a_slow_parallel_verdict_unreliable():
    analysis = _analyse(_summary(0.099, 1.0, 2.0), _summary(0.128, 1.4, 2.0))
    quality = analysis["execution_quality"]
    assert quality["verdict_reliable"] is False and quality["interference"] == "high"
    assert analysis["confidence"] == "low"
    assert "too busy" in analysis["headline"]
    assert any("Other programs were using" in reason for reason in analysis["reasons"])


def test_threads_that_did_not_get_cores_are_reported_as_starved():
    analysis = _analyse(_summary(0.4, 1.0, 0.0), _summary(0.5, 1.3, 0.0))
    assert analysis["execution_quality"]["cpu_starved"] is True


def test_idle_machine_with_real_speedup_is_reliable():
    analysis = _analyse(_summary(0.4, 1.0, 0.0), _summary(0.12, 3.7, 0.0))
    assert analysis["execution_quality"]["verdict_reliable"] is True
    assert analysis["outcome"] == "parallel_faster" and analysis["confidence"] == "good"


def test_unmeasurable_cpu_counters_never_accuse_the_machine():
    analysis = _analyse(_summary(0.1, 0.0, 0.0, measurable=False), _summary(0.13, 0.0, 0.0, measurable=False))
    assert analysis["execution_quality"]["verdict_reliable"] is True
