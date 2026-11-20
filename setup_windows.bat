@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "PBDA_NO_PAUSE=0"
if /I "%~1"=="/auto" set "PBDA_NO_PAUSE=1"
echo.
echo ============================================================
echo  PARALLEL BIG DATA ANALYTICS - WINDOWS SETUP
echo ============================================================
echo.
py -3.12 -c "import sys,struct; print('Python',sys.version); assert struct.calcsize('P') == 8, '64-bit Python is required'" >nul 2>nul
if errorlevel 1 (
  echo ERROR: A 64-bit Python 3.12 installation was not found through the Windows Python Launcher.
  echo Install Python 3.12 x64 from https://www.python.org/downloads/release/python-312/
  echo Enable the Python Launcher and add Python to PATH.
  if "%PBDA_NO_PAUSE%"=="0" pause
  exit /b 1
)
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import struct,sys; assert sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8" >nul 2>nul
  if errorlevel 1 (
    echo Existing virtual environment is not Python 3.12 x64. Recreating it...
    rmdir /s /q ".venv"
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating Python 3.12 x64 project virtual environment...
  py -3.12 -m venv .venv || goto :fail
)
set PY=%CD%\.venv\Scripts\python.exe

echo Updating packaging tools...
"%PY%" -m pip install --upgrade pip setuptools wheel || goto :fail

echo Installing Python dependencies. Internet access is required for first setup...
"%PY%" -m pip install -r requirements.txt || goto :fail

rem Ensure the MSVC environment targets x64, matching the required 64-bit Python.
if /I not "%VSCMD_ARG_TGT_ARCH%"=="x64" (
  set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
  if exist "!VSWHERE!" (
    for /f "usebackq tokens=*" %%I in (`"!VSWHERE!" -latest -products * -property installationPath`) do set "VSROOT=%%I"
    if defined VSROOT if exist "!VSROOT!\Common7\Tools\VsDevCmd.bat" call "!VSROOT!\Common7\Tools\VsDevCmd.bat" -arch=x64 >nul
  )
)
where cl >nul 2>nul
if errorlevel 1 (
  echo.
  echo ERROR: Microsoft C++ compiler cl.exe was not found.
  echo Install Visual Studio Build Tools with the "Desktop development with C++" workload:
  echo https://visualstudio.microsoft.com/visual-cpp-build-tools/
  echo Then run this setup file again. The OpenMP engine cannot be faked or replaced by Python threads.
  if "%PBDA_NO_PAUSE%"=="0" pause
  exit /b 2
)

echo Removing any older native binary and generated build objects...
if exist "app\engine\openmp_kernels*.pyd" del /q "app\engine\openmp_kernels*.pyd"
if exist "build" rmdir /s /q "build"

echo Building native Cython + OpenMP extension for this Windows PC...
"%PY%" build_engine.py build_ext --inplace --force || goto :fail

echo Running native engine and result-parity self-check...
"%PY%" scripts\verify_native_engine.py || goto :fail

echo.
echo ============================================================
echo SETUP COMPLETE
echo Next: RUN.bat will launch the local dashboard.
echo ============================================================
if "%PBDA_NO_PAUSE%"=="0" pause
exit /b 0
:fail
echo.
echo SETUP FAILED. Review the compiler or dependency error above.
if "%PBDA_NO_PAUSE%"=="0" pause
exit /b 1
