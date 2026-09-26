"""UI Automation worker: native Windows apps the way a screen reader sees them.

The Linux build walks Qt/GTK/Chromium widgets through AT-SPI, in a worker process
run by the system interpreter (libatspi is only reachable through PyGObject, a C
extension distributions build for *their* Python). Windows has no such split: UIA
is COM, reachable in-process, so `atspi.py` imports this module directly and both
platforms answer the same JSON operations for the same CLI.

So the shape of the answer matches the AT-SPI worker's on purpose, and
`control a11y tree|read|do|focus|set-text|wait` with the same
`--role/--name/--text/--state/--path/--nth/...` filters keeps working on both:

* `tree` -- `{app: {...}, nodes: [{role, name, states, actions, text, path, ...}]}`
  with `path` a slash-separated list of child indices from the app root
  (`/0/6/1`), usable as `--path`;
* `get`  -- everything about one element, plus its `box`;
* `action`/`focus`/`set_text`/`wait` -- operate one element.

Roles are UIA control types ("Button", "CheckBox", "Edit") read from the type
library rather than hardcoded, so they are always the ones the installed Windows
knows. They line up with what a user types on Linux -- `atspi.normalize_role`
maps "push button", "check box" and "entry" onto both spellings.
"""
from __future__ import annotations

import time

#: Elements collected per `tree` call before it reports truncation.
DEFAULT_LIMIT = 300
#: How often the `wait` operations re-read the tree.
POLL_INTERVAL = 0.25
#: UIA returns -1 for "read to the end" of a text range.
TEXT_TO_END = -1


class UiaError(RuntimeError):
    """UIA is unusable here (comtypes missing, UIAutomationCore not answering)."""

    kind = "missing_dependency"
    hint = ("install the UIA bridge with: pip install comtypes (Windows 10/11 and "
            "Server 2016+ ship UIAutomationCore)")

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        if hint is not None:
            self.hint = hint


class UiaAppNotFound(UiaError):
    kind = "app_not_found"
    hint = "list the apps that publish a tree with: control a11y apps"


class UiaElementNotFound(UiaError):
    kind = "element_not_found"
    hint = "list the app's elements and their paths with: control a11y tree --app NAME"


class UiaActionFailed(UiaError):
    kind = "action_failed"
    hint = "see what the element offers with: control a11y get --path PATH"


class UiaTimeout(UiaError):
    kind = "timeout"
    hint = "raise --timeout, or check the current tree with: control a11y tree"


# --- the type library -----------------------------------------------------------------
# Property, pattern and control-type ids come from UIAutomationClient's own type
# library rather than from a table written by hand: they are 30000-range numbers
# with no mnemonic and no consistency (HelpText is 30013, not 30014; the Toggle
# pattern is 10015, not 10011), and a single wrong digit makes every call fail
# with an unhelpful COM error. comtypes generates the wrappers on demand.

#: The only two ids the generated client module does not declare as constants:
#: the ValuePattern range, documented in UIAutomationClient.h.
_LITERAL_IDS = {"ValueMin": 30046, "ValueMax": 30047}

_PROPERTIES = (
    "Name", "ControlType", "AutomationId", "ClassName", "HelpText", "HasKeyboardFocus",
    "IsKeyboardFocusable", "IsEnabled", "IsOffscreen", "IsPassword", "ItemStatus",
    "ProcessId", "BoundingRectangle", "ValueValue", "ValueMin", "ValueMax",
    "ToggleToggleState", "SelectionItemIsSelected", "ExpandCollapseExpandCollapseState",
    "IsInvokePatternAvailable", "IsTogglePatternAvailable", "IsValuePatternAvailable",
    "IsTextPatternAvailable", "IsExpandCollapsePatternAvailable",
    "IsSelectionItemPatternAvailable",
)
#: Pattern name -> (id constant, interface). The interface matters: comtypes hands
#: back a bare POINTER(IUnknown) for a pattern object, and calling `Invoke()` on
#: that raises AttributeError, so every pattern is QueryInterface'd into its own
#: interface before use.
_PATTERNS = {
    "InvokePattern": ("UIA_InvokePatternId", "IUIAutomationInvokePattern"),
    "ValuePattern": ("UIA_ValuePatternId", "IUIAutomationValuePattern"),
    "TogglePattern": ("UIA_TogglePatternId", "IUIAutomationTogglePattern"),
    "ExpandCollapsePattern": ("UIA_ExpandCollapsePatternId", "IUIAutomationExpandCollapsePattern"),
    "SelectionItemPattern": ("UIA_SelectionItemPatternId", "IUIAutomationSelectionItemPattern"),
    "TextPattern": ("UIA_TextPatternId", "IUIAutomationTextPattern"),
}

_ids: dict[str, int] = {}
_control_types: dict[int, str] = {}
_toggle_states: dict[int, str] = {}
_expand_states: dict[int, str] = {}
_client = None
_automation = None
#: Set from the type library in _load_types(); TreeScope_Children == 2.
TREE_SCOPE_CHILDREN = 2

#: Shell windows that are not applications (same list as windows.py): the
#: desktop, the taskbar, the invisible input-target windows the shell keeps for
#: its own event plumbing.
_SHELL_CLASSES = frozenset({
    "Shell_TrayWnd", "Progman", "WorkerW", "Windows.UI.Core.CoreWindow",
    "InputSiteWindowClass", "Tao Thread Event Target", "ForegroundStaging",
    "MultitaskingViewFrame", "Xaml_WindowedPopupClass", "TaskListThumbnailWnd",
    "EdgeUiInputTopWndClass", "ApplicationFrameWindow",
})

#: AT-SPI role name -> the UIA control types it can mean on Windows.
#:
#: The CLI normalizes whatever the user typed into the AT-SPI spelling ("push
#: button", "check box", "entry"), so the same documented `--role` filters keep
#: working on both platforms instead of being a Windows-only vocabulary nobody
#: would guess. Matching is still a substring test, so a UIA name the user typed
#: directly works too.
_ROLE_TRANSLATION: dict[str, tuple[str, ...]] = {
    "button": ("Button",), "push button": ("Button",), "toggle button": ("CheckBox",),
    "switch": ("CheckBox",), "check box": ("CheckBox",), "radio button": ("RadioButton",),
    "entry": ("Edit",), "password text": ("Edit",), "text": ("Text",),
    "static": ("Text",), "text label": ("Text",), "label": ("Text",),
    "combo box": ("ComboBox",), "menu": ("Menu",), "menu bar": ("MenuBar",),
    "menu item": ("MenuItem",), "list": ("List",), "list item": ("ListItem",),
    "list box": ("List",), "tree": ("Tree",), "tree item": ("TreeItem",),
    "table": ("DataGrid",), "table cell": ("DataItem",), "data item": ("DataItem",),
    "column header": ("HeaderItem",), "row header": ("HeaderItem",), "header": ("Header",),
    "page tab": ("TabItem",), "page tab list": ("TabList",), "split button": ("SplitButton",),
    "frame": ("Window",), "window": ("Window",), "dialog": ("Window",),
    "message box": ("Window",), "alert": ("Window",), "application": ("Window",),
    "document": ("Document",), "document web": ("Document",),
    "image": ("Image",), "link": ("Hyperlink",), "hyperlink": ("Hyperlink",),
    "slider": ("Slider",), "scale": ("Slider",), "spin button": ("Spinner",),
    "progress bar": ("ProgressBar",), "tool bar": ("ToolBar",),
    "status bar": ("StatusBar",), "scroll bar": ("ScrollBar",), "title bar": ("TitleBar",),
    "tool tip": ("ToolTip",), "separator": ("Separator",), "pane": ("Pane",),
    "panel": ("Pane",), "group": ("Group",), "canvas": ("Custom",),
    "filler": ("Custom",), "drawing area": ("Custom",),
}


def _types_module():
    """The generated UIAutomationTypes module, found by what it contains.

    comtypes names it after the type library's GUID (`_944DE083_...`), not after
    its friendly name, and that has changed between Windows versions -- so it is
    located by the constants it must define.
    """
    import importlib
    import pkgutil

    import comtypes.gen

    for entry in pkgutil.iter_modules(comtypes.gen.__path__):
        module = importlib.import_module(f"comtypes.gen.{entry.name}")
        if hasattr(module, "UIA_ButtonControlTypeId"):
            return module
    raise UiaError("comtypes generated UIAutomationClient but not its type library; "
                   "delete the comtypes cache and try again")


def _load_types():
    """Bind the UIA constants this module uses; returns the generated client module.

    Done once per process: the first call generates comtypes' wrappers (about half
    a second), later ones just hand back the module.
    """
    if _client is not None:
        return _client
    try:
        from comtypes.client import GetModule

        # Generates comtypes.gen.UIAutomationClient (and the types module it
        # imports) into comtypes' cache; a no-op after the first run.
        GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA
    except ImportError as exc:
        raise UiaError(f"UI Automation needs comtypes ({exc})") from exc
    except Exception as exc:  # noqa: BLE001 - typelib generation can fail anywhere
        raise UiaError(f"could not generate the UI Automation wrappers: {exc}",
                       hint="delete the comtypes cache (%LOCALAPPDATA%\\Temp\\comtypes_cache)"
                            " and try again") from exc
    for name in _PROPERTIES:
        constant = getattr(UIA, f"UIA_{name}PropertyId", None)
        _ids[name] = int(constant) if constant is not None else _LITERAL_IDS[name]
    for name, (constant, _interface) in _PATTERNS.items():
        _ids[name] = int(getattr(UIA, constant))
    types = _types_module()
    for name in dir(types):
        if name.startswith("UIA_") and name.endswith("ControlTypeId"):
            # UIA_ButtonControlTypeId -> Button
            _control_types[int(getattr(types, name))] = name[4:-len("ControlTypeId")]
    for state, label in (("Off", "off"), ("On", "on"), ("Indeterminate", "indeterminate")):
        _toggle_states[int(getattr(types, f"ToggleState_{state}"))] = label
    for state, label in (("Collapsed", "collapsed"), ("Expanded", "expanded"),
                         ("PartiallyExpanded", "partially expanded"),
                         ("LeafNode", "leaf node")):
        _expand_states[int(getattr(types, f"ExpandCollapseState_{state}"))] = label
    globals()["_client"] = UIA
    globals()["TREE_SCOPE_CHILDREN"] = int(types.TreeScope_Children)
    return UIA


def automation():
    """The CUIAutomation object, created on first use.

    Instantiated from the generated class rather than by ProgID: "UIAutomationClient
    .CUIAutomation" is not registered in the registry on every build, while the
    class in the type library always is.
    """
    global _automation
    if _automation is None:
        client = _load_types()
        try:
            from comtypes.client import CreateObject

            # interface= matters: the coclass' default interface is IUnknown, so
            # without it every IUIAutomation method would be missing.
            _automation = CreateObject(client.CUIAutomation, interface=client.IUIAutomation)
        except ImportError as exc:
            raise UiaError(f"UI Automation needs comtypes ({exc})") from exc
        except Exception as exc:  # noqa: BLE001 - comtypes raises its own error type
            raise UiaError(f"could not talk to UI Automation: {exc}",
                           hint="is UIAutomationCore.dll present (Windows 10/11, "
                                "Server 2016+)?") from exc
    return _automation


def available() -> tuple[bool, str | None]:
    """(usable, why not) without raising, for `control a11y status`."""
    try:
        automation()
        return True, None
    except UiaError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - anything COM can throw
        return False, f"{type(exc).__name__}: {exc}"


# --- element helpers -------------------------------------------------------------------

def _prop(element, name: str, default=None):
    """Current value of a UIA property, or `default` if the element is gone."""
    try:
        value = element.GetCurrentPropertyValue(_ids[name])
    except Exception:  # noqa: BLE001 - a closed window answers nothing, not a raise
        return default
    return default if value is None else value


def _pattern(element, name: str):
    """A UIA pattern as its own interface, or None when the element lacks it.

    `GetCurrentPattern` answers with an `IUnknown` pointer; without the
    QueryInterface the returned object has no `Invoke`, no `SetValue` and no
    `GetText`, which is the first thing a caller would trip over.
    """
    try:
        raw = element.GetCurrentPattern(_ids[name])
        interface = getattr(_load_types(), _PATTERNS[name][1])
        return raw.QueryInterface(interface) if raw is not None else None
    except Exception:  # noqa: BLE001 - no such pattern, or the element is gone
        return None


def _children(element) -> list:
    """Direct children, in order.

    Asked for in one `FindAll` call rather than by walking sibling by sibling: a
    raw-view walk stops dead with E_INVALIDARG on some shell elements (the
    desktop's own last child has no usable sibling pointer), while FindAll
    answers with the whole level at once and holds a real reference per element.
    """
    try:
        array = element.FindAll(TREE_SCOPE_CHILDREN, automation().CreateTrueCondition())
    except Exception:  # noqa: BLE001 - a provider that refuses FindAll: fall back
        return _children_by_walker(element)
    try:
        return [array.GetElement(index) for index in range(int(array.Length))]
    except Exception:  # noqa: BLE001 - the array went stale mid-read
        return []


def _children_by_walker(element) -> list:
    """Sibling-by-sibling walk, for the rare element FindAll will not answer."""
    found = []
    try:
        walker = automation().RawViewWalker
        child = walker.GetFirstChildElement(element)
        while child is not None:
            found.append(child)
            child = walker.GetNextSiblingElement(child)
    except Exception:  # noqa: BLE001 - a dead pointer ends the level, not the walk
        pass
    return found


def _name(element) -> str:
    return str(_prop(element, "Name", "") or "")


def _role(element) -> str:
    return _control_types.get(int(_prop(element, "ControlType", 0) or 0), "Custom")


def _offscreen(element) -> bool:
    return bool(_prop(element, "IsOffscreen", False))


def _text_of(element) -> str | None:
    """The element's text, if it has any. A password is never read back."""
    if _prop(element, "IsPassword", False):
        return ""
    if _prop(element, "IsValuePatternAvailable", False):
        value = _prop(element, "ValueValue", "")
        if value not in (None, ""):
            return str(value)
    pattern = _pattern(element, "TextPattern")
    if pattern is None:
        return None
    try:
        return str(pattern.DocumentRange.GetText(TEXT_TO_END))
    except Exception:  # noqa: BLE001 - the range moved under us
        return None


def _value_of(element) -> dict | None:
    """The ValuePattern's value, as {current} plus {min, max} when they are numbers.

    UIA's Value property is a variant: a slider reports 0.5, a text field reports
    "3 of 10", and a field with no range reports `false` (VT_EMPTY) for min and
    max. Nothing coerces the value here -- the caller formats whatever came back
    (see `cli._format_value`) -- but a range that is not a range is left out
    instead of reported as 0..0.
    """
    if not _prop(element, "IsValuePatternAvailable", False):
        return None
    value: dict = {"current": _prop(element, "ValueValue", "")}
    low, high = _prop(element, "ValueMin", None), _prop(element, "ValueMax", None)
    if (isinstance(low, (int, float)) and not isinstance(low, bool)
            and isinstance(high, (int, float)) and not isinstance(high, bool)):
        # bool is an int in Python, and UIA reports "no range" as false (VT_EMPTY).
        value["min"], value["max"] = low, high
    return value


def _actions(element) -> list[str]:
    """What can be done to this element, named like AT-SPI's actions."""
    available = []
    if _prop(element, "IsInvokePatternAvailable", False):
        available.append("Press")
    if _prop(element, "IsTogglePatternAvailable", False):
        available.append("Toggle")
    if _prop(element, "IsExpandCollapsePatternAvailable", False):
        available.append("Expand")
    if _prop(element, "IsSelectionItemPatternAvailable", False):
        available.append("Select")
    if _prop(element, "IsValuePatternAvailable", False):
        available.append("SetValue")
    if _prop(element, "IsKeyboardFocusable", False):
        available.append("SetFocus")
    return available


def _states(element) -> list[str]:
    """States worth showing next to an element, in a stable order."""
    shown = []
    if _prop(element, "HasKeyboardFocus", False):
        shown.append("focused")
    if _prop(element, "IsKeyboardFocusable", False):
        shown.append("focusable")
    if not _prop(element, "IsEnabled", True):
        shown.append("disabled")
    if _offscreen(element):
        shown.append("offscreen")
    if _prop(element, "SelectionItemIsSelected", False):
        shown.append("selected")
    if _prop(element, "IsInvokePatternAvailable", False):
        shown.append("clickable")
    if _prop(element, "IsTogglePatternAvailable", False):
        shown.append(_toggle_states.get(int(_prop(element, "ToggleToggleState", 0) or 0), ""))
    if _prop(element, "IsPassword", False):
        shown.append("password")
    if _prop(element, "IsValuePatternAvailable", False):
        shown.append("editable")
    return [state for state in shown if state]


def _box_of(element) -> dict:
    """{x, y, width, height} in physical screen pixels.

    UIA's BoundingRectangle is a UiaRect (Left, Top, *Width*, *Height*) and comes
    back through comtypes as a plain 4-tuple, in that order -- not the Win32 RECT
    (left, top, right, bottom) it looks like.
    """
    rect = _prop(element, "BoundingRectangle")
    if rect is None:
        return {}
    left, top, width, height = (int(value) for value in rect)
    return {"x": left, "y": top, "width": width, "height": height}


def _node(element, path: str, depth: int) -> dict:
    node = {"role": _role(element), "name": _name(element), "states": _states(element),
            "actions": _actions(element), "text": _text_of(element), "path": path,
            "depth": depth, "states_all": _states(element),
            "automation_id": str(_prop(element, "AutomationId", "") or ""),
            "class": str(_prop(element, "ClassName", "") or "")}
    if value := _value_of(element):
        node["value"] = value
    if description := str(_prop(element, "HelpText", "") or ""):
        node["description"] = description
    if status := str(_prop(element, "ItemStatus", "") or ""):
        node["status"] = status
    return node


def _role_matches(role: str, actual: str) -> bool:
    """Does a UIA control type answer to an AT-SPI role name the user typed?"""
    wanted = role.lower()
    if wanted in actual.lower():
        return True
    return any(name.lower() in actual.lower() for name in _ROLE_TRANSLATION.get(wanted, ()))


def _matches(node: dict, flt: dict) -> bool:
    if not flt:
        return True
    if role := flt.get("role"):
        if not _role_matches(role, node["role"]):
            return False
    if name := flt.get("name"):
        if name.lower() not in node["name"].lower():
            return False
    if text := flt.get("text"):
        if text.lower() not in (node.get("text") or "").lower():
            return False
    for state in flt.get("states") or ():
        if state.lower() not in [s.lower() for s in node.get("states_all", [])]:
            return False
    if flt.get("interactive") and not _is_interactive(node):
        return False
    return True


def _is_interactive(node: dict) -> bool:
    return bool(node.get("actions") or "value" in node
                or "editable" in node.get("states_all", []))


# --- apps -------------------------------------------------------------------------------

def _describe_app(element, count_children: bool = True) -> dict:
    pid = int(_prop(element, "ProcessId", 0) or 0)
    return {"pid": pid, "name": _name(element) or f"pid {pid}", "toolkit": "uia",
            "children": len(_children(element)) if count_children else 0}


def _app_windows() -> list:
    """Top-level windows, i.e. the applications publishing a tree."""
    found = []
    for element in _children(automation().GetRootElement()):
        if not _name(element):
            continue
        if str(_prop(element, "ClassName", "") or "") in _SHELL_CLASSES:
            continue  # the desktop, the taskbar, the shell's own event windows
        found.append(element)
    return found


def apps(include_empty: bool = False) -> list[dict]:
    """Every application with a tree, as {pid, name, toolkit, children}."""
    found = []
    for window in _app_windows():
        described = _describe_app(window)
        if include_empty or described["children"]:
            found.append(described)
    return found


def _process_name(pid: int) -> str:
    from . import winapi

    return winapi.process_name(int(pid)) if pid else ""


def _find_app(request: dict):
    """The application element named by `--app` / `--pid` / `--window`."""
    if pid := request.get("pid"):
        for window in _app_windows():
            if int(_prop(window, "ProcessId", 0) or 0) == int(pid):
                return window
        raise UiaAppNotFound(f"no application with pid {pid} publishes a tree")
    name = (request.get("app") or "").lower()
    matches = [w for w in _app_windows() if name and name in _name(w).lower()]
    if not matches:
        # Fall back to the executable: a window titled "Untitled - Bloc de notas"
        # belongs to notepad.exe, and agents know apps by their executable.
        matches = [w for w in _app_windows()
                   if name and name in _process_name(int(_prop(w, "ProcessId", 0) or 0))]
    if not matches:
        raise UiaAppNotFound(f"no application matching {request.get('app')!r} publishes a tree")
    if len(matches) > 1:
        names = ", ".join(sorted(_name(w) for w in matches))
        raise UiaAppNotFound(f"{len(matches)} applications match {request.get('app')!r} "
                             f"({names}); narrow it down with --pid")
    return matches[0]


# --- walking ------------------------------------------------------------------------------

def _walk(element, path: str, depth: int, max_depth: int | None, include_hidden: bool,
          out: list[dict], limit: int) -> bool:
    """Depth-first walk collecting nodes. False once `limit` nodes are collected."""
    if depth and _offscreen(element) and not include_hidden:
        return True
    out.append(_node(element, path, depth))
    if len(out) >= limit:
        return False
    if max_depth is not None and depth >= max_depth:
        return True
    for index, child in enumerate(_children(element)):
        child_path = f"{path}/{index}" if path else f"/{index}"
        if not _walk(child, child_path, depth + 1, max_depth, include_hidden, out, limit):
            return False
    return True


def _descend(app_element, path: str):
    """Follow a `/0/6/1` path from the app root."""
    element = app_element
    indices = path.strip("/").split("/") if path.strip("/") else []
    for index in indices:
        children = _children(element)
        try:
            element = children[int(index)]
        except (ValueError, IndexError) as exc:
            raise UiaElementNotFound(f"no element at path {path!r}") from exc
    return element


def _collect(app_element, flt: dict, limit: int = 200) -> list[tuple[dict, object]]:
    """(node, element) for every visible element the filters accept."""
    found: list[tuple[dict, object]] = []
    stack = [(app_element, "")]
    while stack and len(found) < limit:
        element, path = stack.pop(0)
        if _offscreen(element):
            continue
        node = _node(element, path, 0)
        if _matches(node, flt):
            found.append((node, element))
            continue  # a match is a leaf: read/do want the element, not its children
        for index, child in enumerate(_children(element)):
            stack.append((child, f"{path}/{index}" if path else f"/{index}"))
    return found


def _resolve(app_element, params: dict) -> tuple[object, str]:
    """The element an operation is about, and the path it was found at."""
    if path := params.get("path"):
        return _descend(app_element, path), path
    flt = params.get("filter") or {}
    found = _collect(app_element, flt)
    if not found:
        raise UiaElementNotFound("no element matches those filters")
    index = int(flt.get("nth") or 0)
    if index >= len(found):
        raise UiaElementNotFound(f"there is no match number {index} "
                                 f"({len(found)} element(s) matched)")
    return found[index][1], found[index][0]["path"]


# --- operations ------------------------------------------------------------------------------

def call(op: str, timeout: float = 30.0, **params) -> dict:
    """Run one UIA operation; returns the same shape the AT-SPI worker returns."""
    automation()  # fail fast, with a hint, before doing any work
    if op == "apps":
        return {"apps": apps(include_empty=bool(params.get("all")))}
    if op == "status":
        return {"available": True, "apps": apps()}
    if op == "wait":
        return _wait(params, timeout)
    app_element = _find_app(params)
    app = _describe_app(app_element, count_children=False)
    if op == "tree":
        return _tree(app_element, app, params)
    if op == "get":
        element, path = _resolve(app_element, params)
        node = _node(element, path, 0)
        node["box"] = _box_of(element)
        return {"app": app, "element": node}
    if op in ("action", "focus", "set_text"):
        element, path = _resolve(app_element, params)
        node = _node(element, path, 0)
        performed = params.get("action")
        if op == "action":
            # The resolved action, not the requested one: `a11y do` prints it, and
            # "do --role button" with no --action did something specific.
            node["performed"] = performed = _perform_action(element, performed)
        elif op == "focus":
            _focus(element)
        else:
            _set_text(element, str(params.get("value") or ""))
            node.pop("text", None)  # may be a secret: the CLI reports the length
        # `path` at the top level too, exactly as the AT-SPI worker answers: the
        # CLI's one-line summaries print it from there.
        return {"app": app, "path": path, "element": node, "action": performed, "ok": True}
    raise UiaError(f"unknown UIA operation {op!r}")


def _tree(app_element, app: dict, params: dict) -> dict:
    limit = int(params.get("limit") or DEFAULT_LIMIT)
    max_depth = params.get("depth")
    include_hidden = bool(params.get("all"))
    if path := params.get("path"):
        start, root_path = _descend(app_element, path), path
    else:
        start, root_path = app_element, ""
    nodes: list[dict] = []
    complete = _walk(start, root_path, 0, max_depth, include_hidden, nodes, limit)
    flt = params.get("filter") or {}
    if flt and not params.get("path"):
        nodes = [node for node, _ in _collect(app_element, flt, limit)]
        if flt.get("nth") is not None:
            index = int(flt["nth"])
            nodes = nodes[index:index + 1]
    data = {"app": app, "nodes": nodes, "truncated": not complete}
    if not nodes and not flt:
        data["hint"] = ("this app publishes an empty tree: Chromium, Brave and Electron "
                        "must be restarted with --force-renderer-accessibility, and the "
                        "window has to be in front of the screen")
    return data


def _perform_action(element, action) -> str:
    available = _actions(element)
    if action in (None, ""):
        if not available:
            raise UiaActionFailed("this element offers no action")
        action = available[0]
    if isinstance(action, int) or (isinstance(action, str) and action.isdigit()):
        index = int(action)
        if index >= len(available):
            raise UiaActionFailed(f"this element has {len(available)} action(s): "
                                  f"{', '.join(available)}")
        action = available[index]
    key = str(action).lower()
    if key.startswith(("press", "invoke", "click")):
        pattern = _pattern(element, "InvokePattern") or _pattern(element, "SelectionItemPattern")
        if pattern is None:
            raise UiaActionFailed(
                "this element has no Press action; if it can only take focus, use "
                "'control a11y focus' and then 'control key'/'control type'")
        _invoke(pattern)
    elif key.startswith("toggle"):
        pattern = _pattern(element, "TogglePattern")
        if pattern is None:
            raise UiaActionFailed("this element has no Toggle action")
        _invoke(pattern, "Toggle")
    elif key.startswith("expand"):
        pattern = _pattern(element, "ExpandCollapsePattern")
        if pattern is None:
            raise UiaActionFailed("this element has no Expand action")
        _invoke(pattern, "Expand")
    elif key.startswith("collapse"):
        pattern = _pattern(element, "ExpandCollapsePattern")
        if pattern is None:
            raise UiaActionFailed("this element has no Collapse action")
        _invoke(pattern, "Collapse")
    elif key.startswith("select"):
        pattern = _pattern(element, "SelectionItemPattern")
        if pattern is None:
            raise UiaActionFailed("this element has no Select action")
        _invoke(pattern, "Select")
    elif key.startswith("setvalue"):
        _set_text(element, "")
    elif key.startswith("setfocus"):
        _focus(element)
    else:
        raise UiaActionFailed(f"unknown action {action!r}; this element offers: "
                              f"{', '.join(available) or 'nothing'}")
    return str(action)


def _invoke(pattern, method: str | None = None) -> None:
    call_it = getattr(pattern, method or "Invoke")
    try:
        call_it()
    except Exception as exc:  # noqa: BLE001 - comtypes COMError and friends
        raise UiaActionFailed(f"the application rejected the action: {exc}") from exc


def _focus(element) -> None:
    try:
        element.SetFocus()
    except Exception as exc:  # noqa: BLE001
        raise UiaActionFailed(f"the element refused keyboard focus: {exc}") from exc


def _set_text(element, value: str) -> None:
    pattern = _pattern(element, "ValuePattern")
    if pattern is None:
        raise UiaActionFailed("this element is not editable (it has no Value pattern); "
                              "use 'control a11y focus' and then 'control type'")
    try:
        pattern.SetValue(value)
    except Exception as exc:  # noqa: BLE001
        raise UiaActionFailed(f"the element refused the new text: {exc}") from exc


def _wait(params: dict, timeout: float) -> dict:
    """Block until the element appears (or --gone), then optionally until it settles.

    `--settle MS` is the native counterpart of `browser --wait-loaded`: the tree
    is re-read until nothing visible changed for that long, which is what a dialog
    that is still repainting needs.
    """
    started = time.monotonic()
    deadline = started + timeout
    gone = bool(params.get("gone"))
    settle_ms = int(params.get("settle_ms") or 0)
    app_element = _find_app(params)
    app = _describe_app(app_element, count_children=False)
    signature: str | None = None
    since = started
    while True:
        element, path, node = _probe(app_element, params)
        present = node
        if (present is None) == gone:
            if not settle_ms:
                return {"app": app, "element": present, "waited": round(time.monotonic() - started, 2),
                        "gone": gone, "settled": bool(settle_ms) or None}
            now = time.monotonic()
            current = _signature()
            if current != signature:
                signature, since = current, now
            elif (now - since) * 1000 >= settle_ms:
                return {"app": app, "element": present,
                        "waited": round(time.monotonic() - started, 2), "gone": gone,
                        "settled": True}
        if time.monotonic() >= deadline:
            raise UiaTimeout(
                f"the element did not {'disappear' if gone else 'appear'} within {timeout:g}s"
                + (f" (filter: {params.get('filter') or params.get('path')})"
                   if params.get("filter") else ""))
        time.sleep(POLL_INTERVAL)


def _probe(app_element, params: dict):
    """(element, path, node) for what the filters point at; node None if absent."""
    try:
        element, path = _resolve(app_element, params)
    except UiaElementNotFound:
        return None, "", None
    return element, path, _node(element, path, 0)


def _signature() -> str:
    """A cheap fingerprint of what every visible window is showing."""
    parts = []
    for window in _app_windows():
        if _offscreen(window):
            continue
        parts.append(f"{_name(window)}|{_role(window)}")
        for child in _children(window)[:50]:
            parts.append(f"{_role(child)}|{_name(child)}")
    return "\n".join(parts)
