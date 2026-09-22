"""Tests for per-agent sessions: desktop assignment, isolated state, and status.

windows.* is mocked throughout -- these tests must not create real KWin desktops.
The autouse fixture in conftest.py redirects CONTROL_STATE_DIR to a tmp path, so
nothing lands in the developer's home either.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from control import session as session_mod
from control import windows


def _desktop(position: int, name: str) -> windows.Desktop:
    return windows.Desktop(position=position, id=f"uuid-{position}", name=name)


@pytest.fixture
def fake_desktops():
    """Pretend KWin has two agent desktops plus a plain one."""
    desktops = [
        _desktop(0, "Escritorio 1"),
        _desktop(1, "control:agent_1"),
        _desktop(2, "control:agent_2"),
    ]
    with patch("control.session.windows.list_desktops", return_value=desktops), \
         patch("control.session.windows.find_desktop",
               side_effect=lambda key: next(
                   (d for d in desktops
                    if d.name == key or (str(key).isdigit() and d.number == int(key))), None)), \
         patch("control.session.windows.ensure_desktop",
               side_effect=lambda name: next(
                   (d for d in desktops if d.name == name), _desktop(len(desktops), name))):
        yield desktops


class TestInit:
    def test_creates_a_dedicated_desktop_named_after_the_agent(self, fake_desktops):
        with patch("control.session.windows.ensure_desktop",
                   return_value=_desktop(3, "control:agent_7")) as mock_ensure:
            session = session_mod.init("agent_7")
        mock_ensure.assert_called_once_with("control:agent_7")
        assert session.desktop == 4  # 1-based
        assert session.desktop_name == "control:agent_7"

    def test_writes_an_isolated_state_directory(self, fake_desktops):
        session = session_mod.init("agent_1")
        assert session.record_path.is_file()
        assert session.profile_dir.is_dir()
        record = json.loads(session.record_path.read_text())
        assert record["name"] == "agent_1"
        assert record["desktop"] == 2

    def test_two_agents_get_separate_desktops_and_profiles(self, fake_desktops):
        one = session_mod.init("agent_1")
        two = session_mod.init("agent_2")
        assert one.desktop != two.desktop
        assert one.profile_dir != two.profile_dir
        assert one.log_file != two.log_file

    def test_is_idempotent_and_keeps_the_original_timestamp(self, fake_desktops):
        first = session_mod.init("agent_1")
        again = session_mod.init("agent_1")
        assert again.created_at == first.created_at
        assert again.desktop == first.desktop
        assert len(session_mod.list_sessions()) == 1

    def test_can_pin_an_existing_desktop_number(self, fake_desktops):
        session = session_mod.init("agent_x", desktop=3)
        assert session.desktop == 3
        assert session.desktop_name == "control:agent_2"

    def test_pinning_a_missing_desktop_fails_loudly(self, fake_desktops):
        with pytest.raises(session_mod.SessionError, match="no virtual desktop number 99"):
            session_mod.init("agent_x", desktop=99)

    def test_rejects_names_that_would_escape_the_state_dir(self, fake_desktops):
        for bad in ("", "../escape", ".hidden"):
            with pytest.raises(session_mod.SessionError):
                session_mod.init(bad)

    def test_activate_switches_desktop(self, fake_desktops):
        with patch("control.session.windows.set_desktop") as mock_set:
            session_mod.init("agent_1", activate=True)
        mock_set.assert_called_once_with(2)

    def test_logs_to_its_own_file(self, fake_desktops):
        session = session_mod.init("agent_1")
        session.log("did a thing")
        contents = session.log_file.read_text()
        assert "session init" in contents
        assert "[agent_1] did a thing" in contents


class TestResolveDesktop:
    def test_explicit_desktop_wins(self, fake_desktops):
        session_mod.init("agent_1")
        assert session_mod.resolve_desktop(5, "agent_1") == 5

    def test_falls_back_to_the_active_session(self, fake_desktops, monkeypatch):
        session_mod.init("agent_2")
        monkeypatch.setenv("CONTROL_SESSION_ID", "agent_2")
        assert session_mod.resolve_desktop() == 3

    def test_returns_none_without_a_session(self, fake_desktops):
        assert session_mod.resolve_desktop() is None

    def test_returns_none_for_an_unknown_session(self, fake_desktops, monkeypatch):
        monkeypatch.setenv("CONTROL_SESSION_ID", "never_created")
        assert session_mod.resolve_desktop() is None


class TestLoadAndEnd:
    def test_load_returns_none_for_unknown_session(self, fake_desktops):
        assert session_mod.load("nobody") is None

    def test_load_survives_a_corrupt_record(self, fake_desktops):
        session = session_mod.init("agent_1")
        session.record_path.write_text("{not json")
        assert session_mod.load("agent_1") is None

    def test_end_forgets_the_session_but_keeps_its_profile(self, fake_desktops):
        session = session_mod.init("agent_1")
        session_mod.end("agent_1")
        assert session_mod.load("agent_1") is None
        assert session.profile_dir.is_dir()

    def test_end_can_remove_the_desktop(self, fake_desktops):
        session_mod.init("agent_1")
        with patch("control.session.windows.remove_desktop") as mock_remove:
            session_mod.end("agent_1", remove_desktop=True)
        mock_remove.assert_called_once_with("control:agent_1")

    def test_end_on_unknown_session_raises(self, fake_desktops):
        with pytest.raises(session_mod.SessionError):
            session_mod.end("nobody")


class TestStatus:
    def test_reports_each_agent_with_its_own_windows(self, fake_desktops, monkeypatch):
        session_mod.init("agent_1")
        session_mod.init("agent_2")
        monkeypatch.setenv("CONTROL_SESSION_ID", "agent_1")

        async def fake_search(pattern, desktop=None, **kwargs):
            # Each desktop hands back only its own windows -- that is the isolation.
            return [windows.Window(id=f"w{desktop}", title=f"win on {desktop}",
                                   classname="konsole", desktop=desktop)]

        async def fake_desktops_async():
            return fake_desktops

        with patch("control.session.windows.search_async", side_effect=fake_search), \
             patch("control.session.windows.list_desktops_async", side_effect=fake_desktops_async):
            state = session_mod.status()

        assert state["active_session"] == "agent_1"
        by_name = {e["name"]: e for e in state["sessions"]}
        assert by_name["agent_1"]["active"] is True
        assert by_name["agent_2"]["active"] is False
        assert by_name["agent_1"]["windows"][0]["id"] == "w2"
        assert by_name["agent_2"]["windows"][0]["id"] == "w3"
        assert [d["number"] for d in state["desktops"]] == [1, 2, 3]

    def test_flags_a_session_whose_desktop_was_deleted(self, fake_desktops):
        session_mod.init("agent_1")

        async def fake_search(pattern, desktop=None, **kwargs):
            return []

        async def only_one_desktop():
            return [_desktop(0, "Escritorio 1")]

        with patch("control.session.windows.search_async", side_effect=fake_search), \
             patch("control.session.windows.list_desktops_async", side_effect=only_one_desktop):
            state = session_mod.status()
        assert state["sessions"][0]["desktop_exists"] is False

    def test_window_queries_run_concurrently(self, fake_desktops):
        """Polling status with N agents must not cost N serial round-trips."""
        session_mod.init("agent_1")
        session_mod.init("agent_2")
        in_flight = 0
        peak = 0

        async def fake_search(pattern, desktop=None, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return []

        async def fake_desktops_async():
            return fake_desktops

        with patch("control.session.windows.search_async", side_effect=fake_search), \
             patch("control.session.windows.list_desktops_async", side_effect=fake_desktops_async):
            session_mod.status()
        assert peak == 2

    def test_no_windows_skips_the_window_query(self, fake_desktops):
        session_mod.init("agent_1")

        async def fake_desktops_async():
            return fake_desktops

        with patch("control.session.windows.search_async") as mock_search, \
             patch("control.session.windows.list_desktops_async", side_effect=fake_desktops_async):
            state = session_mod.status(include_windows=False)
        mock_search.assert_not_called()
        assert state["sessions"][0]["windows"] == []

    def test_status_does_not_take_the_input_lock(self, fake_desktops):
        """An orchestrator must be able to poll while an agent is typing."""
        from control import input as inp

        session_mod.init("agent_1")

        async def fake_search(pattern, desktop=None, **kwargs):
            return []

        async def fake_desktops_async():
            return fake_desktops

        with inp.input_mutex:
            with patch("control.session.windows.search_async", side_effect=fake_search), \
                 patch("control.session.windows.list_desktops_async", side_effect=fake_desktops_async):
                state = session_mod.status()
        assert state["sessions"][0]["name"] == "agent_1"


class TestBrowserProfileIsolation:
    def test_profile_follows_the_active_session(self, fake_desktops, monkeypatch):
        from control import browser

        monkeypatch.setenv("CONTROL_SESSION_ID", "agent_1")
        assert browser.profile_dir() == session_mod.session_dir("agent_1") / "browser-profile"

    def test_two_agents_never_share_a_profile(self, fake_desktops):
        from control import browser

        assert browser.profile_dir("agent_1") != browser.profile_dir("agent_2")

    def test_falls_back_to_the_shared_profile(self, fake_desktops):
        from control import browser

        assert browser.profile_dir() == browser.PROFILE_DIR
