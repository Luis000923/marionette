@echo off
rem ---------------------------------------------------------------------------
rem control -- run the project CLI from anywhere on Windows.
rem
rem scripts\install.ps1 writes this shim into %LOCALAPPDATA%\Marionette\bin and
rem adds that directory to the user PATH. It works from a copy of the repository
rem too, because it looks for the venv relative to its own location first.
rem
rem Prefer the ctrl.cmd beside it: C:\Windows\System32 owns control.exe (the
rem Control Panel) and comes first in PATH, so bare `control` never gets here.
rem ---------------------------------------------------------------------------
setlocal
set "REPO=%~dp0.."
if not defined MARIONETTE_PYTHON set "MARIONETTE_PYTHON=%REPO%\.venv\Scripts\python.exe"
if not exist "%MARIONETTE_PYTHON%" set "MARIONETTE_PYTHON=python"
"%MARIONETTE_PYTHON%" -m control.cli %*
endlocal
