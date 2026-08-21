@echo off
setlocal
title PerfDog-CN Collector
cd /d "%~dp0collector"

rem --- locate uv (PATH only; no private-path fallback, portable on any machine) ---
set "UV=uv"
where uv >nul 2>nul
if errorlevel 1 (
  echo [ERROR] uv not found in PATH.
  echo         Install uv:  pip install uv   or   https://docs.astral.sh/uv/
  echo         Or use system Python directly:  python main.py --web
  pause
  exit /b 1
)

rem --- browser auto-open now handled by main.py itself (waits for server,
rem     avoids opening a tab before the service is up / duplicate tabs) ---

echo.
echo ==================================================
echo  PerfDog-CN  : collect + web dashboard
echo  Live view   : http://localhost:8080
echo  History     : http://localhost:8080/report.html
echo  Data saved  : output\<timestamp>\  (jsonl + html)
echo  Stop        : Ctrl+C twice
echo ==================================================
echo.
"%UV%" run --no-project python main.py --web

pause
endlocal
