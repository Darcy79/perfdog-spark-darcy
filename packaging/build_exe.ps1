# Build script for perfdog.exe
# Modes:
#   onefile (default): single exe, everything bundled (best for sharing, download & run)
#   onedir: thin exe + external collector source (dev mode, hot-update source without rebuild)
# Usage: pwsh -ExecutionPolicy Bypass -File build_exe.ps1 [-Mode onefile|onedir]
param([ValidateSet("onefile", "onedir")][string]$Mode = "onefile")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = "C:\Users\SparkGame\AppData\Local\Programs\Python\Python314\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

# Ensure PyInstaller
& $py -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyInstaller..."
    & $py -m pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
}

if ($Mode -eq "onefile") {
    Write-Host "== Build onefile (single exe, share-ready) =="
    # clean staging of collector (exclude runtime artifacts)
    $staging = Join-Path $root "packaging\staging\collector"
    if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    Get-ChildItem "$root\collector" -Force | Where-Object {
        $_.Name -notmatch '^(output|__pycache__|\.app_labels\.json)$'
    } | Copy-Item -Destination $staging -Recurse -Force

    & $py -m PyInstaller --noconfirm --clean --onefile --name perfdog `
        --paths collector `
        --hidden-import main --hidden-import web `
        --add-data "packaging\staging\collector;collector" `
        --add-data "web;web" `
        packaging\launcher.py
    if ($LASTEXITCODE -ne 0) { Write-Host "[ERROR] PyInstaller onefile failed"; exit 1 }
    $exe = Join-Path $root "dist\perfdog.exe"
    Write-Host "[OK] single exe: $exe ($((Get-Item $exe).Length / 1MB) MB)"
    Write-Host "Share this single file. First run copies config templates next to exe;"
    Write-Host "output/ data is saved next to exe (persistent)."
} else {
    Write-Host "== Build onedir (thin exe + external collector, dev hot-update) =="
    & $py -m PyInstaller --noconfirm --clean --onedir --name perfdog `
        --paths collector `
        --hidden-import main --hidden-import web `
        packaging\launcher.py
    if ($LASTEXITCODE -ne 0) { Write-Host "[ERROR] PyInstaller onedir failed"; exit 1 }
    $dist = Join-Path $root "dist\perfdog"
    $collectorDest = Join-Path $dist "collector"
    if (Test-Path $collectorDest) { Remove-Item -Recurse -Force $collectorDest }
    New-Item -ItemType Directory -Force -Path $collectorDest | Out-Null
    Get-ChildItem "$root\collector" -Force | Where-Object {
        $_.Name -notmatch '^(output|__pycache__|\.app_labels\.json)$'
    } | Copy-Item -Destination $collectorDest -Recurse -Force
    foreach ($d in @("web", "sdk")) {
        $dest = Join-Path $dist $d
        if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
        Copy-Item -Path (Join-Path $root $d) -Destination $dist -Recurse -Force
    }
    foreach ($f in @("start_perfdog.bat", "start_dashboard.bat", "README.md", "指标说明.md")) {
        Copy-Item -Path (Join-Path $root $f) -Destination $dist -Force
    }
    Write-Host "[OK] onedir dist: $dist"
}

Write-Host "Done."
