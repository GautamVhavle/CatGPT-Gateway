"""
Browser lifecycle manager -- launch, persist, close.

This version uses SeleniumBase Stealthy Playwright Mode:
SeleniumBase launches a stealthy Chrome / Chromium session through CDP, then
Playwright attaches to that session with connect_over_cdp(). The rest of the
application keeps using the standard Playwright async Page API.
"""
from __future__ import annotations

import inspect
import os
import random
import socket
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from seleniumbase import cdp_driver

from src.config import Config
from src.log import setup_logging

log = setup_logging("browser")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_domains_for_chrome() -> str:
    """
    Pre-resolve key domains through the OS resolver and return a Chrome
    --host-resolver-rules value.

    Chrome's built-in DNS resolver can be unreliable in some Docker / Xvfb
    setups. Mapping known provider domains through the OS resolver keeps the
    existing CatGPT DNS workaround while changing the automation backend.
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
    rules: list[str] = []
    for domain in domains:
        try:
            ip = socket.gethostbyname(domain)
            rules.append(f"MAP {domain} {ip}")
            log.debug(f"DNS pre-resolve: {domain} -> {ip}")
        except Exception as exc:  # pragma: no cover - best effort hardening
            log.warning(f"DNS pre-resolve failed: {domain} -> {exc}")
    if rules:
        log.info(f"Chrome host-resolver-rules: {len(rules)} domains mapped")
        return ", ".join(rules)
    return ""


def _cleanup_stale_locks(data_dir: Path) -> None:
    """Remove stale Chromium locks and cache files from crashed sessions."""
    import glob
    import shutil
    import subprocess
    import time

    # 1. Kill orphan browser processes that may still be using the profile.
    kill_patterns = [
        "Google Chrome for Testing",
        "chrome-for-testing",
        "chromium",
        "chrome",
    ]
    for pattern in kill_patterns:
        try:
            result = subprocess.run(
                ["pkill", "-9", "-f", pattern],
                capture_output=True,
                timeout=3,
            )
            if result.returncode == 0:
                log.info(f"Killed orphan browser processes matching '{pattern}'")
                time.sleep(1)
        except Exception:
            pass

    # 2. Remove singleton lock files.
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = data_dir / name
        if path.exists():
            try:
                path.unlink()
                log.info(f"Removed stale lock file: {name}")
            except Exception as exc:
                log.warning(f"Could not remove {name}: {exc}")

    # 3. Remove SQLite journal/WAL/SHM files that cause locked profile errors.
    removed = 0
    for pattern in ("**/*-journal", "**/*-wal", "**/*-shm"):
        for path_str in glob.glob(str(data_dir / pattern), recursive=True):
            try:
                Path(path_str).unlink()
                removed += 1
            except Exception:
                pass
    if removed:
        log.info(f"Removed {removed} stale SQLite journal/WAL/SHM files")

    # 4. Clear network/cache state that can poison Chrome DNS resolution.
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


def _get_endpoint_url(driver: Any) -> str:
    """Return the remote debugging endpoint URL from a SeleniumBase driver."""
    for method_name in ("get_endpoint_url", "get_rd_url"):
        method = getattr(driver, method_name, None)
        if callable(method):
            endpoint = method()
            if endpoint:
                return str(endpoint)
    host_method = getattr(driver, "get_rd_host", None)
    port_method = getattr(driver, "get_rd_port", None)
    if callable(host_method) and callable(port_method):
        return f"http://{host_method()}:{port_method()}"
    raise RuntimeError("SeleniumBase did not expose a remote debugging endpoint")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class BrowserManager:
    """Manages a single SeleniumBase-backed Playwright browser session."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._seleniumbase_driver: Any | None = None

    async def start(self) -> Page:
        """
        Launch SeleniumBase CDP Mode and attach Playwright over CDP.

        SeleniumBase owns the browser process/profile. Playwright connects to
        the remote-debugging endpoint and exposes a normal async Page object to
        the existing ChatGPT / Claude clients.
        """
        Config.ensure_dirs()
        _cleanup_stale_locks(Config.BROWSER_DATA_DIR)

        width = Config.VIEWPORT_WIDTH + random.randint(-20, 20)
        height = Config.VIEWPORT_HEIGHT + random.randint(-20, 20)

        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=AsyncDns,DnsOverHttps",
            "--dns-prefetch-disable",
            "--lang=en-US",
            f"--window-size={width},{height}",
        ]
        if os.path.exists("/.dockerenv") or os.environ.get("DISPLAY") == ":99":
            browser_args.extend(
                [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                ]
            )

        resolver_rules = _resolve_domains_for_chrome()
        if resolver_rules:
            browser_args.append(f"--host-resolver-rules={resolver_rules}")

        use_chromium = _env_bool("SELENIUMBASE_USE_CHROMIUM", default=True)
        log.info(
            "Launching SeleniumBase Stealthy Playwright browser "
            f"(profile={Config.BROWSER_DATA_DIR}, chromium={use_chromium})"
        )

        self._seleniumbase_driver = await cdp_driver.start_async(
            user_data_dir=str(Config.BROWSER_DATA_DIR),
            headless=Config.HEADLESS,
            use_chromium=use_chromium,
            browser_args=browser_args,
        )

        # Ensure the SeleniumBase CDP browser has at least one tab before
        # Playwright attaches to it.
        try:
            await self._seleniumbase_driver.get("about:blank", lang="en")
        except Exception as exc:
            log.debug(f"SeleniumBase initial tab setup skipped: {exc}")

        endpoint_url = _get_endpoint_url(self._seleniumbase_driver)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(
            endpoint_url,
            slow_mo=Config.SLOW_MO,
        )

        if self._browser.contexts:
            self._context = self._browser.contexts[0]
        else:
            self._context = await self._browser.new_context(
                viewport={"width": width, "height": height},
                locale="en-US",
                timezone_id="America/Los_Angeles",
            )

        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()

        try:
            await self._page.set_viewport_size({"width": width, "height": height})
        except Exception as exc:
            log.debug(f"Could not resize Playwright page: {exc}")

        log.info(f"Browser ready via SeleniumBase CDP + Playwright -- viewport {width}x{height}")
        return self._page

    async def _clear_dns_cache(self) -> None:
        """No-op placeholder kept for compatibility with older call sites."""
        log.debug("DNS cache clear skipped; browser uses host-resolver-rules")

    async def apply_stealth_patches(self) -> None:
        """
        Compatibility hook for the old Patchright + playwright-stealth path.

        SeleniumBase Stealthy Playwright Mode applies stealth at launch by
        starting a SeleniumBase CDP browser and attaching Playwright to it. No
        extra init scripts are required here.
        """
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        log.debug("Stealth patches already supplied by SeleniumBase CDP Mode")

    @property
    def page(self) -> Page:
        """Get the active Playwright page. Raises if browser not started."""
        if self._page is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._page

    @property
    def context(self) -> BrowserContext:
        """Get the active Playwright browser context."""
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._context

    async def navigate(self, url: str) -> None:
        """Navigate to a URL and wait for page load."""
        log.info(f"Navigating to {url}")
        await self.page.goto(url, wait_until="domcontentloaded")
        log.info("Page loaded")

    async def recover_page(self) -> bool:
        """
        Recover from DNS / page errors by re-navigating to the active provider.
        """
        import asyncio as _asyncio

        if self._page is None:
            return False

        target_url = Config.CLAUDE_URL if Config.PROVIDER == "claude" else Config.CHATGPT_URL

        try:
            log.info("Page recovery via JS navigation...")
            await self._page.evaluate(f"window.location.href = '{target_url}'")
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
        except Exception as exc:
            log.warning(f"JS navigation recovery failed: {exc}")

        for attempt in range(1, 4):
            try:
                log.info(f"Page recovery attempt {attempt}/3 (page.goto)...")
                await self._page.goto(
                    target_url,
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
            except Exception as exc:
                log.warning(f"Recovery attempt {attempt} failed: {exc}")
                await _asyncio.sleep(attempt * 2)

        log.error("Page recovery failed after all attempts")
        return False

    async def is_logged_in(self) -> bool:
        """
        Check if user is logged in by looking for provider UI selectors.
        """
        from src.claude.selectors import ClaudeSelectors
        from src.selectors import Selectors

        if Config.PROVIDER == "claude":
            chat_inputs = ClaudeSelectors.CHAT_INPUT
            login_indicators = ClaudeSelectors.LOGIN_INDICATORS
            logged_in_indicators = ClaudeSelectors.LOGGED_IN_INDICATORS
        else:
            chat_inputs = Selectors.CHAT_INPUT
            login_indicators = Selectors.LOGIN_INDICATORS
            logged_in_indicators = []

        try:
            for selector in chat_inputs:
                try:
                    element = await self.page.wait_for_selector(selector, timeout=3000)
                    if element:
                        log.info("Login check: LOGGED IN (chat input found)")
                        return True
                except Exception:
                    continue

            for selector in logged_in_indicators:
                try:
                    element = await self.page.wait_for_selector(selector, timeout=2000)
                    if element:
                        log.info("Login check: LOGGED IN (user menu found)")
                        return True
                except Exception:
                    continue

            for selector in login_indicators:
                try:
                    element = await self.page.wait_for_selector(selector, timeout=2000)
                    if element:
                        log.warning("Login check: NOT LOGGED IN (login button found)")
                        return False
                except Exception:
                    continue

            log.warning("Login check: UNCERTAIN -- no chat input or login button found")
            return False
        except Exception as exc:
            log.error(f"Login check error: {exc}")
            return False

    async def close(self) -> None:
        """Gracefully close Playwright and the SeleniumBase-owned browser."""
        log.info("Closing browser...")
        try:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
            if self._seleniumbase_driver:
                for method_name in ("quit", "stop", "close"):
                    method = getattr(self._seleniumbase_driver, method_name, None)
                    if callable(method):
                        try:
                            await _maybe_await(method())
                            break
                        except Exception as exc:
                            log.debug(f"SeleniumBase {method_name}() failed: {exc}")
        except Exception as exc:
            log.error(f"Error closing browser: {exc}")
        finally:
            self._context = None
            self._page = None
            self._browser = None
            self._playwright = None
            self._seleniumbase_driver = None
            log.info("Browser closed")
