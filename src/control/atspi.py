"""Native desktop apps through the platform's accessibility API.

Screen readers use it to read and operate Qt, GTK, Chromium and Electron apps;
so can an agent: list an app's widgets with their role, name, state and text,
read a field's value, press a button or type into an editable -- by *element*,
not by screen coordinates, and without needing the window to be focused or even
visible (actions run inside the app).

Two backends, one interface:

* **Linux -- AT-SPI**, the freedesktop accessibility bus. libatspi is only
  reachable through PyGObject (`gi`), a C extension that distributions build
  against *their* Python, so rather than compiling it into the project venv the
  tree walking happens in `_atspi_helper.py`, run by the system interpreter that
  already has it (CONTROL_ATSPI_PYTHON overrides which one). This module is the
  venv side: it starts the worker, speaks JSON with it and turns its errors into
  ones with a next step;
* **Windows -- UI Automation**, through `comtypes` (`_uia.py`). UIA is COM, so
  there is nothing to compile and nothing to fork: the same JSON operations are
  answered in-process, by the very interpreter running the command.

Apps only publish their tree while the bus is enabled on Linux
(`org.a11y.Status IsEnabled`, see `set_enabled`); Qt and GTK apps notice the
switch live, Chromium/Electron apps read it at startup and additionally want
`--force-renderer-accessibility`. Windows has no such switch: UIA is always on.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import platforms

if platforms.IS_WINDOWS:
    from . import _uia
else:
    _uia = None

HELPER = Path(__file__).with_name("_atspi_helper.py")
DEFAULT_TIMEOUT = 30.0

INSTALL_HINT = (
    "install PyGObject and the AT-SPI typelib for the system Python -- Arch: "
    "sudo pacman -S python-gobject at-spi2-core; Debian/Ubuntu: sudo apt install "
    "python3-gi gir1.2-atspi-2.0 at-spi2-core; Fedora: sudo dnf install python3-gobject "
    "at-spi2-core; or point CONTROL_ATSPI_PYTHON at a Python that has them"
) if _uia is None else _uia.UiaError.hint


class AtspiError(RuntimeError):
    hint = ""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        if hint is not None:
            self.hint = hint


class AtspiUnavailable(AtspiError):
    hint = INSTALL_HINT


class AccessibilityDisabled(AtspiError):
    hint = ("turn it on with: control a11y enable (Qt/GTK apps follow at once; restart "
            "Chromium/Electron apps with --force-renderer-accessibility)")


class A11yAppNotFound(AtspiError):
    hint = "list the apps that publish a tree with: control a11y apps"


class A11yElementNotFound(AtspiError):
    hint = "list the app's elements and their paths with: control a11y tree --app NAME"


class A11yActionFailed(AtspiError):
    hint = "see what the element offers with: control a11y get --path PATH"


class A11yTimeout(AtspiError):
    hint = "raise --timeout, or check the current tree with: control a11y tree"


_KINDS: dict[str, type[AtspiError]] = {
    "missing_dependency": AtspiUnavailable,
    "app_not_found": A11yAppNotFound,
    "ambiguous_app": A11yAppNotFound,
    "element_not_found": A11yElementNotFound,
    "action_failed": A11yActionFailed,
    "timeout": A11yTimeout,
}


# --- role names -----------------------------------------------------------------------

#: Spellings people (and ARIA, Qt, GTK, older at-spi) use, keyed without spaces,
#: mapped to the role name libatspi reports. at-spi2-core 2.5x renamed
#: "push button" to "button"; since roles are matched as substrings, "button"
#: finds both spellings, while "push button" would miss every modern button.
ROLE_ALIASES: dict[str, str] = {
    "pushbutton": "button", "btn": "button",
    "textbox": "entry", "textfield": "entry", "textinput": "entry", "input": "entry",
    "lineedit": "entry", "editbox": "entry", "edit": "entry", "searchbox": "entry",
    "password": "password text", "passwordfield": "password text",
    "checkbox": "check box",
    "radiobutton": "radio button",
    "togglebutton": "toggle button", "switch": "toggle button",
    "combobox": "combo box", "combo": "combo box", "dropdown": "combo box", "select": "combo box",
    "spinbutton": "spin button", "spinbox": "spin button",
    "listitem": "list item", "listbox": "list box", "treeitem": "tree item",
    "treetable": "tree table", "tablecell": "table cell", "cell": "table cell",
    "columnheader": "column header", "rowheader": "row header",
    "menuitem": "menu item", "menubar": "menu bar", "popupmenu": "popup menu",
    "checkmenuitem": "check menu item", "radiomenuitem": "radio menu item",
    "tab": "page tab", "pagetab": "page tab", "tablist": "page tab list",
    "pagetablist": "page tab list", "tabpanel": "page tab",
    "toolbar": "tool bar", "statusbar": "status bar", "scrollbar": "scroll bar",
    "progressbar": "progress bar", "titlebar": "title bar", "tooltip": "tool tip",
    "window": "frame", "mainwindow": "frame", "dialogbox": "dialog", "messagebox": "alert",
    "img": "image", "picture": "image", "hyperlink": "link", "anchor": "link",
    "slider": "slider", "scale": "slider", "statictext": "static", "textlabel": "label",
    "doc": "document", "webview": "document web",
}


def normalize_role(role: str) -> str:
    """Canonical AT-SPI role name for what the user typed.

    Case, CamelCase, underscores, hyphens and extra spaces are evened out
    ("PushButton", "push_button", " Push  Button " -> "button"), then common
    aliases are mapped ("checkbox" -> "check box", "textbox" -> "entry").
    Anything unknown is returned cleaned but otherwise untouched.
    """
    cleaned = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", role.strip())
    cleaned = re.sub(r"[\s_\-]+", " ", cleaned).strip().lower()
    return ROLE_ALIASES.get(cleaned.replace(" ", ""), cleaned)


# --- the accessibility bus switch ----------------------------------------------------

# --- the accessibility bus switch ----------------------------------------------------

def _bus_property(*args: str) -> str:
    try:
        result = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.a11y.Bus", "--object-path",
             "/org/a11y/bus", "--method", *args],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AtspiError(f"cannot reach the accessibility bus: {exc}",
                         hint="is at-spi2-core installed and the session D-Bus reachable?") from exc
    if result.returncode != 0:
        raise AtspiError(f"accessibility bus: {result.stderr.strip()}",
                         hint="install at-spi2-core (it provides org.a11y.Bus)")
    return result.stdout


def is_enabled() -> bool:
    """Whether apps publish their widgets at all.

    Always true on Windows: UI Automation is part of the OS and cannot be
    switched off per session.
    """
    if _uia is not None:
        return True
    out = _bus_property("org.freedesktop.DBus.Properties.Get", "org.a11y.Status", "IsEnabled")
    return "true" in out


def set_enabled(enabled: bool) -> None:
    """Flip org.a11y.Status.IsEnabled for this login session (not persisted)."""
    if _uia is not None:
        return  # nothing to switch: UIA is always on
    _bus_property("org.freedesktop.DBus.Properties.Set", "org.a11y.Status", "IsEnabled",
                  f"<{'true' if enabled else 'false'}>")


# --- the worker ---------------------------------------------------------------------------

def helper_python() -> str | None:
    """The interpreter that answers `a11y` calls.

    Linux: CONTROL_ATSPI_PYTHON, else the system python3 that has `gi`. Windows:
    there is no separate worker, so this is simply the interpreter running us.
    """
    explicit = os.environ.get("CONTROL_ATSPI_PYTHON")
    if explicit:
        return explicit
    if _uia is not None:
        import sys

        return sys.executable
    for candidate in ("/usr/bin/python3", shutil.which("python3")):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def call(op: str, timeout: float = DEFAULT_TIMEOUT, **params) -> dict:
    """Run one accessibility operation; returns its result dict or raises.

    Windows answers in-process through UI Automation; Linux runs the AT-SPI
    worker and translates its JSON.
    """
    if _uia is not None:
        try:
            return _uia.call(op, timeout=timeout, **{k: v for k, v in params.items()
                                                     if v is not None})
        except _uia.UiaError as exc:
            raise _KINDS.get(getattr(exc, "kind", ""), AtspiError)(str(exc), exc.hint) from exc
    python = helper_python()
    if python is None:
        raise AtspiUnavailable("no system python3 found to run the AT-SPI worker")
    request = json.dumps({"op": op, **{k: v for k, v in params.items() if v is not None}})
    # -I: ignore the venv's PYTHONPATH/user site; the worker only needs the
    # system site-packages (gi). -X utf8: names and texts stay UTF-8.
    try:
        proc = subprocess.run([python, "-I", "-X", "utf8", str(HELPER), request],
                              capture_output=True, timeout=timeout + 10)
    except FileNotFoundError as exc:
        raise AtspiUnavailable(f"cannot run {python}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise A11yTimeout(f"the AT-SPI worker did not answer within {timeout + 10:g}s",
                          hint="an application may be frozen; list apps with: control a11y apps") from exc
    lines = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()
    try:
        data = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise AtspiError(f"the AT-SPI worker failed: {stderr[-300:] or 'no output'}") from exc
    if "error" in data:
        raise _KINDS.get(data.get("kind"), AtspiError)(data["error"])
    data.pop("ok", None)
    return data


def require_enabled() -> None:
    if not is_enabled():
        raise AccessibilityDisabled("the accessibility bus is off, so apps publish no widgets")


def status() -> dict:
    """Everything needed to tell why a tree comes back empty."""
    info: dict = {"helper_python": helper_python(), "bus_enabled": None, "apps": [],
                  "backend": "uia" if _uia is not None else "at-spi"}
    try:
        info["bus_enabled"] = is_enabled()
    except AtspiError as exc:
        info["bus_error"] = str(exc)
    try:
        info["apps"] = call("apps")["apps"]
        info["available"] = True
    except AtspiError as exc:
        info["available"] = False
        info["error"] = str(exc)
        info["hint"] = exc.hint
    return info
