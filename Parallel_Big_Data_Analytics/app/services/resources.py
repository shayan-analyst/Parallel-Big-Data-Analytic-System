"""Pre-flight resource planning and CPU-interference measurement.

Purpose: the benchmark must never silently produce a misleading verdict because
the computer is short of RAM / disk or is busy with other programs. Everything
here is *measured* from the operating system (psutil); nothing is assumed.

Two responsibilities:

1. ``plan_run``   - before a benchmark starts, compare what the run needs with
   what the machine currently has and say so in plain words ("needs ~X MB RAM,
   only Y MB free"). Hard shortages block the run; soft problems become warnings.
2. ``CpuWindow``  - during each timed phase, measure how many CPU cores were
   really used by this process and how much CPU other programs consumed, so a
   result taken on a busy laptop is flagged instead of being trusted blindly.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import psutil

MB = 1024 * 1024

# Estimated fixed cost of Python + NumPy + CSV parser blocks + result merging.
# This is an estimate used for planning messages, not a measured guarantee.
FIXED_OVERHEAD_BYTES = 160 * MB
# Never plan to hold prepared arrays in more than this share of *currently
# available* RAM; the rest is left for the OS, browser and other programs.
RAM_SAFETY_FRACTION = 0.50
# Other programs using at least this share of the CPU makes timings doubtful.
CPU_BUSY_WARN_PERCENT = 20.0
CPU_BUSY_UNRELIABLE_PERCENT = 35.0

NUMERIC_OPERATIONS = {"numeric_stats", "filter_gt", "group_sum", "top_k_groups", "comprehensive_analytics"}
CODE_OPERATIONS = {"frequency", "group_sum", "top_k_groups", "date_frequency", "comprehensive_analytics"}


def bytes_per_row(operation: str) -> int:
    """Prepared bytes per record: 8 for the float64 measure, 4 for the int32 category code."""
    return (8 if operation in NUMERIC_OPERATIONS else 0) + (4 if operation in CODE_OPERATIONS else 0)


def _mb(value: float | int | None) -> int | None:
    return None if value is None else int(round(float(value) / MB))


def choose_storage(prepared_bytes: int, available_bytes: int, ram_cap_bytes: int) -> tuple[bool, int]:
    """Return (hold_in_ram, ram_budget_bytes) for the prepared arrays."""
    budget = min(int(ram_cap_bytes), int(available_bytes * RAM_SAFETY_FRACTION))
    return prepared_bytes <= budget, budget


def memory_snapshot() -> dict[str, int]:
    vm = psutil.virtual_memory()
    available = int(getattr(vm, "available", 0) or 0)
    total = int(getattr(vm, "total", 0) or available)
    return {"available": available, "total": total}


def _existing_parent(path: Path) -> Path:
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def disk_free_bytes(path: Path) -> int | None:
    try:
        return int(psutil.disk_usage(str(_existing_parent(path))).free)
    except Exception:
        return None


def _system_times() -> tuple[float, float]:
    t = psutil.cpu_times()
    idle = float(getattr(t, "idle", 0.0)) + float(getattr(t, "iowait", 0.0))
    total = float(sum(t)) - float(getattr(t, "guest", 0.0)) - float(getattr(t, "guest_nice", 0.0))
    return total, idle


def _process_cpu_seconds(proc: psutil.Process) -> float:
    t = proc.cpu_times()
    return float(t.user) + float(t.system)


class CpuWindow:
    """Measure CPU usage of this process and of the whole machine over a window.

    Uses raw OS CPU-time counters (not ``psutil.cpu_percent``) so it cannot be
    disturbed by the hardware monitor, which also polls ``cpu_percent``.
    """

    def __init__(self) -> None:
        self._proc = psutil.Process()
        self._t0 = 0.0
        self._sys0 = (0.0, 0.0)
        self._p0 = 0.0

    def start(self) -> "CpuWindow":
        self._sys0 = _system_times()
        self._p0 = _process_cpu_seconds(self._proc)
        self._t0 = time.perf_counter()
        return self

    def stop(self) -> dict[str, float]:
        wall = max(1e-9, time.perf_counter() - self._t0)
        total1, idle1 = _system_times()
        proc_seconds = max(0.0, _process_cpu_seconds(self._proc) - self._p0)
        d_total = total1 - self._sys0[0]
        d_idle = idle1 - self._sys0[1]
        busy_fraction = min(1.0, max(0.0, 1.0 - d_idle / d_total)) if d_total > 0 else 0.0
        logical = max(1, int(psutil.cpu_count(logical=True) or 1))
        process_cores = proc_seconds / wall
        system_cores = busy_fraction * logical
        return {
            "wall_seconds": wall,
            "system_busy_percent": busy_fraction * 100.0,
            "process_cores_used": process_cores,
            # CPU consumed by everything that is not this benchmark process.
            "background_cores": max(0.0, system_cores - process_cores),
            "logical_processors": logical,
            # Raw sums, so many short windows can be combined without the
            # coarse (10-15 ms) OS counter granularity distorting any one of them.
            "process_cpu_seconds": proc_seconds,
            "system_busy_cpu_seconds": busy_fraction * d_total if d_total > 0 else 0.0,
        }


def combine_windows(windows: list[dict[str, float]], min_total_wall_seconds: float = 0.3) -> dict[str, Any] | None:
    """Aggregate several CpuWindow results into one trustworthy utilisation summary."""
    if not windows:
        return None
    wall = sum(float(w["wall_seconds"]) for w in windows)
    logical = int(windows[0]["logical_processors"])
    proc = sum(float(w["process_cpu_seconds"]) for w in windows)
    busy = sum(float(w["system_busy_cpu_seconds"]) for w in windows)
    process_cores = proc / wall if wall > 0 else 0.0
    system_cores = busy / wall if wall > 0 else 0.0
    return {
        # Below ~0.3 s of total timed work the OS counters are too coarse to judge anything.
        "measurable": wall >= min_total_wall_seconds,
        "measured_wall_seconds": wall,
        "process_cores_used": process_cores,
        "background_cores": max(0.0, system_cores - process_cores),
        "system_busy_percent": min(100.0, system_cores / logical * 100.0),
        "logical_processors": logical,
    }


def sample_cpu(seconds: float = 0.25) -> dict[str, float]:
    window = CpuWindow().start()
    time.sleep(max(0.05, seconds))
    return window.stop()


def plan_run(*, rows: int, operation: str, threads: int, workspace: Path,
             ram_cap_bytes: int, compute_chunk_rows: int, max_threads: int,
             cpu_sample_seconds: float = 0.25) -> dict[str, Any]:
    """Compare what this run needs with what the computer has right now."""
    bpr = bytes_per_row(operation)
    prepared = int(rows) * bpr
    mem = memory_snapshot()
    available = mem["available"]
    use_ram, ram_budget = choose_storage(prepared, available, ram_cap_bytes)
    chunk_bytes = min(int(rows), int(compute_chunk_rows)) * bpr

    ram_needed_in_memory = prepared + FIXED_OVERHEAD_BYTES
    ram_free_needed_in_memory = max(int(prepared / RAM_SAFETY_FRACTION), ram_needed_in_memory)
    ram_minimum = FIXED_OVERHEAD_BYTES + chunk_bytes
    disk_needed = int(prepared * 1.1) + 64 * MB
    disk_free = disk_free_bytes(workspace)

    messages: list[dict[str, str]] = []
    blocked: list[str] = []

    # ---- RAM / disk ------------------------------------------------------
    if available < ram_minimum:
        text = (
            f"Not enough free RAM: even the low-memory mode needs about {_mb(ram_minimum)} MB free, "
            f"but only {_mb(available)} MB is available. Close other applications (browser tabs, IDEs) "
            f"to free at least {_mb(ram_minimum - available)} MB more, then run again."
        )
        messages.append({"level": "error", "code": "insufficient_ram", "text": text})
        blocked.append(text)
        storage_mode = "unavailable"
    elif use_ram:
        storage_mode = "RAM arrays"
    else:
        storage_mode = "memory-mapped arrays"
        if prepared > ram_cap_bytes:
            why = (f"the prepared data (~{_mb(prepared)} MB) is above the in-memory limit of "
                   f"{_mb(ram_cap_bytes)} MB (PBDA_PREPARED_RAM_LIMIT_MB)")
        else:
            why = (f"only {_mb(available)} MB RAM is free and {int(RAM_SAFETY_FRACTION * 100)}% of it "
                   f"({_mb(ram_budget)} MB) is the safe budget for ~{_mb(prepared)} MB of prepared data")
        messages.append({
            "level": "warning", "code": "memory_mapped_storage",
            "text": (f"Running with disk-backed (memory-mapped) data because {why}. Timings can be affected by "
                     f"disk/page-cache behaviour. For exact in-memory timing about {_mb(ram_free_needed_in_memory)} MB "
                     f"of free RAM is needed (currently {_mb(available)} MB)."),
        })
        if disk_free is not None and disk_free < disk_needed:
            text = (f"Not enough free disk space for the memory-mapped working files: about {_mb(disk_needed)} MB "
                    f"needed, only {_mb(disk_free)} MB free on the workspace drive. Free "
                    f"{_mb(disk_needed - disk_free)} MB of disk space or free RAM so the data fits in memory.")
            messages.append({"level": "error", "code": "insufficient_disk", "text": text})
            blocked.append(text)

    if mem["total"] and available < 0.12 * mem["total"] and not blocked:
        messages.append({
            "level": "warning", "code": "memory_pressure",
            "text": (f"The computer is under memory pressure ({_mb(available)} MB of {_mb(mem['total'])} MB free). "
                     "The operating system may swap pages and slow some runs more than others."),
        })

    # ---- CPU -------------------------------------------------------------
    cpu = sample_cpu(cpu_sample_seconds)
    logical = int(cpu["logical_processors"])
    busy_percent = float(cpu["system_busy_percent"])
    background_cores = float(cpu["background_cores"])
    free_cores = max(0.0, logical - background_cores)
    suggested_threads = int(max(1, min(max_threads, round(free_cores))))
    if background_cores / logical * 100.0 >= CPU_BUSY_UNRELIABLE_PERCENT:
        messages.append({
            "level": "warning", "code": "cpu_busy_high",
            "text": (f"Other programs are already using about {background_cores:.1f} of {logical} CPU cores "
                     f"({busy_percent:.0f}% total CPU). Multi-thread timing will be unreliable. Close heavy "
                     "programs (browser, IDE, antivirus scan, downloads) and run again for a fair comparison."),
        })
    elif background_cores / logical * 100.0 >= CPU_BUSY_WARN_PERCENT:
        messages.append({
            "level": "warning", "code": "cpu_busy",
            "text": (f"Other programs are using about {background_cores:.1f} of {logical} CPU cores. "
                     "Results may vary more than usual."),
        })
    if int(threads) > 1 and int(threads) > free_cores + 0.5:
        messages.append({
            "level": "warning", "code": "threads_exceed_free_cores",
            "text": (f"{int(threads)} threads requested but only about {free_cores:.1f} cores are free right now. "
                     f"Suggested: {suggested_threads} thread(s), or close other programs first."),
        })

    levels = {m["level"] for m in messages}
    status = "blocked" if blocked else ("warnings" if "warning" in levels else "ready")
    if status == "blocked":
        headline = " ".join(blocked)
    elif status == "warnings":
        headline = "Ready, with warnings: " + messages[0]["text"]
    else:
        headline = "Resources are sufficient for a reliable measurement."
    return {
        "status": status,
        "can_run": not blocked,
        "headline": headline,
        "storage_mode": storage_mode,
        "memory": {
            "available_mb": _mb(available), "total_mb": _mb(mem["total"]),
            "prepared_data_mb": _mb(prepared),
            "needed_in_memory_mode_mb": _mb(ram_needed_in_memory),
            "free_needed_for_in_memory_mode_mb": _mb(ram_free_needed_in_memory),
            "minimum_required_mb": _mb(ram_minimum),
            "in_memory_cap_mb": _mb(ram_cap_bytes),
            "largest_rows_that_fit_in_memory_now": int(max(0, min(ram_budget, ram_cap_bytes) // bpr)) if bpr else None,
        },
        "disk": {
            "free_mb": _mb(disk_free), "needed_mb_if_memory_mapped": _mb(disk_needed),
        },
        "cpu": {
            "logical_processors": logical,
            "system_busy_percent": round(busy_percent, 1),
            "background_cores": round(background_cores, 2),
            "free_cores": round(free_cores, 2),
        },
        "threads": {"requested": int(threads), "suggested": suggested_threads, "max_allowed": int(max_threads)},
        "messages": messages,
        "blocked_message": " ".join(blocked) if blocked else None,
    }
