"""Tests for the multi-agent CLI surface: --desktop scoping, --session/--env-file,
and the session/desktop/snapshot orchestration commands."""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from control import cli, session as session_mod, windows


def _run(argv: list[str]) -> None:
    args = cli.build_parser().parse_args(argv)
    cli.bootstrap(args)
    args.func(args)


def _desktop(position: int, name: str) -> windows.Desktop:
    return windows.Desktop(position=position, id=f"uuid-{position}", name=name)


@pytest.fixture
def agent_1(monkeypatch):
    """A session pinned to desktop 2, without touching real KWin."""
    with patch("control.session.windows.ensure_desktop",
               return_value=_desktop(1, "control:agent_1")):
        session_mod.init("agent_1")
    yield


class TestDesktopScoping:
    def test_win_list_defaults_to_every_desktop(self):
        with patch("control.cli.windows.list_windows", return_value=[]) as mock_list:
            _run(["win", "list"])
        mock_list.assert_called_once_with("", desktop=None)

    def test_explicit_desktop_flag_scopes_the_query(self):
        with patch("control.cli.windows.list_windows", return_value=[]) as mock_list:
            _run(["win", "list", "--desktop", "3"])
        mock_list.assert_called_once_with("", desktop=3)

    def test_session_supplies_the_default_desktop(self, agent_1):
        with patch("control.cli.windows.list_windows", return_value=[]) as mock_list:
            _run(["--session", "agent_1", "win", "list"])
        mock_list.assert_called_once_with("", desktop=2)

    def test_session_id_env_var_scopes_commands_too(self, agent_1, monkeypatch):
        monkeypatch.setenv("CONTROL_SESSION_ID", "agent_1")
        with patch("control.cli.windows.list_windows", return_value=[]) as mock_list:
            _run(["win", "list"])
        mock_list.assert_called_once_with("", desktop=2)

    def test_explicit_desktop_overrides_the_session(self, agent_1):
        with patch("control.cli.windows.list_windows", return_value=[]) as mock_list:
            _run(["--session", "agent_1", "win", "list", "--desktop", "5"])
        mock_list.assert_called_once_with("", desktop=5)

    def test_win_close_only_matches_inside_the_session_desktop(self, agent_1):
        with patch("control.cli.windows.find_one", return_value=None) as mock_find:
            with pytest.raises(SystemExit) as exc:
                _run(["--session", "agent_1", "win", "close", "Editor"])
        mock_find.assert_called_once_with("Editor", desktop=2)
        assert "desktop 2" in str(exc.value)

    def test_win_activate_uses_the_scoped_match(self, agent_1):
        win = windows.Window(id="w1", title="t", classname="c", desktop=2)
        with patch("control.cli.windows.find_one", return_value=win) as mock_find, \
             patch.object(windows.Window, "activate") as mock_activate:
            _run(["--session", "agent_1", "win", "activate", "t"])
        mock_find.assert_called_once_with("t", desktop=2)
        mock_activate.assert_called_once()

    def test_win_send_moves_a_window_between_desktops(self, capsys):
        win = windows.Window(id="w1", title="t", classname="c")
        with patch("control.cli.windows.find_one", return_value=win), \
             patch.object(windows.Window, "move_to_desktop") as mock_move:
            _run(["win", "send", "t", "4"])
        mock_move.assert_called_once_with(4)
        assert "-> desktop 4" in capsys.readouterr().out

    def test_app_launch_sends_the_new_window_to_the_session_desktop(self, agent_1, capsys):
        win = windows.Window(id="w9", title="Editor", classname="code", desktop=2)
        with patch("control.cli.apps.launch", return_value=win) as mock_launch:
            _run(["--session", "agent_1", "app", "launch", "code"])
        mock_launch.assert_called_once_with(
            "code", wait=True, timeout=cli.apps.DEFAULT_WAIT_TIMEOUT,
            window_pattern=None, desktop=2, activate=True,
        )
        assert capsys.readouterr().out == "w9\tcode\tEditor\n"

    def test_app_launch_without_a_session_stays_fire_and_forget(self):
        with patch("control.cli.apps.launch") as mock_launch:
            _run(["app", "launch", "firefox"])
        mock_launch.assert_called_once_with("firefox")

    def test_win_list_json_includes_the_desktop(self, capsys):
        win = windows.Window(id="w1", title="t", classname="c", desktop=3)
        with patch("control.cli.windows.list_windows", return_value=[win]):
            _run(["win", "list", "--json", "--desktop", "3"])
        assert json.loads(capsys.readouterr().out) == [
            {"id": "w1", "title": "t", "class": "c", "desktop": 3}
        ]


class TestEnvFile:
    def test_env_file_is_loaded_into_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CONTROL_DEMO_VAR", raising=False)
        env_file = tmp_path / "agent.env"
        env_file.write_text('CONTROL_DEMO_VAR="from-agent-env"\n')
        with patch("control.cli.windows.list_windows", return_value=[]):
            _run(["--env-file", str(env_file), "win", "list"])
        import os
        assert os.environ["CONTROL_DEMO_VAR"] == "from-agent-env"

    def test_env_file_can_select_the_session(self, tmp_path, agent_1):
        env_file = tmp_path / "agent.env"
        env_file.write_text("CONTROL_SESSION_ID=agent_1\n")
        with patch("control.cli.windows.list_windows", return_value=[]) as mock_list:
            _run(["--env-file", str(env_file), "win", "list"])
        mock_list.assert_called_once_with("", desktop=2)

    def test_missing_env_file_fails_fast(self, tmp_path):
        with pytest.raises(SystemExit, match="env file not found"):
            _run(["--env-file", str(tmp_path / "nope.env"), "win", "list"])

    def test_session_env_file_is_picked_up_automatically(self, agent_1, monkeypatch):
        monkeypatch.delenv("CONTROL_AGENT_TAG", raising=False)
        session = session_mod.load("agent_1")
        session.env_file.write_text("CONTROL_AGENT_TAG=one\n")
        with patch("control.cli.windows.list_windows", return_value=[]):
            _run(["--session", "agent_1", "win", "list"])
        import os
        assert os.environ["CONTROL_AGENT_TAG"] == "one"

    def test_explicit_env_file_wins_over_the_session_one(self, tmp_path, agent_1, monkeypatch):
        monkeypatch.delenv("CONTROL_AGENT_TAG", raising=False)
        session = session_mod.load("agent_1")
        session.env_file.write_text("CONTROL_AGENT_TAG=session\n")
        override = tmp_path / "override.env"
        override.write_text("CONTROL_AGENT_TAG=explicit\n")
        with patch("control.cli.windows.list_windows", return_value=[]):
            _run(["--session", "agent_1", "--env-file", str(override), "win", "list"])
        import os
        assert os.environ["CONTROL_AGENT_TAG"] == "explicit"


class TestSessionCommands:
    def test_session_init_json_reports_the_workspace(self, capsys):
        with patch("control.session.windows.ensure_desktop",
                   return_value=_desktop(2, "control:agent_2")):
            _run(["session", "init", "--name", "agent_2", "--json"])
        data = json.loads(capsys.readouterr().out)
        assert data["name"] == "agent_2"
        assert data["desktop"] == 3
        assert data["profile_dir"].endswith(os.path.join("agent_2", "browser-profile"))

    def test_session_init_is_idempotent_across_invocations(self, capsys):
        with patch("control.session.windows.ensure_desktop",
                   return_value=_desktop(1, "control:agent_1")):
            _run(["session", "init", "--name", "agent_1", "--json"])
            first = json.loads(capsys.readouterr().out)
            _run(["session", "init", "--name", "agent_1", "--json"])
            second = json.loads(capsys.readouterr().out)
        assert first["created_at"] == second["created_at"]
        assert len(session_mod.list_sessions()) == 1

    def test_session_status_json_maps_agents_to_desktops_and_windows(self, agent_1, capsys):
        async def fake_search(pattern, desktop=None, **kwargs):
            return [windows.Window(id="w1", title="Editor", classname="code", desktop=desktop)]

        async def fake_desktops():
            return [_desktop(0, "Escritorio 1"), _desktop(1, "control:agent_1")]

        with patch("control.session.windows.search_async", side_effect=fake_search), \
             patch("control.session.windows.list_desktops_async", side_effect=fake_desktops):
            _run(["--session", "agent_1", "session", "status", "--json"])

        state = json.loads(capsys.readouterr().out)
        assert state["active_session"] == "agent_1"
        entry = state["sessions"][0]
        assert entry["desktop"] == 2
        assert entry["active"] is True
        assert entry["windows"] == [{"id": "w1", "title": "Editor", "class": "code"}]

    def test_session_status_plain_output(self, agent_1, capsys):
        async def fake_search(pattern, desktop=None, **kwargs):
            return []

        async def fake_desktops():
            return [_desktop(1, "control:agent_1")]

        with patch("control.session.windows.search_async", side_effect=fake_search), \
             patch("control.session.windows.list_desktops_async", side_effect=fake_desktops):
            _run(["session", "status"])
        out = capsys.readouterr().out
        assert "agent_1" in out and "desktop 2" in out

    def test_session_status_with_no_sessions_hints_at_init(self, capsys):
        async def fake_desktops():
            return []

        with patch("control.session.windows.list_desktops_async", side_effect=fake_desktops):
            _run(["session", "status"])
        assert "control session init" in capsys.readouterr().out

    def test_session_list_json(self, agent_1, capsys):
        _run(["session", "list", "--json"])
        data = json.loads(capsys.readouterr().out)
        assert [s["name"] for s in data] == ["agent_1"]

    def test_session_end_removes_the_record(self, agent_1):
        _run(["session", "end", "--name", "agent_1"])
        assert session_mod.load("agent_1") is None

    def test_session_end_can_drop_the_desktop(self, agent_1):
        with patch("control.session.windows.find_desktop",
                   return_value=_desktop(1, "control:agent_1")), \
             patch("control.session.windows.remove_desktop") as mock_remove:
            _run(["session", "end", "--name", "agent_1", "--remove-desktop"])
        mock_remove.assert_called_once_with("control:agent_1")


class TestDesktopCommands:
    def test_desktop_list_json_marks_the_current_one(self, capsys):
        desktops = [_desktop(0, "Escritorio 1"), _desktop(1, "control:agent_1")]
        with patch("control.cli.windows.list_desktops", return_value=desktops), \
             patch("control.cli.windows.current_desktop", return_value=2):
            _run(["desktop", "list", "--json"])
        data = json.loads(capsys.readouterr().out)
        assert [d["current"] for d in data] == [False, True]
        assert [d["number"] for d in data] == [1, 2]

    def test_desktop_create_on_demand(self, capsys):
        with patch("control.cli.windows.create_desktop",
                   return_value=_desktop(2, "control:agent_3")) as mock_create:
            _run(["desktop", "create", "control:agent_3"])
        mock_create.assert_called_once_with("control:agent_3", position=None)
        assert capsys.readouterr().out.startswith("3\tcontrol:agent_3")

    def test_desktop_create_ensure_reuses(self):
        with patch("control.cli.windows.ensure_desktop",
                   return_value=_desktop(1, "control:agent_1")) as mock_ensure, \
             patch("control.cli.windows.create_desktop") as mock_create:
            _run(["desktop", "create", "control:agent_1", "--ensure"])
        mock_ensure.assert_called_once_with("control:agent_1")
        mock_create.assert_not_called()

    def test_desktop_remove_and_switch(self):
        with patch("control.cli.windows.remove_desktop") as mock_remove:
            _run(["desktop", "remove", "control:agent_1"])
        mock_remove.assert_called_once_with("control:agent_1")

        with patch("control.cli.windows.set_desktop") as mock_set:
            _run(["desktop", "switch", "2"])
        mock_set.assert_called_once_with(2)


class TestSnapshot:
    def test_snapshot_reports_desktops_sessions_and_windows(self, agent_1, capsys):
        async def fake_search(pattern, desktop=None, **kwargs):
            return []

        async def fake_desktops():
            return [_desktop(1, "control:agent_1")]

        win = windows.Window(id="w1", title="Editor", classname="code", desktop=2)
        with patch("control.session.windows.search_async", side_effect=fake_search), \
             patch("control.session.windows.list_desktops_async", side_effect=fake_desktops), \
             patch("control.cli.windows.list_windows", return_value=[win]) as mock_list, \
             patch("control.cli.windows.current_desktop", return_value=2):
            _run(["--session", "agent_1", "snapshot"])

        state = json.loads(capsys.readouterr().out)
        mock_list.assert_called_once_with("", desktop=2)
        assert state["current_desktop"] == 2
        assert state["windows"] == [
            {"id": "w1", "title": "Editor", "class": "code", "desktop": 2}
        ]
        assert state["sessions"][0]["name"] == "agent_1"

    def test_snapshot_never_takes_the_input_lock(self, agent_1, capsys):
        """A snapshot must answer even while another agent is mid-keystroke."""
        from control import input as inp

        async def fake_search(pattern, desktop=None, **kwargs):
            return []

        async def fake_desktops():
            return []

        with inp.input_mutex:
            with patch("control.session.windows.search_async", side_effect=fake_search), \
                 patch("control.session.windows.list_desktops_async", side_effect=fake_desktops), \
                 patch("control.cli.windows.list_windows", return_value=[]), \
                 patch("control.cli.windows.current_desktop", return_value=1):
                _run(["snapshot"])
        assert json.loads(capsys.readouterr().out)["windows"] == []
