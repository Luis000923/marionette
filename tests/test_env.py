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
