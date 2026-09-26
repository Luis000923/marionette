"""Tests for native-desktop automation: the AT-SPI worker's tree logic (on fake
nodes), the venv-side bridge, the `a11y` CLI, key names, session guard rails,
ephemeral sandboxes and window settling. Nothing touches the real bus or KWin.

POSIX-only: it drives the AT-SPI worker (`_atspi_helper.py`), which speaks to
libatspi over D-Bus. On Windows the same CLI is answered by UI Automation; that
side is covered by test_win_native.py.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from control import _atspi_helper as h
from control import atspi, cli, session as session_mod, windows
from control import input as inp

from .test_browser_ai import FakeClock, _main
from .test_cli_multiagent import _desktop, _run

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="exercises the AT-SPI worker (POSIX only)")


# --- a fake accessibility tree -----------------------------------------------------------

class Node:
    def __init__(self, role, name="", children=(), states=("showing",), actions=(), text=None,
                 value=None, editable=None, pid=None, toolkit="Qt", accept=True):
        self._role, self._name, self._children = role, name, list(children)
        self._states, self._actions, self._text = set(states), list(actions), text
        self._value, self._editable, self._pid, self._toolkit = value, editable, pid, toolkit
        self.accept = accept
        self.done: list[int] = []

    def role(self): return self._role
    def name(self): return self._name
    def description(self): return ""
    def child_count(self): return len(self._children)
    def child(self, i): return self._children[i]
    def states(self): return self._states
    def actions(self): return self._actions
    def text(self, limit=None): return None if self._text is None else self._text[:limit]
    def value(self): return self._value
    def box(self): return (10, 20, 30, 40)
    def pid(self): return self._pid
    def toolkit(self): return self._toolkit

    def do_action(self, i):
        self.done.append(i)
        return self.accept

    def grab_focus(self):
        return True

    def set_text(self, text):
        if self._editable is None:
            return None
        self._text = text
        return True


def _editor_app():
    save = Node("push button", "Guardar", actions=["Press", "SetFocus"], states=("showing", "focusable", "enabled"))
    field = Node("text", "Asunto", text="Reunión mañana", editable=True,
                 states=("showing", "editable", "focused", "enabled"))
    hidden = Node("push button", "Oculto", actions=["Press"], states=())
    panel = Node("panel", "", children=[save, field, hidden], actions=["SetFocus"])
    frame = Node("frame", "Correo — KMail", children=[panel])
    other = Node("frame", "Otra ventana", children=[Node("push button", "Cancelar", actions=["Press"])])
    app = Node("application", "kmail", children=[frame, other], pid=4242)
    return app, save, field, hidden


class TestWalk:
    def test_outline_drops_unnamed_containers_and_hidden_subtrees(self):
        app, *_ = _editor_app()
        items, truncated = h.walk(app, "")
        assert [(i["depth"], i["role"], i["name"], i["path"]) for i in items] == [
            (0, "application", "kmail", ""),
            (1, "frame", "Correo — KMail", "/0"),
            (2, "push button", "Guardar", "/0/0/0"),   # the unnamed panel is hoisted away
            (2, "text", "Asunto", "/0/0/1"),
            (1, "frame", "Otra ventana", "/1"),
            (2, "push button", "Cancelar", "/1/0"),
        ]
        assert truncated is False

    def test_show_all_includes_hidden_elements(self):
        app, *_ = _editor_app()
        names = [i["name"] for i in h.walk(app, "", show_all=True)[0]]
        assert "Oculto" in names

    def test_filters_give_a_flat_list_of_matches(self):
        app, *_ = _editor_app()
        items, _ = h.walk(app, "", {"role": "button", "name": "guar"})
        assert [(i["depth"], i["name"], i["path"]) for i in items] == [(0, "Guardar", "/0/0/0")]

    def test_state_and_text_filters(self):
        app, *_ = _editor_app()
        assert [i["name"] for i in h.walk(app, "", {"states": ["focused"]})[0]] == ["Asunto"]
        assert [i["name"] for i in h.walk(app, "", {"text": "MAÑANA"})[0]] == ["Asunto"]

    def test_limit_truncates(self):
        app, *_ = _editor_app()
        items, truncated = h.walk(app, "", limit=2)
        assert len(items) == 2 and truncated

    def test_describe_reports_useful_states_actions_and_text(self):
        _, save, field, _ = _editor_app()
        assert h.describe(field, "/x")["states"] == ["focused", "editable"]
        assert h.describe(field, "/x")["text"] == "Reunión mañana"
        assert h.describe(save, "/y")["actions"] == ["Press", "SetFocus"]
        assert h.describe(Node("button", "x", states=("showing",)), "/z")["states"] == ["disabled"]

    def test_object_replacement_characters_are_not_text(self):
        node = Node("button", "Recargar", text="￼")
        assert "text" not in h.describe(node, "/0")

    def test_generic_setfocus_does_not_make_a_container_interactive(self):
        assert not h.is_interactive(Node("filler", actions=["SetFocus"], states=("focusable",)))
        assert h.is_interactive(Node("push button", actions=["Press"]))

    def test_a_crashing_node_does_not_break_the_walk(self):
        class Broken(Node):
            def name(self):
                raise RuntimeError("app went away")

        app = Node("application", "x", children=[Broken("push button", actions=["Press"])])
        items, _ = h.walk(app, "")
        assert items[1]["name"] == ""


class TestTargeting:
    def test_path_resolution_and_stale_paths(self):
        app, save, *_ = _editor_app()
        assert h.resolve_path(app, "/0/0/0") is save
        with pytest.raises(h.HelperError) as exc:
            h.resolve_path(app, "/0/9")
        assert exc.value.kind == "element_not_found"

    def test_nth_match(self):
        app, *_ = _editor_app()
        node, path = h.find_target(app, app, "", {"filter": {"role": "push button"}, "nth": 1})
        assert node.name() == "Cancelar" and path == "/1/0"

    def test_window_title_restricts_to_that_frame(self):
        app, *_ = _editor_app()
        picked, root, path = h.pick_app([app], {"pid": 4242, "window_title": "Otra ventana"})
        assert picked is app and root.name() == "Otra ventana" and path == "/1"

    def test_app_by_name_and_ambiguity(self):
        a, b = Node("application", "chrome", pid=1), Node("application", "chrome", pid=2)
        with pytest.raises(h.HelperError) as exc:
            h.pick_app([a, b], {"app": "chrome"})
        assert exc.value.kind == "ambiguous_app"
        assert h.pick_app([a, b], {"app": "chrome", "pid": 2})[0] is b
        with pytest.raises(h.HelperError) as exc:
            h.pick_app([a], {"app": "kate"})
        assert exc.value.kind == "app_not_found"

    def test_opaque_chromium_apps_get_a_hint(self):
        app = Node("application", "Brave", children=[Node("frame", "Brave")], toolkit="Chromium", pid=7)
        result = h.op_tree([app], {"pid": 7})
        assert "--force-renderer-accessibility" in result["hint"]


class TestOperations:
    def test_action_by_name_is_case_insensitive(self):
        app, save, *_ = _editor_app()
        result = h.op_action([app], {"pid": 4242, "filter": {"name": "Guardar"}, "action": "press"})
        assert save.done == [0] and result["action"] == "Press"

    def test_unknown_action_lists_the_available_ones(self):
        app, *_ = _editor_app()
        with pytest.raises(h.HelperError, match="offers: Press, SetFocus"):
            h.op_action([app], {"pid": 4242, "filter": {"name": "Guardar"}, "action": "Toggle"})

    def test_refused_action_is_an_error(self):
        app, save, *_ = _editor_app()
        save.accept = False
        with pytest.raises(h.HelperError, match="refused"):
            h.op_action([app], {"pid": 4242, "filter": {"name": "Guardar"}})

    def test_set_text_on_an_editable_and_on_a_plain_element(self):
        app, _save, field, _ = _editor_app()
        h.op_set_text([app], {"pid": 4242, "filter": {"name": "Asunto"}, "value": "Año nuevo"})
        assert field.text() == "Año nuevo"
        with pytest.raises(h.HelperError, match="control type"):
            h.op_set_text([app], {"pid": 4242, "filter": {"name": "Guardar"}, "value": "x"})

    def test_get_returns_the_full_text(self):
        app, _save, field, _ = _editor_app()
        field._text = "x" * 500
        el = h.op_get([app], {"pid": 4242, "filter": {"name": "Asunto"}})["element"]
        assert len(el["text"]) == 500 and "showing" in el["states_all"]


class TestWait:
    def _clock(self):
        clock = FakeClock()
        return clock, clock.now, clock.sleep

    def test_waits_until_the_element_appears(self):
        app, *_ = _editor_app()
        clock, now, sleep = self._clock()
        dialog = Node("dialog", "¿Guardar cambios?")
        clock.on_tick = lambda n: n == 3 and app._children.append(dialog)
        result = h.op_wait(lambda: [app], {"pid": 4242, "filter": {"role": "dialog"}}, now, sleep)
        assert result["element"]["name"] == "¿Guardar cambios?" and clock.ticks == 3

    def test_gone_waits_for_disappearance(self):
        app, _save, field, _ = _editor_app()
        clock, now, sleep = self._clock()
        clock.on_tick = lambda n: n == 2 and field._states.discard("showing")
        result = h.op_wait(lambda: [app], {"pid": 4242, "filter": {"name": "Asunto"}, "gone": True},
                           now, sleep)
        assert result["element"] is None and clock.ticks == 2

    def test_settle_waits_for_the_tree_to_stop_changing(self):
        app, save, *_ = _editor_app()
        clock, now, sleep = self._clock()
        clock.on_tick = lambda n: n <= 4 and setattr(save, "_name", f"Guardando {n}")
        h.op_wait(lambda: [app], {"pid": 4242, "settle_ms": 500}, now, sleep)
        # changes until tick 4, then ~0.5 s (2-3 polls of 0.2 s) unchanged
        assert 6 <= clock.ticks <= 8

    def test_timeout_names_the_filter(self):
        app, *_ = _editor_app()
        _, now, sleep = self._clock()
        with pytest.raises(h.HelperError, match="role='dialog' did not appear within 1s") as exc:
            h.op_wait(lambda: [app], {"pid": 4242, "filter": {"role": "dialog"}, "wait_timeout": 1},
                      now, sleep)
        assert exc.value.kind == "timeout"


def test_helper_main_always_answers_json(capsys):
    with patch.object(h, "gi_backend", side_effect=h.HelperError("no gi", "missing_dependency")):
        assert h.main(['{"op": "apps"}']) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "no gi", "kind": "missing_dependency"}


# --- the venv side --------------------------------------------------------------------------

def _proc(payload, returncode=0, stderr=b""):
    return subprocess.CompletedProcess([], returncode,
                                       stdout=(json.dumps(payload, ensure_ascii=False) + "\n").encode(),
                                       stderr=stderr)


class TestBridge:
    def test_call_runs_the_worker_isolated_and_utf8(self, monkeypatch):
        monkeypatch.setenv("CONTROL_ATSPI_PYTHON", "/opt/py")
        with patch("control.atspi.subprocess.run",
                   return_value=_proc({"ok": True, "apps": [{"name": "Configuración"}]})) as run:
            result = atspi.call("apps", pid=None, app="x")
        argv = run.call_args[0][0]
        assert argv[:4] == ["/opt/py", "-I", "-X", "utf8"] and argv[4].endswith("_atspi_helper.py")
        assert json.loads(argv[5]) == {"op": "apps", "app": "x"}  # None params dropped
        assert result == {"apps": [{"name": "Configuración"}]}

    @pytest.mark.parametrize("kind,cls", [("missing_dependency", atspi.AtspiUnavailable),
                                          ("element_not_found", atspi.A11yElementNotFound),
                                          ("ambiguous_app", atspi.A11yAppNotFound),
                                          ("weird", atspi.AtspiError)])
    def test_worker_errors_become_typed_errors_with_hints(self, kind, cls):
        with patch("control.atspi.subprocess.run", return_value=_proc({"error": "boom", "kind": kind}, 1)):
            with pytest.raises(cls, match="boom"):
                atspi.call("tree")

    def test_missing_dependency_hint_covers_the_main_distros(self):
        hint = atspi.AtspiUnavailable("x").hint
        assert "python-gobject" in hint and "gir1.2-atspi-2.0" in hint and "CONTROL_ATSPI_PYTHON" in hint

    def test_garbage_from_the_worker_is_reported(self):
        bad = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"Segmentation fault")
        with patch("control.atspi.subprocess.run", return_value=bad):
            with pytest.raises(atspi.AtspiError, match="Segmentation fault"):
                atspi.call("apps")

    def test_bus_switch_uses_the_status_property(self):
        with patch("control.atspi.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, "(<true>,)\n", "")) as run:
            assert atspi.is_enabled() is True
            atspi.set_enabled(False)
        assert run.call_args[0][0][-4:] == ["org.freedesktop.DBus.Properties.Set", "org.a11y.Status",
                                             "IsEnabled", "<false>"]


class TestA11yCli:
    def _patch(self, monkeypatch, result=None, enabled=True):
        calls = []
        monkeypatch.setattr(cli.atspi, "is_enabled", lambda: enabled)
        monkeypatch.setattr(cli.atspi, "call", lambda op, **p: calls.append((op, p)) or dict(result or {}))
        return calls

    def test_disabled_bus_is_a_clear_error(self, monkeypatch, capsys):
        self._patch(monkeypatch, enabled=False)
        assert _main(["--json", "a11y", "tree", "--app", "kate"], monkeypatch) == 1
        err = json.loads(capsys.readouterr().out)
        assert err["type"] == "accessibility_disabled" and "control a11y enable" in err["hint"]

    def test_a_target_is_required(self, monkeypatch, capsys):
        self._patch(monkeypatch)
        assert _main(["--json", "a11y", "tree"], monkeypatch) == 1
        assert json.loads(capsys.readouterr().out)["type"] == "usage_error"

    def test_window_resolves_to_pid_and_title_without_focusing(self, monkeypatch, capsys):
        win = windows.Window("{w}", "Correo — KMail", "org.kde.kmail2")
        monkeypatch.setattr(windows.Window, "pid", lambda self: 4242)
        monkeypatch.setattr(windows.Window, "focus", lambda self: pytest.fail("must not focus"))
        monkeypatch.setattr(cli, "_find_window", lambda pattern, args: win)
        calls = self._patch(monkeypatch, {"app": {"name": "kmail", "pid": 4242, "toolkit": "Qt"},
                                          "nodes": [], "truncated": False})
        _main(["a11y", "tree", "--window", "KMail", "--role", "button", "--state", "focused"], monkeypatch)
        op, params = calls[0]
        assert op == "tree" and params["pid"] == 4242 and params["window_title"] == "Correo — KMail"
        assert params["filter"] == {"role": "button", "states": ["focused"]}

    def test_tree_text_output(self, monkeypatch, capsys):
        self._patch(monkeypatch, {"app": {"name": "kmail", "pid": 1, "toolkit": "Qt"}, "truncated": True,
                                  "nodes": [{"path": "/0/1", "depth": 1, "role": "push button",
                                             "name": "Guardar", "states": ["focused"],
                                             "actions": ["Press"], "text": "ñ"}]})
        _main(["a11y", "tree", "--app", "kmail"], monkeypatch)
        out = capsys.readouterr().out
        assert '  push button "Guardar" [focused] {Press} text="ñ"\t/0/1' in out
        assert "truncated" in out

    def test_read_prints_just_the_content(self, monkeypatch, capsys):
        self._patch(monkeypatch, {"element": {"path": "/0", "role": "text", "name": "Asunto",
                                              "text": "Reunión mañana"}})
        _main(["a11y", "read", "--app", "kmail", "--name", "Asunto"], monkeypatch)
        assert capsys.readouterr().out == "Reunión mañana\n"

    def test_read_falls_back_to_value_then_name(self, monkeypatch, capsys):
        self._patch(monkeypatch, {"element": {"path": "/0", "role": "slider", "name": "Volumen",
                                              "value": {"current": 40.0, "min": 0, "max": 100}}})
        _main(["a11y", "read", "--app", "x", "--role", "slider"], monkeypatch)
        assert capsys.readouterr().out == "40\n"

    def test_do_passes_the_action(self, monkeypatch, capsys):
        calls = self._patch(monkeypatch, {"path": "/0", "action": "Toggle",
                                          "element": {"role": "check box", "name": "Negrita"}})
        _main(["a11y", "do", "--app", "x", "--name", "Negrita", "--action", "Toggle"], monkeypatch)
        assert calls[0][0] == "action" and calls[0][1]["action"] == "Toggle"
        assert capsys.readouterr().out == 'Toggle check box "Negrita" (/0)\n'

    def test_set_text_never_echoes_the_text(self, monkeypatch, capsys):
        calls = self._patch(monkeypatch, {"path": "/0", "element": {"role": "password text", "name": "Clave",
                                                                    "text": "s3cret"}})
        _main(["--json", "a11y", "set-text", "--app", "x", "--role", "password", "s3cret"], monkeypatch)
        out = capsys.readouterr().out
        assert calls[0][1]["value"] == "s3cret" and "s3cret" not in out and json.loads(out)["chars"] == 6

    def test_wait_separates_the_wait_from_the_worker_timeout(self, monkeypatch, capsys):
        calls = self._patch(monkeypatch, {"element": None, "waited": 0.4})
        _main(["a11y", "wait", "--app", "x", "--settle", "300", "--timeout", "7"], monkeypatch)
        params = calls[0][1]
        assert params["wait_timeout"] == 7.0 and params["timeout"] == 12.0 and params["settle_ms"] == 300


# --- key names ------------------------------------------------------------------------------

class TestKeys:
    @pytest.mark.parametrize("alias,canonical", [("Return", "enter"), ("ESCAPE", "esc"),
                                                 ("Control", "ctrl"), ("PgUp", "pageup"),
                                                 ("win", "super"), ("del", "delete"), ("-", "minus")])
    def test_aliases(self, alias, canonical):
        assert inp.resolve_key(alias) == canonical

    def test_aliases_reach_ydotool_as_the_real_keycode(self):
        with patch("control.input.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            inp.key_combo("Control", "Return")
        assert run.call_args[0][0] == ["ydotool", "key", "29:1", "28:1", "28:0", "29:0"]

    def test_list_json_groups_every_key_and_the_aliases(self, monkeypatch, capsys):
        assert _main(["--json", "key", "--list"], monkeypatch) == 0
        data = json.loads(capsys.readouterr().out)
        listed = {k for keys in data["keys"].values() for k in keys}
        assert listed == set(inp.KEYCODES)
        assert data["aliases"]["return"] == "enter"

    def test_list_text_mentions_type_for_text(self, monkeypatch, capsys):
        _main(["key", "--list"], monkeypatch)
        out = capsys.readouterr().out
        assert out.startswith("modifiers: ctrl") and "control type" in out

    def test_cli_accepts_aliases_and_suggests_on_typos(self, monkeypatch, capsys):
        sent = []
        monkeypatch.setattr(cli.inp, "key_combo", lambda *k: sent.append(k))
        _main(["--json", "key", "ctrl", "Return"], monkeypatch)
        assert sent == [("ctrl", "Return")]
        capsys.readouterr()
        assert _main(["--json", "key", "retrun"], monkeypatch) == 1
        err = json.loads(capsys.readouterr().out)
        assert "return" in err["hint"]

    def test_no_key_is_a_usage_error(self, monkeypatch, capsys):
        assert _main(["--json", "key"], monkeypatch) == 1
        assert "key --list" in json.loads(capsys.readouterr().out)["hint"]


# --- sessions never reach outside their desktop ------------------------------------------------

@pytest.fixture
def agent_1():
    with patch("control.session.windows.ensure_desktop", return_value=_desktop(1, "control:agent_1")):
        session_mod.init("agent_1")


class TestSessionGuards:
    def test_switching_away_from_the_session_desktop_is_refused(self, agent_1):
        with patch("control.cli.windows.set_desktop") as switch:
            with pytest.raises(cli.CliError) as exc:
                _run(["--session", "agent_1", "desktop", "switch", "1"])
        assert exc.value.kind == "session_guard"
        switch.assert_not_called()

    def test_own_desktop_and_force_are_allowed(self, agent_1):
        with patch("control.cli.windows.set_desktop") as switch:
            _run(["--session", "agent_1", "desktop", "switch", "2"])
            _run(["--session", "agent_1", "desktop", "switch", "1", "--force"])
        assert [c[0][0] for c in switch.call_args_list] == [2, 1]

    def test_sending_a_window_off_the_session_desktop_is_refused(self, agent_1):
        win = windows.Window("{w}", "Kate", "kate")
        with patch("control.cli.windows.find_one", return_value=win), \
             patch.object(windows.Window, "move_to_desktop") as move:
            with pytest.raises(cli.CliError, match="refusing to move a window to desktop 1"):
                _run(["--session", "agent_1", "win", "send", "Kate", "1"])
        move.assert_not_called()

    def test_without_a_session_nothing_changes(self):
        with patch("control.cli.windows.set_desktop") as switch:
            _run(["desktop", "switch", "1"])
        switch.assert_called_once_with(1)


# --- ephemeral sandboxes ---------------------------------------------------------------------

class TestSandbox:
    def _setup(self, monkeypatch, on_desktop_after, closes=True, run=None):
        state = {"closed": [], "ended": None, "env": None}
        monkeypatch.setattr(session_mod.windows, "ensure_desktop",
                            lambda name: _desktop(2, name))
        monkeypatch.setattr(session_mod.windows, "window_ids", lambda pattern: ["{old}"])
        remaining = list(on_desktop_after)

        def windows_on(desktop):
            return [w for w in remaining if not (closes and w.id in state["closed"])]

        monkeypatch.setattr(session_mod, "_windows_on", windows_on)
        monkeypatch.setattr(windows.Window, "close", lambda self: state["closed"].append(self.id))
        monkeypatch.setattr(session_mod, "SANDBOX_CLOSE_TIMEOUT", 0.05)

        def fake_run(cmd, env):
            state["env"] = env
            if run:
                run()
            return subprocess.CompletedProcess(cmd, 3)

        monkeypatch.setattr(session_mod.subprocess, "run", fake_run)
        real_end = session_mod.end
        monkeypatch.setattr(session_mod, "end",
                            lambda name, remove_desktop=False: state.update(ended=(name, remove_desktop))
                            or real_end(name))
        return state, None

    def test_runs_scoped_then_closes_its_windows_and_removes_the_desktop(self, monkeypatch):
        new = windows.Window("{new}", "Dolphin", "dolphin")
        state, _ = self._setup(monkeypatch, [new])
        report = session_mod.sandbox(["./task.sh"], name="sb1")
        assert state["env"]["CONTROL_SESSION_ID"] == "sb1"
        assert state["closed"] == ["{new}"]
        assert report["exit_code"] == 3 and report["desktop_removed"] is True
        assert state["ended"] == ("sb1", True)
        assert session_mod.load("sb1") is None

    def test_windows_that_existed_before_are_never_closed(self, monkeypatch):
        old = windows.Window("{old}", "Kate", "kate")  # id is in the "before" snapshot
        state, _ = self._setup(monkeypatch, [old])
        report = session_mod.sandbox(["x"], name="sb5")
        assert state["closed"] == []
        # ...and since it is still there, the desktop stays rather than dumping it elsewhere.
        assert report["desktop_removed"] is False

    def test_a_window_that_refuses_to_close_keeps_the_desktop(self, monkeypatch):
        stubborn = windows.Window("{new}", "¿Guardar cambios?", "kate")
        state, _ = self._setup(monkeypatch, [stubborn], closes=False)
        report = session_mod.sandbox(["x"], name="sb2")
        assert report["desktop_removed"] is False and report["left"][0]["title"] == "¿Guardar cambios?"
        # The session stays registered, so the kept desktop is never an orphan.
        assert state["ended"] is None and report["session_kept"] is True
        assert session_mod.load("sb2") is not None

    def test_keep_windows_closes_nothing(self, monkeypatch):
        win = windows.Window("{new}", "Dolphin", "dolphin")
        state, _ = self._setup(monkeypatch, [win])
        report = session_mod.sandbox(["x"], name="sb3", keep_windows=True)
        assert state["closed"] == [] and report["desktop_removed"] is False

    def test_teardown_runs_even_if_the_command_cannot_start(self, monkeypatch):
        def boom():
            raise FileNotFoundError("no such command")

        state, _ = self._setup(monkeypatch, [], run=boom)
        with pytest.raises(FileNotFoundError):
            session_mod.sandbox(["nope"], name="sb4")
        assert state["ended"] == ("sb4", True)

    def test_windows_on_ignores_windows_shown_on_all_desktops(self, monkeypatch):
        panel = windows.Window("{panel}", "Panel", "plasmashell")
        mine = windows.Window("{mine}", "Kate", "kate")
        monkeypatch.setattr(session_mod.windows, "list_windows", lambda p, desktop: [panel, mine])
        monkeypatch.setattr(windows.Window, "get_desktop",
                            lambda self: None if self.id == "{panel}" else 2)
        assert session_mod._windows_on(2) == [mine]

    def test_cli_needs_a_command(self, monkeypatch, capsys):
        assert _main(["--json", "sandbox"], monkeypatch) == 1
        assert json.loads(capsys.readouterr().out)["type"] == "usage_error"


# --- window settling ---------------------------------------------------------------------------

class TestWaitSettled:
    def _run(self, monkeypatch, states, **kwargs):
        clock = {"t": 0.0}
        seq = iter(states)
        monkeypatch.setattr(windows.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(windows.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
        last = states[-1]
        monkeypatch.setattr(windows, "_window_state", lambda w: next(seq, last))
        return windows.wait_settled(windows.Window("{w}"), **kwargs), clock["t"]

    def test_returns_once_nothing_changed_for_the_stable_period(self, monkeypatch):
        moving = [("T", (("x", i),), True) for i in range(5)]
        result, t = self._run(monkeypatch, moving, stable=0.3, interval=0.05)
        assert result["settled"] is True and result["geometry"] == {"x": 4}
        assert t == pytest.approx(0.2 + 0.3, abs=0.06)

    def test_reports_not_settled_on_timeout(self, monkeypatch):
        forever = [("T", (("x", i),), True) for i in range(1000)]
        result, _ = self._run(monkeypatch, forever, stable=0.3, timeout=1.0, interval=0.05)
        assert result["settled"] is False


# --- launch suggestions, role names, sandbox timeout -------------------------------------------

from control import apps  # noqa: E402


def _apps(*pairs):
    return [apps.App(app_id=i, name=n, exec_="", path=None) for i, n in pairs]


INSTALLED = _apps(("org.kde.kcalc", "KCalc"), ("org.kde.dolphin", "Dolphin"),
                  ("org.kde.kate", "Kate"), ("firefox", "Firefox"))


class TestLaunchSuggestions:
    @pytest.mark.parametrize("typed,expected", [("kcalc", "org.kde.kcalc"), ("dolfin", "org.kde.dolphin"),
                                                ("Kate", "org.kde.kate"), ("firefx", "firefox")])
    def test_suggests_the_desktop_id(self, monkeypatch, typed, expected):
        monkeypatch.setattr(apps, "list_apps", lambda: INSTALLED)
        assert apps.suggest_ids(typed)[0] == expected

    def test_nothing_alike_suggests_nothing(self, monkeypatch):
        monkeypatch.setattr(apps, "list_apps", lambda: INSTALLED)
        assert apps.suggest_ids("zzzqqq") == []

    def test_cli_error_carries_the_suggestion(self, monkeypatch, capsys):
        monkeypatch.setattr(apps, "list_apps", lambda: INSTALLED)
        monkeypatch.setattr(apps, "_gtk_launch", lambda app_id: subprocess.CompletedProcess(
            [], 2, "", "gtk-launch: no such application kcalc"))
        assert _main(["--json", "app", "launch", "kcalc"], monkeypatch) == 1
        err = json.loads(capsys.readouterr().out)
        assert err["type"] == "app_launch_error"
        assert "did you mean: org.kde.kcalc" in err["hint"] and "control app list" in err["hint"]

    def test_unknown_app_still_points_to_app_list(self, monkeypatch, capsys):
        monkeypatch.setattr(apps, "list_apps", lambda: INSTALLED)
        monkeypatch.setattr(apps, "_gtk_launch", lambda app_id: subprocess.CompletedProcess([], 2, "", ""))
        monkeypatch.setattr(apps.windows, "window_ids", lambda pattern, desktop=None: [])
        assert _main(["--json", "app", "launch", "zzzqqq", "--wait"], monkeypatch) == 1
        err = json.loads(capsys.readouterr().out)
        assert "control app list" in err["hint"] and "could not launch zzzqqq" in err["error"]


class TestRoleNames:
    @pytest.mark.parametrize("typed,canonical", [
        ("push button", "button"), ("PushButton", "button"), (" Push_Button ", "button"),
        ("checkbox", "check box"), ("CheckBox", "check box"), ("textbox", "entry"),
        ("list-item", "list item"), ("menuItem", "menu item"), ("combo  box", "combo box"),
        ("entry", "entry"), ("check", "check"), ("Some Custom Role", "some custom role"),
    ])
    def test_normalize(self, typed, canonical):
        assert atspi.normalize_role(typed) == canonical

    def test_button_finds_old_and_new_spellings(self):
        new, old = Node("button", "Aceptar"), Node("push button", "Cancelar")
        app = Node("application", "x", children=[new, old])
        found = h.walk(app, "", {"role": atspi.normalize_role("push button")})[0]
        assert [i["name"] for i in found] == ["Aceptar", "Cancelar"]

    def test_cli_sends_the_normalized_role(self, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(cli.atspi, "is_enabled", lambda: True)
        monkeypatch.setattr(cli.atspi, "call", lambda op, **p: calls.append(p) or
                            {"app": {"name": "x", "pid": 1, "toolkit": "Qt"}, "nodes": [], "truncated": False})
        _main(["a11y", "tree", "--app", "x", "--role", "CheckBox"], monkeypatch)
        assert calls[0]["filter"]["role"] == "check box"


def _py(code):
    return [sys.executable, "-c", code]


class TestSandboxTimeout:
    @pytest.fixture(autouse=True)
    def fast_graces(self, monkeypatch):
        monkeypatch.setattr(session_mod, "SANDBOX_TERM_GRACE", 0.5)
        monkeypatch.setattr(session_mod, "SANDBOX_KILL_GRACE", 0.5)

    def test_a_command_that_finishes_in_time_keeps_its_exit_code(self):
        assert session_mod._run_command(_py("raise SystemExit(7)"), dict(os.environ), 10) == (7, False)

    def test_a_hung_command_is_terminated_and_reported(self, tmp_path):
        marker = tmp_path / "term"
        code = ("import signal, sys, time, pathlib\n"
                f"signal.signal(signal.SIGTERM, lambda *a: (pathlib.Path({str(marker)!r}).write_text('bye'), sys.exit(0)))\n"
                "time.sleep(60)")
        started = time.monotonic()
        assert session_mod._run_command(_py(code), dict(os.environ), 0.5) == (124, True)
        assert marker.read_text() == "bye"  # it got the chance to shut down cleanly
        assert time.monotonic() - started < 5

    def test_sigterm_ignored_escalates_to_sigkill_for_the_whole_group(self, tmp_path):
        pidfile = tmp_path / "child.pid"
        code = ("import signal, subprocess, sys, time, pathlib\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"c = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
                f"pathlib.Path({str(pidfile)!r}).write_text(str(c.pid))\n"
                "time.sleep(60)")
        assert session_mod._run_command(_py(code), dict(os.environ), 1.0) == (124, True)
        child = int(pidfile.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _alive(child):
            time.sleep(0.05)
        assert not _alive(child), "a grandchild outlived the sandbox"

    def test_timeout_forces_the_teardown(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(session_mod.windows, "ensure_desktop", lambda name: _desktop(2, name))
        monkeypatch.setattr(session_mod.windows, "window_ids", lambda pattern: [])
        monkeypatch.setattr(session_mod, "_run_command", lambda cmd, env, timeout, on_timeout: (124, True))
        monkeypatch.setattr(session_mod, "_teardown",
                            lambda s, before, keep, force=False, already_closed=None: seen.update(force=force) or
                            {"closed": [], "killed_pids": [], "left": [], "desktop_removed": True,
                             "session_kept": False})
        report = session_mod.sandbox(["x"], name="sbt", timeout=1)
        assert report["timed_out"] is True and report["exit_code"] == 124 and seen["force"] is True

    def test_stubborn_apps_are_killed_only_if_they_own_nothing_outside(self, monkeypatch):
        sandboxed = windows.Window("{s}", "Sin guardar", "kate")
        shared = windows.Window("{b}", "Brave sandbox tab", "brave")
        pids = {"{s}": 111, "{b}": 222, "{user-brave}": 222, "{user-kate}": 333}
        monkeypatch.setattr(windows.Window, "pid", lambda self: pids[self.id])
        monkeypatch.setattr(session_mod.windows, "window_ids",
                            lambda pattern: ["{s}", "{b}", "{user-brave}", "{user-kate}"])
        assert session_mod._killable_pids([sandboxed, shared], {"{s}", "{b}"}) == {111}

    def test_forced_teardown_kills_then_removes_the_desktop(self, monkeypatch):
        with patch("control.session.windows.ensure_desktop", return_value=_desktop(1, "control:sbk")):
            session = session_mod.init("sbk")
        stubborn = windows.Window("{s}", "Sin guardar", "kate")
        state = {"alive": True, "signals": []}
        monkeypatch.setattr(session_mod, "_windows_on", lambda d: [stubborn] if state["alive"] else [])
        monkeypatch.setattr(windows.Window, "close", lambda self: None)  # ignored by the app
        monkeypatch.setattr(session_mod, "SANDBOX_CLOSE_TIMEOUT", 0.05)
        monkeypatch.setattr(session_mod, "_killable_pids", lambda stub, ours: {111})

        def fake_kill(pid, sig):
            state["signals"].append((pid, sig))
            state["alive"] = False

        monkeypatch.setattr(session_mod.os, "kill", fake_kill)
        with patch("control.session.windows.find_desktop", return_value=None):
            report = session_mod._teardown(session, set(), keep_windows=False, force=True)
        assert state["signals"] == [(111, signal.SIGTERM)]  # SIGTERM was enough
        assert report["killed_pids"] == [111] and report["desktop_removed"] is True
        assert session_mod.load("sbk") is None

    def test_without_timeout_nothing_is_killed(self, monkeypatch):
        with patch("control.session.windows.ensure_desktop", return_value=_desktop(1, "control:sbn")):
            session = session_mod.init("sbn")
        stubborn = windows.Window("{s}", "Sin guardar", "kate")
        monkeypatch.setattr(session_mod, "_windows_on", lambda d: [stubborn])
        monkeypatch.setattr(windows.Window, "close", lambda self: None)
        monkeypatch.setattr(session_mod, "SANDBOX_CLOSE_TIMEOUT", 0.05)
        monkeypatch.setattr(session_mod.os, "kill", lambda *a: pytest.fail("must not kill"))
        report = session_mod._teardown(session, set(), keep_windows=False)
        assert report["session_kept"] is True and report["killed_pids"] == []

    def test_cli_rejects_a_non_positive_timeout(self, monkeypatch, capsys):
        assert _main(["--json", "sandbox", "--timeout", "0", "--", "true"], monkeypatch) == 1
        assert json.loads(capsys.readouterr().out)["type"] == "usage_error"


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); it is dead for our purposes.
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def test_timeout_asks_windows_to_close_before_any_signal():
    order = []
    code = "import time; time.sleep(60)"
    orig = session_mod._signal_group

    def spy(pgid, sig):
        order.append(("signal", sig))
        return orig(pgid, sig)

    with patch.object(session_mod, "_signal_group", side_effect=spy), \
         patch.object(session_mod, "SANDBOX_TERM_GRACE", 0.5):
        result = session_mod._run_command(_py(code), dict(os.environ), 0.3,
                                          on_timeout=lambda: order.append("close windows"))
    assert result == (124, True)
    assert order[0] == "close windows" and order[1] == ("signal", signal.SIGTERM)
