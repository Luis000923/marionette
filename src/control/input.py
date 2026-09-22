"""Mouse and keyboard simulation for any Wayland compositor, via ydotool (uinput-based).

`ydotoold` owns one global kernel `uinput` device, so concurrent agents would
interleave their keystrokes and clicks. Every call here goes through the
`input_mutex` (see locking.py), which serializes input across processes; the
mutex is reentrant so composite gestures hold it for their whole sequence.
"""
from __future__ import annotations

import re
import shutil
import string
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

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

#: Other names agents (and xdotool/X11 keysyms) use for the same keys. Matched
#: case-insensitively, like the canonical names.
KEY_ALIASES: dict[str, str] = {
    "return": "enter", "ret": "enter", "escape": "esc", "control": "ctrl", "lctrl": "ctrl",
    "leftctrl": "ctrl", "control_l": "ctrl", "control_r": "rightctrl", "rctrl": "rightctrl",
    "lshift": "shift", "leftshift": "shift", "shift_l": "shift", "rshift": "rightshift",
    "shift_r": "rightshift", "lalt": "alt", "leftalt": "alt", "alt_l": "alt", "option": "alt",
    "ralt": "rightalt", "alt_r": "rightalt", "altgr": "rightalt", "win": "super",
    "windows": "super", "logo": "super", "cmd": "super", "super_l": "super", "del": "delete",
    "ins": "insert", "pgup": "pageup", "page_up": "pageup", "prior": "pageup", "pgdn": "pagedown",
    "pgdown": "pagedown", "page_down": "pagedown", "next": "pagedown", "bksp": "backspace",
    "back": "backspace", "spacebar": "space", "caps": "capslock", "caps_lock": "capslock",
    "print": "sysrq", "prtsc": "sysrq", "arrowup": "up", "arrowdown": "down",
    "arrowleft": "left", "arrowright": "right", "period": "dot", "-": "minus", "=": "equal",
    ",": "comma", ".": "dot", "/": "slash", ";": "semicolon", "'": "apostrophe", "`": "grave",
    "[": "leftbrace", "]": "rightbrace", "\\": "backslash", "menu_key": "menu", "apps": "menu",
}

#: How `control key --list` groups KEYCODES.
KEY_GROUPS: dict[str, tuple[str, ...]] = {
    "modifiers": ("ctrl", "rightctrl", "shift", "rightshift", "alt", "rightalt", "super", "meta"),
    "editing": ("enter", "tab", "space", "backspace", "delete", "insert", "esc"),
    "navigation": ("up", "down", "left", "right", "home", "end", "pageup", "pagedown"),
    "function": tuple(f"f{i}" for i in range(1, 13)),
    "letters": tuple("abcdefghijklmnopqrstuvwxyz"),
    "digits": tuple("0123456789"),
    "punctuation (US key positions)": ("minus", "equal", "leftbrace", "rightbrace",
                                       "leftbracket", "rightbracket", "backslash", "semicolon",
                                       "apostrophe", "grave", "comma", "dot", "slash"),
    "numpad": ("kp0", "kp1", "kp2", "kp3", "kp4", "kp5", "kp6", "kp7", "kp8", "kp9", "kpdot",
               "kpminus", "kpplus", "kpasterisk", "kpslash", "kpenter", "numlock"),
    "system": ("sysrq", "printscreen", "scrolllock", "pause", "menu", "capslock"),
}


def resolve_key(name: str) -> str | None:
    """Canonical KEYCODES name for `name` or one of its aliases (None if unknown)."""
    key = name.lower()
    if key in KEYCODES:
        return key
    return KEY_ALIASES.get(key)


# ydotool click byte = mode nibble | button nibble.
# Mode: 0xC=click (down+up), 0x4=down only, 0x8=up only.
_BUTTON_NIBBLE = {"left": 0x0, "right": 0x1, "middle": 0x2}


def _button_code(button: str, mode: str) -> str:
    prefix = {"click": 0xC0, "down": 0x40, "up": 0x80}[mode]
    return hex(prefix | _BUTTON_NIBBLE[button])


class YdotoolError(RuntimeError):
    pass


def _argv(args: list[str]) -> list[str | bytes]:
    """Command-line arguments as UTF-8, even under a C/Latin-1 locale.

    subprocess encodes str arguments with the filesystem encoding, which under
    LANG=C is ASCII: "señor" would raise UnicodeEncodeError before ydotool ran.
    """
    if (sys.getfilesystemencoding() or "").lower().replace("-", "") == "utf8":
        return list(args)
    return [a.encode("utf-8") for a in args]


def _run(*args: str) -> None:
    with input_mutex:
        result = subprocess.run(_argv(["ydotool", *args]), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=15)
    if result.returncode != 0:
        raise YdotoolError(
            f"ydotool {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}\n"
            "Is ydotoold running? Try: systemctl status ydotoold"
        )


#: Closed-loop pointer placement: at most this many corrective moves.
MAX_POINTER_CORRECTIONS = 25
#: Relative moves up to this size land ~1:1; bigger ones get accelerated by
#: libinput (up to ~2x), so only half the remaining distance is sent at once.
_EXACT_STEP = 16


def pointer_position() -> tuple[int, int] | None:
    """Where the compositor has the pointer (via kdotool), or None if unknown."""
    tool = shutil.which("kdotool")
    if tool is None:
        return None
    try:
        out = subprocess.run([tool, "getmouselocation"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"x:(-?\d+)\s+y:(-?\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _damped(delta: int) -> int:
    return delta if abs(delta) <= _EXACT_STEP else round(delta / 2)


def _steer(x: int, y: int) -> bool:
    """Walk the pointer onto (x, y) with relative moves, reading its real position
    back after each one. False when no position feedback is available."""
    pos = None
    for _ in range(MAX_POINTER_CORRECTIONS):
        pos = pointer_position()
        if pos is None:
            return False
        dx, dy = x - pos[0], y - pos[1]
        if not dx and not dy:
            return True
        _run("mousemove", "-x", str(_damped(dx)), "-y", str(_damped(dy)))
    raise YdotoolError(f"the pointer did not reach {x},{y} (it is at {pos[0]},{pos[1]}); "
                       "are the coordinates on screen?")


def move_mouse(x: int, y: int) -> None:
    """Put the pointer exactly on (x, y), in compositor (logical) coordinates.

    ydotool 1.0's `--absolute` is unreliable on Wayland: it slams the pointer to
    0,0 and the follow-up move is lost or scaled by pointer acceleration, so the
    pointer typically ends up at 1,1 whatever was asked. The absolute move is
    kept as a starting point, then corrected in a closed loop with kdotool's
    real pointer position (3-4 moves, ~40 ms). Without kdotool the absolute
    move is all there is.
    """
    with input_mutex:
        _run("mousemove", "--absolute", "-x", str(x), "-y", str(y))
        _steer(x, y)


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


#: Indirection so tests can run gestures without real pauses.
_sleep = time.sleep

DEFAULT_DRAG_STEPS = 20
DEFAULT_DRAG_HOLD_MS = 150


def drag(x1: int, y1: int, x2: int, y2: int, button: str = "left",
         steps: int = DEFAULT_DRAG_STEPS, hold_ms: int = DEFAULT_DRAG_HOLD_MS) -> None:
    """Press at (x1, y1), glide to (x2, y2) in `steps` moves, release.

    A single jump from press to release is too fast for real drag-and-drop:
    file managers only start a drag after the pointer moved with the button
    down, and web drop zones only accept a drop after `dragenter`/`dragover`,
    which need intermediate motion. So the pointer moves in steps, pausing
    `hold_ms` after the press and again over the target before letting go.

    The button must stay down for the whole gesture -- no other agent may cut
    in between press and release -- and must never stay down afterwards: if a
    move fails midway, the release is still sent.
    """
    steps = max(1, steps)
    pause = max(0, hold_ms) / 1000
    with input_mutex:
        move_mouse(x1, y1)
        _run("click", _button_code(button, "down"))
        try:
            _sleep(pause)
            for i in range(1, steps + 1):
                x, y = round(x1 + (x2 - x1) * i / steps), round(y1 + (y2 - y1) * i / steps)
                # Relative moves only while the button is down: an absolute move
                # would first drag whatever we hold to the screen's corner.
                if not _steer(x, y):
                    _run("mousemove", "--absolute", "-x", str(x), "-y", str(y))
                if steps > 1:
                    _sleep(pause / steps)
            _sleep(pause)
        finally:
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
    codes = []
    for k in keys:
        canonical = resolve_key(k)
        if canonical is None:
            raise KeyError(k)
        codes.append(KEYCODES[canonical])
    down = [f"{c}:1" for c in codes]
    up = [f"{c}:0" for c in reversed(codes)]
    _run("key", *down, *up)


def press_key(name: str) -> None:
    key_combo(name)


# --- Wayland clipboard (wl-clipboard) -------------------------------------------
# Copying a file's URI and pasting it with Ctrl+V skips a GTK/KDE file dialog
# entirely: Chromium accepts pasted files from `text/uri-list`, and so do file
# managers and most upload drop zones that listen to `paste`.

class ClipboardError(RuntimeError):
    pass


def _wl(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise ClipboardError(
            f"{tool} not found; install wl-clipboard (e.g. sudo pacman -S wl-clipboard)"
        )
    return path


def clipboard_available() -> bool:
    return shutil.which("wl-copy") is not None


def copy_to_clipboard(data: str | bytes, mime: str | None = None) -> None:
    """Put `data` on the Wayland clipboard (optionally announced as `mime`).

    wl-copy forks a child that keeps serving the clipboard until something
    else is copied. That child inherits our stdio, so waiting on a pipe would
    hang until the next copy: stdout goes to /dev/null and stderr to a file.
    """
    args = [_wl("wl-copy")]
    if mime:
        args += ["--type", mime]
    # Always UTF-8, whatever the locale says: that is what text/plain means on
    # Wayland, and what every toolkit decodes a paste as.
    payload = data.encode("utf-8") if isinstance(data, str) else data
    with tempfile.TemporaryFile() as err:
        result = subprocess.run(args, input=payload, stdout=subprocess.DEVNULL,
                                stderr=err, timeout=15)
        if result.returncode != 0:
            err.seek(0)
            raise ClipboardError(f"wl-copy failed: {err.read().decode(errors='replace').strip()}")


def _existing(paths: Sequence[str | Path]) -> list[Path]:
    resolved = [Path(raw).expanduser().resolve() for raw in paths]
    missing = [str(p) for p in resolved if not p.exists()]
    if missing:
        raise ClipboardError(f"no such file: {', '.join(missing)}")
    return resolved


def file_uri_list(paths: Sequence[str | Path]) -> str:
    """`text/uri-list` body for local files (absolute, percent-encoded, CRLF-separated)."""
    return "".join(f"{p.as_uri()}\r\n" for p in _existing(paths))


def copy_files(paths: Sequence[str | Path]) -> list[str]:
    """Copy files the way a file manager does, ready for Ctrl+V into a page.
    Returns the absolute paths that were put on the clipboard."""
    copy_to_clipboard(file_uri_list(paths), mime="text/uri-list")
    return [str(p) for p in _existing(paths)]


def _wl_paste(*args: str) -> bytes | None:
    """Raw wl-paste output; None when the clipboard is empty."""
    result = subprocess.run([_wl("wl-paste"), *args], capture_output=True, timeout=15)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        if "No selection" in message or "Nothing is copied" in message:
            return None
        raise ClipboardError(f"wl-paste failed: {message}")
    return result.stdout


def read_clipboard(mime: str | None = None) -> str:
    # Bytes in, decoded as UTF-8 here: text=True would use the locale's
    # encoding, and a Latin-1 or C locale turns "ó" into "Ã³" or worse.
    args = ["--no-newline"] + (["--type", mime] if mime else [])
    data = _wl_paste(*args)
    return "" if data is None else data.decode("utf-8", errors="replace")


#: Text flavours worth saving before a paste and putting back afterwards, best first.
_RESTORABLE = ("text/uri-list", "text/plain;charset=utf-8", "text/plain", "UTF8_STRING")


def _save_clipboard() -> tuple[str, bytes] | None:
    """(mime, bytes) of the current clipboard; ("", b"") when it is empty;
    None when it holds something that can't be put back (an image...)."""
    try:
        listed = _wl_paste("--list-types")
    except ClipboardError:
        return None
    if listed is None:
        return ("", b"")
    types = listed.decode("utf-8", errors="replace").split()
    for mime in _RESTORABLE:
        if mime in types:
            data = _wl_paste("--no-newline", "--type", mime)
            return (mime, data or b"")
    return None


def clear_clipboard() -> None:
    subprocess.run([_wl("wl-copy"), "--clear"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=15)


DEFAULT_PASTE_KEYS = ("ctrl", "v")
#: Terminals take Ctrl+V as "insert next character literally", not paste.
TERMINAL_PASTE_KEYS = ("ctrl", "shift", "v")
TERMINAL_CLASSES = ("kitty", "konsole", "alacritty", "foot", "wezterm", "xterm", "terminal",
                    "yakuake", "tilix", "ghostty")
#: How long the target app gets to read a paste before the clipboard is put back:
#: Wayland apps fetch the data asynchronously after the key press.
PASTE_SETTLE = 0.3


def paste_keys_for(window_class: str | None) -> tuple[str, ...]:
    name = (window_class or "").lower()
    return TERMINAL_PASTE_KEYS if any(t in name for t in TERMINAL_CLASSES) else DEFAULT_PASTE_KEYS


def paste(keys: Sequence[str] = DEFAULT_PASTE_KEYS) -> None:
    """Press the paste shortcut (Ctrl+V by default) in whatever has focus."""
    key_combo(*keys)


def paste_text(text: str, keys: Sequence[str] = DEFAULT_PASTE_KEYS, restore: bool = True) -> bool:
    """Enter `text` in the focused widget through the clipboard, byte-exact UTF-8.

    With `restore`, whatever text/file list was on the clipboard before is put
    back afterwards (an empty clipboard is cleared again). Returns whether the
    previous contents were restored; False when they couldn't be (e.g. an image).
    Note that a clipboard manager (Klipper) may still record the pasted text in
    its history: don't paste secrets this way.
    """
    with input_mutex:
        saved = _save_clipboard() if restore else None
        copy_to_clipboard(text)
        key_combo(*keys)
        _sleep(PASTE_SETTLE)
        if saved is None:
            return False
        mime, data = saved
        if mime:
            copy_to_clipboard(data, mime=mime)
        else:
            clear_clipboard()
        return True


# --- choosing between typing and pasting ----------------------------------------------

#: What `ydotool type` gets right on any common Latin layout. It sends US-layout
#: keycodes for ASCII and silently drops everything else, so on a Spanish layout
#: "presentación a:b/c" arrives as "presentacin aÑb-c". Only letters, digits,
#: whitespace, comma and period sit on the same keys everywhere that matters.
_LAYOUT_SAFE = frozenset(string.ascii_letters + string.digits + " \t\n,.")
TEXT_METHODS = ("auto", "paste", "type")


def typeable(text: str) -> bool:
    """True when `ydotool type` reproduces `text` exactly, whatever the layout."""
    return all(ch in _LAYOUT_SAFE for ch in text)


def _sample(chars) -> str:
    unique = sorted(set(chars))
    return " ".join(unique[:12]) + (" ..." if len(unique) > 12 else "")


def choose_text_method(text: str, method: str = "auto") -> tuple[str, str | None]:
    """Decide how to enter `text`: ("type" | "paste", warning or None).

    "auto" types plain text (fast path, clipboard untouched) and pastes anything
    with accents, ñ, symbols or other Unicode. Without wl-clipboard, non-ASCII
    text is an error rather than silently losing characters; ASCII symbols are
    typed with a warning, since they are right on a US layout.
    """
    if method not in TEXT_METHODS:
        raise ValueError(f"unknown text method {method!r}")
    non_ascii = [ch for ch in text if not ch.isascii()]
    if method == "type":
        warning = (f"ydotool cannot type these and drops them: {_sample(non_ascii)}"
                   if non_ascii else None)
        return "type", warning
    if method == "paste" or not typeable(text):
        if clipboard_available():
            return "paste", None
        if method == "paste" or non_ascii:
            raise ClipboardError(
                f"the text has characters ydotool cannot type ({_sample(non_ascii)}) and "
                "wl-copy is missing to paste them; install wl-clipboard "
                "(sudo pacman -S wl-clipboard)" if non_ascii else
                "wl-copy is missing; install wl-clipboard (sudo pacman -S wl-clipboard)"
            )
        odd = [ch for ch in text if ch not in _LAYOUT_SAFE]
        return "type", (f"typed without wl-clipboard: {_sample(odd)} only come out right "
                        "on a US keyboard layout")
    return "type", None


def enter_text(text: str, method: str = "auto", delay_ms: int = 0,
               paste_keys: Sequence[str] = DEFAULT_PASTE_KEYS) -> dict:
    """Type or paste `text` into the focused widget (see choose_text_method)."""
    chosen, warning = choose_text_method(text, method)
    result: dict = {"method": chosen}
    if chosen == "paste":
        result["clipboard_restored"] = paste_text(text, keys=paste_keys)
    else:
        type_text(text, delay_ms=delay_ms)
    if warning:
        result["warning"] = warning
    return result
