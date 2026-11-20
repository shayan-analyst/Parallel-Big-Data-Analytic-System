import csv

import pytest

from app.services import datasets


@pytest.mark.parametrize("kind", list(datasets.TEMPLATES))
def test_dataset_generation_and_preview(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(datasets, "DATA_ROOT", tmp_path / "generated")
    monkeypatch.setattr(datasets, "META_ROOT", tmp_path / "metadata")
    item = datasets.generate_dataset(kind, 123, seed=99, batch_size=10_000)
    assert item["rows"] == 123
    assert item["file_size_bytes"] > 0
    preview = datasets.preview_dataset(item["dataset_id"], limit=7)
    assert len(preview["headers"]) == len(item["columns"])
    assert len(preview["rows"]) == 7
    assert preview["headers"][0] == item["columns"][0]["name"]


def test_seed_reproducibility(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "DATA_ROOT", tmp_path / "generated")
    monkeypatch.setattr(datasets, "META_ROOT", tmp_path / "metadata")
    first = datasets.generate_dataset("sales", 100, seed=123, batch_size=10_000)
    second = datasets.generate_dataset("sales", 100, seed=123, batch_size=10_000)
    with open(first["path"], newline="", encoding="utf-8") as f1, open(second["path"], newline="", encoding="utf-8") as f2:
        assert list(csv.reader(f1)) == list(csv.reader(f2))


def test_sales_generator_respects_revenue_relationship(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "DATA_ROOT", tmp_path / "generated")
    monkeypatch.setattr(datasets, "META_ROOT", tmp_path / "metadata")
    item = datasets.generate_dataset("sales", 250, seed=314, batch_size=10_000)
    with open(item["path"], newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 250
    for row in rows:
        expected = round(int(row["Quantity"]) * float(row["Unit_Price"]), 2)
        assert round(float(row["Revenue"]), 2) == expected


def test_generation_reports_initial_and_small_chunk_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "DATA_ROOT", tmp_path / "generated")
    monkeypatch.setattr(datasets, "META_ROOT", tmp_path / "metadata")
    updates = []

    item = datasets.generate_dataset(
        "sales", 25_000, seed=5, batch_size=500_000,
        progress=lambda update: updates.append(dict(update)),
    )

    assert item["rows"] == 25_000
    assert updates[0]["generated_rows"] == 0
    assert updates[0]["progress"] == 0.0
    written = [u for u in updates if u.get("generated_rows", 0) > 0]
    assert written
    assert written[-1]["generated_rows"] == 25_000
    assert all(u.get("batch_rows", 0) <= 100_000 for u in written)
    assert [u["generated_rows"] for u in written] == sorted(u["generated_rows"] for u in written)


def test_generation_cancellation_discards_partial_dataset(tmp_path, monkeypatch):
    data_root = tmp_path / "generated"
    meta_root = tmp_path / "metadata"
    monkeypatch.setattr(datasets, "DATA_ROOT", data_root)
    monkeypatch.setattr(datasets, "META_ROOT", meta_root)
    updates = []
    cancelled = {"value": False}

    def progress(update):
        updates.append(dict(update))
        if int(update.get("generated_rows", 0)) >= 10_000:
            cancelled["value"] = True

    with pytest.raises(InterruptedError, match="cancelled"):
        datasets.generate_dataset(
            "sales", 50_000, seed=5, batch_size=500_000,
            progress=progress, cancel_check=lambda: cancelled["value"],
        )

    assert any(u.get("generated_rows", 0) >= 10_000 for u in updates)
    assert not list(data_root.glob("*.csv"))
    assert not list(data_root.glob("*.part"))
    assert not list(meta_root.glob("*.json"))
    assert not list(meta_root.glob("*.part"))


def test_generation_honours_selected_chunk_size_and_reports_small_write_blocks(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(datasets, "DATA_ROOT", tmp_path / "generated")
    monkeypatch.setattr(datasets, "META_ROOT", tmp_path / "metadata")
    monkeypatch.setattr(datasets.psutil, "virtual_memory", lambda: SimpleNamespace(available=8 * 1024**3))
    updates = []
    item = datasets.generate_dataset(
        "sales", 62_000, seed=7, batch_size=25_000,
        progress=lambda update: updates.append(dict(update)),
    )
    saved = [u for u in updates if u.get("phase") == "saved"]
    assert item["rows"] == 62_000
    assert item["estimated_bytes_per_row"] == datasets.ESTIMATED_CSV_BYTES_PER_ROW["sales"]
    assert item["estimated_file_bytes_before_generation"] == 62_000 * 96
    assert item["batch_size"] == 25_000  # user-selected generation group is retained
    assert item["write_block_size"] == 25_000  # bounded blocks keep writes efficient and cancellation responsive
    assert saved
    assert saved[-1]["generated_rows"] == 62_000
    assert all(0 < u["batch_rows"] <= 25_000 for u in saved)
    assert {u["batch_number"] for u in saved} == {1, 2, 3}
    assert {u["generation_group_rows"] for u in saved} == {25_000, 12_000}
    assert [u["generated_rows"] for u in saved] == sorted(u["generated_rows"] for u in saved)


def test_progress_announces_generating_and_writing_before_first_saved_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "DATA_ROOT", tmp_path / "generated")
    monkeypatch.setattr(datasets, "META_ROOT", tmp_path / "metadata")
    updates = []
    datasets.generate_dataset(
        "sales", 12_000, seed=9, batch_size=10_000,
        progress=lambda update: updates.append(dict(update)),
    )
    phases = [u["phase"] for u in updates]
    assert phases[0] == "preparing"
    assert "generating" in phases and "writing" in phases and "saved" in phases
    first_generate = next(u for u in updates if u["phase"] == "generating")
    assert first_generate["generated_rows"] == 0
    assert first_generate["current_block_rows"] <= 25_000
    first_saved = next(u for u in updates if u["phase"] == "saved")
    assert first_saved["generated_rows"] == first_saved["batch_rows"]


def test_cancel_during_first_block_generation_stops_before_array_allocation(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "DATA_ROOT", tmp_path / "generated")
    monkeypatch.setattr(datasets, "META_ROOT", tmp_path / "metadata")
    cancelled = {"value": False}
    arrays_called = {"value": False}
    original_make_arrays = datasets._make_arrays

    def progress(update):
        if update.get("phase") == "generating":
            cancelled["value"] = True

    def tracked_make_arrays(*args, **kwargs):
        arrays_called["value"] = True
        return original_make_arrays(*args, **kwargs)

    monkeypatch.setattr(datasets, "_make_arrays", tracked_make_arrays)
    with pytest.raises(InterruptedError, match="cancelled"):
        datasets.generate_dataset(
            "sales", 50_000, seed=5, batch_size=25_000,
            progress=progress, cancel_check=lambda: cancelled["value"],
        )
    assert arrays_called["value"] is False
    assert not list((tmp_path / "generated").glob("*.csv"))
    assert not list((tmp_path / "generated").glob("*.part"))


def test_numpy_csv_writer_reuses_one_open_file_handle(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "pa", None)
    monkeypatch.setattr(datasets, "pacsv", None)
    path = tmp_path / "stream.csv.part"
    state = {"writer": None, "file_handle": None, "header_written": False}
    rng = __import__("numpy").random.default_rng(3)
    first = datasets._make_arrays("sales", 0, 100, rng)
    second = datasets._make_arrays("sales", 100, 100, rng)
    datasets._write_batch_csv(path, first, datasets.TEMPLATES["sales"]["columns"], state)
    handle_id = id(state["file_handle"])
    datasets._write_batch_csv(path, second, datasets.TEMPLATES["sales"]["columns"], state)
    assert id(state["file_handle"]) == handle_id
    state["file_handle"].close()
    state["file_handle"] = None
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert len(rows) == 201  # one header + two 100-row blocks
    assert rows[0] == [column[0] for column in datasets.TEMPLATES["sales"]["columns"]]


def test_large_dataset_disk_preflight_prevents_impossible_job(tmp_path, monkeypatch):
    from types import SimpleNamespace

    data_root = tmp_path / "generated"
    meta_root = tmp_path / "metadata"
    monkeypatch.setattr(datasets, "DATA_ROOT", data_root)
    monkeypatch.setattr(datasets, "META_ROOT", meta_root)
    monkeypatch.setattr(datasets.shutil, "disk_usage", lambda _path: SimpleNamespace(free=100 * 1024**2))
    with pytest.raises(ValueError, match="Insufficient disk space"):
        datasets.generate_dataset("sales", 50_000_000, seed=1, batch_size=100_000)
    assert not list(data_root.glob("*.csv"))
    assert not list(data_root.glob("*.part"))
    assert not list(meta_root.glob("*.json"))


def test_dataset_record_limit_is_enforced_before_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "DATA_ROOT", tmp_path / "generated")
    monkeypatch.setattr(datasets, "META_ROOT", tmp_path / "metadata")
    for rows in (0, 75_000_001, True):
        with pytest.raises(ValueError, match="between 1 and 75,000,000"):
            datasets.generate_dataset("sales", rows, seed=1, batch_size=10_000)


def test_generate_dataset_can_write_to_isolated_temporary_roots(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(datasets, "DATA_ROOT", tmp_path / "user-data")
    monkeypatch.setattr(datasets, "META_ROOT", tmp_path / "user-metadata")
    probe_data = tmp_path / "probe-data"
    probe_meta = tmp_path / "probe-metadata"

    item = datasets.generate_dataset(
        "sales", 120, seed=3, batch_size=10_000,
        data_root=probe_data, meta_root=probe_meta,
    )

    assert Path(item["path"]).parent == probe_data.resolve()
    assert Path(item["metadata_path"]).parent == probe_meta.resolve()
    assert Path(item["path"]).exists()
    assert not (tmp_path / "user-data").exists()
    assert not (tmp_path / "user-metadata").exists()
