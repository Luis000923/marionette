param(
    # Reuse the Python that is already in the venv instead of installing one.
    [switch]$UseExistingPython
)
$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $RepoRoot '.venv'
$env:PYTHONPATH = Join-Path $RepoRoot 'src'
$env:PYTHONIOENCODING = 'utf-8'

$python = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $python)) {
    if ($UseExistingPython) { $python = (Get-Command python).Source }
    else {
        Write-Host "No virtual environment yet. Run this first:" -ForegroundColor Yellow
        Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\install.ps1"
        exit 1
    }
}

function Invoke-Control { & $python -m control.cli @args }

# Chromium is the one optional piece: it is only needed by `control browser`,
# and its CDN is not always reachable (proxy, TLS interception, offline). So a
# failed download is reported and stepped over, not treated as a broken install
# -- otherwise this script cannot smoke-test anything on such a machine.
$browserReady = $true
& $python -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    $browserReady = $false
    Write-Host "Playwright's Chromium did not download (network, proxy or TLS)." -ForegroundColor Yellow
    Write-Host "  'control browser' will not work until it does; everything else will." -ForegroundColor Yellow
    Write-Host "  Retry with: $python -m playwright install chromium" -ForegroundColor DarkGray
}

# A smoke test that cannot touch the real desktop: --help, then a JSON usage
# error, then one read-only query against the real window list.
& $python -m control.cli --help > $null
if ($LASTEXITCODE -ne 0) { Write-Host "control --help failed" -ForegroundColor Red; exit 1 }
Write-Host "control --help ok" -ForegroundColor Green

& $python -m control.cli --json win list
if ($LASTEXITCODE -ne 0) {
    Write-Host "win list failed" -ForegroundColor Red
    exit 1
}
Write-Host "win list ok -- you are ready to drive this desktop." -ForegroundColor Green

if ($browserReady) {
    Write-Host "browser ok -- 'control browser' is ready too." -ForegroundColor Green
}
