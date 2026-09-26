"""Tests for apps.py, mocking subprocess.run and windows.wait_for_window so no real
gtk-launch/kdotool call happens.

POSIX-only: the assertions are about the .desktop catalog and gtk-launch. The
Windows catalog (Start Menu shortcuts) is covered by test_win_native.py.
"""
from __future__ import annotations

import os
import subprocess
from unittest.mock import patch

import pytest

from control import apps, windows

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="exercises the .desktop/gtk-launch backend (POSIX only)")


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestLaunch:
    def test_launch_without_wait_returns_completed_process(self):
        with patch("control.apps.subprocess.run", return_value=_completed()) as mock_run, \
             patch("control.apps.windows.wait_for_window") as mock_wait:
            result = apps.launch("firefox")
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == ["gtk-launch", "firefox"]
        kwargs = mock_run.call_args[1]
        # Pipes would be inherited by the GUI app and hang run() until it exits.
        assert "capture_output" not in kwargs
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] not in (subprocess.PIPE, None)
        mock_wait.assert_not_called()
        assert result.returncode == 0

    def test_launch_raises_on_gtk_launch_failure(self):
        def fail(cmd, stderr, **_kwargs):
            stderr.write("gtk-launch: no such application nope")
            return _completed(returncode=1)

        with patch("control.apps.subprocess.run", side_effect=fail):
            with pytest.raises(apps.AppLaunchError, match="no such application"):
                apps.launch("bogus-app")

    def test_launch_with_desktop_moves_the_new_window_there(self):
        fake_win = windows.Window(id="42", title="KCalc", classname="kcalc")
        with patch("control.apps.subprocess.run", return_value=_completed()), \
             patch("control.apps.windows.window_ids", return_value=[]), \
             patch("control.apps.windows.wait_for_window", return_value=fake_win) as mock_wait, \
             patch.object(windows.Window, "move_to_desktop") as mock_move, \
             patch.object(windows.Window, "activate"):
            result = apps.launch("org.kde.kcalc", desktop=3, window_pattern="kcalc", timeout=5)
        mock_wait.assert_called_once_with("kcalc", timeout=5, known_ids=set(), activate=False)
        mock_move.assert_called_once_with(3)
        assert result is fake_win

    def test_launch_with_desktop_never_claims_a_pre_existing_window(self):
        """Another agent's (or the user's) kcalc must not be hijacked."""
        with patch("control.apps.subprocess.run", return_value=_completed()), \
             patch("control.apps.windows.window_ids", return_value=["old"]), \
             patch("control.apps.windows.wait_for_window", return_value=None) as mock_wait:
            apps.launch("org.kde.kcalc", desktop=3, window_pattern="kcalc", timeout=5)
        assert mock_wait.call_args.kwargs["known_ids"] == {"old"}

    def test_launch_with_desktop_returns_none_when_no_window_appears(self):
        with patch("control.apps.subprocess.run", return_value=_completed()), \
             patch("control.apps.windows.window_ids", return_value=[]), \
             patch("control.apps.windows.wait_for_window", return_value=None), \
             patch.object(windows.Window, "move_to_desktop") as mock_move:
            assert apps.launch("slow", desktop=2, timeout=1) is None
        mock_move.assert_not_called()

    def test_launch_with_wait_polls_for_window_and_returns_it(self):
        fake_win = windows.Window(id="42", title="Kdenlive", classname="kdenlive")
        with patch("control.apps.subprocess.run", return_value=_completed()), \
             patch("control.apps.windows.window_ids", return_value=[]), \
             patch("control.apps.windows.wait_for_window", return_value=fake_win) as mock_wait, \
             patch.object(windows.Window, "activate"):
            result = apps.launch("kdenlive", wait=True, timeout=15)
        mock_wait.assert_called_once_with("kdenlive", timeout=15, known_ids=set(), activate=False)
        assert result is fake_win

    def test_launch_with_wait_uses_custom_window_pattern(self):
        with patch("control.apps.subprocess.run", return_value=_completed()), \
             patch("control.apps.windows.window_ids", return_value=[]) as mock_ids, \
             patch("control.apps.windows.wait_for_window", return_value=None) as mock_wait:
            apps.launch("org.kde.kdenlive", wait=True, timeout=5, window_pattern="Kdenlive")
        mock_ids.assert_called_once_with("Kdenlive")
        assert mock_wait.call_args.args == ("Kdenlive",)

    def test_launch_with_wait_returns_none_on_timeout(self):
        with patch("control.apps.subprocess.run", return_value=_completed()), \
             patch("control.apps.windows.window_ids", return_value=[]), \
             patch("control.apps.windows.wait_for_window", return_value=None):
            result = apps.launch("slow-app", wait=True, timeout=1)
        assert result is None


class TestLaunchRaceAndFocus:
    """W1/W2: a plain --wait must not return a window that was already open,
    and the new window must end up focused."""

    def test_wait_takes_the_baseline_before_gtk_launch_runs(self):
        order = []
        with patch("control.apps.windows.window_ids",
                   side_effect=lambda p: order.append("baseline") or ["old"]), \
             patch("control.apps.subprocess.run",
                   side_effect=lambda *a, **k: order.append("launch") or _completed()), \
             patch("control.apps.windows.wait_for_window", return_value=None) as mock_wait:
            apps.launch("kcalc", wait=True, timeout=1)
        assert order == ["baseline", "launch"]
        assert mock_wait.call_args.kwargs["known_ids"] == {"old"}

    def test_new_window_is_activated_after_being_moved(self):
        win = windows.Window(id="new")
        calls = []
        with patch("control.apps.subprocess.run", return_value=_completed()), \
             patch("control.apps.windows.window_ids", return_value=[]), \
             patch("control.apps.windows.wait_for_window", return_value=win), \
             patch.object(windows.Window, "move_to_desktop",
                          side_effect=lambda n: calls.append(("move", n))), \
             patch.object(windows.Window, "activate",
                          side_effect=lambda: calls.append(("activate",))):
            apps.launch("kcalc", desktop=4, timeout=1)
        assert calls == [("move", 4), ("activate",)]

    def test_activation_can_be_turned_off(self):
        with patch("control.apps.subprocess.run", return_value=_completed()), \
             patch("control.apps.windows.window_ids", return_value=[]), \
             patch("control.apps.windows.wait_for_window", return_value=windows.Window(id="n")), \
             patch.object(windows.Window, "activate") as mock_activate:
            apps.launch("kcalc", wait=True, timeout=1, activate=False)
        mock_activate.assert_not_called()

    def test_launched_app_does_not_inherit_secrets(self, monkeypatch):
        monkeypatch.setenv("CONTROL_SUDO_PASSWORD", "hunter2")
        with patch("control.apps.subprocess.run", return_value=_completed()) as mock_run:
            apps.launch("firefox")
        assert "CONTROL_SUDO_PASSWORD" not in mock_run.call_args.kwargs["env"]
