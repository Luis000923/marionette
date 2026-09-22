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
