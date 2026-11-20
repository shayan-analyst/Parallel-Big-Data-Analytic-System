import pytest
from pydantic import ValidationError

from app.main import GenerateRequest, dataset_generation_options, operations


def test_generator_contract_supports_requested_100k_to_75m_sizes():
    options = dataset_generation_options()
    expected = [100_000, 1_000_000, 5_000_000, 10_000_000, 25_000_000, 50_000_000, 75_000_000]
    assert options["maximum_rows"] == 75_000_000
    assert options["minimum_rows"] == 100_000
    assert options["minimum_benchmark_rows"] == 100_000
    assert options["generation_row_presets"] == expected
    assert options["recommended_demo_row_presets"] == expected
    assert options["generation_chunk_sizes"] == [10_000, 25_000, 50_000, 100_000]

    for rows in expected:
        assert GenerateRequest(kind="sales", rows=rows).rows == rows
    for rows in (50_000, 500_000, 20_000_000, 40_000_000, 60_000_000, 75_000_001):
        with pytest.raises(ValidationError):
            GenerateRequest(kind="sales", rows=rows)


def test_comprehensive_analytics_is_a_primary_benchmark_operation():
    payload = operations()
    option = next(item for item in payload["operations"]
                  if item["id"] == "comprehensive_analytics")
    assert option["requires_numeric"] is True
    assert option["requires_category"] is True
    assert option["requires_threshold"] is True
    assert payload["experimental_operations"] == []


def test_benchmark_api_accepts_1m_dataset_without_10m_gate(monkeypatch):
    from fastapi import HTTPException
    from app import main

    monkeypatch.setattr(main.benchmark, "has_active_jobs", lambda: False)
    monkeypatch.setattr(main.generation, "has_active_jobs", lambda: False)
    monkeypatch.setattr(main.datasets, "get_dataset", lambda _dataset_id: {"rows": 1_000_000})
    monkeypatch.setattr(main.benchmark, "create_job", lambda _config: "job-1000k")
    monkeypatch.setattr(main.benchmark, "start_worker", lambda _job_id, _executor: None)
    request = main.BenchmarkRequest(
        dataset_id="starter-1m", operation="numeric_stats", column="Revenue"
    )
    response = main.create_benchmark(request)
    assert response == {"job_id": "job-1000k", "status": "queued"}


def test_generation_api_accepts_100k_and_queues_job_without_validation_mismatch(monkeypatch):
    from app import main

    monkeypatch.setattr(main.generation, "has_active_jobs", lambda: False)
    monkeypatch.setattr(main.benchmark, "has_active_jobs", lambda: False)
    monkeypatch.setattr(main.generation, "create_job", lambda config: "generation-100k")
    monkeypatch.setattr(main.generation, "start_worker", lambda _job_id, _executor: None)
    request = GenerateRequest(kind="sales", rows=100_000, seed=42, batch_size=25_000)
    response = main.generate_dataset(request)
    assert response == {"generation_id": "generation-100k", "status": "queued"}
