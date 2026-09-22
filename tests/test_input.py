"""Tests for input.py, mocking subprocess.run so no real ydotool is invoked."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from control import input as inp


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestRun:
    def test_run_raises_on_nonzero_returncode(self):
        with patch("control.input.subprocess.run", return_value=_completed(stderr="no daemon", returncode=1)):
            with pytest.raises(inp.YdotoolError, match="no daemon"):
                inp._run("click", "0xC0")


class TestClick:
    def test_click_without_coords_only_clicks(self):
        with patch("control.input.subprocess.run", return_value=_completed()) as mock_run:
            inp.click("left")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args == ["ydotool", "click", "0xc0"]

    def test_click_with_coords_moves_then_clicks(self):
        with patch("control.input.subprocess.run", return_value=_completed()) as mock_run:
            inp.click("right", 10, 20)
        assert mock_run.call_count == 2
        move_args = mock_run.call_args_list[0][0][0]
        click_args = mock_run.call_args_list[1][0][0]
        assert move_args == ["ydotool", "mousemove", "--absolute", "-x", "10", "-y", "20"]
        assert click_args == ["ydotool", "click", "0xc1"]


class TestKeyCombo:
    def test_key_combo_presses_in_order_and_releases_in_reverse(self):
        with patch("control.input.subprocess.run", return_value=_completed()) as mock_run:
            inp.key_combo("ctrl", "shift", "t")
        args = mock_run.call_args[0][0]
        ctrl, shift, t = inp.KEYCODES["ctrl"], inp.KEYCODES["shift"], inp.KEYCODES["t"]
        assert args == [
            "ydotool", "key",
            f"{ctrl}:1", f"{shift}:1", f"{t}:1",
            f"{t}:0", f"{shift}:0", f"{ctrl}:0",
        ]

    def test_key_combo_raises_keyerror_for_unknown_key(self):
        with pytest.raises(KeyError):
            inp.key_combo("notarealkey")


class TestTypeText:
    def test_type_text_basic(self):
        with patch("control.input.subprocess.run", return_value=_completed()) as mock_run:
            inp.type_text("hello")
        args = mock_run.call_args[0][0]
        assert args == ["ydotool", "type", "hello"]

    def test_type_text_with_delay(self):
        with patch("control.input.subprocess.run", return_value=_completed()) as mock_run:
            inp.type_text("hi", delay_ms=50)
        args = mock_run.call_args[0][0]
        assert args == ["ydotool", "type", "--key-delay", "50", "hi"]


def test_new_keycodes_present():
    for key in ["kp0", "kp9", "kpenter", "kpdot", "kpminus", "kpplus",
                "printscreen", "scrolllock", "pause", "menu", "numlock"]:
        assert key in inp.KEYCODES


class TestInputSerialization:
    """Every ydotool call must hold the global input mutex; composite gestures
    must hold it for the whole sequence, or a second agent could cut in between
    a button-down and its button-up."""

    def test_run_holds_the_mutex_for_the_subprocess_call(self):
        held_during_call = []

        def record(*_args, **_kwargs):
            held_during_call.append(inp.input_mutex.held)
            return _completed()

        with patch("control.input.subprocess.run", side_effect=record):
            inp._run("click", "0xC0")
        assert held_during_call == [True]
        assert not inp.input_mutex.held  # released afterwards

    def test_mutex_is_released_when_ydotool_fails(self):
        with patch("control.input.subprocess.run", return_value=_completed(stderr="nope", returncode=1)):
            with pytest.raises(inp.YdotoolError):
                inp._run("click", "0xC0")
        assert not inp.input_mutex.held

    def test_drag_holds_the_mutex_across_press_move_and_release(self):
        depths = []

        def record(*_args, **_kwargs):
            # depth > 1 proves the outer gesture lock is still held underneath.
            depths.append(inp.input_mutex._depth)
            return _completed()

        with patch("control.input.subprocess.run", side_effect=record):
            inp.drag(0, 0, 10, 10)
        assert len(depths) == 4  # move, down, move, up
        assert all(d == 2 for d in depths), "drag must not release between press and release"
        assert not inp.input_mutex.held

    def test_click_with_coords_is_one_atomic_gesture(self):
        depths = []

        def record(*_args, **_kwargs):
            depths.append(inp.input_mutex._depth)
            return _completed()

        with patch("control.input.subprocess.run", side_effect=record):
            inp.click("left", 5, 5)
        assert depths == [2, 2]  # move + click, both under the same outer hold

    def test_input_lock_context_manager_covers_several_actions(self):
        depths = []

        def record(*_args, **_kwargs):
            depths.append(inp.input_mutex._depth)
            return _completed()

        with patch("control.input.subprocess.run", side_effect=record):
            with inp.input_lock():
                inp.type_text("hello")
                inp.press_key("enter")
        assert depths == [2, 2]
        assert not inp.input_mutex.held

    def test_a_second_agent_is_blocked_while_input_is_in_flight(self):
        """Simulates another process holding the lock: ydotool must not be called."""
        import threading

        from control import locking

        other = locking.FileMutex()
        started = threading.Event()
        finished = threading.Event()

        def other_agent():
            with other:
                started.set()
                finished.wait(timeout=10)

        thread = threading.Thread(target=other_agent)
        thread.start()
        started.wait(timeout=10)
        try:
            with patch("control.input.subprocess.run", return_value=_completed()) as mock_run:
                with pytest.raises(inp.LockTimeout):
                    inp.input_mutex.acquire(timeout=0.05)
                mock_run.assert_not_called()
        finally:
            finished.set()
            thread.join(timeout=10)

    def test_input_still_works_when_locking_is_disabled(self, monkeypatch):
        monkeypatch.setenv("CONTROL_INPUT_LOCK_DISABLED", "1")
        with patch("control.input.subprocess.run", return_value=_completed()) as mock_run:
            inp.type_text("hi")
        assert mock_run.call_args[0][0] == ["ydotool", "type", "hi"]
