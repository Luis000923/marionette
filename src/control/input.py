"""Mouse and keyboard simulation for any Wayland compositor, via ydotool (uinput-based).

`ydotoold` owns one global kernel `uinput` device, so concurrent agents would
interleave their keystrokes and clicks. Every call here goes through the
`input_mutex` (see locking.py), which serializes input across processes; the
mutex is reentrant so composite gestures hold it for their whole sequence.
"""
from __future__ import annotations

import subprocess
from contextlib import contextmanager
from typing import Iterator

from .locking import FileMutex, LockTimeout  # noqa: F401 - LockTimeout re-exported for callers

#: Process-wide handle on the global input lock.
input_mutex = FileMutex()


@contextmanager
def input_lock(timeout: float | None = None) -> Iterator[FileMutex]:
    """Hold the input mutex across several actions, so no other agent can cut in.

    >>> with input_lock():
    ...     click(100, 200)
    ...     type_text("hello")
    """
    with input_mutex.acquired(timeout):
        yield input_mutex


# Linux input-event-codes.h keycodes for the keys we care about.
# Extend this table as needed: https://github.com/torvalds/linux/blob/master/include/uapi/linux/input-event-codes.h
KEYCODES: dict[str, int] = {
    "esc": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "minus": 12, "equal": 13, "backspace": 14, "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
    "leftbrace": 26, "rightbrace": 27, "enter": 28, "ctrl": 29,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    "semicolon": 39, "apostrophe": 40, "grave": 41, "shift": 42, "backslash": 43,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
    "comma": 51, "dot": 52, "slash": 53, "rightshift": 54, "kpasterisk": 55,
    "alt": 56, "space": 57, "capslock": 58,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65, "f8": 66,
    "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "up": 103, "left": 105, "right": 106, "down": 108,
    "home": 102, "end": 107, "pageup": 104, "pagedown": 109, "insert": 110, "delete": 111,
    "rightctrl": 97, "rightalt": 100, "super": 125, "meta": 125,
    # Numpad
    "kp0": 82, "kp1": 79, "kp2": 80, "kp3": 81, "kp4": 75, "kp5": 76, "kp6": 77,
    "kp7": 71, "kp8": 72, "kp9": 73, "kpdot": 83, "kpminus": 74, "kpplus": 78,
    "kpenter": 96, "kpslash": 98, "numlock": 69,
    # Misc / system keys
    "sysrq": 99, "printscreen": 99, "scrolllock": 70, "pause": 119, "menu": 127,
    "leftbracket": 26, "rightbracket": 27,
}

# ydotool click byte = mode nibble | button nibble.
# Mode: 0xC=click (down+up), 0x4=down only, 0x8=up only.
_BUTTON_NIBBLE = {"left": 0x0, "right": 0x1, "middle": 0x2}


def _button_code(button: str, mode: str) -> str:
    prefix = {"click": 0xC0, "down": 0x40, "up": 0x80}[mode]
    return hex(prefix | _BUTTON_NIBBLE[button])


class YdotoolError(RuntimeError):
    pass


def _run(*args: str) -> None:
    with input_mutex:
        result = subprocess.run(["ydotool", *args], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise YdotoolError(
            f"ydotool {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}\n"
            "Is ydotoold running? Try: systemctl status ydotoold"
        )


def move_mouse(x: int, y: int) -> None:
    _run("mousemove", "--absolute", "-x", str(x), "-y", str(y))


def move_mouse_relative(dx: int, dy: int) -> None:
    _run("mousemove", "-x", str(dx), "-y", str(dy))


def click(button: str = "left", x: int | None = None, y: int | None = None) -> None:
    with input_mutex:
        if x is not None and y is not None:
            move_mouse(x, y)
        _run("click", _button_code(button, "click"))


def double_click(button: str = "left", x: int | None = None, y: int | None = None) -> None:
    with input_mutex:
        click(button, x, y)
        click(button)


def drag(x1: int, y1: int, x2: int, y2: int, button: str = "left") -> None:
    # The button must stay down for the whole gesture: never let another agent
    # in between the press and the release.
    with input_mutex:
        move_mouse(x1, y1)
        _run("click", _button_code(button, "down"))
        move_mouse(x2, y2)
        _run("click", _button_code(button, "up"))


def scroll(amount: int) -> None:
    """Positive scrolls up, negative scrolls down."""
    _run("mousemove", "-w", "-x", "0", "-y", str(-amount))


def type_text(text: str, delay_ms: int = 0) -> None:
    args = ["type"]
    if delay_ms:
        args += ["--key-delay", str(delay_ms)]
    args.append(text)
    _run(*args)


def key_combo(*keys: str) -> None:
    """Press keys together, e.g. key_combo('ctrl', 'shift', 't')."""
    codes = [KEYCODES[k.lower()] for k in keys]
    down = [f"{c}:1" for c in codes]
    up = [f"{c}:0" for c in reversed(codes)]
    _run("key", *down, *up)


def press_key(name: str) -> None:
    key_combo(name)
