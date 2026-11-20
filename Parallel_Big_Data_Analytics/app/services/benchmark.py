from __future__ import annotations

import asyncio
import csv
import json
import logging
import math
import os
import statistics
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import psutil
try:
    import pyarrow.csv as pacsv
except Exception:
    pacsv = None

try:
    from app.engine import openmp_kernels as kernels
except Exception:
    kernels = None
from app.services.datasets import get_dataset
from app.services import datasets, resources, store

WORKSPACE = Path("workspace/results").resolve()
_logger = logging.getLogger("pbda.benchmark")
WORKSPACE.mkdir(parents=True, exist_ok=True)
def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


# Adaptive memory policy. Prepared arrays are held in plain RAM whenever they fit
# inside min(this cap, 50% of the RAM that is free *right now*); only then do they
# fall back to temporary memory-mapped files. In-memory arrays give stable,
# comparable timings. A 25M-record run needs ~300 MB (8 B measure + 4 B code per row),
# so the default cap covers it. The old fixed 96 MB cap forced 25M-row runs onto
# disk-backed pages, where page faults distorted the multi-thread timings.
PREPARED_RAM_LIMIT_BYTES = _env_int(
    "PBDA_PREPARED_RAM_LIMIT_MB", 512, 16, 2048
) * 1024 * 1024
ARROW_BLOCK_SIZE_BYTES = _env_int(
    "PBDA_ARROW_BLOCK_SIZE_BYTES", 2 * 1024 * 1024, 1 * 1024 * 1024, 8 * 1024 * 1024
)
COMPUTE_CHUNK_ROWS = _env_int(
    "PBDA_COMPUTE_CHUNK_ROWS", 4_194_304, 131_072, 8_388_608
)  # 4M rows = at most ~48 MiB for numeric + category arrays.
ENABLE_NEXT_SIZE_PROBE = os.getenv("PBDA_ENABLE_NEXT_SIZE_PROBE", "0").strip().lower() in {"1", "true", "yes", "on"}

_jobs: dict[str, dict[str, Any]] = {}
_job_lock = threading.RLock()
_subscribers: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = defaultdict(list)


def _emit(job_id: str, payload: dict[str, Any]) -> None:
    with _job_lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["event_sequence"] = int(job.get("event_sequence", 0)) + 1
            event = {**payload, "event_id": job["event_sequence"], "timestamp": time.time()}
            job["latest"] = event
            if event.get("type") in {
                "phase_start", "phase_progress", "preparation_start", "preparation_progress", "preparation_complete",
                "recommendation_preparation_start", "recommendation_preparation_progress", "recommendation_preparation_complete",
                "recommendation_generation_progress",
            } or (
                event.get("type") == "job_status" and event.get("status") in {"failed", "cancelled", "verification_failed"}
            ):
                job["progress_state"] = dict(event)
            job["events"].append(event)
            if len(job["events"]) > 400:
                job["events"] = job["events"][-300:]
        else:
            event = {**payload, "timestamp": time.time()}
        queues = list(_subscribers.get(job_id, []))
    for loop, queue in queues:
        if loop.is_closed():
            continue

        def deliver(target: asyncio.Queue = queue, item: dict[str, Any] = event) -> None:
            try:
                target.put_nowait(item)
            except asyncio.QueueFull:
                try:
                    target.get_nowait()
                    target.put_nowait(item)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

        # Workers run in a ThreadPoolExecutor. asyncio.Queue is not thread-safe,
        # so every notification must be delivered on the owning event loop.
        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            continue


def subscribe(job_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    loop = asyncio.get_running_loop()
    with _job_lock:
        _subscribers[job_id].append((loop, q))
    return q


def unsubscribe(job_id: str, q: asyncio.Queue) -> None:
    with _job_lock:
        subscribers = _subscribers.get(job_id, [])
        _subscribers[job_id] = [(loop, queue) for loop, queue in subscribers if queue is not q]


def get_job(job_id: str) -> dict[str, Any] | None:
    with _job_lock:
        item = _jobs.get(job_id)
        if not item:
            return None
        return {k: v for k, v in item.items() if k not in {"cancel_event", "events"}}


def cancel_active_jobs() -> None:
    """Request cooperative cancellation of every queued or running benchmark."""
    with _job_lock:
        for item in _jobs.values():
            if item.get("status") in {"queued", "running"}:
                item["cancel_event"].set()


def has_active_jobs() -> bool:
    with _job_lock:
        return any(x.get("status") in {"queued", "running"} for x in _jobs.values())


def get_active_job() -> dict[str, Any] | None:
    with _job_lock:
        for job_id in reversed(list(_jobs.keys())):
            item = _jobs[job_id]
            if item.get("status") in {"queued", "running"}:
                return get_job(job_id)
    return None


def list_jobs() -> list[dict[str, Any]]:
    with _job_lock:
        return [get_job(jid) for jid in list(_jobs.keys())[-30:]]


def cancel_job(job_id: str) -> bool:
    with _job_lock:
        item = _jobs.get(job_id)
        if not item or item["status"] in {"completed", "failed", "cancelled"}:
            return False
        item["cancel_event"].set()
        return True


def create_job(config: dict[str, Any]) -> str:
    dataset = get_dataset(config["dataset_id"])
    if kernels is None or not kernels.engine_info().get("openmp"):
        raise RuntimeError("The compiled OpenMP engine is unavailable. Build it using setup_windows.bat before benchmarking.")
    operation = config["operation"]
    if operation not in {"numeric_stats", "filter_gt", "frequency", "group_sum", "top_k_groups", "date_frequency", "comprehensive_analytics"}:
        raise ValueError("Unsupported analytical operation.")
    numeric_operations = {"numeric_stats", "filter_gt", "group_sum", "top_k_groups", "comprehensive_analytics"}
    category_operations = {"frequency", "group_sum", "top_k_groups", "comprehensive_analytics"}
    if operation in numeric_operations and config.get("column") not in dataset["numeric_columns"]:
        raise ValueError("Select a numeric column that exists in this dataset.")
    if operation in category_operations and config.get("group_column") not in dataset["category_columns"]:
        raise ValueError("Select a categorical column that exists in this dataset.")
    if operation == "date_frequency":
        if config.get("date_column") not in dataset.get("date_columns", []):
            raise ValueError("Select a date column that exists in this dataset.")
        if config.get("date_granularity", "month") not in {"year", "month", "day"}:
            raise ValueError("Date granularity must be year, month or day.")
    if operation == "top_k_groups" and not 1 <= int(config.get("top_k", 10)) <= 100:
        raise ValueError("Top-K must be between 1 and 100.")
    threads = int(config.get("threads", 2))
    logical_processors = max(1, int(psutil.cpu_count(logical=True) or 1))
    physical_cores = max(1, int(psutil.cpu_count(logical=False) or logical_processors))
    configured_thread_cap = _env_int("PBDA_MAX_BENCHMARK_THREADS", 4, 1, 16)
    max_threads = min(logical_processors, physical_cores, configured_thread_cap)
    if threads < 1 or threads > max_threads:
        raise ValueError(
            f"This memory-safe profile allows 1 to {max_threads} OpenMP threads on this computer. "
            "Choose a lower thread count to avoid CPU oversubscription."
        )
    repetitions = int(config.get("repetitions", 3))
    if repetitions < 1 or repetitions > 7:
        raise ValueError("Repetitions must be between 1 and 7.")
    schedule = config.get("schedule", "static")
    if schedule not in {"static", "dynamic", "guided"}:
        raise ValueError("Unsupported OpenMP schedule.")
    # Pre-flight: compare what this run needs with what the computer has right now.
    # A hard shortage (RAM / disk) is reported in plain words instead of running a
    # benchmark that cannot give a trustworthy answer.
    plan = preflight(config, dataset)
    if not plan["can_run"]:
        raise ValueError(plan["blocked_message"])
    job_id = uuid.uuid4().hex[:12]
    with _job_lock:
        _jobs[job_id] = {
            "job_id": job_id, "status": "queued", "created_at": datetime.now(timezone.utc).isoformat(),
            "config": config, "dataset": dataset, "progress": 0.0, "latest": {}, "resource_plan": plan,
            "events": [], "result": None, "error": None,
            "event_sequence": 0, "progress_state": {},
            "cancel_event": threading.Event(),
        }
    return job_id


def preflight(config: dict[str, Any], dataset: dict[str, Any]) -> dict[str, Any]:
    """Resource plan for a benchmark configuration (no job is created)."""
    limits = resource_limits()
    return resources.plan_run(
        rows=int(dataset["rows"]), operation=config["operation"],
        threads=int(config.get("threads", 2)), workspace=WORKSPACE,
        ram_cap_bytes=PREPARED_RAM_LIMIT_BYTES, compute_chunk_rows=COMPUTE_CHUNK_ROWS,
        max_threads=int(limits["max_threads"]),
    )


class _DictBatch:
    def __init__(self, columns: dict[str, list[str]]):
        self.columns = columns
        self.num_rows = len(next(iter(columns.values()))) if columns else 0


def _read_arrow_batches(path: str, batch_size: int):
    # Preferred path: PyArrow streaming reader. The standard-library path keeps
    # the application usable in constrained environments without changing the
    # compiled processing kernels.
    if pacsv is not None:
        # Keep parser and Python conversion temporaries bounded. This setting
        # affects only benchmark preparation; CSV generation remains unchanged.
        reader = pacsv.open_csv(
            path, read_options=pacsv.ReadOptions(
                block_size=ARROW_BLOCK_SIZE_BYTES, use_threads=False
            )
        )
        for batch in reader:
            yield batch
        return
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns: dict[str, list[str]] = {}
        count = 0
        for row in reader:
            for key, value in row.items():
                columns.setdefault(key, []).append(value)
            count += 1
            if count >= 16384:
                yield _DictBatch(columns)
                columns, count = {}, 0
        if count:
            yield _DictBatch(columns)


def _column_values(batch, column: str):
    if isinstance(batch, _DictBatch):
        return batch.columns[column]
    arr = batch.column(batch.schema.get_field_index(column))
    if arr.null_count:
        arr = arr.fill_null(0)
    return arr


def _to_float_array(batch, column: str) -> np.ndarray:
    arr = _column_values(batch, column)
    if isinstance(batch, _DictBatch):
        return np.ascontiguousarray(np.asarray(arr, dtype=np.float64))
    return np.ascontiguousarray(arr.to_numpy(zero_copy_only=False), dtype=np.float64)


def _to_codes(batch, column: str, date_granularity: str | None = None) -> tuple[np.ndarray, list[str]]:
    """Encode one small parser batch without large Unicode/np.unique temporaries.

    The built-in templates have low-cardinality category columns. A dictionary
    encoder therefore keeps peak memory bounded while retaining exact labels
    and a stable code for every value within this batch.
    """
    arr = _column_values(batch, column)
    values = arr if isinstance(batch, _DictBatch) else arr.to_pylist()
    width = None
    if date_granularity:
        lengths = {"year": 4, "month": 7, "day": 10}
        if date_granularity not in lengths:
            raise ValueError("Date granularity must be year, month or day.")
        width = lengths[date_granularity]

    labels: list[str] = []
    label_to_local_code: dict[str, int] = {}
    codes = np.empty(len(values), dtype=np.int32)
    for index, value in enumerate(values):
        label = "<NULL>" if value is None else str(value)
        if width is not None:
            label = label[:width]
        code = label_to_local_code.get(label)
        if code is None:
            code = len(labels)
            label_to_local_code[label] = code
            labels.append(label)
        codes[index] = code
    return codes, labels


def _supports_fused_comprehensive_kernel() -> bool:
    """Check the loaded native binary, not only the newer .pyx source file.

    Windows users may still have a previously-built .pyd that predates the
    fused comprehensive kernels. The legacy binary remains useful because it
    contains the numeric, threshold and grouped-aggregation kernels.
    """
    return bool(
        kernels is not None
        and callable(getattr(kernels, "comprehensive_analytics_seq", None))
        and callable(getattr(kernels, "comprehensive_analytics_parallel", None))
    )


def _comprehensive_kernel_mode() -> str:
    return "fused_single_pass" if _supports_fused_comprehensive_kernel() else "legacy_three_kernel_compatibility"


def _run_comprehensive_compatibility(
    codes: np.ndarray, values: np.ndarray, category_count: int, threshold: float,
    *, parallel: bool, threads: int, schedule: str,
) -> tuple[tuple[Any, ...], int, float]:
    """Run comprehensive analytics with either the fused or legacy native API.

    The old Windows extension exposes the original numeric/threshold/group
    kernels but not the newly-added fused methods. This adapter guarantees
    that such a binary can still complete a fair benchmark instead of failing
    after the full CSV preparation. The compatibility path performs three
    native passes; the new extension performs one fused pass. Timings include
    only time spent inside the native calls.
    """
    started_total = time.perf_counter()
    if _supports_fused_comprehensive_kernel():
        if parallel:
            out = kernels.comprehensive_analytics_parallel(
                codes, values, category_count, threshold, threads, schedule
            )
            native_seconds = time.perf_counter() - started_total
            return tuple(out[:8]), int(out[8]), native_seconds
        out = kernels.comprehensive_analytics_seq(codes, values, category_count, threshold)
        native_seconds = time.perf_counter() - started_total
        return tuple(out[:8]), 1, native_seconds

    native_seconds = 0.0
    if parallel:
        call_started = time.perf_counter()
        numeric = kernels.numeric_stats_parallel(values, threads, schedule)
        native_seconds += time.perf_counter() - call_started

        call_started = time.perf_counter()
        threshold_result = kernels.threshold_count_parallel(values, threshold, threads, schedule)
        native_seconds += time.perf_counter() - call_started

        call_started = time.perf_counter()
        grouped = kernels.group_sum_parallel(codes, values, category_count, threads, schedule)
        native_seconds += time.perf_counter() - call_started
        observed = max(int(numeric[5]), int(threshold_result[1]), int(grouped[2]))
        combined = (numeric[0], numeric[1], numeric[2], numeric[3], numeric[4],
                    threshold_result[0], grouped[0], grouped[1])
        return combined, observed, native_seconds

    call_started = time.perf_counter()
    numeric = kernels.numeric_stats_seq(values)
    native_seconds += time.perf_counter() - call_started

    call_started = time.perf_counter()
    matched = kernels.threshold_count_seq(values, threshold)
    native_seconds += time.perf_counter() - call_started

    call_started = time.perf_counter()
    grouped = kernels.group_sum_seq(codes, values, category_count)
    native_seconds += time.perf_counter() - call_started
    combined = (numeric[0], numeric[1], numeric[2], numeric[3], numeric[4],
                matched, grouped[0], grouped[1])
    return combined, 1, native_seconds


def _merge_result(acc: dict[str, Any], chunk_result: Any, labels: list[str] | None = None) -> None:
    op = acc["operation"]
    if op == "numeric_stats":
        count, total, sumsq, minimum, maximum = chunk_result[:5]
        acc["count"] += int(count)
        acc["sum"] += float(total)
        acc["sum_squares"] += float(sumsq)
        if count:
            acc["min"] = minimum if acc["min"] is None else min(acc["min"], minimum)
            acc["max"] = maximum if acc["max"] is None else max(acc["max"], maximum)
    elif op == "filter_gt":
        acc["matched"] += int(chunk_result[0])
    elif op in {"frequency", "date_frequency"}:
        counts = chunk_result[0] if isinstance(chunk_result, tuple) else chunk_result
        for label, value in zip(labels or [], counts.tolist()):
            acc["frequency"][label] += int(value)
    elif op in {"group_sum", "top_k_groups"}:
        counts, sums = chunk_result[0], chunk_result[1]
        for label, count, total in zip(labels or [], counts.tolist(), sums.tolist()):
            acc["groups"][label]["count"] += int(count)
            acc["groups"][label]["sum"] += float(total)
    elif op == "comprehensive_analytics":
        count, total, sumsq, minimum, maximum, matched, group_counts, group_sums = chunk_result[:8]
        acc["count"] += int(count)
        acc["sum"] += float(total)
        acc["sum_squares"] += float(sumsq)
        acc["matched"] += int(matched)
        if count:
            acc["min"] = minimum if acc["min"] is None else min(acc["min"], minimum)
            acc["max"] = maximum if acc["max"] is None else max(acc["max"], maximum)
        for label, group_count, group_total in zip(labels or [], group_counts.tolist(), group_sums.tolist()):
            acc["groups"][label]["count"] += int(group_count)
            acc["groups"][label]["sum"] += float(group_total)


def _pretouch(array: np.ndarray) -> None:
    """Read one element per 4 KiB page so the whole array is resident before timing."""
    step = max(1, 4096 // max(1, array.itemsize))
    if array.size:
        float(np.add.reduce(array[::step], dtype=np.float64))


def _release_prepared_arrays(prepared: dict[str, Any] | None) -> None:
    """Close memory maps explicitly so Windows can remove their temporary files."""
    if not prepared:
        return
    for array in prepared.values():
        if isinstance(array, np.memmap):
            try:
                array.flush()
            except Exception:
                pass
            mmap_handle = getattr(array, "_mmap", None)
            if mmap_handle is not None:
                try:
                    mmap_handle.close()
                except Exception:
                    pass


def _prepare_dataset(job_id: str, config: dict[str, Any], dataset: dict[str, Any],
                    cancel: threading.Event, *, event_prefix: str = "preparation") -> tuple[dict[str, Any], list[dict[str, Any]], tempfile.TemporaryDirectory | None, float, str]:
    """Read/parse the CSV once, then share identical prepared arrays across both engines.

    Large prepared datasets use memory-mapped arrays so that a machine already
    under RAM pressure does not need another full in-memory copy of the columns.
    Category labels are mapped into one experiment-wide integer-code space so
    that a large contiguous compute pass can process the entire dataset without
    relaunching an OpenMP team for every CSV parser block.
    """
    operation = config["operation"]
    needs_numeric = operation in {"numeric_stats", "filter_gt", "group_sum", "top_k_groups", "comprehensive_analytics"}
    needs_codes = operation in {"frequency", "group_sum", "top_k_groups", "date_frequency", "comprehensive_analytics"}
    total_rows = int(dataset["rows"])
    bytes_per_row = (8 if needs_numeric else 0) + (4 if needs_codes else 0)
    estimated_bytes = total_rows * bytes_per_row
    try:
        available_bytes = int(psutil.virtual_memory().available)
    except Exception:
        available_bytes = 512 * 1024 * 1024
    # Hold the prepared columns in RAM whenever they fit inside min(cap, 50% of
    # currently free RAM); otherwise use temporary memory-mapped files.
    use_memory, memory_budget = resources.choose_storage(
        estimated_bytes, available_bytes, PREPARED_RAM_LIMIT_BYTES
    )
    if not use_memory:
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        temp_dir = tempfile.TemporaryDirectory(prefix=f"pbda_prepare_{job_id}_", dir=str(WORKSPACE))
    else:
        temp_dir = None
    storage_mode = "RAM arrays" if use_memory else "memory-mapped arrays"

    def allocate(name: str, dtype):
        if use_memory:
            return np.empty(total_rows, dtype=dtype)
        assert temp_dir is not None
        return np.memmap(Path(temp_dir.name) / f"{name}.bin", mode="w+", dtype=dtype, shape=(total_rows,))

    prepared: dict[str, Any] = {}
    segments: list[dict[str, Any]] = []
    global_labels: list[str] = []
    label_to_code: dict[str, int] = {}
    started = time.perf_counter()
    records_done = 0
    _emit(job_id, {
        "type": f"{event_prefix}_start", "phase": "PREPARING DATA", "mode": "preparation",
        "records_processed": 0, "total_records": total_rows, "progress": 0.0,
        "elapsed_seconds": 0.0, "records_per_second": 0.0,
        "message": "Reading and preparing the dataset once. The same prepared values will be used by both engines.",
    })
    try:
        # Allocate inside the protected block: if a second array allocation or a
        # disk write fails, close the first mapping before removing its folder.
        if needs_numeric:
            prepared["numeric"] = allocate("numeric", np.float64)
        if needs_codes:
            prepared["codes"] = allocate("codes", np.int32)
        for batch in _read_arrow_batches(dataset["path"], 1 << 17):
            if cancel.is_set():
                raise InterruptedError("Benchmark cancelled by user during dataset preparation.")
            row_count = int(batch.num_rows)
            if row_count <= 0:
                continue
            start_row = records_done
            end_row = start_row + row_count
            numeric = None
            codes = None
            labels = None
            if needs_numeric:
                numeric = _to_float_array(batch, config["column"])
                prepared["numeric"][start_row:end_row] = numeric
            if needs_codes:
                if operation == "date_frequency":
                    codes, batch_labels = _to_codes(batch, config["date_column"], config.get("date_granularity", "month"))
                else:
                    codes, batch_labels = _to_codes(batch, config["group_column"])
                # Convert each parser block's local codes into one experiment-wide
                # code space. Template category/date cardinality is bounded, so the
                # native kernels can then work on large contiguous compute blocks.
                remap = np.empty(len(batch_labels), dtype=np.int32)
                for label_index, label in enumerate(batch_labels):
                    mapped = label_to_code.get(label)
                    if mapped is None:
                        mapped = len(global_labels)
                        label_to_code[label] = mapped
                        global_labels.append(label)
                    remap[label_index] = mapped
                prepared["codes"][start_row:end_row] = remap[codes]
            records_done = end_row
            elapsed = time.perf_counter() - started
            rate = records_done / elapsed if elapsed > 0 else 0.0
            _emit(job_id, {
                "type": f"{event_prefix}_progress", "phase": "PREPARING DATA", "mode": "preparation",
                "records_processed": records_done, "total_records": total_rows,
                "progress": min(100.0, records_done / total_rows * 100) if total_rows else 100.0,
                "elapsed_seconds": elapsed, "records_per_second": rate,
                "estimated_remaining_seconds": (total_rows - records_done) / rate if rate > 0 else None,
                "storage_mode": storage_mode,
                "message": f"Prepared {records_done:,} of {total_rows:,} records. Both engines will reuse this data.",
            })
        if records_done != total_rows:
            raise RuntimeError(f"Dataset row count mismatch: metadata says {total_rows:,}, CSV contains {records_done:,}.")
        # One logical range means every timed repetition processes large contiguous
        # pieces rather than launching an OpenMP team for every CSV parser block.
        segments = [{"start": 0, "end": total_rows, "labels": global_labels if needs_codes else None}]
        for array in prepared.values():
            if isinstance(array, np.memmap):
                array.flush()
                # Fault every page into memory now, so page-fault cost is paid in
                # preparation and not inside the first timed pass of one engine.
                _pretouch(array)
        # Diagnostic metadata is deliberately kept with the prepared object;
        # it is not an array and is ignored by kernel dispatch/release routines.
        prepared["_pbda_profile"] = {
            "prepared_array_bytes": estimated_bytes,
            "prepared_ram_limit_bytes": memory_budget,
            "storage_mode": storage_mode,
            "parser_block_size_bytes": ARROW_BLOCK_SIZE_BYTES,
            "compute_chunk_rows": COMPUTE_CHUNK_ROWS,
            "compute_chunk_bytes_estimate": min(total_rows, COMPUTE_CHUNK_ROWS) * bytes_per_row,
        }
        elapsed = time.perf_counter() - started
        _emit(job_id, {
            "type": f"{event_prefix}_complete", "phase": "PREPARED", "mode": "preparation",
            "records_processed": total_rows, "total_records": total_rows, "progress": 100.0,
            "elapsed_seconds": elapsed, "storage_mode": storage_mode,
            "message": f"Dataset prepared once in {elapsed:.2f}s using {storage_mode.lower()}.",
        })
        return prepared, segments, temp_dir, elapsed, storage_mode
    except Exception:
        _release_prepared_arrays(prepared)
        if temp_dir is not None:
            temp_dir.cleanup()
        raise


def _run_phase(job_id: str, mode: str, config: dict[str, Any], prepared: dict[str, Any],
               segments: list[dict[str, Any]], repetition: int, total_rows: int,
               cancel: threading.Event, *, report_progress: bool = True,
               run_type: str = "measured") -> dict[str, Any]:
    """Run compute only: no CSV I/O and no parsing/conversion inside timed runs."""
    parallel = mode == "parallel"
    thread_count = int(config["threads"]) if parallel else 1
    operation = config["operation"]
    schedule = config.get("schedule", "static")
    acc: dict[str, Any] = {"operation": operation, "count": 0, "sum": 0.0, "sum_squares": 0.0,
                           "min": None, "max": None, "matched": 0,
                           "frequency": defaultdict(int), "groups": defaultdict(lambda: {"count": 0, "sum": 0.0})}
    records_done = 0
    kernel_seconds = 0.0
    started = time.perf_counter()
    cpu_window = resources.CpuWindow().start()
    actual_threads_seen: set[int] = set()
    actual_threads_last = 1 if not parallel else 0
    for segment in segments:
        seg_start, seg_end = int(segment["start"]), int(segment["end"])
        labels = segment["labels"]
        for offset in range(seg_start, seg_end, COMPUTE_CHUNK_ROWS):
            if cancel.is_set():
                raise InterruptedError("Benchmark cancelled by user.")
            stop = min(seg_end, offset + COMPUTE_CHUNK_ROWS)
            numeric = np.asarray(prepared["numeric"][offset:stop]) if "numeric" in prepared else None
            codes = np.asarray(prepared["codes"][offset:stop]) if "codes" in prepared else None
            kernel_start = time.perf_counter()
            kernel_timed_internally = False
            if operation == "numeric_stats":
                if parallel:
                    out = kernels.numeric_stats_parallel(numeric, thread_count, schedule)
                    actual_threads_last = int(out[5]); actual_threads_seen.add(actual_threads_last)
                else:
                    out = kernels.numeric_stats_seq(numeric)
            elif operation == "filter_gt":
                threshold = float(config.get("threshold", 0))
                if parallel:
                    out = kernels.threshold_count_parallel(numeric, threshold, thread_count, schedule)
                    actual_threads_last = int(out[1]); actual_threads_seen.add(actual_threads_last)
                else:
                    out = kernels.threshold_count_seq(numeric, threshold)
            elif operation in {"frequency", "date_frequency"}:
                if parallel:
                    out = kernels.frequency_parallel(codes, len(labels), thread_count, schedule)
                    actual_threads_last = int(out[1]); actual_threads_seen.add(actual_threads_last)
                else:
                    out = kernels.frequency_seq(codes, len(labels))
            elif operation == "comprehensive_analytics":
                threshold = float(config.get("threshold", 0.0))
                out, actual_threads_last, native_seconds = _run_comprehensive_compatibility(
                    codes, numeric, len(labels), threshold,
                    parallel=parallel, threads=thread_count, schedule=schedule,
                )
                kernel_seconds += native_seconds
                kernel_timed_internally = True
                if parallel:
                    actual_threads_seen.add(actual_threads_last)
            else:
                if parallel:
                    out = kernels.group_sum_parallel(codes, numeric, len(labels), thread_count, schedule)
                    actual_threads_last = int(out[2]); actual_threads_seen.add(actual_threads_last)
                else:
                    out = kernels.group_sum_seq(codes, numeric, len(labels))
            # This metric is strictly the native Cython call duration. Python-side
            # aggregation/result merging is included in wall_seconds, not here.
            if not kernel_timed_internally:
                kernel_seconds += time.perf_counter() - kernel_start

            if operation == "filter_gt":
                _merge_result(acc, (out[0],) if parallel else (out,))
            elif operation in {"frequency", "date_frequency"}:
                _merge_result(acc, (out[0],) if parallel else out, labels)
            else:
                _merge_result(acc, out, labels)
            records_done += stop - offset
            elapsed = time.perf_counter() - started
            rate = records_done / elapsed if elapsed > 0 else 0.0
            if report_progress:
                with _job_lock:
                    if job_id in _jobs:
                        _jobs[job_id]["progress"] = records_done / total_rows * 100 if total_rows else 100.0
                _emit(job_id, {
                    "type": "phase_progress", "mode": mode, "repetition": repetition,
                    "records_processed": records_done, "total_records": total_rows,
                    "progress": records_done / total_rows * 100 if total_rows else 100.0,
                    "elapsed_seconds": elapsed, "records_per_second": rate,
                    "estimated_remaining_seconds": (total_rows - records_done) / rate if rate > 0 else None,
                    "kernel_seconds": kernel_seconds, "configured_threads": thread_count,
                    "observed_threads_in_last_chunk": actual_threads_last,
                    "phase": "sequential" if not parallel else "parallel",
                    "storage_mode": "prepared data (shared by both engines)",
                })
    total_seconds = time.perf_counter() - started
    cpu_usage = cpu_window.stop()
    if operation == "numeric_stats":
        mean = acc["sum"] / acc["count"] if acc["count"] else None
        variance = max(0.0, acc["sum_squares"] / acc["count"] - mean * mean) if acc["count"] and mean is not None else None
        result = {"count": acc["count"], "sum": acc["sum"], "mean": mean,
                  "min": acc["min"], "max": acc["max"],
                  "standard_deviation": math.sqrt(variance) if variance is not None else None}
    elif operation == "filter_gt":
        result = {"column": config["column"], "threshold": float(config.get("threshold", 0)), "matched_records": acc["matched"]}
    elif operation in {"frequency", "date_frequency"}:
        if operation == "frequency":
            result = {"group_column": config["group_column"], "frequencies": dict(sorted(acc["frequency"].items()))}
        else:
            result = {"date_column": config["date_column"], "granularity": config.get("date_granularity", "month"),
                      "frequencies": dict(sorted(acc["frequency"].items()))}
    elif operation == "comprehensive_analytics":
        mean = acc["sum"] / acc["count"] if acc["count"] else None
        variance = max(0.0, acc["sum_squares"] / acc["count"] - mean * mean) if acc["count"] and mean is not None else None
        groups = {k: {"count": v["count"], "sum": v["sum"], "average": v["sum"] / v["count"] if v["count"] else None}
                  for k, v in sorted(acc["groups"].items())}
        result = {
            "record_count": acc["count"], "sum": acc["sum"], "mean": mean,
            "standard_deviation": math.sqrt(variance) if variance is not None else None,
            "minimum": acc["min"], "maximum": acc["max"],
            "threshold": float(config.get("threshold", 0.0)),
            "records_above_threshold": acc["matched"],
            "group_column": config["group_column"], "groups": groups,
        }
    else:
        groups = {k: {"count": v["count"], "sum": v["sum"], "average": v["sum"] / v["count"] if v["count"] else None}
                  for k, v in sorted(acc["groups"].items())}
        result = {"group_column": config["group_column"], "measure_column": config["column"], "groups": groups}
        if operation == "top_k_groups":
            result["top_k"] = int(config.get("top_k", 10))
            result["top_groups"] = [{"group": name, **values}
                                    for name, values in sorted(groups.items(), key=lambda pair: (-pair[1]["sum"], pair[0]))[:result["top_k"]]]
    return {
        "mode": mode, "repetition": repetition, "thread_count": thread_count,
        "configured_threads": thread_count,
        "observed_threads_max": max(actual_threads_seen) if actual_threads_seen else (1 if not parallel else 0),
        "records_processed": records_done, "read_seconds": 0.0, "conversion_seconds": 0.0,
        "kernel_seconds": kernel_seconds, "wall_seconds": total_seconds,
        "orchestration_seconds": max(0.0, total_seconds - kernel_seconds),
        "throughput_records_per_second": records_done / total_seconds if total_seconds else 0,
        "kernel_throughput_records_per_second": records_done / kernel_seconds if kernel_seconds else 0,
        "run_type": run_type,
        "cpu": cpu_usage,
        "result": result,
    }

def _results_equal(left: dict[str, Any], right: dict[str, Any], tolerance: float = 1e-9) -> bool:
    def compare(a: Any, b: Any) -> bool:
        if isinstance(a, dict) and isinstance(b, dict):
            return set(a.keys()) == set(b.keys()) and all(compare(a[k], b[k]) for k in a)
        if isinstance(a, list) and isinstance(b, list):
            return len(a) == len(b) and all(compare(x, y) for x, y in zip(a, b))
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return math.isclose(float(a), float(b), rel_tol=tolerance, abs_tol=tolerance)
        return a == b
    return compare(left, right)



def _verify_repetitions(seq_runs: list[dict[str, Any]], par_runs: list[dict[str, Any]]) -> bool:
    """Require every measured result from both engines to agree."""
    if not seq_runs or not par_runs:
        return False
    seq_reference = seq_runs[0].get("result")
    par_reference = par_runs[0].get("result")
    return (
        all(_results_equal(seq_reference, run.get("result")) for run in seq_runs)
        and all(_results_equal(par_reference, run.get("result")) for run in par_runs)
        and _results_equal(seq_reference, par_reference)
    )


def _relative_spread(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    median = statistics.median(values)
    return ((max(values) - min(values)) / median * 100.0) if median > 0 else 0.0


def _summarize_runs(runs: list[dict[str, Any]], total_rows: int) -> dict[str, Any]:
    wall_values = [float(run["wall_seconds"]) for run in runs]
    kernel_values = [float(run["kernel_seconds"]) for run in runs]
    median_wall = statistics.median(wall_values)
    median_kernel = statistics.median(kernel_values)
    # Pick the run closest to the median wall time for a concrete representative
    # repetition, but cards should use the explicit median display metrics below.
    representative = min(runs, key=lambda run: abs(float(run["wall_seconds"]) - median_wall))
    cpu_utilization = resources.combine_windows([run["cpu"] for run in runs if run.get("cpu")])
    return {
        "cpu_utilization": cpu_utilization,
        "median_wall_seconds": median_wall,
        "median_kernel_seconds": median_kernel,
        "median_throughput": total_rows / median_wall if median_wall > 0 else 0.0,
        "median_kernel_throughput": total_rows / median_kernel if median_kernel > 0 else 0.0,
        "wall_time_spread_percent": _relative_spread(wall_values),
        "kernel_time_spread_percent": _relative_spread(kernel_values),
        "representative_repetition": int(representative["repetition"]),
        "representative_observed_threads": int(representative.get("observed_threads_max", 1)),
        "median_observed_workers": float(statistics.median(
            int(run.get("observed_threads_max", 1)) for run in runs
        )),
        "display_metrics": {
            "summary_statistic": "median",
            "compute_seconds": median_wall,
            "calculation_seconds": median_kernel,
            "throughput_records_per_second": total_rows / median_wall if median_wall > 0 else 0.0,
            "runs_included": len(runs),
        },
        "runs": runs,
    }


def _build_performance_analysis(config: dict[str, Any], total_rows: int,
                                seq_summary: dict[str, Any], par_summary: dict[str, Any],
                                speedup_wall: float | None, speedup_kernel: float | None,
                                verified: bool, warmup_verified: bool,
                                storage_mode: str) -> dict[str, Any]:
    operation = config.get("operation", "unknown")
    schedule = config.get("schedule", "static")
    repetitions = len(seq_summary.get("runs", []))
    seq_spread = float(seq_summary.get("wall_time_spread_percent", 0.0))
    par_spread = float(par_summary.get("wall_time_spread_percent", 0.0))
    max_spread = max(seq_spread, par_spread)
    seq_overhead = max(0.0, float(seq_summary["median_wall_seconds"]) - float(seq_summary["median_kernel_seconds"]))
    par_overhead = max(0.0, float(par_summary["median_wall_seconds"]) - float(par_summary["median_kernel_seconds"]))
    reasons: list[str] = []
    if not verified or not warmup_verified:
        return {
            "outcome": "not_verified", "confidence": "not_applicable",
            "headline": "Performance comparison withheld because the two engines did not return matching results.",
            "reasons": ["Correctness verification failed. Resolve the result mismatch before interpreting speed measurements."],
            "recommended_next_test": "Check the result payload and repeat the benchmark only after both engines return identical analytical results.",
        }

    if operation == "frequency":
        reasons.append("Category Frequency performs a relatively small amount of work per row: it reads a category code and increments a category counter. For low-cardinality columns, this can be a cache- and memory-access-sensitive operation, so thread coordination may cost more than the work saved.")
    elif operation == "filter_gt":
        reasons.append("Threshold Filter performs a simple comparison and count per row. This is a light operation and can become limited by memory throughput rather than arithmetic capacity.")
    elif operation == "numeric_stats":
        reasons.append("Numeric Summary calculates a sum, squared sum, minimum and maximum for each value. It does more work per row than a simple count, but the single pass can still be constrained by memory bandwidth.")
    elif operation in {"group_sum", "top_k_groups"}:
        reasons.append("Grouped aggregation updates per-category counts and sums. The cost depends on category count, data distribution, cache behaviour and how efficiently worker-local partial results can be merged.")
    elif operation == "date_frequency":
        reasons.append("Date Frequency maps each prepared date code into a counter. Its work per row is modest, while category-cardinality and memory/cache behaviour can affect the result.")
    elif operation == "comprehensive_analytics":
        reasons.append("Comprehensive Analytics performs several useful tasks in one pass: numeric profiling, threshold counting, and per-category count/sum/average. This gives each worker more useful work per record than a single-purpose frequency count.")

    if total_rows <= 10_000_000 and speedup_wall is not None and speedup_wall < 1.0:
        reasons.append("At this data size, the selected operation may finish before the parallel setup, scheduling and partial-result merge costs are fully amortized. Larger workloads or a more computation-dense operation are useful follow-up tests.")
    if schedule in {"dynamic", "guided"} and operation in {"numeric_stats", "filter_gt", "frequency", "date_frequency", "comprehensive_analytics"}:
        reasons.append(f"The selected {schedule.title()} schedule adds work-distribution coordination. These row-wise operations usually perform similar work for each record, so Static is a useful lower-overhead comparison.")
    if "memory-mapped" in storage_mode.lower():
        reasons.append("Prepared columns were memory-mapped rather than held wholly in RAM. Page faults and available memory bandwidth can affect timings, especially when the working set is larger than the available file cache.")
    if max_spread >= 30.0:
        reasons.append(f"Timed repetitions varied substantially (up to {max_spread:.1f}% range relative to the median). Cold pages, cache state, background processes or power/thermal changes may have influenced the runs; the median is safer than selecting the fastest run, but the result should be repeated.")

    # ---- Was the machine able to give the threads real cores? ------------------
    # Measured from OS CPU counters during the timed phases (not assumed).
    seq_cpu = seq_summary.get("cpu_utilization") or {}
    par_cpu = par_summary.get("cpu_utilization") or {}
    cpu_measurable = bool(seq_cpu.get("measurable") and par_cpu.get("measurable"))
    logical = max(1, int(par_cpu.get("logical_processors") or seq_cpu.get("logical_processors") or 1))
    background = max(float(seq_cpu.get("background_cores", 0.0)), float(par_cpu.get("background_cores", 0.0))) if cpu_measurable else 0.0
    configured = int(config.get("threads", 1))
    cores_used = par_cpu.get("process_cores_used") if cpu_measurable else None
    expected_cores = min(configured, logical)
    cpu_starved = bool(cores_used is not None and configured >= 2 and cores_used < 0.6 * expected_cores)
    if background >= 1.0 or background / logical >= 0.25:
        interference = "high"
    elif background >= 0.5 or background / logical >= 0.15:
        interference = "moderate"
    else:
        interference = "none"
    if interference != "none":
        reasons.append(
            f"Other programs were using about {background:.1f} of {logical} CPU cores while this benchmark ran "
            "(measured from OS CPU counters). That steals cores from the worker threads and makes the multi-thread "
            "time unreliable. Close heavy programs and repeat the run."
        )
    if cpu_starved:
        reasons.append(
            f"The {configured} worker threads obtained only about {float(cores_used):.1f} cores' worth of CPU time "
            f"(about {expected_cores} expected). The OS was not running them at the same time, so this is a machine "
            "availability problem, not a flaw in the algorithm."
        )
    verdict_reliable = interference != "high" and not cpu_starved

    if speedup_wall is None:
        outcome = "not_available"
        headline = "A reliable speed comparison could not be calculated for this run."
    elif speedup_wall > 1.05:
        outcome = "parallel_faster"
        headline = f"Multi-thread processing was {speedup_wall:.2f}× faster for this measured workload (median compute time)."
        reasons.insert(0, "The multi-worker engine reduced the measured compute time while preserving the same analytical result. The measured gain applies to this dataset, operation, thread count and schedule—not to every workload.")
    elif speedup_wall < 0.95:
        outcome = "single_worker_faster"
        headline = f"Single-thread processing was faster for this measured workload; parallel speedup was {speedup_wall:.2f}×."
        reasons.insert(0, "The parallel engine used more workers, but its coordination and partial-result costs were greater than the time saved by dividing this particular workload.")
    else:
        outcome = "similar"
        headline = f"Both approaches performed similarly for this workload ({speedup_wall:.2f}× parallel speedup)."
        reasons.insert(0, "The time difference is small enough that normal run-to-run variation may account for part of it; there is no clear winner in this test.")

    if speedup_kernel is not None and speedup_wall is not None:
        if speedup_kernel > 1.05 and speedup_wall <= 1.0:
            reasons.append("The native calculation itself benefited from parallelism, but Python-side orchestration, chunk handling and result merging reduced or removed that gain in the full compute-phase time.")
        elif speedup_kernel < 1.0 and speedup_wall < 1.0:
            reasons.append("The native calculation was also slower in parallel, so the loss is not only caused by the Python wrapper; examine schedule choice, memory bandwidth and the operation's work per record.")
        elif par_overhead > seq_overhead * 1.25 and par_overhead - seq_overhead > 0.001:
            reasons.append(
                f"The parallel phase spent about {par_overhead:.4f}s outside the native kernel, versus {seq_overhead:.4f}s for the single-worker phase. This extra orchestration/result-merge cost is part of Compute Time, but is excluded from Calculation Time."
            )

    if max_spread >= 50.0 or repetitions < 3:
        confidence = "low"
    elif max_spread >= 25.0:
        confidence = "moderate"
    else:
        confidence = "good"
    if not verdict_reliable:
        confidence = "low"
        if outcome in {"single_worker_faster", "similar"}:
            headline += " NOTE: the computer was too busy for this verdict to be reliable; see the reasons below."
    elif interference == "moderate" and confidence == "good":
        confidence = "moderate"

    # Recommendations are deliberately not guessed here. They are populated
    # only after _probe_recommendation_options has actually tested the candidate
    # worker settings against the same prepared data.
    recommended = "No faster thread configuration has been verified yet."
    recommendation = None

    if outcome == "parallel_faster":
        audience_explanation = (
            f"Parallel won: {float(par_summary['median_wall_seconds']):.3f}s versus "
            f"{float(seq_summary['median_wall_seconds']):.3f}s on one thread "
            f"({float(speedup_wall or 0):.2f}×); the work saved exceeded thread coordination and merging costs."
        )
    elif outcome == "single_worker_faster":
        audience_explanation = (
            f"Single-thread won: {float(seq_summary['median_wall_seconds']):.3f}s versus "
            f"{float(par_summary['median_wall_seconds']):.3f}s with {int(config.get('threads', 1))} OpenMP threads; "
            "OpenMP coordination and merging cost more than parallel processing saved."
        )
    elif outcome == "similar":
        audience_explanation = (
            f"The medians were close ({float(seq_summary['median_wall_seconds']):.3f}s single, "
            f"{float(par_summary['median_wall_seconds']):.3f}s parallel); neither method showed a clear advantage."
        )
    else:
        audience_explanation = "No explanation is available because a valid comparison was not completed."

    return {
        "outcome": outcome, "confidence": confidence, "headline": headline,
        "audience_explanation": audience_explanation,
        "speedup_definition": "sequential median compute wall time divided by parallel median compute wall time",
        "parallel_speedup": speedup_wall,
        "native_calculation_speedup": speedup_kernel,
        "compute_minus_native_seconds": {
            "single_worker": seq_overhead,
            "multi_worker": par_overhead,
            "definition": "compute wall time minus native Cython kernel time; includes Python orchestration and result merging",
        },
        "execution_quality": {
            "verdict_reliable": verdict_reliable,
            "interference": interference,
            "background_cpu_cores": background,
            "logical_processors": logical,
            "configured_threads": configured,
            "cores_actually_used_by_workers": cores_used,
            "cpu_starved": cpu_starved,
            "cpu_measurement": "measured" if cpu_measurable else "run too short for reliable OS CPU counters",
        },
        "sequential_wall_time_spread_percent": seq_spread,
        "parallel_wall_time_spread_percent": par_spread,
        "reasons": reasons,
        "recommended_next_test": recommended,
        "recommended_configuration": recommendation,
    }


_RECOMMENDATION_THREAD_STEPS = (2, 4, 6, 8, 12, 16)


def _recommendation_probe_threads(current_threads: int, logical_processors: int) -> list[int]:
    """Pick the two closest supported alternatives for a same-size worker scan."""
    available = [
        n for n in _RECOMMENDATION_THREAD_STEPS
        if n <= min(16, logical_processors) and n != current_threads
    ]
    # Test nearby thread counts rather than jumping straight to the maximum.
    # This keeps the automatic validation bounded and makes the next suggestion
    # easier to interpret as a one-variable change.
    return sorted(available, key=lambda value: (abs(value - current_threads), value))[:2]


def _recommendation_size_probe_threads(current_threads: int, logical_processors: int) -> list[int]:
    """Test the current setting plus its two closest supported alternatives."""
    supported = [n for n in _RECOMMENDATION_THREAD_STEPS if n <= min(16, logical_processors)]
    chosen: list[int] = []
    if 2 <= current_threads <= 16:
        chosen.append(current_threads)
    nearby = sorted(
        (n for n in supported if n != current_threads),
        key=lambda value: (abs(value - current_threads), value),
    )
    chosen.extend(nearby[:max(0, 3 - len(chosen))])
    # Preserve the explicitly configured value even if the OS reports fewer
    # logical processors; the test will still require >=2 observed workers.
    return list(dict.fromkeys(chosen))[:3]



def _probe_next_dataset_size(
    job_id: str,
    config: dict[str, Any],
    dataset: dict[str, Any],
    total_rows: int,
    cancel: threading.Event,
    repetitions: int,
) -> dict[str, Any]:
    """Generate the next preset in an isolated temp folder and test it for real.

    The probe uses the same dataset template and seed as the user's dataset.
    It never adds the temporary dataset to the user's dataset library. Only a
    matching, repeatable parallel win can become a recommendation.
    """
    started = time.perf_counter()
    next_rows = next((int(n) for n in datasets.RECOMMENDED_DEMO_ROW_PRESETS if int(n) > total_rows), None)
    if next_rows is None:
        return {"status": "no_larger_preset", "message": "The largest supported dataset size is already selected."}
    max_auto_rows = int(os.getenv("PBDA_RECOMMENDATION_MAX_AUTO_ROWS", "25000000"))
    if next_rows > max_auto_rows:
        return {
            "status": "skipped_size_limit", "next_dataset_rows": next_rows,
            "message": f"The next preset is {next_rows:,} rows, above the automatic trial limit of {max_auto_rows:,}; it was not generated or assumed faster.",
        }
    kind = dataset.get("kind")
    if kind not in datasets.TEMPLATES:
        return {"status": "skipped_template", "next_dataset_rows": next_rows,
                "message": "The current dataset template cannot be regenerated for an isolated size trial."}

    logical_processors = max(1, int(psutil.cpu_count(logical=True) or 1))
    if logical_processors < 2:
        return {"status": "skipped_no_workers", "next_dataset_rows": next_rows,
                "message": "This computer reports fewer than two logical processors; no multi-thread trial was attempted."}
    threads_to_test = _recommendation_size_probe_threads(int(config.get("threads", 2)), logical_processors)
    if not threads_to_test:
        return {"status": "skipped_no_workers", "next_dataset_rows": next_rows,
                "message": "No supported multi-worker configuration is available to test on this computer."}

    probe_root = tempfile.TemporaryDirectory(prefix=f"pbda_recommendation_{job_id}_")
    prepared: dict[str, Any] | None = None
    arrays_temp = None
    probe_dataset: dict[str, Any] | None = None
    candidate_results: list[dict[str, Any]] = []
    try:
        data_root = Path(probe_root.name) / "data"
        meta_root = Path(probe_root.name) / "metadata"

        last_progress_emit = 0.0

        def generation_progress(payload: dict[str, Any]) -> None:
            nonlocal last_progress_emit
            if cancel.is_set():
                return
            now = time.monotonic()
            progress_value = float(payload.get("progress", 0.0))
            phase_value = str(payload.get("phase", "generating"))
            # The generator may report after every small write block. Throttle
            # recommendation-only telemetry so a large trial cannot flood the
            # WebSocket event queue or make the UI stutter.
            if (now - last_progress_emit < 0.25
                    and progress_value < 100.0
                    and phase_value not in {"complete", "completed"}):
                return
            last_progress_emit = now
            _emit(job_id, {
                "type": "recommendation_generation_progress",
                "candidate_rows": next_rows,
                "generated_rows": int(payload.get("generated_rows", 0)),
                "total_rows": int(payload.get("total_rows", next_rows)),
                "progress": progress_value,
                "elapsed_seconds": float(payload.get("elapsed_seconds", 0.0)),
                "records_per_second": float(payload.get("records_per_second", 0.0)),
                "file_size_bytes": int(payload.get("file_size_bytes", 0)),
                "phase": phase_value,
                "message": f"Creating temporary {next_rows:,}-record trial data to verify the next size before recommending it.",
            })

        if cancel.is_set():
            raise InterruptedError("Benchmark cancelled before the recommendation data-size trial.")
        _emit(job_id, {
            "type": "recommendation_generation_progress", "candidate_rows": next_rows,
            "generated_rows": 0, "total_rows": next_rows, "progress": 0.0,
            "elapsed_seconds": 0.0, "records_per_second": 0.0, "file_size_bytes": 0,
            "phase": "preparing",
            "message": f"Preparing a temporary {next_rows:,}-record dataset to test before suggesting it.",
        })
        probe_dataset = datasets.generate_dataset(
            kind=kind,
            rows=next_rows,
            seed=int(dataset.get("seed", 42)),
            batch_size=int(dataset.get("requested_batch_size", dataset.get("batch_size", 25_000))),
            name=f"recommendation_trial_{job_id}",
            progress=generation_progress,
            cancel_check=cancel.is_set,
            data_root=data_root,
            meta_root=meta_root,
        )
        if cancel.is_set():
            raise InterruptedError("Benchmark cancelled after generating recommendation trial data.")

        _emit(job_id, {
            "type": "recommendation_probe_progress", "candidate_threads": None,
            "candidate_number": 0, "candidate_count": len(threads_to_test),
            "message": f"Temporary {next_rows:,}-record data is ready. Preparing it once for paired worker tests.",
        })
        prepared, segments, arrays_temp, preparation_seconds, storage_mode = _prepare_dataset(
            job_id, config, probe_dataset, cancel, event_prefix="recommendation_preparation",
        )
        sequential_reference = None

        for candidate_number, candidate_threads in enumerate(threads_to_test, start=1):
            if cancel.is_set():
                raise InterruptedError("Benchmark cancelled during the next-size recommendation trial.")
            candidate_config = {**config, "threads": candidate_threads}
            _emit(job_id, {
                "type": "recommendation_probe_progress",
                "candidate_threads": candidate_threads,
                "candidate_number": candidate_number,
                "candidate_count": len(threads_to_test),
                "message": f"Testing {next_rows:,} records with {candidate_threads} threads ({candidate_number}/{len(threads_to_test)}).",
            })
            warmup_seq = _run_phase(
                job_id, "sequential", candidate_config, prepared, segments, 0,
                next_rows, cancel, report_progress=False, run_type="recommendation_warmup",
            )
            warmup_par = _run_phase(
                job_id, "parallel", candidate_config, prepared, segments, 0,
                next_rows, cancel, report_progress=False, run_type="recommendation_warmup",
            )
            warmup_ok = _results_equal(warmup_seq["result"], warmup_par["result"])
            seq_trial_runs: list[dict[str, Any]] = []
            par_trial_runs: list[dict[str, Any]] = []
            for repetition in range(1, repetitions + 1):
                if cancel.is_set():
                    raise InterruptedError("Benchmark cancelled during the next-size recommendation trial.")
                order = ["sequential", "parallel"] if repetition % 2 else ["parallel", "sequential"]
                for mode in order:
                    run = _run_phase(
                        job_id, mode, candidate_config, prepared, segments, repetition,
                        next_rows, cancel, report_progress=False, run_type="recommendation_size_probe",
                    )
                    if mode == "sequential":
                        seq_trial_runs.append(run)
                    else:
                        par_trial_runs.append(run)
            seq_trial_summary = _summarize_runs(seq_trial_runs, next_rows)
            par_trial_summary = _summarize_runs(par_trial_runs, next_rows)
            result_verified = warmup_ok and _verify_repetitions(seq_trial_runs, par_trial_runs)
            spread = max(
                float(seq_trial_summary["wall_time_spread_percent"]),
                float(par_trial_summary["wall_time_spread_percent"]),
            )
            speedup = (
                float(seq_trial_summary["median_wall_seconds"]) / float(par_trial_summary["median_wall_seconds"])
                if float(par_trial_summary["median_wall_seconds"]) > 0 else 0.0
            )
            candidate_results.append({
                "threads": candidate_threads,
                "dataset_rows": next_rows,
                "median_single_compute_seconds": float(seq_trial_summary["median_wall_seconds"]),
                "median_parallel_compute_seconds": float(par_trial_summary["median_wall_seconds"]),
                "speedup_vs_single": speedup,
                "spread_percent": spread,
                "result_verified": result_verified,
                "warmup_verified": warmup_ok,
                "measured_runs": repetitions,
                "observed_workers_max": max(int(run.get("observed_threads_max", 0)) for run in par_trial_runs),
                "single_run_measurements": [
                    {"repetition": int(run["repetition"]), "compute_seconds": float(run["wall_seconds"]),
                     "native_seconds": float(run["kernel_seconds"])} for run in seq_trial_runs
                ],
                "parallel_run_measurements": [
                    {"repetition": int(run["repetition"]), "compute_seconds": float(run["wall_seconds"]),
                     "native_seconds": float(run["kernel_seconds"]),
                     "observed_workers": int(run.get("observed_threads_max", 0))} for run in par_trial_runs
                ],
                "storage_mode": storage_mode,
                "preparation_seconds": preparation_seconds,
            })

        eligible = [
            item for item in candidate_results
            if item["result_verified"]
            and item["warmup_verified"]
            and float(item["spread_percent"]) < 25.0
            and float(item["speedup_vs_single"]) >= 1.05
            and int(item["observed_workers_max"]) >= 2
        ]
        if eligible:
            winner = max(eligible, key=lambda item: float(item["speedup_vs_single"]))
            status = "verified_next_size"
            recommendation = {
                "threads": int(winner["threads"]),
                "dataset_rows": int(next_rows),
                "operation": config.get("operation"),
                "schedule": config.get("schedule", "static"),
                "median_single_compute_seconds": winner["median_single_compute_seconds"],
                "median_parallel_compute_seconds": winner["median_parallel_compute_seconds"],
                "speedup_vs_single": winner["speedup_vs_single"],
                "spread_percent": winner["spread_percent"],
                "measured_runs": winner["measured_runs"],
                "reason": "This exact dataset size and OpenMP thread count were generated and benchmarked locally; all paired results matched and the parallel median was at least 5% faster.",
                "automatically_generated_trial": True,
            }
            message = (
                f"Verified next setup: {winner['threads']} threads on {next_rows:,} records; "
                f"{winner['speedup_vs_single']:.2f}x speedup in {repetitions} paired runs."
            )
        else:
            status = "no_verified_parallel_win_at_next_size"
            recommendation = None
            message = (
                f"The next size ({next_rows:,} records) was generated and tested with {', '.join(map(str, threads_to_test))} threads; "
                "no stable, matching parallel configuration beat one worker by at least 5%."
            )
        return {
            "status": status,
            "next_dataset_rows": next_rows,
            "tested_candidates": candidate_results,
            "recommended_configuration": recommendation,
            "message": message,
            "temporary_dataset_removed": True,
            "duration_seconds": time.perf_counter() - started,
        }
    finally:
        _release_prepared_arrays(prepared)
        if arrays_temp is not None:
            arrays_temp.cleanup()
        probe_root.cleanup()

def _probe_recommendation_options(
    job_id: str,
    config: dict[str, Any],
    dataset: dict[str, Any],
    prepared: dict[str, Any],
    segments: list[dict[str, Any]],
    total_rows: int,
    cancel: threading.Event,
    seq_runs: list[dict[str, Any]],
    seq_summary: dict[str, Any],
    par_runs: list[dict[str, Any]],
    par_summary: dict[str, Any],
    verified: bool,
    warmup_verified: bool,
    repetitions: int,
) -> dict[str, Any]:
    """Only recommend a multi-worker setup after measuring it on these same rows.

    Candidate thread counts first use the same prepared data. If none wins,
    generate the next supported size in an isolated temporary folder and test
    paired sequential/parallel runs there. Only measured, repeatable, matching
    results can become a recommendation.
    """
    started = time.perf_counter()
    seq_spread = float(seq_summary.get("wall_time_spread_percent", 0.0))
    current_par_spread = float(par_summary.get("wall_time_spread_percent", 0.0))
    current_threads = int(config.get("threads", 1))
    logical_processors = max(1, int(psutil.cpu_count(logical=True) or 1))

    if not verified or not warmup_verified:
        return {
            "status": "skipped_unverified", "tested_candidates": [],
            "message": "Result verification did not pass; no configuration was recommended.",
            "duration_seconds": time.perf_counter() - started,
        }
    if repetitions < 3:
        return {
            "status": "skipped_insufficient_repetitions", "tested_candidates": [],
            "message": "At least 3 measured repetitions are required before ranking worker settings.",
            "duration_seconds": time.perf_counter() - started,
        }
    if max(seq_spread, current_par_spread) >= 25.0:
        spread = max(seq_spread, current_par_spread)
        return {
            "status": "skipped_unstable", "tested_candidates": [],
            "max_baseline_spread_percent": spread,
            "message": (
                f"The initial timing spread was {spread:.1f}%, so additional worker settings "
                "were not ranked. Repeat the same benchmark while the computer is idle."
            ),
            "duration_seconds": time.perf_counter() - started,
        }

    sequential_reference = seq_runs[0].get("result") if seq_runs else None
    tested: list[dict[str, Any]] = []

    # The configuration already measured in the main benchmark is a candidate,
    # but it qualifies only when its repetitions were stable and fully verified.
    current_speedup = (
        float(seq_summary["median_wall_seconds"]) / float(par_summary["median_wall_seconds"])
        if float(par_summary["median_wall_seconds"]) > 0 else 0.0
    )
    current_verified = bool(
        _verify_repetitions(seq_runs, par_runs)
        and current_par_spread < 25.0
        and current_speedup >= 1.05
    )
    if current_verified:
        tested.append({
            "threads": current_threads,
            "dataset_rows": total_rows,
            "median_compute_seconds": float(par_summary["median_wall_seconds"]),
            "speedup_vs_single": current_speedup,
            "spread_percent": current_par_spread,
            "result_verified": True,
            "source": "main_benchmark",
            "runs": len(par_runs),
            "observed_workers_max": max(int(run.get("observed_threads_max", 0)) for run in par_runs),
            "run_measurements": [
                {
                    "repetition": int(run.get("repetition", 0)),
                    "compute_seconds": float(run.get("wall_seconds", 0.0)),
                    "native_seconds": float(run.get("kernel_seconds", 0.0)),
                    "observed_workers": int(run.get("observed_threads_max", 0)),
                }
                for run in par_runs
            ],
        })

    candidates = _recommendation_probe_threads(current_threads, resource_limits()["max_threads"])
    if candidates:
        _emit(job_id, {
            "type": "recommendation_probe_start",
            "candidate_count": len(candidates),
            "message": "Checking alternative worker counts on the same prepared dataset before suggesting a setting.",
        })

    for index, candidate_threads in enumerate(candidates, start=1):
        if cancel.is_set():
            raise InterruptedError("Benchmark cancelled during recommendation verification.")
        candidate_config = {**config, "threads": candidate_threads}
        _emit(job_id, {
            "type": "recommendation_probe_progress",
            "candidate_threads": candidate_threads,
            "candidate_number": index,
            "candidate_count": len(candidates),
            "message": f"Pair-testing {candidate_threads} threads on the same {total_rows:,} records ({index}/{len(candidates)}).",
        })
        warmup_seq = _run_phase(
            job_id, "sequential", candidate_config, prepared, segments, 0,
            total_rows, cancel, report_progress=False, run_type="recommendation_warmup",
        )
        warmup_par = _run_phase(
            job_id, "parallel", candidate_config, prepared, segments, 0,
            total_rows, cancel, report_progress=False, run_type="recommendation_warmup",
        )
        warmup_verified_candidate = _results_equal(warmup_seq["result"], warmup_par["result"])
        candidate_seq_runs: list[dict[str, Any]] = []
        candidate_par_runs: list[dict[str, Any]] = []
        for repetition in range(1, repetitions + 1):
            if cancel.is_set():
                raise InterruptedError("Benchmark cancelled during recommendation verification.")
            order = ["sequential", "parallel"] if repetition % 2 else ["parallel", "sequential"]
            for mode in order:
                run = _run_phase(
                    job_id, mode, candidate_config, prepared, segments, repetition,
                    total_rows, cancel, report_progress=False, run_type="recommendation_probe",
                )
                if mode == "sequential":
                    candidate_seq_runs.append(run)
                else:
                    candidate_par_runs.append(run)

        candidate_seq_summary = _summarize_runs(candidate_seq_runs, total_rows)
        candidate_summary = _summarize_runs(candidate_par_runs, total_rows)
        candidate_spread = max(
            float(candidate_seq_summary["wall_time_spread_percent"]),
            float(candidate_summary["wall_time_spread_percent"]),
        )
        candidate_speedup = (
            float(candidate_seq_summary["median_wall_seconds"]) / float(candidate_summary["median_wall_seconds"])
            if float(candidate_summary["median_wall_seconds"]) > 0 else 0.0
        )
        result_verified = bool(
            warmup_verified_candidate
            and _verify_repetitions(candidate_seq_runs, candidate_par_runs)
            and sequential_reference is not None
            and all(_results_equal(sequential_reference, run.get("result")) for run in candidate_par_runs)
        )
        candidate_entry = {
            "threads": candidate_threads,
            "dataset_rows": total_rows,
            "median_sequential_compute_seconds": float(candidate_seq_summary["median_wall_seconds"]),
            "median_compute_seconds": float(candidate_summary["median_wall_seconds"]),
            "speedup_vs_single": candidate_speedup,
            "spread_percent": candidate_spread,
            "result_verified": result_verified,
            "warmup_verified": warmup_verified_candidate,
            "source": "automatic_paired_probe",
            "runs": len(candidate_par_runs),
            "observed_workers_max": max(int(run.get("observed_threads_max", 0)) for run in candidate_par_runs),
            "run_measurements": [
                {
                    "repetition": int(run.get("repetition", 0)),
                    "sequential_compute_seconds": float(candidate_seq_runs[i].get("wall_seconds", 0.0)),
                    "compute_seconds": float(run.get("wall_seconds", 0.0)),
                    "native_seconds": float(run.get("kernel_seconds", 0.0)),
                    "observed_workers": int(run.get("observed_threads_max", 0)),
                }
                for i, run in enumerate(candidate_par_runs)
            ],
        }
        tested.append(candidate_entry)
        _emit(job_id, {
            "type": "recommendation_probe_progress",
            "candidate_threads": candidate_threads,
            "candidate_number": index,
            "candidate_count": len(candidates),
            "candidate_speedup": candidate_speedup,
            "candidate_spread_percent": candidate_spread,
            "result_verified": result_verified,
            "message": f"Completed paired {candidate_threads}-thread verification; checking repeatability and result parity.",
        })

    eligible = [
        item for item in tested
        if item["result_verified"]
        and float(item["spread_percent"]) < 25.0
        and float(item["speedup_vs_single"]) >= 1.05
        and int(item.get("observed_workers_max", 0)) >= 2
    ]
    size_probe = None
    recommendation_config = None
    if eligible:
        winner = max(eligible, key=lambda item: float(item["speedup_vs_single"]))
        status = "verified"
        recommendation_config = {
            "threads": int(winner["threads"]),
            "dataset_rows": int(total_rows),
            "operation": config.get("operation"),
            "schedule": config.get("schedule", "static"),
            "median_compute_seconds": float(winner["median_compute_seconds"]),
            "speedup_vs_single": float(winner["speedup_vs_single"]),
            "spread_percent": float(winner["spread_percent"]),
            "measured_runs": int(winner["runs"]),
            "reason": "This exact worker count was tested on the same dataset and operation; every result matched and its median was at least 5% faster than the sequential baseline.",
            "automatically_generated_trial": False,
        }
        message = (
            f"Verified setting: {winner['threads']} threads on {total_rows:,} records; "
            f"median speedup {winner['speedup_vs_single']:.2f}x across {winner['runs']} measured runs."
        )
    elif not ENABLE_NEXT_SIZE_PROBE:
        status = "no_verified_parallel_win"
        message = (
            f"No multi-thread setting beat one thread reliably on the same {total_rows:,}-record dataset. "
            "Automatic generation of a larger trial dataset is disabled in the 500 MB memory-safe profile."
        )
    else:
        _emit(job_id, {
            "type": "recommendation_probe_progress", "candidate_threads": None,
            "candidate_number": len(candidates), "candidate_count": len(candidates),
            "message": f"No faster worker setting was verified on {total_rows:,} rows. Testing the next supported data size before making a recommendation.",
        })
        try:
            size_probe = _probe_next_dataset_size(
                job_id, config, dataset, total_rows, cancel, repetitions,
            )
        except InterruptedError:
            raise
        except Exception as probe_error:
            # Recommendation experiments are optional. A disk-space, parser,
            # temporary-file or candidate-kernel problem must never invalidate
            # the already completed and verified primary benchmark.
            _logger.exception(
                "Automatic next-size recommendation trial failed | job=%s | rows=%s",
                job_id, total_rows,
            )
            size_probe = {
                "status": "skipped_resources",
                "message": (
                    "The next-size trial could not be completed "
                    f"({type(probe_error).__name__}: {str(probe_error)[:180]}). "
                    "No untested configuration is being recommended."
                ),
                "error_type": type(probe_error).__name__,
            }
        if size_probe.get("status") == "verified_next_size":
            status = "verified_next_size"
            recommendation_config = size_probe["recommended_configuration"]
            message = size_probe["message"]
        elif size_probe.get("status") == "no_verified_parallel_win_at_next_size":
            status = "no_verified_parallel_win"
            message = (
                f"No multi-thread setting beat one thread reliably on {total_rows:,} rows or on the tested "
                f"next size ({size_probe.get('next_dataset_rows', 0):,} rows)."
            )
        else:
            status = size_probe.get("status", "no_verified_parallel_win")
            message = size_probe.get("message", "No faster worker configuration was verified.")

    result = {
        "status": status,
        "tested_candidates": tested,
        "next_size_trial": size_probe,
        "recommended_configuration": recommendation_config,
        "message": message,
        "probe_thread_counts": candidates,
        "logical_processors_detected": logical_processors,
        "duration_seconds": time.perf_counter() - started,
    }
    _emit(job_id, {
        "type": "recommendation_probe_complete",
        "status": status,
        "message": message,
        "duration_seconds": result["duration_seconds"],
    })
    return result

def _worker(job_id: str) -> None:
    with _job_lock:
        job = _jobs[job_id]
        job["status"] = "running"
        config = dict(job["config"])
        dataset = dict(job["dataset"])
        cancel = job["cancel_event"]
        resource_plan = job.get("resource_plan")
    _logger.info("Benchmark started | job=%s | dataset=%s | rows=%s | operation=%s | threads=%s | schedule=%s | repetitions=%s",
                 job_id, dataset.get("name"), dataset.get("rows"), config.get("operation"),
                 config.get("threads"), config.get("schedule"), config.get("repetitions"))
    _emit(job_id, {"type": "job_status", "status": "running", "message": "Benchmark started."})
    all_runs: list[dict[str, Any]] = []
    temp_dir = None
    try:
        if cancel.is_set():
            raise InterruptedError("Benchmark cancelled before preparation.")
        prepared, segments, temp_dir, preparation_seconds, storage_mode = _prepare_dataset(job_id, config, dataset, cancel)
        repetitions = int(config.get("repetitions", 3))
        # One untimed pass per engine warms the same prepared arrays and OpenMP
        # runtime before measured repetitions. These passes are never included
        # in reported medians or speedup.
        warmup_runs: list[dict[str, Any]] = []
        for warmup_mode in ("sequential", "parallel"):
            if cancel.is_set():
                raise InterruptedError("Benchmark cancelled during warm-up.")
            warmup = _run_phase(
                job_id, warmup_mode, config, prepared, segments, 0,
                int(dataset["rows"]), cancel, report_progress=False, run_type="warmup",
            )
            warmup_runs.append(warmup)
        warmup_verified = _results_equal(warmup_runs[0]["result"], warmup_runs[1]["result"])
        if not warmup_verified:
            raise RuntimeError("Sequential and parallel warm-up results did not match. Timed benchmarking was stopped.")

        total_phases = repetitions * 2
        phase_number = 0
        for rep in range(1, repetitions + 1):
            if cancel.is_set():
                raise InterruptedError("Benchmark cancelled by user.")
            # Alternate order to limit systematic cache and thermal bias.
            order = ["sequential", "parallel"] if rep % 2 else ["parallel", "sequential"]
            for mode in order:
                if cancel.is_set():
                    raise InterruptedError("Benchmark cancelled by user.")
                phase_number += 1
                _emit(job_id, {
                    "type": "phase_start", "mode": mode, "repetition": rep,
                    "repetitions": repetitions, "phase_number": phase_number,
                    "total_phases": total_phases, "total_records": int(dataset["rows"]),
                    "configured_threads": int(config.get("threads", 1)) if mode == "parallel" else 1,
                    "message": ("The CSV is already prepared. One CPU worker is calculating the result."
                                if mode == "sequential" else
                                "The same prepared data is now being divided across OpenMP CPU workers."),
                })
                run = _run_phase(job_id, mode, config, prepared, segments, rep, int(dataset["rows"]), cancel)
                run["phase_number"] = phase_number
                all_runs.append(run)
                _emit(job_id, {"type": "phase_complete", **run})
        seq = [x for x in all_runs if x["mode"] == "sequential"]
        par = [x for x in all_runs if x["mode"] == "parallel"]
        seq_summary = _summarize_runs(seq, int(dataset["rows"]))
        par_summary = _summarize_runs(par, int(dataset["rows"]))
        seq_median = seq_summary["median_wall_seconds"]
        par_median = par_summary["median_wall_seconds"]
        seq_kernel = seq_summary["median_kernel_seconds"]
        par_kernel = par_summary["median_kernel_seconds"]
        seq_result, par_result = seq[-1]["result"], par[-1]["result"]
        # Verify every measured repetition, not only the final pair. A transient
        # race or nondeterministic output must invalidate the comparison.
        verified = _verify_repetitions(seq, par)
        speedup_wall = seq_median / par_median if par_median > 0 else None
        speedup_kernel = seq_kernel / par_kernel if par_kernel > 0 else None
        observed_worker_median = float(par_summary.get("median_observed_workers", 0.0))
        efficiency = (
            speedup_kernel / observed_worker_median * 100
            if speedup_kernel is not None and observed_worker_median >= 1.0 else None
        )
        saved = ((seq_median - par_median) / seq_median * 100) if seq_median > 0 else None
        end_to_end_seq = preparation_seconds + seq_median
        end_to_end_par = preparation_seconds + par_median
        end_to_end_speedup = end_to_end_seq / end_to_end_par if end_to_end_par > 0 else None
        analysis = _build_performance_analysis(
            config, int(dataset["rows"]), seq_summary, par_summary,
            speedup_wall, speedup_kernel, verified, warmup_verified, storage_mode,
        )
        try:
            recommendation_validation = _probe_recommendation_options(
                job_id, config, dataset, prepared, segments, int(dataset["rows"]), cancel,
                seq, seq_summary, par, par_summary, verified, warmup_verified, repetitions,
            )
        except InterruptedError as probe_cancel:
            # The main comparison is already complete. Cancelling the optional
            # recommendation trial must not discard the valid measured result.
            recommendation_validation = {
                "status": "skipped_cancelled", "tested_candidates": [],
                "message": "Comparison saved. Automatic recommendation trial was cancelled; no untested setting is recommended.",
                "cancel_detail": str(probe_cancel),
            }
            _emit(job_id, {
                "type": "recommendation_probe_complete", "status": "skipped_cancelled",
                "message": recommendation_validation["message"],
            })
        except Exception as probe_error:
            # A failure in the optional recommendation scan must not turn a
            # successfully measured comparison into a failed benchmark job.
            _logger.exception("Optional recommendation scan failed | job=%s", job_id)
            recommendation_validation = {
                "status": "skipped_probe_error", "tested_candidates": [],
                "message": (
                    "The comparison is saved, but the optional recommendation test failed "
                    f"({type(probe_error).__name__}). No untested setting is recommended."
                ),
                "error_type": type(probe_error).__name__,
            }
            _emit(job_id, {
                "type": "recommendation_probe_complete", "status": "skipped_probe_error",
                "message": recommendation_validation["message"],
            })
        analysis["recommendation_validation"] = recommendation_validation
        if recommendation_validation["status"] in {"verified", "verified_next_size"}:
            recommendation = recommendation_validation["recommended_configuration"]
            analysis["recommended_configuration"] = recommendation
            setup_label = "Verified next setup" if recommendation_validation["status"] == "verified_next_size" else "Verified setup"
            analysis["recommended_next_test"] = (
                f"{setup_label}: {recommendation['threads']} threads on {recommendation['dataset_rows']:,} records "
                f"({recommendation['speedup_vs_single']:.2f}x faster than one worker in {recommendation['measured_runs']} measured runs)."
            )
        elif recommendation_validation["status"] == "skipped_unstable":
            analysis["recommended_configuration"] = None
            spread = float(recommendation_validation.get("max_baseline_spread_percent", 0.0))
            analysis["recommended_next_test"] = (
                f"Repeat the same dataset and thread settings while the computer is idle (timing varied up to {spread:.1f}%). "
                "No untested setup is recommended."
            )
        elif recommendation_validation["status"] == "skipped_unverified":
            analysis["recommended_configuration"] = None
            analysis["recommended_next_test"] = "Fix result verification before testing or recommending another configuration."
        elif recommendation_validation["status"] == "skipped_insufficient_repetitions":
            analysis["recommended_configuration"] = None
            analysis["recommended_next_test"] = "Repeat this benchmark with at least 3 measured runs; no worker setting has been ranked yet."
        elif recommendation_validation["status"] == "skipped_cancelled":
            analysis["recommended_configuration"] = None
            analysis["recommended_next_test"] = "The measured comparison is saved; automatic recommendation testing was cancelled."
        elif recommendation_validation["status"] == "skipped_size_limit":
            analysis["recommended_configuration"] = None
            analysis["recommended_next_test"] = recommendation_validation.get("message", "The next size was not tested; no larger-size winner is claimed.")
        elif recommendation_validation["status"] in {"skipped_template", "skipped_no_workers", "skipped_resources", "skipped_probe_error"}:
            analysis["recommended_configuration"] = None
            analysis["recommended_next_test"] = recommendation_validation.get("message", "The next-size trial could not be completed; no untested setting is recommended.")
        elif recommendation_validation["status"] == "no_larger_preset":
            analysis["recommended_configuration"] = None
            analysis["recommended_next_test"] = "You are already at the largest supported dataset size; no larger-size suggestion is available."
        else:
            analysis["recommended_configuration"] = None
            analysis["recommended_next_test"] = recommendation_validation.get(
                "message", "No tested multi-worker configuration was faster; no untested setting is recommended."
            )
        result = {
            "experiment_id": job_id, "dataset_id": dataset["dataset_id"],
            "dataset_name": dataset["name"], "dataset_rows": dataset["rows"],
            "operation": config["operation"], "config": config,
            "preparation_seconds": preparation_seconds,
            "prepared_storage_mode": storage_mode,
            "resource_plan": resource_plan,
            "resource_profile": dict(prepared.get("_pbda_profile", {})),
            "sequential": seq_summary,
            "parallel": par_summary,
            "warmup": {
                "runs_per_mode": 1,
                "timings_excluded_from_medians": True,
                "verified": warmup_verified,
                "sequential_wall_seconds": warmup_runs[0]["wall_seconds"],
                "sequential_kernel_seconds": warmup_runs[0]["kernel_seconds"],
                "parallel_wall_seconds": warmup_runs[1]["wall_seconds"],
                "parallel_kernel_seconds": warmup_runs[1]["kernel_seconds"],
                "runs": warmup_runs,
            },
            "speedup_wall": speedup_wall, "speedup_kernel": speedup_kernel,
            "end_to_end_speedup": end_to_end_speedup,
            "parallel_efficiency_percent": efficiency, "time_saved_percent": saved,
            "verified": verified, "result": seq_result,
            "analysis": analysis,
            "recommendation_validation": recommendation_validation,
            "comparison_metrics": {
                "summary_statistic": "median",
                "sequential_compute_seconds": seq_median,
                "parallel_compute_seconds": par_median,
                "sequential_calculation_seconds": seq_kernel,
                "parallel_calculation_seconds": par_kernel,
                "sequential_throughput": seq_summary["median_throughput"],
                "parallel_throughput": par_summary["median_throughput"],
            },
            "methodology": {
                "timed_runs_per_mode": repetitions,
                "warmup_runs_per_mode": 1,
                "summary_statistic": "median",
                "execution_order": "alternated between repetitions",
                "data_read_and_conversion": "performed once before warm-up and measured runs",
                "same_prepared_arrays": True,
                "compute_wall_definition": "full timed processing phase, including Python orchestration and result merging but excluding common CSV read/conversion",
                "calculation_definition": "sum of native Cython kernel call durations only; Python result merging is excluded",
                "speedup_wall_definition": "sequential median compute wall time / parallel median compute wall time",
                "speedup_excludes_common_csv_read_and_conversion": True,
                "end_to_end_speedup_includes_common_preparation": True,
                "warmup_excluded_from_reported_medians": True,
                "small_workloads_may_run_slower_in_parallel_due_to_thread_scheduling_overhead": True,
                "compute_chunk_rows": COMPUTE_CHUNK_ROWS,
                "parallel_efficiency_uses_median_observed_workers": True,
                "automatic_next_dataset_generation_enabled": ENABLE_NEXT_SIZE_PROBE,
                "comprehensive_kernel_implementation": _comprehensive_kernel_mode() if config.get("operation") == "comprehensive_analytics" else None,
                "comprehensive_kernel_note": (
                    "The loaded native extension uses a fused single-pass kernel."
                    if _comprehensive_kernel_mode() == "fused_single_pass" else
                    "The loaded native extension predates the fused method; a compatibility adapter executes the existing numeric, threshold and group kernels separately for both engines. Rebuild the extension to use the fused single-pass kernel."
                ) if config.get("operation") == "comprehensive_analytics" else None,
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        report_path = WORKSPACE / f"{job_id}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        result["report_path"] = str(report_path)
        store.save_experiment({
            "experiment_id": job_id, "created_at": result["created_at"],
            "dataset_id": dataset["dataset_id"], "dataset_name": dataset["name"],
            "operation": config["operation"], "config": config,
            "result": {k: v for k, v in result.items() if k not in {"sequential", "parallel"}},
            "verified": verified, "status": "completed" if verified else "verification_failed",
        })
        with _job_lock:
            job = _jobs[job_id]
            job["status"] = "completed" if verified else "verification_failed"
            job["progress"] = 100.0
            job["result"] = result
        _logger.info("Benchmark completed | job=%s | status=%s | seq_compute=%.6f | par_compute=%.6f | preparation=%.3f | verified=%s",
                     job_id, "completed" if verified else "verification_failed", seq_median, par_median, preparation_seconds, verified)
        _emit(job_id, {"type": "job_complete", "status": "completed" if verified else "verification_failed", "result": result})
    except InterruptedError as exc:
        total_rows = int(locals().get("dataset", {}).get("rows", 0) or 0)
        failure_event = {
            "type": "job_status", "status": "cancelled", "phase": "CANCELLED",
            "records_processed": 0, "total_records": total_rows, "progress": 0.0,
            "message": str(exc),
        }
        with _job_lock:
            _jobs[job_id]["status"] = "cancelled"
            _jobs[job_id]["error"] = str(exc)
            _jobs[job_id]["progress"] = 0.0
        _emit(job_id, failure_event)
    except Exception as exc:
        _logger.exception("Benchmark failed | job=%s", job_id)
        total_rows = int(locals().get("dataset", {}).get("rows", 0) or 0)
        failure_event = {
            "type": "job_status", "status": "failed", "phase": "FAILED",
            "records_processed": 0, "total_records": total_rows, "progress": 0.0,
            "message": f"{type(exc).__name__}: {exc}",
        }
        with _job_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = failure_event["message"]
            _jobs[job_id]["progress"] = 0.0
        _emit(job_id, failure_event)
    finally:
        _release_prepared_arrays(locals().get("prepared"))
        if temp_dir is not None:
            temp_dir.cleanup()

def resource_limits() -> dict[str, Any]:
    logical_processors = max(1, int(psutil.cpu_count(logical=True) or 1))
    physical_cores = max(1, int(psutil.cpu_count(logical=False) or logical_processors))
    max_threads = min(logical_processors, physical_cores, _env_int("PBDA_MAX_BENCHMARK_THREADS", 4, 1, 16))
    return {
        "prepared_ram_limit_mb": PREPARED_RAM_LIMIT_BYTES // (1024 * 1024),
        "parser_block_size_bytes": ARROW_BLOCK_SIZE_BYTES,
        "compute_chunk_rows": COMPUTE_CHUNK_ROWS,
        "max_threads": max_threads,
        "physical_cores_detected": physical_cores,
        "logical_processors_detected": logical_processors,
        "automatic_next_size_probe": ENABLE_NEXT_SIZE_PROBE,
        "profile": "adaptive_memory_safe",
        "ram_policy": "in-memory when prepared data fits min(cap, 50% of free RAM); otherwise memory-mapped",
    }


def kernel_runtime_capabilities() -> dict[str, Any]:
    """Describe functions exported by the binary currently loaded by Python."""
    if kernels is None:
        return {"native_engine_loaded": False, "openmp": False, "comprehensive_mode": "unavailable"}
    try:
        info = kernels.engine_info()
    except Exception as exc:
        return {"native_engine_loaded": False, "openmp": False, "error": str(exc),
                "comprehensive_mode": "unavailable"}
    return {
        "native_engine_loaded": bool(info.get("compiled")),
        "openmp": bool(info.get("openmp")),
        "available_functions": [name for name in (
            "numeric_stats_seq", "numeric_stats_parallel",
            "threshold_count_seq", "threshold_count_parallel",
            "frequency_seq", "frequency_parallel",
            "group_sum_seq", "group_sum_parallel",
            "comprehensive_analytics_seq", "comprehensive_analytics_parallel",
        ) if callable(getattr(kernels, name, None))],
        "comprehensive_mode": _comprehensive_kernel_mode(),
        "comprehensive_fused_available": _supports_fused_comprehensive_kernel(),
    }


def start_worker(job_id: str, executor) -> None:
    executor.submit(_worker, job_id)
