#!/usr/bin/env python3
"""
First Login — One-time script to open browser and let user sign in manually.

Run this once to create a persistent browser session. After signing in,
the session (cookies, tokens) is saved to browser_data/ and reused.

Usage:
    python scripts/first_login.py
"""

import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.manager import BrowserManager
from src.config import Config
from src.log import setup_logging

log = setup_logging("first_login", log_file="first_login.log")


async def main():
    browser = BrowserManager()

    try:
        print("\n" + "=" * 60)
        print("  ChatGPT First Login — Browser Session Setup")
        print("=" * 60)
        print(f"\n  Browser data dir: {Config.BROWSER_DATA_DIR}")
        print(f"  Target: {Config.CHATGPT_URL}")
        print("\n  A Chrome window will open. Please:")
        print("  1. Sign in to ChatGPT with your account")
        print("  2. Complete any CAPTCHA / Cloudflare checks")
        print("  3. Wait until you see the chat interface")
        print("  4. Come back here and press Enter")
        print("\n" + "=" * 60 + "\n")

        # Launch browser
        page = await browser.start()

        # Navigate to ChatGPT
        await browser.navigate(Config.CHATGPT_URL)

        print("  Browser opened. Sign in now...")
        print()

        # Wait for user input
        input("  Press ENTER after you've signed in successfully > ")

        # Verify login
        logged_in = await browser.is_logged_in()

        if logged_in:
            print("\n  ✅ Login verified! Session saved to browser_data/")
            print("  You won't need to sign in again.\n")
            log.info("First login completed successfully")
        else:
            print("\n  ⚠️  Could not verify login. Session may still be saved.")
            print("  Try running test_phase1.py to check.\n")
            log.warning("Login verification uncertain")

    except KeyboardInterrupt:
        print("\n\n  Cancelled by user.")
    finally:
        await browser.close()
        print("  Browser closed.\n")


if __name__ == "__main__":
    asyncio.run(main())
