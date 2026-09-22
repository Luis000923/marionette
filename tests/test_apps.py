"""Tests for apps.py, mocking subprocess.run and windows.wait_for_window so no real
gtk-launch/kdotool call happens."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from control import apps, windows


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestLaunch:
    def test_launch_without_wait_returns_completed_process(self):
        with patch("control.apps.subprocess.run", return_value=_completed()) as mock_run, \
             patch("control.apps.windows.wait_for_window") as mock_wait:
            result = apps.launch("firefox")
        mock_run.assert_called_once_with(
            ["gtk-launch", "firefox"], capture_output=True, text=True, timeout=10
        )
        mock_wait.assert_not_called()
        assert result.returncode == 0

    def test_launch_raises_on_gtk_launch_failure(self):
        with patch("control.apps.subprocess.run", return_value=_completed(stderr="nope", returncode=1)):
            with pytest.raises(apps.AppLaunchError, match="nope"):
                apps.launch("bogus-app")

    def test_launch_with_wait_polls_for_window_and_returns_it(self):
        fake_win = windows.Window(id="42", title="Kdenlive", classname="kdenlive")
        with patch("control.apps.subprocess.run", return_value=_completed()), \
             patch("control.apps.windows.wait_for_window", return_value=fake_win) as mock_wait:
            result = apps.launch("kdenlive", wait=True, timeout=15)
        mock_wait.assert_called_once_with("kdenlive", timeout=15)
        assert result is fake_win

    def test_launch_with_wait_uses_custom_window_pattern(self):
        with patch("control.apps.subprocess.run", return_value=_completed()), \
             patch("control.apps.windows.wait_for_window", return_value=None) as mock_wait:
            apps.launch("org.kde.kdenlive", wait=True, timeout=5, window_pattern="Kdenlive")
        mock_wait.assert_called_once_with("Kdenlive", timeout=5)

    def test_launch_with_wait_returns_none_on_timeout(self):
        with patch("control.apps.subprocess.run", return_value=_completed()), \
             patch("control.apps.windows.wait_for_window", return_value=None):
            result = apps.launch("slow-app", wait=True, timeout=1)
        assert result is None
