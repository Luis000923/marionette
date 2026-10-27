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
