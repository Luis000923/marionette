"""Brave launcher and snapshot compaction."""
import pytest

from control import browser


def test_compact_text_collapses_and_caps():
    text, cut = browser.compact_text("a  \n\n\n\n\nb\n", 100)
    assert text == "a\n\nb" and not cut
    text, cut = browser.compact_text("x" * 50, 10)
    assert cut and text.startswith("x" * 10) and "truncated 40" in text


def test_compact_text_zero_means_no_cap():
    text, cut = browser.compact_text("x" * 50, 0)
    assert text == "x" * 50 and not cut


def test_launch_brave_noop_when_port_up(monkeypatch):
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.0: True)
    assert browser.launch_brave(9222) == {"port": 9222, "started": False}


def test_launch_brave_missing_binary(monkeypatch):
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.0: False)
    monkeypatch.setattr(browser, "find_brave", lambda: None)
    with pytest.raises(browser.BrowserError, match="Brave not found"):
        browser.launch_brave(9222)


def test_launch_brave_uses_loopback_and_private_profile(monkeypatch, tmp_path):
    calls = []
    states = iter([False, True])
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.0: next(states))
    monkeypatch.setattr(browser, "find_brave", lambda: "/usr/bin/brave")
    monkeypatch.setattr(browser.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    info = browser.launch_brave(9333, tmp_path / "p")
    args, kw = calls[0]
    assert "--remote-debugging-address=127.0.0.1" in args
    assert "--remote-debugging-port=9333" in args
    assert f"--user-data-dir={tmp_path / 'p'}" in args
    assert kw["start_new_session"] is True and info["started"] is True


def test_launch_browser_chromium_uses_bundled_binary(monkeypatch, tmp_path):
    calls = []
    states = iter([False, True])
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.0: next(states))
    monkeypatch.setattr(browser, "find_chromium", lambda: "/pw/chrome")
    monkeypatch.setattr(browser.subprocess, "Popen", lambda args, **kw: calls.append(args))
    info = browser.launch_browser("chromium", 9444, tmp_path / "p")
    assert calls[0][0] == "/pw/chrome" and "--remote-debugging-port=9444" in calls[0]
    assert info["started"] is True


def test_launch_browser_chromium_missing(monkeypatch):
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.0: False)
    monkeypatch.setattr(browser, "find_chromium", lambda: None)
    with pytest.raises(browser.BrowserError):
        browser.launch_browser("chromium", 9444)


def test_obscura_fetch_parses_title_and_builds_command(monkeypatch):
    seen = {}

    class R:
        returncode = 0
        stdout = "body text"
        stderr = 'Fetching u...\nPage loaded: https://x.test/ - "Titulo"\n'

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return R()

    monkeypatch.setattr(browser.subprocess, "run", fake_run)
    res = browser.obscura_fetch("https://x.test", "markdown", selector="main", exe="/o")
    assert res == {"url": "https://x.test/", "title": "Titulo", "content": "body text"}
    assert seen["cmd"][:2] == ["/o", "fetch"] and "--selector" in seen["cmd"]
    assert "--allow-private-network" not in seen["cmd"]


def test_obscura_fetch_missing_binary(monkeypatch):
    monkeypatch.setattr(browser, "find_obscura", lambda: None)
    with pytest.raises(browser.BrowserError):
        browser.obscura_fetch("https://x.test")


def test_find_obscura_honours_env(monkeypatch, tmp_path):
    exe = tmp_path / "obscura"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("CONTROL_OBSCURA", str(exe))
    assert browser.find_obscura() == str(exe)


def test_relaunch_requires_confirmation():
    import argparse
    from control import cli
    args = argparse.Namespace(window_class="brave-browser", port=9222, yes=False, json=True)
    with pytest.raises(cli.CliError) as e:
        cli._cmd_browser_relaunch(args)
    assert e.value.kind == "confirmation_required"


def test_relaunch_finds_process_by_window_class(monkeypatch):
    class PS:
        stdout = ("zsh /usr/bin/zsh -l\n"
                  "brave /opt/brave-bin/brave --disable-features=X\n"
                  "brave /opt/brave-bin/brave --type=renderer --foo\n")

    monkeypatch.setattr(browser, "cdp_alive", lambda *a, **k: False)
    monkeypatch.setattr(browser.subprocess, "run", lambda *a, **k: PS())
    from control import windows
    monkeypatch.setattr(windows, "list_windows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError, match="stop"):
        browser.relaunch_with_cdp("brave-browser")
