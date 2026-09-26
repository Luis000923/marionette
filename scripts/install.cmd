@echo off
rem Double-clickable entry point: runs the PowerShell installer, which is the
rem real script (and the one to read). Kept so setup is one click away.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
