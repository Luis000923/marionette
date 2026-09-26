"""Application discovery and launching.

Both platforms are driven by the same, deliberately loose, contract: an `app_id`
that resolves to something the shell can start, a `name` for humans, and a
`launch()` that either starts it or waits for the window it opens.

* **KDE** -- freedesktop `.desktop` entries, listed from the XDG application
  directories and started with `gtk-launch`;
* **Windows** -- the Start Menu (`.lnk` shortcuts and Store app aliases) plus
  anything on `PATH`, resolved and started through the shell, which is what makes
  a shortcut with its own working directory and arguments behave like a
  double-click.

Because ids come from reverse-DNS names on one side and shortcut paths on the
other, the error for a wrong id names the ones the user probably meant.
"""
from __future__ import annotations

import configparser
import difflib
import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import platforms, windows
from .env import scrubbed_environ
from .locking import FileMutex

if platforms.IS_WINDOWS:
    from . import winapi as _win
else:
    _win = None


def _app_dirs() -> list[Path]:
    """Where installed applications advertise themselves, most specific first."""
    if _win is not None:
        dirs = []
        for variable in ("APPDATA", "ProgramData"):  # per-user, then all-users
            base = os.environ.get(variable)
            if base:
                dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        local = os.environ.get("LOCALAPPDATA")
        if local:  # Store app aliases and per-user binaries
            dirs.append(Path(local) / "Microsoft" / "WindowsApps")
        return dirs
    return [
        Path.home() / ".local/share/applications",
        Path("/usr/local/share/applications"),
        Path("/usr/share/applications"),
    ]


@dataclass
class App:
    app_id: str  # what `launch` accepts: a .desktop basename, or a shortcut id
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
    found: dict[str, App] = {}
    for base in _app_dirs():
        if not base.is_dir():
            continue
        if _win is not None:
            for app in _list_windows_apps(base, found):
                found[app.app_id] = app
        else:
            for app in _list_xdg_apps(base, found):
                found[app.app_id] = app
    return sorted(found.values(), key=lambda a: a.name.lower())


def _list_xdg_apps(base: Path, found: dict[str, App]) -> list[App]:
    """`.desktop` entries, the freedesktop standard (DE-agnostic, DE-optional)."""
    added = []
    for entry in base.glob("*.desktop"):
        app_id = entry.stem
        if app_id in found:
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
        app = App(app_id=app_id, name=name, exec_=section.get("Exec", ""), path=entry)
        found[app_id] = app
        added.append(app)
    return added


_SHORTCUT_SUFFIXES = (".lnk", ".url", ".exe")


def _shortcut_name(entry: Path) -> str:
    """`Google Chrome - Shortcut.lnk` -> `Google Chrome` (what Start Menu shows)."""
    name = entry.stem
    for suffix in (" - Shortcut", " (1)", " - Enlace", " - Acceso directo"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _list_windows_apps(base: Path, found: dict[str, App]) -> list[App]:
    """Start Menu shortcuts and Store app aliases, as `folder.app` ids.

    The id is the shortcut's path under `Programs` with `\\` turned into `.`, so
    it has the same "vendor last, short name first" shape the freedesktop ids
    have, which is what `suggest_ids` reasons about. A folder named after the
    shortcut inside it (`Google\\Chrome\\Chrome.lnk`, `Adobe\\Photoshop\\...`) is
    the vendor repeating itself, and repeating it in the id would only make
    `control app launch chrome` fail: those collapse to `google.chrome`.
    """
    added = []
    for entry in sorted(base.rglob("*")):
        if entry.suffix.lower() not in _SHORTCUT_SUFFIXES or not entry.is_file():
            continue
        try:
            relative = entry.relative_to(base)
        except ValueError:  # pragma: no cover - rglob stays inside base
            continue
        parts = list(relative.with_suffix("").parts)
        if len(parts) > 1 and parts[-1].lower() == parts[-2].lower():
            parts = parts[:-1]
        app_id = ".".join(parts).lower()
        if app_id in found:
            continue
        app = App(app_id=app_id, name=_shortcut_name(entry), exec_=str(entry), path=entry)
        found[app_id] = app
        added.append(app)
    return added


def find(query: str) -> list[App]:
    q = query.lower()
    return [a for a in list_apps() if q in a.name.lower() or q in a.app_id.lower()]


def suggest_ids(app_id: str, limit: int = 3) -> list[str]:
    """Application ids the user probably meant by `app_id`.

    The usual mistake is the short name for a reverse-DNS id ("kcalc" for
    "org.kde.kcalc", or "chrome" for "google.chrome"), so ids ending in
    ".<app_id>" come first, then ids or display names containing it, then
    look-alikes (typos).
    """
    q = app_id.lower().removesuffix(".desktop").removesuffix(".lnk")
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
    if suggestions:
        hint = (f"did you mean: {', '.join(suggestions)}? (list them all with: "
                "control app list)")
    else:
        hint = (f"no installed app looks like {app_id!r}; list them with: "
                "control app list")
    return AppLaunchError(result.stderr.strip() or f"could not launch {app_id}", hint=hint)


DEFAULT_WAIT_TIMEOUT = 30.0  # seconds; heavy apps (video editors, IDEs, office suites) can be slow

#: Extensions Windows can start directly with CreateProcess; anything else
#: (`.lnk`, `.url`) has to go through the shell, like a double-click.
_DIRECT_SUFFIXES = (".exe", ".com", ".bat", ".cmd")


def _run_app(app_id: str) -> subprocess.CompletedProcess:
    """Start `app_id`, without handing the new app any of our pipes.

    A GUI app started with `capture_output=True` keeps the pipes open, so `run()`
    would block until *the app* exits: every launch would time out even though it
    worked. A temp file has no EOF to wait for, and still reports the launcher's
    own error.
    """
    if _win is not None:
        return _run_windows_app(app_id)
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as err:
        proc = subprocess.run(
            ["gtk-launch", app_id],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
            timeout=10, env=scrubbed_environ(),
        )
        err.seek(0)
        stderr = err.read()
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout="", stderr=stderr)


def _run_windows_app(app_id: str) -> subprocess.CompletedProcess:
    target = _resolve_windows_app(app_id)
    if target is None:
        return subprocess.CompletedProcess([app_id], 1, stdout="",
                                           stderr=f"no installed application matches {app_id!r}")
    if target.suffix.lower() in _DIRECT_SUFFIXES:
        try:
            # Not waited on: the app outlives us by design, and waiting for a GUI
            # process to exit is exactly the bug gtk-launch's temp file avoids.
            # DETACHED_PROCESS + a new process group keeps it alive when our
            # console goes away.
            subprocess.Popen(
                [str(target)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=scrubbed_environ(),
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        except OSError as exc:
            return subprocess.CompletedProcess([str(target)], 1, stdout="", stderr=str(exc))
        return subprocess.CompletedProcess([str(target)], 0, stdout="", stderr="")
    try:
        # Start Menu shortcuts and .url files need the shell to resolve the target,
        # its arguments and its working directory.
        _win.open_path(str(target))
    except _win.Win32Error as exc:
        return subprocess.CompletedProcess([str(target)], 1, stdout="", stderr=str(exc))
    return subprocess.CompletedProcess([str(target)], 0, stdout="", stderr="")


def _resolve_windows_app(app_id: str) -> Path | None:
    """Where `app_id` lives: a Start Menu entry, a PATH executable, or a bare path."""
    key = app_id.strip().strip('"')
    for base in _app_dirs():
        if not base.is_dir():
            continue
        for entry in base.rglob("*"):
            if entry.is_file() and entry.suffix.lower() in _SHORTCUT_SUFFIXES:
                try:
                    relative = entry.relative_to(base)
                except ValueError:  # pragma: no cover
                    continue
                parts = list(relative.with_suffix("").parts)
                if len(parts) > 1 and parts[-1].lower() == parts[-2].lower():
                    parts = parts[:-1]
                if ".".join(parts).lower() == key.lower():
                    return entry
    direct = Path(key)
    if direct.is_file():
        return direct
    for candidate in (key, f"{key}.exe", f"{key}.lnk"):
        found = shutil.which(candidate)
        if found:
            return Path(found)
    return None


def launch(
    app_id: str,
    wait: bool = False,
    timeout: float = DEFAULT_WAIT_TIMEOUT,
    window_pattern: str | None = None,
    desktop: int | None = None,
    activate: bool = True,
) -> subprocess.CompletedProcess | windows.Window | None:
    """Launch an app by id (e.g. 'org.kde.dolphin', 'google.chrome', 'notepad').

    If `wait` is True, blocks (up to `timeout` seconds) until a *new* window
    matching `window_pattern` (default: `app_id`) appears and returns it instead of
    firing-and-forgetting. Returns None if no new window showed up in time.

    "New" is decided by baseline-diff: the matching window ids are captured just
    before the app starts and excluded while polling, so an already-open window
    of the same app (the user's, or another agent's) is never mistaken for the
    one this call started.

    `desktop` (1-based) sends the new window to that virtual desktop, which is
    how an agent keeps what it launches inside its own workspace. It implies
    `wait`, since the window has to exist before it can be moved.

    `activate` focuses the new window once it's in place, so the next typed
    keys go to it. Note that focusing a window on another desktop makes the
    compositor show that desktop.
    """
    if desktop is None and not wait:
        result = _run_app(app_id)
        if result.returncode != 0:
            raise _launch_failed(app_id, result)
        return result

    pattern = window_pattern or app_id
    # Held from "what exists now" until the new window is identified, so two
    # agents launching the same app at once can't both claim one window. Bounded
    # by the launch timeout plus slack so a stuck launch can't wedge the others.
    with _launch_lock(pattern).acquired(timeout=timeout + 10):
        known = set(windows.window_ids(pattern))
        result = _run_app(app_id)
        if result.returncode != 0:
            raise _launch_failed(app_id, result)
        # Searched across all desktops: the window appears wherever the OS put it.
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
    if _win is not None:
        return subprocess.Popen(
            [command, *(args or [])],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=scrubbed_environ(), creationflags=subprocess.DETACHED_PROCESS,
        )
    return subprocess.Popen(
        [command, *(args or [])],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env=scrubbed_environ(),
    )
