"""Measure real multi-thread scaling of the native engine on THIS computer.

Run:   python scripts/diagnose_parallel.py            (25M records, threads 2..4)
       python scripts/diagnose_parallel.py --rows 10000000 --threads 2 4 --reps 7

It uses in-memory synthetic data (no CSV, no disk), runs each kernel
sequentially and with N OpenMP threads, verifies the results match, and
measures how many CPU cores the worker threads *actually* got from the OS.
Nothing is assumed or hard-coded: if the machine is busy or short of RAM the
script says so instead of printing a misleading speedup.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services import resources  # noqa: E402

MB = 1024 * 1024


def timed(fn, reps):
    """Run fn reps times; return (median seconds, last result, cpu summary over all reps)."""
    times, out, windows = [], None, []
    for _ in range(reps):
        window = resources.CpuWindow().start()
        t0 = time.perf_counter()
        out = fn()
        times.append(time.perf_counter() - t0)
        windows.append(window.stop())
    return statistics.median(times), out, resources.combine_windows(windows)


def same(a, b):
    """Recursive numeric comparison of two kernel payloads."""
    if isinstance(a, (tuple, list)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, np.ndarray):
        return np.allclose(a, b, rtol=1e-9, atol=1e-6)
    return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=25_000_000)
    ap.add_argument("--threads", type=int, nargs="+", default=None)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--schedule", default="static", choices=["static", "dynamic", "guided"])
    ap.add_argument("--categories", type=int, default=8)
    args = ap.parse_args()

    try:
        from app.engine import openmp_kernels as k
    except Exception as exc:
        print(f"Native engine is not built: {exc}\nRun setup_windows.bat / setup_linux.sh first.")
        return 2
    info = k.engine_info()
    print(f"Engine: {info.get('engine')} | false-sharing guard: {info.get('false_sharing_guard', False)}")
    if not info.get("false_sharing_guard"):
        print("  !! This binary is OLDER than the source (no cache-line padding). Rebuild: python build_engine.py build_ext --inplace")

    logical = psutil.cpu_count(logical=True) or 1
    physical = psutil.cpu_count(logical=False) or logical
    threads_list = args.threads or [n for n in (2, 4, 6, 8) if n <= physical] or [2]
    idle = resources.sample_cpu(0.5)
    mem = resources.memory_snapshot()
    print(f"CPU: {physical} physical / {logical} logical | other programs use ~{idle['background_cores']:.1f} cores "
          f"| free RAM {mem['available'] // MB} MB")

    rows = args.rows
    need = rows * 12 + resources.FIXED_OVERHEAD_BYTES
    if need > mem["available"] * resources.RAM_SAFETY_FRACTION * 2:
        fit = int(max(1_000_000, (mem["available"] * resources.RAM_SAFETY_FRACTION * 2 - resources.FIXED_OVERHEAD_BYTES) // 12))
        print(f"Not enough free RAM for {rows:,} rows (needs ~{need // MB} MB, {mem['available'] // MB} MB free). "
              f"Using {fit:,} rows instead - free more RAM to test the full size.")
        rows = fit
    if idle["background_cores"] >= 0.5:
        print(f"WARNING: other programs use ~{idle['background_cores']:.1f} cores right now; speedups below will be understated. "
              "Close browsers/IDEs/antivirus scans and run again.")

    rng = np.random.default_rng(7)
    values = rng.uniform(0, 100_000, rows)
    codes = rng.integers(0, args.categories, rows, dtype=np.int32)
    cats, thr, s = args.categories, 50_000.0, args.schedule
    kernels = {
        # Each entry: (sequential call, parallel call). Payload only: the trailing
        # "observed threads" value of the parallel kernels is dropped before comparing.
        "numeric_stats": (lambda: k.numeric_stats_seq(values),
                          lambda t: k.numeric_stats_parallel(values, t, s)[:5]),
        "frequency": (lambda: k.frequency_seq(codes, cats),
                      lambda t: k.frequency_parallel(codes, cats, t, s)[0]),
        "comprehensive": (lambda: k.comprehensive_analytics_seq(codes, values, cats, thr)[:8],
                          lambda t: k.comprehensive_analytics_parallel(codes, values, cats, thr, t, s)[:8]),
    }
    print(f"\n{rows:,} records | schedule={s} | median of {args.reps} runs\n")
    print(f"{'kernel':<14}{'threads':>8}{'time (s)':>11}{'speedup':>9}{'cores used':>12}{'correct':>9}")
    worst = None
    unmeasured = False
    for name, (seq_fn, par_fn) in kernels.items():
        seq_fn()  # warm-up
        base, ref, _ = timed(seq_fn, args.reps)
        print(f"{name:<14}{1:>8}{base:>11.4f}{1.0:>9.2f}{'-':>12}{'-':>9}")
        for t in threads_list:
            par_fn(t)  # warm-up
            sec, out, cpu = timed(lambda: par_fn(t), args.reps)
            ok = same(ref, out)
            used = cpu["process_cores_used"] if cpu and cpu["measurable"] else float("nan")
            print(f"{'':<14}{t:>8}{sec:>11.4f}{base / sec:>9.2f}{used:>12.2f}{'yes' if ok else 'NO':>9}")
            if not ok:
                print("  !! RESULT MISMATCH - do not trust this build.")
            if not (cpu and cpu["measurable"]):
                unmeasured = True
            elif used < 0.6 * min(t, logical):
                worst = (name, t, used, cpu["background_cores"])
    print()
    if worst:
        name, t, used, bg = worst
        print(f"DIAGNOSIS: {t} threads only got ~{used:.1f} cores' CPU time in '{name}' "
              f"(other programs used ~{bg:.1f} cores). The OS did not run the threads in parallel, "
              "so a low speedup here reflects the machine's availability, not the algorithm. "
              "Close other programs, plug in the charger / set 'Best performance' power mode, and re-run.")
    elif unmeasured:
        print("DIAGNOSIS: the runs were too short for the OS CPU counters (shown as nan), so core usage could not "
              "be verified. Use more records (--rows) or more repetitions (--reps) before drawing conclusions.")
    else:
        print("DIAGNOSIS: worker threads received the cores they asked for; speedups above are real. "
              "If a kernel is still < ~1.5x at 4 threads it is limited by memory bandwidth (it only reads each value once).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
