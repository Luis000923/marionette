"""Tests for windows.py, mocking subprocess.run so no real kdotool is invoked.

POSIX-only: every assertion here is about the kdotool/gdbus backend. The Windows
backend (winapi.py, no subprocess at all) is covered by test_win_native.py.
"""
from __future__ import annotations

import os
import subprocess
from unittest.mock import patch

import pytest

from control import windows

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="exercises the kdotool/gdbus backend (POSIX only)")


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestRun:
    def test_run_returns_stripped_stdout(self):
        with patch("control.windows.subprocess.run", return_value=_completed(stdout="  abc123\n")) as mock_run:
            result = windows._run("getactivewindow")
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == ["kdotool", "getactivewindow"]
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True
        assert result == "abc123"

    def test_run_raises_on_nonzero_returncode(self):
        with patch("control.windows.subprocess.run", return_value=_completed(stderr="boom", returncode=1)):
            with pytest.raises(windows.KdotoolError, match="boom"):
                windows._run("badcommand")

    def test_run_raises_falls_back_to_stdout_when_no_stderr(self):
        with patch("control.windows.subprocess.run", return_value=_completed(stdout="oops", returncode=2)):
            with pytest.raises(windows.KdotoolError, match="oops"):
                windows._run("badcommand")


class TestSearch:
    def test_search_parses_multiple_window_ids(self):
        responses = [
            _completed(stdout="111\n222\n"),  # search
            _completed(stdout="Window One"),  # getwindowname 111
            _completed(stdout="org.kde.one"),  # getwindowclassname 111
            _completed(stdout="Window Two"),  # getwindowname 222
            _completed(stdout="org.kde.two"),  # getwindowclassname 222
        ]
        with patch("control.windows.subprocess.run", side_effect=responses):
            results = windows.search("Window")

        assert len(results) == 2
        assert results[0] == windows.Window(id="111", title="Window One", classname="org.kde.one")
        assert results[1] == windows.Window(id="222", title="Window Two", classname="org.kde.two")

    def test_search_returns_empty_list_when_no_matches(self):
        with patch("control.windows.subprocess.run", return_value=_completed(stdout="")):
            assert windows.search("nonexistent") == []

    def test_search_tolerates_metadata_lookup_failure(self):
        responses = [
            _completed(stdout="111\n"),
            _completed(stderr="gone", returncode=1),  # getwindowname fails
        ]
        with patch("control.windows.subprocess.run", side_effect=responses):
            results = windows.search("Window")
        assert results == [windows.Window(id="111", title="", classname="")]


class TestWaitForWindow:
    def test_returns_immediately_when_window_already_present(self):
        with patch("control.windows.window_ids", return_value=["1"]) as mock_ids, \
             patch("control.windows._describe", side_effect=lambda i, d=None: windows.Window(id=i)), \
             patch.object(windows.Window, "activate"), \
             patch("control.windows.time.sleep") as mock_sleep:
            win = windows.wait_for_window("t", timeout=5, interval=0.1)
        assert win is not None
        assert win.id == "1"
        mock_ids.assert_called_once_with("t", desktop=None)
        mock_sleep.assert_not_called()

    def test_polls_until_window_appears(self):
        with patch("control.windows.window_ids", side_effect=[[], [], ["2"]]), \
             patch("control.windows._describe", side_effect=lambda i, d=None: windows.Window(id=i)), \
             patch.object(windows.Window, "activate"), \
             patch("control.windows.time.sleep") as mock_sleep, \
             patch("control.windows.time.monotonic", side_effect=[0, 0, 1, 2, 3]):
            win = windows.wait_for_window("t", timeout=10, interval=0.1)
        assert win is not None
        assert win.id == "2"
        assert mock_sleep.call_count == 2

    def test_returns_none_on_timeout(self):
        with patch("control.windows.window_ids", return_value=[]), \
             patch("control.windows.time.sleep"), \
             patch("control.windows.time.monotonic", side_effect=[0, 5, 11]):
            win = windows.wait_for_window("nope", timeout=10, interval=0.1)
        assert win is None

    def test_baseline_diff_ignores_pre_existing_windows(self):
        """W1: an already-open window of the same app must not end the wait."""
        with patch("control.windows.window_ids", side_effect=[["old"], ["old"], ["old", "new"]]), \
             patch("control.windows._describe", side_effect=lambda i, d=None: windows.Window(id=i)) as mock_describe, \
             patch.object(windows.Window, "activate"), \
             patch("control.windows.time.sleep"), \
             patch("control.windows.time.monotonic", side_effect=[0, 1, 2, 3]):
            win = windows.wait_for_window("kcalc", timeout=10, known_ids={"old"})
        assert win.id == "new"
        # Only the new window's metadata is fetched, not the baseline's.
        mock_describe.assert_called_once_with("new", None)

    def test_baseline_diff_times_out_when_only_old_windows_exist(self):
        with patch("control.windows.window_ids", return_value=["old"]), \
             patch("control.windows.time.sleep"), \
             patch("control.windows.time.monotonic", side_effect=[0, 5, 11]):
            assert windows.wait_for_window("x", timeout=10, known_ids={"old"}) is None

    def test_found_window_is_activated(self):
        """W2: focus moves to the detected window so the next input lands there."""
        with patch("control.windows.window_ids", return_value=["7"]), \
             patch("control.windows._describe", side_effect=lambda i, d=None: windows.Window(id=i)), \
             patch.object(windows.Window, "activate") as mock_activate:
            windows.wait_for_window("t", timeout=1)
        mock_activate.assert_called_once()

    def test_activation_can_be_disabled(self):
        with patch("control.windows.window_ids", return_value=["7"]), \
             patch("control.windows._describe", side_effect=lambda i, d=None: windows.Window(id=i)), \
             patch.object(windows.Window, "activate") as mock_activate:
            windows.wait_for_window("t", timeout=1, activate=False)
        mock_activate.assert_not_called()

    def test_scopes_polling_to_a_desktop(self):
        with patch("control.windows.window_ids", return_value=["7"]) as mock_ids, \
             patch("control.windows._describe", side_effect=lambda i, d=None: windows.Window(id=i, desktop=d)):
            win = windows.wait_for_window("t", timeout=1, desktop=3, activate=False)
        mock_ids.assert_called_once_with("t", desktop=3)
        assert win.desktop == 3


class TestFocus:
    def test_focus_waits_until_kwin_reports_the_window_active(self):
        win = windows.Window(id="w1")
        with patch.object(windows.Window, "activate") as mock_activate, \
             patch("control.windows._run", side_effect=["other", "w1"]), \
             patch("control.windows.time.sleep"):
            assert win.focus(timeout=5) is True
        mock_activate.assert_called_once()

    def test_focus_gives_up_after_the_timeout(self):
        win = windows.Window(id="w1")
        with patch.object(windows.Window, "activate"), \
             patch("control.windows._run", return_value="other"), \
             patch("control.windows.time.sleep"), \
             patch("control.windows.time.monotonic", side_effect=[0, 0.5, 2]):
            assert win.focus(timeout=1) is False


class TestFindOne:
    def test_find_one_returns_first_match(self):
        responses = [
            _completed(stdout="111\n"),
            _completed(stdout="Some Window"),
            _completed(stdout="org.kde.some"),
        ]
        with patch("control.windows.subprocess.run", side_effect=responses):
            win = windows.find_one("Some")
        assert win is not None
        assert win.id == "111"

    def test_find_one_returns_none_when_no_matches(self):
        with patch("control.windows.subprocess.run", return_value=_completed(stdout="")):
            assert windows.find_one("nope") is None


class TestDesktopScoping:
    """Virtual-desktop filtering is what isolates one agent's windows from another's."""

    def test_search_passes_desktop_filter_to_kdotool(self):
        with patch("control.windows.subprocess.run", return_value=_completed(stdout="")) as mock_run:
            windows.search("", desktop=3)
        assert mock_run.call_args[0][0] == ["kdotool", "search", "--desktop", "3", ""]

    def test_search_without_desktop_stays_unscoped(self):
        with patch("control.windows.subprocess.run", return_value=_completed(stdout="")) as mock_run:
            windows.search("foo")
        assert mock_run.call_args[0][0] == ["kdotool", "search", "foo"]

    def test_search_tags_results_with_the_desktop(self):
        responses = [
            _completed(stdout="111\n"),
            _completed(stdout="Agent window"),
            _completed(stdout="konsole"),
        ]
        with patch("control.windows.subprocess.run", side_effect=responses):
            results = windows.search("", desktop=2)
        assert results[0].desktop == 2

    def test_limit_and_desktop_combine(self):
        with patch("control.windows.subprocess.run", return_value=_completed(stdout="")) as mock_run:
            windows.search("x", limit=1, desktop=4)
        assert mock_run.call_args[0][0] == [
            "kdotool", "search", "--limit", "1", "--desktop", "4", "x",
        ]

    def test_find_one_forwards_desktop(self):
        with patch("control.windows.search", return_value=[]) as mock_search:
            windows.find_one("x", desktop=7)
        mock_search.assert_called_once_with("x", limit=1, desktop=7)

    def test_window_get_desktop_parses_number(self):
        win = windows.Window(id="{abc}")
        with patch("control.windows.subprocess.run", return_value=_completed(stdout="2\n")) as mock_run:
            assert win.get_desktop() == 2
        assert mock_run.call_args[0][0] == ["kdotool", "get_desktop_for_window", "{abc}"]
        assert win.desktop == 2

    def test_window_get_desktop_handles_null(self):
        """KWin reports `null` for windows pinned to every desktop."""
        win = windows.Window(id="{abc}")
        with patch("control.windows.subprocess.run", return_value=_completed(stdout="null")):
            assert win.get_desktop() is None

    def test_window_move_to_desktop(self):
        win = windows.Window(id="{abc}")
        with patch("control.windows.subprocess.run", return_value=_completed()) as mock_run:
            win.move_to_desktop(3)
        assert mock_run.call_args[0][0] == ["kdotool", "set_desktop_for_window", "{abc}", "3"]
        assert win.desktop == 3


class TestDesktopManagement:
    _DESKTOPS = (
        "(<[(uint32 0, '607170c4-aaaa', 'Escritorio 1'), "
        "(1, '79189b45-bbbb', 'control:agent_1')]>,)"
    )

    def test_parses_gdbus_desktop_listing(self):
        with patch("control.windows.subprocess.run", return_value=_completed(stdout=self._DESKTOPS)):
            desktops = windows.list_desktops()
        assert [d.name for d in desktops] == ["Escritorio 1", "control:agent_1"]
        assert [d.position for d in desktops] == [0, 1]
        # kdotool/Plasma numbering is 1-based, KWin's D-Bus positions are 0-based.
        assert [d.number for d in desktops] == [1, 2]
        assert desktops[1].id == "79189b45-bbbb"

    def test_parses_names_containing_quotes(self):
        raw = r"(<[(uint32 0, 'id-1', 'agent\'s desk')]>,)"
        with patch("control.windows.subprocess.run", return_value=_completed(stdout=raw)):
            assert windows.list_desktops()[0].name == "agent's desk"

    def test_find_desktop_by_number_and_by_name(self):
        with patch("control.windows.subprocess.run", return_value=_completed(stdout=self._DESKTOPS)):
            assert windows.find_desktop(2).name == "control:agent_1"
            assert windows.find_desktop("control:agent_1").number == 2
            assert windows.find_desktop("nope") is None

    def test_create_desktop_appends_at_the_end_by_default(self):
        listing = _completed(stdout=self._DESKTOPS)
        created = _completed(stdout=(
            "(<[(uint32 0, 'a', 'Escritorio 1'), (1, 'b', 'control:agent_1'), "
            "(2, 'c', 'control:agent_2')]>,)"
        ))
        with patch("control.windows.subprocess.run",
                   side_effect=[listing, _completed(), created]) as mock_run:
            desktop = windows.create_desktop("control:agent_2")
        create_call = mock_run.call_args_list[1][0][0]
        assert create_call[-3:] == [
            "org.kde.KWin.VirtualDesktopManager.createDesktop", "2", "control:agent_2",
        ]
        assert desktop.number == 3
        assert desktop.id == "c"

    def test_create_desktop_honours_explicit_position(self):
        created = _completed(stdout="(<[(uint32 0, 'z', 'first')]>,)")
        with patch("control.windows.subprocess.run", side_effect=[_completed(), created]) as mock_run:
            windows.create_desktop("first", position=0)
        assert mock_run.call_args_list[0][0][0][-2:] == ["0", "first"]

    def test_create_desktop_raises_when_kwin_silently_drops_it(self):
        listing = _completed(stdout=self._DESKTOPS)
        with patch("control.windows.subprocess.run", side_effect=[listing, _completed(), listing]):
            with pytest.raises(windows.DesktopError, match="no such desktop"):
                windows.create_desktop("control:ghost")

    def test_ensure_desktop_reuses_an_existing_one(self):
        with patch("control.windows.subprocess.run", return_value=_completed(stdout=self._DESKTOPS)) as mock_run:
            desktop = windows.ensure_desktop("control:agent_1")
        assert desktop.number == 2
        assert mock_run.call_count == 1  # listed only; nothing created

    def test_ensure_desktop_creates_a_missing_one(self):
        with patch("control.windows.list_desktops",
                   return_value=[windows.Desktop(position=0, id="a", name="Escritorio 1")]), \
             patch("control.windows.create_desktop") as mock_create:
            windows.ensure_desktop("control:agent_9")
        mock_create.assert_called_once_with("control:agent_9")

    def test_remove_desktop_resolves_the_uuid_first(self):
        listing = _completed(stdout=self._DESKTOPS)
        with patch("control.windows.subprocess.run", side_effect=[listing, _completed()]) as mock_run:
            windows.remove_desktop("control:agent_1")
        remove_call = mock_run.call_args_list[1][0][0]
        assert remove_call[-2:] == [
            "org.kde.KWin.VirtualDesktopManager.removeDesktop", "79189b45-bbbb",
        ]

    def test_remove_unknown_desktop_raises(self):
        with patch("control.windows.subprocess.run", return_value=_completed(stdout=self._DESKTOPS)):
            with pytest.raises(windows.DesktopError, match="no virtual desktop"):
                windows.remove_desktop("control:ghost")

    def test_gdbus_failure_is_reported_as_desktop_error(self):
        with patch("control.windows.subprocess.run",
                   return_value=_completed(stderr="no such name", returncode=1)):
            with pytest.raises(windows.DesktopError, match="no such name"):
                windows.list_desktops()


class TestAsyncQueries:
    """State queries run concurrently and must never take the input lock."""

    def test_search_async_gathers_metadata_in_parallel(self):
        import asyncio

        calls: list[tuple[str, ...]] = []
        in_flight = 0
        peak = 0

        async def fake_run(*args: str) -> str:
            nonlocal in_flight, peak
            calls.append(args)
            if args[0] == "search":
                return "111\n222\n333\n"
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return "name" if args[0] == "getwindowname" else "cls"

        with patch("control.windows._run_async", side_effect=fake_run):
            results = asyncio.run(windows.search_async("", desktop=2))

        assert [w.id for w in results] == ["111", "222", "333"]
        assert all(w.desktop == 2 for w in results)
        assert calls[0] == ("search", "--desktop", "2", "")
        assert peak > 1, "metadata lookups should overlap, not run one at a time"

    def test_search_async_tolerates_metadata_failure(self):
        import asyncio

        async def fake_run(*args: str) -> str:
            if args[0] == "search":
                return "111\n"
            raise windows.KdotoolError("window vanished mid-query")

        with patch("control.windows._run_async", side_effect=fake_run):
            results = asyncio.run(windows.search_async(""))
        assert results == [windows.Window(id="111", title="", classname="", desktop=None)]

    def test_list_windows_does_not_touch_the_input_lock(self):
        """A status query must answer even while another agent holds the input lock."""
        import asyncio

        from control import input as inp

        async def fake_run(*args: str) -> str:
            return "" if args[0] == "search" else "x"

        with inp.input_mutex:  # another agent is mid-keystroke
            with patch("control.windows._run_async", side_effect=fake_run):
                assert windows.list_windows("", desktop=1) == []
        assert asyncio.get_event_loop_policy() is not None  # sanity: loop cleaned up


class TestWaitForNewWindow:
    def test_skips_windows_that_already_existed(self):
        with patch("control.windows.window_ids", side_effect=[["old"], ["old", "new"]]), \
             patch("control.windows._describe", side_effect=lambda i, d=None: windows.Window(id=i)), \
             patch("control.windows.time.sleep"), \
             patch("control.windows.time.monotonic", side_effect=[0, 1, 2]):
            win = windows.wait_for_new_window("kcalc", {"old"}, timeout=10)
        assert win.id == "new"

    def test_returns_none_on_timeout(self):
        with patch("control.windows.window_ids", return_value=["old"]), \
             patch("control.windows.time.sleep"), \
             patch("control.windows.time.monotonic", side_effect=[0, 5, 11]):
            assert windows.wait_for_new_window("x", {"old"}, timeout=10) is None


class TestResolveDesktop:
    DESKTOPS = [windows.Desktop(0, "u0", "Main"), windows.Desktop(1, "u1", "control:agent_1")]

    def test_number_resolves_to_an_existing_desktop(self):
        with patch("control.windows.list_desktops", return_value=self.DESKTOPS):
            assert windows.resolve_desktop("2").name == "control:agent_1"

    def test_missing_number_is_an_error_even_with_create(self):
        with patch("control.windows.list_desktops", return_value=self.DESKTOPS):
            with pytest.raises(windows.DesktopError, match="number 9"):
                windows.resolve_desktop("9", create=True)

    def test_name_is_looked_up_without_creating_by_default(self):
        with patch("control.windows.list_desktops", return_value=self.DESKTOPS), \
             patch("control.windows.create_desktop") as mock_create:
            with pytest.raises(windows.DesktopError, match="agent_7"):
                windows.resolve_desktop("agent_7")
        mock_create.assert_not_called()

    def test_name_is_created_on_demand(self):
        created = windows.Desktop(2, "u2", "agent_7")
        with patch("control.windows.list_desktops", return_value=self.DESKTOPS), \
             patch("control.windows.create_desktop", return_value=created) as mock_create:
            assert windows.resolve_desktop("agent_7", create=True) is created
        mock_create.assert_called_once_with("agent_7")


def test_geometry_parses_kdotool_output():
    out = "Window {96a3}\n  Position: 332,-14\n  Geometry: 1256x809\n"
    with patch("control.windows.subprocess.run", return_value=_completed(stdout=out)):
        geo = windows.Window("{96a3}").geometry()
    assert geo == {"x": 332, "y": -14, "width": 1256, "height": 809}
