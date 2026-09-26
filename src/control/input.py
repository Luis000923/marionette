"""Mouse and keyboard simulation, for Wayland through ydotool or on Windows natively.

On Wayland the global input device is `ydotoold`'s `uinput` node; on Windows it
is the Win32 input queue. Either way it is a *single* device shared by the whole
machine, so concurrent agents would interleave their keystrokes and clicks. Every
call here goes through the `input_mutex` (see locking.py), which serializes input
across processes; the mutex is reentrant so composite gestures hold it for their
whole sequence.

The two backends differ in one way that matters to the caller. `ydotool type`
sends US-layout keycodes for ASCII and silently drops everything else, so on a
Spanish layout "presentación a:b/c" arrives as "presentacin aÑb-c" and anything
with accents has to go through the clipboard. Windows has no such problem:
`SendInput` with `KEYEVENTF_UNICODE` types the characters themselves, layout
behaving or not, so `choose_text_method` answers "type" there and the clipboard
is only used when it is asked for.
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

from . import platforms
from .locking import FileMutex, LockTimeout  # noqa: F401 - LockTimeout re-exported for callers

if platforms.IS_WINDOWS:
    from . import winapi as _win
else:
    _win = None

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
# On Windows the same canonical names are translated to virtual keys instead; see
# `winapi.VK`, which covers exactly this list.
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
    """Canonical key name for `name` or one of its aliases (None if unknown)."""
    key = name.lower()
    if key in KEYCODES:
        return key
    return KEY_ALIASES.get(key)


class InputError(RuntimeError):
    """Any input failure, whichever backend produced it."""

    hint = ""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        if hint is not None:
            self.hint = hint


class YdotoolError(InputError):
    """ydotool/ydotoold said no (Linux backend)."""


class SendInputError(InputError):
    """The Win32 input queue refused the events (Windows backend)."""


class ClipboardError(RuntimeError):
    pass


# ydotool click byte = mode nibble | button nibble.
# Mode: 0xC=click (down+up), 0x4=down only, 0x8=up only.
_BUTTON_NIBBLE = {"left": 0x0, "right": 0x1, "middle": 0x2}


def _button_code(button: str, mode: str) -> str:
    prefix = {"click": 0xC0, "down": 0x40, "up": 0x80}[mode]
    return hex(prefix | _BUTTON_NIBBLE[button])


def _argv(args: list[str]) -> list[str | bytes]:
    """Command-line arguments as UTF-8, even under a C/Latin-1 locale.

    subprocess encodes str arguments with the filesystem encoding, which under
    LANG=C is ASCII: "señor" would raise UnicodeEncodeError before ydotool ran.
    (Windows passes wide strings, so the conversion never applies there.)
    """
    if (sys.getfilesystemencoding() or "").lower().replace("-", "") == "utf8":
        return list(args)
    return [a.encode("utf-8") for a in args]


def _run(*args: str) -> None:
    """Send one ydotool command, under the global input lock."""
    with input_mutex:
        result = subprocess.run(_argv(["ydotool", *args]), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=15)
    if result.returncode != 0:
        raise YdotoolError(
            f"ydotool {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}\n"
            "Is ydotoold running? Try: systemctl status ydotoold"
        )


def _button_action(button: str, mode: str) -> None:
    """Click / press / release a mouse button, under the global input lock."""
    if _win is not None:
        with input_mutex:
            if mode == "click":
                _win.click_button(button, True)
                _win.click_button(button, False)
            else:
                _win.click_button(button, down=(mode == "down"))
        return
    _run("click", _button_code(button, mode))


#: Closed-loop pointer placement: at most this many corrective moves.
MAX_POINTER_CORRECTIONS = 25
#: Relative moves up to this size land ~1:1; bigger ones get accelerated by
#: libinput (up to ~2x), so only half the remaining distance is sent at once.
_EXACT_STEP = 16


def pointer_position() -> tuple[int, int] | None:
    """Where the compositor (kdotool) or Windows (GetCursorPos) has the pointer."""
    if _win is not None:
        return _win.cursor_pos()
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
    """Walk the pointer onto (x, y) and read its real position back. False when
    no position feedback is available."""
    pos = None
    for _ in range(MAX_POINTER_CORRECTIONS):
        pos = pointer_position()
        if pos is None:
            return False
        dx, dy = x - pos[0], y - pos[1]
        if not dx and not dy:
            return True
        if _win is not None:
            # SetCursorPos is absolute and exact: Windows applies no acceleration
            # to it, so a single move lands on the pixel asked for.
            _win.set_cursor_pos(x, y)
            continue
        _run("mousemove", "-x", str(_damped(dx)), "-y", str(_damped(dy)))
    raise InputError(f"the pointer did not reach {x},{y} (it is at {pos[0]},{pos[1]}); "
                     "are the coordinates on screen?")


def move_mouse(x: int, y: int) -> None:
    """Put the pointer exactly on (x, y), in screen (physical) coordinates.

    ydotool 1.0's `--absolute` is unreliable on Wayland: it slams the pointer to
    0,0 and the follow-up move is lost or scaled by pointer acceleration, so the
    pointer typically ends up at 1,1 whatever was asked. The absolute move is
    kept as a starting point, then corrected in a closed loop with kdotool's
    real pointer position (3-4 moves, ~40 ms). Without kdotool the absolute
    move is all there is. On Windows `SetCursorPos` is exact, so the correction
    loop converges on the first try.
    """
    with input_mutex:
        if _win is None:
            _run("mousemove", "--absolute", "-x", str(x), "-y", str(y))
        if not _steer(x, y) and _win is not None:  # pragma: no cover - GetCursorPos always answers
            _win.set_cursor_pos(x, y)


def move_mouse_relative(dx: int, dy: int) -> None:
    if _win is not None:
        current = _win.cursor_pos() or (0, 0)
        move_mouse(current[0] + int(dx), current[1] + int(dy))
        return
    _run("mousemove", "-x", str(dx), "-y", str(dy))


def click(button: str = "left", x: int | None = None, y: int | None = None) -> None:
    with input_mutex:
        if x is not None and y is not None:
            move_mouse(x, y)
        _button_action(button, "click")


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
        _button_action(button, "down")
        try:
            _sleep(pause)
            for i in range(1, steps + 1):
                x, y = round(x1 + (x2 - x1) * i / steps), round(y1 + (y2 - y1) * i / steps)
                # Only relative moves while the button is down on ydotool: an
                # absolute move would first drag whatever we hold to the screen
                # corner. Windows only ever moves absolutely (exactly), so the
                # same call is right for both backends.
                if not _steer(x, y) and _win is None:
                    _run("mousemove", "--absolute", "-x", str(x), "-y", str(y))
                if steps > 1:
                    _sleep(pause / steps)
            _sleep(pause)
        finally:
            _button_action(button, "up")


def scroll(amount: int) -> None:
    """Positive scrolls up, negative scrolls down."""
    if _win is not None:
        with input_mutex:
            _win.scroll(amount)
        return
    _run("mousemove", "-w", "-x", "0", "-y", str(-amount))


def type_text(text: str, delay_ms: int = 0) -> None:
    """Type `text` into whatever has focus.

    On Windows every character is sent as a Unicode key event, so accents, ñ,
    symbols and CJK come out exactly as written whatever the keyboard layout, and
    the whole string goes out in one `SendInput` call. Characters outside the
    Basic Multilingual Plane (emoji) are the one exception: a single key event
    cannot carry a surrogate pair, which is why `choose_text_method` pastes them.
    On ydotool the text goes through `ydotool type`, which is why anything
    non-ASCII is routed through the clipboard there.
    """
    if _win is not None:
        with input_mutex:
            if delay_ms:
                for char in text:
                    _win.type_unicode(char)
                    _sleep(delay_ms / 1000)
            else:
                _win.type_unicode(text)
        return
    args = ["type"]
    if delay_ms:
        args += ["--key-delay", str(delay_ms)]
    args.append(text)
    _run(*args)


def key_combo(*keys: str) -> None:
    """Press keys together, e.g. key_combo('ctrl', 'shift', 't')."""
    canonical = []
    for key in keys:
        name = resolve_key(key)
        if name is None:
            raise KeyError(key)
        canonical.append(name)
    if _win is not None:
        with input_mutex:
            for name in canonical:
                _win.key_press(name)
            for name in reversed(canonical):
                _win.key_press(name, up=True)
        return
    codes = [KEYCODES[name] for name in canonical]
    down = [f"{c}:1" for c in codes]
    up = [f"{c}:0" for c in reversed(codes)]
    _run("key", *down, *up)


def press_key(name: str) -> None:
    key_combo(name)


# --- clipboard ------------------------------------------------------------------------
# Copying a file's URI and pasting it with Ctrl+V skips a file dialog entirely:
# Chromium accepts pasted files from `text/uri-list`, and so do file managers and
# most upload drop zones that listen for `paste`. The Wayland build shells out to
# wl-copy; on Windows the clipboard is the OS's own, reached through user32.

#: MIME types we can put on the clipboard and take back off it, best first.
_RESTORABLE = ("text/uri-list", "text/plain;charset=utf-8", "text/plain", "UTF8_STRING")


def _wl(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise ClipboardError(
            f"{tool} not found; install wl-clipboard (e.g. sudo pacman -S wl-clipboard)"
        )
    return path


def clipboard_available() -> bool:
    """True when text can be put on the clipboard at all (always true on Windows)."""
    if _win is not None:
        return True
    return shutil.which("wl-copy") is not None


def copy_to_clipboard(data: str | bytes, mime: str | None = None) -> None:
    """Put `data` on the clipboard (optionally announced as `mime`).

    `mime="text/uri-list"` takes file URIs and puts the files on the clipboard the
    way Explorer does, so a page's paste handler or an upload area takes them with
    Ctrl+V instead of a file dialog.

    wl-copy forks a child that keeps serving the clipboard until something else is
    copied. That child inherits our stdio, so waiting on a pipe would hang until
    the next copy: stdout goes to /dev/null and stderr to a file.
    """
    if _win is not None:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        if mime == "text/uri-list":
            _win.set_files([_uri_to_path(line) for line in text.splitlines() if line.strip()])
        else:
            _win.set_text(text)
        return
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


def _uri_to_path(uri: str) -> str:
    """`file:///C:/x%20y` -> `C:\\x y` (a plain path is returned unchanged)."""
    from urllib.parse import unquote, urlparse

    text = uri.strip()
    if not text.lower().startswith("file:"):
        return text
    parsed = urlparse(text)
    if parsed.netloc:  # file://server/share/x: a UNC path
        return unquote(f"//{parsed.netloc}{parsed.path}")
    path = unquote(parsed.path)
    if not platforms.IS_WINDOWS:
        return path
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]  # file:///C:/x -> C:/x
    return str(Path(path))  # and C:/x/y -> C:\x\y, the way Windows writes it


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
    files = _existing(paths)
    copy_to_clipboard(file_uri_list(files), mime="text/uri-list")
    return [str(p) for p in files]


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
    if _win is not None:
        if mime == "text/uri-list":
            return "".join(f"{Path(p).as_uri()}\r\n" for p in _win.get_files())
        return _win.get_text()
    # Bytes in, decoded as UTF-8 here: text=True would use the locale's
    # encoding, and a Latin-1 or C locale turns "ó" into "Ã³" or worse.
    args = ["--no-newline"] + (["--type", mime] if mime else [])
    data = _wl_paste(*args)
    return "" if data is None else data.decode("utf-8", errors="replace")


def _save_clipboard() -> tuple[str, bytes] | None:
    """(mime, bytes) of the current clipboard; ("", b"") when it is empty;
    None when it holds something that can't be put back (an image...)."""
    if _win is not None:
        if not _win.clipboard_has_restorable_content():
            # Something is there but not in a format we can write back: an image
            # clipboard must be left alone rather than "restored" to nothing.
            return None if not _win.clipboard_is_empty() else ("", b"")
        files = _win.get_files()
        if files:
            uris = "".join(f"{Path(p).as_uri()}\r\n" for p in files)
            return ("text/uri-list", uris.encode("utf-8"))
        return ("text/plain", _win.get_text().encode("utf-8"))
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
    if _win is not None:
        _win.clear_clipboard()
        return
    subprocess.run([_wl("wl-copy"), "--clear"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=15)


DEFAULT_PASTE_KEYS = ("ctrl", "v")
#: Terminals take Ctrl+V as "insert next character literally", not paste.
TERMINAL_PASTE_KEYS = ("ctrl", "shift", "v")
TERMINAL_CLASSES = ("kitty", "konsole", "alacritty", "foot", "wezterm", "xterm", "terminal",
                    "yakuake", "tilix", "ghostty",
                    # Windows: the console host class plus the shells' own names.
                    "windowsterminal", "cascadia_hosting_window", "cascadia", "openterminal",
                    "conhost", "powershell", "pwsh", "cmd", "wt")
#: How long the target app gets to read a paste before the clipboard is put back:
#: apps fetch the data asynchronously after the key press.
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
    On Windows the clipboard history (Win+V) keeps pasted text, so `type` is the
    better method for secrets there.
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


# --- choosing between typing and pasting ------------------------------------------------

#: What `ydotool type` gets right on any common Latin layout. It sends US-layout
#: keycodes for ASCII and silently drops everything else, so on a Spanish layout
#: "presentación a:b/c" arrives as "presentacin aÑb-c". Only letters, digits,
#: whitespace, comma and period sit on the same keys everywhere that matters.
#:
#: On Windows this is not the limitation: `SendInput` with KEYEVENTF_UNICODE types
#: the characters themselves, so accents, ñ, symbols and CJK all arrive exactly
#: (measured: "Hola señor ¿Qué tal? a:b/c ñáéíóú€ [x] &q ~z^ 50%" came through
#: character for character in a raw Win32 edit control). What a key event *cannot*
#: carry is a character outside the Basic Multilingual Plane -- emoji, rare CJK
#: ideographs, musical symbols: those need a surrogate pair, and the receiving
#: control usually turns the pair into one stray character. So on Windows, text
#: holding one of those is pasted instead of typed.
_LAYOUT_SAFE = frozenset(string.ascii_letters + string.digits + " \t\n,.")
TEXT_METHODS = ("auto", "paste", "type")

#: Last code point of the Basic Multilingual Plane; above it, characters live in
#: a surrogate pair and cannot be typed with a single key event.
BMP_MAX = 0xFFFF


def outside_bmp(text: str) -> list[str]:
    """The characters of `text` that a key event cannot carry (emoji and friends)."""
    return [ch for ch in text if ord(ch) > BMP_MAX]


def typeable(text: str) -> bool:
    """True when typing `text` reproduces it exactly, whatever the layout."""
    if _win is not None:
        return not outside_bmp(text)
    return all(ch in _LAYOUT_SAFE for ch in text)


def _sample(chars) -> str:
    unique = sorted(set(chars))
    return " ".join(unique[:12]) + (" ..." if len(unique) > 12 else "")


def choose_text_method(text: str, method: str = "auto") -> tuple[str, str | None]:
    """Decide how to enter `text`: ("type" | "paste", warning or None).

    On Windows, typing wins by default: key events carry Unicode directly, so
    accents, ñ, symbols and CJK go in exactly and the clipboard is never touched.
    Only characters a key event cannot carry (emoji and anything else outside the
    Basic Multilingual Plane) are pasted, and pasting is exact everywhere.

    On Wayland it is the other way round: ydotool sends US-layout keycodes, so
    anything with accents, ñ, symbols or other Unicode is pasted through the
    clipboard, and without wl-clipboard non-ASCII text is an error rather than
    silently losing characters (ASCII symbols are typed with a warning, since
    they are right on a US layout).
    """
    if method not in TEXT_METHODS:
        raise ValueError(f"unknown text method {method!r}")
    non_ascii = [ch for ch in text if not ch.isascii()]
    if method == "type":
        if _win is not None:
            astral = outside_bmp(text)
            return "type", (f"a key event cannot carry these and they may arrive as a "
                            f"stray surrogate: {_sample(astral)}; use --method paste"
                            if astral else None)
        return "type", (f"ydotool cannot type these and drops them: {_sample(non_ascii)}"
                        if non_ascii else None)
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
