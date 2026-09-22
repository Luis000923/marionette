"""Tests for sudo.py, mocking subprocess.run so no real sudo is ever invoked."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from control import sudo


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestRunAsRoot:
    def test_raises_when_password_unset(self, monkeypatch):
        monkeypatch.delenv("CONTROL_SUDO_PASSWORD", raising=False)
        with patch("control.sudo.load_env"):
            with pytest.raises(sudo.SudoError, match="CONTROL_SUDO_PASSWORD"):
                sudo.run_as_root(["systemctl", "restart", "ydotoold"])

    def test_pipes_password_via_stdin_not_argv(self, monkeypatch):
        monkeypatch.setenv("CONTROL_SUDO_PASSWORD", "hunter2")
        with patch("control.sudo.load_env"), \
             patch("control.sudo.subprocess.run", return_value=_completed(stdout="ok")) as mock_run:
            result = sudo.run_as_root(["systemctl", "restart", "ydotoold"])

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == ["sudo", "-S", "systemctl", "restart", "ydotoold"]
        # password must never appear in the argv list
        assert "hunter2" not in args[0]
        assert kwargs["input"] == "hunter2\n"
        assert result.ok
        assert result.returncode == 0
        assert result.stdout == "ok"

    def test_returns_nonzero_result_without_raising(self, monkeypatch):
        monkeypatch.setenv("CONTROL_SUDO_PASSWORD", "hunter2")
        with patch("control.sudo.load_env"), \
             patch("control.sudo.subprocess.run", return_value=_completed(stderr="Sorry, try again.", returncode=1)):
            result = sudo.run_as_root(["true"])
        assert not result.ok
        assert result.returncode == 1
        assert "try again" in result.stderr


class TestWrappers:
    def test_restart_ydotoold_calls_systemctl(self, monkeypatch):
        monkeypatch.setenv("CONTROL_SUDO_PASSWORD", "hunter2")
        with patch("control.sudo.load_env"), \
             patch("control.sudo.subprocess.run", return_value=_completed()) as mock_run:
            sudo.restart_ydotoold()
        args = mock_run.call_args[0][0]
        assert args == ["sudo", "-S", "systemctl", "restart", "ydotoold.service"]

    def test_ensure_uinput_group_calls_usermod(self, monkeypatch):
        monkeypatch.setenv("CONTROL_SUDO_PASSWORD", "hunter2")
        with patch("control.sudo.load_env"), \
             patch("control.sudo.subprocess.run", return_value=_completed()) as mock_run:
            sudo.ensure_uinput_group("alice")
        args = mock_run.call_args[0][0]
        assert args == ["sudo", "-S", "usermod", "-aG", "ydotoold", "alice"]
