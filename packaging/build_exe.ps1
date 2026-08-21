# Build script for perfdog.exe (PyInstaller onedir, thin exe + external collector source)
# Usage:  pwsh -ExecutionPolicy Bypass -File build_exe.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = "C:\Users\SparkGame\AppData\Local\Programs\Python\Python314\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

Write-Host "== 1/3 PyInstaller build (onedir, thin launcher) =="
& $py -m PyInstaller --noconfirm --clean --onedir --name perfdog packaging\launcher.py
if ($LASTEXITCODE -ne 0) { Write-Host "[ERROR] PyInstaller failed"; exit 1 }

$dist = Join-Path $root "dist\perfdog"
if (-not (Test-Path $dist)) { Write-Host "[ERROR] dist not found: $dist"; exit 1 }

Write-Host "== 2/3 Assemble distribution dir: collector/web/sdk/docs/bat =="
# collector source (exclude runtime artifacts)
$collectorDest = Join-Path $dist "collector"
if (Test-Path $collectorDest) { Remove-Item -Recurse -Force $collectorDest }
New-Item -ItemType Directory -Force -Path $collectorDest | Out-Null
Get-ChildItem "$root\collector" -Force | Where-Object {
    $_.Name -notmatch '^(output|__pycache__|\.app_labels\.json)$'
} | Copy-Item -Destination $collectorDest -Recurse -Force

# web / sdk / docs / bat
foreach ($d in @("web", "sdk")) {
    $dest = Join-Path $dist $d
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    Copy-Item -Path (Join-Path $root $d) -Destination $dist -Recurse -Force
}
foreach ($f in @("start_perfdog.bat", "start_dashboard.bat", "README.md", "指标说明.md")) {
    Copy-Item -Path (Join-Path $root $f) -Destination $dist -Force
}

Write-Host "== 3/3 Done =="
Write-Host "OUTPUT: $dist"
Write-Host "Quick test: & '$dist\perfdog.exe' --show-foreground"
