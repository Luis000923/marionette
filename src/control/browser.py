"""Precise, DOM-level browser control via Playwright (persistent Chromium profile).

When a session is active (CONTROL_SESSION_ID), the profile lives inside that
session's directory instead of the shared one: parallel agents then keep
separate cookies, logins and local storage, and Chromium never refuses to start
because another instance already locked the profile.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from playwright.sync_api import BrowserContext, Page, sync_playwright

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


class Browser:
    """Thin wrapper around a persistent Playwright Chromium context.

    Persistent context keeps cookies/logins across runs, like a normal browser profile.
    """

    def __init__(self, headless: bool = False, profile: Path | str | None = None):
        self._playwright = sync_playwright().start()
        self.profile = Path(profile) if profile is not None else profile_dir()
        self.profile.mkdir(parents=True, exist_ok=True)
        self.context: BrowserContext = self._playwright.chromium.launch_persistent_context(
            str(self.profile), headless=headless, viewport=None,
        )

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
        self.context.close()
        self._playwright.stop()

    def __enter__(self) -> "Browser":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
