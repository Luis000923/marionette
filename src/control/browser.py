"""Precise, DOM-level browser control via Playwright (persistent Chromium profile).

When a session is active (CONTROL_SESSION_ID), the profile lives inside that
session's directory instead of the shared one: parallel agents then keep
separate cookies, logins and local storage, and Chromium never refuses to start
because another instance already locked the profile.

It also knows a few AI chat sites (`AI_SITES`), so an agent can get a second
opinion from another model with one command: `ask()` types a question into the
site's composer, waits for the answer to finish streaming and returns its text.
Logins live in the persistent profile, so they only have to be done once (see
`wait_for_login`).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, sync_playwright

if TYPE_CHECKING:
    from . import windows

#: Shared profile, used when no session is active (unchanged default).
PROFILE_DIR = Path.home() / ".local/share/control/browser-profile"


def profile_dir(session_name: str | None = None) -> Path:
    """Profile directory for `session_name`, or the active session, or the shared one."""
    name = session_name or os.environ.get("CONTROL_SESSION_ID") or ""
    if not name:
        return PROFILE_DIR
    # Imported lazily: browser.py must stay importable without a KDE session.
    from . import session as session_mod

    return session_mod.session_dir(name) / "browser-profile"


# --- errors -------------------------------------------------------------------

class BrowserError(RuntimeError):
    """A browser-automation failure that carries its own next-step hint."""
    hint = ""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        if hint is not None:
            self.hint = hint


class UnknownSite(BrowserError):
    hint = "list the known sites with: control browser sites"


class LoginRequired(BrowserError):
    hint = ("log in once in the persistent profile with: control browser login <site> "
            "(the session is then reused by every later command)")


class SelectorNotFound(BrowserError):
    hint = ("the site's layout probably changed; override its selectors with a JSON "
            "file in CONTROL_AI_SITES (see README), or check the page with: "
            "control browser snapshot")


class ResponseTimeout(BrowserError):
    hint = "raise --timeout, or check the page state with: control browser snapshot"


# --- AI chat catalog -------------------------------------------------------------

@dataclass(frozen=True)
class AISite:
    """How to drive one AI chat web UI.

    Every selector field is an ordered list of CSS selectors: the first one that
    matches a visible element wins, so a layout change on one variant falls back
    to the next instead of failing outright. `aria-label` variants cover English
    and Spanish UIs.
    """
    name: str
    url: str
    hosts: tuple[str, ...]
    input: tuple[str, ...] = ()
    send: tuple[str, ...] = ()
    response: tuple[str, ...] = ()
    #: Visible while the answer is still being generated (usually a Stop button).
    busy: tuple[str, ...] = ()
    #: Elements that only show up for logged-out visitors.
    login: tuple[str, ...] = ()
    #: Hosts that mean "we got redirected to a sign-in page".
    login_hosts: tuple[str, ...] = ()
    description: str = ""

    @property
    def can_ask(self) -> bool:
        return bool(self.input and self.response)

    def to_dict(self) -> dict:
        return {"name": self.name, "url": self.url, "ask": self.can_ask,
                "description": self.description}


AI_SITES: dict[str, AISite] = {
    "chatgpt": AISite(
        name="chatgpt", url="https://chatgpt.com/", hosts=("chatgpt.com", "chat.openai.com"),
        description="OpenAI ChatGPT",
        input=("#prompt-textarea", "div[contenteditable='true'][id='prompt-textarea']",
               "textarea[name='prompt-textarea']", "form textarea"),
        send=("button[data-testid='send-button']", "#composer-submit-button",
              "button[aria-label*='Send']", "button[aria-label*='Enviar']"),
        response=("[data-message-author-role='assistant']",
                  "article [data-message-author-role='assistant']"),
        busy=("button[data-testid='stop-button']", "button[aria-label*='Stop']",
              "button[aria-label*='Detener']"),
        login=("button[data-testid='login-button']", "a[href*='auth/login']"),
        login_hosts=("auth.openai.com", "auth0.openai.com"),
    ),
    "gemini": AISite(
        name="gemini", url="https://gemini.google.com/app", hosts=("gemini.google.com",),
        description="Google Gemini",
        input=("rich-textarea .ql-editor", "div.ql-editor[contenteditable='true']",
               "[contenteditable='true'][role='textbox']"),
        send=("button.send-button", "button[aria-label*='Send']",
              "button[aria-label*='Enviar']"),
        response=("model-response message-content", "model-response .markdown",
                  "message-content"),
        busy=("button[aria-label*='Stop']", "button[aria-label*='Detener']",
              "model-response .loading-indicator"),
        login=("a[href*='accounts.google.com/ServiceLogin']",
               "a[href*='accounts.google.com/signin']"),
        login_hosts=("accounts.google.com",),
    ),
    "claude": AISite(
        name="claude", url="https://claude.ai/new", hosts=("claude.ai",),
        description="Anthropic Claude",
        input=("div[contenteditable='true'].ProseMirror", "[data-testid='chat-input']",
               "fieldset div[contenteditable='true']"),
        send=("button[aria-label='Send message']", "button[aria-label*='Send']",
              "button[aria-label*='Enviar']"),
        response=("div.font-claude-response", "div.font-claude-message",
                  "[data-testid='assistant-message']"),
        busy=("[data-is-streaming='true']", "button[aria-label*='Stop']",
              "button[aria-label*='Detener']"),
        login=("button[data-testid='login-with-google']", "input[type='email']"),
        login_hosts=(),
    ),
    # Bookmarks only: `goto` knows them, `ask` doesn't drive them (yet).
    "perplexity": AISite(name="perplexity", url="https://www.perplexity.ai/",
                         hosts=("www.perplexity.ai", "perplexity.ai"),
                         description="Perplexity (bookmark only)"),
    "copilot": AISite(name="copilot", url="https://copilot.microsoft.com/",
                      hosts=("copilot.microsoft.com",),
                      description="Microsoft Copilot (bookmark only)"),
    "deepseek": AISite(name="deepseek", url="https://chat.deepseek.com/",
                       hosts=("chat.deepseek.com",),
                       description="DeepSeek (bookmark only)"),
    "mistral": AISite(name="mistral", url="https://chat.mistral.ai/chat",
                      hosts=("chat.mistral.ai",),
                      description="Mistral Le Chat (bookmark only)"),
}

#: Alternative spellings accepted wherever a site name is.
ALIASES = {"gpt": "chatgpt", "openai": "chatgpt", "bard": "gemini", "le-chat": "mistral"}

_SELECTOR_FIELDS = ("input", "send", "response", "busy", "login", "login_hosts", "hosts")


def sites() -> dict[str, AISite]:
    """The catalog, with overrides from the JSON file in CONTROL_AI_SITES applied.

    The file maps site names to fields, e.g.
    `{"gemini": {"input": ["div.new-editor"]}, "mylocal": {"url": "http://localhost:3000", ...}}`.
    Override selectors are tried *before* the built-in ones, so a fix can be
    dropped in without code changes when a site redesigns its composer.
    """
    catalog = dict(AI_SITES)
    path = os.environ.get("CONTROL_AI_SITES")
    if not path:
        return catalog
    try:
        overrides = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BrowserError(f"cannot read CONTROL_AI_SITES={path}: {exc}",
                           hint="fix or unset CONTROL_AI_SITES") from exc
    for name, spec in overrides.items():
        base = catalog.get(name)
        if base is None:
            url = spec.get("url", "")
            base = AISite(name=name, url=url, hosts=(urlparse(url).hostname or "",))
        changes = {k: tuple(spec[k]) + getattr(base, k) for k in _SELECTOR_FIELDS if k in spec}
        for key in ("url", "description"):
            if key in spec:
                changes[key] = spec[key]
        catalog[name] = replace(base, **changes)
    return catalog


def get_site(name: str) -> AISite:
    key = ALIASES.get(name.lower(), name.lower())
    catalog = sites()
    if key not in catalog:
        raise UnknownSite(f"unknown AI site {name!r} (known: {', '.join(sorted(catalog))})")
    return catalog[key]


def resolve_url(target: str) -> str:
    """Turn a site alias (`gemini`), a bare host (`example.com`) or a URL into a URL."""
    key = ALIASES.get(target.lower(), target.lower())
    catalog = sites()
    if key in catalog:
        return catalog[key].url
    if "://" in target or target.startswith(("about:", "data:", "file:")):
        return target
    if "." in target or target.startswith("localhost"):
        return f"https://{target}"
    raise UnknownSite(f"{target!r} is neither a URL nor a known site "
                      f"(known: {', '.join(sorted(catalog))})")


#: Default Chrome DevTools Protocol debug port (`--remote-debugging-port=9222`).
DEFAULT_CDP_PORT = 9222


class Browser:
    """Thin wrapper around a Playwright Chromium context.

    Two ways to get one: `Browser(profile=...)` launches its own, persistent
    context (keeps cookies/logins across runs, like a normal browser profile).
    `Browser(cdp_port=...)` / `Browser.attach(port)` instead connects to a
    Chromium the user already has open (started with
    `--remote-debugging-port=PORT`), and drives its first tab -- no separate
    profile, no new window.
    """

    def __init__(self, headless: bool = False, profile: Path | str | None = None,
                cdp_port: int | None = None):
        self._playwright = sync_playwright().start()
        self.cdp_port = cdp_port
        if cdp_port is not None:
            self.profile = None
            try:
                self._browser = self._playwright.chromium.connect_over_cdp(
                    f"http://localhost:{cdp_port}"
                )
            except Exception as exc:
                self._playwright.stop()
                raise BrowserError(
                    f"cannot reach a Chromium debug port at localhost:{cdp_port}: {exc}",
                    hint=f"open the browser with --remote-debugging-port={cdp_port} "
                         "first (see README), or check the port number",
                ) from exc
            self.context: BrowserContext = (
                self._browser.contexts[0] if self._browser.contexts
                else self._browser.new_context()
            )
            return
        self.profile = Path(profile) if profile is not None else profile_dir()
        self.profile.mkdir(parents=True, exist_ok=True)
        try:
            self.context = self._playwright.chromium.launch_persistent_context(
                str(self.profile), headless=headless, viewport=None,
            )
        except Exception as exc:
            self._playwright.stop()
            if any(m in str(exc) for m in ("ProcessSingleton", "SingletonLock", "in use")):
                raise BrowserError(
                    f"browser profile {self.profile} is already open in another Chromium",
                    hint="close the other 'control browser' command, or give each agent "
                         "its own profile with --session NAME",
                ) from exc
            raise

    @classmethod
    def attach(cls, port: int = DEFAULT_CDP_PORT, headless: bool = False) -> "Browser":
        """Connect to a Chromium already running with `--remote-debugging-port=PORT`."""
        return cls(headless=headless, cdp_port=port)

    @property
    def pages(self) -> list[Page]:
        return self.context.pages

    def new_tab(self, url: str = "about:blank") -> Page:
        page = self.context.new_page()
        if url and url != "about:blank":
            page.goto(url)
        return page

    def close_tab(self, page: Page) -> None:
        page.close()

    def active_tab(self) -> Optional[Page]:
        return self.pages[-1] if self.pages else None

    def goto(self, url: str, page: Optional[Page] = None) -> Page:
        target = page or self.active_tab() or self.new_tab()
        target.goto(url)
        return target

    def screenshot(self, page: Optional[Page] = None, path: str | Path | None = None) -> bytes:
        target = page or self.active_tab()
        if target is None:
            raise RuntimeError("no open tab to screenshot")
        return target.screenshot(path=str(path) if path else None, full_page=True)

    def click(self, selector: str, page: Optional[Page] = None) -> None:
        (page or self.active_tab()).click(selector)

    def fill(self, selector: str, text: str, page: Optional[Page] = None) -> None:
        (page or self.active_tab()).fill(selector, text)

    def text_content(self, selector: str, page: Optional[Page] = None) -> str | None:
        return (page or self.active_tab()).text_content(selector)

    def close(self) -> None:
        # Attached mode: the context/tabs belong to the user's own browser, so
        # only disconnect -- closing them would yank their open tabs away.
        if self.cdp_port is None:
            self.context.close()
        self._playwright.stop()

    def __enter__(self) -> "Browser":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --- asking an AI site -------------------------------------------------------------
# Written against the small slice of Playwright's sync API it needs (page.url,
# goto, query_selector(_all), keyboard.press, element fill/click/inner_text/
# is_visible/is_enabled), so tests can drive it with plain fake objects.

#: Indirections so tests can run the polling loops on a fake clock.
_now = time.monotonic
_sleep = time.sleep

POLL_INTERVAL = 0.5
#: The answer counts as finished once its text hasn't changed for this long
#: (and no "busy" indicator is visible).
DEFAULT_SETTLE = 2.0
DEFAULT_ASK_TIMEOUT = 180.0
DEFAULT_INPUT_TIMEOUT = 20.0


@dataclass
class AskResult:
    site: str
    url: str
    response: str
    complete: bool
    elapsed: float
    question_chars: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"site": self.site, "url": self.url, "response": self.response,
                "complete": self.complete, "elapsed": round(self.elapsed, 2),
                "question_chars": self.question_chars}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _visible(page, selectors: tuple[str, ...]):
    """First visible element matching any selector, in order (None if none)."""
    for selector in selectors:
        try:
            for element in page.query_selector_all(selector):
                if element.is_visible():
                    return element
        except Exception:  # noqa: BLE001 - a selector invalid on this page is just a miss
            continue
    return None


def _on_login_page(page, site: AISite) -> bool:
    return _host(page.url) in site.login_hosts or "/login" in urlparse(page.url).path


def _needs_login(page, site: AISite) -> bool:
    return _on_login_page(page, site) or _visible(page, site.login) is not None


def _open_site(browser: "Browser", site: AISite, url: str | None = None):
    """Reuse a tab already on the site, else navigate the active tab there."""
    target = url or site.url
    for page in browser.pages:
        if _host(page.url) in site.hosts and (url is None or page.url == url):
            return page
    page = browser.active_tab() or browser.new_tab()
    page.goto(target)
    return page


def find_input(page, site: AISite, timeout: float = DEFAULT_INPUT_TIMEOUT):
    """Wait for the site's composer. Raises LoginRequired / SelectorNotFound."""
    deadline = _now() + timeout
    while True:
        if _on_login_page(page, site):
            raise LoginRequired(f"{site.name} redirected to a sign-in page ({page.url})")
        element = _visible(page, site.input)
        if element is not None:
            return element
        if _now() >= deadline:
            if _needs_login(page, site):
                raise LoginRequired(f"{site.name} needs you to log in first")
            raise SelectorNotFound(
                f"no input box found on {page.url} (tried: {', '.join(site.input)})"
            )
        _sleep(POLL_INTERVAL)


def _response_counts(page, site: AISite) -> dict[str, int]:
    counts = {}
    for selector in site.response:
        try:
            counts[selector] = len(page.query_selector_all(selector))
        except Exception:  # noqa: BLE001
            counts[selector] = 0
    return counts


def _new_response(page, site: AISite, baseline: dict[str, int]):
    """Last response element, if one appeared since `baseline` was taken."""
    for selector in site.response:
        try:
            found = page.query_selector_all(selector)
        except Exception:  # noqa: BLE001
            continue
        if len(found) > baseline.get(selector, 0):
            return found[-1]
    return None


def _submit(page, site: AISite) -> None:
    button = _visible(page, site.send)
    if button is not None and button.is_enabled():
        button.click()
    else:
        page.keyboard.press("Enter")


def check_ask(site_name: str, question: str) -> AISite:
    """Validate an `ask` before any browser is started (cheap, raises early)."""
    site = get_site(site_name)
    if not site.can_ask:
        raise UnknownSite(f"{site.name} is a bookmark only; `ask` supports: "
                          f"{', '.join(n for n, s in sites().items() if s.can_ask)}")
    if not question.strip():
        raise BrowserError("empty question", hint="pass the question text, or '-' for stdin")
    return site


def ask(browser: "Browser", site_name: str, question: str,
        timeout: float = DEFAULT_ASK_TIMEOUT, settle: float = DEFAULT_SETTLE,
        url: str | None = None) -> AskResult:
    """Ask `question` on an AI chat site and return the model's answer.

    Same baseline-diff idea as waiting for a new window: the assistant messages
    already on the page are counted *before* sending, so an older answer in the
    same conversation is never mistaken for the new one. The answer is complete
    when its text has been stable for `settle` seconds and the site's "busy"
    indicator (the Stop button) is gone. On `timeout` a partial answer is
    returned with `complete=False`; no answer at all raises ResponseTimeout.

    `url` continues an existing conversation instead of starting a new one.
    """
    site = check_ask(site_name, question)
    started = _now()
    page = _open_site(browser, site, url)
    box = find_input(page, site)
    baseline = _response_counts(page, site)
    box.click()
    box.fill(question)
    _submit(page, site)

    deadline = started + timeout
    last_text, stable_since, element = None, None, None
    while True:
        now = _now()
        element = element or _new_response(page, site, baseline)
        if element is not None:
            try:
                text = element.inner_text().strip()
            except Exception:  # noqa: BLE001 - the node was re-rendered; look it up again
                element, text = None, last_text
            if text != last_text:
                last_text, stable_since = text, now
            busy = _visible(page, site.busy) is not None
            if last_text and not busy and now - stable_since >= settle:
                return AskResult(site.name, page.url, last_text, True, now - started,
                                 len(question))
        if now >= deadline:
            if last_text:
                return AskResult(site.name, page.url, last_text, False, now - started,
                                 len(question), ["timed out while the answer was still changing"])
            if _needs_login(page, site):
                raise LoginRequired(f"{site.name} asked for a login instead of answering")
            raise ResponseTimeout(f"{site.name} produced no answer within {timeout:.0f}s")
        _sleep(POLL_INTERVAL)


# --- keyboard fallback, for browsers with no CDP/DOM access (e.g. Firefox) -----

def focus_and_goto(window: "windows.Window", url: str) -> None:
    """Navigate a browser window to `url` by focusing it and typing, not the DOM.

    The only way in when the browser wasn't opened with a debug port: focus the
    window (`windows.py`), press Ctrl+L to reach the address bar, type the URL
    and press Enter (`input.py`). Works on any browser, but is blind -- no
    selectors, no return value beyond "the keys were sent".
    """
    from . import input as inp

    if not window.focus():
        raise BrowserError(
            f"window {window.id} ({window.title!r}) did not take focus",
            hint="KWin focus-stealing prevention may be blocking it; "
                 "try 'control win activate' first",
        )
    with inp.input_lock():
        inp.key_combo("ctrl", "l")
        inp.type_text(url)
        inp.press_key("enter")


def wait_for_login(browser: "Browser", site_name: str, timeout: float = 300.0) -> str:
    """Open the site and wait until the composer shows up, i.e. the user logged in.

    Meant for a headed browser: the human completes the sign-in in the window,
    and the cookies stay in the persistent profile for every later command.
    """
    site = get_site(site_name)
    page = _open_site(browser, site)
    deadline = _now() + timeout
    while True:
        if not _on_login_page(page, site) and not _needs_login(page, site) \
                and _visible(page, site.input) is not None:
            return page.url
        if _now() >= deadline:
            raise LoginRequired(f"not logged in to {site.name} after {timeout:.0f}s",
                                hint="finish the sign-in in the browser window, or raise --timeout")
        _sleep(POLL_INTERVAL)


# --- remembered conversations ---------------------------------------------------------

def _threads_file(profile: Path) -> Path:
    # Kept inside the profile: it's per session exactly like the cookies are.
    return profile / "control-ai-threads.json"


def last_thread(profile: Path, site_name: str) -> str | None:
    try:
        data = json.loads(_threads_file(profile).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data.get(get_site(site_name).name)


def remember_thread(profile: Path, site_name: str, url: str) -> None:
    path = _threads_file(profile)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data[get_site(site_name).name] = url
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
