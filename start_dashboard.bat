@echo off
setlocal
title PerfDog-CN Dashboard
cd /d "%~dp0collector"

set "UV=uv"
where uv >nul 2>nul
if errorlevel 1 (
  echo [ERROR] uv not found in PATH.
  echo         Install uv:  pip install uv   or   https://docs.astral.sh/uv/
  echo         Or use system Python directly:  python dashboard.py
  pause
  exit /b 1
)

start "" http://localhost:8080/report.html

echo.
echo ==================================================
echo  PerfDog-CN  : dashboard only (no device needed)
echo  History     : http://localhost:8080/report.html
echo  Stop        : close window or Ctrl+C
echo ==================================================
echo.
"%UV%" run --no-project python dashboard.py

pause
endlocal
