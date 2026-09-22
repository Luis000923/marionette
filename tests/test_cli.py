"""Tests for cli.py argument parsing and the --json output path.

These mock the underlying windows.search / apps.list_apps functions so no real
subprocess call or filesystem app scan happens.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from control import apps, cli, windows


def test_build_parser_parses_basic_subcommands():
    parser = cli.build_parser()

    args = parser.parse_args(["win", "list", "foo"])
    assert args.command == "win"
    assert args.win_command == "list"
    assert args.pattern == "foo"
    assert args.json is False

    args = parser.parse_args(["key", "ctrl", "shift", "t"])
    assert args.keys == ["ctrl", "shift", "t"]

    args = parser.parse_args(["browser", "goto", "https://example.com"])
    assert args.command == "browser"
    assert args.browser_command == "goto"
    assert args.url == "https://example.com"

    args = parser.parse_args(["app", "list", "--json"])
    assert args.json is True


def test_win_list_plain_output(capsys):
    fake_windows = [windows.Window(id="1", title="Term", classname="konsole")]
    parser = cli.build_parser()
    args = parser.parse_args(["win", "list"])
    with patch("control.cli.windows.search", return_value=fake_windows) as mock_search:
        args.func(args)
    mock_search.assert_called_once_with("")
    out = capsys.readouterr().out
    assert out == "1\tkonsole\tTerm\n"


def test_win_list_json_output(capsys):
    fake_windows = [
        windows.Window(id="1", title="Term", classname="konsole"),
        windows.Window(id="2", title="Files", classname="dolphin"),
    ]
    parser = cli.build_parser()
    args = parser.parse_args(["win", "list", "--json"])
    with patch("control.cli.windows.search", return_value=fake_windows):
        args.func(args)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data == [
        {"id": "1", "title": "Term", "class": "konsole"},
        {"id": "2", "title": "Files", "class": "dolphin"},
    ]


def test_app_list_plain_output(capsys):
    fake_apps = [apps.App(app_id="firefox", name="Firefox", exec_="firefox", path=None)]
    parser = cli.build_parser()
    args = parser.parse_args(["app", "list"])
    with patch("control.cli.apps.list_apps", return_value=fake_apps):
        args.func(args)
    out = capsys.readouterr().out
    assert out == "firefox\tFirefox\n"


def test_app_list_json_output(capsys):
    fake_apps = [apps.App(app_id="firefox", name="Firefox", exec_="firefox", path=None)]
    parser = cli.build_parser()
    args = parser.parse_args(["app", "list", "--json"])
    with patch("control.cli.apps.list_apps", return_value=fake_apps):
        args.func(args)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data == [{"id": "firefox", "name": "Firefox"}]


def test_app_list_json_uses_find_when_query_given(capsys):
    fake_apps = [apps.App(app_id="firefox", name="Firefox", exec_="firefox", path=None)]
    parser = cli.build_parser()
    args = parser.parse_args(["app", "list", "fire", "--json"])
    with patch("control.cli.apps.find", return_value=fake_apps) as mock_find:
        args.func(args)
    mock_find.assert_called_once_with("fire")


def test_app_launch_without_wait_does_not_poll_for_window():
    parser = cli.build_parser()
    args = parser.parse_args(["app", "launch", "firefox"])
    assert args.wait is False
    with patch("control.cli.apps.launch") as mock_launch:
        args.func(args)
    mock_launch.assert_called_once_with("firefox")


def test_app_launch_with_wait_prints_window_info(capsys):
    fake_win = windows.Window(id="9", title="Kdenlive", classname="kdenlive")
    parser = cli.build_parser()
    args = parser.parse_args(["app", "launch", "kdenlive", "--wait", "--timeout", "5"])
    with patch("control.cli.apps.launch", return_value=fake_win) as mock_launch:
        args.func(args)
    mock_launch.assert_called_once_with("kdenlive", wait=True, timeout=5.0, window_pattern=None)
    out = capsys.readouterr().out
    assert out == "9\tkdenlive\tKdenlive\n"


def test_app_launch_with_wait_exits_when_no_window_appears():
    parser = cli.build_parser()
    args = parser.parse_args(["app", "launch", "slow-app", "--wait"])
    with patch("control.cli.apps.launch", return_value=None):
        with pytest.raises(SystemExit):
            args.func(args)


class TestMainErrorHandling:
    def test_main_reports_kdotool_error_friendly(self, capsys):
        with patch("control.cli.build_parser") as mock_build:
            parser = mock_build.return_value
            fake_args = parser.parse_args.return_value
            fake_args.func.side_effect = windows.KdotoolError("no such window")
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "[ERROR] kdotool_error" in err
        assert "kdotool" in err.lower()

    def test_main_reports_ydotool_error_friendly(self, capsys):
        with patch("control.cli.build_parser") as mock_build:
            parser = mock_build.return_value
            fake_args = parser.parse_args.return_value
            fake_args.func.side_effect = cli.inp.YdotoolError("daemon not running")
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "[ERROR] ydotool_error" in err
        assert "ydotoold" in err

    def test_main_reports_app_launch_error_friendly(self, capsys):
        with patch("control.cli.build_parser") as mock_build:
            parser = mock_build.return_value
            fake_args = parser.parse_args.return_value
            fake_args.func.side_effect = apps.AppLaunchError("unknown app")
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "[ERROR] app_launch_error" in err
        assert "control app list" in err

    def test_main_reraises_unexpected_errors(self, capsys):
        with patch("control.cli.build_parser") as mock_build:
            parser = mock_build.return_value
            fake_args = parser.parse_args.return_value
            fake_args.func.side_effect = ValueError("something else broke")
            with pytest.raises(ValueError):
                cli.main()
        err = capsys.readouterr().err
        assert "[ERROR] unexpected" in err
