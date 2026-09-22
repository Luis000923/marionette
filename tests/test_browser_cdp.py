"""Tests for the CDP/DOM layer used against an external browser (Brave/Chrome):
page readiness, tab selection, DOM/AX reading, checked clicks, file upload/drop,
and the CLI commands on top. Fake pages answer page.evaluate by script, on a
fake clock -- no Chromium is ever launched."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from control import browser, cli

from .test_browser_ai import FakeClock, _main


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(browser, "_now", c.now)
    monkeypatch.setattr(browser, "_sleep", c.sleep)
    return c


class ScriptPage:
    """A page whose evaluate() is answered per script constant from browser.py."""

    def __init__(self, url: str = "https://example.com/", handlers: dict | None = None):
        self.url = url
        self.handlers = handlers or {}
        self.evaluated: list[tuple[str, object]] = []
        self.clicked: list[tuple[str, float]] = []
        self.fronted = False

    def evaluate(self, script, arg=None):
        self.evaluated.append((script, arg))
        handler = self.handlers.get(script)
        if handler is None:
            return None
        return handler(arg) if callable(handler) else handler

    def click(self, selector, timeout=None):
        self.clicked.append((selector, timeout))

    def title(self):
        return "Example"

    def bring_to_front(self):
        self.fronted = True

    def is_closed(self):
        return False

    def scripts(self):
        return [s for s, _ in self.evaluated]


def _probes(*states, origin=1.0, resources=0):
    """Successive _LOAD_PROBE answers; a state of None raises (mid-navigation)."""
    answers = iter(states)

    def probe(_arg):
        state = next(answers, states[-1])
        if state is None:
            raise RuntimeError("Execution context was destroyed")
        if isinstance(state, dict):
            return state
        return {"state": state, "url": "https://example.com/", "origin": origin,
                "resources": resources}

    return probe


# --- readiness ---------------------------------------------------------------------

class TestWaitLoaded:
    def test_returns_once_ready_state_is_complete(self, clock):
        page = ScriptPage(handlers={browser._LOAD_PROBE: _probes("loading", "interactive", "complete")})
        result = browser.wait_loaded(page)
        assert result["ready_state"] == "complete" and result["state"] == "complete"
        assert clock.ticks == 2

    def test_a_torn_down_document_is_not_an_error(self, clock):
        page = ScriptPage(handlers={browser._LOAD_PROBE: _probes(None, None, "complete")})
        assert browser.wait_loaded(page)["ready_state"] == "complete"

    def test_times_out_with_the_last_seen_state(self, clock):
        page = ScriptPage(handlers={browser._LOAD_PROBE: _probes("loading")})
        with pytest.raises(browser.PageNotLoaded, match="readyState: loading"):
            browser.wait_loaded(page, timeout=1.0)

    def test_network_idle_waits_for_resources_to_stop(self, clock):
        counts = iter([1, 2, 3, 3, 3, 3, 3, 3, 3, 3])

        def probe(_arg):
            return {"state": "complete", "url": "u", "origin": 1.0, "resources": next(counts, 3)}

        page = ScriptPage(handlers={browser._LOAD_PROBE: probe})
        result = browser.wait_loaded(page, "networkidle", idle=0.5)
        assert result["state"] == "networkidle"
        # 3 changing probes, then ~0.5s (5-6 polls, float rounding) of the same count.
        assert 0.7 - 1e-9 <= clock.t <= 0.8 + 1e-9

    def test_unknown_state_is_rejected(self):
        with pytest.raises(browser.BrowserError, match="unknown load state"):
            browser.wait_loaded(ScriptPage(), "idle")


class TestWaitForNavigation:
    def _browser(self, pages):
        class B:
            pass

        b = B()
        b.pages = pages
        return b

    def test_ignores_the_old_document_and_waits_for_the_new_one(self, clock):
        answers = iter([
            {"state": "complete", "url": "https://github.com/old", "origin": 1.0, "resources": 0},
            {"state": "loading", "url": "https://github.com/new", "origin": 2.0, "resources": 0},
        ])
        last = {"state": "complete", "url": "https://github.com/new", "origin": 2.0, "resources": 0}
        page = ScriptPage(handlers={browser._LOAD_PROBE: lambda a: next(answers, last)})
        found, load = browser.wait_for_navigation(
            self._browser([page]), "https://github.com/new", {id(page): 1.0})
        assert found is page and load["ready_state"] == "complete"

    def test_a_redirect_to_www_counts_as_the_same_site(self, clock):
        page = ScriptPage(handlers={browser._LOAD_PROBE: {
            "state": "complete", "url": "https://www.google.com/", "origin": 5.0, "resources": 0}})
        found, _ = browser.wait_for_navigation(self._browser([page]), "https://google.com", {})
        assert found is page

    def test_other_sites_never_match(self, clock):
        page = ScriptPage(handlers={browser._LOAD_PROBE: {
            "state": "complete", "url": "https://evil-google.com/", "origin": 5.0, "resources": 0}})
        with pytest.raises(browser.PageNotLoaded, match="no tab loaded"):
            browser.wait_for_navigation(self._browser([page]), "https://google.com", {}, timeout=1)

    def test_await_navigation_start_returns_when_the_document_changes(self, clock):
        page = ScriptPage(handlers={browser._LOAD_PROBE: _probes(
            "complete", {"state": "loading", "url": "u", "origin": 9.0, "resources": 0})})
        browser.await_navigation_start(page, before_origin=1.0, grace=5)
        assert clock.ticks == 1


# --- tabs in the user's browser ----------------------------------------------------------

def _attached(pages, targets, monkeypatch):
    """A Browser in CDP mode without Playwright: just its tab logic."""
    b = object.__new__(browser.Browser)
    b.cdp_port = 9222
    b._selected = None
    b.context = type("Ctx", (), {"pages": pages})()
    monkeypatch.setattr(browser, "cdp_targets", lambda port: targets)
    return b


class TestTabs:
    def test_active_tab_is_the_most_recently_focused_target(self, monkeypatch):
        a, c = ScriptPage("https://a.com/"), ScriptPage("https://c.com/")
        b = _attached([a, c], [{"url": "https://a.com/", "title": "A", "type": "page"},
                              {"url": "https://c.com/", "title": "C", "type": "page"}], monkeypatch)
        assert b.active_tab() is a  # not pages[-1]

    def test_falls_back_to_the_newest_tab_without_the_endpoint(self, monkeypatch):
        a, c = ScriptPage("https://a.com/"), ScriptPage("https://c.com/")
        assert _attached([a, c], [], monkeypatch).active_tab() is c

    def test_select_tab_by_title_url_or_number(self, monkeypatch):
        a, c = ScriptPage("https://a.com/"), ScriptPage("https://docs.google.com/x")
        targets = [{"url": "https://a.com/", "title": "Inbox"},
                   {"url": "https://docs.google.com/x", "title": "Slides deck"}]
        b = _attached([a, c], targets, monkeypatch)
        assert b.select_tab("slides") is c
        assert b.active_tab() is c  # sticks for later commands
        assert b.select_tab("A.COM") is a
        assert b.select_tab("1") is c

    def test_unknown_tab_is_a_clear_error(self, monkeypatch):
        b = _attached([ScriptPage()], [], monkeypatch)
        with pytest.raises(browser.TabNotFound, match="no open tab matches"):
            b.select_tab("nothing-like-this")

    def test_tabs_mark_the_active_one_and_use_endpoint_titles(self, monkeypatch):
        a, c = ScriptPage("https://a.com/"), ScriptPage("https://c.com/")
        b = _attached([a, c], [{"url": "https://c.com/", "title": "Front"}], monkeypatch)
        assert b.tabs() == [
            {"index": 0, "url": "https://a.com/", "title": "Example", "active": False},
            {"index": 1, "url": "https://c.com/", "title": "Front", "active": True},
        ]


# --- reading the page ------------------------------------------------------------------

def _ax(node_id, role, name="", parent=None, children=(), ignored=False, **extra):
    node = {"nodeId": node_id, "role": {"value": role}, "name": {"value": name},
            "childIds": list(children), "ignored": ignored, **extra}
    if parent:
        node["parentId"] = parent
    return node


class TestAccessibilityTree:
    NODES = [
        _ax("1", "RootWebArea", "Upload", children=["2"]),
        _ax("2", "generic", parent="1", children=["3", "5", "6"]),
        _ax("3", "button", "Send", parent="2", children=["4", "8"],
            properties=[{"name": "disabled", "value": {"value": True}},
                        {"name": "focused", "value": {"value": False}}]),
        _ax("4", "StaticText", "Send", parent="3"),
        _ax("5", "none", parent="2", ignored=True),
        _ax("6", "paragraph", parent="2", children=["7"]),
        _ax("7", "StaticText", "Drop your\n  .pptx here", parent="6"),
        _ax("8", "InlineTextBox", "Send", parent="3"),
    ]

    def test_flattens_to_what_a_screen_reader_would_announce(self):
        data = browser.flatten_ax(self.NODES)
        assert data["nodes"] == [
            {"depth": 0, "role": "RootWebArea", "name": "Upload"},
            {"depth": 1, "role": "button", "name": "Send", "disabled": "true"},
            {"depth": 1, "role": "paragraph", "name": ""},
            {"depth": 2, "role": "StaticText", "name": "Drop your .pptx here"},
        ]
        assert data["truncated"] is False

    def test_limit_truncates_but_counts_everything(self):
        data = browser.flatten_ax(self.NODES, limit=2)
        assert len(data["nodes"]) == 2 and data["total"] == 4 and data["truncated"]

    def test_reads_the_tree_over_a_cdp_session(self):
        sent = []

        class Session:
            def send(self, method):
                sent.append(method)
                return {"nodes": TestAccessibilityTree.NODES}

            def detach(self):
                sent.append("detach")

        page = ScriptPage()
        page.context = type("Ctx", (), {"new_cdp_session": lambda self, p: Session()})()
        data = browser.accessibility_tree(page)
        assert sent == ["Accessibility.getFullAXTree", "detach"]
        assert data["url"] == page.url and data["nodes"][1]["role"] == "button"

    def test_dom_outline_passes_options_and_reports_a_missing_root(self):
        page = ScriptPage(handlers={browser._DOM_OUTLINE: {"missing_root": True}})
        with pytest.raises(browser.ElementNotFound, match="#dialog"):
            browser.dom_outline(page, root="#dialog", limit=5)
        assert page.evaluated[0][1] == {"root": "#dialog", "limit": 5, "all": False, "max_text": 80}


# --- checked clicks ----------------------------------------------------------------------

def _state(**overrides):
    state = {"selector": "button.send", "count": 1, "exists": True, "tag": "button",
             "text": "Send", "visible": True, "enabled": True, "in_viewport": True,
             "receives_pointer": True, "interactable": True,
             "box": {"x": 0, "y": 0, "width": 10, "height": 10}, "center": {"x": 5, "y": 5}}
    state.update(overrides)
    return state


def _click_page(state):
    return ScriptPage(handlers={browser._CHECK: state, browser._JS_CLICK: True})


class TestClickElement:
    def test_interactable_target_gets_a_native_click_on_exactly_that_element(self):
        page = _click_page(_state())
        result = browser.click_element(page, "button.send")
        assert result["method"] == "native"
        check_arg = page.evaluated[0][1]
        assert check_arg["scroll"] is True and check_arg["mark"]
        assert page.clicked == [(f'[data-control-target="{check_arg["mark"]}"]', 5000.0)]
        assert page.scripts()[-1] == browser._UNMARK

    def test_covered_target_falls_back_to_a_js_click_in_auto_mode(self):
        page = _click_page(_state(receives_pointer=False, interactable=False,
                                  covered_by="div.cookie-banner"))
        result = browser.click_element(page, "button.send")
        assert result["method"] == "js" and "cookie-banner" in result["note"]
        assert page.clicked == []
        assert browser._JS_CLICK in page.scripts()

    def test_native_mode_refuses_a_covered_target(self):
        page = _click_page(_state(receives_pointer=False, interactable=False, covered_by="div.x"))
        with pytest.raises(browser.ElementNotInteractable, match="covered by <div.x>"):
            browser.click_element(page, "button.send", mode="native")
        assert page.scripts()[-1] == browser._UNMARK  # cleanup still happens

    def test_js_mode_skips_the_native_click(self):
        page = _click_page(_state())
        assert browser.click_element(page, "button.send", mode="js")["method"] == "js"
        assert page.clicked == []

    def test_disabled_target_is_refused_in_every_mode(self):
        for mode in browser.CLICK_MODES:
            page = _click_page(_state(enabled=False, interactable=False))
            with pytest.raises(browser.ElementNotInteractable, match="disabled"):
                browser.click_element(page, "button.send", mode=mode)

    def test_missing_target_is_element_not_found(self):
        page = _click_page({"selector": "#nope", "count": 0, "exists": False})
        with pytest.raises(browser.ElementNotFound, match="no element matches '#nope'"):
            browser.click_element(page, "#nope")

    def test_failed_native_click_falls_back_to_js(self):
        page = _click_page(_state())

        def boom(selector, timeout=None):
            raise RuntimeError("Timeout 5000ms exceeded.\nwaiting for element")

        page.click = boom
        result = browser.click_element(page, "button.send")
        assert result["method"] == "js" and "Timeout 5000ms" in result["note"]

    def test_invalid_selector_is_reported(self):
        page = ScriptPage(handlers={browser._CHECK: {"selector": "a[", "invalid": "bad"}})
        with pytest.raises(browser.ElementNotFound, match="invalid CSS selector"):
            browser.check_element(page, "a[")


# --- files -------------------------------------------------------------------------------

class TestFiles:
    def test_drop_sends_the_real_bytes_and_reports_handling(self, tmp_path):
        deck = tmp_path / "deck.pptx"
        deck.write_bytes(b"PK\x03\x04slides")
        page = ScriptPage(handlers={browser._CHECK: _state(),
                                    browser._DROP: {"dragenter": True, "dragover": True, "drop": True}})
        result = browser.drop_files(page, "#dropzone", [deck])
        arg = next(a for s, a in page.evaluated if s == browser._DROP)
        assert arg["selector"].startswith('[data-control-target="')
        [f] = arg["files"]
        assert f["name"] == "deck.pptx" and base64.b64decode(f["data"]) == b"PK\x03\x04slides"
        assert "presentation" in f["type"]
        assert result["handled"] is True and result["bytes"] == 10

    def test_missing_file_is_refused_before_touching_the_page(self, tmp_path):
        page = ScriptPage()
        with pytest.raises(browser.BrowserError, match="no such file"):
            browser.drop_files(page, "#dropzone", [tmp_path / "nope.pptx"])
        assert page.evaluated == []

    def test_upload_to_a_file_input_sets_the_files_directly(self, tmp_path):
        deck = tmp_path / "deck.pptx"
        deck.write_bytes(b"x")
        page = ScriptPage(handlers={browser._CHECK: _state(tag="input", type="file", visible=False)})
        seen = {}
        page.set_input_files = lambda sel, files, timeout=None: seen.update(sel=sel, files=files)
        result = browser.upload_files(page, "input[type=file]", [deck])
        assert result["method"] == "input"
        assert seen == {"sel": "input[type=file]", "files": [str(deck)]}


# --- CLI ---------------------------------------------------------------------------------

class CtxBrowser:
    cdp_port = 9222
    profile = None

    def __init__(self, page):
        self.page = page
        self.selected = None

    def active_tab(self):
        return self.page

    def select_tab(self, pattern):
        self.selected = pattern
        return self.page

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def _attach(monkeypatch, fake):
    monkeypatch.setattr(cli.Browser, "attach", staticmethod(lambda port=9222, headless=False: fake))


class TestCli:
    def test_click_js_flag_and_tab_option(self, monkeypatch, capsys):
        fake = CtxBrowser(_click_page(_state()))
        _attach(monkeypatch, fake)
        assert _main(["--json", "browser", "--cdp-port", "9222", "--tab", "slides",
                      "click", "button.send", "--js"], monkeypatch) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["method"] == "js" and data["cdp_port"] == 9222
        assert fake.selected == "slides"

    def test_click_wait_loaded_reports_the_load(self, monkeypatch, capsys, clock):
        page = _click_page(_state())
        page.handlers[browser._LOAD_PROBE] = _probes("loading", "complete", origin=2.0)
        _attach(monkeypatch, CtxBrowser(page))
        assert _main(["--json", "browser", "--cdp-port", "9222", "click", "a.next",
                      "--wait-loaded"], monkeypatch) == 0
        assert json.loads(capsys.readouterr().out)["load"]["ready_state"] == "complete"

    def test_click_on_missing_element_is_structured(self, monkeypatch, capsys):
        _attach(monkeypatch, CtxBrowser(_click_page({"selector": "#x", "count": 0, "exists": False})))
        assert _main(["--json", "browser", "--cdp-port", "9222", "click", "#x"], monkeypatch) == 1
        err = json.loads(capsys.readouterr().out)
        assert err["type"] == "element_not_found" and "browser dom" in err["hint"]

    def test_dom_text_lists_role_name_selector_and_center(self, monkeypatch, capsys):
        outline = {"url": "https://x/", "title": "X", "total": 1, "truncated": False,
                   "elements": [{"role": "button", "name": "Upload", "tag": "button",
                                 "selector": "#up", "in_viewport": True, "disabled": True,
                                 "box": {"x": 10, "y": 20, "width": 100, "height": 40}}]}
        _attach(monkeypatch, CtxBrowser(ScriptPage(handlers={browser._DOM_OUTLINE: outline})))
        assert _main(["browser", "--cdp-port", "9222", "dom"], monkeypatch) == 0
        out = capsys.readouterr().out
        assert 'button "Upload" (disabled)\t#up\t@60,40' in out

    def test_check_reports_why_not_interactable(self, monkeypatch, capsys):
        state = _state(interactable=False, receives_pointer=False, covered_by="div.modal")
        _attach(monkeypatch, CtxBrowser(ScriptPage(handlers={browser._CHECK: state})))
        assert _main(["browser", "--cdp-port", "9222", "check", "button.send"], monkeypatch) == 0
        assert "NOT interactable: it is covered by <div.modal>" in capsys.readouterr().out

    def test_focus_and_goto_wait_needs_a_cdp_port(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_find_window", lambda *a: pytest.fail("must fail before focusing"))
        assert _main(["--json", "browser", "focus-and-goto", "Brave", "github.com",
                      "--wait-loaded"], monkeypatch) == 1
        err = json.loads(capsys.readouterr().out)
        assert err["type"] == "usage_error" and "--remote-debugging-port" in err["hint"]

    def test_focus_and_goto_waits_for_the_new_document(self, monkeypatch, capsys):
        class Win:
            id, title = "0x1", "Brave"

            def to_dict(self):
                return {"id": self.id}

        page = ScriptPage("https://github.com/")
        fake = CtxBrowser(page)
        fake.pages = [page]
        _attach(monkeypatch, fake)
        monkeypatch.setattr(cli, "_find_window", lambda *a: Win())
        order = []
        monkeypatch.setattr(browser, "document_ids", lambda pages: order.append("snap") or {})
        monkeypatch.setattr(browser, "focus_and_goto", lambda w, u: order.append(("type", u)))

        def nav(b, url, before, state, timeout):
            order.append(("wait", state, timeout))
            return page, {"state": state, "ready_state": "complete", "url": url, "waited": 1.5}

        monkeypatch.setattr(browser, "wait_for_navigation", nav)
        assert _main(["--json", "browser", "--cdp-port", "9222", "focus-and-goto", "Brave",
                      "github.com", "--network-idle", "--load-timeout", "9"], monkeypatch) == 0
        assert order == ["snap", ("type", "https://github.com"), ("wait", "networkidle", 9.0)]
        assert json.loads(capsys.readouterr().out)["load"]["waited"] == 1.5

    def test_tab_brings_the_match_to_front(self, monkeypatch, capsys):
        page = ScriptPage()
        fake = CtxBrowser(page)
        _attach(monkeypatch, fake)
        assert _main(["--json", "browser", "--cdp-port", "9222", "tab", "example"], monkeypatch) == 0
        assert fake.selected == "example" and page.fronted


class TestInputCli:
    def test_drag_parses_points_and_passes_options(self, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(cli.inp, "drag", lambda *a, **k: calls.append((a, k)))
        assert _main(["--json", "drag", "--from", "10,20", "--to", "300,400",
                      "--steps", "5", "--hold", "50"], monkeypatch) == 0
        assert calls == [((10, 20, 300, 400, "left"), {"steps": 5, "hold_ms": 50})]
        data = json.loads(capsys.readouterr().out)
        assert data["start"] == [10, 20] and data["end"] == [300, 400]

    def test_drag_rejects_a_malformed_point(self, monkeypatch, capsys):
        assert _main(["--json", "drag", "--from", "10;20", "--to", "1,1"], monkeypatch) == 2
        assert "expected X,Y" in json.loads(capsys.readouterr().out)["error"]

    def test_clip_set_files_and_paste_into_a_window(self, monkeypatch, capsys, tmp_path):
        class Win:
            id, title = "0x2", "Brave"

            def focus(self):
                return True

            def to_dict(self):
                return {"id": self.id}

        copied, pasted = [], []
        monkeypatch.setattr(cli.inp, "copy_files", lambda files: copied.append(files) or ["/x.pptx"])
        monkeypatch.setattr(cli.inp, "paste", lambda keys: pasted.append(keys))
        monkeypatch.setattr(cli, "_find_window", lambda *a: Win())
        assert _main(["--json", "clip", "set", "--file", "x.pptx", "--window", "Brave"],
                     monkeypatch) == 0
        data = json.loads(capsys.readouterr().out)
        assert copied == [["x.pptx"]] and pasted == [("ctrl", "v")]
        assert data["pasted"] is True and data["mime"] == "text/uri-list"
        assert data["window"] == {"id": "0x2"}

    def test_clip_set_text_never_echoes_it(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.inp, "copy_to_clipboard", lambda text, mime=None: None)
        assert _main(["--json", "clip", "set", "s3cret"], monkeypatch) == 0
        out = capsys.readouterr().out
        assert "s3cret" not in out and json.loads(out)["chars"] == 6

    def test_clip_set_needs_exactly_one_source(self, monkeypatch, capsys):
        assert _main(["--json", "clip", "set"], monkeypatch) == 1
        assert json.loads(capsys.readouterr().out)["type"] == "usage_error"

    def test_clipboard_errors_carry_an_install_hint(self, monkeypatch, capsys):
        def missing(*a, **k):
            raise cli.inp.ClipboardError("wl-paste not found")

        monkeypatch.setattr(cli.inp, "read_clipboard", missing)
        assert _main(["--json", "clip", "get"], monkeypatch) == 1
        err = json.loads(capsys.readouterr().out)
        assert err["type"] == "clipboard_error" and "wl-clipboard" in err["hint"]


def test_readme_documents_every_new_command():
    import re

    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    for sub in ("dom", "check", "tabs", "tab", "wait-loaded", "upload", "drop"):
        assert re.search(rf"control browser[^\n]* {sub}\b", readme), f"browser {sub}"
    for flag in ("--wait-loaded", "--network-idle", "--js", "--tab"):
        assert flag in readme, flag
    for cmd in ("control drag --from", "control clip set", "control clip get"):
        assert cmd in readme, cmd
