# Smart Video Compiler - local build script
# Produces: dist\SmartVideoCompiler\  and  SmartVideoCompiler_Setup.exe
#
# Prerequisites (one time):
#   - Python env with: pip install -r requirements.txt
#   - Inno Setup 6 (https://jrsoftware.org/isinfo.php)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot

Write-Host "[1/3] PyInstaller..." -ForegroundColor Cyan
python -m PyInstaller SmartVideoCompiler.spec --noconfirm
if ($LASTEXITCODE -ne 0) { Pop-Location; Write-Error "PyInstaller failed."; exit 1 }

Write-Host "[2/3] Locating Inno Setup (ISCC.exe)..." -ForegroundColor Cyan
$isccCandidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    Pop-Location
    Write-Error "Inno Setup 6 not found. Install from https://jrsoftware.org/isdl.php"
    exit 1
}

Write-Host "[3/3] Compiling installer..." -ForegroundColor Cyan
& $iscc SmartVideoCompiler.iss
if ($LASTEXITCODE -ne 0) { Pop-Location; Write-Error "Inno Setup failed."; exit 1 }

$setup = Join-Path $PSScriptRoot "SmartVideoCompiler_Setup.exe"
Pop-Location

if (Test-Path $setup) {
    Write-Host "`nDONE -> $setup" -ForegroundColor Green
} else {
    Write-Error "Setup file not found after build."
}
