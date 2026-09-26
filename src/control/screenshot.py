"""Screenshots, through the platform's own compositor-safe capture tool.

* **KDE/Wayland** -- `spectacle`, in background mode so it never steals focus or
  shows its own UI;
* **Windows** -- GDI directly (`PrintWindow` for a window, `BitBlt` for the
  screen) plus the drag-select overlay in `winapi.py` for a region.

The image itself is encoded by `_png.py`: raw GDI bits in, a real PNG out, with
no image library in between. With an active session (CONTROL_SESSION_ID)
captures land in that session's own directory, so parallel agents never overwrite
or pick up each other's images.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

from . import platforms

if platforms.IS_WINDOWS:
    from . import winapi as _win
    from ._png import encode as _encode_png
else:
    _win = None

DEFAULT_DIR = platforms.scratch_dir("control-screenshots")


class ScreenshotError(RuntimeError):
    pass


def _capture(mode_flag: str, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if _win is not None:
        return _capture_windows(mode_flag, out_path)
    result = subprocess.run(
        ["spectacle", "-b", "-n", mode_flag, "-o", str(out_path)],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0 or not out_path.exists():
        raise ScreenshotError(result.stderr.strip() or result.stdout.strip() or "spectacle failed")
    return out_path


def _capture_windows(mode: str, out_path: Path) -> Path:
    """Grab with GDI and write the PNG by hand.

    `mode` is the same vocabulary spectacle was called with ("-f", "-a", "-r"),
    so both backends can share the callers.
    """
    if mode == "-f":
        width, height, rgb = _win.capture_screen()
    elif mode == "-a":
        hwnd = _win.foreground_window()
        if not hwnd:
            raise ScreenshotError("no window has the focus, so there is nothing to capture")
        width, height, rgb = _win.capture_window(hwnd)
    elif mode == "-r":
        area = _win.select_region()
        if area is None:
            raise ScreenshotError("region selection was cancelled", hint="nothing was captured")
        width, height, rgb = _win.capture_screen(area)
    else:
        raise ScreenshotError(f"unknown capture mode {mode!r}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(_encode_png(width, height, rgb))
    return out_path


def output_dir() -> Path:
    name = os.environ.get("CONTROL_SESSION_ID")
    if not name:
        return DEFAULT_DIR
    from . import session as session_mod  # lazy: keep this module dependency-free

    return session_mod.session_dir(name) / "screenshots"


def _timestamped(prefix: str) -> Path:
    # pid in the name: two agents sharing a directory in the same millisecond
    # must still get distinct files.
    return output_dir() / f"{prefix}-{int(time.time() * 1000)}-{os.getpid()}.png"


def fullscreen(out_path: Path | None = None) -> Path:
    return _capture("-f", out_path or _timestamped("full"))


def active_window(out_path: Path | None = None) -> Path:
    return _capture("-a", out_path or _timestamped("window"))


def region(out_path: Path | None = None) -> Path:
    """Interactive region select (blocks until the user drags a rectangle)."""
    return _capture("-r", out_path or _timestamped("region"))
