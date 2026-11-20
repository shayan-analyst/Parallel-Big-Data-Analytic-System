import numpy as np
import pytest

from app.engine import openmp_kernels as kernels


@pytest.mark.parametrize("schedule", ["static", "dynamic", "guided"])
def test_numeric_summary_sequential_parallel_match(schedule):
    values = np.random.default_rng(12).normal(100, 25, 150_000).astype(np.float64)
    seq = kernels.numeric_stats_seq(values)
    par = kernels.numeric_stats_parallel(values, 4, schedule)
    assert seq[:1] == par[:1]
    assert np.allclose(seq[1:5], par[1:5], rtol=1e-10, atol=1e-8)
    assert 1 <= par[5] <= 4


@pytest.mark.parametrize("schedule", ["static", "dynamic", "guided"])
def test_threshold_count_match(schedule):
    values = np.random.default_rng(3).uniform(0, 100, 80_000).astype(np.float64)
    assert kernels.threshold_count_seq(values, 55.5) == kernels.threshold_count_parallel(values, 55.5, 4, schedule)[0]


@pytest.mark.parametrize("schedule", ["static", "dynamic", "guided"])
def test_frequency_match(schedule):
    codes = np.random.default_rng(4).integers(0, 13, 100_000, dtype=np.int32)
    seq = kernels.frequency_seq(codes, 13)
    par, observed = kernels.frequency_parallel(codes, 13, 4, schedule)
    np.testing.assert_array_equal(seq, par)
    assert 1 <= observed <= 4


@pytest.mark.parametrize("schedule", ["static", "dynamic", "guided"])
def test_group_sum_match(schedule):
    rng = np.random.default_rng(5)
    codes = rng.integers(0, 8, 70_000, dtype=np.int32)
    values = rng.uniform(1, 1000, 70_000).astype(np.float64)
    seq_count, seq_sum = kernels.group_sum_seq(codes, values, 8)
    par_count, par_sum, observed = kernels.group_sum_parallel(codes, values, 8, 4, schedule)
    np.testing.assert_array_equal(seq_count, par_count)
    np.testing.assert_allclose(seq_sum, par_sum, rtol=1e-10, atol=1e-7)
    assert 1 <= observed <= 4


@pytest.mark.parametrize("schedule", ["static", "dynamic", "guided"])
def test_comprehensive_analytics_kernel_matches(schedule):
    rng = np.random.default_rng(31)
    codes = rng.integers(0, 7, 250_000, dtype=np.int32)
    values = rng.uniform(0, 250_000, 250_000).astype(np.float64)
    threshold = 125_000.0
    seq = kernels.comprehensive_analytics_seq(codes, values, 7, threshold)
    par = kernels.comprehensive_analytics_parallel(codes, values, 7, threshold, 4, schedule)

    assert seq[0] == par[0]
    assert seq[5] == par[5]
    np.testing.assert_allclose(seq[1:5], par[1:5], rtol=1e-9, atol=1e-7)
    np.testing.assert_array_equal(seq[6], par[6])
    np.testing.assert_allclose(seq[7], par[7], rtol=1e-9, atol=1e-6)
    assert 1 <= par[8] <= 4


@pytest.mark.parametrize("categories", [1, 2, 5, 8, 9, 13, 64, 100])
@pytest.mark.parametrize("schedule", ["static", "dynamic", "guided"])
def test_padded_thread_accumulators_match_for_any_category_count(categories, schedule):
    """Cache-line padded per-thread rows must not change any result (frequency/group/comprehensive)."""
    rng = np.random.default_rng(categories)
    n = 300_000
    codes = rng.integers(0, categories, n, dtype=np.int32)
    values = rng.uniform(0, 1000, n).astype(np.float64)
    np.testing.assert_array_equal(
        kernels.frequency_seq(codes, categories), kernels.frequency_parallel(codes, categories, 4, schedule)[0])
    seq_c, seq_s = kernels.group_sum_seq(codes, values, categories)
    par_c, par_s, _ = kernels.group_sum_parallel(codes, values, categories, 4, schedule)
    np.testing.assert_array_equal(seq_c, par_c)
    np.testing.assert_allclose(seq_s, par_s, rtol=1e-9, atol=1e-6)
    seq = kernels.comprehensive_analytics_seq(codes, values, categories, 500.0)
    par = kernels.comprehensive_analytics_parallel(codes, values, categories, 500.0, 4, schedule)
    np.testing.assert_array_equal(seq[6], par[6])
    np.testing.assert_allclose(seq[7], par[7], rtol=1e-9, atol=1e-6)


def test_engine_reports_false_sharing_guard():
    assert kernels.engine_info().get("false_sharing_guard") is True
