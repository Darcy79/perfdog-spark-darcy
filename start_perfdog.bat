@echo off
setlocal
title PerfDog-CN Collector
cd /d "%~dp0collector"

rem --- locate uv (PATH first, fallback to Cherry Studio bundled) ---
set "UV=uv"
where uv >nul 2>nul
if errorlevel 1 (
  if exist "C:\Users\SparkGame\.cherrystudio\bin\uv.exe" (
    set "UV=C:\Users\SparkGame\.cherrystudio\bin\uv.exe"
  ) else (
    echo [ERROR] uv not found. Please install uv or add it to PATH.
    pause
    exit /b 1
  )
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
