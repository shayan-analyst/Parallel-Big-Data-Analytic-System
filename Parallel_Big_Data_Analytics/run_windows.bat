@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

rem Conservative benchmark profile for an 8 GB office laptop.
rem Prepared columns use <=96 MiB of ordinary RAM; larger columns are memory-mapped.
set "PBDA_PREPARED_RAM_LIMIT_MB=96"
set "PBDA_ARROW_BLOCK_SIZE_BYTES=2097152"
set "PBDA_COMPUTE_CHUNK_ROWS=4194304"
set "PBDA_MAX_BENCHMARK_THREADS=4"
set "PBDA_ENABLE_NEXT_SIZE_PROBE=0"

if not exist ".venv\Scripts\python.exe" goto :setup
set "PY=%CD%\.venv\Scripts\python.exe"

rem Do not launch an old .pyd that lacks the fused native analytics kernels.
"%PY%" scripts\verify_native_engine.py >nul 2>nul
if errorlevel 1 goto :setup

goto :launch

:setup
echo First launch or outdated native engine detected. Running one-time setup/build...
call setup_windows.bat /auto
if errorlevel 1 (
  echo.
  echo Setup could not finish. Check Python 3.12 x64 and Visual Studio C++ Build Tools.
  pause
  exit /b 1
)
set "PY=%CD%\.venv\Scripts\python.exe"
"%PY%" scripts\verify_native_engine.py
if errorlevel 1 (
  echo Native engine verification failed. The app will not launch with a stale binary.
  pause
  exit /b 2
)

:launch
if not exist "%PY%" (
  echo Python environment is missing. Run setup_windows.bat.
  pause
  exit /b 1
)
echo Starting Parallel Big Data Analytics in memory-safe mode...
echo Prepared-array RAM threshold: 96 MB ^| compute chunks: 4,194,304 rows ^| OpenMP cap: 4 threads
echo Browser opens after the local server is ready. Keep this window open.
"%PY%" scripts\launcher.py
pause
