"""Unified CLI for desktop control: windows, input, screenshots, apps, browser, sessions.

Multi-agent use rests on four things wired up here:

* `--session NAME` / `CONTROL_SESSION_ID` selects an agent's workspace, which
  supplies the default `--desktop` for every window/app command;
* `--desktop REF` takes a 1-based number or a desktop *name*; commands that
  create things (`app launch`, `win send`) create a missing named desktop on the
  fly, read-only queries never do;
* `--env-file PATH` layers per-agent configuration on top of the repo `.env`;
* input commands (`click`, `type`, `key`) accept `--window PATTERN`, which
  focuses that window and sends the input while holding the global input lock,
  so no other agent can steal focus in between.

Every command is machine-readable: `--json` (anywhere on the command line) or
`CONTROL_JSON=1` makes it print exactly one JSON value on stdout, and failures
print `{"error": ..., "hint": ..., "type": ...}` on stdout with exit status 1
(2 for usage errors). Plain text stays the default for humans.
"""
from __future__ import annotations

import argparse
import difflib
import functools
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Error as PlaywrightError

from . import apps, atspi, cursor as cursor_mod, env, input as inp, screenshot, session as session_mod, windows
from . import browser as browser_mod
from .browser import Browser
from .env import load_env


# --- output plumbing ---------------------------------------------------------

class CliError(SystemExit):
    """A user-facing failure with a hint. Subclasses SystemExit so a handler
    called directly (as the tests do) still stops with the message."""

    def __init__(self, message: str, hint: str = "", kind: str = "cli_error"):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.kind = kind


@dataclass
class Out:
    """What a command produced: `data` for --json, `text` for humans."""
    data: Any
    text: str | None = None


def _truthy(value: str | None) -> bool:
    return (value or "").lower() in ("1", "true", "yes")


def _json_requested(argv: list[str] | None = None) -> bool:
    """JSON mode before (or without) a parsed namespace, e.g. for usage errors."""
    argv = sys.argv[1:] if argv is None else argv
    return "--json" in argv or _truthy(os.environ.get("CONTROL_JSON"))


# --- text encoding: UTF-8 end to end, whatever the locale -------------------------

def _is_utf8(encoding: str | None) -> bool:
    return (encoding or "").lower().replace("-", "").replace("_", "") == "utf8"


def utf8_arg(value: str) -> str:
    """Undo a non-UTF-8 decoding of a command-line argument.

    Python decodes argv with the filesystem encoding. Under a Latin-1 locale the
    UTF-8 bytes of "ó" become "Ã³"; under a strict one, undecodable bytes become
    lone surrogates that can't even be encoded again. Re-encoding with the same
    codec recovers the original bytes, which are then decoded as UTF-8 (the
    encoding of every terminal and of Wayland). Arguments that really were in
    another encoding are left alone.
    """
    has_surrogates = any("\udc80" <= ch <= "\udcff" for ch in value)
    if _is_utf8(sys.getfilesystemencoding()) and not has_surrogates:
        return value
    try:
        raw = value.encode(sys.getfilesystemencoding(), sys.getfilesystemencodeerrors())
    except (UnicodeEncodeError, LookupError):
        return value
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace") if has_surrogates else value


def read_stdin_text() -> str:
    """All of stdin as UTF-8 text, decoded from the raw bytes (a BOM is dropped).

    sys.stdin.read() would decode with the locale's encoding; a C or Latin-1
    locale then mangles every accent before the text ever reaches ydotool.
    """
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:  # already text (e.g. replaced by a StringIO)
        return sys.stdin.read()
    data = buffer.read()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CliError(f"stdin is not valid UTF-8 (byte {exc.start}: {data[exc.start:exc.start + 4]!r})",
                       hint="convert it first, e.g.: iconv -f latin1 -t utf-8 file | control ...",
                       kind="encoding_error") from exc


def _text_arg(value: str) -> str:
    return read_stdin_text() if value == "-" else value


def _utf8_stdio() -> None:
    """Print accents as themselves instead of crashing on an ASCII/Latin-1 stdout."""
    for stream in (sys.stdout, sys.stderr):
        if not _is_utf8(getattr(stream, "encoding", "utf-8")) and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _json_mode(args: argparse.Namespace) -> bool:
    # `is True`: a mocked namespace must not count as JSON mode.
    return getattr(args, "json", False) is True or _truthy(os.environ.get("CONTROL_JSON"))


def command(fn: Callable[[argparse.Namespace], Out | None]) -> Callable[[argparse.Namespace], None]:
    """Turn a handler returning `Out` into one that prints it in the right format."""

    @functools.wraps(fn)
    def run(args: argparse.Namespace) -> None:
        out = fn(args)
        if out is None:
            return
        if _json_mode(args):
            print(json.dumps(out.data, ensure_ascii=False, indent=2 if getattr(args, "pretty", False) else None, separators=None if getattr(args, "pretty", False) else (",", ":")))
        elif out.text:
            print(out.text)

    return run


def _ok(action: str, **fields: Any) -> dict:
    return {"ok": True, "action": action, **fields}


def _win_line(w: windows.Window) -> str:
    return f"{w.id}\t{w.classname}\t{w.title}"


def _desktop_dict(d: windows.Desktop, current: int | None = None) -> dict:
    data = {"number": d.number, "position": d.position, "id": d.id, "name": d.name}
    if current is not None:
        data["current"] = d.number == current
    return data


# --- targeting ---------------------------------------------------------------

def _desktop(args: argparse.Namespace, create: bool = False) -> int | None:
    """Desktop a command should target: explicit --desktop, else the session's.

    Numbers pass straight through (kdotool validates them). Names are resolved
    through KWin, and created when `create` is set.
    """
    ref = getattr(args, "desktop", None)
    if ref is not None:
        ref = str(ref)
        if ref.isdigit():
            return int(ref)
        return windows.resolve_desktop(ref, create=create).number
    name = session_mod.current_name()
    if name and session_mod.load(name) is None:
        # Silently falling back to "all desktops" would let this agent act on
        # every other agent's windows while believing it is sandboxed.
        raise CliError(
            f"session {name!r} is not initialised",
            hint=f"run: control session init --name {name}", kind="session_error",
        )
    return session_mod.resolve_desktop(None)


def _find_window(pattern: str, args: argparse.Namespace) -> windows.Window:
    desktop = _desktop(args)
    win = windows.find_one(pattern, desktop=desktop)
    if not win:
        scope = f" on desktop {desktop}" if desktop is not None else ""
        raise CliError(
            f"no window matching {pattern!r}{scope}",
            hint="list candidates with: control win list --json",
            kind="window_not_found",
        )
    if win.desktop is None:
        win.desktop = desktop
    return win


def _require_window(args: argparse.Namespace) -> windows.Window:
    return _find_window(args.pattern, args)


def _focused_input(args: argparse.Namespace, action: str, send: Callable[[], None],
                   **fields: Any) -> Out:
    """Run `send` under the input lock, first focusing --window if one was given.

    Focus is as global as uinput: if agent A activated its window and then
    released the lock before typing, agent B could activate its own in between
    and A's keystrokes would land in B's window. Holding one lock across
    "focus + input" makes the pair atomic.
    """
    with inp.input_lock():
        win = None
        if getattr(args, "window", None):
            win = _find_window(args.window, args)
            if not win.focus():
                raise CliError(
                    f"window {win.id} ({win.title!r}) did not take focus",
                    hint="KWin focus-stealing prevention may be blocking it; "
                         "try 'control win activate' first",
                    kind="focus_error",
                )
        send()
    data = _ok(action, **fields)
    if win is not None:
        data["window"] = win.to_dict()
    return Out(data)


# --- windows -----------------------------------------------------------------

@command
def _cmd_win_list(args: argparse.Namespace) -> Out:
    results = windows.list_windows(args.pattern or "", desktop=_desktop(args))
    return Out([w.to_dict() for w in results], "\n".join(_win_line(w) for w in results))


@command
def _cmd_win_active(args: argparse.Namespace) -> Out:
    win = windows.active_window()
    return Out(win.to_dict(), _win_line(win))


def _window_action(name: str, act: Callable[[windows.Window, argparse.Namespace], None]):
    @command
    def run(args: argparse.Namespace) -> Out:
        win = _require_window(args)
        act(win, args)
        return Out(_ok(name, window=win.to_dict()))

    run.__name__ = f"_cmd_win_{name}"
    return run


_cmd_win_activate = _window_action("activate", lambda w, a: w.activate())
_cmd_win_close = _window_action("close", lambda w, a: w.close())
_cmd_win_move = _window_action("move", lambda w, a: w.move(a.x, a.y))
_cmd_win_resize = _window_action("resize", lambda w, a: w.resize(a.width, a.height))


def _guard_session_desktop(number: int, args: argparse.Namespace, what: str) -> None:
    """Inside a session, never reach outside its own desktop without --force:
    that desktop is the agent's sandbox, the others belong to the human (or to
    other agents)."""
    name = session_mod.current_name()
    if not name or getattr(args, "force", False):
        return
    own = session_mod.resolve_desktop(None)
    if own is not None and number != own:
        raise CliError(
            f"session {name!r} works on desktop {own}; refusing to {what} desktop {number}",
            hint="pass --force if that is really intended", kind="session_guard",
        )


@command
def _cmd_win_send(args: argparse.Namespace) -> Out:
    """Move a matching window onto another virtual desktop (created if named and missing)."""
    win = _require_window(args)
    target = str(args.target)
    number = int(target) if target.isdigit() else windows.resolve_desktop(target, create=True).number
    _guard_session_desktop(number, args, "move a window to")
    win.move_to_desktop(number)
    return Out(_ok("send", window=win.to_dict(), desktop=number), f"{win.id}\t-> desktop {number}")


@command
def _cmd_win_wait(args: argparse.Namespace) -> Out:
    win = windows.wait_for_window(
        args.pattern, timeout=args.timeout, desktop=_desktop(args),
        activate=not args.no_activate,
    )
    if win is None:
        raise CliError(
            f"no window matching {args.pattern!r} within {args.timeout}s",
            hint="raise --timeout, or check the pattern with: control win list --json",
            kind="timeout",
        )
    return Out(win.to_dict(), _win_line(win))


@command
def _cmd_win_wait_settled(args: argparse.Namespace) -> Out:
    win = _require_window(args)
    state = windows.wait_settled(win, stable=args.stable / 1000, timeout=args.timeout)
    state["window"] = win.to_dict()
    if not state["settled"]:
        raise CliError(f"window {win.id} was still changing after {args.timeout:g}s",
                       hint="raise --timeout, or lower --stable", kind="timeout")
    return Out(state, f"{win.id}\tsettled after {state['waited']}s")


# --- screenshots ---------------------------------------------------------------

def _box(value: str) -> tuple[int, int, int, int]:
    try:
        x, y, w, h = (int(v) for v in value.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected X,Y,W,H (e.g. 0,0,400,300), got {value!r}") from None
    return x, y, w, h


def _shoot(args: argparse.Namespace, window: windows.Window | None = None) -> dict:
    """Capture, then crop/shrink/OCR locally. Returns the fields for the output."""
    mode = getattr(args, "mode", "full")
    crop_arg = getattr(args, "crop", None)
    if mode == "region" and crop_arg:
        # An explicit box needs no interactive selection: grab the screen and crop.
        mode_fn = screenshot.fullscreen
    else:
        mode_fn = {"full": screenshot.fullscreen, "window": screenshot.active_window,
                   "region": screenshot.region}[mode]
    fn = mode_fn
    data: dict[str, Any] = {"mode": mode}
    origin = (0, 0)
    if window is not None or getattr(args, "window", None):
        # "window" mode captures whatever is focused, so focus + capture must
        # not be split by another agent's activation.
        with inp.input_lock():
            win = window or _find_window(args.window, args)
            win.focus()
            path = fn()
            g = win.geometry()
        data["window"] = win.to_dict()
        if mode == "window":
            origin = (g.get("x", 0), g.get("y", 0))
            try:
                screenshot.trim_window_shadow(path, g.get("width", 0), g.get("height", 0))
            except OSError:
                pass  # keep the untrimmed capture
    else:
        path = fn()
    crop = getattr(args, "crop", None)
    if crop:
        origin = (origin[0] + crop[0], origin[1] + crop[1])
    scale = getattr(args, "scale", 1.0)
    try:
        if getattr(args, "ocr", False) or getattr(args, "grep", None):
            src = screenshot.crop_scale(path, crop) if crop else path
            lines = screenshot.ocr(src, origin, words=getattr(args, "ocr_words", False))
            pat = getattr(args, "grep", None)
            if pat:
                rx = re.compile(pat, re.I)
                lines = [ln for ln in lines if rx.search(ln["text"])]
            data["lines"] = lines
        if crop or scale != 1.0:
            path = screenshot.crop_scale(path, crop, scale)
    except screenshot.ScreenshotError as e:
        raise CliError(str(e), kind="ocr_error") from e
    data["path"] = str(path)
    return data


def _shot_text(data: dict) -> str:
    rows = [f"{ln['x']},{ln['y']},{ln['w']},{ln['h']} {ln['text']}" for ln in data.get("lines", [])]
    return "\n".join([data["path"], *rows])


@command
def _cmd_shot(args: argparse.Namespace) -> Out:
    data = _shoot(args)
    return Out(data, _shot_text(data))


# --- apps --------------------------------------------------------------------

@command
def _cmd_apps_list(args: argparse.Namespace) -> Out:
    found = apps.find(args.query) if args.query else apps.list_apps()
    return Out([{"id": a.app_id, "name": a.name} for a in found],
               "\n".join(f"{a.app_id}\t{a.name}" for a in found))


@command
def _cmd_apps_launch(args: argparse.Namespace) -> Out:
    desktop = _desktop(args, create=True)
    if not (args.wait or desktop is not None):
        apps.launch(args.app_id)
        return Out(_ok("launch", app_id=args.app_id, window=None))
    win = apps.launch(
        args.app_id, wait=True, timeout=args.timeout,
        window_pattern=args.window_pattern, desktop=desktop,
        activate=not args.no_activate,
    )
    if win is None:
        raise CliError(
            f"launched {args.app_id!r} but no new window appeared within {args.timeout}s",
            hint="raise --timeout, or pass --window-pattern matching the window's title/class",
            kind="timeout",
        )
    return Out(_ok("launch", app_id=args.app_id, window=win.to_dict()), _win_line(win))


# --- agent cursor ------------------------------------------------------------

def _cursor_call(fn: Callable[..., dict], *a: Any, **kw: Any) -> dict:
    try:
        return fn(*a, **kw)
    except (cursor_mod.CursorError, inp.YdotoolError) as e:
        raise CliError(str(e), kind="cursor_error") from e


def _cursor_out(action: str, data: dict) -> Out:
    where = f" at {data['x']},{data['y']}" if data.get("visible") else ""
    return Out(_ok(action, **data), f"cursor {'visible' if data['visible'] else 'hidden'}{where}")


@command
def _cmd_cursor_show(args: argparse.Namespace) -> Out:
    return _cursor_out("cursor_show", _cursor_call(cursor_mod.show, args.color))


@command
def _cmd_cursor_hide(args: argparse.Namespace) -> Out:
    return _cursor_out("cursor_hide", _cursor_call(cursor_mod.hide))


@command
def _cmd_cursor_status(args: argparse.Namespace) -> Out:
    return _cursor_out("cursor_status", cursor_mod.status())


@command
def _cmd_cursor_move(args: argparse.Namespace) -> Out:
    return _cursor_out("cursor_move", _cursor_call(cursor_mod.move, args.x, args.y, args.duration))


@command
def _cmd_cursor_click(args: argparse.Namespace) -> Out:
    data = _cursor_call(cursor_mod.click, args.button, args.x, args.y,
                        restore=not args.no_restore, duration=args.duration)
    return _cursor_out("cursor_click", data)


# --- input -------------------------------------------------------------------

def _cursor_follow(x: int | None, y: int | None) -> None:
    """If the agent cursor is on screen, glide it to (x, y) so the user sees where we act."""
    if x is None or y is None or not cursor_mod.status()["visible"]:
        return
    try:
        cursor_mod.move(x, y)
    except cursor_mod.CursorError:
        pass  # decoration only: never fail the real click over it


def _then_shot(args: argparse.Namespace, out: Out) -> Out:
    """--then-shot: capture the result of an action in the same call."""
    target = getattr(args, "then_shot", None)
    if not target:
        return out
    time.sleep(0.4)
    ns = argparse.Namespace(mode="full" if target == "full" else "window",
                            window=None if target == "full" else target,
                            desktop=getattr(args, "desktop", None), crop=args.crop, scale=args.scale,
                            ocr=args.ocr, grep=args.grep)
    shot = _shoot(ns)
    out.data["shot"] = shot
    out.text = (out.text or "") + "\n" + _shot_text(shot)
    return out


def _locate_text(args: argparse.Namespace) -> dict:
    """Find `--text` on screen (or in --window) by OCR; returns the match with its centre."""
    ns = argparse.Namespace(**vars(args))
    ns.mode = "window" if getattr(args, "window", None) else "full"
    ns.crop, ns.scale, ns.ocr, ns.grep, ns.ocr_words = None, 1.0, True, args.text, True
    try:
        lines = _shoot(ns)["lines"]
    except re.error as e:
        raise CliError(f"bad --text regex: {e}", kind="usage_error") from e
    # Reading order, so --nth means "the Nth one you would read".
    lines.sort(key=lambda ln: (round(ln["y"] / 10), ln["x"]))
    if len(lines) < args.nth:
        found = [ln["text"] for ln in lines][:10]
        raise CliError(
            f"text {args.text!r} not found on screen"
            + (f" (only {len(lines)} match(es), asked for #{args.nth})" if lines else ""),
            hint="look at what OCR sees with: control shot --ocr" + (f"; matches: {found}" if found else ""),
            kind="text_not_found",
        )
    ln = lines[args.nth - 1]
    box = _match_box(ln, args.text)
    return {"text": ln["text"], "x": box[0] + box[2] // 2, "y": box[1] + box[3] // 2}


def _match_box(line: dict, pattern: str) -> tuple[int, int, int, int]:
    """x, y, w, h of the words the regex matched (the whole line if that can't be told),
    so a click lands on the target word, not the middle of a long line."""
    whole = (line["x"], line["y"], line["w"], line["h"])
    m = re.search(pattern, line["text"], re.I)
    ws = line.get("words")
    if not m or not ws or " ".join(w["text"] for w in ws) != line["text"]:
        return whole
    hit, pos = [], 0
    for w in ws:
        start, end = pos, pos + len(w["text"])
        if start < m.end() and end > m.start():
            hit.append(w)
        pos = end + 1
    if not hit:
        return whole
    x1, y1 = min(w["x"] for w in hit), min(w["y"] for w in hit)
    x2, y2 = max(w["x"] + w["w"] for w in hit), max(w["y"] + w["h"] for w in hit)
    return x1, y1, x2 - x1, y2 - y1


@command
def _cmd_click(args: argparse.Namespace) -> Out:
    matched = None
    if args.text:
        if args.x is not None or args.y is not None:
            raise CliError("use either --text or -x/-y, not both", kind="usage_error")
        matched = _locate_text(args)
        args.x, args.y = matched["x"], matched["y"]
    _cursor_follow(args.x, args.y)
    out = _focused_input(args, "click", lambda: inp.click(args.button, args.x, args.y),
                         button=args.button, x=args.x, y=args.y)
    if matched:
        out.data["matched"] = matched["text"]
    return _then_shot(args, out)


def _paste_keys(args: argparse.Namespace) -> tuple[str, ...]:
    """--paste-keys if given, else Ctrl+Shift+V for terminals and Ctrl+V elsewhere.
    Called after --window took focus, so the active window is the target."""
    if getattr(args, "paste_keys", None):
        return tuple(k.strip().lower() for k in args.paste_keys.split("+"))
    try:
        return inp.paste_keys_for(windows.active_window().classname)
    except Exception:  # noqa: BLE001 - no KWin answer: the common case is not a terminal
        return inp.DEFAULT_PASTE_KEYS


@command
def _cmd_type(args: argparse.Namespace) -> Out:
    text = _text_arg(args.text)
    # Decided (and wl-clipboard checked) before focusing anything.
    method, warning = inp.choose_text_method(text, args.method)
    result: dict[str, Any] = {}

    def send() -> None:
        if method == "paste":
            result["clipboard_restored"] = inp.paste_text(text, keys=_paste_keys(args))
        else:
            inp.type_text(text, delay_ms=args.delay)

    # Only the length is echoed back: the text may be a password, and repeating
    # it would cost the agent tokens for nothing.
    out = _focused_input(args, "type", send, chars=len(text), method=method)
    out.data.update(result)
    if warning:
        out.data["warning"] = warning
        print(f"[WARN] {warning}", file=sys.stderr)
    return out


def _key_list() -> Out:
    groups = {g: [k for k in keys if k in inp.KEYCODES] for g, keys in inp.KEY_GROUPS.items()}
    lines = [f"{group}: {' '.join(keys)}" for group, keys in groups.items()]
    by_target: dict[str, list[str]] = {}
    for alias, target in inp.KEY_ALIASES.items():
        by_target.setdefault(target, []).append(alias)
    lines.append("aliases: " + "; ".join(f"{t} = {', '.join(a)}" for t, a in sorted(by_target.items())))
    lines.append("names are case-insensitive physical keys (US positions); combine them: "
                 "control key ctrl shift t. To enter text use: control type")
    return Out({"keys": groups, "aliases": inp.KEY_ALIASES}, "\n".join(lines))


@command
def _cmd_key(args: argparse.Namespace) -> Out:
    if args.list:
        return _key_list()
    if not args.keys:
        raise CliError("no key given", hint="e.g. control key ctrl s; list names with: control key --list",
                       kind="usage_error")
    unknown = [k for k in args.keys if inp.resolve_key(k) is None]
    if unknown:
        suggestions = difflib.get_close_matches(unknown[0].lower(),
                                                [*inp.KEYCODES, *inp.KEY_ALIASES], n=3)
        raise CliError(
            f"unknown key name(s): {', '.join(unknown)}",
            hint=f"did you mean: {', '.join(suggestions)}" if suggestions
            else "list valid names with: control key --list",
            kind="unknown_key",
        )
    return _focused_input(args, "key", lambda: inp.key_combo(*args.keys), keys=args.keys)


def _point(value: str) -> tuple[int, int]:
    try:
        x, y = (int(v) for v in value.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected X,Y (e.g. 640,480), got {value!r}") from None
    return x, y


@command
def _cmd_drag(args: argparse.Namespace) -> Out:
    (x1, y1), (x2, y2) = args.start, args.end
    _cursor_follow(x1, y1)
    out = _focused_input(
        args, "drag",
        lambda: inp.drag(x1, y1, x2, y2, args.button, steps=args.steps, hold_ms=args.hold),
        start=[x1, y1], end=[x2, y2], button=args.button, steps=args.steps,
    )
    _cursor_follow(x2, y2)
    return _then_shot(args, out)


@command
def _cmd_clip_set(args: argparse.Namespace) -> Out:
    if bool(args.files) == (args.text is not None):
        raise CliError("pass either TEXT (or '-' for stdin) or --file PATH...",
                       hint="e.g. control clip set --file ~/deck.pptx --paste --window Brave",
                       kind="usage_error")
    if args.files:
        fields: dict[str, Any] = {"files": inp.copy_files(args.files), "mime": "text/uri-list"}
        text = f"copied {len(fields['files'])} file(s)"
    else:
        content = _text_arg(args.text)
        inp.copy_to_clipboard(content, mime=args.mime)
        # Only the length is echoed back, as with `type`: it may be a secret.
        fields = {"chars": len(content), "mime": args.mime or "text/plain"}
        text = f"copied {len(content)} chars"
    if not (args.paste or args.window):
        return Out(_ok("clip_set", pasted=False, **fields), text)
    out = _focused_input(args, "clip_set", lambda: inp.paste(_paste_keys(args)), pasted=True,
                         **fields)
    out.text = f"{text} and pasted"
    return out


@command
def _cmd_clip_get(args: argparse.Namespace) -> Out:
    content = inp.read_clipboard(mime=args.mime)
    return Out({"text": content, "chars": len(content)}, content)


# --- native apps through AT-SPI ------------------------------------------------------

def _a11y_request(args: argparse.Namespace) -> dict:
    """Which app/element an a11y command is about, as worker parameters."""
    req: dict[str, Any] = {"app": getattr(args, "app", None), "pid": getattr(args, "pid", None)}
    if getattr(args, "window", None):
        win = _find_window(args.window, args)
        req["pid"] = win.pid()
        req["window_title"] = win.title
    if not any(req.values()):
        raise CliError("say which application: --app NAME, --pid N or --window PATTERN",
                       hint="list them with: control a11y apps", kind="usage_error")
    role = getattr(args, "role", None)
    flt = {"role": atspi.normalize_role(role) if role else None, "name": getattr(args, "name", None),
           "text": getattr(args, "text", None), "states": getattr(args, "state", None) or None,
           "interactive": getattr(args, "interactive", False) or None}
    req["filter"] = {k: v for k, v in flt.items() if v}
    for key in ("path", "nth", "limit", "depth"):
        if getattr(args, key, None) is not None:
            req[key] = getattr(args, key)
    if getattr(args, "all", False):
        req["all"] = True
    return req


def _node_line(n: dict, indent: bool = True) -> str:
    parts = [("  " * n["depth"] if indent else "") + n["role"]]
    if n["name"]:
        parts.append(f'"{n["name"]}"')
    if n.get("states"):
        parts.append(f"[{', '.join(n['states'])}]")
    if n.get("actions"):
        parts.append("{" + ", ".join(n["actions"]) + "}")
    if n.get("text"):
        parts.append("text=" + json.dumps(n["text"], ensure_ascii=False))
    if n.get("value"):
        parts.append(f"value={n['value']['current']:g}")
    return " ".join(parts) + f"\t{n['path'] or '/'}"


def _a11y(op: str, args: argparse.Namespace, **extra: Any) -> dict:
    atspi.require_enabled()
    return atspi.call(op, **_a11y_request(args), **extra)


@command
def _cmd_a11y_status(args: argparse.Namespace) -> Out:
    info = atspi.status()
    lines = [f"bus enabled: {info['bus_enabled']}", f"worker python: {info['helper_python']}"]
    if not info.get("available"):
        lines.append(f"worker unavailable: {info.get('error')} ({info.get('hint')})")
    lines += [f"  {a['pid']}\t{a['toolkit']}\t{a['name']}\t{a['children']} top-level"
              for a in info["apps"] if a["children"]]
    return Out(info, "\n".join(lines))


@command
def _cmd_a11y_enable(args: argparse.Namespace) -> Out:
    atspi.set_enabled(not args.off)
    enabled = atspi.is_enabled()
    text = ("accessibility bus enabled: Qt/GTK apps publish their widgets now; restart "
            "Chromium/Electron apps with --force-renderer-accessibility" if enabled
            else "accessibility bus disabled")
    return Out(_ok("a11y_enable", enabled=enabled), text)


@command
def _cmd_a11y_apps(args: argparse.Namespace) -> Out:
    data = atspi.call("apps", all=args.all or None)
    return Out(data, "\n".join(f"{a['pid']}\t{a['toolkit']}\t{a['name']}" for a in data["apps"]))


def _tree_text(data: dict) -> str:
    flat = bool(data.get("_flat"))
    lines = [f"# {data['app']['name']} (pid {data['app']['pid']}, {data['app']['toolkit']})"]
    lines += [_node_line(n, indent=not flat) for n in data["nodes"]]
    if data.get("truncated"):
        lines.append("... truncated: raise --limit, or narrow with --path/--role/--name")
    if data.get("hint"):
        lines.append(f"hint: {data['hint']}")
    return "\n".join(lines)


@command
def _cmd_a11y_tree(args: argparse.Namespace) -> Out:
    data = _a11y("tree", args)
    flat = any(getattr(args, k, None) for k in ("role", "name", "text", "state", "interactive"))
    return Out(data, _tree_text(dict(data, _flat=flat)))


@command
def _cmd_a11y_get(args: argparse.Namespace) -> Out:
    data = _a11y("get", args)
    el = data["element"]
    lines = [_node_line(el, indent=False)]
    if el.get("value"):
        lines.append(f"value: {el['value']['current']:g} (range {el['value']['min']:g}..{el['value']['max']:g})")
    if el.get("description"):
        lines.append(f"description: {el['description']}")
    lines.append(f"states: {', '.join(el.get('states_all', []))}")
    return Out(data, "\n".join(lines))


@command
def _cmd_a11y_read(args: argparse.Namespace) -> Out:
    """Just the content: a field's text, else its value, else its name."""
    el = _a11y("get", args)["element"]
    if el.get("text") is not None:
        content = el["text"]
    elif el.get("value"):
        content = f"{el['value']['current']:g}"
    else:
        content = el["name"]
    return Out({"path": el["path"], "role": el["role"], "name": el["name"], "content": content},
               content)


def _acted(verb: str):
    return lambda d: f"{verb} {d['element']['role']} \"{d['element']['name']}\" ({d['path']})"


@command
def _cmd_a11y_do(args: argparse.Namespace) -> Out:
    data = _a11y("action", args, action=args.action)
    return Out(data, _acted(data["action"])(data))


@command
def _cmd_a11y_focus(args: argparse.Namespace) -> Out:
    data = _a11y("focus", args)
    return Out(data, _acted("focused")(data))


@command
def _cmd_a11y_set_text(args: argparse.Namespace) -> Out:
    data = _a11y("set_text", args, value=_text_arg(args.value))
    data["element"].pop("text", None)  # may be a secret: report the length only
    data["chars"] = len(_text_arg(args.value)) if args.value != "-" else None
    return Out(data, _acted("set text of")(data))


@command
def _cmd_a11y_wait(args: argparse.Namespace) -> Out:
    # `timeout` bounds the worker process; `wait_timeout` is the wait itself.
    data = _a11y("wait", args, gone=args.gone or None, wait_timeout=args.timeout,
                 settle_ms=args.settle or None, timeout=args.timeout + 5)
    what = "gone" if args.gone else ("found" if data.get("element") else "settled")
    return Out(data, f"{what} after {data['waited']}s"
               + (f": {_node_line(data['element'], indent=False)}" if data.get("element") else ""))


@command
def _cmd_sandbox(args: argparse.Namespace) -> Out:
    command_line = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not command_line:
        raise CliError("no command to run", hint="e.g. control sandbox -- ./agent_task.sh",
                       kind="usage_error")
    if args.timeout is not None and args.timeout <= 0:
        raise CliError("--timeout must be positive", kind="usage_error")
    report = session_mod.sandbox(command_line, name=args.name, keep_windows=args.keep_windows,
                                 timeout=args.timeout)
    text = (f"sandbox {report['name']} (desktop {report['desktop']}): "
            + (f"timed out after {args.timeout:g}s, " if report["timed_out"] else "")
            + f"exit {report['exit_code']}, closed {len(report['closed'])} window(s)"
            + (f", killed pid(s) {', '.join(map(str, report['killed_pids']))}"
               if report["killed_pids"] else "")
            + ("" if report["desktop_removed"] else
               f"; desktop and session kept, {len(report['left'])} window(s) still open "
               f"(clean up with: control session end --name {report['name']} --remove-desktop)"))
    if not _json_mode(args):
        print(text, file=sys.stderr)  # stdout belongs to the sandboxed command
        sys.exit(report["exit_code"])
    print(json.dumps(report))
    sys.exit(report["exit_code"])


# --- virtual desktops & sessions: the multi-agent orchestration surface ---

@command
def _cmd_desktop_list(args: argparse.Namespace) -> Out:
    desktops = windows.list_desktops()
    current = windows.current_desktop()
    lines = [f"{'*' if d.number == current else ' '} {d.number}\t{d.name}\t{d.id}"
             for d in desktops]
    return Out([_desktop_dict(d, current) for d in desktops], "\n".join(lines))


@command
def _cmd_desktop_create(args: argparse.Namespace) -> Out:
    desktop = windows.ensure_desktop(args.name) if args.ensure else windows.create_desktop(
        args.name, position=args.position
    )
    return Out(_desktop_dict(desktop), f"{desktop.number}\t{desktop.name}\t{desktop.id}")


@command
def _cmd_desktop_remove(args: argparse.Namespace) -> Out:
    windows.remove_desktop(args.desktop)
    return Out(_ok("remove", desktop=args.desktop))


@command
def _cmd_desktop_current(args: argparse.Namespace) -> Out:
    current = windows.current_desktop()
    return Out({"current": current}, str(current))


@command
def _cmd_desktop_switch(args: argparse.Namespace) -> Out:
    ref = str(args.desktop)
    number = int(ref) if ref.isdigit() else windows.resolve_desktop(ref).number
    _guard_session_desktop(number, args, "switch the screen to")
    windows.set_desktop(number)
    return Out(_ok("switch", desktop=number))


@command
def _cmd_session_init(args: argparse.Namespace) -> Out:
    args.name = args.name or session_mod.current_name()
    if not args.name:
        raise CliError("session name required", hint="pass --name NAME or --session NAME",
                       kind="usage_error")
    session = session_mod.init(
        args.name, desktop=args.desktop, desktop_name=args.desktop_name,
        activate=args.activate,
    )
    text = "\n".join([
        f"session {session.name} ready on desktop {session.desktop} ({session.desktop_name})",
        f"  state:   {session.dir}",
        f"  profile: {session.profile_dir}",
        f"  export CONTROL_SESSION_ID={session.name}",
    ])
    return Out(session.to_dict(), text)


@command
def _cmd_session_status(args: argparse.Namespace) -> Out:
    state = session_mod.status(include_windows=not args.no_windows)
    if not state["sessions"]:
        return Out(state, "no sessions (create one with: control session init --name agent_1)")
    lines = []
    for entry in state["sessions"]:
        marker = "*" if entry["active"] else " "
        missing = "" if entry["desktop_exists"] else " (desktop gone)"
        lines.append(f"{marker} {entry['name']}\tdesktop {entry['desktop']}{missing}\t"
                     f"{len(entry['windows'])} window(s)")
        lines += [f"    {w['id']}\t{w['class']}\t{w['title']}" for w in entry["windows"]]
    return Out(state, "\n".join(lines))


@command
def _cmd_session_list(args: argparse.Namespace) -> Out:
    sessions = session_mod.list_sessions()
    return Out([s.to_dict() for s in sessions],
               "\n".join(f"{s.name}\t{s.desktop}\t{s.desktop_name}" for s in sessions))


@command
def _cmd_session_end(args: argparse.Namespace) -> Out:
    session_mod.end(args.name, remove_desktop=args.remove_desktop)
    return Out(_ok("end", session=args.name, desktop_removed=args.remove_desktop))


@command
def _cmd_snapshot(args: argparse.Namespace) -> Out:
    """Whole-desktop state in one non-blocking pass (always JSON)."""
    state = session_mod.status(include_windows=not args.no_windows)
    state["windows"] = [
        w.to_dict() for w in windows.list_windows("", desktop=_desktop(args))
    ]
    state["current_desktop"] = windows.current_desktop()
    return Out(state, json.dumps(state, indent=2 if args.pretty else None))


# --- browser: small functions that take a Browser/Page so they're testable
# without actually launching a real Chromium context. ---

def _page(browser: Browser):
    return browser.active_tab() or browser.new_tab()


def _load_state(args: argparse.Namespace) -> str | None:
    """--network-idle implies --wait-loaded; neither means "don't wait"."""
    if getattr(args, "network_idle", False):
        return "networkidle"
    return "complete" if getattr(args, "wait_loaded", False) else None


def browser_goto(browser: Browser, url: str, wait: str | None = None,
                 load_timeout: float = browser_mod.DEFAULT_LOAD_TIMEOUT) -> dict:
    page = browser.goto(browser_mod.resolve_url(url))
    data = {"url": page.url, "title": page.title()}
    if wait:
        data["load"] = browser_mod.wait_loaded(page, wait, load_timeout)
        data["url"], data["title"] = page.url, page.title()
    return data


def browser_click(browser: Browser, selector: str, mode: str = "auto", wait: str | None = None,
                  load_timeout: float = browser_mod.DEFAULT_LOAD_TIMEOUT) -> dict:
    page = _page(browser)
    before = browser_mod.document_id(page) if wait else None
    result = browser_mod.click_element(page, selector, mode=mode)
    data = _ok("click", selector=selector, method=result["method"], matches=result["matches"])
    if "note" in result:
        data["note"] = result["note"]
    if wait:
        browser_mod.await_navigation_start(page, before)
        data["load"] = browser_mod.wait_loaded(page, wait, load_timeout)
        data["url"] = page.url
    return data


def browser_check(browser: Browser, selector: str) -> dict:
    return browser_mod.check_element(_page(browser), selector)


def _check_text(data: dict) -> str:
    if not data["exists"]:
        return f"{data['selector']!r}: no match"
    verdict = "interactable" if data["interactable"] else f"NOT interactable: {browser_mod._why_not(data)}"
    return (f"{data['selector']!r}: {data['count']} match(es), <{data['tag']}> "
            f"{data['text']!r} at {data['center']['x']},{data['center']['y']} -- {verdict}")


def browser_dom(browser: Browser, root: str | None = None, ax: bool = False,
                limit: int = browser_mod.DEFAULT_DOM_LIMIT, hidden: bool = False) -> dict:
    page = _page(browser)
    if ax:
        return {"mode": "ax", **browser_mod.accessibility_tree(page, limit=limit)}
    return {"mode": "elements",
            **browser_mod.dom_outline(page, root=root, limit=limit, include_hidden=hidden)}


def _dom_text(data: dict) -> str:
    if data["mode"] == "ax":
        lines = []
        for n in data["nodes"]:
            extra = "".join(f" [{k}={v}]" for k, v in n.items()
                            if k not in ("depth", "role", "name"))
            name = f' "{n["name"]}"' if n["name"] else ""
            lines.append(f"{'  ' * n['depth']}- {n['role']}{name}{extra}")
    else:
        lines = [f"# {data['title']} -- {data['url']}"]
        for e in data["elements"]:
            flags = "".join(f" ({f})" for f in ("hidden", "disabled") if e.get(f))
            if not e["in_viewport"]:
                flags += " (offscreen)"
            if "checked" in e:
                flags += " (checked)" if e["checked"] else " (unchecked)"
            box = e["box"]
            center = f"{box['x'] + box['width'] // 2},{box['y'] + box['height'] // 2}"
            lines.append(f'{e["role"]} "{e["name"]}"{flags}\t{e["selector"]}\t@{center}')
    if data["truncated"]:
        lines.append(f"... {data['total']} total, raise --limit to see more")
    return "\n".join(lines)


def browser_tabs(browser: Browser) -> dict:
    return {"tabs": browser.tabs()}


def _tabs_text(data: dict) -> str:
    return "\n".join(f"{'*' if t['active'] else ' '} {t['index']}\t{t['title']}\t{t['url']}"
                     for t in data["tabs"])


def browser_tab(browser: Browser, pattern: str, wait: str | None = None,
                load_timeout: float = browser_mod.DEFAULT_LOAD_TIMEOUT) -> dict:
    """Bring a tab to the front (in the user's browser too), optionally waiting for it."""
    page = browser.select_tab(pattern)
    page.bring_to_front()
    data = _ok("tab", url=page.url, title=page.title())
    if wait:
        data["load"] = browser_mod.wait_loaded(page, wait, load_timeout)
    return data


def browser_wait_loaded(browser: Browser, state: str,
                        load_timeout: float = browser_mod.DEFAULT_LOAD_TIMEOUT) -> dict:
    return browser_mod.wait_loaded(_page(browser), state, load_timeout)


def browser_fill(browser: Browser, selector: str, text: str) -> dict:
    browser.fill(selector, text)
    return _ok("fill", selector=selector, chars=len(text))


def browser_eval(browser: Browser, expression: str) -> dict:
    return {"result": _page(browser).evaluate(expression)}


def browser_snapshot(browser: Browser, max_chars: int | None = browser_mod.SNAPSHOT_MAX_CHARS) -> dict:
    page = _page(browser)
    text, truncated = browser_mod.compact_text(page.inner_text("body"), max_chars)
    data = {"url": page.url, "text": text}
    if truncated:
        data["truncated"] = True
    return data


def browser_ask(browser: Browser, site: str, question: str, timeout: float,
                settle: float, continue_thread: bool = False) -> dict:
    """Ask an AI site, optionally inside the conversation this profile used last."""
    url = browser_mod.last_thread(browser.profile, site) if continue_thread else None
    result = browser_mod.ask(browser, site, question, timeout=timeout, settle=settle, url=url)
    browser_mod.remember_thread(browser.profile, site, result.url)
    data = result.to_dict()
    data["continued"] = url is not None
    return data


def _ask_text(data: dict) -> str:
    if not data["complete"]:
        print("[WARN] the answer was still changing when --timeout hit; it may be cut off",
              file=sys.stderr)
    return data["response"]


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name, "")
    return int(raw) if raw.isdigit() else None


def _read_question(args: argparse.Namespace) -> str:
    # Cached: stdin can only be read once, and validation reads it first.
    if not hasattr(args, "_question"):
        args._question = _text_arg(args.question)
    return args._question


def _open_browser(args: argparse.Namespace, headless: bool | None = None) -> Browser:
    cdp_port = getattr(args, "cdp_port", None)
    if cdp_port:
        return Browser.attach(port=cdp_port)
    return Browser(headless=args.headless if headless is None else headless)


def _run_on_tab(run: Callable[[Browser, argparse.Namespace], dict]):
    """Honor the browser-level --tab before running a command."""
    def wrapped(b: Browser, args: argparse.Namespace) -> dict:
        if getattr(args, "tab", None):
            b.select_tab(args.tab)
        return run(b, args)

    return wrapped


def _browser_command(run: Callable[[Browser, argparse.Namespace], dict],
                     text: Callable[[dict], str], headless: bool | None = None,
                     validate: Callable[[argparse.Namespace], None] | None = None):
    @command
    def handler(args: argparse.Namespace) -> Out:
        if validate is not None:
            validate(args)  # fail fast, before a Chromium window pops up
        with _open_browser(args, headless) as b:
            data = _run_on_tab(run)(b, args)
        data["profile"] = str(b.profile) if b.profile is not None else None
        if b.cdp_port is not None:
            data["cdp_port"] = b.cdp_port
        return Out(data, text(data))

    return handler


def _eval_text(data: dict) -> str:
    result = data["result"]
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


_cmd_browser_goto = _browser_command(
    lambda b, a: browser_goto(b, a.url, _load_state(a), a.load_timeout), lambda d: d["url"])
_cmd_browser_click = _browser_command(
    lambda b, a: browser_click(b, a.selector, a.mode, _load_state(a), a.load_timeout),
    lambda d: f"clicked {d['selector']!r} ({d['method']})" + (f"; {d['note']}" if "note" in d else ""),
)
_cmd_browser_check = _browser_command(lambda b, a: browser_check(b, a.selector), _check_text)
_cmd_browser_dom = _browser_command(
    lambda b, a: browser_dom(b, a.root, a.ax, a.limit, a.hidden), _dom_text)
_cmd_browser_tabs = _browser_command(lambda b, a: browser_tabs(b), _tabs_text)
_cmd_browser_tab = _browser_command(
    lambda b, a: browser_tab(b, a.pattern, _load_state(a), a.load_timeout),
    lambda d: f"{d['title']}\t{d['url']}",
)
_cmd_browser_wait_loaded = _browser_command(
    lambda b, a: browser_wait_loaded(b, _load_state(a) or "complete", a.load_timeout),
    lambda d: f"{d['state']} after {d['waited']}s: {d['url']}",
)
_cmd_browser_upload = _browser_command(
    lambda b, a: browser_mod.upload_files(_page(b), a.selector, a.files),
    lambda d: f"uploaded {len(d['files'])} file(s) via {d['method']}",
)
_cmd_browser_drop = _browser_command(
    lambda b, a: browser_mod.drop_files(_page(b), a.selector, a.files),
    lambda d: f"dropped {len(d['files'])} file(s); "
              f"{'the page took them' if d['handled'] else 'the page did NOT handle the drop'}",
)
_cmd_browser_fill = _browser_command(lambda b, a: browser_fill(b, a.selector, a.text),
                                     lambda d: f"filled {d['selector']!r}")
_cmd_browser_eval = _browser_command(lambda b, a: browser_eval(b, a.expression), _eval_text)
_cmd_browser_snapshot = _browser_command(
    lambda b, a: browser_snapshot(b, a.max_chars), lambda d: d["text"])
_cmd_browser_ask = _browser_command(
    lambda b, a: browser_ask(b, a.site, _read_question(a), a.timeout, a.settle, a.continue_thread),
    _ask_text,
    validate=lambda a: browser_mod.check_ask(a.site, _read_question(a)),
)
# Always headed: a human has to type the password in the window.
_cmd_browser_login = _browser_command(
    lambda b, a: {"site": browser_mod.get_site(a.site).name, "logged_in": True,
                  "url": browser_mod.wait_for_login(b, a.site, timeout=a.timeout)},
    lambda d: f"logged in to {d['site']}; the session is saved in the browser profile",
    headless=False,
    validate=lambda a: browser_mod.get_site(a.site),
)


@command
def _cmd_browser_brave(args: argparse.Namespace) -> Out:
    info = browser_mod.launch_brave(args.port, args.profile, args.url)
    data = _ok("brave", **info)
    return Out(data, f"Brave debug port {args.port} " + ("started" if info["started"] else "already up")
               + f"; use: --cdp-port {args.port}")


@command
def _cmd_browser_launch(args: argparse.Namespace) -> Out:
    info = browser_mod.launch_browser(args.browser, args.port, args.profile, args.url)
    return Out(_ok("launch", browser=args.browser, **info),
               f"{args.browser} debug port {args.port} "
               + ("started" if info["started"] else "already up")
               + f"; use: --cdp-port {args.port}")


@command
def _cmd_browser_attach(args: argparse.Namespace) -> Out:
    with Browser.attach(port=args.port) as b:
        tabs = [{"url": p.url, "title": p.title()} for p in b.pages]
    data = _ok("attach", port=args.port, tabs=tabs)
    return Out(data, f"attached on port {args.port}: {len(tabs)} tab(s)")


@command
def _cmd_browser_focus_and_goto(args: argparse.Namespace) -> Out:
    """Focus a window, then Ctrl+L + type + Enter; with --wait-loaded, watch the
    navigation over CDP until the new page is ready."""
    url = browser_mod.resolve_url(args.url)
    state = _load_state(args)
    cdp_port = getattr(args, "cdp_port", None)
    if state and not cdp_port:
        raise CliError(
            "--wait-loaded needs --cdp-port: keystrokes alone can't see when the page is ready",
            hint="start the browser with --remote-debugging-port=9222, then: "
                 "control browser --cdp-port 9222 focus-and-goto PATTERN URL --wait-loaded",
            kind="usage_error",
        )
    win = _find_window(args.window, args)
    if not state:
        entered_by = browser_mod.focus_and_goto(win, url)
        return Out(_ok("focus_and_goto", window=win.to_dict(), url=url, entered_by=entered_by),
                   f"{win.id}\t-> {url}")
    with Browser.attach(port=cdp_port) as b:
        before = browser_mod.document_ids(b.pages)
        entered_by = browser_mod.focus_and_goto(win, url)
        page, load = browser_mod.wait_for_navigation(b, url, before, state, args.load_timeout)
        data = _ok("focus_and_goto", window=win.to_dict(), url=page.url, title=page.title(),
                   entered_by=entered_by, load=load, cdp_port=cdp_port)
    return Out(data, f"{win.id}\t-> {page.url} ({state} after {load['waited']}s)")


@command
def _cmd_browser_sites(args: argparse.Namespace) -> Out:
    catalog = browser_mod.sites()
    lines = [f"{s.name}\t{'ask' if s.can_ask else '-'}\t{s.url}" for s in catalog.values()]
    return Out([s.to_dict() for s in catalog.values()], "\n".join(lines))


# --- parser ------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    """ArgumentParser whose usage errors are JSON too when JSON was asked for."""

    def error(self, message: str):  # type: ignore[override]
        if _json_requested():
            print(json.dumps({"error": message, "hint": f"see: {self.prog} --help",
                              "type": "usage_error"}))
            sys.exit(2)
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="control", description="Full desktop control for KDE/Wayland")
    p.add_argument(
        "--session", default=None, metavar="NAME",
        help="act as this agent session (same as CONTROL_SESSION_ID): scopes window "
             "commands to its virtual desktop and uses its browser profile",
    )
    p.add_argument(
        "--env-file", default=None, metavar="PATH",
        help="load this .env before running (per-agent config; overrides the repo .env)",
    )
    p.add_argument(
        "--json", action="store_true", default=False,
        help="machine-readable output for every command, errors included "
             "(also accepted after the subcommand, or via CONTROL_JSON=1)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def leaf(parent, name: str, func, **kwargs) -> argparse.ArgumentParser:
        parser = parent.add_parser(name, **kwargs)
        # SUPPRESS: when absent here, the top-level --json value is kept.
        parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                            help="output JSON instead of text")
        parser.set_defaults(func=func)
        return parser

    def add_desktop_option(parser: argparse.ArgumentParser, creates: bool = False) -> None:
        extra = "; a missing name is created" if creates else ""
        parser.add_argument(
            "--desktop", default=None, metavar="N|NAME",
            help="only act on this virtual desktop (1-based number or name"
                 f"{extra}); defaults to the active session's desktop, or all "
                 "desktops when there is none",
        )

    def add_shot_options(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--crop", type=_box, default=None, metavar="X,Y,W,H",
                            help="keep only this pixel box of the capture")
        parser.add_argument("--scale", type=float, default=1.0,
                            help="shrink the image (e.g. 0.5): fewer image tokens if you must look")
        parser.add_argument("--ocr", action="store_true",
                            help="read the text locally (tesseract) and print 'x,y,w,h text' per "
                                 "line in screen coordinates, so no image needs to be viewed")
        parser.add_argument("--grep", default=None, metavar="REGEX",
                            help="with --ocr, keep only lines matching REGEX")

    def add_window_option(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--window", default=None, metavar="PATTERN",
            help="focus the first window matching PATTERN first; focus and input "
                 "happen under one input lock, so no other agent can cut in",
        )
        add_desktop_option(parser)

    win = sub.add_parser("win", help="window management")
    win_sub = win.add_subparsers(dest="win_command", required=True)

    w = leaf(win_sub, "list", _cmd_win_list, help="list windows matching a pattern (default: all)")
    w.add_argument("pattern", nargs="?", default="")
    add_desktop_option(w)

    leaf(win_sub, "active", _cmd_win_active, help="the currently focused window")

    for name, func in (("activate", _cmd_win_activate), ("close", _cmd_win_close)):
        w = leaf(win_sub, name, func)
        w.add_argument("pattern")
        add_desktop_option(w)

    w = leaf(win_sub, "move", _cmd_win_move)
    w.add_argument("pattern")
    w.add_argument("x")
    w.add_argument("y")
    add_desktop_option(w)

    w = leaf(win_sub, "resize", _cmd_win_resize)
    w.add_argument("pattern")
    w.add_argument("width")
    w.add_argument("height")
    add_desktop_option(w)

    w = leaf(win_sub, "send", _cmd_win_send, help="move a window to another virtual desktop")
    w.add_argument("--force", action="store_true",
                   help="inside a session, allow sending windows off its own desktop")
    w.add_argument("pattern")
    w.add_argument("target", help="destination desktop: 1-based number, or a name "
                                  "(created if it doesn't exist)")
    add_desktop_option(w)

    w = leaf(win_sub, "wait-settled", _cmd_win_wait_settled,
             help="block until a window stops changing (title, geometry, focus): "
                  "use after activate/launch/maximize instead of a blind sleep")
    w.add_argument("pattern")
    w.add_argument("--stable", type=int, default=300, metavar="MS",
                   help="how long nothing may change (default: 300)")
    w.add_argument("--timeout", type=float, default=10.0)
    add_desktop_option(w)

    w = leaf(win_sub, "wait", _cmd_win_wait,
             help="block until a window matching PATTERN exists, then focus it")
    w.add_argument("pattern")
    w.add_argument("--timeout", type=float, default=apps.DEFAULT_WAIT_TIMEOUT)
    w.add_argument("--no-activate", action="store_true", help="don't focus the window found")
    add_desktop_option(w)

    shot = leaf(sub, "shot", _cmd_shot, help="take a screenshot")
    shot.add_argument("mode", choices=["full", "window", "region"], default="full", nargs="?")
    add_shot_options(shot)
    add_window_option(shot)

    app = sub.add_parser("app", help="find/launch applications")
    app_sub = app.add_subparsers(dest="app_command", required=True)

    a = leaf(app_sub, "list", _cmd_apps_list)
    a.add_argument("query", nargs="?", default="")

    a = leaf(app_sub, "launch", _cmd_apps_launch)
    a.add_argument("app_id")
    a.add_argument(
        "--wait", action="store_true",
        help="block until a *new* window for this app appears (pre-existing ones "
             "are ignored), then focus it",
    )
    a.add_argument(
        "--timeout", type=float, default=apps.DEFAULT_WAIT_TIMEOUT,
        help=f"seconds to wait with --wait (default: {apps.DEFAULT_WAIT_TIMEOUT})",
    )
    a.add_argument(
        "--window-pattern", default=None,
        help="regex to match the new window against with --wait (default: app_id)",
    )
    a.add_argument("--no-activate", action="store_true",
                   help="leave focus where it is once the window appears")
    add_desktop_option(a, creates=True)

    click = leaf(sub, "click", _cmd_click)
    click.add_argument("--button", default="left", choices=["left", "right", "middle"])
    click.add_argument("-x", type=int, default=None)
    click.add_argument("-y", type=int, default=None)
    click.add_argument("--text", default=None, metavar="REGEX",
                       help="click the centre of on-screen text found by OCR (case-insensitive "
                            "regex) instead of -x/-y; searches --window if given, else the screen")
    click.add_argument("--nth", type=int, default=1, metavar="N",
                       help="with --text: pick the Nth match in reading order (default 1)")
    click.add_argument("--then-shot", default=None, metavar="PATTERN|full",
                       help="after the click, capture that window (or 'full') in the same call; "
                            "combine with --ocr/--scale/--crop/--grep")
    add_shot_options(click)
    add_window_option(click)

    typ = leaf(sub, "type", _cmd_type)
    typ.add_argument("text", help="text to type, or '-' to read it (UTF-8) from stdin")
    typ.add_argument("--delay", type=int, default=0, metavar="MS", help="delay between keys")
    typ.add_argument(
        "--method", choices=inp.TEXT_METHODS, default="auto",
        help="auto (default): type plain ASCII letters/digits, paste through the clipboard "
             "anything with accents, ñ, symbols or other Unicode (ydotool drops or "
             "remaps those); paste/type: force one. Use type for secrets: a clipboard "
             "manager may keep pasted text in its history",
    )
    typ.add_argument("--paste-keys", default=None, metavar="KEYS",
                     help="paste shortcut, e.g. ctrl+shift+v (default: ctrl+shift+v in "
                          "terminals, ctrl+v elsewhere)")
    add_window_option(typ)

    key = leaf(
        sub, "key", _cmd_key,
        help="press a key or key combo",
        description=(
            "Press one or more keys together, in order, then release in reverse order. "
            "Combine modifiers by listing them before the final key, e.g.:\n"
            "  control key ctrl shift t\n"
            "  control key ctrl alt delete\n"
            "  control key super"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    key.add_argument(
        "keys", nargs="*",
        help="one or more key names, e.g. ctrl shift t (aliases like Return, Escape, "
             "PgUp work too; see --list)",
    )
    key.add_argument("--list", action="store_true", help="print every key name and alias")
    add_window_option(key)

    drag = leaf(sub, "drag", _cmd_drag,
                help="press, glide and release the mouse (e.g. drag a file from Dolphin "
                     "onto a web page); the button is always released, even on errors")
    drag.add_argument("--from", dest="start", type=_point, required=True, metavar="X,Y")
    drag.add_argument("--then-shot", default=None, metavar="PATTERN|full",
                      help="after the drag, capture that window (or 'full')")
    add_shot_options(drag)
    drag.add_argument("--to", dest="end", type=_point, required=True, metavar="X,Y")
    drag.add_argument("--button", default="left", choices=["left", "right", "middle"])
    drag.add_argument("--steps", type=int, default=inp.DEFAULT_DRAG_STEPS,
                      help="intermediate pointer moves; drop targets need them to see "
                           f"dragover (default: {inp.DEFAULT_DRAG_STEPS})")
    drag.add_argument("--hold", type=int, default=inp.DEFAULT_DRAG_HOLD_MS, metavar="MS",
                      help="pause after pressing and before releasing "
                           f"(default: {inp.DEFAULT_DRAG_HOLD_MS})")
    add_window_option(drag)

    cur = sub.add_parser(
        "cursor",
        help="Marionette's own on-screen pointer (an overlay arrow the agent moves; "
             "the user's real pointer is only borrowed for clicks)")
    cur_sub = cur.add_subparsers(dest="cursor_command", required=True)
    c = leaf(cur_sub, "show", _cmd_cursor_show, help="show the agent cursor")
    c.add_argument("--color", default="#d97757", help="arrow color (default: %(default)s)")
    leaf(cur_sub, "hide", _cmd_cursor_hide, help="remove the agent cursor")
    leaf(cur_sub, "status", _cmd_cursor_status, help="is it visible, and where")
    c = leaf(cur_sub, "move", _cmd_cursor_move, help="glide the agent cursor to X Y")
    c.add_argument("x", type=int)
    c.add_argument("y", type=int)
    c.add_argument("--duration", type=float, default=cursor_mod.DEFAULT_MOVE_SECONDS,
                   help="glide time in seconds, 0 to jump (default: %(default)s)")
    c = leaf(cur_sub, "click", _cmd_cursor_click,
             help="click at the agent cursor (or at -x/-y after moving there); the real "
                  "pointer returns to where the user left it")
    c.add_argument("--button", default="left", choices=["left", "right", "middle"])
    c.add_argument("-x", type=int, default=None)
    c.add_argument("-y", type=int, default=None)
    c.add_argument("--duration", type=float, default=cursor_mod.DEFAULT_MOVE_SECONDS)
    c.add_argument("--no-restore", action="store_true",
                   help="leave the real pointer on the click target")

    clip = sub.add_parser("clip", help="Wayland clipboard (wl-copy/wl-paste)")
    clip_sub = clip.add_subparsers(dest="clip_command", required=True)
    c = leaf(clip_sub, "set", _cmd_clip_set,
             help="copy text, or files as text/uri-list (paste them into a page instead "
                  "of going through a file dialog)")
    c.add_argument("text", nargs="?", default=None, help="text to copy, or '-' for stdin")
    c.add_argument("--file", dest="files", action="append", default=[], metavar="PATH",
                   help="copy this file (repeatable), like a file manager's Copy")
    c.add_argument("--mime", default=None, help="MIME type to announce for TEXT")
    c.add_argument("--paste", action="store_true",
                   help="press Ctrl+V right after copying (into --window if given)")
    c.add_argument("--paste-keys", default=None, metavar="KEYS",
                   help="paste shortcut (default: ctrl+shift+v in terminals, ctrl+v elsewhere)")
    add_window_option(c)
    c = leaf(clip_sub, "get", _cmd_clip_get, help="print the clipboard contents")
    c.add_argument("--mime", default=None, help="ask for this MIME type (e.g. text/uri-list)")

    desktop = sub.add_parser("desktop", help="KDE virtual desktops (create/remove/switch)")
    desktop_sub = desktop.add_subparsers(dest="desktop_command")
    desktop.set_defaults(func=_cmd_desktop_list)  # bare `desktop` lists

    leaf(desktop_sub, "list", _cmd_desktop_list)

    d = leaf(desktop_sub, "create", _cmd_desktop_create, help="create a virtual desktop on demand")
    d.add_argument("name")
    d.add_argument("--position", type=int, default=None,
                   help="0-based insert position (default: append at the end)")
    d.add_argument("--ensure", action="store_true",
                   help="reuse an existing desktop with this name instead of failing")

    d = leaf(desktop_sub, "remove", _cmd_desktop_remove)
    d.add_argument("desktop", help="desktop name or 1-based number")

    leaf(desktop_sub, "current", _cmd_desktop_current)

    d = leaf(desktop_sub, "switch", _cmd_desktop_switch)
    d.add_argument("desktop", metavar="N|NAME", help="desktop number (1-based) or name")
    d.add_argument("--force", action="store_true",
                   help="inside a session, allow switching to a desktop other than its own")

    sb = leaf(sub, "sandbox", _cmd_sandbox,
              help="run a command as an agent on a throwaway virtual desktop, then close "
                   "the windows it opened there and remove the desktop")
    sb.add_argument("--name", default=None, help="session name (default: sandbox-<random>)")
    sb.add_argument("--keep-windows", action="store_true",
                    help="leave the windows (and so the desktop) in place afterwards")
    sb.add_argument("--timeout", type=float, default=None, metavar="S",
                    help="stop the command after S seconds (SIGTERM, then SIGKILL), close its "
                         "windows, end apps that ignore that, and remove the desktop; exit 124")
    sb.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- COMMAND ...")

    sess = sub.add_parser("session", help="multi-agent workspaces (one desktop + profile each)")
    sess_sub = sess.add_subparsers(dest="session_command", required=True)

    sc = leaf(sess_sub, "init", _cmd_session_init, help="prepare (or re-attach to) an agent workspace")
    sc.add_argument("--name", default=None,
                    help="session name (default: the global --session / CONTROL_SESSION_ID)")
    sc.add_argument("--desktop", default=None, metavar="N|NAME",
                    help="pin to this desktop (number, or name -- created if missing) "
                         "instead of creating control:<name>")
    sc.add_argument("--desktop-name", default=None,
                    help="name for the created desktop (default: control:<name>)")
    sc.add_argument("--activate", action="store_true", help="switch to the desktop now")

    sc = leaf(sess_sub, "status", _cmd_session_status, help="which agent owns which desktop and windows")
    sc.add_argument("--no-windows", action="store_true", help="skip the per-desktop window query")

    leaf(sess_sub, "list", _cmd_session_list)

    sc = leaf(sess_sub, "end", _cmd_session_end, help="forget a session (its profile and log are kept)")
    sc.add_argument("--name", required=True)
    sc.add_argument("--remove-desktop", action="store_true",
                    help="also delete the session's virtual desktop")

    a11y = sub.add_parser("a11y", help="native apps (Qt/GTK/Electron) through the AT-SPI "
                                       "accessibility tree: read and operate widgets by name")
    a11y_sub = a11y.add_subparsers(dest="a11y_command", required=True)

    def a11y_target(parser: argparse.ArgumentParser, element: bool = True) -> None:
        parser.add_argument("--app", default=None, help="application name (substring)")
        parser.add_argument("--pid", type=int, default=None)
        parser.add_argument("--window", default=None, metavar="PATTERN",
                            help="the app owning this window (only its frame is walked); "
                                 "nothing gets focused")
        add_desktop_option(parser)
        if not element:
            return
        parser.add_argument("--path", default=None, help="element path from 'a11y tree', e.g. /0/3/1")
        parser.add_argument("--role", default=None, help="role contains, e.g. 'push button', entry")
        parser.add_argument("--name", default=None, help="accessible name contains (case-insensitive)")
        parser.add_argument("--text", default=None, help="text content contains")
        parser.add_argument("--state", action="append", default=[], metavar="STATE",
                            help="required state, e.g. focused, checked (repeatable)")
        parser.add_argument("--interactive", action="store_true",
                            help="only elements with actions or that are editable/focusable")
        parser.add_argument("--nth", type=int, default=None, help="pick the Nth match (0-based)")
        parser.add_argument("--all", action="store_true", help="include hidden (not showing) elements")

    leaf(a11y_sub, "status", _cmd_a11y_status,
         help="is the bus on, can the worker run, which apps publish a tree")
    x = leaf(a11y_sub, "enable", _cmd_a11y_enable,
             help="turn the accessibility bus on for this login session (--off to turn it off)")
    x.add_argument("--off", action="store_true")
    x = leaf(a11y_sub, "apps", _cmd_a11y_apps, help="applications on the accessibility bus")
    x.add_argument("--all", action="store_true", help="include apps that publish no widgets")

    x = leaf(a11y_sub, "tree", _cmd_a11y_tree,
             help="an app's widgets: role, name, states, actions, text and path "
                  "(with --role/--name/... a flat list of matches)")
    a11y_target(x)
    x.add_argument("--depth", type=int, default=None, help="max depth below --path")
    x.add_argument("--limit", type=int, default=None, help="max elements (default: 300)")

    for name, func, text in (
        ("get", _cmd_a11y_get, "everything about one element (full text, value, all states)"),
        ("read", _cmd_a11y_read, "just an element's content: its text, else value, else name"),
        ("focus", _cmd_a11y_focus, "give an element keyboard focus (then: control type)"),
    ):
        a11y_target(leaf(a11y_sub, name, func, help=text))

    x = leaf(a11y_sub, "do", _cmd_a11y_do,
             help="run an element's action (Press, Toggle, Activate...) inside the app: "
                  "no coordinates, no focus needed")
    a11y_target(x)
    x.add_argument("--action", default=None,
                   help="action name or index (default: the first one listed)")

    x = leaf(a11y_sub, "set-text", _cmd_a11y_set_text,
             help="replace an editable field's text through AT-SPI")
    a11y_target(x)
    x.add_argument("value", help="the new text, or '-' for stdin (UTF-8)")

    x = leaf(a11y_sub, "wait", _cmd_a11y_wait,
             help="block until an element appears (or --gone), and/or until the app's "
                  "visible tree stops changing (--settle)")
    a11y_target(x)
    x.add_argument("--gone", action="store_true", help="wait for the match to disappear")
    x.add_argument("--settle", type=int, default=0, metavar="MS",
                   help="then wait until the visible tree is unchanged for MS")
    x.add_argument("--timeout", type=float, default=10.0)

    snap = leaf(
        sub, "snapshot", _cmd_snapshot,
        help="full desktop state as JSON (desktops, sessions, windows) -- non-blocking",
    )
    snap.add_argument("--pretty", action="store_true")
    snap.add_argument("--no-windows", action="store_true", help="skip per-session window queries")
    add_desktop_option(snap)

    browser = sub.add_parser("browser", help="DOM-precise browser control (Playwright/Chromium)")
    browser.add_argument("--headless", action="store_true", help="run Chromium headless")
    browser.add_argument(
        "--cdp-port", type=int, default=_env_int("CONTROL_CDP_PORT"), metavar="PORT",
        help="drive a Chromium/Brave already running (started with "
             "--remote-debugging-port=PORT) instead of launching one; commands then "
             "act on the tab in front, and focus-and-goto can --wait-loaded",
    )
    browser.add_argument(
        "--tab", default=None, metavar="PATTERN",
        help="act on the tab whose URL or title contains PATTERN (or tab number N "
             "from 'browser tabs') instead of the one in front",
    )
    browser_sub = browser.add_subparsers(dest="browser_command", required=True)

    def add_load_options(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--wait-loaded", action="store_true",
                            help="return only once document.readyState is 'complete'")
        parser.add_argument("--network-idle", action="store_true",
                            help="like --wait-loaded, and also wait until no request has "
                                 f"finished for {browser_mod.DEFAULT_NETWORK_IDLE}s (SPAs)")
        parser.add_argument("--load-timeout", type=float,
                            default=browser_mod.DEFAULT_LOAD_TIMEOUT, metavar="S",
                            help=f"(default: {browser_mod.DEFAULT_LOAD_TIMEOUT:g})")

    b = leaf(browser_sub, "goto", _cmd_browser_goto,
             help="navigate the active tab to a URL, host, or AI site alias (e.g. gemini)")
    b.add_argument("url")
    add_load_options(b)

    b = leaf(browser_sub, "click", _cmd_browser_click,
             help="click an element by CSS selector, after checking it exists and can be clicked")
    b.add_argument("selector")
    b.add_argument("--mode", choices=browser_mod.CLICK_MODES, default="auto",
                   help="native: trusted CDP mouse click; js: pointer/mouse events + el.click() "
                        "in the page (React/Vue, hidden or covered targets); auto (default): "
                        "native, falling back to js when native can't reach the element")
    b.add_argument("--js", dest="mode", action="store_const", const="js",
                   help="shorthand for --mode js")
    add_load_options(b)

    b = leaf(browser_sub, "check", _cmd_browser_check,
             help="does SELECTOR exist, and is it visible, enabled, in view and uncovered?")
    b.add_argument("selector")

    b = leaf(browser_sub, "dom", _cmd_browser_dom,
             help="list the page's clickable elements with unique selectors (or, with "
                  "--ax, its accessibility tree via CDP)")
    b.add_argument("--root", default=None, metavar="SELECTOR",
                   help="only look inside this element (e.g. a dialog)")
    b.add_argument("--ax", action="store_true",
                   help="accessibility tree (Accessibility.getFullAXTree) instead")
    b.add_argument("--limit", type=int, default=browser_mod.DEFAULT_DOM_LIMIT,
                   help=f"max entries (default: {browser_mod.DEFAULT_DOM_LIMIT})")
    b.add_argument("--hidden", action="store_true", help="include hidden elements too")

    leaf(browser_sub, "tabs", _cmd_browser_tabs, help="list open tabs; * marks the one in front")

    b = leaf(browser_sub, "tab", _cmd_browser_tab,
             help="switch to (bring to front) the tab matching PATTERN or number N")
    b.add_argument("pattern")
    add_load_options(b)

    b = leaf(browser_sub, "wait-loaded", _cmd_browser_wait_loaded,
             help="block until the active tab has finished loading")
    b.add_argument("--network-idle", action="store_true",
                   help="also wait until the network has been idle for a moment")
    b.add_argument("--load-timeout", type=float, default=browser_mod.DEFAULT_LOAD_TIMEOUT,
                   metavar="S")

    b = leaf(browser_sub, "upload", _cmd_browser_upload,
             help="give files to a file <input> or to the button that opens the file "
                  "chooser -- the native dialog never opens")
    b.add_argument("selector")
    b.add_argument("files", nargs="+", metavar="FILE")

    b = leaf(browser_sub, "drop", _cmd_browser_drop,
             help="drop files onto a drag-and-drop zone from inside the page "
                  "(no mouse, no file manager)")
    b.add_argument("selector")
    b.add_argument("files", nargs="+", metavar="FILE")

    b = leaf(browser_sub, "fill", _cmd_browser_fill, help="fill a form field by CSS selector")
    b.add_argument("selector")
    b.add_argument("text")

    b = leaf(browser_sub, "eval", _cmd_browser_eval, help="evaluate a JS expression on the active page")
    b.add_argument("expression")

    b = leaf(browser_sub, "snapshot", _cmd_browser_snapshot,
             help="dump the visible text of the active page (capped, blank lines collapsed)")
    b.add_argument("--max-chars", type=int, default=browser_mod.SNAPSHOT_MAX_CHARS, metavar="N",
                   help=f"truncate to N chars, 0 = no cap (default: {browser_mod.SNAPSHOT_MAX_CHARS})")

    b = leaf(browser_sub, "brave", _cmd_browser_brave,
             help="start Brave with a loopback debug port (dedicated profile) if not already "
                  "reachable; afterwards use --cdp-port or CONTROL_CDP_PORT")
    b.add_argument("--port", type=int, default=browser_mod.DEFAULT_CDP_PORT)
    b.add_argument("--profile", default=None, metavar="DIR", help="user-data-dir for Brave")
    b.add_argument("--url", default=None, help="open this URL on start")

    b = leaf(browser_sub, "launch", _cmd_browser_launch,
             help="start a persistent browser (chromium = Playwright's bundled one, or brave) "
                  "with a loopback debug port; tabs survive between calls via --cdp-port")
    b.add_argument("--browser", choices=["chromium", "brave"], default="chromium")
    b.add_argument("--port", type=int, default=browser_mod.DEFAULT_CDP_PORT)
    b.add_argument("--profile", default=None, metavar="DIR", help="user-data-dir")
    b.add_argument("--url", default=None, help="open this URL on start")

    b = leaf(browser_sub, "attach", _cmd_browser_attach,
             help="connect to a Chromium already running with --remote-debugging-port "
                  "and list its open tabs")
    b.add_argument("--port", type=int, default=browser_mod.DEFAULT_CDP_PORT,
                   help=f"debug port (default: {browser_mod.DEFAULT_CDP_PORT})")

    b = leaf(browser_sub, "focus-and-goto", _cmd_browser_focus_and_goto,
             help="fallback for browsers with no CDP access: focus a window by pattern "
                  "and type a URL via Ctrl+L")
    b.add_argument("window", metavar="PATTERN", help="window title/class pattern to focus")
    b.add_argument("url")
    add_load_options(b)
    add_desktop_option(b)

    leaf(browser_sub, "sites", _cmd_browser_sites, help="list the AI site shortcuts")

    b = leaf(browser_sub, "ask", _cmd_browser_ask,
             help="ask an AI chat site a question and print its answer")
    b.add_argument("site", help="chatgpt, gemini or claude (see: control browser sites)")
    b.add_argument("question", help="the question, or '-' to read it from stdin")
    b.add_argument("--timeout", type=float, default=browser_mod.DEFAULT_ASK_TIMEOUT,
                   help=f"seconds to wait for the full answer (default: "
                        f"{browser_mod.DEFAULT_ASK_TIMEOUT:.0f})")
    b.add_argument("--settle", type=float, default=browser_mod.DEFAULT_SETTLE,
                   help="seconds the answer must stay unchanged to count as finished")
    b.add_argument("--continue", dest="continue_thread", action="store_true",
                   help="ask inside the last conversation with this site, not a new chat")

    b = leaf(browser_sub, "login", _cmd_browser_login,
             help="open an AI site (headed) and wait while you log in by hand")
    b.add_argument("site")
    b.add_argument("--timeout", type=float, default=300.0)

    return p


# --- entry point -------------------------------------------------------------

def _snake_case(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


_ERROR_HINTS: dict[type, str] = {
    windows.KdotoolError: "check that kdotool is installed and KWin scripting is enabled",
    windows.DesktopError: "list desktops with 'control desktop list' (KWin's D-Bus API must be reachable)",
    inp.YdotoolError: "check ydotoold is running: systemctl status ydotoold",
    inp.ClipboardError: "install wl-clipboard (sudo pacman -S wl-clipboard) and run inside "
                        "the Wayland session (WAYLAND_DISPLAY must be set)",
    inp.LockTimeout: (
        "another control process held the input lock too long; check with "
        "'control session status', or raise CONTROL_INPUT_LOCK_TIMEOUT"
    ),
    session_mod.SessionError: "list known sessions with: control session list",
    apps.AppLaunchError: "check the app id with: control app list",
    screenshot.ScreenshotError: "check that spectacle is installed and works: spectacle -b -n -f",
    env.EnvPermissionError: "restrict the file to its owner: chmod 600 <file>",
    browser_mod.BrowserError: "browser automation failed",
    atspi.AtspiError: "check the accessibility setup with: control a11y status",
    PlaywrightError: "browser action failed; check the selector/URL and that the page finished loading",
}


def _hint_for(exc: BaseException) -> str:
    if getattr(exc, "hint", ""):
        return exc.hint  # errors that know their own next step (browser.py)
    for klass in type(exc).__mro__:
        if klass in _ERROR_HINTS:
            return _ERROR_HINTS[klass]
    return ""


def _report(args: argparse.Namespace | None, kind: str, message: str, hint: str) -> None:
    if (args is not None and _json_mode(args)) or (args is None and _json_requested()):
        print(json.dumps({"error": message, "hint": hint, "type": kind}))
    else:
        suffix = f" ({hint})" if hint else ""
        print(f"[ERROR] {kind}: {message}{suffix}", file=sys.stderr)


def bootstrap(args: argparse.Namespace) -> None:
    """Apply the global --session/--env-file options before any command runs.

    Env precedence, highest first: variables already exported in the shell, the
    explicit --env-file, the active session's own .env, then the repo .env. That
    ordering lets each agent terminal override shared defaults without editing
    (or racing on) the repo-level file. Secrets in any of them stay out of
    os.environ (see env.py).
    """
    if getattr(args, "session", None):
        os.environ["CONTROL_SESSION_ID"] = args.session
    if getattr(args, "env_file", None):
        path = Path(args.env_file)
        if not path.is_file():
            raise CliError(f"env file not found: {path}", hint="check the --env-file path",
                           kind="env_file_not_found")
        load_env(path, override=True)
    name = session_mod.current_name()
    if name:
        session_env = session_mod.session_dir(name) / ".env"
        if session_env.is_file():
            load_env(session_env)
    load_env()


def _command_path(args: argparse.Namespace) -> str:
    top = str(getattr(args, "command", "?"))
    return " ".join(filter(None, [top, str(getattr(args, f"{top}_command", "") or "")]))


def _trace(args: argparse.Namespace, outcome: str) -> None:
    """One line per command in the session's own log, so agents' traces never mix.

    Only the command path and outcome are logged -- never arguments, which can
    hold typed text or form contents.
    """
    name = session_mod.current_name()
    if not name:
        return
    try:
        session = session_mod.load(name)
        if session is not None:
            session.log(f"{_command_path(args)} -> {outcome}")
    except OSError:
        pass


def main() -> None:
    _utf8_stdio()
    parser = build_parser()
    args = parser.parse_args([utf8_arg(a) for a in sys.argv[1:]])
    try:
        bootstrap(args)
        args.func(args)
    except CliError as exc:
        _report(args, exc.kind, exc.message, exc.hint)
        _trace(args, exc.kind)
        sys.exit(1)
    except SystemExit:
        raise
    except tuple(_ERROR_HINTS) as exc:
        kind = _snake_case(type(exc).__name__)
        _report(args, kind, str(exc), _hint_for(exc))
        _trace(args, kind)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, but reported not hidden
        _trace(args, "unexpected")
        if _json_mode(args) and not _truthy(os.environ.get("CONTROL_DEBUG")):
            _report(args, "unexpected", f"{type(exc).__name__}: {exc}",
                    "set CONTROL_DEBUG=1 to get the traceback")
            sys.exit(1)
        print(f"[ERROR] unexpected: {exc}", file=sys.stderr)
        raise
    else:
        _trace(args, "ok")


if __name__ == "__main__":
    main()
