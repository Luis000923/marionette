"""AT-SPI worker: reads and drives native desktop apps through the accessibility bus.

Run by the *system* Python, not the project venv: that is where distributions
ship PyGObject and the Atspi typelib (python-gobject + at-spi2-core on Arch,
python3-gi + gir1.2-atspi-2.0 on Debian/Ubuntu). atspi.py starts it as

    /usr/bin/python3 -I _atspi_helper.py '{"op": "tree", ...}'

and it prints exactly one JSON object: the result, or {"error", "kind"}.

Only `GiNode` and `main()` touch gi. Everything else works on any object with
GiNode's methods, so the venv's tests drive the tree logic with fake nodes.
Kept compatible with Python 3.8+: the system interpreter is whatever the
distribution ships.
"""
import json
import sys
import time

#: Containers that only add nesting; unnamed ones are skipped (children move up).
NOISE_ROLES = {
    "filler", "panel", "section", "redundant object", "unknown", "layered pane",
    "split pane", "scroll pane", "viewport", "grouping", "invalid", "internal frame",
    "block quote", "static",
}
#: States worth showing; the rest (enabled, sensitive, visible...) is the norm.
SHOWN_STATES = ("focused", "checked", "selected", "expanded", "collapsed", "pressed",
                "editable", "indeterminate", "modal", "busy", "required", "invalid-entry")
DEFAULT_LIMIT = 300
MAX_CHILDREN = 200
TEXT_PREVIEW = 100
POLL = 0.2
OBJECT_CHAR = "\ufffc"


class HelperError(Exception):
    def __init__(self, message, kind="bad_request"):
        Exception.__init__(self, message)
        self.kind = kind


def safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a dying or busy app must not kill the walk
        return default


# --- describing nodes ---------------------------------------------------------------

#: Actions toolkits hang on nearly everything; they don't make a widget interactive.
GENERIC_ACTIONS = {"setfocus", "showcontextmenu", "scroll up", "scroll down"}


def is_interactive(node, states=None):
    states = states if states is not None else safe(node.states, set())
    actions = [a for a in safe(node.actions, []) or [] if a.lower() not in GENERIC_ACTIONS]
    return bool(actions) or "editable" in states or (
        "focusable" in states and (safe(node.role, "") or "") not in NOISE_ROLES)


def describe(node, path, depth=0, states=None, full=False):
    states = states if states is not None else safe(node.states, set())
    name = safe(node.name, "") or ""
    item = {"path": path, "depth": depth, "role": safe(node.role, "") or "", "name": name}
    shown = [s for s in SHOWN_STATES if s in states]
    if states and "enabled" not in states and "sensitive" not in states:
        shown.append("disabled")
    if shown:
        item["states"] = shown
    actions = safe(node.actions, [])
    if actions:
        item["actions"] = actions
    text = safe(lambda: node.text(None if full else TEXT_PREVIEW + 1))
    # U+FFFC marks where a child object sits inside the text: noise, not content.
    text = text.replace(OBJECT_CHAR, "").strip() if text else text
    if text and (full or text.strip() != name.strip()):
        item["text"] = text if full or len(text) <= TEXT_PREVIEW else text[:TEXT_PREVIEW] + "..."
    value = safe(node.value)
    if value is not None:
        item["value"] = value
    box = safe(node.box)
    if box and (box[2] or box[3]):
        item["box"] = {"x": box[0], "y": box[1], "width": box[2], "height": box[3]}
    if full:
        item["states_all"] = sorted(states)
        description = safe(node.description, "")
        if description:
            item["description"] = description
        item["child_count"] = safe(node.child_count, 0)
    return item


def matches(node, states, flt):
    """Does `node` pass the find filters (all given ones must hold)?"""
    if flt.get("role") and flt["role"].lower() not in (safe(node.role, "") or "").lower():
        return False
    if flt.get("name") and flt["name"].lower() not in (safe(node.name, "") or "").lower():
        return False
    if flt.get("text"):
        text = safe(lambda: node.text(4000), "") or ""
        if flt["text"].lower() not in text.lower():
            return False
    for state in flt.get("states") or ():
        if state not in states:
            return False
    if flt.get("interactive") and not is_interactive(node, states):
        return False
    return True


def has_filters(flt):
    return any(flt.get(k) for k in ("role", "name", "text", "states", "interactive"))


def walk(root, root_path, flt=None, limit=DEFAULT_LIMIT, max_depth=60, show_all=False):
    """Depth-first listing of the subtree under `root`.

    Without filters it is an indented outline (noise containers dropped); with
    filters it is a flat list of matches. Hidden subtrees (not "showing") are
    skipped unless `show_all`. Returns (items, truncated).
    """
    flt = flt or {}
    filtering = has_filters(flt)
    out = []
    stack = [(root, root_path, 0, 0)]
    while stack:
        node, path, depth, level = stack.pop()
        states = safe(node.states, set())
        if level and not show_all and "showing" not in states:
            continue
        if filtering:
            keep = matches(node, states, flt)
        else:
            role = safe(node.role, "") or ""
            keep = bool(safe(node.name, "")) or role not in NOISE_ROLES or is_interactive(node, states)
        if keep:
            if len(out) >= limit:
                return out, True
            out.append(describe(node, path, 0 if filtering else depth, states))
        if level >= max_depth:
            continue
        count = safe(node.child_count, 0) or 0
        if count > MAX_CHILDREN and keep:
            out[-1]["children_truncated"] = count
        child_depth = depth + (1 if keep and not filtering else 0)
        for i in reversed(range(min(count, MAX_CHILDREN))):
            child = safe(lambda i=i: node.child(i))
            if child is not None:
                stack.append((child, "%s/%d" % (path, i), child_depth, level + 1))
    return out, False


def resolve_path(app, path):
    """Node at "/i/j/..." (child indices from the application object)."""
    node = app
    for part in [p for p in path.strip("/").split("/") if p != ""]:
        if not part.isdigit():
            raise HelperError("bad element path %r (expected /0/2/5)" % path)
        node = safe(lambda: node.child(int(part)))
        if node is None:
            raise HelperError("no element at %s: the UI changed since it was listed" % path,
                              "element_not_found")
    return node


def find_target(app, root, root_path, req):
    """The element a request is about: its --path, else the nth filter match."""
    if req.get("path"):
        return resolve_path(app, req["path"]), req["path"]
    flt = req.get("filter") or {}
    if not has_filters(flt):
        return root, root_path
    nth = int(req.get("nth") or 0)
    found, _ = walk(root, root_path, flt, limit=nth + 1, show_all=bool(req.get("all")))
    if len(found) <= nth:
        raise HelperError("no element matches %s" % describe_filter(flt), "element_not_found")
    path = found[nth]["path"]
    return resolve_path(app, path), path


def describe_filter(flt):
    parts = ["%s=%r" % (k, flt[k]) for k in ("role", "name", "text") if flt.get(k)]
    parts += ["state=%s" % s for s in flt.get("states") or ()]
    if flt.get("interactive"):
        parts.append("interactive")
    return ", ".join(parts) or "(anything)"


# --- choosing the application -----------------------------------------------------------

CHROMIUM_HINT = ("Chromium/Electron apps (Brave, Chrome, VS Code, Slack...) only expose their "
                 "UI to AT-SPI when started with --force-renderer-accessibility "
                 "(VS Code: or with \"editor.accessibilitySupport\": \"on\"); restart them so")


def app_info(app):
    return {"name": safe(app.name, ""), "pid": safe(app.pid), "toolkit": safe(app.toolkit, ""),
            "children": safe(app.child_count, 0)}


def pick_app(apps, req):
    """(app, root, root_path) for the request's pid / app name / window title."""
    candidates = list(apps)
    if req.get("pid") is not None:
        candidates = [a for a in candidates if safe(a.pid) == int(req["pid"])]
    if req.get("app"):
        needle = req["app"].lower()
        candidates = [a for a in candidates if needle in (safe(a.name, "") or "").lower()]
    if not candidates:
        who = req.get("app") or ("pid %s" % req.get("pid"))
        raise HelperError("no accessible application matches %s" % who, "app_not_found")
    if len(candidates) > 1 and req.get("pid") is None:
        names = ", ".join("%s (pid %s)" % (safe(a.name, ""), safe(a.pid)) for a in candidates[:6])
        raise HelperError("%d applications match: %s; pass --pid or --window" % (len(candidates), names),
                          "ambiguous_app")
    app = candidates[0]
    title = req.get("window_title")
    if title:
        # The app's top-level frames are its windows; KWin's title is the frame name.
        for i in range(safe(app.child_count, 0) or 0):
            frame = safe(lambda i=i: app.child(i))
            name = safe(frame.name, "") if frame is not None else ""
            if name and (name == title or name in title or title in name):
                return app, frame, "/%d" % i
    return app, app, ""


def looks_opaque(app):
    """A Chromium app whose frames expose nothing: accessibility not forced on."""
    if "chrom" not in (safe(app.toolkit, "") or "").lower():
        return False
    frames = [safe(lambda i=i: app.child(i)) for i in range(safe(app.child_count, 0) or 0)]
    return all(f is None or not safe(f.child_count, 0) for f in frames)


# --- operations ---------------------------------------------------------------------------

def op_apps(apps, req):
    listed = [app_info(a) for a in apps]
    return {"apps": [a for a in listed if req.get("all") or a["children"]]}


def op_tree(apps, req):
    app, root, root_path = pick_app(apps, req)
    target, path = find_target(app, root, root_path, {"path": req.get("path")})
    items, truncated = walk(target, path, req.get("filter"), limit=int(req.get("limit") or DEFAULT_LIMIT),
                            max_depth=int(req.get("depth") or 60), show_all=bool(req.get("all")))
    result = {"app": app_info(app), "root": path, "nodes": items, "truncated": truncated}
    if looks_opaque(app):
        result["hint"] = CHROMIUM_HINT
    return result


def op_get(apps, req):
    app, root, root_path = pick_app(apps, req)
    node, path = find_target(app, root, root_path, req)
    return {"app": app_info(app), "element": describe(node, path, full=True)}


def op_action(apps, req):
    app, root, root_path = pick_app(apps, req)
    node, path = find_target(app, root, root_path, req)
    actions = safe(node.actions, []) or []
    wanted = req.get("action")
    if not actions:
        raise HelperError("%s %r at %s has no actions" % (safe(node.role, ""), safe(node.name, ""), path),
                          "action_failed")
    if wanted is None:
        index = 0
    else:
        lowered = [a.lower() for a in actions]
        if str(wanted).isdigit():
            index = int(wanted)
        elif wanted.lower() in lowered:
            index = lowered.index(wanted.lower())
        else:
            raise HelperError("no action %r; this element offers: %s" % (wanted, ", ".join(actions)),
                              "action_failed")
    if index >= len(actions) or not node.do_action(index):
        raise HelperError("action %r was refused by the application" % actions[min(index, len(actions) - 1)],
                          "action_failed")
    return {"app": app_info(app), "path": path, "action": actions[index],
            "element": describe(node, path)}


def op_focus(apps, req):
    app, root, root_path = pick_app(apps, req)
    node, path = find_target(app, root, root_path, req)
    if not safe(node.grab_focus, False):
        raise HelperError("the application refused to focus %s" % path, "action_failed")
    return {"app": app_info(app), "path": path, "element": describe(node, path)}


def op_set_text(apps, req):
    app, root, root_path = pick_app(apps, req)
    node, path = find_target(app, root, root_path, req)
    ok = safe(lambda: node.set_text(req.get("value", "")), None)
    if ok is None:
        raise HelperError("%s at %s is not editable through AT-SPI; focus it (control a11y focus) "
                          "and use: control type" % (safe(node.role, ""), path), "action_failed")
    if not ok:
        raise HelperError("the application refused the new text", "action_failed")
    return {"app": app_info(app), "path": path, "element": describe(node, path, full=True)}


def signature(apps, req):
    app, root, root_path = pick_app(apps, req)
    items, _ = walk(root, root_path, None, limit=int(req.get("limit") or DEFAULT_LIMIT))
    return json.dumps([(i["path"], i["role"], i["name"], i.get("states"), i.get("text")) for i in items])


def op_wait(apps_fn, req, now=time.monotonic, sleep=time.sleep):
    """Poll until the filter matches (or, with `gone`, stops matching); then, with
    `settle_ms`, until the app's visible tree stops changing for that long."""
    timeout = float(req.get("wait_timeout") or 10)
    deadline = now() + timeout
    started = now()
    flt = req.get("filter") or {}
    found = None
    while has_filters(flt):
        try:
            app, root, root_path = pick_app(apps_fn(), req)
            hits, _ = walk(root, root_path, flt, limit=1, show_all=bool(req.get("all")))
        except HelperError as exc:
            if exc.kind != "app_not_found":
                raise
            hits = []
        if bool(hits) != bool(req.get("gone")):
            found = hits[0] if hits else None
            break
        if now() >= deadline:
            what = "disappear" if req.get("gone") else "appear"
            raise HelperError("%s did not %s within %gs" % (describe_filter(flt), what, timeout),
                              "timeout")
        sleep(POLL)
    settle = float(req.get("settle_ms") or 0) / 1000
    if settle:
        last, since = None, now()
        while True:
            current = signature(apps_fn(), req)
            if current != last:
                last, since = current, now()
            elif now() - since >= settle:
                break
            if now() >= deadline:
                raise HelperError("the UI was still changing after %gs" % timeout, "timeout")
            sleep(POLL)
    return {"element": found, "waited": round(now() - started, 2)}


OPS = {"apps": op_apps, "tree": op_tree, "find": op_tree, "get": op_get, "action": op_action,
       "focus": op_focus, "set_text": op_set_text}


def run(req, apps_fn):
    op = req.get("op")
    if op == "wait":
        return op_wait(apps_fn, req)
    if op not in OPS:
        raise HelperError("unknown operation %r" % op)
    return OPS[op](apps_fn(), req)


# --- the real thing: libatspi through GObject introspection ----------------------------------

def gi_backend():
    import warnings
    warnings.simplefilter("ignore")
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError) as exc:
        raise HelperError("PyGObject with the Atspi 2.0 typelib is not available to %s: %s"
                          % (sys.executable, exc), "missing_dependency")
    # Don't hang on a frozen app: fail that call after 2 s instead of the 25 s default.
    safe(lambda: Atspi.set_timeout(2000, 15000))

    class GiNode(object):
        def __init__(self, acc):
            self.acc = acc
            self._ifaces = None

        def ifaces(self):
            if self._ifaces is None:
                self._ifaces = set(safe(self.acc.get_interfaces, []) or [])
            return self._ifaces

        def role(self):
            return self.acc.get_role_name() or ""

        def name(self):
            return self.acc.get_name() or ""

        def description(self):
            return self.acc.get_description() or ""

        def child_count(self):
            return self.acc.get_child_count()

        def child(self, i):
            child = self.acc.get_child_at_index(i)
            return GiNode(child) if child is not None else None

        def states(self):
            return {s.value_nick for s in self.acc.get_state_set().get_states()}

        def actions(self):
            if "Action" not in self.ifaces():
                return []
            return [Atspi.Action.get_action_name(self.acc, i)
                    for i in range(Atspi.Action.get_n_actions(self.acc))]

        def text(self, limit=None):
            if "Text" not in self.ifaces():
                return None
            count = Atspi.Text.get_character_count(self.acc)
            return Atspi.Text.get_text(self.acc, 0, count if limit is None else min(count, limit))

        def value(self):
            if "Value" not in self.ifaces():
                return None
            return {"current": Atspi.Value.get_current_value(self.acc),
                    "min": Atspi.Value.get_minimum_value(self.acc),
                    "max": Atspi.Value.get_maximum_value(self.acc)}

        def box(self):
            if "Component" not in self.ifaces():
                return None
            # WINDOW, not SCREEN: Wayland clients don't know where their window is,
            # so "screen" extents come back window-relative (or 0,0) anyway.
            r = Atspi.Component.get_extents(self.acc, Atspi.CoordType.WINDOW)
            return (r.x, r.y, r.width, r.height)

        def pid(self):
            return self.acc.get_process_id()

        def toolkit(self):
            return self.acc.get_toolkit_name() or ""

        def do_action(self, i):
            return bool(Atspi.Action.do_action(self.acc, i))

        def grab_focus(self):
            return "Component" in self.ifaces() and bool(Atspi.Component.grab_focus(self.acc))

        def set_text(self, text):
            if "EditableText" not in self.ifaces():
                return None
            return bool(Atspi.EditableText.set_text_contents(self.acc, text))

    def apps():
        desktop = Atspi.get_desktop(0)
        found = []
        for i in range(desktop.get_child_count()):
            app = safe(lambda i=i: desktop.get_child_at_index(i))
            if app is not None:
                found.append(GiNode(app))
        return found

    return apps


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        req = json.loads(argv[0]) if argv else {}
        result = run(req, gi_backend())
        out = {"ok": True}
        out.update(result)
    except HelperError as exc:
        out = {"error": str(exc), "kind": exc.kind}
    except Exception as exc:  # noqa: BLE001 - always answer in JSON
        out = {"error": "%s: %s" % (type(exc).__name__, exc), "kind": "unexpected"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    return 0 if "error" not in out else 1


if __name__ == "__main__":
    sys.exit(main())
