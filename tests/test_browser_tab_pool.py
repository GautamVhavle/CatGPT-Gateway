from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.api.browser_gate import BrowserTabPool, PageLease, acquire_browser_page, configure_tab_pool
from src.config import Config


class _FakePage:
    def __init__(self, url: str = "https://chatgpt.com") -> None:
        self.url = url
        self._closed = False

    def is_closed(self) -> bool:
        return self._closed

    async def goto(self, url: str, **_kwargs) -> None:
        self.url = url

    async def close(self) -> None:
        self._closed = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.page = _FakePage("https://chatgpt.com")
        self.context = SimpleNamespace(pages=[self.page])

    async def new_page(self) -> _FakePage:
        page = _FakePage("about:blank")
        self.context.pages.append(page)
        return page


class BrowserTabPoolTests(unittest.TestCase):
    def test_same_session_reuses_tab(self) -> None:
        browser = _FakeBrowser()
        pool = BrowserTabPool(browser)

        async def _run() -> tuple[object, object]:
            async with pool.acquire("sess-a") as first:
                page_one = first.page
                first_turn = first.is_first_turn
            async with pool.acquire("sess-a") as second:
                return page_one, second.page, first_turn, second.is_first_turn

        page_one, page_two, first_turn, second_turn = asyncio.run(_run())
        self.assertIs(page_one, page_two)
        self.assertTrue(first_turn)
        self.assertFalse(second_turn)

    def test_fallback_uses_process_lock_when_pool_disabled(self) -> None:
        configure_tab_pool(None)

        async def _run() -> PageLease:
            async with acquire_browser_page("sess-a") as lease:
                return lease

        with patch.object(Config, "uses_browser", return_value=True):
            lease = asyncio.run(_run())
        self.assertIsNone(lease.page)
        self.assertFalse(lease.is_first_turn)


if __name__ == "__main__":
    unittest.main()
