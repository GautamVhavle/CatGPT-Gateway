"""
Human-like behavior simulation — typing, clicking, delays, mouse movement.

Makes browser automation look organic to anti-bot systems.
"""

from __future__ import annotations

import asyncio
import random

from patchright.async_api import Page

from src.config import Config
from src.log import setup_logging

log = setup_logging("human")


async def random_delay(min_ms: int | None = None, max_ms: int | None = None) -> None:
    """Sleep for a random duration (milliseconds)."""
    lo = min_ms or Config.THINKING_PAUSE_MIN
    hi = max_ms or Config.THINKING_PAUSE_MAX
    ms = random.randint(lo, hi)
    log.debug(f"Random delay: {ms}ms")
    await asyncio.sleep(ms / 1000)


async def human_type(page: Page, selector: str, text: str) -> None:
    """
    Paste text all at once into the input field.

    Uses keyboard.insert_text() which behaves like a clipboard paste —
    fast and reliable, avoids issues with per-character typing on
    contenteditable divs.
    """
    element = page.locator(selector).first
    await element.click()
    # Small pause after focusing (human would take a moment)
    await asyncio.sleep(random.uniform(0.2, 0.5))

    log.debug(f"Pasting {len(text)} chars into {selector}")
    await page.keyboard.insert_text(text)
    log.debug("Paste complete")


async def human_click(page: Page, selector: str) -> None:
    """
    Click an element with human-like behavior:
    1. Hover over element (triggers mouseover)
    2. Brief pause
    3. Click

    Uses .first to handle cases where multiple elements match.
    """
    element = page.locator(selector).first
    await element.hover()
    await asyncio.sleep(random.uniform(0.1, 0.3))
    await element.click()
    log.debug(f"Human-clicked: {selector}")


async def idle_mouse_movement(page: Page) -> None:
    """
    Simulate idle mouse movement — small random movements to look alive.
    Call this periodically while waiting for responses.
    """
    try:
        viewport = page.viewport_size
        if viewport:
            x = random.randint(100, viewport["width"] - 100)
            y = random.randint(100, viewport["height"] - 100)
            await page.mouse.move(x, y, steps=random.randint(5, 15))
            log.debug(f"Idle mouse move to ({x}, {y})")
    except Exception:
        pass  # Non-critical — don't break the flow


async def thinking_pause() -> None:
    """Simulate a 'thinking' pause before the user starts typing."""
    ms = random.randint(Config.THINKING_PAUSE_MIN, Config.THINKING_PAUSE_MAX)
    log.debug(f"Thinking pause: {ms}ms")
    await asyncio.sleep(ms / 1000)
