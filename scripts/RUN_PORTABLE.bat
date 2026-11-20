@echo off
setlocal
cd /d "%~dp0"
if not exist "ParallelBigDataAnalytics.exe" (
  echo Portable application executable not found.
  pause
  exit /b 1
)
echo Starting portable Parallel Big Data Analytics in memory-safe mode...
set "PBDA_PREPARED_RAM_LIMIT_MB=96"
set "PBDA_ARROW_BLOCK_SIZE_BYTES=2097152"
set "PBDA_COMPUTE_CHUNK_ROWS=4194304"
set "PBDA_MAX_BENCHMARK_THREADS=4"
set "PBDA_ENABLE_NEXT_SIZE_PROBE=0"
start "Parallel Big Data Analytics" /min "ParallelBigDataAnalytics.exe"
echo The application will open your browser when the local server is ready.
echo Use STOP.bat to stop the application.
