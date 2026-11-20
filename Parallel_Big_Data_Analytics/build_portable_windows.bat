@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
echo Building a portable Windows folder package...
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_windows.bat first.
  pause
  exit /b 1
)

rem Load the x64 MSVC environment; a separate setup script does not preserve compiler variables.
if /I not "%VSCMD_ARG_TGT_ARCH%"=="x64" (
  set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
  if exist "!VSWHERE!" (
    for /f "usebackq tokens=*" %%I in (`"!VSWHERE!" -latest -products * -property installationPath`) do set "VSROOT=%%I"
    if defined VSROOT if exist "!VSROOT!\Common7\Tools\VsDevCmd.bat" call "!VSROOT!\Common7\Tools\VsDevCmd.bat" -arch=x64 >nul
  )
)
where cl >nul 2>nul
if errorlevel 1 (
  echo ERROR: MSVC cl.exe not found. Install Visual Studio Build Tools with Desktop development with C++.
  pause
  exit /b 2
)

set "PY=%CD%\.venv\Scripts\python.exe"
"%PY%" -m pip install -r requirements-build.txt || goto :fail
"%PY%" build_engine.py build_ext --inplace || goto :fail
"%PY%" -c "import numpy as np; from app.engine import openmp_kernels as k; x=np.arange(1,100001,dtype=np.float64); assert k.numeric_stats_seq(x)[:5] == k.numeric_stats_parallel(x,4,'static')[:5]; print(k.engine_info())" || goto :fail
"%PY%" -m PyInstaller --noconfirm --clean --onedir --name ParallelBigDataAnalytics --add-data "frontend;frontend" --collect-all fastapi --collect-all uvicorn --collect-all numpy --collect-all psutil --collect-all pyarrow --hidden-import app.main --hidden-import app.engine.openmp_kernels --hidden-import pynvml --collect-submodules app.services scripts\portable_entry.py || goto :fail
if not exist "dist\ParallelBigDataAnalytics\workspace" mkdir "dist\ParallelBigDataAnalytics\workspace"
copy /y "scripts\RUN_PORTABLE.bat" "dist\ParallelBigDataAnalytics\RUN.bat" >nul
copy /y "scripts\STOP_PORTABLE.bat" "dist\ParallelBigDataAnalytics\STOP.bat" >nul
copy /y "README.md" "dist\ParallelBigDataAnalytics\README.txt" >nul
powershell -NoProfile -Command "Compress-Archive -Path 'dist\ParallelBigDataAnalytics\*' -DestinationPath 'dist\Parallel_Big_Data_Analytics_Windows_x64.zip' -Force"
if errorlevel 1 goto :fail
echo.
echo Portable build created:
echo dist\Parallel_Big_Data_Analytics_Windows_x64.zip
echo Test this ZIP on a clean Windows PC before publishing it.
pause
exit /b 0
:fail
echo Portable build failed. Review the compiler or PyInstaller error.
pause
exit /b 1
