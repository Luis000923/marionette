"""Window management on KDE Plasma / Wayland, via kdotool (KWin scripting under the hood)."""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass


class KdotoolError(RuntimeError):
    pass


def _run(*args: str) -> str:
    result = subprocess.run(
        ["kdotool", *args], capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        raise KdotoolError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


@dataclass
class Window:
    id: str
    title: str = ""
    classname: str = ""

    def activate(self) -> None:
        _run("windowactivate", self.id)

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
        out = _run("getwindowgeometry", self.id)
        geo: dict[str, int] = {}
        for part in out.replace(",", " ").split():
            if ":" in part:
                k, v = part.split(":", 1)
                try:
                    geo[k.strip().lower()] = int(v.strip())
                except ValueError:
                    pass
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


def active_window() -> Window:
    win_id = _run("getactivewindow", "getwindowid")
    title = _run("getwindowname", win_id)
    classname = _run("getwindowclassname", win_id)
    return Window(id=win_id, title=title, classname=classname)


def search(pattern: str, by: str = "any", limit: int = 0) -> list[Window]:
    """Search windows by title/class/classname/role regex. Returns matches with metadata."""
    args = ["search"]
    if limit:
        args += ["--limit", str(limit)]
    args.append(pattern)
    ids_out = _run(*args)
    ids = [i for i in ids_out.splitlines() if i.strip()]
    windows = []
    for win_id in ids:
        try:
            title = _run("getwindowname", win_id)
            classname = _run("getwindowclassname", win_id)
        except KdotoolError:
            title, classname = "", ""
        windows.append(Window(id=win_id, title=title, classname=classname))
    return windows


def find_one(pattern: str) -> Window | None:
    matches = search(pattern, limit=1)
    return matches[0] if matches else None


def wait_for_window(pattern: str, timeout: float = 30.0, interval: float = 0.5) -> Window | None:
    """Poll `search` until a window matching `pattern` appears, or `timeout` seconds pass.

    Useful after launching a heavy app (video editor, IDE, office suite) instead of a
    blind fixed sleep. Returns None on timeout rather than raising.
    """
    deadline = time.monotonic() + timeout
    while True:
        win = find_one(pattern)
        if win is not None:
            return win
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def current_desktop() -> int:
    return int(_run("get_desktop"))


def set_desktop(number: int) -> None:
    _run("set_desktop", str(number))


def num_desktops() -> int:
    return int(_run("get_num_desktops"))
