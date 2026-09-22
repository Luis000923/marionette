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
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Error as PlaywrightError

from . import apps, env, input as inp, screenshot, session as session_mod, windows
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
            print(json.dumps(out.data, indent=2 if getattr(args, "pretty", False) else None))
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


@command
def _cmd_win_send(args: argparse.Namespace) -> Out:
    """Move a matching window onto another virtual desktop (created if named and missing)."""
    win = _require_window(args)
    target = str(args.target)
    number = int(target) if target.isdigit() else windows.resolve_desktop(target, create=True).number
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


# --- screenshots ---------------------------------------------------------------

@command
def _cmd_shot(args: argparse.Namespace) -> Out:
    fn = {"full": screenshot.fullscreen, "window": screenshot.active_window,
          "region": screenshot.region}[args.mode]
    data: dict[str, Any] = {"mode": args.mode}
    if args.window:
        # "window" mode captures whatever is focused, so focus + capture must
        # not be split by another agent's activation.
        with inp.input_lock():
            win = _find_window(args.window, args)
            win.focus()
            path = fn()
        data["window"] = win.to_dict()
    else:
        path = fn()
    data["path"] = str(path)
    return Out(data, str(path))


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


# --- input -------------------------------------------------------------------

@command
def _cmd_click(args: argparse.Namespace) -> Out:
    return _focused_input(args, "click", lambda: inp.click(args.button, args.x, args.y),
                          button=args.button, x=args.x, y=args.y)


@command
def _cmd_type(args: argparse.Namespace) -> Out:
    text = sys.stdin.read() if args.text == "-" else args.text
    # Only the length is echoed back: the text may be a password, and repeating
    # it would cost the agent tokens for nothing.
    return _focused_input(args, "type", lambda: inp.type_text(text, delay_ms=args.delay),
                          chars=len(text))


@command
def _cmd_key(args: argparse.Namespace) -> Out:
    unknown = [k for k in args.keys if k.lower() not in inp.KEYCODES]
    if unknown:
        suggestions = difflib.get_close_matches(unknown[0].lower(), inp.KEYCODES, n=3)
        raise CliError(
            f"unknown key name(s): {', '.join(unknown)}",
            hint=f"did you mean: {', '.join(suggestions)}" if suggestions
            else "see input.KEYCODES for valid names",
            kind="unknown_key",
        )
    return _focused_input(args, "key", lambda: inp.key_combo(*args.keys), keys=args.keys)


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
    windows.set_desktop(number)
    return Out(_ok("switch", desktop=number))


@command
def _cmd_session_init(args: argparse.Namespace) -> Out:
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


def browser_goto(browser: Browser, url: str) -> dict:
    page = browser.goto(browser_mod.resolve_url(url))
    return {"url": page.url, "title": page.title()}


def browser_click(browser: Browser, selector: str) -> dict:
    browser.click(selector)
    return _ok("click", selector=selector)


def browser_fill(browser: Browser, selector: str, text: str) -> dict:
    browser.fill(selector, text)
    return _ok("fill", selector=selector, chars=len(text))


def browser_eval(browser: Browser, expression: str) -> dict:
    return {"result": _page(browser).evaluate(expression)}


def browser_snapshot(browser: Browser) -> dict:
    page = _page(browser)
    return {"url": page.url, "text": page.inner_text("body")}


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


def _read_question(args: argparse.Namespace) -> str:
    # Cached: stdin can only be read once, and validation reads it first.
    if not hasattr(args, "_question"):
        args._question = sys.stdin.read() if args.question == "-" else args.question
    return args._question


def _open_browser(args: argparse.Namespace, headless: bool | None = None) -> Browser:
    cdp_port = getattr(args, "cdp_port", None)
    if cdp_port:
        return Browser.attach(port=cdp_port)
    return Browser(headless=args.headless if headless is None else headless)


def _browser_command(run: Callable[[Browser, argparse.Namespace], dict],
                     text: Callable[[dict], str], headless: bool | None = None,
                     validate: Callable[[argparse.Namespace], None] | None = None):
    @command
    def handler(args: argparse.Namespace) -> Out:
        if validate is not None:
            validate(args)  # fail fast, before a Chromium window pops up
        with _open_browser(args, headless) as b:
            data = run(b, args)
        data["profile"] = str(b.profile) if b.profile is not None else None
        if b.cdp_port is not None:
            data["cdp_port"] = b.cdp_port
        return Out(data, text(data))

    return handler


def _eval_text(data: dict) -> str:
    result = data["result"]
    return result if isinstance(result, str) else json.dumps(result)


_cmd_browser_goto = _browser_command(lambda b, a: browser_goto(b, a.url), lambda d: d["url"])
_cmd_browser_click = _browser_command(lambda b, a: browser_click(b, a.selector),
                                      lambda d: f"clicked {d['selector']!r}")
_cmd_browser_fill = _browser_command(lambda b, a: browser_fill(b, a.selector, a.text),
                                     lambda d: f"filled {d['selector']!r}")
_cmd_browser_eval = _browser_command(lambda b, a: browser_eval(b, a.expression), _eval_text)
_cmd_browser_snapshot = _browser_command(lambda b, a: browser_snapshot(b), lambda d: d["text"])
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
def _cmd_browser_attach(args: argparse.Namespace) -> Out:
    with Browser.attach(port=args.port) as b:
        tabs = [{"url": p.url, "title": p.title()} for p in b.pages]
    data = _ok("attach", port=args.port, tabs=tabs)
    return Out(data, f"attached on port {args.port}: {len(tabs)} tab(s)")


@command
def _cmd_browser_focus_and_goto(args: argparse.Namespace) -> Out:
    """Fallback for browsers without CDP: focus a window, then Ctrl+L + type + Enter."""
    url = browser_mod.resolve_url(args.url)
    win = _find_window(args.window, args)
    browser_mod.focus_and_goto(win, url)
    return Out(_ok("focus_and_goto", window=win.to_dict(), url=url), f"{win.id}\t-> {url}")


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
    w.add_argument("pattern")
    w.add_argument("target", help="destination desktop: 1-based number, or a name "
                                  "(created if it doesn't exist)")
    add_desktop_option(w)

    w = leaf(win_sub, "wait", _cmd_win_wait,
             help="block until a window matching PATTERN exists, then focus it")
    w.add_argument("pattern")
    w.add_argument("--timeout", type=float, default=apps.DEFAULT_WAIT_TIMEOUT)
    w.add_argument("--no-activate", action="store_true", help="don't focus the window found")
    add_desktop_option(w)

    shot = leaf(sub, "shot", _cmd_shot, help="take a screenshot")
    shot.add_argument("mode", choices=["full", "window", "region"], default="full", nargs="?")
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
    add_window_option(click)

    typ = leaf(sub, "type", _cmd_type)
    typ.add_argument("text", help="text to type, or '-' to read it from stdin")
    typ.add_argument("--delay", type=int, default=0, metavar="MS", help="delay between keys")
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
        "keys", nargs="+",
        help="one or more key names (see input.KEYCODES), e.g. ctrl shift t",
    )
    add_window_option(key)

    desktop = sub.add_parser("desktop", help="KDE virtual desktops (create/remove/switch)")
    desktop_sub = desktop.add_subparsers(dest="desktop_command", required=True)

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

    sess = sub.add_parser("session", help="multi-agent workspaces (one desktop + profile each)")
    sess_sub = sess.add_subparsers(dest="session_command", required=True)

    sc = leaf(sess_sub, "init", _cmd_session_init, help="prepare (or re-attach to) an agent workspace")
    sc.add_argument("--name", required=True)
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
        "--cdp-port", type=int, default=None, metavar="PORT",
        help="drive a Chromium already running (started with "
             "--remote-debugging-port=PORT) instead of launching one; "
             "applies to goto/click/fill/eval/snapshot",
    )
    browser_sub = browser.add_subparsers(dest="browser_command", required=True)

    b = leaf(browser_sub, "goto", _cmd_browser_goto,
             help="navigate the active tab to a URL, host, or AI site alias (e.g. gemini)")
    b.add_argument("url")

    b = leaf(browser_sub, "click", _cmd_browser_click, help="click an element by CSS selector")
    b.add_argument("selector")

    b = leaf(browser_sub, "fill", _cmd_browser_fill, help="fill a form field by CSS selector")
    b.add_argument("selector")
    b.add_argument("text")

    b = leaf(browser_sub, "eval", _cmd_browser_eval, help="evaluate a JS expression on the active page")
    b.add_argument("expression")

    leaf(browser_sub, "snapshot", _cmd_browser_snapshot, help="dump the visible text of the active page")

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
    inp.LockTimeout: (
        "another control process held the input lock too long; check with "
        "'control session status', or raise CONTROL_INPUT_LOCK_TIMEOUT"
    ),
    session_mod.SessionError: "list known sessions with: control session list",
    apps.AppLaunchError: "check the app id with: control app list",
    screenshot.ScreenshotError: "check that spectacle is installed and works: spectacle -b -n -f",
    env.EnvPermissionError: "restrict the file to its owner: chmod 600 <file>",
    browser_mod.BrowserError: "browser automation failed",
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
    parser = build_parser()
    args = parser.parse_args()
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
