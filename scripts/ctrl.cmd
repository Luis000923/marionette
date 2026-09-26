@echo off
rem ---------------------------------------------------------------------------
rem ctrl -- run the project CLI on Windows.
rem
rem The short name is the point: C:\Windows\System32 already owns control.exe
rem (the Control Panel) and System32 comes first in PATH, so bare `control`
rem never reaches this project however the PATH is ordered. `ctrl` is free,
rem and control.cmd still exists for parity with the Linux command.
rem
rem scripts\install.ps1 writes this shim into %LOCALAPPDATA%\Marionette\bin and
rem adds that directory to the user PATH. It works from a copy of the repository
rem too, because it looks for the venv relative to its own location first.
rem ---------------------------------------------------------------------------
setlocal
set "REPO=%~dp0.."
if not defined MARIONETTE_PYTHON set "MARIONETTE_PYTHON=%REPO%\.venv\Scripts\python.exe"
if not exist "%MARIONETTE_PYTHON%" set "MARIONETTE_PYTHON=python"
"%MARIONETTE_PYTHON%" -m control.cli %*
endlocal
