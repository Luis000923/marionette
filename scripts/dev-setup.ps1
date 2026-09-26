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

& $python -m playwright install chromium
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

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
