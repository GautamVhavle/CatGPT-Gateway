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

log = setup_logging("browser")


def _resolve_domains_for_chrome() -> str:
    """
    Pre-resolve key domains via the OS and return a --host-resolver-rules
    string for Chrome.

    Chrome's built-in DNS client (even with --disable-features=AsyncDns)
    is unreliable — it can return DNS_PROBE_FINISHED_NXDOMAIN for domains
    that the OS resolver handles fine.  By pre-resolving here and passing
    the IPs via --host-resolver-rules, Chrome bypasses its own resolver
    entirely and the problem disappears.

    Returns empty string if all resolutions fail.
    """
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
        # Claude domains
        "claude.ai",
        "api.claude.ai",
        "cdn.claude.ai",
        "anthropic.com",
        "www.anthropic.com",
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

    We also attempt to kill any orphan Chromium processes that are using
    our user-data-dir.
    """
    import subprocess

    # 1. Kill orphan Chromium processes FIRST.
    #    Match multiple patterns: the macOS app name has spaces ("Google Chrome
    #    for Testing"), Linux uses lowercase hyphens ("chrome-for-testing"),
    #    and generic "chromium" for bundled builds.
    kill_patterns = [
        "Google Chrome for Testing",
        "chrome-for-testing",
        "chromium",
    ]
    for pattern in kill_patterns:
        try:
            result = subprocess.run(
                ["pkill", "-9", "-f", pattern],
                capture_output=True, timeout=3
            )
            if result.returncode == 0:
                log.info(f"Killed orphan browser processes matching '{pattern}'")
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

    # 4. Clear ALL network / DNS / cache state that can corrupt Chrome's
    #    resolver and cause DNS_PROBE_FINISHED_NXDOMAIN for every domain.
    #    Chrome's built-in DNS client stores state in the persistent profile
    #    that survives restarts and can poison resolution for ALL sites.
    import shutil

    # 4a. Delete network state files (DNS, QUIC, HTTP/3 connection cache)
    network_files = [
        "Default/Network Persistent State",
        "Default/Network Action Predictor",
        "Default/TransportSecurity",
        "Default/Reporting and NEL",
        "Default/SCT Auditing Pending Reports",
        "Default/ServerCertificate",
        "Default/DIPS",
        "Default/Safe Browsing Cookies",
    ]
    for rel_path in network_files:
        fpath = data_dir / rel_path
        if fpath.exists():
            try:
                fpath.unlink()
                log.info(f"Cleared network state: {rel_path}")
            except Exception:
                pass

    # 4b. Delete cache directories (HTTP cache, compiled JS, GPU shaders).
    #     These can grow large and contain stale connection/DNS info.
    cache_dirs = [
        "Default/Cache",
        "Default/Code Cache",
        "Default/GPUCache",
        "Default/DawnGraphiteCache",
        "Default/DawnWebGPUCache",
        "Default/Service Worker",
        "GrShaderCache",
        "GraphiteDawnCache",
        "ShaderCache",
    ]
    for rel_dir in cache_dirs:
        dpath = data_dir / rel_dir
        if dpath.exists() and dpath.is_dir():
            try:
                shutil.rmtree(dpath, ignore_errors=True)
                log.info(f"Cleared cache directory: {rel_dir}")
            except Exception:
                pass


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
            # Disable Chrome's built-in DNS client entirely.  Even with
            # AsyncDns off, Chrome's stub resolver can return NXDOMAIN for
            # domains the OS resolves fine.  We also pre-resolve domains
            # via --host-resolver-rules (see _resolve_domains_for_chrome).
            "--disable-features=AsyncDns,DnsOverHttps",
            "--dns-prefetch-disable",
        ]

        # Docker-specific flags
        if os.path.exists("/.dockerenv") or os.environ.get("DISPLAY") == ":99":
            chrome_args.extend([
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-gpu",
            ])

        # Pre-resolve domains via the OS and hardcode the IPs for Chrome.
        # This prevents Chrome's built-in DNS client from ever being used.
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

        # NOTE: We intentionally do NOT flush Chrome's DNS cache here.
        # The --host-resolver-rules flag handles DNS resolution for all
        # mapped domains.  Previously, _clear_dns_cache() would navigate
        # to chrome://net-internals and flush the host cache + socket
        # pools — but this destroyed working connection state and caused
        # DNS_PROBE_FINISHED_NXDOMAIN on subsequent navigations.

        log.info(f"Browser ready — viewport {width}x{height}")
        return self._page

    async def _clear_dns_cache(self) -> None:
        """Clear Chrome's in-memory DNS host cache via chrome://net-internals."""
        import asyncio as _asyncio

        if self._page is None:
            return

        try:
            await self._page.goto(
                "chrome://net-internals/#dns",
                wait_until="domcontentloaded",
                timeout=10000,
            )
            await _asyncio.sleep(0.5)

            # The "Clear host cache" button ID in chrome://net-internals/#dns
            cleared = await self._page.evaluate(
                """
                () => {
                    // Try the standard button
                    const btn = document.getElementById('dns-view-clear-cache');
                    if (btn) { btn.click(); return 'clicked-dns-view-clear-cache'; }
                    // Newer Chrome: look for any button that says "Clear"
                    const buttons = Array.from(document.querySelectorAll('button'));
                    for (const b of buttons) {
                        if (b.textContent.toLowerCase().includes('clear')) {
                            b.click();
                            return 'clicked-' + b.textContent.trim();
                        }
                    }
                    return 'no-clear-button-found';
                }
                """
            )
            log.info(f"Chrome DNS cache flush: {cleared}")
            await _asyncio.sleep(0.3)

            # Also try to flush socket pools
            try:
                await self._page.goto(
                    "chrome://net-internals/#sockets",
                    wait_until="domcontentloaded",
                    timeout=5000,
                )
                await _asyncio.sleep(0.3)
                await self._page.evaluate(
                    """
                    () => {
                        const buttons = Array.from(document.querySelectorAll('button'));
                        for (const b of buttons) {
                            if (b.textContent.toLowerCase().includes('flush') ||
                                b.textContent.toLowerCase().includes('close')) {
                                b.click();
                            }
                        }
                    }
                    """
                )
                log.info("Chrome socket pools flushed")
            except Exception:
                pass  # Best-effort

        except Exception as e:
            log.warning(f"Could not clear Chrome DNS cache: {e}")

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

    async def recover_page(self) -> bool:
        """Recover from DNS / page errors by re-navigating to ChatGPT.

        Tries JS navigation first (avoids DNS lookup), then page.goto().
        Returns True if recovery succeeded, False otherwise.
        """
        import asyncio as _asyncio

        if self._page is None:
            return False

        # Strategy 1: JS navigation (doesn't go through Chrome's DNS resolver)
        try:
            log.info("Page recovery via JS navigation...")
            await self._page.evaluate(f"window.location.href = '{Config.CHATGPT_URL}'")
            await self._page.wait_for_load_state("domcontentloaded", timeout=15000)
            await _asyncio.sleep(1)
            error = await self._page.evaluate(
                """
                () => {
                    const body = document.body ? document.body.innerText : '';
                    if (body.includes('DNS_PROBE_FINISHED_NXDOMAIN')) return 'dns';
                    if (body.includes('ERR_NAME_NOT_RESOLVED')) return 'dns';
                    if (body.includes('ERR_CONNECTION_REFUSED')) return 'conn';
                    return null;
                }
                """
            )
            if not error:
                log.info("Page recovery succeeded (JS navigation)")
                return True
            log.warning(f"JS navigation recovery still shows error: {error}")
        except Exception as e:
            log.warning(f"JS navigation recovery failed: {e}")

        # Strategy 2: page.goto() with retries
        for attempt in range(1, 4):
            try:
                log.info(f"Page recovery attempt {attempt}/3 (page.goto)...")
                await self._page.goto(
                    Config.CHATGPT_URL,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await _asyncio.sleep(1)

                error = await self._page.evaluate(
                    """
                    () => {
                        const body = document.body ? document.body.innerText : '';
                        if (body.includes('DNS_PROBE_FINISHED_NXDOMAIN')) return 'dns';
                        if (body.includes('ERR_NAME_NOT_RESOLVED')) return 'dns';
                        if (body.includes('ERR_CONNECTION_REFUSED')) return 'conn';
                        return null;
                    }
                    """
                )
                if error:
                    log.warning(f"Recovery attempt {attempt} still shows error: {error}")
                    await _asyncio.sleep(attempt * 2)
                    continue

                log.info("Page recovery succeeded")
                return True

            except Exception as e:
                log.warning(f"Recovery attempt {attempt} failed: {e}")
                await _asyncio.sleep(attempt * 2)

        log.error("Page recovery failed after all attempts")
        return False

    async def is_logged_in(self) -> bool:
        """
        Check if user is logged in by looking for chat input vs login indicators.

        Returns True if the chat interface is visible, False if login page detected.
        """
        from src.selectors import Selectors
        from src.claude.selectors import ClaudeSelectors

        if Config.PROVIDER == "claude":
            chat_inputs = ClaudeSelectors.CHAT_INPUT
            login_indicators = ClaudeSelectors.LOGIN_INDICATORS
            logged_in_indicators = ClaudeSelectors.LOGGED_IN_INDICATORS
        else:
            chat_inputs = Selectors.CHAT_INPUT
            login_indicators = Selectors.LOGIN_INDICATORS
            logged_in_indicators = []

        try:
            # Try to find the chat input
            for selector in chat_inputs:
                try:
                    el = await self.page.wait_for_selector(selector, timeout=3000)
                    if el:
                        log.info("Login check: LOGGED IN (chat input found)")
                        return True
                except Exception:
                    continue

            # Claude: also check for user-menu-button as a logged-in signal
            for selector in logged_in_indicators:
                try:
                    el = await self.page.wait_for_selector(selector, timeout=2000)
                    if el:
                        log.info("Login check: LOGGED IN (user menu found)")
                        return True
                except Exception:
                    continue

            # Check for login indicators
            for selector in login_indicators:
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
