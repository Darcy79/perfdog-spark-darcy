# =====================================================================
# build_installer.ps1 — 一键构建 PerfDog-CN 安装包
# 流程：PyInstaller onefile（dist\perfdog.exe，已存在则跳过）
#        → Inno Setup ISCC 编译 packaging\perfdog_installer.iss
#        → 产物 packaging\installer_output\PerfDog-CN-Setup-<版本>.exe
#
# 用法（在项目根目录或 packaging 目录均可）：
#   pwsh -ExecutionPolicy Bypass -File packaging\build_installer.ps1
#   pwsh ... -File packaging\build_installer.ps1 -IsccPath "D:\tools\ISCC.exe"
#   pwsh ... -File packaging\build_installer.ps1 -SkipExe   # 复用已有 exe，只编安装包
#
# Inno Setup 6.x 下载：https://jrsoftware.org/isdl.php （免费）
# =====================================================================
param(
    [string]$IsccPath = "",          # ISCC.exe 路径，默认按常见安装位置自动探测
    [switch]$SkipExe                 # 跳过 exe 构建（dist\perfdog.exe 已就绪时）
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# ---- 1) 确保 dist\perfdog.exe（onefile）存在 ----
$exe = Join-Path $root "dist\perfdog.exe"
if ($SkipExe) {
    if (-not (Test-Path $exe)) { Write-Host "[ERROR] -SkipExe 但找不到 $exe"; exit 1 }
    Write-Host "[*] 跳过 exe 构建，使用已有: $exe"
} elseif (Test-Path $exe) {
    Write-Host "[*] dist\perfdog.exe 已存在，跳过 exe 构建（如需强制重建先删它或去掉本判断）"
} else {
    Write-Host "== [1/2] 构建 perfdog.exe（PyInstaller onefile） =="
    $buildExe = Join-Path $PSScriptRoot "build_exe.ps1"
    # 优先 pwsh（PowerShell 7），没有则回退 Windows PowerShell 5.1
    if (Get-Command pwsh -ErrorAction SilentlyContinue) {
        & pwsh -ExecutionPolicy Bypass -File $buildExe -Mode onefile
    } else {
        & powershell -ExecutionPolicy Bypass -File $buildExe -Mode onefile
    }
    if ($LASTEXITCODE -ne 0) { Write-Host "[ERROR] exe 构建失败"; exit 1 }
}

# ---- 2) 定位 ISCC.exe ----
if (-not $IsccPath) {
    $candidates = @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    # 版本号不同的 Inno Setup 目录（Inno Setup 5/7 等）也扫一遍
    foreach ($base in @("C:\Program Files (x86)", "C:\Program Files")) {
        Get-ChildItem $base -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "Inno Setup*" } |
            ForEach-Object { $candidates += (Join-Path $_.FullName "ISCC.exe") }
    }
    foreach ($c in $candidates) {
        if (Test-Path $c) { $IsccPath = $c; break }
    }
}
if (-not $IsccPath -or -not (Test-Path $IsccPath)) {
    Write-Host "[ERROR] 未找到 ISCC.exe。请先安装 Inno Setup 6.x："
    Write-Host "        https://jrsoftware.org/isdl.php"
    Write-Host "        装好后重跑本脚本，或用 -IsccPath 指定 ISCC.exe 路径。"
    exit 1
}
Write-Host "[*] ISCC: $IsccPath"

# ---- 3) 编译安装包 ----
Write-Host "== [2/2] 编译安装包（Inno Setup） =="
$iss = Join-Path $PSScriptRoot "perfdog_installer.iss"
& $IsccPath $iss
if ($LASTEXITCODE -ne 0) { Write-Host "[ERROR] ISCC 编译失败"; exit 1 }

$setup = Get-ChildItem (Join-Path $PSScriptRoot "installer_output") -Filter "*.exe" |
         Sort-Object LastWriteTime -Descending | Select-Object -First 1
Write-Host ""
Write-Host "[OK] 安装包已生成: $($setup.FullName) ($([math]::Round($setup.Length / 1MB, 1)) MB)"
Write-Host "     发布时把这个 Setup exe 放到 GitHub Release，用户双击即可安装。"
Write-Host "     （未签名 exe/安装包首次运行会有 SmartScreen 提示：更多信息→仍要运行，"
Write-Host "       教程《使用教程-保姆级.md》第四节已说明。）"
