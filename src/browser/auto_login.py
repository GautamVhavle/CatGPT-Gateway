"""
Auto-login helper — detects missing login and prompts user to sign in.

Used by both the FastAPI server and the TUI to automatically trigger
first-time login when no existing session is found, instead of crashing.
"""

from __future__ import annotations

import asyncio
import os
import sys

from src.browser.manager import BrowserManager
from src.config import Config
from src.log import setup_logging

log = setup_logging("auto_login")


def can_prompt_for_login() -> bool:
    """
    Return true when this process can safely block for terminal input.

    Docker/supervisor deployments expose the browser through a web GUI, but the API
    process itself is non-interactive. In that mode startup must not wait on
    input(), because nobody can answer it.
    """
    override = os.getenv("AUTO_LOGIN_INTERACTIVE", "auto").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return bool(sys.stdin and sys.stdin.isatty())


async def ensure_logged_in(browser: BrowserManager, has_session: bool = False) -> bool:
    """
    Check if the user is logged in. If not, guide them through login.

    This replaces the need to manually run `scripts/first_login.py`.
    Opens the browser to ChatGPT, waits for the user to sign in,
    and verifies the login before returning.

    Args:
        browser:     The active BrowserManager.
        has_session: True if session cookies were detected (even if login check failed).
                     Used to suppress the first-time setup banner.

    Returns True if logged in (or successfully logged in now).
    Raises RuntimeError if login fails after the user presses Enter.
    """
    if await browser.is_logged_in():
        log.info("Already logged in")
        return True

    if has_session:
        # Session exists but login check failed (e.g. token expired, page not ready)
        log.warning("Session cookies found but login check failed — session may have expired.")
        print("\n" + "=" * 60)
        print("  ⚠️  ChatGPT Session Expired — Re-login Required")
        print("=" * 60)
    else:
        log.info("No session — starting interactive first-time login flow")
        print("\n" + "=" * 60)
        print("  🔐 ChatGPT Login Required — First-Time Setup")
        print("=" * 60)

    provider_name = "Claude" if Config.PROVIDER == "claude" else "ChatGPT"
    target_url = Config.provider_url()
    print(f"\n  Browser data dir: {Config.BROWSER_DATA_DIR}")
    print(f"  Target: {target_url}")
    print("\n  A Chrome window is open. Please:")
    print(f"  1. Sign in to {provider_name} with your account")
    print("  2. Complete any CAPTCHA / verification checks")
    print("  3. Wait until you see the chat interface")
    if can_prompt_for_login():
        print("  4. Come back here and press Enter")
    else:
        print("  4. The API will keep running; check /status after login")
    print("\n" + "=" * 60 + "\n")

    if not can_prompt_for_login():
        log.warning(
            "Login required, but stdin is not interactive. "
            "Leaving browser open for web GUI/VNC login."
        )
        return False

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
