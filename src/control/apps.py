"""Application discovery and launching via .desktop entries (freedesktop standard, DE-agnostic)."""
from __future__ import annotations

import configparser
import difflib
import hashlib
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import windows
from .env import scrubbed_environ
from .locking import FileMutex

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
    hint = "check the app id with: control app list"

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        if hint is not None:
            self.hint = hint


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


def suggest_ids(app_id: str, limit: int = 3) -> list[str]:
    """Desktop-file ids the user probably meant by `app_id`.

    The usual mistake is the short name for a reverse-DNS id ("kcalc" for
    "org.kde.kcalc"), so ids ending in ".<app_id>" come first, then ids or
    display names containing it, then look-alikes (typos).
    """
    q = app_id.lower().removesuffix(".desktop")
    installed = list_apps()
    ids = [a.app_id for a in installed]
    ranked = [i for i in ids if i.lower().endswith("." + q)]
    ranked += [a.app_id for a in installed if q in a.app_id.lower() or q in a.name.lower()]
    # Typos: compare against what people actually type -- the id's last segment
    # ("dolphin" of org.kde.dolphin) and the display name -- not the full id.
    short: dict[str, str] = {}
    for a in installed:
        short.setdefault(a.app_id.lower().rsplit(".", 1)[-1], a.app_id)
        short.setdefault(a.name.lower(), a.app_id)
    ranked += [short[m] for m in difflib.get_close_matches(q, short, n=limit, cutoff=0.7)]
    return list(dict.fromkeys(i for i in ranked if i.lower() != q))[:limit]


def _launch_failed(app_id: str, result: subprocess.CompletedProcess) -> AppLaunchError:
    suggestions = suggest_ids(app_id)
    hint = (f"did you mean: {', '.join(suggestions)}? (ids are .desktop names; "
            "list them with: control app list)" if suggestions else
            f"no installed app looks like {app_id!r}; list the valid .desktop ids with: "
            f"control app list")
    return AppLaunchError(result.stderr.strip() or f"could not launch {app_id}", hint=hint)


DEFAULT_WAIT_TIMEOUT = 30.0  # seconds; heavy apps (video editors, IDEs, office suites) can be slow


def _gtk_launch(app_id: str) -> subprocess.CompletedProcess:
    """Run gtk-launch without inheriting pipes into the launched app.

    With `capture_output=True` the GUI app inherits gtk-launch's stdout/stderr
    pipes and keeps them open, so `run()` waits for EOF until the *app* exits --
    every launch "timed out" after 10s even though the app started fine. A temp
    file has no EOF to wait for, and still lets us report gtk-launch's error.
    """
    with tempfile.TemporaryFile(mode="w+") as err:
        proc = subprocess.run(
            ["gtk-launch", app_id],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
            timeout=10, env=scrubbed_environ(),
        )
        err.seek(0)
        stderr = err.read()
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout="", stderr=stderr)


def launch(
    app_id: str,
    wait: bool = False,
    timeout: float = DEFAULT_WAIT_TIMEOUT,
    window_pattern: str | None = None,
    desktop: int | None = None,
    activate: bool = True,
) -> subprocess.CompletedProcess | windows.Window | None:
    """Launch by desktop-file id (e.g. 'org.kde.dolphin', 'firefox').

    If `wait` is True, blocks (up to `timeout` seconds) until a *new* window
    matching `window_pattern` (default: `app_id`) appears and returns it instead of
    firing-and-forgetting. Returns None if no new window showed up in time.

    "New" is decided by baseline-diff: the matching window ids are captured just
    before gtk-launch runs and excluded while polling, so an already-open window
    of the same app (the user's, or another agent's) is never mistaken for the
    one this call started.

    `desktop` (1-based) sends the new window to that virtual desktop, which is
    how an agent keeps what it launches inside its own workspace. It implies
    `wait`, since the window has to exist before it can be moved.

    `activate` focuses the new window once it's in place, so the next typed
    keys go to it. Note that focusing a window on another desktop makes KWin
    switch to that desktop.
    """
    if desktop is None and not wait:
        result = _gtk_launch(app_id)
        if result.returncode != 0:
            raise _launch_failed(app_id, result)
        return result

    pattern = window_pattern or app_id
    # Held from "what exists now" until the new window is identified, so two
    # agents launching the same app at once can't both claim one window. Bounded
    # by the launch timeout plus slack so a stuck launch can't wedge the others.
    with _launch_lock(pattern).acquired(timeout=timeout + 10):
        known = set(windows.window_ids(pattern))
        result = _gtk_launch(app_id)
        if result.returncode != 0:
            raise _launch_failed(app_id, result)
        # Searched across all desktops: the window appears wherever KWin put it.
        win = windows.wait_for_window(pattern, timeout=timeout, known_ids=known,
                                      activate=False)
    if win is None:
        return None
    if desktop is not None:
        win.move_to_desktop(desktop)
    if activate:
        win.activate()
    return win


def _launch_lock(pattern: str) -> FileMutex:
    """One lock per window pattern, so only launches of the *same* app queue up."""
    digest = hashlib.sha1(pattern.encode()).hexdigest()[:12]
    lock_dir = Path(tempfile.gettempdir()) / "control-launch-locks"
    return FileMutex(lock_dir / f"{digest}.lock", timeout=None, can_disable=False)


def launch_command(command: str, args: list[str] | None = None) -> subprocess.Popen:
    """Launch an arbitrary binary directly, detached from this process."""
    return subprocess.Popen(
        [command, *(args or [])],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env=scrubbed_environ(),
    )
