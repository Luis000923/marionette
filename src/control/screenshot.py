"""Screenshots via spectacle, KDE's native (Wayland-safe) screenshot tool.

With an active session (CONTROL_SESSION_ID) captures land in that session's own
directory, so parallel agents never overwrite or pick up each other's images.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

DEFAULT_DIR = Path(tempfile.gettempdir()) / "control-screenshots"


class ScreenshotError(RuntimeError):
    pass


def _capture(mode_flag: str, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["spectacle", "-b", "-n", mode_flag, "-o", str(out_path)],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0 or not out_path.exists():
        raise ScreenshotError(result.stderr.strip() or result.stdout.strip() or "spectacle failed")
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
