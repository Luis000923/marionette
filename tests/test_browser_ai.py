"""Tests for the AI-site shortcuts and `browser ask`, driven by fake pages on a
fake clock -- no Chromium is ever launched."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from control import browser, cli


# --- fakes -------------------------------------------------------------------------

class FakeElement:
    def __init__(self, text: str = "", visible: bool = True, enabled: bool = True):
        self.text = text
        self.visible = visible
        self.enabled = enabled
        self.filled: str | None = None
        self.clicks = 0

    def is_visible(self) -> bool:
        return self.visible

    def is_enabled(self) -> bool:
        return self.enabled

    def inner_text(self) -> str:
        return self.text

    def click(self) -> None:
        self.clicks += 1

    def fill(self, text: str) -> None:
        self.filled = text


class FakeKeyboard:
    def __init__(self):
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        self.pressed.append(key)


class FakePage:
    def __init__(self, url: str = "about:blank", elements: dict | None = None):
        self.url = url
        self.elements: dict[str, list[FakeElement]] = elements or {}
        self.keyboard = FakeKeyboard()
        self.visited: list[str] = []

    def goto(self, url: str) -> None:
        self.visited.append(url)
        self.url = url

    def title(self) -> str:
        return ""

    def query_selector_all(self, selector: str) -> list[FakeElement]:
        return list(self.elements.get(selector, []))


class FakeBrowser:
    cdp_port: int | None = None

    def __init__(self, page: FakePage, profile: Path | None = None):
        self._pages = [page]
        self.profile = profile or Path("/nonexistent")

    @property
    def pages(self):
        return self._pages

    def active_tab(self):
        return self._pages[-1]

    def new_tab(self, url: str = "about:blank"):
        page = FakePage(url)
        self._pages.append(page)
        return page


class FakeClock:
    """Replaces browser._now/_sleep; `on_tick` lets a test evolve the page over time."""

    def __init__(self, on_tick=None):
        self.t = 0.0
        self.ticks = 0
        self.on_tick = on_tick

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.ticks += 1
        if self.on_tick:
            self.on_tick(self.ticks)


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(browser, "_now", c.now)
    monkeypatch.setattr(browser, "_sleep", c.sleep)
    return c


GEMINI = browser.AI_SITES["gemini"]
INPUT = GEMINI.input[0]
SEND = GEMINI.send[0]
RESPONSE = GEMINI.response[0]
BUSY = GEMINI.busy[0]


def _gemini_page(**extra) -> FakePage:
    return FakePage("https://gemini.google.com/app", {INPUT: [FakeElement()], **extra})


# --- shortcuts -----------------------------------------------------------------------

class TestShortcuts:
    @pytest.mark.parametrize("alias, url", [
        ("gemini", "https://gemini.google.com/app"),
        ("chatgpt", "https://chatgpt.com/"),
        ("claude", "https://claude.ai/new"),
        ("GPT", "https://chatgpt.com/"),
        ("perplexity", "https://www.perplexity.ai/"),
    ])
    def test_aliases_resolve_to_their_url(self, alias, url):
        assert browser.resolve_url(alias) == url

    def test_urls_and_bare_hosts_pass_through(self):
        assert browser.resolve_url("https://example.com/x") == "https://example.com/x"
        assert browser.resolve_url("example.com") == "https://example.com"
        assert browser.resolve_url("about:blank") == "about:blank"

    def test_unknown_word_is_an_error_not_a_search(self):
        with pytest.raises(browser.UnknownSite, match="known:"):
            browser.resolve_url("gemnii")

    def test_goto_uses_the_alias(self):
        page = FakePage()
        page.title = lambda: "Gemini"

        class B(FakeBrowser):
            def goto(self, url):
                page.goto(url)
                return page

        assert cli.browser_goto(B(page), "gemini") == {
            "url": "https://gemini.google.com/app", "title": "Gemini"}

    def test_overrides_are_tried_before_the_builtin_selectors(self, tmp_path, monkeypatch):
        override = tmp_path / "sites.json"
        override.write_text(json.dumps({
            "gemini": {"input": ["div.new-editor"]},
            "local": {"url": "http://localhost:8080/chat", "input": ["textarea"],
                      "response": [".answer"]},
        }))
        monkeypatch.setenv("CONTROL_AI_SITES", str(override))
        gemini = browser.get_site("gemini")
        assert gemini.input[0] == "div.new-editor"
        assert gemini.input[1:] == GEMINI.input
        local = browser.get_site("local")
        assert local.can_ask and local.hosts == ("localhost",)

    def test_broken_override_file_is_reported(self, tmp_path, monkeypatch):
        bad = tmp_path / "sites.json"
        bad.write_text("{nope")
        monkeypatch.setenv("CONTROL_AI_SITES", str(bad))
        with pytest.raises(browser.BrowserError, match="CONTROL_AI_SITES"):
            browser.sites()

    def test_sites_command_json(self, capsys):
        args = cli.build_parser().parse_args(["browser", "sites", "--json"])
        args.func(args)
        data = {s["name"]: s for s in json.loads(capsys.readouterr().out)}
        assert data["gemini"]["ask"] is True
        assert data["perplexity"]["ask"] is False


# --- ask -------------------------------------------------------------------------------

class TestAsk:
    def test_types_sends_and_waits_for_the_answer_to_settle(self, clock):
        page = _gemini_page(**{SEND: [FakeElement()]})
        answer = FakeElement("Use")
        stop = FakeElement()

        def stream(tick):
            if tick == 1:
                page.elements[RESPONSE] = [answer]
                page.elements[BUSY] = [stop]
            elif tick == 2:
                answer.text = "Use a dict"
            elif tick == 3:
                answer.text = "Use a dict lookup."
            elif tick == 4:
                page.elements[BUSY] = []

        clock.on_tick = stream
        result = browser.ask(FakeBrowser(page), "gemini", "How?", settle=1.0)

        box = page.elements[INPUT][0]
        assert box.filled == "How?" and box.clicks == 1
        assert page.elements[SEND][0].clicks == 1
        assert result.response == "Use a dict lookup."
        assert result.complete is True
        # Text stable since t=1.5, Stop gone at t=2.0: done at 1.5 + settle, and
        # not a poll earlier while the Stop button was still visible.
        assert clock.t == 2.5

    def test_an_older_answer_in_the_thread_is_not_returned(self, clock):
        """Baseline-diff: the previous reply is already on the page before sending."""
        old = FakeElement("previous answer")
        page = _gemini_page(**{RESPONSE: [old]})
        new = FakeElement("fresh answer")

        def reply(tick):
            if tick == 3:
                page.elements[RESPONSE] = [old, new]

        clock.on_tick = reply
        result = browser.ask(FakeBrowser(page), "gemini", "again?", settle=1.0)
        assert result.response == "fresh answer"

    def test_falls_back_to_enter_without_a_usable_send_button(self, clock):
        page = _gemini_page(**{SEND: [FakeElement(enabled=False)],
                               RESPONSE: []})
        clock.on_tick = lambda t: page.elements.__setitem__(RESPONSE, [FakeElement("ok")])
        browser.ask(FakeBrowser(page), "gemini", "hi", settle=0.5)
        assert page.keyboard.pressed == ["Enter"]

    def test_reuses_a_tab_already_on_the_site(self, clock):
        page = _gemini_page()
        clock.on_tick = lambda t: page.elements.__setitem__(RESPONSE, [FakeElement("ok")])
        browser.ask(FakeBrowser(page), "gemini", "hi", settle=0.5)
        assert page.visited == []

    def test_navigates_a_blank_tab_to_the_site(self, clock):
        page = FakePage()

        def load(tick):
            page.elements[INPUT] = [FakeElement()]

        orig_goto = page.goto
        page.goto = lambda url: (orig_goto(url), load(0))
        clock.on_tick = lambda t: page.elements.__setitem__(RESPONSE, [FakeElement("ok")])
        browser.ask(FakeBrowser(page), "gemini", "hi", settle=0.5)
        assert page.visited == ["https://gemini.google.com/app"]

    def test_continue_opens_the_given_conversation(self, clock):
        page = FakePage("https://gemini.google.com/app/other")
        thread = "https://gemini.google.com/app/abc123"
        orig_goto = page.goto
        page.goto = lambda url: (orig_goto(url), page.elements.__setitem__(INPUT, [FakeElement()]))
        clock.on_tick = lambda t: page.elements.__setitem__(RESPONSE, [FakeElement("ok")])
        browser.ask(FakeBrowser(page), "gemini", "follow-up", settle=0.5, url=thread)
        assert page.visited == [thread]

    def test_partial_answer_on_timeout_is_returned_as_incomplete(self, clock):
        page = _gemini_page()
        answer = FakeElement("")

        def never_stops(tick):
            page.elements[RESPONSE] = [answer]
            answer.text += "x"

        clock.on_tick = never_stops
        result = browser.ask(FakeBrowser(page), "gemini", "long?", timeout=5, settle=1.0)
        assert result.complete is False
        assert result.response.startswith("x")

    def test_no_answer_at_all_raises_response_timeout(self, clock):
        with pytest.raises(browser.ResponseTimeout):
            browser.ask(FakeBrowser(_gemini_page()), "gemini", "hello?", timeout=3)

    def test_bookmark_only_sites_cannot_be_asked(self):
        with pytest.raises(browser.UnknownSite, match="bookmark only"):
            browser.check_ask("perplexity", "hi")

    def test_empty_question_is_rejected(self):
        with pytest.raises(browser.BrowserError, match="empty"):
            browser.check_ask("gemini", "  \n")


class TestLoginAndSelectorErrors:
    def test_redirect_to_a_sign_in_host_means_login_required(self, clock):
        page = FakePage("https://accounts.google.com/v3/signin/identifier")
        with pytest.raises(browser.LoginRequired, match="sign-in"):
            browser.find_input(page, GEMINI, timeout=5)
        assert clock.t == 0  # detected immediately, no waiting out the timeout

    def test_login_marker_without_input_means_login_required(self, clock):
        page = FakePage("https://gemini.google.com/app",
                        {GEMINI.login[0]: [FakeElement()]})
        with pytest.raises(browser.LoginRequired):
            browser.find_input(page, GEMINI, timeout=2)

    def test_missing_input_without_login_hints_at_changed_layout(self, clock):
        page = FakePage("https://gemini.google.com/app")
        with pytest.raises(browser.SelectorNotFound) as exc:
            browser.find_input(page, GEMINI, timeout=2)
        assert "CONTROL_AI_SITES" in exc.value.hint

    def test_invisible_inputs_are_skipped_for_the_next_selector(self, clock):
        fallback = FakeElement()
        page = FakePage("https://gemini.google.com/app", {
            GEMINI.input[0]: [FakeElement(visible=False)],
            GEMINI.input[1]: [fallback],
        })
        assert browser.find_input(page, GEMINI, timeout=1) is fallback

    def test_wait_for_login_returns_once_the_composer_appears(self, clock):
        page = FakePage("https://accounts.google.com/signin")

        def user_logs_in(tick):
            if tick == 4:
                page.url = "https://gemini.google.com/app"
                page.elements[INPUT] = [FakeElement()]

        clock.on_tick = user_logs_in
        assert browser.wait_for_login(FakeBrowser(page), "gemini", timeout=60) == \
            "https://gemini.google.com/app"

    def test_wait_for_login_times_out(self, clock):
        page = FakePage("https://accounts.google.com/signin")
        with pytest.raises(browser.LoginRequired, match="not logged in"):
            browser.wait_for_login(FakeBrowser(page), "gemini", timeout=2)


class TestThreads:
    def test_remembers_the_last_conversation_per_site(self, tmp_path):
        browser.remember_thread(tmp_path, "gemini", "https://gemini.google.com/app/1")
        browser.remember_thread(tmp_path, "gpt", "https://chatgpt.com/c/2")
        assert browser.last_thread(tmp_path, "gemini") == "https://gemini.google.com/app/1"
        assert browser.last_thread(tmp_path, "chatgpt") == "https://chatgpt.com/c/2"
        assert browser.last_thread(tmp_path, "claude") is None


# --- CLI -----------------------------------------------------------------------------

class _CtxBrowser(FakeBrowser):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def _main(argv, monkeypatch):
    monkeypatch.setattr("sys.argv", ["control", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
        raise SystemExit(0)
    return exc.value.code


class TestAskCommand:
    def _patch_browser(self, monkeypatch, tmp_path, result):
        fake = _CtxBrowser(FakePage(), profile=tmp_path)
        monkeypatch.setattr(cli, "Browser", lambda headless=False: fake)
        calls = []

        def fake_ask(b, site, question, timeout, settle, url):
            calls.append({"site": site, "question": question, "url": url})
            return result

        monkeypatch.setattr(browser, "ask", fake_ask)
        return calls

    def _result(self, complete=True):
        return browser.AskResult("gemini", "https://gemini.google.com/app/t1",
                                 "42 is the answer", complete, 3.2, 8)

    def test_json_output(self, monkeypatch, tmp_path, capsys):
        calls = self._patch_browser(monkeypatch, tmp_path, self._result())
        assert _main(["--json", "browser", "ask", "gemini", "question"], monkeypatch) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["response"] == "42 is the answer"
        assert data["complete"] is True and data["continued"] is False
        assert data["profile"] == str(tmp_path)
        assert calls == [{"site": "gemini", "question": "question", "url": None}]

    def test_text_output_is_just_the_answer(self, monkeypatch, tmp_path, capsys):
        self._patch_browser(monkeypatch, tmp_path, self._result())
        _main(["browser", "ask", "gemini", "q"], monkeypatch)
        assert capsys.readouterr().out == "42 is the answer\n"

    def test_incomplete_answer_warns_on_stderr(self, monkeypatch, tmp_path, capsys):
        self._patch_browser(monkeypatch, tmp_path, self._result(complete=False))
        _main(["browser", "ask", "gemini", "q"], monkeypatch)
        out = capsys.readouterr()
        assert out.out == "42 is the answer\n" and "cut off" in out.err

    def test_question_from_stdin(self, monkeypatch, tmp_path):
        calls = self._patch_browser(monkeypatch, tmp_path, self._result())
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO("def f(): ...\nwhy slow?"))
        _main(["browser", "ask", "gemini", "-"], monkeypatch)
        assert calls[0]["question"] == "def f(): ...\nwhy slow?"

    def test_continue_reuses_the_remembered_thread(self, monkeypatch, tmp_path, capsys):
        calls = self._patch_browser(monkeypatch, tmp_path, self._result())
        _main(["--json", "browser", "ask", "gemini", "first"], monkeypatch)
        _main(["--json", "browser", "ask", "gemini", "second", "--continue"], monkeypatch)
        assert calls[1]["url"] == "https://gemini.google.com/app/t1"
        assert json.loads(capsys.readouterr().out.splitlines()[-1])["continued"] is True

    def test_invalid_site_fails_before_chromium_starts(self, monkeypatch, capsys):
        def must_not_launch(**kwargs):
            raise AssertionError("Chromium was launched for an invalid request")

        monkeypatch.setattr(cli, "Browser", must_not_launch)
        assert _main(["--json", "browser", "ask", "perplexity", "q"], monkeypatch) == 1
        assert json.loads(capsys.readouterr().out)["type"] == "unknown_site"

    def test_login_required_is_structured(self, monkeypatch, tmp_path, capsys):
        fake = _CtxBrowser(FakePage(), profile=tmp_path)
        monkeypatch.setattr(cli, "Browser", lambda headless=False: fake)

        def needs_login(*a, **k):
            raise browser.LoginRequired("gemini needs you to log in first")

        monkeypatch.setattr(browser, "ask", needs_login)
        assert _main(["--json", "browser", "ask", "gemini", "q"], monkeypatch) == 1
        err = json.loads(capsys.readouterr().out)
        assert err["type"] == "login_required"
        assert "control browser login" in err["hint"]

    def test_login_command_is_always_headed(self, monkeypatch, tmp_path, capsys):
        seen = {}

        def make(headless=False):
            seen["headless"] = headless
            return _CtxBrowser(FakePage(), profile=tmp_path)

        monkeypatch.setattr(cli, "Browser", make)
        monkeypatch.setattr(browser, "wait_for_login", lambda b, s, timeout: "https://claude.ai/new")
        _main(["--json", "browser", "--headless", "login", "claude"], monkeypatch)
        assert seen["headless"] is False
        assert json.loads(capsys.readouterr().out)["logged_in"] is True


class TestAttachCommand:
    def test_json_output(self, monkeypatch, tmp_path, capsys):
        fake = _CtxBrowser(FakePage("https://example.com/"), profile=None)
        fake.cdp_port = 9222
        seen = {}

        def make(port=browser.DEFAULT_CDP_PORT, headless=False):
            seen["port"] = port
            return fake

        monkeypatch.setattr(cli.Browser, "attach", staticmethod(make))
        assert _main(["--json", "browser", "attach", "--port", "9222"], monkeypatch) == 0
        data = json.loads(capsys.readouterr().out)
        assert seen["port"] == 9222
        assert data["port"] == 9222
        assert data["tabs"] == [{"url": "https://example.com/", "title": ""}]

    def test_unreachable_port_reports_a_clear_error(self, monkeypatch, capsys):
        def make(port=browser.DEFAULT_CDP_PORT, headless=False):
            raise browser.BrowserError(
                f"cannot reach a Chromium debug port at 127.0.0.1:{port}",
                hint="open the browser with --remote-debugging-port=9222 first",
            )

        monkeypatch.setattr(cli.Browser, "attach", staticmethod(make))
        assert _main(["--json", "browser", "attach"], monkeypatch) == 1
        err = json.loads(capsys.readouterr().out)
        assert "cannot reach" in err["error"]
        assert "--remote-debugging-port" in err["hint"]


class TestFocusAndGotoCommand:
    def test_focuses_and_types_the_resolved_url(self, monkeypatch, capsys):
        class FakeWindow:
            id, title = "0x1", "Brave"

            def to_dict(self):
                return {"id": self.id, "title": self.title}

        win = FakeWindow()
        monkeypatch.setattr(cli, "_find_window", lambda pattern, args: win)
        calls = []
        monkeypatch.setattr(browser, "focus_and_goto",
                            lambda w, u: calls.append((w, u)))
        assert _main(
            ["--json", "browser", "focus-and-goto", "Brave", "gemini"], monkeypatch
        ) == 0
        data = json.loads(capsys.readouterr().out)
        assert calls == [(win, "https://gemini.google.com/app")]
        assert data["url"] == "https://gemini.google.com/app"
        assert data["window"] == {"id": "0x1", "title": "Brave"}


class TestProfileLock:
    def test_profile_in_use_gives_a_clear_error(self, tmp_path):
        class FakeChromium:
            def launch_persistent_context(self, *a, **k):
                raise RuntimeError("Failed to create a ProcessSingleton for your profile")

        class FakePlaywright:
            chromium = FakeChromium()
            stopped = False

            def stop(self):
                FakePlaywright.stopped = True

        class Starter:
            def start(self):
                return FakePlaywright()

        with patch("control.browser.sync_playwright", return_value=Starter()):
            with pytest.raises(browser.BrowserError, match="already open") as exc:
                browser.Browser(profile=tmp_path)
        assert "--session" in exc.value.hint
        assert FakePlaywright.stopped


class TestCdpAttach:
    """`Browser(cdp_port=...)` connects to an already-open Chromium instead of
    launching a fresh one."""

    def _starter(self, cdp_browser):
        class FakeChromium:
            def connect_over_cdp(self, url):
                self.url = url
                return cdp_browser

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()
                self.stopped = False

            def stop(self):
                self.stopped = True

        class Starter:
            def __init__(self):
                self.instance = FakePlaywright()

            def start(self):
                return self.instance

        return Starter()

    def test_attaches_to_the_first_existing_context(self):
        page = FakePage("https://example.com/")

        class FakeCdpContext:
            pages = [page]

        class FakeCdpBrowser:
            contexts = [FakeCdpContext()]

        starter = self._starter(FakeCdpBrowser())
        with patch("control.browser.sync_playwright", return_value=starter):
            b = browser.Browser(cdp_port=9222)
        assert starter.instance.chromium.url == "http://127.0.0.1:9222"
        assert b.cdp_port == 9222
        assert b.profile is None
        assert b.active_tab() is page

    def test_creates_a_context_when_none_exists_yet(self):
        new_context = object()

        class FakeCdpBrowser:
            contexts = []

            def new_context(self):
                return new_context

        starter = self._starter(FakeCdpBrowser())
        with patch("control.browser.sync_playwright", return_value=starter):
            b = browser.Browser(cdp_port=9222)
        assert b.context is new_context

    def test_close_disconnects_without_closing_the_users_tabs(self):
        page = FakePage("https://example.com/")

        class FakeCdpContext:
            pages = [page]

            def close(self):
                raise AssertionError("attached context must not be closed")

        class FakeCdpBrowser:
            contexts = [FakeCdpContext()]

        starter = self._starter(FakeCdpBrowser())
        with patch("control.browser.sync_playwright", return_value=starter):
            b = browser.Browser(cdp_port=9222)
            b.close()
        assert starter.instance.stopped

    def test_unreachable_port_is_reported_without_a_raw_traceback(self):
        class FakeChromium:
            def connect_over_cdp(self, url):
                raise Exception("connect ECONNREFUSED 127.0.0.1:9222")

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()
                self.stopped = False

            def stop(self):
                self.stopped = True

        class Starter:
            def __init__(self):
                self.instance = FakePlaywright()

            def start(self):
                return self.instance

        starter = Starter()
        with patch("control.browser.sync_playwright", return_value=starter):
            with pytest.raises(browser.BrowserError, match="9222") as exc:
                browser.Browser(cdp_port=9222)
        assert "--remote-debugging-port" in exc.value.hint
        assert starter.instance.stopped

    def test_attach_classmethod_uses_the_default_port(self):
        page = FakePage("https://example.com/")

        class FakeCdpContext:
            pages = [page]

        class FakeCdpBrowser:
            contexts = [FakeCdpContext()]

        starter = self._starter(FakeCdpBrowser())
        with patch("control.browser.sync_playwright", return_value=starter):
            b = browser.Browser.attach()
        assert starter.instance.chromium.url == f"http://127.0.0.1:{browser.DEFAULT_CDP_PORT}"
        assert b.cdp_port == browser.DEFAULT_CDP_PORT


class TestFocusAndGoto:
    def test_focuses_the_window_and_types_the_url(self):
        class FakeWindow:
            id, title = "0x1", "Brave"

            def __init__(self):
                self.focused = False

            def focus(self):
                self.focused = True
                return True

        win = FakeWindow()
        pressed = []
        with patch("control.input.key_combo", side_effect=lambda *k: pressed.append(("combo", k))), \
             patch("control.input.type_text", side_effect=lambda t: pressed.append(("type", t))), \
             patch("control.input.press_key", side_effect=lambda k: pressed.append(("key", k))):
            browser.focus_and_goto(win, "https://example.com")
        assert win.focused
        assert pressed == [
            ("combo", ("ctrl", "l")), ("type", "https://example.com"), ("key", "enter"),
        ]

    def test_pastes_the_url_when_wl_copy_is_available(self):
        """ydotool types US keycodes: on a Spanish layout "https://" would come out
        as "httpsÑ--". Pasting is layout-independent."""
        class FakeWindow:
            id, title = "0x1", "Brave"

            def focus(self):
                return True

        pressed = []
        with patch("control.input.clipboard_available", return_value=True), \
             patch("control.input.paste_text", side_effect=lambda t: pressed.append(("paste", t))), \
             patch("control.input.key_combo", side_effect=lambda *k: pressed.append(("combo", k))), \
             patch("control.input.type_text", side_effect=AssertionError("must not type")), \
             patch("control.input.press_key", side_effect=lambda k: pressed.append(("key", k))):
            assert browser.focus_and_goto(FakeWindow(), "https://example.com/a?b=1") == "paste"
        assert pressed == [("combo", ("ctrl", "l")), ("paste", "https://example.com/a?b=1"),
                           ("key", "enter")]

    def test_focus_failure_is_a_clear_browser_error(self):
        class FakeWindow:
            id, title = "0x1", "Brave"

            def focus(self):
                return False

        with pytest.raises(browser.BrowserError, match="did not take focus"):
            browser.focus_and_goto(FakeWindow(), "https://example.com")
