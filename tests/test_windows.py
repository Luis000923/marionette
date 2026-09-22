"""Tests for windows.py, mocking subprocess.run so no real kdotool is invoked."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from control import windows


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
        with patch("control.windows.find_one", return_value=windows.Window(id="1", title="t", classname="c")) as mock_find, \
             patch("control.windows.time.sleep") as mock_sleep:
            win = windows.wait_for_window("t", timeout=5, interval=0.1)
        assert win is not None
        assert win.id == "1"
        mock_find.assert_called_once_with("t")
        mock_sleep.assert_not_called()

    def test_polls_until_window_appears(self):
        results = [None, None, windows.Window(id="2", title="t", classname="c")]
        with patch("control.windows.find_one", side_effect=results), \
             patch("control.windows.time.sleep") as mock_sleep, \
             patch("control.windows.time.monotonic", side_effect=[0, 0, 1, 2, 3]):
            win = windows.wait_for_window("t", timeout=10, interval=0.1)
        assert win is not None
        assert win.id == "2"
        assert mock_sleep.call_count == 2

    def test_returns_none_on_timeout(self):
        with patch("control.windows.find_one", return_value=None), \
             patch("control.windows.time.sleep"), \
             patch("control.windows.time.monotonic", side_effect=[0, 5, 11]):
            win = windows.wait_for_window("nope", timeout=10, interval=0.1)
        assert win is None


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
