"""Application discovery and launching via .desktop entries (freedesktop standard, DE-agnostic)."""
from __future__ import annotations

import configparser
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import windows

_APP_DIRS = [
    Path.home() / ".local/share/applications",
    Path("/usr/local/share/applications"),
    Path("/usr/share/applications"),
]


@dataclass
class App:
    app_id: str  # desktop file basename without .desktop, what gtk-launch expects
    name: str
    exec_: str
    path: Path


class AppLaunchError(RuntimeError):
    pass


def list_apps() -> list[App]:
    seen: dict[str, App] = {}
    for base in _APP_DIRS:
        if not base.is_dir():
            continue
        for entry in base.glob("*.desktop"):
            app_id = entry.stem
            if app_id in seen:
                continue
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(entry, encoding="utf-8")
            except configparser.Error:
                continue
            if "Desktop Entry" not in parser:
                continue
            section = parser["Desktop Entry"]
            if section.get("NoDisplay", "false").lower() == "true":
                continue
            if section.get("Type", "Application") != "Application":
                continue
            name = section.get("Name", app_id)
            exec_ = section.get("Exec", "")
            seen[app_id] = App(app_id=app_id, name=name, exec_=exec_, path=entry)
    return sorted(seen.values(), key=lambda a: a.name.lower())


def find(query: str) -> list[App]:
    q = query.lower()
    return [a for a in list_apps() if q in a.name.lower() or q in a.app_id.lower()]


DEFAULT_WAIT_TIMEOUT = 30.0  # seconds; heavy apps (video editors, IDEs, office suites) can be slow


def launch(
    app_id: str,
    wait: bool = False,
    timeout: float = DEFAULT_WAIT_TIMEOUT,
    window_pattern: str | None = None,
) -> subprocess.Popen | windows.Window | None:
    """Launch by desktop-file id (e.g. 'org.kde.dolphin', 'firefox').

    If `wait` is True, blocks (up to `timeout` seconds) polling window.py until a window
    matching `window_pattern` (default: `app_id`) appears, and returns that Window instead
    of firing-and-forgetting -- handy for heavy apps that take a while to show a window.
    Returns None if `wait` is True but no matching window showed up before the timeout.
    """
    result = subprocess.run(
        ["gtk-launch", app_id], capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        raise AppLaunchError(result.stderr.strip() or f"could not launch {app_id}")
    if wait:
        return windows.wait_for_window(window_pattern or app_id, timeout=timeout)
    return result


def launch_command(command: str, args: list[str] | None = None) -> subprocess.Popen:
    """Launch an arbitrary binary directly, detached from this process."""
    return subprocess.Popen(
        [command, *(args or [])],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
