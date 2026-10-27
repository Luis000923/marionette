"""Tests for cursor.py: state file and click/restore logic, with kdotool and ydotool mocked."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from control import cursor


@pytest.fixture(autouse=True)
def runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


def _running(x=10, y=20):
    import os
    cursor._save({"pid": os.getpid(), "wid": "{w}", "x": x, "y": y})


def test_status_hidden_without_state():
    assert cursor.status() == {"visible": False, "x": None, "y": None}


def test_status_reports_position_when_overlay_alive():
    _running(5, 6)
    assert cursor.status() == {"visible": True, "x": 5, "y": 6}


def test_stale_pid_counts_as_hidden():
    cursor._save({"pid": 2**22 + 12345, "wid": "{w}", "x": 1, "y": 2})
    assert cursor.status()["visible"] is False


def test_move_updates_state_and_places_overlay():
    _running()
    with patch("control.cursor._place") as place:
        cursor.move(100, 200, duration=0)
    place.assert_called_once_with("{w}", 100, 200)
    assert cursor.status()["x"] == 100 and cursor.status()["y"] == 200


def test_click_restores_real_pointer():
    _running(300, 300)
    with patch("control.cursor.inp.pointer_position", return_value=(7, 8)), \
         patch("control.cursor.inp.click") as click, \
         patch("control.cursor.inp.move_mouse") as mv:
        out = cursor.click("left")
    click.assert_called_once_with("left", 300, 300)
    mv.assert_called_once_with(7, 8)
    assert out["restored"] is True


def test_click_no_restore_leaves_pointer():
    _running(300, 300)
    with patch("control.cursor.inp.click"), patch("control.cursor.inp.move_mouse") as mv:
        out = cursor.click("left", restore=False)
    mv.assert_not_called()
    assert out["restored"] is False
