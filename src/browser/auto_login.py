"""
Auto-login helper — detects missing login and prompts user to sign in.

Used by both the FastAPI server and the TUI to automatically trigger
first-time login when no existing session is found, instead of crashing.
"""

from __future__ import annotations

import asyncio

from src.browser.manager import BrowserManager
from src.config import Config
from src.log import setup_logging

log = setup_logging("auto_login")


async def ensure_logged_in(browser: BrowserManager) -> bool:
    """
    Check if the user is logged in. If not, guide them through login.

    This replaces the need to manually run `scripts/first_login.py`.
    Opens the browser to ChatGPT, waits for the user to sign in,
    and verifies the login before returning.

    Returns True if logged in (or successfully logged in now).
    Raises RuntimeError if login fails after the user presses Enter.
    """
    if await browser.is_logged_in():
        log.info("Already logged in")
        return True

    log.info("Not logged in — starting interactive login flow")

    print("\n" + "=" * 60)
    print("  🔐 ChatGPT Login Required — First-Time Setup")
    print("=" * 60)
    print(f"\n  Browser data dir: {Config.BROWSER_DATA_DIR}")
    print(f"  Target: {Config.CHATGPT_URL}")
    print("\n  A Chrome window is open. Please:")
    print("  1. Sign in to ChatGPT with your account")
    print("  2. Complete any CAPTCHA / Cloudflare checks")
    print("  3. Wait until you see the chat interface")
    print("  4. Come back here and press Enter")
    print("\n" + "=" * 60 + "\n")

    # Wait for user to sign in
    await asyncio.get_event_loop().run_in_executor(
        None, lambda: input("  Press ENTER after you've signed in successfully > ")
    )

    # Give the page a moment to settle
    await asyncio.sleep(2)

    # Verify login
    if await browser.is_logged_in():
        print("\n  ✅ Login verified! Session saved.")
        print("  You won't need to sign in again.\n")
        log.info("Interactive login completed successfully")
        return True
    else:
        print("\n  ⚠️  Could not verify login.")
        print("  The session may still be saved — trying to continue...\n")
        log.warning("Login verification uncertain after interactive login")
        # Don't crash — let the caller decide what to do
        return False
