"""Unified CLI for desktop control: windows, input, screenshots, apps, browser."""
from __future__ import annotations

import argparse
import json
import sys

from playwright.sync_api import Error as PlaywrightError

from . import apps, input as inp, screenshot, windows
from .browser import Browser


def _cmd_win_list(args: argparse.Namespace) -> None:
    results = windows.search(args.pattern or "")
    if args.json:
        print(json.dumps([
            {"id": w.id, "title": w.title, "class": w.classname} for w in results
        ]))
        return
    for w in results:
        print(f"{w.id}\t{w.classname}\t{w.title}")


def _cmd_win_activate(args: argparse.Namespace) -> None:
    win = windows.find_one(args.pattern)
    if not win:
        sys.exit(f"no window matching {args.pattern!r}")
    win.activate()


def _cmd_win_close(args: argparse.Namespace) -> None:
    win = windows.find_one(args.pattern)
    if not win:
        sys.exit(f"no window matching {args.pattern!r}")
    win.close()


def _cmd_win_move(args: argparse.Namespace) -> None:
    win = windows.find_one(args.pattern)
    if not win:
        sys.exit(f"no window matching {args.pattern!r}")
    win.move(args.x, args.y)


def _cmd_win_resize(args: argparse.Namespace) -> None:
    win = windows.find_one(args.pattern)
    if not win:
        sys.exit(f"no window matching {args.pattern!r}")
    win.resize(args.width, args.height)


def _cmd_shot(args: argparse.Namespace) -> None:
    fn = {"full": screenshot.fullscreen, "window": screenshot.active_window,
          "region": screenshot.region}[args.mode]
    path = fn()
    print(path)


def _cmd_apps_list(args: argparse.Namespace) -> None:
    found = apps.find(args.query) if args.query else apps.list_apps()
    if args.json:
        print(json.dumps([{"id": a.app_id, "name": a.name} for a in found]))
        return
    for a in found:
        print(f"{a.app_id}\t{a.name}")


def _cmd_apps_launch(args: argparse.Namespace) -> None:
    apps.launch(args.app_id)


def _cmd_click(args: argparse.Namespace) -> None:
    inp.click(args.button, args.x, args.y)


def _cmd_type(args: argparse.Namespace) -> None:
    inp.type_text(args.text)


def _cmd_key(args: argparse.Namespace) -> None:
    inp.key_combo(*args.keys)


# --- browser: small functions that take a Browser/Page so they're testable
# without actually launching a real Chromium context. ---

def browser_goto(browser: Browser, url: str) -> str:
    page = browser.goto(url)
    return page.url


def browser_click(browser: Browser, selector: str) -> str:
    browser.click(selector)
    return f"clicked {selector!r}"


def browser_fill(browser: Browser, selector: str, text: str) -> str:
    browser.fill(selector, text)
    return f"filled {selector!r}"


def browser_eval(browser: Browser, expression: str) -> str:
    page = browser.active_tab()
    if page is None:
        page = browser.new_tab()
    result = page.evaluate(expression)
    return json.dumps(result) if not isinstance(result, str) else result


def browser_snapshot(browser: Browser) -> str:
    page = browser.active_tab()
    if page is None:
        page = browser.new_tab()
    return page.inner_text("body")


def _cmd_browser_goto(args: argparse.Namespace) -> None:
    with Browser(headless=args.headless) as b:
        print(browser_goto(b, args.url))


def _cmd_browser_click(args: argparse.Namespace) -> None:
    with Browser(headless=args.headless) as b:
        print(browser_click(b, args.selector))


def _cmd_browser_fill(args: argparse.Namespace) -> None:
    with Browser(headless=args.headless) as b:
        print(browser_fill(b, args.selector, args.text))


def _cmd_browser_eval(args: argparse.Namespace) -> None:
    with Browser(headless=args.headless) as b:
        print(browser_eval(b, args.expression))


def _cmd_browser_snapshot(args: argparse.Namespace) -> None:
    with Browser(headless=args.headless) as b:
        print(browser_snapshot(b))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="control", description="Full desktop control for KDE/Wayland")
    sub = p.add_subparsers(dest="command", required=True)

    win = sub.add_parser("win", help="window management")
    win_sub = win.add_subparsers(dest="win_command", required=True)

    w = win_sub.add_parser("list", help="list windows matching a pattern (default: all)")
    w.add_argument("pattern", nargs="?", default="")
    w.add_argument("--json", action="store_true", help="output a JSON array instead of tab-separated text")
    w.set_defaults(func=_cmd_win_list)

    w = win_sub.add_parser("activate")
    w.add_argument("pattern")
    w.set_defaults(func=_cmd_win_activate)

    w = win_sub.add_parser("close")
    w.add_argument("pattern")
    w.set_defaults(func=_cmd_win_close)

    w = win_sub.add_parser("move")
    w.add_argument("pattern")
    w.add_argument("x")
    w.add_argument("y")
    w.set_defaults(func=_cmd_win_move)

    w = win_sub.add_parser("resize")
    w.add_argument("pattern")
    w.add_argument("width")
    w.add_argument("height")
    w.set_defaults(func=_cmd_win_resize)

    shot = sub.add_parser("shot", help="take a screenshot")
    shot.add_argument("mode", choices=["full", "window", "region"], default="full", nargs="?")
    shot.set_defaults(func=_cmd_shot)

    app = sub.add_parser("app", help="find/launch applications")
    app_sub = app.add_subparsers(dest="app_command", required=True)

    a = app_sub.add_parser("list")
    a.add_argument("query", nargs="?", default="")
    a.add_argument("--json", action="store_true", help="output a JSON array instead of tab-separated text")
    a.set_defaults(func=_cmd_apps_list)

    a = app_sub.add_parser("launch")
    a.add_argument("app_id")
    a.set_defaults(func=_cmd_apps_launch)

    click = sub.add_parser("click")
    click.add_argument("--button", default="left", choices=["left", "right", "middle"])
    click.add_argument("-x", type=int, default=None)
    click.add_argument("-y", type=int, default=None)
    click.set_defaults(func=_cmd_click)

    typ = sub.add_parser("type")
    typ.add_argument("text")
    typ.set_defaults(func=_cmd_type)

    key = sub.add_parser(
        "key",
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
    key.set_defaults(func=_cmd_key)

    browser = sub.add_parser("browser", help="DOM-precise browser control (Playwright/Chromium)")
    browser.add_argument("--headless", action="store_true", help="run Chromium headless")
    browser_sub = browser.add_subparsers(dest="browser_command", required=True)

    b = browser_sub.add_parser("goto", help="navigate the active tab to a URL")
    b.add_argument("url")
    b.set_defaults(func=_cmd_browser_goto)

    b = browser_sub.add_parser("click", help="click an element by CSS selector")
    b.add_argument("selector")
    b.set_defaults(func=_cmd_browser_click)

    b = browser_sub.add_parser("fill", help="fill a form field by CSS selector")
    b.add_argument("selector")
    b.add_argument("text")
    b.set_defaults(func=_cmd_browser_fill)

    b = browser_sub.add_parser("eval", help="evaluate a JS expression on the active page")
    b.add_argument("expression")
    b.set_defaults(func=_cmd_browser_eval)

    b = browser_sub.add_parser("snapshot", help="dump the visible text of the active page")
    b.set_defaults(func=_cmd_browser_snapshot)

    return p


def _snake_case(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


_ERROR_HINTS: dict[type, str] = {
    windows.KdotoolError: "check that kdotool is installed and KWin scripting is enabled",
    inp.YdotoolError: "check ydotoold is running: systemctl status ydotoold",
    apps.AppLaunchError: "check the app id with: control app list",
    PlaywrightError: "browser action failed; check the selector/URL and that the page finished loading",
}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except SystemExit:
        raise
    except tuple(_ERROR_HINTS) as exc:
        hint = _ERROR_HINTS[type(exc)]
        print(f"[ERROR] {_snake_case(type(exc).__name__)}: {exc} ({hint})", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, but reported not hidden
        print(f"[ERROR] unexpected: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
