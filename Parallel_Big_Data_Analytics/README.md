# Parallel Big Data Analytics & System Performance Benchmarking Platform

A local-first analytics and benchmarking application that compares compiled sequential kernels with native Cython/OpenMP parallel kernels. The dashboard is built with HTML, CSS and vanilla JavaScript; the backend and data pipeline are Python-based.

> **Authenticity rule:** The benchmark endpoint refuses to run unless the compiled Cython/OpenMP extension is present. It does not replace a missing OpenMP engine with Python threads or simulated progress. CPU/RAM/disk/network metrics come from the operating system through `psutil`; NVIDIA GPU telemetry is optional and is displayed only when supported.

## What is included

- Six generated dataset templates: Sales, Finance, Product Inventory, Student Performance, Employee and Attendance.
- Chunk-based, seeded NumPy generation with local CSV output. PyArrow CSV writing is used when installed; a slower NumPy batch writer is available as a fallback.
- Schema preview, dataset metadata, paginated local CSV preview and download.
- Six analytical workloads built on compiled kernels: numeric summary, threshold count, category frequency, date-period frequency, group-by sum and top-K groups.
- Sequential and OpenMP implementations compiled from the same Cython source.
- Static, dynamic and guided OpenMP scheduling options.
- Repeated benchmarks with alternating execution order and median reporting.
- Separate wall-clock, CSV read, conversion and native kernel timings.
- Result parity verification before an experiment is marked verified.
- Live hardware telemetry: system/per-core CPU, application CPU, system RAM, process RSS, threads, mounted storage, system disk I/O, network traffic and top process list.
- Optional NVIDIA GPU telemetry through NVML when supported.
- WebSocket progress updates and local SQLite experiment history.
- JSON and CSV benchmark report exports, plus print-to-PDF from the Results page.
- Windows setup/run scripts and a separate Windows portable-build script.

## Important delivery distinction

This repository ZIP is the **source/setup package**. It is not a precompiled Windows portable release because native Windows `.pyd` extensions must be built with a compatible Windows compiler. The setup script compiles the OpenMP engine on the Windows machine. After setup, `RUN.bat` launches the local app.

To create a no-Python-install portable Windows ZIP, run `build_portable_windows.bat` on a Windows build machine after the source setup succeeds. That produces `dist/Parallel_Big_Data_Analytics_Windows_x64.zip`. The packaged release must be tested on a clean Windows PC before redistribution. The included GitHub Actions workflow can also build a Windows portable artifact on a Windows runner.

## Windows setup (source package)

### Prerequisites

1. Windows 10/11 x64.
2. Python 3.12 x64 and the Python Launcher (`py`).
3. Visual Studio 2022 Build Tools with the **Desktop development with C++** workload and x64 compiler tools.
4. Internet access for first-time Python dependency installation.

The OpenMP native extension is compiled with MSVC `/openmp`. The build cannot be performed by a pure Python installation alone.

### Install and run

1. Extract the ZIP into a writable folder (avoid `Program Files` for the first run).
2. Double-click `RUN.bat`. On first launch, or when the native extension is missing/outdated, it automatically calls `setup_windows.bat`, creates `.venv`, installs dependencies and rebuilds the Cython/OpenMP engine.
3. Your default browser opens `http://127.0.0.1:8000` after the local server is ready.
4. Keep the launcher window open while using the app. `STOP.bat` requests cooperative shutdown and asks active jobs to stop at a batch boundary; Ctrl+C in the server window is also supported.

The first run needs internet access, Python 3.12 x64 and Visual Studio 2022 Build Tools with the **Desktop development with C++** workload. These compiler prerequisites cannot be silently replaced by Python; `RUN.bat` reports the missing prerequisite if it is not installed. Subsequent launches verify the native engine and start without rebuilding it.

### Build a portable Windows package

After `setup_windows.bat` succeeds, run `build_portable_windows.bat`. It installs the build packager, freezes the Python app into a one-folder distribution and creates a ZIP in `dist/`.

The portable release is intended to run without a separately installed Python environment. A compatible Microsoft Visual C++ runtime may still be needed depending on the target Windows image and compiler dependencies. Test on a clean machine before sharing.

## Linux development setup

On a Linux machine with GCC and OpenMP support:

```bash
chmod +x setup_linux.sh run_linux.sh
./setup_linux.sh
./run_linux.sh
```

The current build script supports Windows/MSVC and Linux/GCC. macOS requires a separately configured `libomp` toolchain and is not included in the initial release target.

## Benchmark workflow

1. Generate a dataset in **Dataset Generator**.
2. Inspect it in **Data Preview**.
3. Select an operation in **Analytics Workspace**.
4. Configure OpenMP threads (the memory-safe profile offers up to four, limited by physical cores), scheduling and repetitions.
5. Start the benchmark.
6. The system alternates which engine runs first between measured repetitions to reduce systematic run-order/cache bias.
7. The dashboard reports the median wall time and median kernel time for each engine. The Results page supports JSON/CSV downloads and browser Print / Save as PDF.
8. Sequential and parallel outputs are compared using exact matching for categorical/count results and a numerical tolerance for floating-point outputs.

### Available operations

- **Numeric Summary:** count, sum, mean, standard deviation, minimum and maximum.
- **Threshold Filter Count:** count numeric values greater than a supplied threshold.
- **Category Frequency:** count each unique category.
- **Group By + Sum:** per-category record count, sum and average.
- **Top-K Groups by Measure:** group a numeric measure by category and rank the highest groups (for example, highest-revenue products).
- **Date Period Frequency:** count transactions/records by year, month or day from a template date column.

These operations work with the typed numeric, categorical and date columns in the built-in dataset templates. Top-K uses the compiled group-by kernel and ranks the merged group results; date frequency encodes year/month/day periods and counts them using the compiled frequency kernel.

### Measurement definitions

- **Wall time:** elapsed time for streaming CSV read, column conversion and native computation together.
- **Read time:** time spent obtaining CSV batches from the streaming reader.
- **Conversion time:** time spent converting selected columns to contiguous typed arrays / category codes.
- **Kernel time:** time spent inside the compiled sequential or OpenMP kernel calls.
- **Kernel speedup:** median sequential kernel time divided by median parallel kernel time.
- **Wall speedup:** median sequential wall time divided by median parallel wall time.
- **Parallel efficiency:** kernel speedup divided by the median number of OpenMP workers actually observed, multiplied by 100. This avoids treating configured-but-unused workers as active workers.
- **Throughput:** processed records divided by the stated wall or kernel time.

Kernel speedup is the primary compute comparison. Wall speedup is also shown because it includes file reading and conversion. These are different measurements and should not be mixed.


### Memory-safe benchmark profile (8 GB office laptops)

The Windows launcher starts the benchmark with a conservative profile:

- Prepared numeric/category arrays use ordinary RAM only when their combined size is at most 96 MiB (and the available-RAM safety check permits it). Larger arrays are written to temporary memory-mapped files and deleted after the experiment.
- CSV preparation uses 2 MiB parser blocks and single-threaded parsing. Category encoding uses a bounded dictionary encoder rather than large temporary Unicode arrays.
- Compute runs in 4,194,304-row slices. For the combined numeric + category workload this represents up to approximately 48 MiB of active input data per slice; both engines reuse the same prepared values.
- OpenMP worker choices are capped at the detected physical-core count, up to four workers by default. Actual workers observed by the native kernel are reported separately.
- The optional automatic generation of a larger recommendation dataset is disabled. Alternative worker counts, when tested, use the already-prepared dataset only.
- During active benchmarks, hardware telemetry switches to a lighter 3-second sample and skips process-list, disk-volume and GPU enumeration.

This profile is designed to keep the benchmark's working set comfortably below a 500 MiB target on the intended class of laptop. It is **not an operating-system-enforced 500 MiB process limit**: Python, native libraries, the browser, file cache and other programs also consume memory. Memory-mapped arrays still require temporary disk space; those temporary files are cleaned up when the experiment finishes or is cancelled. The dataset generator, its seeded values, CSV schema and generated output are unchanged.

The settings can be adjusted with `PBDA_PREPARED_RAM_LIMIT_MB`, `PBDA_ARROW_BLOCK_SIZE_BYTES`, `PBDA_COMPUTE_CHUNK_ROWS`, `PBDA_MAX_BENCHMARK_THREADS` and `PBDA_ENABLE_NEXT_SIZE_PROBE` before launching the application.

## Telemetry definitions and limitations

- CPU percentage is measured from OS counters. A multithreaded process can exceed 100% CPU because process CPU is expressed relative to one logical CPU; system CPU is averaged across logical processors.
- RAM includes system physical memory and this application's resident-set size (RSS), shown separately.
- System disk read/write throughput is system-wide OS disk activity and can include other programs. Application process read/write counters are shown separately when the OS supports them; these may include backend/runtime/database files, not only dataset files.
- Network values are OS network-interface traffic rates, not an ISP maximum-speed test.
- GPU monitoring is optional and currently uses NVIDIA NVML. Missing drivers, unsupported hardware or permissions result in an unavailable state, not invented values.
- The OpenMP analytics engine is CPU-based. GPU telemetry does not mean the analytics kernels run on the GPU.
- Other process details may be restricted by operating-system permissions.

## Data locations

The application stores data relative to the folder from which it is launched:

- `workspace/data/generated/` — generated CSV datasets.
- `workspace/data/metadata/` — dataset schema and generation metadata.
- `workspace/results/` — experiment JSON reports.
- `workspace/history/experiments.db` — local SQLite history.

Generated datasets are not uploaded to GitHub or a cloud service. Do not commit `workspace/` to the repository.

## API and local security

- Dashboard: `http://127.0.0.1:8000`
- API health: `http://127.0.0.1:8000/api/health`
- Interactive API docs: `http://127.0.0.1:8000/api/docs`
- The server binds to loopback (`127.0.0.1`) by default. It is not exposed to the internet.

## Tests

After setup:

```bash
.venv\Scripts\python.exe -m pytest -q
```

## Troubleshooting

**OpenMP engine unavailable** — Run `setup_windows.bat` and confirm the native engine self-check passes. Do not use the app's benchmarking screen until the Cython extension is built.

**`cl.exe` not found** — Install Visual Studio 2022 Build Tools with Desktop development with C++, or run setup from an x64 Native Tools Developer Command Prompt.

**Port 8000 already in use** — Stop the other local service or set `PBDA_PORT` to another local port before running the launcher.

**GPU data unavailable** — Install a supported NVIDIA driver and NVML runtime; otherwise the GPU panel remains unavailable. CPU benchmarking still works.

**10M dataset too large** — Reduce the record count or batch size, check free disk capacity, and close other memory-intensive applications. Dataset generation checks the output size only as the file grows; it cannot guarantee that every workload fits every computer.

## Repository layout

```text
app/                 FastAPI application, generator, benchmark and monitoring services
app/engine/           Cython/OpenMP native kernel source
frontend/             HTML, CSS and vanilla JavaScript dashboard
scripts/              Local and portable launchers
workspace/            Created at runtime; local data and results
build_engine.py       Native extension build configuration
setup_windows.bat     Windows source setup and OpenMP compilation
RUN.bat               Local development launch
STOP.bat              Stops the managed local app process
build_portable_windows.bat  Creates a PyInstaller Windows distribution
```

## License

MIT. See `LICENSE`.

## UI refresh (modern theme)

The frontend (`frontend/index.html`, `static/styles.css`, `static/fx.js`) was redesigned to match the AlgoVision project:
dark "chalkboard" / light "graph paper" themes (toggle in the top bar), glass panels, a guided 1→4 journey
(Data Lab → Benchmark Setup → Live Monitor → Results) and a live chip-network background — microchips drift, join each
other with chain links, break apart and re-link to their nearest neighbours; move the mouse to pull nearby chips into a hub.
Backend, API and benchmark logic are unchanged.

**Performance note:** the UI switches to an idle *busy mode* (background animation stopped, effects off) while a benchmark or dataset generation is running, so the browser never competes with the OpenMP threads and timings stay unaffected.
