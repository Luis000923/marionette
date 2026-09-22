"""Window management on KDE Plasma / Wayland, via kdotool (KWin scripting under the hood).

Virtual desktops are the isolation unit for parallel agents: each agent gets its
own KWin desktop and every window command can be scoped to it with `desktop=N`,
so `win list`/`win close` in agent 1 never see or touch agent 2's windows.

Two numbering schemes meet here, and mixing them up silently targets the wrong
desktop:

* kdotool (and everything in this module's public API) uses **1-based** desktop
  numbers, matching what Plasma shows the user;
* KWin's `org.kde.KWin.VirtualDesktopManager` D-Bus API, which we need for
  *creating* and *removing* desktops (kdotool has no such command), uses
  **0-based** positions plus an opaque UUID.

`Desktop` carries both: `number` (1-based) and `position` (0-based).
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import time
from dataclasses import dataclass

from .locking import sibling_lock

#: Max concurrent kdotool processes for async queries; kdotool loads a KWin
#: script per invocation, so unbounded fan-out would hammer the compositor.
MAX_CONCURRENT_QUERIES = 8

_DBUS_DEST = "org.kde.KWin"
_DBUS_PATH = "/VirtualDesktopManager"
_DBUS_IFACE = "org.kde.KWin.VirtualDesktopManager"


class KdotoolError(RuntimeError):
    pass


class DesktopError(RuntimeError):
    """A KWin virtual-desktop operation (create/remove/lookup) failed."""


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

    def activate(self) -> None:
        _run("windowactivate", self.id)

    def focus(self, timeout: float = 1.0, interval: float = 0.05) -> bool:
        """Activate and wait until KWin really reports this window as active.

        `windowactivate` returns before the compositor has moved focus; typing
        straight after it can still land in the previous window. Returns False
        if focus didn't settle within `timeout` (e.g. focus-stealing prevention).
        """
        self.activate()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if _run("getactivewindow") == self.id:
                    return True
            except KdotoolError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "class": self.classname,
                "desktop": self.desktop}

    def raise_(self) -> None:
        _run("windowraise", self.id)

    def close(self) -> None:
        _run("windowclose", self.id)

    def minimize(self) -> None:
        _run("windowminimize", self.id)

    def move(self, x: int | str, y: int | str, relative: bool = False) -> None:
        args = ["windowmove"]
        if relative:
            args.append("--relative")
        args += [self.id, str(x), str(y)]
        _run(*args)

    def resize(self, width: int | str, height: int | str) -> None:
        _run("windowsize", self.id, str(width), str(height))

    def geometry(self) -> dict[str, int]:
        """{x, y, width, height} in screen coordinates (empty if kdotool says nothing).

        kdotool prints "Position: X,Y" and "Geometry: WxH" on separate lines.
        """
        out = _run("getwindowgeometry", self.id)
        geo: dict[str, int] = {}
        if m := re.search(r"Position:\s*(-?\d+)\s*,\s*(-?\d+)", out):
            geo["x"], geo["y"] = int(m.group(1)), int(m.group(2))
        if m := re.search(r"Geometry:\s*(\d+)\s*x\s*(\d+)", out):
            geo["width"], geo["height"] = int(m.group(1)), int(m.group(2))
        return geo

    def set_state(self, add: list[str] | None = None, remove: list[str] | None = None,
                  toggle: list[str] | None = None) -> None:
        args = ["windowstate"]
        for prop in add or []:
            args += ["--add", prop]
        for prop in remove or []:
            args += ["--remove", prop]
        for prop in toggle or []:
            args += ["--toggle", prop]
        args.append(self.id)
        _run(*args)

    def maximize(self) -> None:
        self.set_state(add=["MAXIMIZED"])

    def fullscreen(self) -> None:
        self.set_state(add=["FULLSCREEN"])

    def get_desktop(self) -> int | None:
        """1-based number of the desktop this window lives on.

        None when KWin reports no desktop -- windows pinned to *all* desktops
        (and some special surfaces) have no single home.
        """
        self.desktop = _parse_desktop_number(_run("get_desktop_for_window", self.id))
        return self.desktop

    def move_to_desktop(self, number: int | str) -> None:
        """Send this window to another desktop (1-based, or "current_desktop"/"all").

        This is how an agent claims a window it just launched: pull it onto the
        agent's own desktop so it stops sharing screen space with other agents.
        """
        _run("set_desktop_for_window", self.id, str(number))
        self.desktop = number if isinstance(number, int) else None


def active_window() -> Window:
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


def search(pattern: str, by: str = "any", limit: int = 0,
           desktop: int | None = None) -> list[Window]:
    """Search windows by title/class/classname/role regex. Returns matches with metadata.

    `desktop` (1-based) restricts the search to a single virtual desktop, which
    is what keeps one agent's `win list` blind to another agent's windows.
    """
    return [_describe(win_id, desktop) for win_id in window_ids(pattern, limit, desktop)]


def window_ids(pattern: str, limit: int = 0, desktop: int | None = None) -> list[str]:
    """Ids of matching windows only -- one kdotool call, no per-window lookups.

    Cheap enough to take as a *baseline* right before launching something.
    """
    out = _run(*_search_args(pattern, limit, desktop))
    return [i for i in out.splitlines() if i.strip()]


def _describe(win_id: str, desktop: int | None = None) -> Window:
    try:
        title = _run("getwindowname", win_id)
        classname = _run("getwindowclassname", win_id)
    except KdotoolError:
        title, classname = "", ""
    return Window(id=win_id, title=title, classname=classname, desktop=desktop)


async def search_async(pattern: str, limit: int = 0,
                       desktop: int | None = None) -> list[Window]:
    """Non-blocking `search`, fetching every window's metadata concurrently.

    A plain `search` costs 1 + 2N sequential kdotool round-trips; this issues the
    per-window lookups in parallel (bounded by MAX_CONCURRENT_QUERIES) and never
    touches the input lock, so status queries stay responsive while another
    agent is typing.
    """
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
    return asyncio.run(search_async(pattern, limit=limit, desktop=desktop))


def find_one(pattern: str, desktop: int | None = None) -> Window | None:
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


def wait_for_new_window(pattern: str, known_ids: set[str], timeout: float = 30.0,
                        interval: float = 0.5, activate: bool = False) -> Window | None:
    """`wait_for_window` with a mandatory baseline (kept for existing callers)."""
    return wait_for_window(pattern, timeout=timeout, interval=interval,
                           known_ids=known_ids, activate=activate)


def current_desktop() -> int:
    return int(_run("get_desktop"))


def set_desktop(number: int) -> None:
    _run("set_desktop", str(number))


def num_desktops() -> int:
    return int(_run("get_num_desktops"))


# --- virtual desktops -------------------------------------------------------
# kdotool can switch desktops and count them, but not create or destroy them.
# KWin exposes that over D-Bus, which we drive with `gdbus` (present wherever
# GLib is, and already a dependency of a Plasma session) to stay stdlib-only.

@dataclass
class Desktop:
    position: int  # 0-based, as KWin's D-Bus API reports it
    id: str        # KWin UUID, the handle removeDesktop/setDesktopName want
    name: str

    @property
    def number(self) -> int:
        """1-based number, which is what kdotool and Plasma's UI use."""
        return self.position + 1


#: `(uint32 0, 'uuid', 'name')` tuples inside the GVariant `desktops` property.
#: The `uint32` prefix only appears on the first element, hence the optional group.
_DESKTOP_RE = re.compile(
    r"\((?:uint32\s+)?(\d+),\s*'((?:[^'\\]|\\.)*)',\s*'((?:[^'\\]|\\.)*)'\)"
)


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


def list_desktops() -> list[Desktop]:
    """All KWin virtual desktops, ordered by position."""
    raw = _gdbus("org.freedesktop.DBus.Properties.Get", _DBUS_IFACE, "desktops")
    return sorted(_parse_desktops(raw), key=lambda d: d.position)


async def list_desktops_async() -> list[Desktop]:
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

    Returns the created desktop, re-read from KWin so the caller gets its real
    position and UUID rather than what we asked for.
    """
    at = len(list_desktops()) if position is None else position
    _gdbus(f"{_DBUS_IFACE}.createDesktop", str(at), name)
    created = find_desktop(name)
    if created is None:
        raise DesktopError(f"KWin accepted createDesktop({at}, {name!r}) but no such desktop exists")
    return created


def remove_desktop(name_or_number: str | int) -> None:
    """Remove a virtual desktop. Windows on it are moved by KWin, not closed."""
    target = find_desktop(name_or_number)
    if target is None:
        raise DesktopError(f"no virtual desktop matching {name_or_number!r}")
    _gdbus(f"{_DBUS_IFACE}.removeDesktop", target.id)


def rename_desktop(name_or_number: str | int, new_name: str) -> Desktop:
    target = find_desktop(name_or_number)
    if target is None:
        raise DesktopError(f"no virtual desktop matching {name_or_number!r}")
    _gdbus(f"{_DBUS_IFACE}.setDesktopName", target.id, new_name)
    return Desktop(position=target.position, id=target.id, name=new_name)


def ensure_desktop(name: str) -> Desktop:
    """Get the desktop called `name`, creating it if it doesn't exist yet.

    Idempotent on purpose: re-running `session init` for an agent must land on
    the same desktop instead of piling up duplicates. The check-then-create runs
    under a cross-process lock, so two agents asking for the same new desktop at
    once get one desktop, not two.
    """
    with sibling_lock("desktop", timeout=15):
        existing = find_desktop(name)
        return existing if existing is not None else create_desktop(name)


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
