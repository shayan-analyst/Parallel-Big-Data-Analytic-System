"""Verify that the currently loaded Cython/OpenMP binary exports the latest kernels."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        import numpy as np
        from app.engine import openmp_kernels as kernels
    except Exception as exc:
        print(f"ERROR: Could not import the native extension: {type(exc).__name__}: {exc}")
        return 2

    required = (
        "numeric_stats_seq", "numeric_stats_parallel",
        "threshold_count_seq", "threshold_count_parallel",
        "group_sum_seq", "group_sum_parallel",
        "comprehensive_analytics_seq", "comprehensive_analytics_parallel",
    )
    missing = [name for name in required if not callable(getattr(kernels, name, None))]
    if missing:
        print("LEGACY NATIVE BINARY DETECTED")
        print("Missing exports: " + ", ".join(missing))
        print("The application compatibility path can still benchmark using the existing kernels.")
        print("Rebuild with: .\\.venv\\Scripts\\python.exe build_engine.py build_ext --inplace --force")
        return 1

    if not kernels.engine_info().get("false_sharing_guard"):
        print("OUTDATED NATIVE BINARY DETECTED")
        print("This binary predates the cache-line padding fix (threads can slow each other down through false sharing).")
        print("Rebuild with: .\\.venv\\Scripts\\python.exe build_engine.py build_ext --inplace --force")
        return 1

    # Parity at category counts that do not fill a 64-byte cache line.
    rng = np.random.default_rng(1)
    for categories in (1, 5, 13):
        c = rng.integers(0, categories, 300_000, dtype=np.int32)
        v = rng.uniform(0, 100, 300_000)
        np.testing.assert_array_equal(kernels.frequency_seq(c, categories),
                                      kernels.frequency_parallel(c, categories, 4, "static")[0])
        sc, ss = kernels.group_sum_seq(c, v, categories)
        pc, ps, _ = kernels.group_sum_parallel(c, v, categories, 4, "dynamic")
        np.testing.assert_array_equal(sc, pc)
        np.testing.assert_allclose(ss, ps, rtol=1e-9, atol=1e-6)

    count = 200_000
    codes = (np.arange(count, dtype=np.int32) % 7).copy()
    values = (np.arange(count, dtype=np.float64) * 0.25).copy()
    threshold = 20_000.0
    seq = kernels.comprehensive_analytics_seq(codes, values, 7, threshold)
    par = kernels.comprehensive_analytics_parallel(codes, values, 7, threshold, 4, "static")
    assert int(seq[0]) == int(par[0])
    assert int(seq[5]) == int(par[5])
    assert np.allclose(seq[1:5], par[1:5], rtol=1e-9, atol=1e-7)
    np.testing.assert_array_equal(seq[6], par[6])
    np.testing.assert_allclose(seq[7], par[7], rtol=1e-9, atol=1e-6)
    observed = int(par[8])
    if not 1 <= observed <= 4:
        raise AssertionError(f"Unexpected observed OpenMP worker count: {observed}")

    print("NATIVE ENGINE CHECK: PASS")
    print("Fused Comprehensive Analytics functions: available")
    print("Cache-line padded thread accumulators (no false sharing): available")
    print(f"OpenMP workers observed: {observed} / 4 configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
