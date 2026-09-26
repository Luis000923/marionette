"""Which desktop we are driving, and where each platform keeps per-user state.

Every other module asks this one instead of hardcoding POSIX conventions:
`/tmp` for scratch files, `~/.local/share` for state, `fcntl.flock` for locking,
`xdg-open`/`gtk-launch` for launching. Those are Linux/KDE facts, and hardcoding
them is exactly what makes a tool "not really native" somewhere else -- state
scattered over `C:/` roots, lock files named `/tmp/...` (a literal directory
name on Windows), a `.desktop` file list that a Windows user has never seen.

So the two questions are answered once, here:

* which backend speaks to the desktop (`IS_WINDOWS` -> Win32 through ctypes,
  otherwise kdotool/ydotool/spectacle on KDE Plasma/Wayland);
* where the state goes (`data_home()` -> `%LOCALAPPDATA%\\control` or
  `~/.local/share/control`; `runtime_dir()` -> `%TEMP%` or `$XDG_RUNTIME_DIR`).

Everything downstream is `pathlib`, so separators and drive letters come out
right on both platforms without any string munging.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

#: True on native Windows (where the Win32 backend in `winapi.py` is used).
IS_WINDOWS = os.name == "nt" or sys.platform == "win32"
#: True on Linux/BSD/macOS (where the kdotool/ydotool backend is used).
IS_POSIX = not IS_WINDOWS


def data_home() -> Path:
    """Per-user data root: where sessions, profiles and logs belong.

    Windows: `%LOCALAPPDATA%` (per-user, never synced, the equivalent of
    XDG_DATA_HOME). POSIX: `$XDG_DATA_HOME`, else `~/.local/share`.
    """
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base)
        return Path.home() / "AppData" / "Local"
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base)
    return Path.home() / ".local" / "share"


def runtime_dir() -> Path:
    """Scratch root for lock files, screenshots and other throwaway data.

    Windows: `%TEMP%` (per-user, cleaned up by the OS). POSIX:
    `$XDG_RUNTIME_DIR`, falling back to `/tmp` when there is no session bus
    (cron, containers, a Windows-style headless run).
    """
    if IS_WINDOWS:
        return Path(os.environ.get("TEMP") or tempfile.gettempdir())
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base)
    return Path(tempfile.gettempdir())


def state_dir() -> Path:
    """Root of this tool's own state (sessions, browser profiles, logs)."""
    return data_home() / "control"


def scratch_dir(name: str) -> Path:
    """A named scratch directory next to the lock files (`control-screenshots`...)."""
    return runtime_dir() / name
