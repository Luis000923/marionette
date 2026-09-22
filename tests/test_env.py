"""Tests for env.py, the minimal .env parser/loader."""
from __future__ import annotations

from control import env


class TestParseEnv:
    def test_parses_simple_key_value(self):
        assert env.parse_env("FOO=bar\n") == {"FOO": "bar"}

    def test_ignores_comments_and_blank_lines(self):
        text = "\n# a comment\nFOO=bar\n\n# another\nBAZ=qux\n"
        assert env.parse_env(text) == {"FOO": "bar", "BAZ": "qux"}

    def test_strips_matching_quotes(self):
        assert env.parse_env('FOO="bar baz"\n') == {"FOO": "bar baz"}
        assert env.parse_env("FOO='bar baz'\n") == {"FOO": "bar baz"}

    def test_empty_quoted_value(self):
        assert env.parse_env('CONTROL_SUDO_PASSWORD=""\n') == {"CONTROL_SUDO_PASSWORD": ""}

    def test_ignores_lines_without_equals(self):
        assert env.parse_env("not a valid line\nFOO=bar\n") == {"FOO": "bar"}


class TestLoadEnv:
    def test_loads_from_file_into_environ(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MY_TEST_VAR", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("MY_TEST_VAR=hello\n")
        result = env.load_env(env_file)
        assert result == {"MY_TEST_VAR": "hello"}
        assert __import__("os").environ["MY_TEST_VAR"] == "hello"

    def test_missing_file_is_a_noop(self, tmp_path):
        assert env.load_env(tmp_path / "does-not-exist.env") == {}

    def test_does_not_override_existing_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_TEST_VAR2", "original")
        env_file = tmp_path / ".env"
        env_file.write_text("MY_TEST_VAR2=fromfile\n")
        env.load_env(env_file)
        assert __import__("os").environ["MY_TEST_VAR2"] == "original"

    def test_override_true_overwrites_existing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_TEST_VAR3", "original")
        env_file = tmp_path / ".env"
        env_file.write_text("MY_TEST_VAR3=fromfile\n")
        env.load_env(env_file, override=True)
        assert __import__("os").environ["MY_TEST_VAR3"] == "fromfile"


class TestPermissions:
    """S2: an .env readable by other users is flagged (or refused in strict mode)."""

    def _file(self, tmp_path, mode, text="SAFE_VAR=1\n"):
        path = tmp_path / ".env"
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_private_file_loads_silently(self, tmp_path, capsys):
        env.load_env(self._file(tmp_path, 0o600))
        assert capsys.readouterr().err == ""

    def test_world_readable_file_warns_once(self, tmp_path, capsys):
        path = self._file(tmp_path, 0o644)
        env.load_env(path)
        env.load_env(path)
        err = capsys.readouterr().err
        assert err.count("insecure env file") == 1
        assert "0644" in err and "chmod 600" in err

    def test_group_readable_file_warns_too(self, tmp_path, capsys):
        env.load_env(self._file(tmp_path, 0o640))
        assert "0640" in capsys.readouterr().err

    def test_strict_mode_refuses_to_load(self, tmp_path, monkeypatch):
        import os

        import pytest

        monkeypatch.setenv("CONTROL_ENV_STRICT", "1")
        monkeypatch.delenv("SAFE_VAR", raising=False)
        with pytest.raises(env.EnvPermissionError, match="0644"):
            env.load_env(self._file(tmp_path, 0o644))
        assert "SAFE_VAR" not in os.environ


class TestSecrets:
    def test_secret_keys_are_never_exported(self, tmp_path, monkeypatch):
        import os

        path = tmp_path / ".env"
        path.write_text("CONTROL_SUDO_PASSWORD=hunter2\nPLAIN=ok\n")
        path.chmod(0o600)
        values = env.load_env(path)
        assert values == {"PLAIN": "ok"}
        assert "CONTROL_SUDO_PASSWORD" not in os.environ

    def test_get_secret_follows_load_precedence(self, tmp_path):
        repo = tmp_path / "repo.env"
        agent = tmp_path / "agent.env"
        for path, value in ((repo, "repo"), (agent, "agent")):
            path.write_text(f"CONTROL_SUDO_PASSWORD={value}\n")
            path.chmod(0o600)
        env.load_env(repo)
        env.load_env(agent, override=True)  # like --env-file
        assert env.get_secret("CONTROL_SUDO_PASSWORD") == "agent"

    def test_get_secret_prefers_an_explicit_export(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONTROL_SUDO_PASSWORD", "exported")
        assert env.get_secret("CONTROL_SUDO_PASSWORD", paths=[]) == "exported"

    def test_get_secret_returns_none_when_absent(self, tmp_path):
        assert env.get_secret("CONTROL_SUDO_PASSWORD", paths=[tmp_path / "nope"]) is None

    def test_scrubbed_environ_drops_secrets(self, monkeypatch):
        monkeypatch.setenv("CONTROL_SUDO_PASSWORD", "x")
        monkeypatch.setenv("KEEP_ME", "y")
        scrubbed = env.scrubbed_environ()
        assert "CONTROL_SUDO_PASSWORD" not in scrubbed
        assert scrubbed["KEEP_ME"] == "y"
