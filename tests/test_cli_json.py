"""Tests for the agent-facing contract: JSON on every command, structured errors,
named --desktop targets, focus-then-input under one lock, per-session traces."""
from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

from control import cli, session as session_mod, windows


def _run(argv: list[str]) -> None:
    args = cli.build_parser().parse_args(argv)
    cli.bootstrap(args)
    args.func(args)


def _main(argv: list[str], monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["control", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
        raise SystemExit(0)
    return exc.value.code


def _desktop(position: int, name: str) -> windows.Desktop:
    return windows.Desktop(position=position, id=f"uuid-{position}", name=name)


class TestJsonEverywhere:
    def test_global_flag_before_the_subcommand(self, capsys):
        with patch("control.cli.windows.list_windows", return_value=[]):
            _run(["--json", "win", "list"])
        assert json.loads(capsys.readouterr().out) == []

    def test_env_var_turns_json_on(self, capsys, monkeypatch):
        monkeypatch.setenv("CONTROL_JSON", "1")
        with patch("control.cli.windows.current_desktop", return_value=3):
            _run(["desktop", "current"])
        assert json.loads(capsys.readouterr().out) == {"current": 3}

    @pytest.mark.parametrize("argv, target", [
        (["win", "activate", "x"], "activate"),
        (["win", "close", "x"], "close"),
        (["win", "move", "x", "1", "2"], "move"),
        (["win", "resize", "x", "3", "4"], "resize"),
    ])
    def test_window_actions_report_what_they_did(self, argv, target, capsys):
        win = windows.Window(id="w1", title="t", classname="c")
        with patch("control.cli.windows.find_one", return_value=win), \
             patch.object(windows.Window, target):
            _run([*argv, "--json"])
        data = json.loads(capsys.readouterr().out)
        assert data["ok"] is True and data["action"] == target
        assert data["window"]["id"] == "w1"

    def test_action_commands_stay_silent_in_text_mode(self, capsys):
        win = windows.Window(id="w1")
        with patch("control.cli.windows.find_one", return_value=win), \
             patch.object(windows.Window, "close"):
            _run(["win", "close", "x"])
        assert capsys.readouterr().out == ""

    def test_type_reports_length_not_the_text(self, capsys):
        with patch("control.cli.inp.type_text") as mock_type:
            _run(["type", "s3cret", "--json"])
        mock_type.assert_called_once_with("s3cret", delay_ms=0)
        out = capsys.readouterr().out
        assert "s3cret" not in out
        assert json.loads(out) == {"ok": True, "action": "type", "chars": 6}

    def test_key_and_click(self, capsys):
        with patch("control.cli.inp.key_combo") as mock_key, \
             patch("control.cli.inp.click") as mock_click:
            _run(["key", "ctrl", "s", "--json"])
            _run(["click", "-x", "5", "-y", "6", "--json"])
        mock_key.assert_called_once_with("ctrl", "s")
        mock_click.assert_called_once_with("left", 5, 6)
        first, second = capsys.readouterr().out.splitlines()
        assert json.loads(first)["keys"] == ["ctrl", "s"]
        assert json.loads(second)["x"] == 5

    def test_shot_json(self, capsys, tmp_path):
        with patch("control.cli.screenshot.fullscreen", return_value=tmp_path / "a.png"):
            _run(["shot", "--json"])
        assert json.loads(capsys.readouterr().out) == {"mode": "full", "path": str(tmp_path / "a.png")}

    def test_fire_and_forget_launch_json(self, capsys):
        with patch("control.cli.apps.launch"):
            _run(["app", "launch", "firefox", "--json"])
        assert json.loads(capsys.readouterr().out)["window"] is None

    def test_session_end_json(self, capsys):
        with patch("control.session.windows.ensure_desktop", return_value=_desktop(1, "control:a")):
            session_mod.init("a")
        _run(["session", "end", "--name", "a", "--json"])
        assert json.loads(capsys.readouterr().out)["session"] == "a"

    def test_browser_helpers_return_structured_data(self):
        class Page:
            url = "https://example.com/"

            def title(self):
                return "Example"

            def evaluate(self, expr):
                return {"n": 2}

            def inner_text(self, sel):
                return "hello"

        class FakeBrowser:
            def goto(self, url):
                return Page()

            def active_tab(self):
                return Page()

        b = FakeBrowser()
        assert cli.browser_goto(b, "https://example.com/") == {"url": "https://example.com/", "title": "Example"}
        assert cli.browser_eval(b, "1+1") == {"result": {"n": 2}}
        assert cli.browser_snapshot(b) == {"url": "https://example.com/", "text": "hello"}


class TestStructuredErrors:
    def test_known_error_becomes_json(self, capsys, monkeypatch):
        with patch("control.cli.windows.list_windows",
                   side_effect=windows.KdotoolError("kwin script failed")):
            code = _main(["win", "list", "--json"], monkeypatch)
        assert code == 1
        err = json.loads(capsys.readouterr().out)
        assert err == {"error": "kwin script failed", "type": "kdotool_error",
                       "hint": cli._ERROR_HINTS[windows.KdotoolError]}

    def test_cli_error_carries_its_hint(self, capsys, monkeypatch):
        with patch("control.cli.windows.find_one", return_value=None):
            code = _main(["--json", "win", "close", "Nope"], monkeypatch)
        assert code == 1
        err = json.loads(capsys.readouterr().out)
        assert err["type"] == "window_not_found"
        assert "Nope" in err["error"] and "win list" in err["hint"]

    def test_usage_errors_are_json_too(self, capsys, monkeypatch):
        code = _main(["win", "move", "--json"], monkeypatch)
        assert code == 2
        err = json.loads(capsys.readouterr().out)
        assert err["type"] == "usage_error" and "required" in err["error"]

    def test_unexpected_errors_do_not_dump_a_traceback_in_json_mode(self, capsys, monkeypatch):
        with patch("control.cli.windows.list_windows", side_effect=ValueError("boom")):
            code = _main(["win", "list", "--json"], monkeypatch)
        assert code == 1
        out = capsys.readouterr()
        assert json.loads(out.out)["type"] == "unexpected"
        assert "Traceback" not in out.err

    def test_unknown_key_suggests_names(self, capsys, monkeypatch):
        with patch("control.cli.inp.key_combo") as mock_key:
            code = _main(["key", "ctlr", "--json"], monkeypatch)
        assert code == 1
        mock_key.assert_not_called()
        assert "ctrl" in json.loads(capsys.readouterr().out)["hint"]

    def test_text_mode_errors_go_to_stderr(self, capsys, monkeypatch):
        with patch("control.cli.windows.find_one", return_value=None):
            _main(["win", "close", "Nope"], monkeypatch)
        out = capsys.readouterr()
        assert out.out == ""
        assert "[ERROR] window_not_found" in out.err

    def test_uninitialised_session_is_refused_not_silently_unscoped(self, capsys, monkeypatch):
        with patch("control.cli.windows.list_windows") as mock_list:
            code = _main(["--session", "ghost", "--json", "win", "list"], monkeypatch)
        assert code == 1
        mock_list.assert_not_called()
        assert "session init --name ghost" in json.loads(capsys.readouterr().out)["hint"]


class TestNamedDesktops:
    def test_query_resolves_a_name_without_creating_it(self):
        with patch("control.cli.windows.resolve_desktop",
                   return_value=_desktop(3, "agent_x")) as mock_resolve, \
             patch("control.cli.windows.list_windows", return_value=[]) as mock_list:
            _run(["win", "list", "--desktop", "agent_x"])
        mock_resolve.assert_called_once_with("agent_x", create=False)
        mock_list.assert_called_once_with("", desktop=4)

    def test_launch_creates_a_missing_named_desktop(self):
        win = windows.Window(id="w1")
        with patch("control.cli.windows.resolve_desktop",
                   return_value=_desktop(4, "agent_7")) as mock_resolve, \
             patch("control.cli.apps.launch", return_value=win) as mock_launch:
            _run(["app", "launch", "kcalc", "--desktop", "agent_7"])
        mock_resolve.assert_called_once_with("agent_7", create=True)
        assert mock_launch.call_args.kwargs["desktop"] == 5

    def test_win_send_to_a_named_desktop(self, capsys):
        win = windows.Window(id="w1")
        with patch("control.cli.windows.find_one", return_value=win), \
             patch("control.cli.windows.resolve_desktop", return_value=_desktop(2, "scratch")), \
             patch.object(windows.Window, "move_to_desktop") as mock_move:
            _run(["win", "send", "x", "scratch", "--json"])
        mock_move.assert_called_once_with(3)
        assert json.loads(capsys.readouterr().out)["desktop"] == 3

    def test_launch_no_activate(self):
        with patch("control.cli.apps.launch", return_value=windows.Window(id="w")) as mock_launch:
            _run(["app", "launch", "kcalc", "--wait", "--no-activate"])
        assert mock_launch.call_args.kwargs["activate"] is False


class TestFocusedInput:
    def test_window_is_focused_then_typed_into_under_one_lock(self):
        from control import input as inp

        seen = []
        win = windows.Window(id="w1", title="Editor")

        def fake_focus(self_win=None, *a, **k):
            seen.append(("focus", inp.input_mutex.held))
            return True

        with patch("control.cli.windows.find_one", return_value=win), \
             patch.object(windows.Window, "focus", fake_focus), \
             patch("control.cli.inp.type_text",
                   side_effect=lambda t, delay_ms=0: seen.append(("type", inp.input_mutex.held))):
            _run(["type", "hi", "--window", "Editor"])
        assert seen == [("focus", True), ("type", True)]
        assert not inp.input_mutex.held

    def test_input_is_not_sent_when_focus_fails(self):
        win = windows.Window(id="w1", title="Editor")
        with patch("control.cli.windows.find_one", return_value=win), \
             patch.object(windows.Window, "focus", return_value=False), \
             patch("control.cli.inp.key_combo") as mock_key:
            with pytest.raises(SystemExit, match="did not take focus"):
                _run(["key", "enter", "--window", "Editor"])
        mock_key.assert_not_called()

    def test_window_lookup_honours_the_session_desktop(self):
        with patch("control.session.windows.ensure_desktop", return_value=_desktop(1, "control:a")):
            session_mod.init("a")
        win = windows.Window(id="w1")
        with patch("control.cli.windows.find_one", return_value=win) as mock_find, \
             patch.object(windows.Window, "focus", return_value=True), \
             patch("control.cli.inp.click"):
            _run(["--session", "a", "click", "--window", "Editor"])
        mock_find.assert_called_once_with("Editor", desktop=2)


class TestSessionTrace:
    def test_each_command_is_logged_to_the_session_without_arguments(self, monkeypatch):
        with patch("control.session.windows.ensure_desktop", return_value=_desktop(1, "control:a")):
            session_mod.init("a")
        with patch("control.cli.inp.type_text"):
            _main(["--session", "a", "type", "hunter2"], monkeypatch)
        log = session_mod.load("a").log_file.read_text()
        assert "type -> ok" in log
        assert "hunter2" not in log


class TestSessionDesktopRenumbering:
    def test_session_follows_its_desktop_uuid_after_renumbering(self, monkeypatch):
        with patch("control.session.windows.ensure_desktop", return_value=_desktop(2, "control:a")):
            session_mod.init("a")
        # Desktop 2 was removed by another agent: ours moved from position 2 to 1.
        monkeypatch.setattr(session_mod, "_list_live_desktops",
                            lambda: [_desktop(0, "Main"),
                                     windows.Desktop(position=1, id="uuid-2", name="control:a")])
        assert session_mod.resolve_desktop(None, name="a") == 2
        assert session_mod.load("a").desktop == 2

    def test_session_whose_desktop_vanished_is_an_error(self, monkeypatch):
        with patch("control.session.windows.ensure_desktop", return_value=_desktop(2, "control:a")):
            session_mod.init("a")
        monkeypatch.setattr(session_mod, "_list_live_desktops", lambda: [_desktop(0, "Main")])
        with pytest.raises(session_mod.SessionError, match="no longer exists"):
            session_mod.resolve_desktop(None, name="a")
