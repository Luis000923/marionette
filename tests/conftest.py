"""Keep tests off the real desktop state: no shared lock file, no real ~/.local state.

Without this, a test run would create session directories in the developer's home
and contend on /tmp/control_input.lock with any agent actually using the tool.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_control_state(tmp_path, monkeypatch):
    # cli.bootstrap() writes straight into os.environ (--session, --env-file, the
    # repo .env); snapshot and restore it so no test leaks config into the next.
    saved_env = dict(os.environ)
    monkeypatch.setenv("CONTROL_STATE_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("CONTROL_INPUT_LOCK", str(tmp_path / "input.lock"))
    monkeypatch.delenv("CONTROL_SESSION_ID", raising=False)
    monkeypatch.delenv("CONTROL_INPUT_LOCK_DISABLED", raising=False)
    monkeypatch.delenv("CONTROL_INPUT_LOCK_TIMEOUT", raising=False)
    # Hermetic: with an empty PATH any kdotool/gdbus/ydotool/sudo call a test
    # forgot to mock fails with FileNotFoundError instead of driving the real
    # desktop (it once returned -- and could have activated -- a live window).
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.delenv("CONTROL_JSON", raising=False)
    monkeypatch.delenv("CONTROL_ENV_STRICT", raising=False)
    monkeypatch.delenv("CONTROL_SUDO_PASSWORD", raising=False)
    # Session commands re-resolve their desktop through KWin; never ask the real
    # compositor from a test (tests that exercise that path patch it themselves).
    from control import env, session

    def no_kwin():
        raise session.windows.DesktopError("KWin is not reachable from tests")

    monkeypatch.setattr(session, "_list_live_desktops", no_kwin)
    # The repo .env belongs to the developer, not to the test run.
    monkeypatch.setattr(env, "_DEFAULT_ENV_PATH", tmp_path / "repo.env")
    env._reset_for_tests()
    yield
    env._reset_for_tests()
    os.environ.clear()
    os.environ.update(saved_env)
