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

        with patch("control.input.subprocess.run", side_effect=record), \
             patch("control.input._sleep"):
            inp.drag(0, 0, 10, 10, steps=3)
        assert len(depths) == 6  # move, down, 3 moves, up
        assert all(d >= 2 for d in depths), "drag must not release between press and release"
        assert not inp.input_mutex.held

    def test_click_with_coords_is_one_atomic_gesture(self):
        depths = []

        def record(*_args, **_kwargs):
            depths.append(inp.input_mutex._depth)
            return _completed()

        with patch("control.input.subprocess.run", side_effect=record):
            inp.click("left", 5, 5)
        # move + click, both under the same outer hold (move_mouse nests its own).
        assert len(depths) == 2 and all(d >= 2 for d in depths)

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


class TestDrag:
    def _calls(self, **kwargs):
        with patch("control.input.subprocess.run", return_value=_completed()) as mock_run, \
             patch("control.input._sleep") as mock_sleep:
            inp.drag(0, 0, 100, 50, **kwargs)
        return [c[0][0][1:] for c in mock_run.call_args_list], mock_sleep

    def test_glides_in_steps_and_ends_exactly_on_target(self):
        calls, _ = self._calls(steps=4)
        assert calls[0] == ["mousemove", "--absolute", "-x", "0", "-y", "0"]
        assert calls[1] == ["click", "0x40"]
        moves = [(int(c[3]), int(c[5])) for c in calls[2:-1]]
        assert moves == [(25, 12), (50, 25), (75, 38), (100, 50)]
        assert calls[-1] == ["click", "0x80"]

    def test_pauses_after_press_and_before_release(self):
        _, mock_sleep = self._calls(steps=1, hold_ms=200)
        assert [c[0][0] for c in mock_sleep.call_args_list] == [0.2, 0.2]

    def test_button_is_released_even_if_a_move_fails(self):
        issued = []

        def run(args, **_kwargs):
            issued.append(args[1:])
            if args[1] == "mousemove" and len(issued) > 2:
                return _completed(stderr="boom", returncode=1)
            return _completed()

        with patch("control.input.subprocess.run", side_effect=run), \
             patch("control.input._sleep"):
            with pytest.raises(inp.YdotoolError, match="boom"):
                inp.drag(0, 0, 10, 10, steps=5)
        assert issued[-1] == ["click", "0x80"]
        assert not inp.input_mutex.held


class TestClipboard:
    def test_copy_pipes_the_text_into_wl_copy(self):
        with patch("control.input.shutil.which", return_value="/usr/bin/wl-copy"), \
             patch("control.input.subprocess.run", return_value=_completed()) as mock_run:
            inp.copy_to_clipboard("hola", mime="text/plain")
        args, kwargs = mock_run.call_args
        assert args[0] == ["/usr/bin/wl-copy", "--type", "text/plain"]
        assert kwargs["input"] == b"hola"
        # wl-copy's background child would hold a pipe open forever.
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is not subprocess.PIPE

    def test_missing_wl_clipboard_is_a_clear_error(self):
        with patch("control.input.shutil.which", return_value=None):
            with pytest.raises(inp.ClipboardError, match="wl-clipboard"):
                inp.copy_to_clipboard("x")

    def test_files_become_a_percent_encoded_uri_list(self, tmp_path):
        deck = tmp_path / "mi deck.pptx"
        deck.write_bytes(b"x")
        with patch("control.input.shutil.which", return_value="/usr/bin/wl-copy"), \
             patch("control.input.subprocess.run", return_value=_completed()) as mock_run:
            paths = inp.copy_files([deck])
        assert paths == [str(deck)]
        assert mock_run.call_args[0][0][-2:] == ["--type", "text/uri-list"]
        assert mock_run.call_args[1]["input"] == f"{deck.as_uri()}\r\n".encode()
        assert "mi%20deck.pptx" in deck.as_uri()

    def test_missing_file_is_refused_before_touching_the_clipboard(self, tmp_path):
        with patch("control.input.subprocess.run") as mock_run:
            with pytest.raises(inp.ClipboardError, match="no such file"):
                inp.copy_files([tmp_path / "nope.pptx"])
        mock_run.assert_not_called()

    def test_empty_clipboard_reads_as_empty_string(self):
        with patch("control.input.shutil.which", return_value="/usr/bin/wl-paste"), \
             patch("control.input.subprocess.run",
                   return_value=_completed(stdout=b"", stderr=b"No selection", returncode=1)):
            assert inp.read_clipboard() == ""


class FakePointer:
    """A pointer with libinput-like acceleration, fed by ydotool mousemove calls.
    Mimics ydotool 1.0: --absolute always lands on 1,1."""

    def __init__(self, width=1920, height=1080):
        self.x, self.y = 500, 500
        self.w, self.h = width, height
        self.calls: list[list[str]] = []

    def run(self, args, **_kwargs):
        self.calls.append(args[1:])
        if args[1] == "mousemove":
            if "--absolute" in args:
                self.x, self.y = 1, 1
            else:
                dx, dy = int(args[args.index("-x") + 1]), int(args[args.index("-y") + 1])
                gain = 1.0 if max(abs(dx), abs(dy)) <= 16 else 1.9
                self.x = min(max(0, self.x + round(dx * gain)), self.w - 1)
                self.y = min(max(0, self.y + round(dy * gain)), self.h - 1)
        return _completed()

    def position(self):
        return self.x, self.y


class TestPointerPlacement:
    def _patched(self, pointer):
        return patch("control.input.subprocess.run", side_effect=pointer.run), \
               patch("control.input.pointer_position", side_effect=pointer.position)

    def test_move_mouse_corrects_ydotools_absolute_move(self):
        pointer = FakePointer()
        run, pos = self._patched(pointer)
        with run, pos:
            inp.move_mouse(960, 618)
        assert pointer.position() == (960, 618)
        assert len(pointer.calls) < 12

    def test_off_screen_target_is_an_error_not_a_silent_miss(self):
        pointer = FakePointer()
        run, pos = self._patched(pointer)
        with run, pos, pytest.raises(inp.YdotoolError, match="did not reach 5000,10"):
            inp.move_mouse(5000, 10)

    def test_without_feedback_the_absolute_move_is_all_there_is(self):
        with patch("control.input.subprocess.run", return_value=_completed()) as mock_run, \
             patch("control.input.pointer_position", return_value=None):
            inp.move_mouse(10, 20)
        assert [c[0][0][1:] for c in mock_run.call_args_list] == [
            ["mousemove", "--absolute", "-x", "10", "-y", "20"]]

    def test_drag_never_moves_absolutely_while_the_button_is_down(self):
        pointer = FakePointer()
        run, pos = self._patched(pointer)
        with run, pos, patch("control.input._sleep"):
            inp.drag(100, 100, 400, 250, steps=5)
        down = pointer.calls.index(["click", "0x40"])
        up = pointer.calls.index(["click", "0x80"])
        assert not any("--absolute" in c for c in pointer.calls[down:up])
        assert pointer.position() == (400, 250)
