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
        with patch("control.sudo.get_secret", return_value=None):
            with pytest.raises(sudo.SudoError, match="CONTROL_SUDO_PASSWORD"):
                sudo.run_as_root(["systemctl", "restart", "ydotoold"])

    def test_pipes_password_via_stdin_not_argv(self, monkeypatch):
        monkeypatch.setenv("CONTROL_SUDO_PASSWORD", "hunter2")
        with patch("control.sudo.subprocess.run", return_value=_completed(stdout="ok")) as mock_run:
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
        with patch("control.sudo.subprocess.run", return_value=_completed(stderr="Sorry, try again.", returncode=1)):
            result = sudo.run_as_root(["true"])
        assert not result.ok
        assert result.returncode == 1
        assert "try again" in result.stderr


class TestWrappers:
    def test_restart_ydotoold_calls_systemctl(self, monkeypatch):
        monkeypatch.setenv("CONTROL_SUDO_PASSWORD", "hunter2")
        with patch("control.sudo.subprocess.run", return_value=_completed()) as mock_run:
            sudo.restart_ydotoold()
        args = mock_run.call_args[0][0]
        assert args == ["sudo", "-S", "systemctl", "restart", "ydotoold.service"]

    def test_ensure_uinput_group_calls_usermod(self, monkeypatch):
        monkeypatch.setenv("CONTROL_SUDO_PASSWORD", "hunter2")
        with patch("control.sudo.subprocess.run", return_value=_completed()) as mock_run:
            sudo.ensure_uinput_group("alice")
        args = mock_run.call_args[0][0]
        assert args == ["sudo", "-S", "usermod", "-aG", "ydotoold", "alice"]


class TestNoCredentialLeak:
    """S1: the password travels only over sudo's stdin."""

    def _env_file(self, tmp_path, mode=0o600):
        path = tmp_path / "secret.env"
        path.write_text("CONTROL_SUDO_PASSWORD=hunter2\nOTHER=1\n")
        path.chmod(mode)
        return path

    def test_password_is_read_from_the_file_but_never_exported(self, tmp_path, monkeypatch):
        import os

        from control import env

        path = self._env_file(tmp_path)
        monkeypatch.setattr(env, "_DEFAULT_ENV_PATH", path)
        env.load_env(path)
        assert "CONTROL_SUDO_PASSWORD" not in os.environ
        assert os.environ["OTHER"] == "1"

        with patch("control.sudo.subprocess.run", return_value=_completed()) as mock_run:
            sudo.run_as_root(["true"])
        kwargs = mock_run.call_args.kwargs
        assert kwargs["input"] == "hunter2\n"
        assert "CONTROL_SUDO_PASSWORD" not in os.environ
        assert "CONTROL_SUDO_PASSWORD" not in kwargs["env"]

    def test_an_exported_password_is_not_passed_on_to_the_child(self, monkeypatch):
        monkeypatch.setenv("CONTROL_SUDO_PASSWORD", "hunter2")
        with patch("control.sudo.subprocess.run", return_value=_completed()) as mock_run:
            sudo.run_as_root(["true"])
        assert "CONTROL_SUDO_PASSWORD" not in mock_run.call_args.kwargs["env"]
        assert mock_run.call_args.kwargs["input"] == "hunter2\n"

    def test_refuses_a_world_readable_password_file(self, tmp_path, monkeypatch):
        from control import env

        monkeypatch.setattr(env, "_DEFAULT_ENV_PATH", self._env_file(tmp_path, mode=0o644))
        with patch("control.sudo.subprocess.run") as mock_run:
            with pytest.raises(sudo.SudoError, match="chmod 600"):
                sudo.run_as_root(["true"])
        mock_run.assert_not_called()
