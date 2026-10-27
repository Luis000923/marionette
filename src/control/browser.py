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

import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import time
import urllib.request
import uuid
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


class PageNotLoaded(BrowserError):
    hint = "raise --load-timeout, or check what the page shows with: control browser snapshot"


class TabNotFound(BrowserError):
    hint = "list the open tabs with: control browser --cdp-port PORT tabs"


class ElementNotFound(BrowserError):
    hint = "list the page's clickable elements and their selectors with: control browser dom"


class ElementNotInteractable(BrowserError):
    hint = ("see why with: control browser check SELECTOR; a hidden or covered "
            "element can still be clicked with --js")


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
#: Default cap for `browser snapshot` output, in characters (~1.5k tokens).
SNAPSHOT_MAX_CHARS = 6000


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
        self._selected: Optional[Page] = None
        if cdp_port is not None:
            self.profile = None
            try:
                self._browser = self._playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{cdp_port}"
                )
            except Exception as exc:
                self._playwright.stop()
                raise BrowserError(
                    f"cannot reach a Chromium debug port at 127.0.0.1:{cdp_port}: {exc}",
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
        """The tab commands act on: the one picked with `select_tab`; else, when
        attached to the user's browser, the tab they are looking at; else the
        newest one. Playwright's page list is in creation order, which has
        nothing to do with which tab is in front in Brave/Chrome.
        """
        if self._selected is not None and not self._selected.is_closed():
            return self._selected
        if not self.pages:
            return None
        if self.cdp_port is not None:
            page = _match_target(self.pages, cdp_targets(self.cdp_port)[:1])
            if page is not None:
                return page
        return self.pages[-1]

    def tabs(self) -> list[dict]:
        """Every open tab as {index, url, title, active}; `index` works with select_tab."""
        titles = {}
        if self.cdp_port is not None:
            # Titles from the debug endpoint: page.title() would run JS in every
            # tab, and a tab the browser froze in the background may never answer.
            titles = {t.get("url"): t.get("title", "") for t in reversed(cdp_targets(self.cdp_port))}
        active = self.active_tab()
        return [{"index": i, "url": p.url,
                 "title": titles[p.url] if p.url in titles else _safe_title(p),
                 "active": p is active} for i, p in enumerate(self.pages)]

    def select_tab(self, pattern: str) -> Page:
        """Make later commands act on the tab whose URL or title contains `pattern`
        (case-insensitive), or on tab number `pattern` from `tabs()`."""
        tabs = self.tabs()
        if pattern.isdigit() and int(pattern) < len(tabs):
            chosen = tabs[int(pattern)]
        else:
            needle = pattern.lower()
            matches = [t for t in tabs if needle in t["url"].lower() or needle in t["title"].lower()]
            if not matches:
                raise TabNotFound(f"no open tab matches {pattern!r} "
                                  f"({len(tabs)} tab(s) open)")
            # Prefer the tab in front when several match.
            chosen = next((t for t in matches if t["active"]), matches[0])
        self._selected = self.pages[chosen["index"]]
        return self._selected

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


# --- the attached browser's own view of its tabs -------------------------------------

def cdp_targets(port: int, timeout: float = 2.0) -> list[dict]:
    """Page targets from the debug endpoint (`/json/list`), most recently focused first.

    Chromium orders this list by last activation, so its first entry is the tab
    the user is looking at -- something neither Playwright's page list nor any
    DOM property can tell reliably. Plain urllib: no extra dependency, and an
    unreachable endpoint just means "don't know" (empty list).
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=timeout) as resp:
            data = json.load(resp)
    except (OSError, ValueError):
        return []
    return [t for t in data if isinstance(t, dict) and t.get("type") == "page"]


#: Dedicated profile for the debuggable Brave. Chromium 136+ ignores
#: `--remote-debugging-port` on the default profile, so a separate dir is required.
BRAVE_PROFILE_DIR = Path.home() / ".local/share/control/brave-cdp-profile"
_BRAVE_NAMES = ("brave", "brave-browser", "brave-browser-stable", "brave-bin")


def find_brave() -> str | None:
    """Path of the Brave binary, or None."""
    for name in _BRAVE_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


CHROMIUM_PROFILE_DIR = Path.home() / ".local/share/control/chromium-cdp-profile"


def find_chromium() -> str | None:
    """Playwright's bundled Chromium (newest build), else a system Chromium/Chrome."""
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache/ms-playwright")
    builds = sorted(root.glob("chromium-*/chrome-linux*/chrome"), reverse=True)
    if builds:
        return str(builds[0])
    for name in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


# --- Obscura: lightweight headless engine for simple page reads ---------------
# https://github.com/h4ckf0r0day/obscura (Apache-2.0, by h4ckf0r0day). Used as the
# default reader: no window, no Playwright, ~30 MB. See THIRD_PARTY.md.

OBSCURA_DUMPS = ("text", "markdown", "links", "html")
_OBSCURA_REPO = Path(__file__).resolve().parents[2] / "obscura"


def find_obscura() -> str | None:
    """CONTROL_OBSCURA, then PATH, ~/.local/bin, then the repo's own obscura/ build."""
    cands = [os.environ.get("CONTROL_OBSCURA"), shutil.which("obscura"),
             str(Path.home() / ".local/bin/obscura"),
             str(_OBSCURA_REPO / "target/release/obscura")]
    for cand in cands:
        if cand and os.access(cand, os.X_OK) and Path(cand).is_file():
            return cand
    return None


def obscura_fetch(url: str, dump: str = "text", selector: str | None = None,
                  wait: float | None = None, timeout: float = 30.0,
                  allow_private: bool = False, exe: str | None = None) -> dict:
    """Render `url` (JS included) with Obscura and return {url, title, content}."""
    exe = exe or find_obscura()
    if exe is None:
        raise BrowserError("obscura not found",
                           hint="run scripts/install_obscura.sh, or set CONTROL_OBSCURA=/path/to/obscura")
    if dump not in OBSCURA_DUMPS:
        raise BrowserError(f"unknown dump {dump!r}", hint=f"use one of: {', '.join(OBSCURA_DUMPS)}")
    cmd = [exe]
    if allow_private:
        cmd.append("--allow-private-network")
    cmd += ["fetch", resolve_url(url), "--dump", dump, "--timeout", str(int(timeout))]
    if selector:
        cmd += ["--selector", selector]
    if wait is not None:
        cmd += ["--wait", str(wait)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
    except subprocess.TimeoutExpired:
        raise BrowserError(f"obscura timed out after {timeout:g}s", hint="raise --timeout") from None
    if r.returncode != 0:
        raise BrowserError((r.stderr.strip().splitlines() or ["obscura failed"])[-1],
                           hint="private/loopback URLs need --allow-private-network")
    m = re.search(r'Page loaded: (.*) - "(.*)"\s*$', r.stderr, re.M)
    return {"url": m.group(1) if m else url, "title": m.group(2) if m else "",
            "content": r.stdout}


_BROWSER_CLASS = re.compile(r"brave|chrom|firefox|vivaldi|opera|edge|librewolf|zen", re.I)
_PROFILE_DIRS = {"brave": "~/.config/BraveSoftware/Brave-Browser", "chromium": "~/.config/chromium",
                 "google-chrome": "~/.config/google-chrome"}


def detect_browsers(ports: tuple[int, ...] = (DEFAULT_CDP_PORT,)) -> dict:
    """Browsers already open on this desktop and the best way to drive each."""
    from . import windows

    seen: dict[str, dict] = {}
    for w in windows.list_windows():
        if _BROWSER_CLASS.search(w.classname or ""):
            seen.setdefault(w.classname, {"class": w.classname, "windows": 0, "titles": []})
            seen[w.classname]["windows"] += 1
            seen[w.classname]["titles"].append(w.title)
    live = [p for p in ports if cdp_alive(p)]
    if live:
        how = f"attached: control browser --cdp-port {live[0]} dom|click|fill|eval ..."
    elif seen:
        how = ("no debug port: drive it visually now (control click --text / type / key --window), "
               "or control browser relaunch --yes to restart it with CDP on its own profile")
    else:
        how = "no browser open: control browser fetch URL (Obscura) or control browser launch"
    return {"open": list(seen.values()), "cdp_ports": live, "obscura": find_obscura(),
            "recommendation": how}


def relaunch_with_cdp(window_class: str, port: int = DEFAULT_CDP_PORT, wait: float = 20.0) -> dict:
    """Close an already-open Chromium-family browser and start it again on its own
    profile with a loopback debug port. Open tabs only come back if the browser is
    set to restore its session; anything unsaved in a page is lost."""
    from . import windows

    if cdp_alive(port):
        return {"port": port, "started": False}
    ps = subprocess.run(["ps", "-eo", "comm=,args="], capture_output=True, text=True).stdout
    cls = window_class.lower()
    comm = None
    for ln in ps.splitlines():
        parts = ln.split(None, 1)
        if len(parts) == 2 and parts[0].lower() != "ps" and (
                parts[0].lower() in cls or cls.split(".")[0] in parts[0].lower()):
            # main process only: renderer/utility children carry --type=
            if " --type=" not in parts[1]:
                comm = parts
                break
    if not comm:
        raise BrowserError(f"no running process for window class {window_class!r}")
    exe = comm[1].split(" --", 1)[0].strip()
    kind = "brave" if "brave" in exe.lower() else "chromium" if "chromium" in exe.lower() else "google-chrome"
    prof = Path(os.path.expanduser(_PROFILE_DIRS[kind]))
    for w in windows.list_windows(window_class):
        windows.Window(id=w.id).close()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and subprocess.run(
            ["pgrep", "-f", f"^{re.escape(exe)}( |$)"], capture_output=True).returncode == 0:
        time.sleep(0.5)
    else:
        if time.monotonic() >= deadline:
            raise BrowserError(f"{window_class} did not close within {wait:g}s",
                               hint="save your work and close it manually, then run: "
                                    f"control browser launch --browser {kind if kind == 'brave' else 'chromium'} --profile {prof}")
    args = [exe, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={prof}", "--restore-last-session"]
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if cdp_alive(port):
            return {"port": port, "started": True, "profile": str(prof), "binary": exe}
        time.sleep(0.3)
    raise BrowserError(f"{kind} did not open debug port {port} within {wait:g}s")


def cdp_alive(port: int, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def launch_brave(port: int = DEFAULT_CDP_PORT, profile: Path | str | None = None,
                 url: str | None = None, wait: float = 15.0) -> dict:
    """Start Brave with a loopback-only debug port; no-op if the port already answers."""
    return launch_browser("brave", port, profile, url, wait)


def launch_browser(kind: str = "brave", port: int = DEFAULT_CDP_PORT,
                   profile: Path | str | None = None, url: str | None = None,
                   wait: float = 15.0) -> dict:
    """Start Brave or Chromium detached with a loopback-only debug port, so a
    browser (and its tabs) outlives each CLI call. No-op if the port answers."""
    if cdp_alive(port):
        return {"port": port, "started": False}
    if kind == "chromium":
        exe, label, default_prof = find_chromium(), "Chromium", CHROMIUM_PROFILE_DIR
    else:
        exe, label, default_prof = find_brave(), "Brave", BRAVE_PROFILE_DIR
    if exe is None:
        raise BrowserError(f"{label} not found",
                           hint="install it (e.g. yay -S brave-bin) or start any Chromium with "
                                "--remote-debugging-port and use --cdp-port")
    prof = Path(profile) if profile else default_prof
    prof.mkdir(parents=True, exist_ok=True, mode=0o700)
    args = [exe, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check"]
    if url:
        args.append(resolve_url(url))
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if cdp_alive(port):
            return {"port": port, "started": True, "profile": str(prof), "binary": exe}
        time.sleep(0.25)
    raise BrowserError(f"{label} did not open debug port {port} within {wait:g}s",
                       hint="a browser already using this profile blocks it; close it or pass --profile")


def compact_text(text: str, max_chars: int | None) -> tuple[str, bool]:
    """Collapse blank-line runs and trailing spaces; cap length. Returns (text, truncated)."""
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if max_chars and len(text) > max_chars:
        return text[:max_chars].rstrip() + f"\n[... truncated {len(text) - max_chars} chars; --max-chars 0 for all]", True
    return text, False


def _match_target(pages, targets: list[dict]):
    for target in targets:
        for page in pages:
            if page.url == target.get("url"):
                return page
    return None


def _safe_title(page) -> str:
    try:
        return page.title()
    except Exception:  # noqa: BLE001 - a tab mid-navigation has no title yet
        return ""


# --- page readiness ---------------------------------------------------------------
# Everything below is written against page.evaluate (plus a few Playwright calls
# named in each docstring), so it works the same on a launched Chromium and on a
# CDP-attached Brave, and tests can drive it with fake pages on a fake clock.

LOAD_STATES = ("complete", "networkidle")
DEFAULT_LOAD_TIMEOUT = 30.0
#: "networkidle": no new resource finished loading for this long.
DEFAULT_NETWORK_IDLE = 0.5
LOAD_POLL_INTERVAL = 0.1

# timeOrigin identifies the document: it changes on every real navigation, so a
# stale "complete" from the page being left is never mistaken for the new one.
# The resource-timing buffer defaults to 250 entries; once full the count would
# freeze and look idle, so it is raised on every probe (idempotent).
_LOAD_PROBE = """() => {
  try { performance.setResourceTimingBufferSize(100000); } catch (e) {}
  return {state: document.readyState, url: location.href, origin: performance.timeOrigin,
          resources: performance.getEntriesByType('resource').length};
}"""


def _probe(page) -> dict | None:
    try:
        return page.evaluate(_LOAD_PROBE)
    except Exception:  # noqa: BLE001 - the old document is being torn down mid-navigation
        return None


def wait_loaded(page, state: str = "complete", timeout: float = DEFAULT_LOAD_TIMEOUT,
                idle: float = DEFAULT_NETWORK_IDLE) -> dict:
    """Block until the page's `document.readyState` is "complete" -- and, for
    `state="networkidle"`, until no new resource has finished for `idle` seconds
    (catches SPAs that keep fetching after the load event). Raises PageNotLoaded.
    """
    if state not in LOAD_STATES:
        raise BrowserError(f"unknown load state {state!r}", hint=f"use one of: {', '.join(LOAD_STATES)}")
    started = _now()
    deadline = started + timeout
    count, quiet_since, probe = None, None, None
    while True:
        now = _now()
        probe = _probe(page)
        if probe and probe.get("state") == "complete":
            if state == "complete":
                break
            if probe.get("resources") != count:
                count, quiet_since = probe.get("resources"), now
            elif now - quiet_since >= idle:
                break
        else:
            count = quiet_since = None
        if now >= deadline:
            seen = probe.get("state") if probe else "unreachable"
            raise PageNotLoaded(f"{page.url} not {state} after {timeout:g}s (readyState: {seen})")
        _sleep(LOAD_POLL_INTERVAL)
    return {"state": state, "ready_state": "complete", "url": probe.get("url", page.url),
            "waited": round(_now() - started, 2)}


def document_id(page) -> float | None:
    """Which document the tab shows right now (see _LOAD_PROBE); None if unknown."""
    return (_probe(page) or {}).get("origin")


def document_ids(pages) -> dict[int, float | None]:
    return {id(p): document_id(p) for p in pages}


def _same_site(a: str, b: str) -> bool:
    """example.com vs www.example.com vs m.example.com count as the same site:
    typed URLs routinely redirect to a sibling host."""
    a, b = a.removeprefix("www."), b.removeprefix("www.")
    return bool(a) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def wait_for_navigation(browser: "Browser", url: str, before: dict[int, float | None],
                        state: str = "complete", timeout: float = DEFAULT_LOAD_TIMEOUT):
    """After a navigation started outside Playwright (keyboard, a click), find the tab
    that loaded a *new* document on `url`'s host, and wait until it's loaded.

    `before` comes from document_ids() taken before the navigation started.
    Returns (page, wait_loaded() result). Raises PageNotLoaded.
    """
    host = _host(url)
    started = _now()
    deadline = started + timeout
    while True:
        # Probing every tab also keeps Playwright's event loop turning, which is
        # what updates page.url and adds tabs opened since we connected.
        for page in list(browser.pages):
            probe = _probe(page)
            if not probe or not _same_site(_host(probe.get("url", "")), host):
                continue
            if before.get(id(page)) != probe.get("origin"):
                result = wait_loaded(page, state, timeout=max(0.0, deadline - _now()))
                result["waited"] = round(_now() - started, 2)
                return page, result
        if _now() >= deadline:
            raise PageNotLoaded(
                f"no tab loaded {url} within {timeout:g}s",
                hint="the keystrokes may have gone to another window; check with: "
                     "control browser --cdp-port PORT tabs",
            )
        _sleep(LOAD_POLL_INTERVAL)


def await_navigation_start(page, before_origin: float | None, grace: float = 1.0) -> None:
    """Give an action up to `grace` seconds to start a navigation.

    Right after a click, the page being left still says "complete"; waiting for
    readiness straight away would return before the new page even started.
    Returns as soon as a new document shows up or the old one leaves "complete";
    an action that doesn't navigate (an SPA button) just costs the grace period.
    """
    deadline = _now() + grace
    while _now() < deadline:
        probe = _probe(page)
        if probe is None or probe.get("origin") != before_origin or probe.get("state") != "complete":
            return
        _sleep(LOAD_POLL_INTERVAL)


# --- reading the page: interactive elements and the accessibility tree ---------------

DEFAULT_DOM_LIMIT = 300

_DOM_OUTLINE = r"""(opts) => {
  const INTERACTIVE = 'a[href],button,input:not([type=hidden]),select,textarea,summary,' +
    '[role=button],[role=link],[role=tab],[role=menuitem],[role=menuitemcheckbox],' +
    '[role=menuitemradio],[role=checkbox],[role=radio],[role=switch],[role=option],' +
    '[role=combobox],[role=textbox],[role=searchbox],[role=slider],[contenteditable=""],' +
    '[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"])';
  const STRUCTURE = 'h1,h2,h3,h4,h5,h6,[role=heading],[role=dialog],[role=alertdialog],[role=alert]';
  const root = opts.root ? document.querySelector(opts.root) : document.body;
  if (!root) return {missing_root: true};
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, opts.max_text);
  const unique = (sel) => { try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
  const selectorFor = (el) => {
    const tag = el.tagName.toLowerCase();
    if (el.id && unique('#' + CSS.escape(el.id))) return '#' + CSS.escape(el.id);
    for (const attr of ['data-testid', 'data-test', 'data-qa', 'name', 'aria-label',
                        'placeholder', 'title', 'alt', 'href', 'type']) {
      const v = el.getAttribute(attr);
      if (v && v.length < 120) {
        const sel = tag + '[' + attr + '=' + JSON.stringify(v) + ']';
        if (unique(sel)) return sel;
      }
    }
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && node !== document.documentElement;
         node = node.parentElement) {
      if (node !== el && node.id && unique('#' + CSS.escape(node.id))) {
        parts.unshift('#' + CSS.escape(node.id));
        break;
      }
      const t = node.tagName.toLowerCase();
      const parent = node.parentElement;
      const same = parent ? Array.from(parent.children).filter(c => c.tagName === node.tagName) : [];
      parts.unshift(same.length > 1 ? t + ':nth-of-type(' + (same.indexOf(node) + 1) + ')' : t);
    }
    return parts.join(' > ');
  };
  const IMPLICIT = {a: 'link', button: 'button', select: 'combobox', textarea: 'textbox',
                    summary: 'button', h1: 'heading', h2: 'heading', h3: 'heading',
                    h4: 'heading', h5: 'heading', h6: 'heading'};
  const INPUT_ROLES = {checkbox: 'checkbox', radio: 'radio', button: 'button', submit: 'button',
                       reset: 'button', image: 'button', range: 'slider', file: 'file',
                       search: 'searchbox'};
  const roleOf = (el) => {
    const tag = el.tagName.toLowerCase();
    if (el.getAttribute('role')) return el.getAttribute('role');
    if (tag === 'input') return INPUT_ROLES[(el.type || '').toLowerCase()] || 'textbox';
    if (el.isContentEditable) return 'textbox';
    return IMPLICIT[tag] || tag;
  };
  const nameOf = (el) => {
    const label = el.labels && el.labels.length ? el.labels[0].innerText : '';
    const byId = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => document.getElementById(id)).filter(Boolean).map(n => n.innerText).join(' ');
    const ownValue = el.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes(el.type) ? el.value : '';
    return clean(el.getAttribute('aria-label') || byId || label || el.innerText || ownValue ||
                 el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt') || '');
  };
  const shown = (el, r) => {
    if (r.width === 0 || r.height === 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity || '1') > 0;
  };
  const out = [];
  let total = 0;
  for (const el of root.querySelectorAll(INTERACTIVE + ',' + STRUCTURE)) {
    const r = el.getBoundingClientRect();
    const visible = shown(el, r);
    if (!visible && !opts.all) continue;
    total++;
    if (out.length >= opts.limit) continue;
    const item = {role: roleOf(el), name: nameOf(el), tag: el.tagName.toLowerCase(),
                  selector: selectorFor(el),
                  box: {x: Math.round(r.left), y: Math.round(r.top),
                        width: Math.round(r.width), height: Math.round(r.height)},
                  in_viewport: r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth};
    if (!visible) item.hidden = true;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') item.disabled = true;
    if ('checked' in el && ['checkbox', 'radio'].includes(el.type)) item.checked = el.checked;
    // Never echo a password field's contents back to the agent.
    if ('value' in el && el.type !== 'password' && typeof el.value === 'string' && el.value &&
        !['button', 'submit', 'reset'].includes(el.type)) item.value = clean(el.value);
    if (el.tagName === 'A') item.href = el.getAttribute('href');
    out.push(item);
  }
  return {url: location.href, title: document.title, total: total, elements: out,
          truncated: total > out.length,
          viewport: {width: innerWidth, height: innerHeight, scroll_y: Math.round(scrollY)}};
}"""


def dom_outline(page, root: str | None = None, limit: int = DEFAULT_DOM_LIMIT,
                include_hidden: bool = False, max_text: int = 80) -> dict:
    """The page's interactive elements (and headings/dialogs for orientation), each
    with a role, an accessible-ish name, a CSS selector that is unique on the page,
    and its box in viewport coordinates. Meant to be read before clicking, so the
    agent picks a selector instead of guessing coordinates."""
    data = page.evaluate(_DOM_OUTLINE, {"root": root, "limit": limit, "all": include_hidden,
                                        "max_text": max_text})
    if data.get("missing_root"):
        raise ElementNotFound(f"no element matches {root!r} on {page.url}")
    return data


#: Roles that add nesting but no meaning; their children are kept, one level up.
_AX_NOISE = {"generic", "none", "presentation", "group", "Section"}
#: Layout fragments that only repeat their parent's text: always dropped.
_AX_FRAGMENTS = {"InlineTextBox", "LineBreak"}
_AX_PROPS = ("focused", "disabled", "checked", "expanded", "selected", "pressed", "required",
             "invalid", "level", "url")


def _ax_value(field) -> str:
    if isinstance(field, dict):
        value = field.get("value")
        if isinstance(value, bool):
            return "true" if value else "false"
        return "" if value is None else str(value)
    return ""


def flatten_ax(nodes: list[dict], limit: int = DEFAULT_DOM_LIMIT) -> dict:
    """Turn CDP `Accessibility.getFullAXTree` nodes into a compact, indented list.

    Ignored nodes and anonymous wrappers are dropped (their children move up),
    and a text node repeating its parent's name is dropped too: what is left is
    roughly what a screen reader would announce.
    """
    by_id = {n.get("nodeId"): n for n in nodes}
    roots = [n for n in nodes if n.get("parentId") not in by_id]
    out: list[dict] = []
    total = 0
    # Iterative DFS: real pages nest far deeper than Python's recursion limit likes.
    stack = [(n, 0, "") for n in reversed(roots)]
    while stack:
        node, depth, parent_name = stack.pop()
        role = _ax_value(node.get("role"))
        name = " ".join(_ax_value(node.get("name")).split())
        keep = (not node.get("ignored") and role not in _AX_FRAGMENTS
                and (role not in _AX_NOISE or name))
        if role == "StaticText" and (not name or name == parent_name):
            keep = False
        child_depth = depth
        if keep:
            total += 1
            if len(out) < limit:
                item: dict = {"depth": depth, "role": role, "name": name}
                value = _ax_value(node.get("value"))
                if value and role != "StaticText":
                    item["value"] = value
                for prop in node.get("properties", []):
                    if prop.get("name") in _AX_PROPS:
                        pv = _ax_value(prop.get("value"))
                        if pv and pv != "false":
                            item[prop["name"]] = pv
                out.append(item)
            child_depth = depth + 1
            parent_name = name or parent_name
        children = [by_id[c] for c in node.get("childIds", []) if c in by_id]
        stack.extend((c, child_depth, parent_name) for c in reversed(children))
    return {"nodes": out, "total": total, "truncated": total > len(out)}


def accessibility_tree(page, limit: int = DEFAULT_DOM_LIMIT) -> dict:
    """The tab's accessibility tree, straight from CDP (Accessibility.getFullAXTree)."""
    session = page.context.new_cdp_session(page)
    try:
        nodes = session.send("Accessibility.getFullAXTree").get("nodes", [])
    finally:
        try:
            session.detach()
        except Exception:  # noqa: BLE001 - the tab may have gone away meanwhile
            pass
    data = flatten_ax(nodes, limit)
    data["url"] = page.url
    return data


# --- checking and clicking elements ---------------------------------------------------

DEFAULT_ACTION_TIMEOUT = 5.0
CLICK_MODES = ("auto", "native", "js")
_MARK = "data-control-target"

# Picks the first *visible* match (falling back to the first match), reports why
# it could or couldn't take a click, and optionally tags it so the follow-up
# action hits exactly this element even when the selector matches several.
_CHECK = r"""(opts) => {
  let list;
  try { list = document.querySelectorAll(opts.selector); }
  catch (e) { return {selector: opts.selector, invalid: String(e.message || e)}; }
  const out = {selector: opts.selector, count: list.length, exists: list.length > 0};
  if (!list.length) return out;
  const hasBox = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const el = Array.from(list).find(hasBox) || list[0];
  if (opts.scroll) el.scrollIntoView({block: 'center', inline: 'center'});
  if (opts.mark) el.setAttribute(opts.mark_attr, opts.mark);
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  const describe = (n) => n.tagName.toLowerCase() + (n.id ? '#' + n.id : '') +
    (typeof n.className === 'string' && n.className.trim() ? '.' + n.className.trim().split(/\s+/).slice(0, 3).join('.') : '');
  out.tag = el.tagName.toLowerCase();
  if (el.type) out.type = String(el.type).toLowerCase();
  out.text = (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  out.visible = r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' &&
                parseFloat(cs.opacity || '1') > 0;
  out.enabled = !el.disabled && el.getAttribute('aria-disabled') !== 'true' && !el.closest('fieldset[disabled]');
  out.editable = !el.readOnly && (el.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName));
  out.box = {x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height)};
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  out.center = {x: Math.round(cx), y: Math.round(cy)};
  out.in_viewport = cx >= 0 && cy >= 0 && cx < innerWidth && cy < innerHeight;
  out.receives_pointer = false;
  if (out.visible && out.in_viewport && cs.pointerEvents !== 'none') {
    const hit = document.elementFromPoint(cx, cy);
    out.receives_pointer = !!hit && (hit === el || el.contains(hit) || (el.shadowRoot && el.shadowRoot.contains(hit)));
    if (hit && !out.receives_pointer) out.covered_by = describe(hit);
  }
  out.interactable = out.visible && out.enabled && out.receives_pointer;
  return out;
}"""

_UNMARK = """(opts) => document.querySelectorAll('[' + opts.attr + '="' + opts.mark + '"]')
  .forEach(e => e.removeAttribute(opts.attr))"""

# The full sequence a real mouse click produces, then el.click() for the click
# itself (and its default action: following a link, toggling a checkbox,
# submitting a form). Events bubble and are composed, so React/Vue's delegated
# listeners on the root see them like any user click.
_JS_CLICK = r"""(opts) => {
  const el = document.querySelector(opts.selector);
  if (!el) return false;
  el.scrollIntoView({block: 'center', inline: 'center'});
  const r = el.getBoundingClientRect();
  const base = {bubbles: true, cancelable: true, composed: true, view: window,
                clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, button: 0};
  const pointer = Object.assign({pointerId: 1, pointerType: 'mouse', isPrimary: true}, base);
  const Pointer = window.PointerEvent || MouseEvent;
  el.dispatchEvent(new Pointer('pointerover', pointer));
  el.dispatchEvent(new MouseEvent('mouseover', base));
  el.dispatchEvent(new Pointer('pointerdown', Object.assign({buttons: 1}, pointer)));
  el.dispatchEvent(new MouseEvent('mousedown', Object.assign({buttons: 1}, base)));
  if (typeof el.focus === 'function') el.focus({preventScroll: true});
  el.dispatchEvent(new Pointer('pointerup', pointer));
  el.dispatchEvent(new MouseEvent('mouseup', base));
  el.click();
  return true;
}"""


def check_element(page, selector: str, scroll: bool = False, mark: str | None = None) -> dict:
    """Whether `selector` exists and could take a real click right now: visible,
    enabled, inside the viewport, and not covered by another element (an overlay,
    a cookie banner, a spinner). Never raises for a missing element -- check
    `exists`; an invalid selector raises ElementNotFound."""
    state = page.evaluate(_CHECK, {"selector": selector, "scroll": scroll,
                                   "mark": mark, "mark_attr": _MARK})
    if state.get("invalid"):
        raise ElementNotFound(f"invalid CSS selector {selector!r}: {state['invalid']}",
                              hint="check the selector syntax; list valid ones with: control browser dom")
    return state


def _why_not(state: dict) -> str:
    if not state.get("visible"):
        return "it is hidden (zero size, display:none, visibility:hidden or opacity 0)"
    if not state.get("enabled"):
        return "it is disabled"
    if not state.get("in_viewport"):
        return "it is outside the viewport"
    if state.get("covered_by"):
        return f"it is covered by <{state['covered_by']}>"
    return "it does not receive pointer events"


def _require(page, selector: str, scroll: bool = False, mark: str | None = None) -> dict:
    state = check_element(page, selector, scroll=scroll, mark=mark)
    if not state["exists"]:
        raise ElementNotFound(f"no element matches {selector!r} on {page.url}")
    return state


def click_element(page, selector: str, mode: str = "auto",
                  timeout: float = DEFAULT_ACTION_TIMEOUT) -> dict:
    """Click `selector` after checking it can be clicked.

    Modes:
    * "native": Playwright's click -- trusted CDP mouse events at the element's
      center, after its own actionability checks. Fails on hidden/covered elements.
    * "js": dispatch the pointer/mouse sequence and call `el.click()` in the page.
      Reaches hidden or covered elements; events are untrusted (`isTrusted` false),
      which a few sites reject.
    * "auto" (default): native when the element is interactable, otherwise -- or
      if the native click fails -- fall back to the JS click.

    A disabled element is refused in every mode: el.click() on it is a no-op, and
    reporting success for a click that did nothing would mislead the agent.
    """
    if mode not in CLICK_MODES:
        raise BrowserError(f"unknown click mode {mode!r}", hint=f"use one of: {', '.join(CLICK_MODES)}")
    mark = uuid.uuid4().hex[:12]
    target = f'[{_MARK}="{mark}"]'
    state = _require(page, selector, scroll=True, mark=mark)
    result = {"selector": selector, "matches": state["count"], "target": state}
    try:
        if not state["enabled"]:
            raise ElementNotInteractable(f"cannot click {selector!r}: {_why_not(state)}",
                                         hint="wait for the page to enable it, e.g. after filling "
                                              "required fields; inspect with: control browser check")
        if mode == "native" and not state["interactable"]:
            raise ElementNotInteractable(f"cannot click {selector!r} natively: {_why_not(state)}")
        if mode == "js" or not state["interactable"]:
            if mode == "auto":
                result["note"] = f"native click impossible ({_why_not(state)}); used a JS click"
            page.evaluate(_JS_CLICK, {"selector": target})
            result["method"] = "js"
            return result
        try:
            page.click(target, timeout=timeout * 1000)
            result["method"] = "native"
        except Exception as exc:  # noqa: BLE001 - reported in the result, not swallowed
            if mode == "native":
                raise
            page.evaluate(_JS_CLICK, {"selector": target})
            result["method"] = "js"
            result["note"] = f"native click failed ({str(exc).splitlines()[0]}); used a JS click"
        return result
    finally:
        try:
            page.evaluate(_UNMARK, {"attr": _MARK, "mark": mark})
        except Exception:  # noqa: BLE001 - the click navigated away: nothing left to clean
            pass


# --- files: upload through an input/file chooser, or drop onto a drop zone ----------------

#: Dropped files travel through page.evaluate as base64; keep that sane.
MAX_DROP_BYTES = 100 * 1024 * 1024


def _local_files(files) -> list[Path]:
    paths = [Path(f).expanduser().resolve() for f in files]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise BrowserError(f"no such file: {', '.join(missing)}", hint="check the file path(s)")
    return paths


def upload_files(page, selector: str, files, timeout: float = DEFAULT_ACTION_TIMEOUT) -> dict:
    """Hand files to a page without its native file dialog ever opening.

    `selector` may be the `<input type=file>` itself (hidden ones included), or
    the button that opens the chooser: then the chooser is intercepted over CDP
    (page.expect_file_chooser) and answered with the files.
    """
    paths = _local_files(files)
    state = _require(page, selector)
    if state.get("tag") == "input" and state.get("type") == "file":
        page.set_input_files(selector, [str(p) for p in paths], timeout=timeout * 1000)
        method = "input"
    else:
        with page.expect_file_chooser(timeout=timeout * 1000) as chooser:
            click_element(page, selector, timeout=timeout)
        chooser.value.set_files([str(p) for p in paths])
        method = "file_chooser"
    return {"selector": selector, "method": method, "files": [str(p) for p in paths]}


_DROP = r"""(opts) => {
  const el = document.querySelector(opts.selector);
  if (!el) return null;
  const dt = new DataTransfer();
  for (const f of opts.files) {
    const bytes = Uint8Array.from(atob(f.data), c => c.charCodeAt(0));
    dt.items.add(new File([bytes], f.name, {type: f.type}));
  }
  el.scrollIntoView({block: 'center', inline: 'center'});
  const r = el.getBoundingClientRect();
  const init = {bubbles: true, cancelable: true, composed: true, dataTransfer: dt,
                clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};
  const events = {};
  for (const type of ['dragenter', 'dragover', 'drop']) {
    const ev = new DragEvent(type, init);
    el.dispatchEvent(ev);
    events[type] = ev.defaultPrevented;
  }
  return events;
}"""


def drop_files(page, selector: str, files) -> dict:
    """Drop local files onto a drag-and-drop zone, entirely inside the page.

    Builds a DataTransfer with the files' real bytes and dispatches
    dragenter/dragover/drop on the element -- no mouse, no file manager, no
    screen coordinates. `handled` is true when the page called preventDefault()
    on the drop, which is what drop zones do when they take the files.
    """
    paths = _local_files(files)
    size = sum(p.stat().st_size for p in paths)
    if size > MAX_DROP_BYTES:
        raise BrowserError(f"{size} bytes is too much to drop through the page",
                           hint="use 'control browser upload' with the page's file input instead")
    mark = uuid.uuid4().hex[:12]
    _require(page, selector, mark=mark)
    payload = [{"name": p.name, "type": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                "data": base64.b64encode(p.read_bytes()).decode()} for p in paths]
    try:
        events = page.evaluate(_DROP, {"selector": f'[{_MARK}="{mark}"]', "files": payload})
    finally:
        try:
            page.evaluate(_UNMARK, {"attr": _MARK, "mark": mark})
        except Exception:  # noqa: BLE001
            pass
    return {"selector": selector, "files": [str(p) for p in paths], "bytes": size,
            "handled": bool(events and events.get("drop")), "events": events}


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

def focus_and_goto(window: "windows.Window", url: str) -> str:
    """Navigate a browser window to `url` by focusing it and typing, not the DOM.

    The only way in when the browser wasn't opened with a debug port: focus the
    window (`windows.py`), press Ctrl+L to reach the address bar, enter the URL
    and press Enter (`input.py`). Works on any browser, but is blind -- no
    selectors, no return value beyond how the URL was entered.

    The URL is pasted from the Wayland clipboard when wl-copy is available:
    `ydotool type` sends US-layout keycodes, so on any other layout ':' and '/'
    come out as other characters (on a Spanish one, "https://" turns into
    "httpsÑ--" and the browser searches for it). Pasting replaces the user's
    clipboard contents. Returns "paste" or "type".
    """
    from . import input as inp

    if not window.focus():
        raise BrowserError(
            f"window {window.id} ({window.title!r}) did not take focus",
            hint="KWin focus-stealing prevention may be blocking it; "
                 "try 'control win activate' first",
        )
    # URLs always hold ':' and '/', so this pastes whenever wl-copy exists.
    method, _warning = inp.choose_text_method(url)
    with inp.input_lock():
        inp.key_combo("ctrl", "l")
        if method == "paste":
            inp.paste_text(url)
        else:
            inp.type_text(url)
        inp.press_key("enter")
    return method


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
