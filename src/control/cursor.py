"""Marionette's own pointer: a visible, click-through arrow the agent moves around.

Wayland has a single real pointer, so a truly separate one is impossible. This
is the next best thing: an overlay arrow (see `_cursor_overlay.py`) that marks
where the agent is working, leaving the user's pointer alone. `click` borrows
the real pointer only for the instant of the click, then puts it back.

State (overlay pid, window id, position) lives in a small JSON file under
$XDG_RUNTIME_DIR, so separate CLI invocations share one cursor.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import input as inp

TITLE = "Marionette Cursor"
#: Arrow tip offset inside the overlay window (see _cursor_overlay.py).
HOTSPOT = (2, 2)
DEFAULT_MOVE_SECONDS = 0.25
_STEP_SECONDS = 0.015
_OVERLAY = Path(__file__).with_name("_cursor_overlay.py")


class CursorError(RuntimeError):
    pass


def _state_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/marionette-{os.getuid()}"
    return Path(base) / "marionette-cursor.json"


def _load() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kdotool(*args: str) -> str:
    tool = shutil.which("kdotool")
    if tool is None:
        raise CursorError("kdotool not found; the cursor overlay is moved with it")
    result = subprocess.run([tool, *args], capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise CursorError(f"kdotool {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _overlay_python() -> str:
    """A python that can import PySide6: CONTROL_CURSOR_PYTHON, else the system one."""
    for candidate in (os.environ.get("CONTROL_CURSOR_PYTHON"), "/usr/bin/python3", sys.executable):
        if not candidate or not Path(candidate).exists():
            continue
        probe = subprocess.run([candidate, "-c", "import PySide6.QtWidgets"], capture_output=True)
        if probe.returncode == 0:
            return candidate
    raise CursorError("no python with PySide6 found for the cursor overlay; "
                      "install pyside6 or set CONTROL_CURSOR_PYTHON")


def status() -> dict:
    state = _load()
    running = _alive(state.get("pid"))
    return {"visible": running, "x": state.get("x") if running else None,
            "y": state.get("y") if running else None}


def show(color: str = "#d97757", timeout: float = 5.0) -> dict:
    """Start the overlay if it is not running. Returns `status()`."""
    state = _load()
    if _alive(state.get("pid")) and state.get("wid"):
        return status()
    env = {**os.environ, "QT_QPA_PLATFORM": "wayland"}
    proc = subprocess.Popen([_overlay_python(), str(_OVERLAY), color], env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    wid = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not wid:
        if proc.poll() is not None:
            raise CursorError("cursor overlay exited at startup (is PySide6 working on this session?)")
        try:
            wid = _kdotool("search", "--name", f"^{TITLE}$").splitlines()[0]
        except (CursorError, IndexError):
            time.sleep(0.1)
    if not wid:
        proc.terminate()
        raise CursorError(f"cursor overlay window did not appear within {timeout}s")
    _kdotool("windowstate", "--add", "ABOVE", wid)
    pos = inp.pointer_position() or (0, 0)
    _save({"pid": proc.pid, "wid": wid, "x": pos[0], "y": pos[1]})
    _place(wid, *pos)
    return status()


def hide() -> dict:
    state = _load()
    pid = state.get("pid")
    if _alive(pid):
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    _state_path().unlink(missing_ok=True)
    return status()


def _place(wid: str, x: int, y: int) -> None:
    _kdotool("windowmove", wid, str(x - HOTSPOT[0]), str(y - HOTSPOT[1]))


def move(x: int, y: int, duration: float = DEFAULT_MOVE_SECONDS) -> dict:
    """Glide the agent's pointer to (x, y) in compositor coordinates."""
    show()
    state = _load()
    wid, x0, y0 = state["wid"], state.get("x", x), state.get("y", y)
    steps = max(1, min(40, int(duration / _STEP_SECONDS))) if duration > 0 else 1
    for i in range(1, steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)  # smoothstep
        _place(wid, round(x0 + (x - x0) * t), round(y0 + (y - y0) * t))
        if steps > 1:
            time.sleep(_STEP_SECONDS)
    _save({**state, "x": x, "y": y})
    return status()


def click(button: str = "left", x: int | None = None, y: int | None = None,
          restore: bool = True, duration: float = DEFAULT_MOVE_SECONDS) -> dict:
    """Click where the agent's pointer is (moving it first if x,y are given).

    The real pointer jumps there for the click and returns to where the user had
    it, unless `restore` is False.
    """
    if x is not None and y is not None:
        move(x, y, duration)
    else:
        show()
    state = _load()
    tx, ty = state["x"], state["y"]
    with inp.input_lock():
        before = inp.pointer_position() if restore else None
        inp.click(button, tx, ty)
        if before is not None and before != (tx, ty):
            inp.move_mouse(*before)
    return {**status(), "button": button, "restored": before is not None}
