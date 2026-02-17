"""
Stealth wrapper — configures playwright-stealth to evade bot detection.

Uses page.evaluate() instead of context.add_init_script() because
add_init_script() breaks Chrome's DNS resolver inside Docker containers.
Any call to add_init_script() — even a trivial console.log — causes
net::ERR_NAME_NOT_RESOLVED on all subsequent navigations.

The workaround: inject the stealth JS via evaluate() on the current page,
and re-inject automatically on every frame navigation via an event listener.
"""

from __future__ import annotations

import os

from patchright.async_api import BrowserContext, Page, Frame
from playwright_stealth import Stealth

from src.log import setup_logging

log = setup_logging("stealth")

# Single Stealth instance; grab the JS payload once
_stealth = Stealth()
_STEALTH_JS: str = _stealth.script_payload

# Track whether we're in Docker (add_init_script is unsafe there)
_IN_DOCKER: bool = os.path.exists("/.dockerenv") or os.environ.get("DISPLAY") == ":99"


async def _inject_stealth_js(page: Page) -> None:
    """Inject stealth JS into the current page via evaluate()."""
    try:
        await page.evaluate(_STEALTH_JS)
    except Exception:
        # Page may have navigated away or closed — non-fatal
        pass


async def apply_stealth(context: BrowserContext) -> None:
    """
    Apply stealth patches to a browser context.

    In Docker: uses page.evaluate() + navigation listener (safe for DNS).
    Outside Docker: uses the standard add_init_script() approach.
    """
    if _IN_DOCKER:
        await _apply_stealth_docker(context)
    else:
        await _stealth.apply_stealth_async(context)
    log.info("Stealth patches applied to browser context")


async def _apply_stealth_docker(context: BrowserContext) -> None:
    """
    Docker-safe stealth: evaluate JS on every page and listen for navigations.

    add_init_script() is broken in Docker (kills DNS), so we:
    1. Inject stealth JS into all existing pages via evaluate()
    2. Hook 'framenavigated' to re-inject after every navigation
    3. Hook 'page' to cover new tabs/popups
    """

    async def on_frame_navigated(frame: Frame) -> None:
        """Re-inject stealth JS when the main frame navigates."""
        if frame == frame.page.main_frame:
            await _inject_stealth_js(frame.page)

    async def on_new_page(page: Page) -> None:
        """Inject stealth into new pages and attach navigation listener."""
        page.on("framenavigated", on_frame_navigated)
        await _inject_stealth_js(page)

    # Inject into all existing pages
    for page in context.pages:
        await _inject_stealth_js(page)
        page.on("framenavigated", on_frame_navigated)

    # Hook new pages (popups, new tabs)
    context.on("page", on_new_page)

    log.debug("Docker-safe stealth: evaluate + navigation listener active")


def get_stealth() -> Stealth:
    """Return the shared Stealth instance (for use with launch helpers)."""
    return _stealth
