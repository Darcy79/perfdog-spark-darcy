@echo off
setlocal
title PerfDog-CN Dashboard
cd /d "%~dp0collector"

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
