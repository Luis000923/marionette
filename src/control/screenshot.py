"""Screenshots via spectacle, KDE's native (Wayland-safe) screenshot tool."""
from __future__ import annotations

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


def _timestamped(prefix: str) -> Path:
    return DEFAULT_DIR / f"{prefix}-{int(time.time() * 1000)}.png"


def fullscreen(out_path: Path | None = None) -> Path:
    return _capture("-f", out_path or _timestamped("full"))


def active_window(out_path: Path | None = None) -> Path:
    return _capture("-a", out_path or _timestamped("window"))


def region(out_path: Path | None = None) -> Path:
    """Interactive region select (blocks until the user drags a rectangle)."""
    return _capture("-r", out_path or _timestamped("region"))
