from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from app.services.datasets import generate_dataset

_logger = logging.getLogger("pbda.generation")
_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()
_subscribers: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}


def _emit(job_id: str, event: dict[str, Any]) -> None:
    with _lock:
        item = _jobs.get(job_id)
        if item:
            item["latest"] = {**event, "timestamp": time.time()}
        queues = list(_subscribers.get(job_id, []))
    for loop, q in queues:
        if loop.is_closed():
            continue

        def deliver(target: asyncio.Queue = q, payload: dict[str, Any] = event) -> None:
            try:
                target.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    target.get_nowait()
                    target.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

        # The generator executes in a worker thread; marshal events back onto
        # the WebSocket's event loop instead of touching asyncio.Queue directly.
        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            continue


def create_job(config: dict[str, Any]) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {
            "generation_id": job_id, "status": "queued", "created_at": datetime.now(timezone.utc).isoformat(),
            "config": config, "progress": 0.0, "latest": {}, "result": None,
            "error": None, "cancel": threading.Event(),
            "started_at_epoch": None, "last_emit_monotonic": 0.0, "progress_state": {
                "generated_rows": 0, "total_rows": int(config.get("rows", 0)),
                "progress": 0.0, "batch_number": 0, "total_batches": 0,
                "elapsed_seconds": 0.0, "records_per_second": 0.0,
                "file_size_bytes": 0, "writer": "Preparing writer",
                "phase": "queued", "message": "Waiting for a generation worker.",
            },
        }
    return job_id


def worker(job_id: str) -> None:
    with _lock:
        job = _jobs[job_id]
        cfg = dict(job["config"])
        cancel = job["cancel"]
        job["status"] = "generating"
        job["started_at_epoch"] = time.time()
    _logger.info("Generation started | job=%s | kind=%s | rows=%s | requested_batch=%s",
                 job_id, cfg.get("kind"), cfg.get("rows"), cfg.get("batch_size"))
    _emit(job_id, {
        "type": "generation_progress", "status": "generating", "phase": "preparing",
        "message": "Preparing the first data chunk.", "generated_rows": 0,
        "total_rows": int(cfg.get("rows", 0)), "progress": 0.0,
        "elapsed_seconds": 0.0, "records_per_second": 0.0,
        "estimated_remaining_seconds": None, "file_size_bytes": 0,
        "batch_number": 0, "total_batches": 0,
    })
    try:
        def progress(data: dict[str, Any]) -> None:
            now = time.monotonic()
            phase = str(data.get("phase", "working"))
            generated_rows = int(data.get("generated_rows", 0))
            total_rows = int(data.get("total_rows", 0))
            with _lock:
                item = _jobs[job_id]
                item["progress"] = float(data.get("progress", 0))
                # Persist every real sub-block snapshot so REST polling can always
                # recover progress, even when WebSocket events are coalesced.
                item["progress_state"] = dict(data)
                if item["status"] != "cancelling":
                    item["status"] = "generating"
                last_emit = float(item.get("last_emit_monotonic", 0.0))
                force_emit = phase in {"preparing", "completed"} or generated_rows >= total_rows
                should_emit = force_emit or (now - last_emit >= 0.25)
                if should_emit:
                    item["last_emit_monotonic"] = now
            # Keep a compact diagnostic trail without writing a log line for
            # every small write block on a 50-million-row dataset.
            if generated_rows == 0 or generated_rows % 100_000 == 0 or generated_rows >= total_rows:
                _logger.info(
                    "Generation progress | job=%s | phase=%s | rows=%s/%s | percent=%.1f | elapsed=%.2fs | rate=%.0f rows/s | file_bytes=%s | writer=%s",
                    job_id, phase, generated_rows, total_rows, float(data.get("progress", 0)),
                    float(data.get("elapsed_seconds", 0)), float(data.get("records_per_second", 0)),
                    int(data.get("file_size_bytes", 0)), data.get("writer", "unknown"),
                )
            if should_emit:
                _emit(job_id, {"type": "generation_progress", **data})
        result = generate_dataset(
            kind=cfg["kind"], rows=int(cfg["rows"]), seed=int(cfg.get("seed", 42)),
            batch_size=int(cfg.get("batch_size", 100_000)), name=cfg.get("name"),
            progress=progress, cancel_check=cancel.is_set,
        )
        with _lock:
            _jobs[job_id]["status"] = "completed"
            _jobs[job_id]["progress"] = 100.0
            _jobs[job_id]["result"] = result
        _logger.info("Generation completed | job=%s | rows=%s | seconds=%.3f | file=%s | writer=%s",
                     job_id, result.get("rows"), result.get("generation_seconds", 0),
                     result.get("filename"), result.get("writer"))
        _emit(job_id, {"type": "generation_complete", "status": "completed", "dataset": result})
    except InterruptedError as exc:
        with _lock:
            _jobs[job_id]["status"] = "cancelled"
            _jobs[job_id]["error"] = str(exc)
        _logger.info("Generation cancelled | job=%s", job_id)
        _emit(job_id, {"type": "generation_status", "status": "cancelled", "message": str(exc)})
    except Exception as exc:
        _logger.exception("Generation failed | job=%s", job_id)
        with _lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = f"{type(exc).__name__}: {exc}"
        _emit(job_id, {"type": "generation_status", "status": "failed", "message": f"{type(exc).__name__}: {exc}"})


def start_worker(job_id: str, executor) -> None:
    executor.submit(worker, job_id)


def cancel_active_jobs() -> None:
    """Request cooperative cancellation of every queued or running generation."""
    with _lock:
        for item in _jobs.values():
            if item.get("status") in {"queued", "generating"}:
                item["cancel"].set()


def has_active_jobs() -> bool:
    with _lock:
        return any(x.get("status") in {"queued", "generating", "cancelling"} for x in _jobs.values())


def get_active_job() -> dict[str, Any] | None:
    with _lock:
        active = next((x for x in reversed(list(_jobs.values()))
                       if x.get("status") in {"queued", "generating", "cancelling"}), None)
        if active is None:
            return None
        return get_job(active["generation_id"])


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        item = _jobs.get(job_id)
        if not item:
            return None
        result = {k: v for k, v in item.items() if k != "cancel"}
        started = item.get("started_at_epoch")
        elapsed = max(0.0, time.time() - started) if started else 0.0
        result["elapsed_seconds"] = elapsed

        # The progress snapshot is refreshed on every status poll, even while
        # the worker is inside a chunk. The record percentage remains truthful,
        # but elapsed time/rate/ETA continue to move instead of looking frozen.
        progress = dict(item.get("progress_state") or {})
        total = int(progress.get("total_rows") or item.get("config", {}).get("rows", 0))
        generated = int(progress.get("generated_rows") or 0)
        rate = generated / elapsed if generated > 0 and elapsed > 0 else 0.0
        progress["elapsed_seconds"] = elapsed
        progress["records_per_second"] = rate
        progress["estimated_remaining_seconds"] = (
            (total - generated) / rate if rate > 0 and total >= generated else None
        )
        if item.get("status") == "cancelling":
            progress["message"] = "Cancellation received. Finishing the current small write block."
        elif not progress.get("message") and item.get("status") == "generating" and generated == 0:
            writer = progress.get("writer", "CSV writer")
            progress["message"] = f"Preparing the first small write block with {writer}."
        result["progress_state"] = progress
        return result


def cancel_job(job_id: str) -> bool:
    with _lock:
        item = _jobs.get(job_id)
        if not item or item["status"] in {"completed", "failed", "cancelled"}:
            return False
        item["cancel"].set()
        item["status"] = "cancelling"
        progress = dict(item.get("progress_state") or {})
    _logger.info(
        "Generation cancellation requested | job=%s | rows=%s/%s | batch=%s/%s",
        job_id, progress.get("generated_rows", 0), progress.get("total_rows", 0),
        progress.get("batch_number", 0), progress.get("total_batches", 0),
    )
    _emit(job_id, {
        "type": "generation_status", "status": "cancelling",
        "message": "Cancellation requested. The current small chunk is finishing; no partial dataset will be saved.",
    })
    return True


def subscribe(job_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    loop = asyncio.get_running_loop()
    with _lock:
        _subscribers.setdefault(job_id, []).append((loop, q))
    return q


def unsubscribe(job_id: str, q: asyncio.Queue) -> None:
    with _lock:
        subscribers = _subscribers.get(job_id, [])
        _subscribers[job_id] = [(loop, queue) for loop, queue in subscribers if queue is not q]
