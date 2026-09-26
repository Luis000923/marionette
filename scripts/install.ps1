<#
.SYNOPSIS
    One-time setup for the control project on Windows (the equivalent of
    scripts/install.sh on KDE Plasma).

.DESCRIPTION
    Almost nothing has to be installed: input comes from the Win32 input queue,
    the clipboard from user32, screenshots from GDI and the accessibility tree
    from UI Automation, all of which are part of Windows. So this script only
    does the things that are genuinely missing:

      * installs uv (or checks for a usable Python 3.12+),
      * syncs the project and its Chromium download for Playwright,
      * writes a private .env (secrets stay out of os.environ),
      * adds ctrl.cmd (and control.cmd) to the user PATH, so `ctrl win list`
        works from any PowerShell prompt. The short name matters: System32
        already owns control.exe, so bare `control` opens the Control Panel.

    Run it from a normal (non-elevated) PowerShell: nothing here needs admin.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1
    ctrl win list --json
#>
[CmdletBinding()]
param(
    # Skip the Playwright Chromium download (large, and only needed for `control browser`).
    [switch]$NoBrowser,
    # Print what would happen without touching anything.
    [switch]$WhatIfOnly
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $RepoRoot '.venv'
$ShimDir = Join-Path $env:LOCALAPPDATA 'Marionette\bin'

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Note($message) { Write-Host "    $message" -ForegroundColor DarkGray }

function Get-Python {
    # Prefer a real 3.12+: the package uses `type` statements and PEP 604 unions.
    foreach ($candidate in @('py', 'python3', 'python')) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        try {
            $version = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -eq 0 -and $version) {
            $parts = $version.Trim().Split('.')
            if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 12)) {
                return @{ Exe = $candidate; Version = $version.Trim() }
            }
        }
    }
    return $null
}

function Get-Uv {
    $command = Get-Command 'uv' -ErrorAction SilentlyContinue
    if ($command) { return 'uv' }
    return $null
}

Write-Step "Checking Python"
$python = Get-Python
if ($python) {
    Write-Note "found $($python.Exe) (Python $($python.Version))"
} else {
    Write-Note "no Python 3.12+ on PATH"
    Write-Note "install it with: winget install Python.Python.3.12"
    Write-Note "then reopen this terminal and run the script again"
    exit 1
}

Write-Step "Checking uv"
$uv = Get-Uv
if (-not $uv) {
    if ($WhatIfOnly) {
        Write-Note "would install uv with: winget install astral-sh.uv"
    } else {
        Write-Note "installing uv with winget"
        winget install --id astral-sh.uv --exact --source winget --accept-package-agreements --accept-source-agreements | Out-Null
        $env:PATH = "$env:LOCALAPPDATA\Microsoft\WinGet\Links;$env:PATH"
        $uv = Get-Uv
    }
}
if (-not $uv) {
    if ($WhatIfOnly) {
        Write-Note "would stop here: install uv from https://docs.astral.sh/uv/"
    } else {
        Write-Note "install uv from https://docs.astral.sh/uv/ and re-run this script"
        Write-Note "(the project is also usable with plain pip: python -m pip install -e .)"
    }
    exit 1
}

Write-Step "Syncing the project into .venv"
if ($WhatIfOnly) {
    Write-Note "would run: $uv sync"
} else {
    Push-Location $RepoRoot
    try {
        & $uv sync
    } finally {
        Pop-Location
    }
}

if (-not $NoBrowser) {
    Write-Step "Downloading the Playwright Chromium (for 'control browser')"
    if ($WhatIfOnly) {
        Write-Note "would run: .venv\Scripts\python.exe -m playwright install chromium"
    } else {
        Push-Location $RepoRoot
        try {
            # The venv's own interpreter, not `uv run`: the binary is what ships
            # the playwright CLI, and this keeps the download out of a resolver.
            & (Join-Path $Venv 'Scripts\python.exe') -m playwright install chromium
        } finally {
            Pop-Location
        }
    }
}

Write-Step "Writing a private .env"
$envFile = Join-Path $RepoRoot '.env'
if (Test-Path $envFile) {
    Write-Note "$envFile already exists, leaving it alone"
} elseif ($WhatIfOnly) {
    Write-Note "would copy .env.example to .env"
} else {
    Copy-Item (Join-Path $RepoRoot '.env.example') $envFile
    Write-Note "created $envFile"
    # Windows ACLs, not mode bits: only this user (and administrators) may read
    # it. Best effort -- the tool itself refuses to read a secret from a file
    # others can read, so a failure here is reported, not fatal.
    try {
        $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $acl = Get-Acl $envFile
        $acl.SetAccessRuleProtection($true, $false)   # drop inherited permissions
        $acl.SetAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            $me, 'FullControl', 'Allow')))
        Set-Acl -Path $envFile -AclObject $acl
        Write-Note "locked it down: readable only by $me"
    } catch {
        Write-Note "could not set the ACL ($($_.Exception.Message))"
        Write-Note "move the .env into your user profile, which is private by default"
    }
}

Write-Step "Adding the control command to your PATH"
if ($WhatIfOnly) {
    foreach ($name in 'ctrl.cmd', 'control.cmd') {
        Write-Note "would write $(Join-Path $ShimDir $name)"
    }
} else {
    New-Item -ItemType Directory -Force -Path $ShimDir | Out-Null
    # `ctrl` is the name to actually type on Windows: C:\Windows\System32 already
    # owns `control.exe` (the Control Panel) and System32 comes first in PATH, so
    # `control` there can never reach us. `control.cmd` is still written for parity
    # with the Linux command and for anyone who puts this dir ahead of System32.
    foreach ($name in 'ctrl.cmd', 'control.cmd') {
        @(
            '@echo off',
            'rem Generated by scripts\install.ps1 -- runs the project venv CLI from anywhere.',
            'rem Delete it, or re-run the installer, to move to a different checkout.',
            'setlocal',
            "set `"MARIONETTE_PYTHON=$Venv\Scripts\python.exe`"",
            'if not exist "%MARIONETTE_PYTHON%" set "MARIONETTE_PYTHON=python"',
            '"%MARIONETTE_PYTHON%" -m control.cli %*',
            'endlocal'
        ) | Set-Content -Path (Join-Path $ShimDir $name) -Encoding ascii
        Write-Note "wrote $(Join-Path $ShimDir $name)"
    }
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath -notlike "*$ShimDir*") {
        [Environment]::SetEnvironmentVariable('Path', "$userPath;$ShimDir", 'User')
        Write-Note "added $ShimDir to your user PATH (reopen the terminal)"
    }
    Write-Note "type 'ctrl', not 'control': System32's control.exe is the Control Panel"
}

Write-Host ""
Write-Host "Done. Try:" -ForegroundColor Green
Write-Host "  ctrl win list --json"
Write-Host "  ctrl shot full"
Write-Host "  ctrl a11y apps              # UI Automation: native widgets by name"
Write-Host ""
Write-Host "Notes:" -ForegroundColor DarkGray
Write-Host "  * The command is 'ctrl' here, 'control' on Linux: C:\Windows\System32"
Write-Host "    already owns control.exe (the Control Panel) and it comes first in PATH."
Write-Host "    From the repo itself, .venv\Scripts\control.exe works under either name."
Write-Host "  * Nothing here needs an elevated shell: input, clipboard, screenshots and"
Write-Host "    accessibility all come from the session itself."
Write-Host "  * 'ctrl a11y' needs the comtypes dependency (installed by uv sync)."
Write-Host "  * Virtual desktops need Windows 10 1607+; on builds without them the tool"
Write-Host "    reports it and works against the single desktop instead."
