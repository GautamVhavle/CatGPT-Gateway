"""
Shared browser-access coordination.

API surfaces that touch ChatGPT/Claude must not share one unlocked page.
When a tab pool is configured, independent sessions run in parallel across
tabs (capped by MAX_CONCURRENT_REQUESTS). Same-session work stays serial.
When the pool is not configured, callers fall back to a process-wide lock.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from src.config import Config
from src.log import setup_logging

log = setup_logging("browser_gate")

# Fallback lock used when the tab pool is not configured (tests, MiniMax, startup).
browser_access_lock = asyncio.Lock()

CONTROL_SESSION = "__control__"
CLEANUP_SESSION = "__cleanup__"


@dataclass(frozen=True, slots=True)
class PageLease:
    """A borrowed browser page for the duration of one request."""

    page: Any | None
    is_first_turn: bool
    session_key: str | None = None


class BrowserTabPool:
    """Persistent session tabs plus a small ephemeral pool for stateless requests."""

    def __init__(self, browser: Any) -> None:
        self._browser = browser
        self._semaphore = asyncio.Semaphore(max(1, Config.MAX_CONCURRENT_REQUESTS))
        self._ephemeral: asyncio.Queue[Any] = asyncio.Queue()
        self._pages: dict[str, Any] = {}
        self._urls: dict[str, str] = {}
        self._initialized: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._lru: OrderedDict[str, float] = OrderedDict()
        self._struct_lock = asyncio.Lock()

    def _session_lock(self, session_key: str) -> asyncio.Lock:
        lock = self._locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_key] = lock
        return lock

    def _control_page(self) -> Any | None:
        return getattr(self._browser, "page", None)

    def _is_control_page(self, page: Any) -> bool:
        control = self._control_page()
        return control is not None and page is control

    def _page_is_open(self, page: Any | None) -> bool:
        if page is None:
            return False
        try:
            return not page.is_closed()
        except Exception:
            return False

    async def _create_worker_page(self) -> Any:
        new_page = getattr(self._browser, "new_page", None)
        if not callable(new_page):
            raise RuntimeError("Browser manager cannot open additional tabs")
        page = await new_page()
        log.info("Opened worker browser tab")
        return page

    def _worker_tab_count(self) -> int:
        control = self._control_page()
        context = getattr(self._browser, "context", None)
        pages = list(getattr(context, "pages", []) or [])
        return sum(1 for page in pages if page is not control and self._page_is_open(page))

    async def _close_page(self, page: Any | None) -> None:
        if page is None or self._is_control_page(page) or not self._page_is_open(page):
            return
        try:
            await page.close()
        except Exception as exc:
            log.debug("Error closing worker tab: %s", exc)

    async def _evict_idle_persistent(self) -> bool:
        async with self._struct_lock:
            for session_key in list(self._lru.keys()):
                lock = self._locks.get(session_key)
                if lock and lock.locked():
                    continue
                page = self._pages.get(session_key)
                if not self._page_is_open(page):
                    self._pages.pop(session_key, None)
                    continue
                url = ""
                try:
                    url = page.url or ""
                except Exception:
                    url = ""
                if "/c/" in url or "/chat/" in url:
                    self._urls[session_key] = url
                await self._close_page(page)
                self._pages.pop(session_key, None)
                log.info("Evicted idle tab for session %s", session_key)
                return True
        return False

    async def _discard_idle_ephemeral(self) -> bool:
        try:
            page = self._ephemeral.get_nowait()
        except asyncio.QueueEmpty:
            return False
        await self._close_page(page)
        return True

    async def _ensure_capacity(self) -> None:
        while self._worker_tab_count() >= max(1, Config.MAX_ACTIVE_TABS):
            if await self._discard_idle_ephemeral():
                continue
            if await self._evict_idle_persistent():
                continue
            log.warning("Tab cap reached and no idle tab could be evicted")
            break

    def _touch(self, session_key: str) -> None:
        self._lru[session_key] = time.monotonic()
        self._lru.move_to_end(session_key)

    def _remember_url(self, session_key: str, page: Any) -> None:
        try:
            url = page.url or ""
        except Exception:
            return
        if "/c/" in url or "/chat/" in url:
            self._urls[session_key] = url

    async def _open_persistent_page(self, session_key: str) -> tuple[Any, bool]:
        page = self._pages.get(session_key)
        if self._page_is_open(page):
            self._touch(session_key)
            first_turn = session_key not in self._initialized
            self._initialized.add(session_key)
            return page, first_turn

        if session_key == CONTROL_SESSION:
            page = self._control_page()
            if not self._page_is_open(page):
                raise RuntimeError("Control browser page is not available")
            self._pages[session_key] = page
            self._touch(session_key)
            first_turn = session_key not in self._initialized
            self._initialized.add(session_key)
            return page, first_turn

        await self._ensure_capacity()
        page = await self._create_worker_page()
        known_url = self._urls.get(session_key)
        first_turn = True
        if known_url:
            try:
                await page.goto(known_url, wait_until="domcontentloaded", timeout=25000)
                first_turn = False
                log.info("Restored session %s -> %s", session_key, known_url)
            except Exception as exc:
                log.warning("Failed to restore session %s: %s", session_key, exc)
                known_url = None
        if not known_url:
            await page.goto(Config.provider_url(), wait_until="domcontentloaded", timeout=25000)
        self._pages[session_key] = page
        self._initialized.add(session_key)
        self._touch(session_key)
        return page, first_turn

    async def _borrow_ephemeral_page(self) -> Any:
        try:
            page = self._ephemeral.get_nowait()
            if self._page_is_open(page) and not self._is_control_page(page):
                return page
            await self._close_page(page)
        except asyncio.QueueEmpty:
            pass
        await self._ensure_capacity()
        return await self._create_worker_page()

    async def _reset_ephemeral_page(self, page: Any) -> None:
        target = Config.provider_url().rstrip("/")
        try:
            current = (page.url or "").rstrip("/")
        except Exception:
            current = ""
        if current.startswith(target) and current in {target, f"{target}/"}:
            return
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=25000)
        except Exception as exc:
            log.warning("Failed to reset ephemeral tab: %s", exc)

    @asynccontextmanager
    async def acquire(self, session_key: str | None) -> AsyncIterator[PageLease]:
        if session_key:
            lock = self._session_lock(session_key)
            await lock.acquire()
            await self._semaphore.acquire()
            try:
                page, first_turn = await self._open_persistent_page(session_key)
                yield PageLease(page=page, is_first_turn=first_turn, session_key=session_key)
                self._remember_url(session_key, page)
            finally:
                self._semaphore.release()
                lock.release()
            return

        await self._semaphore.acquire()
        page = None
        keep = True
        try:
            page = await self._borrow_ephemeral_page()
            await self._reset_ephemeral_page(page)
            yield PageLease(page=page, is_first_turn=True, session_key=None)
        except Exception:
            keep = False
            raise
        finally:
            if keep and self._page_is_open(page) and not self._is_control_page(page):
                await self._ephemeral.put(page)
            elif page is not None and not self._is_control_page(page):
                await self._close_page(page)
            self._semaphore.release()


_tab_pool: BrowserTabPool | None = None


def configure_tab_pool(browser: Any | None) -> None:
    """Install or clear the process-wide tab pool. Called from server startup."""
    global _tab_pool
    if browser is None or not Config.uses_browser():
        _tab_pool = None
        log.info("Browser tab pool disabled")
        return
    _tab_pool = BrowserTabPool(browser)
    log.info(
        "Browser tab pool ready (max concurrent=%s, max tabs=%s)",
        Config.MAX_CONCURRENT_REQUESTS,
        Config.MAX_ACTIVE_TABS,
    )


def get_tab_pool() -> BrowserTabPool | None:
    return _tab_pool


@asynccontextmanager
async def acquire_browser_page(session_key: str | None = None) -> AsyncIterator[PageLease]:
    """Yield a page lease. Falls back to the process lock when no pool exists."""
    pool = _tab_pool
    if pool is None:
        if Config.uses_browser():
            async with browser_access_lock:
                yield PageLease(page=None, is_first_turn=False, session_key=session_key)
        else:
            yield PageLease(page=None, is_first_turn=True, session_key=session_key)
        return

    async with pool.acquire(session_key) as lease:
        yield lease
