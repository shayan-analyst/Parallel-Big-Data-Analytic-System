# Update — Parallel-vs-Sequential reliability fix (18 Nov 2026)

## Why parallel looked slower than sequential
1. **Memory policy regression.** The fixed 96 MiB RAM cap forced every 25M-record run (300 MB of prepared data) onto disk-backed memory-mapped files, and under RAM pressure even 1M rows. Page faults hit the multi-thread run hardest. Now: arrays stay in RAM whenever they fit in `min(512 MB cap, 50% of free RAM)`, and memory-mapped pages are pre-touched before timing.
2. **False sharing in the native kernels.** `frequency` and `group_sum` kept each thread's counters in unpadded adjacent rows, and `comprehensive` depended on allocator luck for 64-byte alignment. Threads fought over cache lines, so results flipped between runs. All per-thread accumulators are now 64-byte aligned and padded (`_aligned_zeros`).
3. **No awareness of machine load.** The verdict is now qualified by measured OS CPU counters: cores the worker threads really received and CPU used by other programs. A verdict taken on a busy computer is marked unreliable instead of blaming the algorithm.

## New behaviour
- `POST /api/benchmarks/preflight` and `create_job` compare required vs available RAM, disk and free CPU cores. Hard shortages stop the run with exact numbers ("needs ~208 MB, only 150 MB free").
- Results include `resource_plan` and `analysis.execution_quality` (`verdict_reliable`, `interference`, `cpu_starved`, cores actually used).
- `scripts/diagnose_parallel.py` measures scaling per kernel and thread count on this machine.
- `verify_native_engine.py` now rejects binaries without the padding fix, so `RUN.bat` rebuilds a stale `.pyd`.

## Validation status (honest)
- Python side (resource planner, CPU meter, analysis logic, memory-map path) was exercised with a numpy stand-in for the engine in a 1-core sandbox.
- The `.pyx` change could **not** be compiled there (no Cython/network). Run `python build_engine.py build_ext --inplace --force`, then `python scripts/verify_native_engine.py`, `python -m pytest -q` and `python scripts/diagnose_parallel.py` on the target laptop.

---

# Memory-Safe Benchmark & Native Engine Update — Build Status

Date: 18 November 2026

## What changed

- Preserved the dataset-generation implementation and generated-data schema. The files `app/services/datasets.py` and `app/services/generation.py` were not modified.
- Replaced full-array preparation with bounded CSV batches, a bounded category-code encoder, and temporary memory-mapped arrays when prepared columns do not fit the RAM threshold.
- Set conservative Windows launcher defaults: 96 MiB prepared-array RAM threshold, 2 MiB CSV parser blocks, 4,194,304-row compute slices, at most four OpenMP threads (also limited by detected physical cores), and no automatic generation of a larger recommendation dataset.
- Changed active-benchmark telemetry to a lighter three-second sample.
- Corrected parallel efficiency to use the median observed OpenMP worker count rather than the requested thread count.
- Made the native build force regeneration from the current `.pyx` source. `RUN.bat` verifies the native extension and automatically invokes setup when the environment/engine is missing or outdated.
- Added the benchmark resource profile to the health endpoint and benchmark reports.

## Automated validation

- **70 tests passed** (`python -m pytest -q`).
- Python syntax compilation passed for the modified backend and build files.
- JavaScript syntax check passed (`node --check frontend/static/app.js`).
- Linux Cython/OpenMP extension compiled successfully in the development environment.
- `scripts/verify_native_engine.py` passed: fused Comprehensive Analytics functions were present and result parity passed; the self-check observed two workers for the two-worker test.
- A real 1M-row Sales dataset was prepared through the memory-mapped path and benchmarked with the fused Comprehensive Analytics kernel. The report completed with `verified=True`, and temporary prepared files were removed after completion.

## Memory-profile interpretation

The profile bounds ordinary-RAM prepared arrays to 96 MiB (subject to the available-RAM safety check), parses CSV in 2 MiB blocks, and limits each compute slice to 4,194,304 rows. For the combined numeric/category example this is about 48 MiB of active input per full slice. Larger prepared arrays are backed by temporary files rather than retained as complete in-memory arrays.

This is a conservative working-set design for a 500 MiB target, **not a hard operating-system process-memory limit**. Python, NumPy, PyArrow, native libraries, the browser, memory-mapped page cache, dataset generation and other applications also consume memory. Actual RSS must be checked on the target Windows laptop before claiming an absolute 500 MiB ceiling. Memory-mapped workloads need sufficient free disk space.

## Windows build limitation

The development environment used Linux/GCC and Python 3.13, so the native Linux extension and common tests were validated here. A Windows CPython 3.12 `.pyd` could not be built in this environment. The supplied Windows `.pyd` was intentionally excluded from the updated source ZIP because it was stale relative to the `.pyx` source. On the target Windows PC, `RUN.bat` will call `setup_windows.bat` to build the matching binary.

The first Windows build requires Python 3.12 x64, internet access for Python packages, and Visual Studio 2022 Build Tools with **Desktop development with C++**. The batch files automate the project setup/build but cannot install Microsoft's compiler toolchain on the user's behalf.

## Important scope note

The data generator was deliberately left unchanged. The memory profile applies to benchmark preparation, computation and active-benchmark monitoring. It does not claim that all dataset-generation workloads or the full Python application process remain below 500 MiB on every machine.
