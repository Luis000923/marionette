"""Window management, on KDE Plasma/Wayland through kdotool, or on Windows natively.

Virtual desktops are the isolation unit for parallel agents: each agent gets its
own desktop and every window command can be scoped to it with `desktop=N`, so
`win list`/`win close` in agent 1 never see or touch agent 2's windows. Which
mechanism provides that desktop depends on the platform:

* **KDE Plasma/Wayland** -- a KWin virtual desktop. kdotool drives the windows
  and `gdbus` drives KWin's `org.kde.KWin.VirtualDesktopManager` D-Bus API (the
  only way to create and remove desktops, which kdotool cannot do);
* **Windows** -- a Win32 virtual desktop, through the `IVirtualDesktopManager`
  COM interface in `winapi.py`. Where Windows does not have them (pre-1607
  builds, Server editions, stripped images) the whole module degrades to the one
  desktop Windows always has, and says so through `virtual_desktops_available()`.

Two numbering schemes meet here, and mixing them up silently targets the wrong
desktop:

* this module's public API uses **1-based** desktop numbers, matching what the
  user sees, and what kdotool and KWin's 0-based D-Bus positions map onto;
* KWin's D-Bus API uses **0-based** positions plus an opaque UUID.

`Desktop` carries both: `number` (1-based) and `position` (0-based).
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import time
from dataclasses import dataclass

from . import platforms
from .locking import sibling_lock

#: Max concurrent kdotool processes for async queries; kdotool loads a KWin
#: script per invocation, so unbounded fan-out would hammer the compositor.
#: Windows talks to user32 in-process, so it needs no such bound.
MAX_CONCURRENT_QUERIES = 8

_DBUS_DEST = "org.kde.KWin"
_DBUS_PATH = "/VirtualDesktopManager"
_DBUS_IFACE = "org.kde.KWin.VirtualDesktopManager"

if platforms.IS_WINDOWS:
    from . import winapi as _win
    from .winapi import Win32Error
else:
    _win = None
    Win32Error = None  # only the Windows backend can raise it


class WindowError(RuntimeError):
    """Any window-control failure, whichever backend produced it.

    Carries an optional `hint` (the next command to try), which the CLI prints
    next to the error instead of guessing one from the exception type.
    """

    hint = ""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        if hint is not None:
            self.hint = hint


class KdotoolError(WindowError):
    """kdotool/KWin said no (Linux backend)."""


class DesktopError(WindowError):
    """A virtual-desktop operation (create/remove/lookup) failed."""


#: Every exception a window call can raise, whichever backend is in use. Used by
#: the `focus`/`wait_settled` polling loops, which must not die because a window
#: vanished between two polls.
WINDOW_ERRORS: tuple[type[BaseException], ...] = (
    (KdotoolError, Win32Error) if Win32Error else (KdotoolError,)
)


def _parse_desktop_number(raw: str) -> int | None:
    """kdotool prints `null` for windows that belong to every desktop."""
    raw = raw.strip()
    if not raw or raw == "null":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _run(*args: str) -> str:
    result = subprocess.run(
        ["kdotool", *args], capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        raise KdotoolError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


async def _run_async(*args: str) -> str:
    """Async twin of `_run`, for state queries that should never block the caller."""
    proc = await asyncio.create_subprocess_exec(
        "kdotool", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=10)
    out = raw_out.decode().strip()
    if proc.returncode != 0:
        raise KdotoolError(raw_err.decode().strip() or out)
    return out


@dataclass
class Window:
    id: str
    title: str = ""
    classname: str = ""
    desktop: int | None = None  # 1-based; None when not looked up
    #: Executable that owns the window, lowercased without `.exe` ("chrome").
    #: Windows only: kdotool has no equivalent, and a Win32 window class such as
    #: "Chrome_WidgetWin_1" says nothing about which app it is.
    process: str = ""

    def to_dict(self) -> dict:
        data = {"id": self.id, "title": self.title, "class": self.classname,
                "desktop": self.desktop}
        if self.process:
            data["process"] = self.process
        return data

    def match_text(self) -> str:
        """What a search pattern is matched against: title, class and executable.

        On Windows the window class is generic (`Chrome_WidgetWin_1`,
        `CabinetWClass`), so `win list brave` has to be able to match the
        *executable* to be as useful as it is on Linux.
        """
        return " ".join(part for part in (self.title, self.classname, self.process) if part)

    def activate(self) -> None:
        if _win is not None:
            _win.activate(int(self.id))
            return
        _run("windowactivate", self.id)

    def focus(self, timeout: float = 1.0, interval: float = 0.05) -> bool:
        """Activate and wait until the desktop really reports this window as active.

        `windowactivate`/`SetForegroundWindow` return before focus has moved;
        typing straight after it can still land in the previous window. Returns
        False if focus didn't settle within `timeout` (e.g. focus-stealing
        prevention, or the window refusing activation).
        """
        self.activate()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.is_active():
                    return True
            except WINDOW_ERRORS:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    def is_active(self) -> bool:
        if _win is not None:
            return _win.foreground_window() == int(self.id)
        return _run("getactivewindow") == self.id

    def raise_(self) -> None:
        if _win is not None:
            _win.raise_window(int(self.id))
            return
        _run("windowraise", self.id)

    def close(self) -> None:
        if _win is not None:
            _win.close_window(int(self.id))
            return
        _run("windowclose", self.id)

    def minimize(self) -> None:
        if _win is not None:
            _win.show_window(int(self.id), _win.SW_MINIMIZE)
            return
        _run("windowminimize", self.id)

    def move(self, x: int | str, y: int | str, relative: bool = False) -> None:
        if _win is not None:
            if relative:
                raise Win32Error(
                    "Windows has no relative window move; pass absolute coordinates",
                    api="SetWindowPos")
            area = self.geometry()
            _win.move_resize(int(self.id), int(x), int(y),
                             area.get("width", 0) or 1, area.get("height", 0) or 1)
            return
        args = ["windowmove"]
        if relative:
            args.append("--relative")
        args += [self.id, str(x), str(y)]
        _run(*args)

    def resize(self, width: int | str, height: int | str) -> None:
        if _win is not None:
            area = self.geometry()
            _win.move_resize(int(self.id), area.get("x", 0), area.get("y", 0),
                             int(width), int(height))
            return
        _run("windowsize", self.id, str(width), str(height))

    def pid(self) -> int | None:
        """Process owning the window (what AT-SPI/UIA identify applications by)."""
        if _win is not None:
            pid = _win.window_pid(int(self.id))
            return pid or None
        out = _run("getwindowpid", self.id).strip()
        return int(out) if out.isdigit() else None

    def geometry(self) -> dict[str, int]:
        """{x, y, width, height} in screen coordinates (empty if nothing is known).

        kdotool prints "Position: X,Y" and "Geometry: WxH" on separate lines; on
        Windows the DWM extended frame bounds are used, so the numbers exclude the
        drop shadow and match what the user sees.
        """
        if _win is not None:
            try:
                return _win.window_rect(int(self.id)).as_dict()
            except Win32Error:
                return {}
        out = _run("getwindowgeometry", self.id)
        geo: dict[str, int] = {}
        if m := re.search(r"Position:\s*(-?\d+)\s*,\s*(-?\d+)", out):
            geo["x"], geo["y"] = int(m.group(1)), int(m.group(2))
        if m := re.search(r"Geometry:\s*(\d+)\s*x\s*(\d+)", out):
            geo["width"], geo["height"] = int(m.group(1)), int(m.group(2))
        return geo

    def set_state(self, add: list[str] | None = None, remove: list[str] | None = None,
                  toggle: list[str] | None = None) -> None:
        if _win is not None:
            self._set_state_windows(add or [], remove or [], toggle or [])
            return
        args = ["windowstate"]
        for prop in add or []:
            args += ["--add", prop]
        for prop in remove or []:
            args += ["--remove", prop]
        for prop in toggle or []:
            args += ["--toggle", prop]
        args.append(self.id)
        _run(*args)

    def _set_state_windows(self, add: list[str], remove: list[str], toggle: list[str]) -> None:
        """Map kdotool's window states onto ShowWindow/SetWindowPos.

        "FULLSCREEN" means "cover the monitor the window is on" rather than a mode
        change: Windows' real fullscreen is a display-mode switch that only a
        foreground process may do, and it would rearrange every monitor.
        """
        hwnd = int(self.id)
        for state, wanted in ((s, True) for s in add) | ((s, False) for s in remove):
            if state == "MAXIMIZED":
                _win.show_window(hwnd, _win.SW_MAXIMIZE if wanted else _win.SW_RESTORE)
            elif state == "MINIMIZED":
                _win.show_window(hwnd, _win.SW_MINIMIZE if wanted else _win.SW_RESTORE)
            elif state == "FULLSCREEN":
                if wanted:
                    self.fullscreen()
                elif _win.is_maximized(hwnd):
                    _win.show_window(hwnd, _win.SW_RESTORE)
            elif state in ("ABOVE", "BELOW"):
                _win.set_topmost(hwnd, wanted if state == "ABOVE" else False)
            else:
                raise Win32Error(
                    f"Windows has no {state} window state; the states Windows can "
                    "change are MAXIMIZED, MINIMIZED, FULLSCREEN, ABOVE and BELOW",
                    api="SetState")
        for state in toggle:
            if state == "MAXIMIZED":
                _win.show_window(hwnd, _win.SW_RESTORE if _win.is_maximized(hwnd)
                                 else _win.SW_MAXIMIZE)
            elif state == "ABOVE":
                _win.set_topmost(hwnd, not _win.is_topmost(hwnd))
            else:
                raise Win32Error(f"cannot toggle the {state} window state on Windows",
                                 api="SetState")

    def maximize(self) -> None:
        self.set_state(add=["MAXIMIZED"])

    def fullscreen(self) -> None:
        if _win is not None:
            if _win.is_minimized(int(self.id)):
                _win.show_window(int(self.id), _win.SW_RESTORE)
            monitor = _win.monitor_rect(int(self.id))
            _win.move_resize(int(self.id), monitor.left, monitor.top, monitor.width,
                             monitor.height)
            return
        self.set_state(add=["FULLSCREEN"])

    def get_desktop(self) -> int | None:
        """1-based number of the desktop this window lives on.

        None when no desktop can be determined -- windows pinned to *all*
        desktops (and some special surfaces) have no single home.
        """
        if _win is not None:
            self.desktop = _win_desktop_of(int(self.id))
            return self.desktop
        self.desktop = _parse_desktop_number(_run("get_desktop_for_window", self.id))
        return self.desktop

    def move_to_desktop(self, number: int | str) -> None:
        """Send this window to another desktop (1-based, or "current_desktop"/"all").

        This is how an agent claims a window it just launched: pull it onto the
        agent's own desktop so it stops sharing screen space with other agents.
        """
        if _win is not None:
            ref = str(number)
            if ref == "current_desktop":
                return
            if ref == "all":
                raise Win32Error(
                    "Windows cannot pin a window to every virtual desktop; give it "
                    "one instead (control win send PATTERN N|NAME)",
                    api="MoveWindowToDesktop")
            if not ref.isdigit():
                target = find_desktop(ref)
                if target is None:
                    raise DesktopError(f"no virtual desktop named {ref!r}")
                self.desktop = target.number
            else:
                self.desktop = int(ref)
            desktops = list_desktops()
            chosen = next((d for d in desktops if d.number == self.desktop), None)
            if chosen is None:
                raise DesktopError(f"there is no virtual desktop number {self.desktop}")
            _win.move_window_to_desktop(int(self.id), chosen.id)
            return
        _run("set_desktop_for_window", self.id, str(number))
        self.desktop = number if isinstance(number, int) else None


# --- searching ---------------------------------------------------------------------

def anything_focused() -> bool:
    """Whether any window holds the foreground at all.

    `GetForegroundWindow` returns 0 when nothing is focused -- a locked screen, or
    the window that had the focus was destroyed -- and in that state Windows lets
    *no* process take the foreground, because there is no foreground thread whose
    input queue could be joined. Commands that need focus say that plainly
    instead of blaming focus-stealing prevention. Always True off Windows, where
    the question is not asked.
    """
    return bool(_win.foreground_window()) if _win is not None else True


def active_window() -> Window:
    if _win is not None:
        hwnd = _win.foreground_window()
        if not hwnd:
            # GetForegroundWindow returns 0 when nothing at all is focused -- the
            # screen is locked, or the window that had the focus was destroyed.
            # Nothing can claim the foreground in that state (there is no
            # foreground thread to join), so say that rather than leaving the
            # user to wonder why every activation is refused.
            raise Win32Error(
                "this session has no focused window at all, so none can be activated "
                "either: click any window once and try again (or check the screen is "
                "not locked)",
                api="GetForegroundWindow")
        return _win_describe(hwnd)
    win_id = _run("getactivewindow", "getwindowid")
    title = _run("getwindowname", win_id)
    classname = _run("getwindowclassname", win_id)
    return Window(id=win_id, title=title, classname=classname)


def _search_args(pattern: str, limit: int = 0, desktop: int | None = None) -> list[str]:
    args = ["search"]
    if limit:
        args += ["--limit", str(limit)]
    if desktop is not None:
        args += ["--desktop", str(desktop)]
    args.append(pattern)
    return args


def _win_describe(hwnd: int, desktop: int | None = None) -> Window:
    """Title, class and owning executable of one window, or blanks if it just died."""
    if not _win.window_exists(hwnd):
        return Window(id=str(hwnd), title="", classname="", desktop=desktop)
    title = _win.window_text(hwnd)
    classname = _win.window_class(hwnd)
    process = _win.process_name(_win.window_pid(hwnd))
    return Window(id=str(hwnd), title=title, classname=classname, desktop=desktop,
                  process=process)


def _win_match(pattern: str, window: Window) -> bool:
    """kdotool's `search` is a case-insensitive regex over the window's identity."""
    haystack = window.match_text()
    if not pattern:
        return True
    try:
        return re.search(pattern, haystack, re.IGNORECASE) is not None
    except re.error:
        # A pattern that is not a valid regex (plenty of users pass a plain title
        # with brackets in it) still deserves a substring match, not a crash.
        return pattern.lower() in haystack.lower()


def _win_candidates(desktop: int | None = None) -> list[Window]:
    """Every user window, front to back, optionally filtered to one desktop."""
    found: list[Window] = []
    for hwnd in _win.list_windows():
        if _win.is_shell_window(hwnd):
            continue  # the desktop, the taskbar, shell helper windows
        window = _win_describe(hwnd)
        if not window.title:
            continue  # a titleless top-level window is a helper, not an app
        if desktop is not None and _win_desktop_of(hwnd) != desktop:
            continue
        found.append(window)
    return found


def _win_desktop_of(hwnd: int) -> int | None:
    """1-based desktop number of a window, or None when it has no single home."""
    if not _win.virtual_desktops_supported():
        return 1  # one desktop always exists; nothing to disambiguate
    desktop_id = _win.window_desktop_id(hwnd)
    if not desktop_id:
        return None
    position = _win.desktop_position_of(desktop_id)
    return position + 1 if position is not None else None


def search(pattern: str, by: str = "any", limit: int = 0,
           desktop: int | None = None) -> list[Window]:
    """Search windows by title/class/process regex. Returns matches with metadata.

    `desktop` (1-based) restricts the search to a single virtual desktop, which
    is what keeps one agent's `win list` blind to another agent's windows.
    """
    if _win is not None:
        found = [w for w in _win_candidates(desktop) if _win_match(pattern, w)]
        return found[:limit] if limit else found
    return [_describe(win_id, desktop) for win_id in window_ids(pattern, limit, desktop)]


def window_ids(pattern: str, limit: int = 0, desktop: int | None = None) -> list[str]:
    """Ids of matching windows only -- one lookup pass, no per-window calls.

    Cheap enough to take as a *baseline* right before launching something.
    """
    if _win is not None:
        return [w.id for w in search(pattern, limit=limit, desktop=desktop)]
    out = _run(*_search_args(pattern, limit, desktop))
    return [i for i in out.splitlines() if i.strip()]


def _describe(win_id: str, desktop: int | None = None) -> Window:
    if _win is not None:
        return _win_describe(int(win_id), desktop)
    try:
        title = _run("getwindowname", win_id)
        classname = _run("getwindowclassname", win_id)
    except KdotoolError:
        title, classname = "", ""
    return Window(id=win_id, title=title, classname=classname, desktop=desktop)


async def search_async(pattern: str, limit: int = 0,
                       desktop: int | None = None) -> list[Window]:
    """Non-blocking `search`.

    A plain `search` costs 1 + 2N sequential kdotool round-trips; this issues the
    per-window lookups in parallel (bounded by MAX_CONCURRENT_QUERIES) and never
    touches the input lock, so status queries stay responsive while another
    agent is typing. Windows needs neither: the answers are already in memory.
    """
    if _win is not None:
        return search(pattern, limit=limit, desktop=desktop)
    ids_out = await _run_async(*_search_args(pattern, limit, desktop))
    ids = [i for i in ids_out.splitlines() if i.strip()]
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_QUERIES)

    async def describe(win_id: str) -> Window:
        async with semaphore:
            try:
                title, classname = await asyncio.gather(
                    _run_async("getwindowname", win_id),
                    _run_async("getwindowclassname", win_id),
                )
            except KdotoolError:
                title, classname = "", ""
        return Window(id=win_id, title=title, classname=classname, desktop=desktop)

    return list(await asyncio.gather(*(describe(i) for i in ids)))


def list_windows(pattern: str = "", desktop: int | None = None,
                 limit: int = 0) -> list[Window]:
    """Blocking wrapper around `search_async` -- same result, parallel lookups."""
    if _win is not None:
        return search(pattern, limit=limit, desktop=desktop)
    return asyncio.run(search_async(pattern, limit=limit, desktop=desktop))


def find_one(pattern: str, desktop: int | None = None) -> Window | None:
    """The first window matching `pattern`, or None.

    On Windows a bare number is also taken as a window id: `win list --json`
    prints them and an agent has every reason to feed one straight back into
    `--window`, which would otherwise only match titles, classes and executables.
    """
    if _win is not None and pattern.strip().isdigit():
        hwnd = int(pattern.strip())
        if _win.window_exists(hwnd) and not _win.is_shell_window(hwnd):
            return _win_describe(hwnd, desktop)
    matches = search(pattern, limit=1, desktop=desktop)
    return matches[0] if matches else None


def wait_for_window(pattern: str, timeout: float = 30.0, interval: float = 0.5,
                    desktop: int | None = None, known_ids: set[str] | None = None,
                    activate: bool = True) -> Window | None:
    """Poll until a window matching `pattern` appears, or `timeout` seconds pass.

    Useful after launching a heavy app (video editor, IDE, office suite) instead of a
    blind fixed sleep. Returns None on timeout rather than raising.

    Baseline-diff: pass `known_ids` -- the ids from `window_ids(pattern)` taken
    *before* launching -- and only a genuinely new window is accepted. Without it
    any match counts, so a pre-existing window of the same app is returned
    instantly (the false positive `apps.launch` used to hit).

    `activate` (default) focuses the window that was found, so the input
    commands that follow go to it rather than to whatever had focus before.
    """
    known = known_ids or set()
    deadline = time.monotonic() + timeout
    while True:
        fresh = next((i for i in window_ids(pattern, desktop=desktop) if i not in known), None)
        if fresh is not None:
            win = _describe(fresh, desktop)
            if activate:
                win.activate()
            return win
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def _window_state(win: Window) -> tuple:
    try:
        title = _win.window_text(int(win.id)) if _win is not None \
            else _run("getwindowname", win.id)
    except WINDOW_ERRORS:
        title = None
    try:
        active = win.is_active()
    except WINDOW_ERRORS:
        active = None
    return (title, tuple(sorted(win.geometry().items())), active)


def wait_settled(win: Window, stable: float = 0.3, timeout: float = 10.0,
                 interval: float = 0.05) -> dict:
    """Block until `win` stops changing: same title, geometry and focus for `stable` s.

    Replaces a blind sleep after activating, launching, maximizing or moving a
    window: the compositor animates those (~150-250 ms), apps retitle themselves
    while loading, and input sent mid-animation can land in the wrong spot. Returns
    {settled, waited, title, geometry, active}; settled is False on timeout
    (the window kept changing), and a window that disappears raises.
    """
    started = time.monotonic()
    deadline = started + timeout
    last, since = _window_state(win), time.monotonic()
    while True:
        time.sleep(interval)
        now = time.monotonic()
        current = _window_state(win)
        if current != last:
            last, since = current, now
        settled = now - since >= stable
        if settled or now >= deadline:
            title, geometry, active = last
            return {"settled": settled, "waited": round(now - started, 2), "title": title,
                    "geometry": dict(geometry), "active": active}


def wait_for_new_window(pattern: str, known_ids: set[str], timeout: float = 30.0,
                        interval: float = 0.5, activate: bool = False) -> Window | None:
    """`wait_for_window` with a mandatory baseline (kept for existing callers)."""
    return wait_for_window(pattern, timeout=timeout, interval=interval,
                           known_ids=known_ids, activate=activate)


def current_desktop() -> int:
    if _win is not None:
        if not _win.virtual_desktops_supported():
            return 1
        hwnd = _win.foreground_window() or (_win.list_windows() or [0])[0]
        number = _win_desktop_of(hwnd) if hwnd else None
        return number or 1
    return int(_run("get_desktop"))


def set_desktop(number: int) -> None:
    if _win is not None:
        desktops = list_desktops()
        target = next((d for d in desktops if d.number == number), None)
        if target is None:
            raise DesktopError(f"there is no virtual desktop number {number}")
        _win.switch_to_desktop(target.id)
        return
    _run("set_desktop", str(number))


def num_desktops() -> int:
    if _win is not None:
        return len(list_desktops())
    return int(_run("get_num_desktops"))


# --- virtual desktops -----------------------------------------------------------------
# kdotool can switch desktops and count them, but not create or destroy them.
# KWin exposes that over D-Bus, which we drive with `gdbus` (present wherever
# GLib is, and already a dependency of a Plasma session) to stay stdlib-only.
# Windows exposes the same operations through IVirtualDesktopManager (winapi.py).

@dataclass
class Desktop:
    position: int  # 0-based, as the backend reports it
    id: str        # KWin UUID / Windows desktop GUID, the handle remove wants
    name: str

    @property
    def number(self) -> int:
        """1-based number, which is what the CLI and the user's desktop show."""
        return self.position + 1


#: `(uint32 0, 'uuid', 'name')` tuples inside the GVariant `desktops` property.
#: The `uint32` prefix only appears on the first element, hence the optional group.
_DESKTOP_RE = re.compile(
    r"\((?:uint32\s+)?(\d+),\s*'((?:[^'\\]|\\.)*)',\s*'((?:[^'\\]|\\.)*)'\)"
)

#: Where `control` remembers the names it gave to desktops on builds whose Windows
#: cannot store one (every build before Windows 11 22H2). Small, and only written
#: when `desktop create`/`rename` is actually used.
_DESKTOP_NAMES_FILE = "desktop-names.json"


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def _gdbus(method: str, *args: str) -> str:
    result = subprocess.run(
        ["gdbus", "call", "--session", "--dest", _DBUS_DEST,
         "--object-path", _DBUS_PATH, "--method", method, *args],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise DesktopError(
            (result.stderr.strip() or result.stdout.strip() or "gdbus call failed")
            + " (is this a KDE Plasma session with KWin running?)"
        )
    return result.stdout.strip()


async def _gdbus_async(method: str, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "gdbus", "call", "--session", "--dest", _DBUS_DEST,
        "--object-path", _DBUS_PATH, "--method", method, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=10)
    out = raw_out.decode().strip()
    if proc.returncode != 0:
        raise DesktopError(raw_err.decode().strip() or out or "gdbus call failed")
    return out


def _parse_desktops(raw: str) -> list[Desktop]:
    return [
        Desktop(position=int(pos), id=_unescape(uuid), name=_unescape(name))
        for pos, uuid, name in _DESKTOP_RE.findall(raw)
    ]


def virtual_desktops_available() -> bool:
    """Whether the platform can create and remove more than one virtual desktop.

    False on Windows builds with no `IVirtualDesktopManager`; the rest of the
    module then works against the single desktop Windows always has.
    """
    return _win is None or _win.virtual_desktops_supported()


def virtual_desktops_hint() -> str:
    """Why desktops cannot be created, or "" when they can."""
    if _win is None:
        return ""
    return _win.virtual_desktops_unavailable_reason() or ""


def _saved_desktop_names() -> dict[str, str]:
    from . import session as session_mod  # lazy: keeps this module importable alone

    path = session_mod.state_dir() / _DESKTOP_NAMES_FILE
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _remember_desktop_name(desktop_id: str, name: str) -> None:
    import json

    from . import session as session_mod

    names = _saved_desktop_names()
    if name:
        names[desktop_id] = name
    else:
        names.pop(desktop_id, None)
    path = session_mod.state_dir() / _DESKTOP_NAMES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(names, indent=2), encoding="utf-8")


def list_desktops() -> list[Desktop]:
    """All virtual desktops, ordered by position."""
    if _win is not None:
        return _win_list_desktops()
    raw = _gdbus("org.freedesktop.DBus.Properties.Get", _DBUS_IFACE, "desktops")
    return sorted(_parse_desktops(raw), key=lambda d: d.position)


def _win_list_desktops() -> list[Desktop]:
    if not _win.virtual_desktops_supported():
        # One desktop, always. `id` is empty because there is no handle to move a
        # window to; everything that scopes by desktop 1 keeps working.
        return [Desktop(position=0, id="", name="Desktop 1")]
    saved = _saved_desktop_names()
    desktops = []
    for position in range(_win.desktop_count()):
        desktop_id = _win.desktop_id_at(position)
        name = _win.desktop_name_at(position) or saved.get(desktop_id, "")
        desktops.append(Desktop(position=position, id=desktop_id,
                                name=name or f"Desktop {position + 1}"))
    return desktops


async def list_desktops_async() -> list[Desktop]:
    if _win is not None:
        return _win_list_desktops()
    raw = await _gdbus_async("org.freedesktop.DBus.Properties.Get", _DBUS_IFACE, "desktops")
    return sorted(_parse_desktops(raw), key=lambda d: d.position)


def find_desktop(name_or_number: str | int) -> Desktop | None:
    """Look a desktop up by exact name, or by 1-based number."""
    desktops = list_desktops()
    if isinstance(name_or_number, int) or str(name_or_number).isdigit():
        number = int(name_or_number)
        return next((d for d in desktops if d.number == number), None)
    return next((d for d in desktops if d.name == name_or_number), None)


def create_desktop(name: str, position: int | None = None) -> Desktop:
    """Create a virtual desktop named `name` (appended unless `position` is given).

    Returns the created desktop, re-read from the compositor so the caller gets its
    real position and UUID rather than what we asked for. Windows has no insert
    position: it always appends.
    """
    if _win is not None:
        if not _win.virtual_desktops_supported():
            raise DesktopError(
                virtual_desktops_hint() or "this Windows has no virtual desktops",
                hint="run agents one after another, or check the Windows build; "
                     "'control desktop list' will show the single desktop available",
            )
        desktop_id = _win.create_desktop(name)
        _remember_desktop_name(desktop_id, name)  # Windows usually cannot store it
        created = next((d for d in _win_list_desktops() if d.id == desktop_id), None)
        if created is None:  # pragma: no cover - it was just created
            raise DesktopError(f"CreateDesktop({name!r}) succeeded but the desktop is gone")
        return created
    at = len(list_desktops()) if position is None else position
    _gdbus(f"{_DBUS_IFACE}.createDesktop", str(at), name)
    created = find_desktop(name)
    if created is None:
        raise DesktopError(f"KWin accepted createDesktop({at}, {name!r}) but no such desktop exists")
    return created


def remove_desktop(name_or_number: str | int) -> None:
    """Remove a virtual desktop. Windows on it are moved by the OS, not closed."""
    target = find_desktop(name_or_number)
    if target is None:
        raise DesktopError(f"no virtual desktop matching {name_or_number!r}")
    if _win is not None:
        if not _win.virtual_desktops_supported():
            raise DesktopError("this Windows has only one virtual desktop, and it cannot "
                               "be removed", hint="nothing to clean up here")
        _win.remove_desktop(target.id)
        _remember_desktop_name(target.id, "")
        return
    _gdbus(f"{_DBUS_IFACE}.removeDesktop", target.id)


def rename_desktop(name_or_number: str | int, new_name: str) -> Desktop:
    target = find_desktop(name_or_number)
    if target is None:
        raise DesktopError(f"no virtual desktop matching {name_or_number!r}")
    if _win is not None:
        # Windows only learned to name its own virtual desktops in 11 22H2; before
        # that the name is ours, stored next to the session state.
        _remember_desktop_name(target.id, new_name)
        return Desktop(position=target.position, id=target.id, name=new_name)
    _gdbus(f"{_DBUS_IFACE}.setDesktopName", target.id, new_name)
    return Desktop(position=target.position, id=target.id, name=new_name)


def ensure_desktop(name: str) -> Desktop:
    """Get the desktop called `name`, creating it if it doesn't exist yet.

    Idempotent on purpose: re-running `session init` for an agent must land on
    the same desktop instead of piling up duplicates. The check-then-create runs
    under a cross-process lock, so two agents asking for the same new desktop at
    once get one desktop, not two.

    Where the platform cannot create desktops (a Windows build without
    `IVirtualDesktopManager`), this returns the single desktop there is instead of
    failing: scoping still resolves, it just cannot isolate. `create_desktop`
    remains a loud error, because *that* is an explicit request to create one.
    """
    with sibling_lock("desktop", timeout=15):
        existing = find_desktop(name)
        if existing is not None:
            return existing
        if not virtual_desktops_available():
            return list_desktops()[0]
        return create_desktop(name)


def resolve_desktop(ref: str | int, create: bool = False) -> Desktop:
    """Turn a `--desktop` value (1-based number or name) into a Desktop.

    Numbers must exist. A name that doesn't exist yet is created when `create`
    is set -- that's how `app launch --desktop agent_7` provisions a workspace
    on the fly -- and is an error otherwise, so read-only queries never create
    desktops as a side effect.
    """
    ref = str(ref).strip()
    if not ref:
        raise DesktopError("empty desktop name")
    if ref.isdigit():
        found = find_desktop(int(ref))
        if found is None:
            raise DesktopError(f"no virtual desktop number {ref} (there are {len(list_desktops())})")
        return found
    if create:
        return ensure_desktop(ref)
    found = find_desktop(ref)
    if found is None:
        raise DesktopError(f"no virtual desktop named {ref!r}")
    return found
