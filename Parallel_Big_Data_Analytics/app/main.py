from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import platform
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

try:
    from app.engine import openmp_kernels
except Exception:
    openmp_kernels = None
from app.services import benchmark, datasets, generation, hardware_info, monitor, store

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pbda-worker")

app = FastAPI(
    title="Parallel Big Data Analytics",
    description="Local-first, real-hardware parallel analytics and benchmarking platform.",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
)


class GenerateRequest(BaseModel):
    kind: str
    rows: int = Field(ge=datasets.MIN_GENERATION_ROWS, le=datasets.MAX_DATASET_ROWS)
    seed: int = Field(default=42, ge=0)
    batch_size: int = Field(default=25_000, ge=10_000, le=100_000)
    name: str | None = None

    @field_validator("rows")
    @classmethod
    def require_supported_big_data_size(cls, value: int) -> int:
        if value not in datasets.RECOMMENDED_DEMO_ROW_PRESETS:
            choices = ", ".join((f"{n // 1_000_000}M" if n >= 1_000_000 else f"{n // 1_000}K") for n in datasets.GENERATION_ROW_PRESETS)
            raise ValueError(f"Choose one of the supported dataset sizes: {choices} records.")
        return value


class BenchmarkRequest(BaseModel):
    dataset_id: str
    operation: str
    column: str | None = None
    group_column: str | None = None
    threshold: float = 0.0
    top_k: int = Field(default=10, ge=1, le=100)
    date_column: str | None = None
    date_granularity: str = "month"
    threads: int = Field(default=2, ge=1, le=256)
    schedule: str = "static"
    repetitions: int = Field(default=3, ge=1, le=7)


@app.on_event("startup")
def startup() -> None:
    runtime_dir = ROOT / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_path = runtime_dir / "application.log"
    app_logger = logging.getLogger("pbda")
    app_logger.setLevel(logging.INFO)
    if not any(getattr(handler, "baseFilename", None) == str(log_path) for handler in app_logger.handlers):
        file_handler = RotatingFileHandler(
            log_path, maxBytes=3 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        ))
        app_logger.addHandler(file_handler)
    store.initialize()
    (ROOT / "workspace").mkdir(exist_ok=True)
    app_logger.info("Application started | root=%s | python=%s", ROOT, platform.python_version())


@app.on_event("shutdown")
def shutdown() -> None:
    # Ask long-running work to stop at the next completed processing/generation
    # batch, then let the executor finish cooperatively.
    benchmark.cancel_active_jobs()
    generation.cancel_active_jobs()
    executor.shutdown(wait=True, cancel_futures=True)


@app.get("/api/health")
def health() -> dict[str, Any]:
    try:
        if openmp_kernels is None:
            raise RuntimeError("Native Cython/OpenMP extension is not built. Run setup_windows.bat.")
        engine = openmp_kernels.engine_info()
        engine_error = None
    except Exception as exc:
        engine = {"compiled": False, "openmp": False, "engine": "Unavailable"}
        engine_error = str(exc)
    try:
        hardware = monitor.hardware_summary()
    except Exception as exc:
        hardware = {"error": str(exc)}
    return {
        "status": "ok", "application": "Parallel Big Data Analytics",
        "version": app.version, "engine": engine, "engine_error": engine_error,
        "hardware": hardware, "pyarrow_available": _pyarrow_available(),
        "kernel_capabilities": benchmark.kernel_runtime_capabilities(),
        "benchmark_limits": benchmark.resource_limits(),
        "local_execution": True,
    }


def _pyarrow_available() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except Exception:
        return False


@app.get("/api/hardware/summary")
def hardware_summary() -> dict[str, Any]:
    return monitor.hardware_summary()


@app.get("/api/hardware/live")
def hardware_live(light: bool = False) -> dict[str, Any]:
    # While a benchmark is active the UI requests light telemetry to avoid
    # enumerating every process, storage volume and GPU at each sample.
    return monitor.get_live_metrics(light=light)


def _workload_active() -> bool:
    return bool(benchmark.has_active_jobs() or generation.has_active_jobs())


@app.get("/api/hardware/details")
def hardware_details() -> dict[str, Any]:
    """Static hardware inventory (CPU generation, DDR type, SSD/HDD, GPU...). Cached; never collected while a workload runs."""
    return hardware_info.get_details(collect_allowed=not _workload_active())


@app.get("/api/hardware/live-extra")
def hardware_live_extra() -> dict[str, Any]:
    """GPU utilisation + volume usage for the Overview cards. Refused (paused) while a benchmark or generation runs."""
    if _workload_active():
        return {"paused": True, "available": False, "gpus": [], "volumes": []}
    return hardware_info.live_extra()


@app.get("/api/datasets/templates")
def dataset_templates() -> dict[str, Any]:
    return {"templates": [
        {"kind": key, "label": value["label"], "description": value["description"],
         "columns": [{"name": n, "type": t, "role": r} for n, t, r in value["columns"]],
         "numeric_columns": value["numeric_columns"], "category_columns": value["category_columns"],
         "date_columns": value["date_columns"]}
        for key, value in datasets.TEMPLATES.items()
    ]}




@app.get("/api/datasets/generation-options")
def dataset_generation_options() -> dict[str, Any]:
    """Backend contract for the dataset-size/chunk presets used by the UI."""
    return {
        "minimum_rows": datasets.MIN_GENERATION_ROWS,
        "minimum_benchmark_rows": datasets.MIN_BENCHMARK_ROWS,
        "maximum_rows": datasets.MAX_DATASET_ROWS,
        "generation_row_presets": datasets.GENERATION_ROW_PRESETS,
        "recommended_demo_row_presets": datasets.RECOMMENDED_DEMO_ROW_PRESETS,
        "generation_chunk_sizes": [10_000, 25_000, 50_000, 100_000],
        "recommended_demo_sequence": datasets.RECOMMENDED_DEMO_ROW_PRESETS,
        "notes": "Generation and benchmarking are available from 100K records. Start at 100K or 1M to validate the setup, then compare 5M, 10M, 25M, 50M and 75M. Large CSV files require substantial free disk space; check the preflight estimate before starting.",
    }


@app.post("/api/datasets/generate")
def generate_dataset(request: GenerateRequest) -> dict[str, Any]:
    if generation.has_active_jobs() or benchmark.has_active_jobs():
        raise HTTPException(409, "A generation or benchmark task is already active. Wait for it to finish before starting another workload.")
    if request.kind not in datasets.TEMPLATES:
        raise HTTPException(400, "Select one of the supported dataset templates.")
    config = request.model_dump()
    job_id = generation.create_job(config)
    generation.start_worker(job_id, executor)
    return {"generation_id": job_id, "status": "queued"}


@app.get("/api/datasets/generation/active")
def active_generation_status() -> dict[str, Any]:
    item = generation.get_active_job()
    return item or {"status": "idle", "latest": {}, "progress": 0.0}


@app.get("/api/datasets/generation/{generation_id}")
def generation_status(generation_id: str) -> dict[str, Any]:
    item = generation.get_job(generation_id)
    if not item:
        raise HTTPException(404, "Generation job not found.")
    return item


@app.post("/api/datasets/generation/{generation_id}/cancel")
def generation_cancel(generation_id: str) -> dict[str, Any]:
    if not generation.cancel_job(generation_id):
        raise HTTPException(409, "Generation cannot be cancelled.")
    return {"cancel_requested": True, "status": "cancelling"}


@app.get("/api/datasets")
def list_datasets() -> dict[str, Any]:
    return {"datasets": datasets.list_datasets()}


@app.get("/api/datasets/{dataset_id}/preview")
def preview_dataset(dataset_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    try:
        return datasets.preview_dataset(dataset_id, limit, offset)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/datasets/{dataset_id}/download")
def download_dataset(dataset_id: str):
    try:
        item = datasets.get_dataset(dataset_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    return FileResponse(item["path"], filename=item["filename"], media_type="text/csv")


@app.get("/api/benchmarks/operations")
def operations() -> dict[str, Any]:
    return {
        "operations": [
            {"id": "numeric_stats", "label": "Numeric Summary", "description": "Count, sum, mean, standard deviation, minimum and maximum.", "requires_numeric": True},
            {"id": "filter_gt", "label": "Threshold Filter Count", "description": "Count records where the selected numeric field exceeds a threshold.", "requires_numeric": True, "requires_threshold": True},
            {"id": "frequency", "label": "Category Frequency", "description": "Count the frequency of each category in a categorical column.", "requires_category": True},
            {"id": "group_sum", "label": "Group By + Sum", "description": "Calculate record counts and sums for each category using a numeric measure.", "requires_numeric": True, "requires_category": True},
            {"id": "top_k_groups", "label": "Top-K Groups by Measure", "description": "Aggregate a numeric measure by category and return the highest-ranked groups.", "requires_numeric": True, "requires_category": True, "requires_top_k": True},
            {"id": "date_frequency", "label": "Date Period Frequency", "description": "Count records by year, month or day using a date column.", "requires_date": True},
            {"id": "comprehensive_analytics", "label": "Full Big Data Analysis", "description": "In one pass, calculate numeric summary, count values above a threshold, and produce per-category count, sum and average.", "requires_numeric": True, "requires_category": True, "requires_threshold": True},
        ],
        "experimental_operations": [],
    }


@app.post("/api/benchmarks/preflight")
def benchmark_preflight(request: BenchmarkRequest) -> dict[str, Any]:
    """Compare what a run needs (RAM, disk, free CPU cores) with what the computer has right now."""
    try:
        config = request.model_dump()
        dataset = datasets.get_dataset(config["dataset_id"])
        if config["operation"] not in {"numeric_stats", "filter_gt", "frequency", "group_sum",
                                       "top_k_groups", "date_frequency", "comprehensive_analytics"}:
            raise ValueError("Unsupported analytical operation.")
        return benchmark.preflight(config, dataset)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/benchmarks")
def create_benchmark(request: BenchmarkRequest) -> dict[str, Any]:
    if benchmark.has_active_jobs() or generation.has_active_jobs():
        raise HTTPException(409, "Another generation or benchmark task is active. Wait for it to finish before starting a benchmark.")
    try:
        config = request.model_dump()
        selected_dataset = datasets.get_dataset(config["dataset_id"])
        if int(selected_dataset.get("rows", 0)) < datasets.MIN_BENCHMARK_ROWS:
            raise ValueError(
                "Benchmarking requires at least 100,000 records. "
                "Generate a dataset of 100K records or larger in Data Lab first."
            )
        job_id = benchmark.create_job(config)
        benchmark.start_worker(job_id, executor)
        return {"job_id": job_id, "status": "queued"}
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/benchmarks")
def benchmark_jobs() -> dict[str, Any]:
    return {"jobs": benchmark.list_jobs()}


@app.get("/api/benchmarks/active")
def active_benchmark_status() -> dict[str, Any]:
    item = benchmark.get_active_job()
    return item or {"job_id": None, "status": "idle"}


@app.get("/api/benchmarks/{job_id}")
def benchmark_status(job_id: str) -> dict[str, Any]:
    item = benchmark.get_job(job_id)
    if not item:
        raise HTTPException(404, "Benchmark job not found.")
    return item


@app.post("/api/benchmarks/{job_id}/cancel")
def benchmark_cancel(job_id: str) -> dict[str, Any]:
    if not benchmark.cancel_job(job_id):
        raise HTTPException(409, "Benchmark cannot be cancelled.")
    return {"cancel_requested": True}


@app.get("/api/history")
def history(limit: int = 100) -> dict[str, Any]:
    return {"experiments": store.list_experiments(limit)}


@app.get("/api/reports/{experiment_id}/json")
def report_json(experiment_id: str):
    path = benchmark.WORKSPACE / f"{experiment_id}.json"
    if not path.exists():
        raise HTTPException(404, "Report not found.")
    return FileResponse(path, filename=f"benchmark_{experiment_id}.json", media_type="application/json")


@app.get("/api/reports/{experiment_id}/csv")
def report_csv(experiment_id: str):
    path = benchmark.WORKSPACE / f"{experiment_id}.json"
    if not path.exists():
        raise HTTPException(404, "Report not found.")
    report = json.loads(path.read_text(encoding="utf-8"))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["metric", "sequential", "parallel", "unit"])
    writer.writerow(["median_wall_seconds", report["sequential"]["median_wall_seconds"], report["parallel"]["median_wall_seconds"], "seconds"])
    writer.writerow(["median_kernel_seconds", report["sequential"]["median_kernel_seconds"], report["parallel"]["median_kernel_seconds"], "seconds"])
    writer.writerow(["median_throughput", report["sequential"]["median_throughput"], report["parallel"]["median_throughput"], "records/second"])
    writer.writerow(["speedup_kernel", "", report.get("speedup_kernel"), "x"])
    writer.writerow(["parallel_efficiency_percent", "", report.get("parallel_efficiency_percent"), "%"])
    writer.writerow(["time_saved_percent", "", report.get("time_saved_percent"), "%"])
    writer.writerow(["preparation_seconds", report.get("preparation_seconds"), "", "seconds (shared once)"])
    writer.writerow(["end_to_end_speedup", "", report.get("end_to_end_speedup"), "x (includes shared preparation)"])
    writer.writerow(["warmup_runs_per_mode", report.get("warmup", {}).get("runs_per_mode"), "", "untimed; excluded from medians"])
    writer.writerow(["sequential_wall_time_spread_percent", report["sequential"].get("wall_time_spread_percent"), "", "% range relative to median"])
    writer.writerow(["parallel_wall_time_spread_percent", "", report["parallel"].get("wall_time_spread_percent"), "% range relative to median"])
    writer.writerow(["analysis_outcome", report.get("analysis", {}).get("outcome"), "", "performance interpretation"])
    writer.writerow(["analysis_confidence", report.get("analysis", {}).get("confidence"), "", "repeatability indicator"])
    writer.writerow(["analysis_headline", report.get("analysis", {}).get("headline"), "", "measured result interpretation"])
    for index, reason in enumerate(report.get("analysis", {}).get("reasons", []), start=1):
        writer.writerow([f"analysis_reason_{index}", reason, "", "diagnostic explanation"])
    writer.writerow(["recommended_next_test", report.get("analysis", {}).get("recommended_next_test"), "", "recommendation"])
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=benchmark_{experiment_id}.csv"})


@app.websocket("/ws/benchmarks/{job_id}")
async def benchmark_socket(websocket: WebSocket, job_id: str):
    await websocket.accept()
    queue = benchmark.subscribe(job_id)
    item = benchmark.get_job(job_id)
    if not item:
        benchmark.unsubscribe(job_id, queue)
        await websocket.send_json({"type": "error", "message": "Benchmark job not found."})
        await websocket.close()
        return
    try:
        if item.get("latest"):
            await websocket.send_json(item["latest"])
            if item["latest"].get("type") == "job_complete" or item.get("status") in {"completed", "verification_failed", "failed", "cancelled"}:
                await websocket.close()
                return
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
                await websocket.send_json(event)
                if event.get("type") in {"job_complete"} or event.get("status") in {"failed", "cancelled"}:
                    break
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "heartbeat", "timestamp": time.time()})
    except WebSocketDisconnect:
        pass
    finally:
        benchmark.unsubscribe(job_id, queue)


@app.websocket("/ws/generation/{generation_id}")
async def generation_socket(websocket: WebSocket, generation_id: str):
    await websocket.accept()
    item = generation.get_job(generation_id)
    if not item:
        await websocket.send_json({"type": "error", "message": "Generation job not found."})
        await websocket.close()
        return
    queue = generation.subscribe(generation_id)
    try:
        latest = item.get("latest")
        if latest:
            await websocket.send_json(latest)
        if item.get("status") in {"completed", "failed", "cancelled"}:
            await websocket.send_json({"type": "generation_status", "status": item["status"], "message": item.get("error")})
            return
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
                await websocket.send_json(event)
                if event.get("type") == "generation_complete" or event.get("status") in {"failed", "cancelled"}:
                    break
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "heartbeat", "timestamp": time.time()})
    except WebSocketDisconnect:
        pass
    finally:
        generation.unsubscribe(generation_id, queue)


@app.get("/api/system/info")
def system_info():
    engine = openmp_kernels.engine_info() if openmp_kernels is not None else {"compiled": False, "openmp": False, "engine": "Unavailable"}
    return {"hardware": monitor.hardware_summary(), "engine": engine,
            "python": platform.python_version(), "platform": platform.platform()}


app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
