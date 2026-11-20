from __future__ import annotations

import csv
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import psutil

try:
    import pyarrow as pa
    import pyarrow.csv as pacsv
except Exception:  # PyArrow is an accelerator; the NumPy writer remains available.
    pa = None
    pacsv = None

DATA_ROOT = Path(os.getenv("PBDA_DATA_DIR", "workspace/data/generated")).resolve()
META_ROOT = Path(os.getenv("PBDA_META_DIR", "workspace/data/metadata")).resolve()

# A selected generation chunk is subdivided into cancellable write blocks.
# Keep the block small enough for responsive progress/cancellation while the
# user-selected chunk still controls the higher-level generation grouping.
DEFAULT_WRITE_BLOCK_ROWS = 25_000
MIN_WRITE_BLOCK_ROWS = 500
MAX_WRITE_BLOCK_ROWS = 50_000
MIN_GENERATION_ROWS = 100_000
MIN_BENCHMARK_ROWS = 100_000
# Backwards-compatible name retained for modules/tests that imported it.
MIN_DEMO_ROWS = MIN_BENCHMARK_ROWS
MAX_DATASET_ROWS = 75_000_000
GENERATION_ROW_PRESETS = [
    100_000, 1_000_000, 5_000_000, 10_000_000,
    25_000_000, 50_000_000, 75_000_000,
]
RECOMMENDED_DEMO_ROW_PRESETS = GENERATION_ROW_PRESETS
# Conservative CSV-size estimates by template. These are deliberately higher
# than the measured average row length to leave room for variable field widths.
ESTIMATED_CSV_BYTES_PER_ROW = {
    "sales": 96, "finance": 96, "products": 104,
    "students": 88, "employees": 100, "attendance": 100,
}

TEMPLATES: dict[str, dict[str, Any]] = {
    "sales": {
        "label": "Sales Dataset",
        "description": "Transactions with products, categories, regions, quantities and revenue.",
        "columns": [
            ("Transaction_ID", "int64", "identifier"), ("Transaction_Date", "date", "date"),
            ("Product_ID", "int32", "identifier"), ("Product", "string", "category"),
            ("Category", "string", "category"), ("Region", "string", "category"),
            ("Quantity", "int32", "numeric"), ("Unit_Price", "float64", "numeric"),
            ("Revenue", "float64", "numeric"),
        ],
        "numeric_columns": ["Quantity", "Unit_Price", "Revenue"],
        "category_columns": ["Product", "Category", "Region"],
        "date_columns": ["Transaction_Date"],
    },
    "finance": {
        "label": "Finance Dataset",
        "description": "Synthetic financial transactions with account, type, category, amount and balance.",
        "columns": [
            ("Transaction_ID", "int64", "identifier"), ("Transaction_Date", "date", "date"),
            ("Account_ID", "int32", "identifier"), ("Transaction_Type", "string", "category"),
            ("Category", "string", "category"), ("Amount", "float64", "numeric"),
            ("Balance", "float64", "numeric"), ("Region", "string", "category"),
        ],
        "numeric_columns": ["Amount", "Balance"],
        "category_columns": ["Transaction_Type", "Category", "Region"],
        "date_columns": ["Transaction_Date"],
    },
    "products": {
        "label": "Product Inventory Dataset",
        "description": "Warehouse inventory, stock, unit costs and selling prices.",
        "columns": [
            ("Product_ID", "int64", "identifier"), ("Product_Name", "string", "category"),
            ("Category", "string", "category"), ("Warehouse", "string", "category"),
            ("Stock_Quantity", "int32", "numeric"), ("Unit_Cost", "float64", "numeric"),
            ("Selling_Price", "float64", "numeric"), ("Inventory_Value", "float64", "numeric"),
        ],
        "numeric_columns": ["Stock_Quantity", "Unit_Cost", "Selling_Price", "Inventory_Value"],
        "category_columns": ["Product_Name", "Category", "Warehouse"],
        "date_columns": [],
    },
    "students": {
        "label": "Student Performance Dataset",
        "description": "Student marks, departments, courses, semesters and attendance.",
        "columns": [
            ("Student_ID", "int64", "identifier"), ("Department", "string", "category"),
            ("Semester", "int32", "numeric"), ("Course_ID", "int32", "identifier"),
            ("Marks", "float64", "numeric"), ("Attendance_Percentage", "float64", "numeric"),
            ("Credits", "int32", "numeric"), ("Academic_Year", "string", "category"),
        ],
        "numeric_columns": ["Semester", "Marks", "Attendance_Percentage", "Credits"],
        "category_columns": ["Department", "Academic_Year"],
        "date_columns": [],
    },
    "employees": {
        "label": "Employee Dataset",
        "description": "Employees, departments, designations, experience and salary.",
        "columns": [
            ("Employee_ID", "int64", "identifier"), ("Department", "string", "category"),
            ("Designation", "string", "category"), ("Joining_Date", "date", "date"),
            ("Experience_Years", "int32", "numeric"), ("Salary", "float64", "numeric"),
            ("Region", "string", "category"),
        ],
        "numeric_columns": ["Experience_Years", "Salary"],
        "category_columns": ["Department", "Designation", "Region"],
        "date_columns": ["Joining_Date"],
    },
    "attendance": {
        "label": "Attendance Dataset",
        "description": "Employee attendance records, working hours, shifts and status.",
        "columns": [
            ("Attendance_ID", "int64", "identifier"), ("Employee_ID", "int32", "identifier"),
            ("Department", "string", "category"), ("Attendance_Date", "date", "date"),
            ("Attendance_Status", "string", "category"), ("Working_Hours", "float64", "numeric"),
            ("Shift", "string", "category"), ("Region", "string", "category"),
        ],
        "numeric_columns": ["Working_Hours"],
        "category_columns": ["Department", "Attendance_Status", "Shift", "Region"],
        "date_columns": ["Attendance_Date"],
    },
}

PRODUCTS = np.array(["Laptop", "Phone", "Headphones", "Monitor", "Keyboard", "Mouse", "Tablet", "Camera", "Speaker", "Printer"])
PRODUCT_CATEGORIES = np.array(["Computers", "Mobiles", "Audio", "Displays", "Accessories", "Accessories", "Mobiles", "Cameras", "Audio", "Office"])
REGIONS = np.array(["North", "South", "East", "West", "Central"])
DEPARTMENTS = np.array(["Computer Science", "Software Engineering", "Business", "Electrical", "Data Science", "Human Resources"])
DESIGNATIONS = np.array(["Analyst", "Engineer", "Manager", "Associate", "Specialist", "Administrator"])


def _make_arrays(kind: str, start: int, count: int, rng: np.random.Generator) -> dict[str, np.ndarray]:
    ix = np.arange(start, start + count, dtype=np.int64)
    if kind == "sales":
        prod = rng.integers(0, len(PRODUCTS), count, dtype=np.int32)
        qty = rng.integers(1, 15, count, dtype=np.int32)
        price = np.round(rng.uniform(150.0, 250000.0, count), 2)
        return {
            "Transaction_ID": ix + 1000001,
            "Transaction_Date": (np.datetime64("2022-01-01") + rng.integers(0, 1461, count).astype("timedelta64[D]")).astype(str),
            "Product_ID": prod.astype(np.int32) + 1,
            "Product": PRODUCTS[prod], "Category": PRODUCT_CATEGORIES[prod],
            "Region": REGIONS[rng.integers(0, len(REGIONS), count)],
            "Quantity": qty, "Unit_Price": price,
            "Revenue": np.round(qty * price, 2),
        }
    if kind == "finance":
        amount = np.round(rng.uniform(100, 250000, count), 2)
        tx_types = np.array(["Credit", "Debit", "Transfer", "Payment"])
        cats = np.array(["Salary", "Shopping", "Bills", "Investment", "Travel", "Food"])
        return {
            "Transaction_ID": ix + 5000001,
            "Transaction_Date": (np.datetime64("2021-01-01") + rng.integers(0, 1826, count).astype("timedelta64[D]")).astype(str),
            "Account_ID": rng.integers(10000, 90000, count, dtype=np.int32),
            "Transaction_Type": tx_types[rng.integers(0, len(tx_types), count)],
            "Category": cats[rng.integers(0, len(cats), count)],
            "Amount": amount,
            "Balance": np.round(rng.uniform(500, 5000000, count), 2),
            "Region": REGIONS[rng.integers(0, len(REGIONS), count)],
        }
    if kind == "products":
        qty = rng.integers(0, 5000, count, dtype=np.int32)
        cost = np.round(rng.uniform(50, 150000, count), 2)
        price = np.round(cost * rng.uniform(1.05, 1.8, count), 2)
        prod = rng.integers(0, len(PRODUCTS), count)
        return {
            "Product_ID": ix + 1, "Product_Name": PRODUCTS[prod],
            "Category": PRODUCT_CATEGORIES[prod],
            "Warehouse": np.array(["WH-East", "WH-West", "WH-North", "WH-South"])[rng.integers(0, 4, count)],
            "Stock_Quantity": qty, "Unit_Cost": cost, "Selling_Price": price,
            "Inventory_Value": np.round(qty * cost, 2),
        }
    if kind == "students":
        return {
            "Student_ID": ix + 200001,
            "Department": DEPARTMENTS[rng.integers(0, len(DEPARTMENTS), count)],
            "Semester": rng.integers(1, 9, count, dtype=np.int32),
            "Course_ID": rng.integers(100, 650, count, dtype=np.int32),
            "Marks": np.round(np.clip(rng.normal(69, 15, count), 0, 100), 2),
            "Attendance_Percentage": np.round(np.clip(rng.normal(82, 12, count), 0, 100), 2),
            "Credits": rng.integers(1, 5, count, dtype=np.int32),
            "Academic_Year": np.array(["2023-24", "2024-25", "2025-26", "2026-27"])[rng.integers(0, 4, count)],
        }
    if kind == "employees":
        experience = rng.integers(0, 26, count, dtype=np.int32)
        extra_days = rng.integers(0, 365, count, dtype=np.int32)
        joined = (np.datetime64("2026-11-17") - (experience.astype(np.int64) * 365 + extra_days).astype("timedelta64[D]")).astype(str)
        return {
            "Employee_ID": ix + 10001,
            "Department": DEPARTMENTS[rng.integers(0, len(DEPARTMENTS), count)],
            "Designation": DESIGNATIONS[rng.integers(0, len(DESIGNATIONS), count)],
            "Joining_Date": joined,
            "Experience_Years": experience,
            "Salary": np.round(rng.uniform(25000, 450000, count), 2),
            "Region": REGIONS[rng.integers(0, len(REGIONS), count)],
        }
    if kind == "attendance":
        emp = rng.integers(0, 50000, count, dtype=np.int32)
        statuses = np.array(["Present", "Absent", "Late", "Leave"])
        status = statuses[rng.integers(0, len(statuses), count)]
        hours = np.clip(rng.normal(8.1, 0.9, count), 6, 10)
        hours = np.where(status == "Late", np.clip(hours - 1.5, 4, 8), hours)
        hours = np.where((status == "Absent") | (status == "Leave"), 0.0, hours)
        return {
            "Attendance_ID": ix + 9000001, "Employee_ID": emp + 1,
            "Department": DEPARTMENTS[emp % len(DEPARTMENTS)],
            "Attendance_Date": (np.datetime64("2024-01-01") + rng.integers(0, 1096, count).astype("timedelta64[D]")).astype(str),
            "Attendance_Status": status,
            "Working_Hours": np.round(hours, 2),
            "Shift": np.array(["Morning", "Evening", "Night"])[rng.integers(0, 3, count)],
            "Region": REGIONS[rng.integers(0, len(REGIONS), count)],
        }
    raise ValueError(f"Unknown dataset type: {kind}")


def _write_batch_csv(path: Path, arrays: dict[str, np.ndarray], columns: list[tuple[str, str, str]], writer_state: dict[str, Any]) -> None:
    names = [c[0] for c in columns]
    if pa is not None and pacsv is not None:
        arrow_arrays = []
        arrow_fields = []
        for name, dtype, _role in columns:
            arr = arrays[name]
            if dtype == "int64": arrow_type = pa.int64()
            elif dtype == "int32": arrow_type = pa.int32()
            elif dtype == "float64": arrow_type = pa.float64()
            else: arrow_type = pa.string()
            arrow_arrays.append(pa.array(arr, type=arrow_type))
            arrow_fields.append(pa.field(name, arrow_type))
        table = pa.Table.from_arrays(arrow_arrays, schema=pa.schema(arrow_fields))
        if writer_state.get("writer") is None:
            writer_state["writer"] = pacsv.CSVWriter(str(path), table.schema)
        writer = writer_state["writer"]
        writer.write_table(table)
        flush = getattr(writer, "flush", None)
        if callable(flush):
            try:
                flush()
            except (NotImplementedError, AttributeError):
                pass
    else:
        matrix = np.column_stack([arrays[name] for name in names])
        # Keep one append handle open for the complete generation job. Reopening
        # the CSV for every 5k-row block causes avoidable filesystem overhead on
        # multi-million-row datasets, especially on Windows.
        handle = writer_state.get("file_handle")
        if handle is None:
            handle = path.open("wb")
            writer_state["file_handle"] = handle
        if not writer_state.get("header_written"):
            handle.write((",".join(names) + "\n").encode("utf-8"))
            writer_state["header_written"] = True
        np.savetxt(handle, matrix, delimiter=",", fmt="%s")
        handle.flush()


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TB"


def _emit_progress(progress: Callable[[dict[str, Any]], None] | None, payload: dict[str, Any]) -> None:
    if progress:
        progress(payload)


def generate_dataset(
    kind: str,
    rows: int,
    seed: int = 42,
    batch_size: int = 25_000,
    name: str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    data_root: Path | str | None = None,
    meta_root: Path | str | None = None,
) -> dict[str, Any]:
    """Generate a CSV incrementally, with real per-write progress and cancellation.

    The user's selected batch size is retained as a generation group size. Each
    group is split into smaller write blocks (normally 25,000 rows) so the UI can
    report real progress and the cancel event is checked frequently. A completed
    row is counted only after its block has been written successfully.
    """
    if kind not in TEMPLATES:
        raise ValueError(f"Unsupported dataset type: {kind}")
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 1 or rows > MAX_DATASET_ROWS:
        raise ValueError("Record count must be between 1 and 75,000,000.")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("Generation chunk size must be a positive whole number.")

    requested_batch_size = max(10_000, min(int(batch_size), 100_000))
    available_ram = int(psutil.virtual_memory().available)
    # Estimate temporary in-flight arrays conservatively; the 100k ceiling is
    # intentional even if the user has abundant RAM.
    memory_batch_limit = max(5_000, min(100_000, int((available_ram * 0.02) / 256)))
    generation_chunk_size = max(1_000, min(requested_batch_size, memory_batch_limit))
    configured_write_block = int(os.getenv("PBDA_GENERATION_WRITE_BLOCK_ROWS", str(DEFAULT_WRITE_BLOCK_ROWS)))
    if configured_write_block < MIN_WRITE_BLOCK_ROWS or configured_write_block > MAX_WRITE_BLOCK_ROWS:
        configured_write_block = DEFAULT_WRITE_BLOCK_ROWS
    write_block_size = min(generation_chunk_size, configured_write_block)

    # Optional isolated directories are used by the benchmark's temporary
    # recommendation trial. Normal user-generated datasets still use defaults.
    output_data_root = Path(data_root).resolve() if data_root is not None else DATA_ROOT
    output_meta_root = Path(meta_root).resolve() if meta_root is not None else META_ROOT
    output_data_root.mkdir(parents=True, exist_ok=True)
    output_meta_root.mkdir(parents=True, exist_ok=True)
    estimated_bytes_per_row = ESTIMATED_CSV_BYTES_PER_ROW[kind]
    estimated_file_bytes = int(rows * estimated_bytes_per_row)
    required_disk_bytes = int(estimated_file_bytes * 1.15)
    free_disk_bytes = int(shutil.disk_usage(output_data_root).free)
    if free_disk_bytes < required_disk_bytes:
        raise ValueError(
            f"Insufficient disk space estimate: approximately {_human_bytes(required_disk_bytes)} "
            f"required including reserve, {_human_bytes(free_disk_bytes)} available. "
            "Choose fewer records or free disk space."
        )

    template = TEMPLATES[kind]
    dataset_id = uuid.uuid4().hex[:12]
    requested_name = (name or f"{kind}_{rows:,}".replace(",", "")).strip()
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", requested_name).strip("_")[:60] or kind
    filename = f"{safe_name}_{dataset_id}.csv"
    final_path = output_data_root / filename
    temp_path = output_data_root / f".{safe_name}_{dataset_id}.csv.part"
    meta_path = output_meta_root / f"{dataset_id}.json"
    temp_meta = output_meta_root / f".{dataset_id}.json.part"

    rng = np.random.default_rng(seed)
    writer_state: dict[str, Any] = {"writer": None, "file_handle": None, "header_written": False}
    started = time.perf_counter()
    generated = 0
    completed_generation_groups = 0
    total_generation_groups = (rows + generation_chunk_size - 1) // generation_chunk_size
    total_write_blocks = (rows + write_block_size - 1) // write_block_size
    writer_name = "PyArrow CSVWriter" if pa is not None and pacsv is not None else "NumPy batch writer"

    def check_cancel() -> None:
        if cancel_check and cancel_check():
            raise InterruptedError("Dataset generation cancelled by user. The incomplete dataset was discarded.")

    def publish_progress(*, phase: str, message: str, block_number: int, group_number: int,
                         block_rows: int = 0, current_block_start: int = 0) -> None:
        elapsed = time.perf_counter() - started
        rate = generated / elapsed if elapsed > 0 else 0.0
        file_size = temp_path.stat().st_size if temp_path.exists() else 0
        _emit_progress(progress, {
            "status": "generating", "phase": phase, "message": message,
            "generated_rows": generated, "total_rows": rows,
            "progress": generated / rows * 100.0,
            "elapsed_seconds": elapsed, "records_per_second": rate,
            "estimated_remaining_seconds": (rows - generated) / rate if rate > 0 else None,
            "file_size_bytes": file_size, "writer": writer_name,
            "batch_number": group_number, "total_batches": total_generation_groups,
            "batch_size": generation_chunk_size,
            "write_block_number": block_number, "total_write_blocks": total_write_blocks,
            "write_block_size": write_block_size, "current_block_rows": block_rows,
            "current_block_start": current_block_start, "batch_rows": block_rows,
            "generation_group_rows": min(generation_chunk_size, rows - ((group_number - 1) * generation_chunk_size)),
        })

    _emit_progress(progress, {
        "status": "generating", "phase": "preparing",
        "message": f"Preparing {rows:,} records. First write block: up to {write_block_size:,} rows.",
        "generated_rows": 0, "total_rows": rows, "progress": 0.0,
        "batch_number": 0, "total_batches": total_generation_groups,
        "batch_size": generation_chunk_size, "write_block_number": 0,
        "total_write_blocks": total_write_blocks, "write_block_size": write_block_size,
        "elapsed_seconds": 0.0, "records_per_second": 0.0,
        "estimated_remaining_seconds": None, "file_size_bytes": 0, "writer": writer_name,
    })

    try:
        while generated < rows:
            check_cancel()
            group_number = generated // generation_chunk_size + 1
            group_end = min(rows, generated + generation_chunk_size)

            while generated < group_end:
                check_cancel()
                count = min(write_block_size, group_end - generated)
                block_number = generated // write_block_size + 1
                current_start = generated + 1
                current_end = generated + count
                publish_progress(
                    phase="generating",
                    message=(f"Generating rows {current_start:,}–{current_end:,} "
                             f"(write block {block_number:,}/{total_write_blocks:,}; "
                             f"generation chunk {group_number:,}/{total_generation_groups:,})."),
                    block_number=block_number, group_number=group_number,
                    block_rows=count, current_block_start=current_start,
                )
                check_cancel()
                arrays = _make_arrays(kind, generated, count, rng)
                check_cancel()
                publish_progress(
                    phase="writing",
                    message=(f"Writing {count:,} generated rows to CSV "
                             f"(block {block_number:,}/{total_write_blocks:,})."),
                    block_number=block_number, group_number=group_number,
                    block_rows=count, current_block_start=current_start,
                )
                check_cancel()
                _write_batch_csv(temp_path, arrays, template["columns"], writer_state)
                generated += count

                publish_progress(
                    phase="saved",
                    message=(f"Saved {generated:,} of {rows:,} records "
                             f"(block {block_number:,}/{total_write_blocks:,}; "
                             f"chunk {group_number:,}/{total_generation_groups:,})."),
                    block_number=block_number, group_number=group_number,
                    block_rows=count, current_block_start=current_start,
                )
                check_cancel()


        check_cancel()
        publish_progress(
            phase="finalizing", message="All records are written. Closing the CSV file and publishing dataset metadata.",
            block_number=total_write_blocks, group_number=total_generation_groups,
        )
        check_cancel()
        if writer_state.get("writer") is not None:
            writer_state["writer"].close()
            writer_state["writer"] = None
        if writer_state.get("file_handle") is not None:
            writer_state["file_handle"].flush()
            writer_state["file_handle"].close()
            writer_state["file_handle"] = None
        check_cancel()

        # Atomically expose the CSV only after the writer is closed and the final
        # cancellation checkpoint has passed.
        temp_path.replace(final_path)
        check_cancel()
        size = final_path.stat().st_size
        elapsed = time.perf_counter() - started
        metadata = {
            "dataset_id": dataset_id, "name": safe_name, "kind": kind,
            "label": template["label"], "description": template["description"],
            "filename": filename, "path": str(final_path), "rows": rows,
            "columns": [{"name": n, "type": t, "role": r} for n, t, r in template["columns"]],
            "numeric_columns": template["numeric_columns"],
            "category_columns": template["category_columns"],
            "date_columns": template["date_columns"],
            "seed": int(seed), "batch_size": generation_chunk_size,
            "requested_batch_size": requested_batch_size,
            "write_block_size": write_block_size,
            "available_ram_at_start_bytes": available_ram,
            "estimated_bytes_per_row": estimated_bytes_per_row,
            "estimated_file_bytes_before_generation": estimated_file_bytes,
            "file_size_bytes": size, "file_size_human": _human_bytes(size),
            "generation_seconds": elapsed,
            "generation_records_per_second": rows / elapsed if elapsed else 0.0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "writer": writer_name,
        }
        temp_meta.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        check_cancel()
        temp_meta.replace(meta_path)
        check_cancel()
        metadata["metadata_path"] = str(meta_path)
        if progress:
            progress({
                "status": "generating", "phase": "completed",
                "message": f"Dataset saved: {rows:,} records.",
                "generated_rows": rows, "total_rows": rows, "progress": 100.0,
                "elapsed_seconds": elapsed, "records_per_second": rows / elapsed if elapsed else 0.0,
                "estimated_remaining_seconds": 0.0, "file_size_bytes": size,
                "writer": writer_name, "batch_number": total_generation_groups,
                "total_batches": total_generation_groups, "batch_size": generation_chunk_size,
                "write_block_size": write_block_size, "write_block_number": total_write_blocks,
                "total_write_blocks": total_write_blocks,
            })
        return metadata
    except Exception:
        if writer_state.get("writer") is not None:
            try:
                writer_state["writer"].close()
            except Exception:
                pass
            writer_state["writer"] = None
        if writer_state.get("file_handle") is not None:
            try:
                writer_state["file_handle"].close()
            except Exception:
                pass
            writer_state["file_handle"] = None
        for partial in (temp_path, final_path, meta_path, temp_meta):
            try:
                partial.unlink(missing_ok=True)
            except Exception:
                pass
        raise


def list_datasets() -> list[dict[str, Any]]:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    META_ROOT.mkdir(parents=True, exist_ok=True)
    items = []
    for p in META_ROOT.glob("*.json"):
        try:
            item = json.loads(p.read_text(encoding="utf-8"))
            if Path(item.get("path", "")).exists():
                items.append(item)
        except Exception:
            continue
    return sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)


def get_dataset(dataset_id: str) -> dict[str, Any]:
    p = META_ROOT / f"{dataset_id}.json"
    if not p.exists():
        raise FileNotFoundError("Dataset metadata was not found.")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not Path(data["path"]).exists():
        raise FileNotFoundError("Dataset file is missing from local storage.")
    return data


def preview_dataset(dataset_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    item = get_dataset(dataset_id)
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    with open(item["path"], "r", newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        headers = next(reader, [])
        rows = []
        for index, row in enumerate(reader):
            if index < offset:
                continue
            if len(rows) >= limit:
                break
            rows.append(row)
    return {"dataset": item, "headers": headers, "rows": rows, "offset": offset, "limit": limit}
