"""
Browser lifecycle manager — launch, persist, close.

Uses a persistent Chrome context so the user only signs in once.
Session data (cookies, localStorage, IndexedDB) survives restarts.
"""

from __future__ import annotations

import os
import random
import signal
import socket
from pathlib import Path
from patchright.async_api import async_playwright, BrowserContext, Page, Playwright

from src.config import Config
from src.browser.stealth import apply_stealth
from src.log import setup_logging
from src.selectors import Selectors

log = setup_logging("browser")


def _resolve_domains_for_chrome() -> str:
    """
    Pre-resolve key domains and return a --host-resolver-rules string
    for Chrome. This works around Chrome's built-in DNS resolver
    failing inside Docker containers.

    Returns empty string if not running in Docker or all resolutions fail.
    """
    # Only needed in Docker (check for /.dockerenv or DISPLAY=:99)
    if not os.path.exists("/.dockerenv") and os.environ.get("DISPLAY") != ":99":
        return ""

    domains = [
        "chatgpt.com",
        "cdn.oaistatic.com",
        "ab.chatgpt.com",
        "auth.openai.com",
        "auth0.openai.com",
        "openai.com",
        "api.openai.com",
        "platform.openai.com",
        "challenges.cloudflare.com",
        "static.cloudflareinsights.com",
        "tcr9i.chat.openai.com",
    ]
    rules = []
    for domain in domains:
        try:
            ip = socket.gethostbyname(domain)
            rules.append(f"MAP {domain} {ip}")
            log.debug(f"DNS pre-resolve: {domain} -> {ip}")
        except Exception as e:
            log.warning(f"DNS pre-resolve failed: {domain} -> {e}")

    if rules:
        result = ", ".join(rules)
        log.info(f"Chrome host-resolver-rules: {len(rules)} domains mapped")
        return result
    return ""


def _cleanup_stale_locks(data_dir: Path) -> None:
    """
    Remove stale lock / journal / WAL files that prevent browser launch.

    After a crash, Chromium leaves behind:
    - SingletonLock/Socket/Cookie — prevents new instance from using data dir.
    - *-journal, *-wal, *-shm — SQLite journal/WAL files that cause
      "database is locked" errors (UKM, Top Sites, History, etc.)

    We also attempt to kill any orphan chrome-for-testing processes.
    """
    import subprocess

    # 1. Kill orphan chrome-for-testing processes FIRST
    try:
        result = subprocess.run(
            ["pkill", "-f", "chrome-for-testing"],
            capture_output=True, timeout=3
        )
        if result.returncode == 0:
            log.info("Killed orphan chrome processes")
            import time
            time.sleep(1)
    except Exception:
        pass  # Non-critical

    # 2. Remove singleton lock files
    lock_files = ["SingletonLock", "SingletonSocket", "SingletonCookie"]
    for name in lock_files:
        path = data_dir / name
        if path.exists():
            try:
                path.unlink()
                log.info(f"Removed stale lock file: {name}")
            except Exception as e:
                log.warning(f"Could not remove {name}: {e}")

    # 3. Remove SQLite journal/WAL/SHM files that cause "database is locked"
    import glob as _glob
    patterns = ["**/*-journal", "**/*-wal", "**/*-shm"]
    removed = 0
    for pattern in patterns:
        for path_str in _glob.glob(str(data_dir / pattern), recursive=True):
            try:
                Path(path_str).unlink()
                removed += 1
            except Exception:
                pass
    if removed:
        log.info(f"Removed {removed} stale SQLite journal/WAL/SHM files")


class BrowserManager:
    """Manages a single persistent Chromium browser context."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def start(self) -> Page:
        """
        Launch a persistent Chrome context with stealth and human-like settings.

        Automatically cleans up stale lock files from previous crashed sessions.
        Returns the active page ready for navigation.
        """
        Config.ensure_dirs()

        # Clean up stale locks from previous sessions
        _cleanup_stale_locks(Config.BROWSER_DATA_DIR)

        log.info("Launching browser...")
        self._playwright = await async_playwright().start()

        # Randomize viewport slightly to avoid fingerprint consistency
        width = Config.VIEWPORT_WIDTH + random.randint(-20, 20)
        height = Config.VIEWPORT_HEIGHT + random.randint(-20, 20)

        # Try real Chrome first, fall back to bundled Chromium
        chrome_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]

        # Docker-specific flags
        if os.path.exists("/.dockerenv") or os.environ.get("DISPLAY") == ":99":
            chrome_args.extend([
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-gpu",
            ])

        # In Docker, Chrome's DNS resolver can fail. Pre-resolve domains
        # and pass them directly via --host-resolver-rules.
        resolver_rules = _resolve_domains_for_chrome()
        if resolver_rules:
            chrome_args.append(f"--host-resolver-rules={resolver_rules}")

        launch_kwargs = dict(
            user_data_dir=str(Config.BROWSER_DATA_DIR),
            headless=Config.HEADLESS,
            slow_mo=Config.SLOW_MO,
            viewport={"width": width, "height": height},
            locale="en-US",
            timezone_id="America/Los_Angeles",
            args=chrome_args,
        )

        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                channel="chrome", **launch_kwargs
            )
            log.info("Launched with real Chrome")
        except Exception:
            log.info("Real Chrome not found, using bundled Chromium")
            self._context = await self._playwright.chromium.launch_persistent_context(
                **launch_kwargs
            )

        # NOTE: Stealth patches are applied AFTER the first navigation.
        # In Docker, applying stealth init scripts before navigation
        # causes Chrome's DNS resolver to fail (ERR_NAME_NOT_RESOLVED).
        # Call apply_stealth_patches() after navigating to the target page.

        # Use existing page or create one
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()

        log.info(f"Browser ready — viewport {width}x{height}")
        return self._page

    async def apply_stealth_patches(self) -> None:
        """
        Apply stealth patches to the browser context.

        Must be called AFTER the first page navigation, not before.
        In Docker containers, applying stealth init scripts before any
        navigation causes Chrome's DNS resolver to fail.
        """
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        await apply_stealth(self._context)

    @property
    def page(self) -> Page:
        """Get the active page. Raises if browser not started."""
        if self._page is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._page

    @property
    def context(self) -> BrowserContext:
        """Get the browser context."""
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._context

    async def navigate(self, url: str) -> None:
        """Navigate to a URL and wait for page load."""
        log.info(f"Navigating to {url}")
        await self.page.goto(url, wait_until="domcontentloaded")
        log.info("Page loaded")

    async def is_logged_in(self) -> bool:
        """
        Check if user is logged in by looking for chat input vs login indicators.

        Returns True if the chat interface is visible, False if login page detected.
        """
        try:
            # Try to find the chat input
            for selector in Selectors.CHAT_INPUT:
                try:
                    el = await self.page.wait_for_selector(selector, timeout=3000)
                    if el:
                        log.info("Login check: LOGGED IN (chat input found)")
                        return True
                except Exception:
                    continue

            # Check for login indicators
            for selector in Selectors.LOGIN_INDICATORS:
                try:
                    el = await self.page.wait_for_selector(selector, timeout=2000)
                    if el:
                        log.warning("Login check: NOT LOGGED IN (login button found)")
                        return False
                except Exception:
                    continue

            log.warning("Login check: UNCERTAIN — no chat input or login button found")
            return False

        except Exception as e:
            log.error(f"Login check error: {e}")
            return False

    async def close(self) -> None:
        """Gracefully close the browser context and playwright instance."""
        log.info("Closing browser...")
        try:
            if self._context:
                await self._context.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            log.error(f"Error closing browser: {e}")
        finally:
            self._context = None
            self._page = None
            self._playwright = None
            log.info("Browser closed")
