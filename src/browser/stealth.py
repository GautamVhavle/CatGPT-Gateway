"""
Stealth compatibility hooks.

The SeleniumBase backend launches the browser in CDP stealth mode and then
attaches Playwright with connect_over_cdp(), so the old playwright-stealth init
script path is intentionally a no-op.
"""
from __future__ import annotations

from playwright.async_api import BrowserContext

from src.log import setup_logging

log = setup_logging("browser.stealth")


async def apply_stealth(context: BrowserContext) -> None:
    """Compatibility shim for older BrowserManager call sites."""
    _ = context
    log.debug("No additional stealth scripts applied; SeleniumBase CDP Mode owns stealth")
