@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
if not exist "runtime\app.pid" (
  echo No managed application process was found.
  pause
  exit /b 0
)
set /p APPPID=<runtime\app.pid
echo Requesting graceful shutdown of Parallel Big Data Analytics (PID %APPPID%)...
echo stop>runtime\stop.request
for /l %%I in (1,1,20) do (
  if not exist "runtime\app.pid" goto :stopped
  timeout /t 1 /nobreak >nul
)
if exist "runtime\app.pid" (
  echo Application did not stop within 20 seconds. Sending a final process-tree stop...
  taskkill /PID %APPPID% /T >nul 2>nul
  timeout /t 2 /nobreak >nul
  if exist "runtime\app.pid" taskkill /PID %APPPID% /T /F >nul 2>nul
  del /q "runtime\app.pid" >nul 2>nul
)
:stopped
echo Application stopped.
pause
