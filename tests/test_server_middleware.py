from __future__ import annotations

import asyncio
import sys
import types
import unittest


# Keep importing the server module from requiring browser automation packages.
if "patchright" not in sys.modules:
    patchright_mod = types.ModuleType("patchright")
    async_api_mod = types.ModuleType("patchright.async_api")
    async_api_mod.Page = object
    async_api_mod.BrowserContext = object
    async_api_mod.Playwright = object
    async_api_mod.Frame = object
    async_api_mod.Request = object
    async_api_mod.Response = object

    async def _fake_async_playwright():
        return None

    async_api_mod.async_playwright = _fake_async_playwright
    sys.modules["patchright"] = patchright_mod
    sys.modules["patchright.async_api"] = async_api_mod

    impl_mod = types.ModuleType("patchright._impl")
    errors_mod = types.ModuleType("patchright._impl._errors")

    class TargetClosedError(Exception):
        pass

    errors_mod.TargetClosedError = TargetClosedError
    sys.modules["patchright._impl"] = impl_mod
    sys.modules["patchright._impl._errors"] = errors_mod

if "playwright_stealth" not in sys.modules:
    playwright_stealth_mod = types.ModuleType("playwright_stealth")

    class _FakeStealth:
        script_payload = ""

    playwright_stealth_mod.Stealth = _FakeStealth
    sys.modules["playwright_stealth"] = playwright_stealth_mod

from src.api.server import BearerTokenMiddleware
from src.config import Config


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _call_middleware(path: str, headers: dict[str, str] | None = None) -> list[dict]:
    events: list[dict] = []
    encoded_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": encoded_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
        "query_string": b"",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        events.append(message)

    await BearerTokenMiddleware(_ok_app)(scope, receive, send)
    return events


class BearerTokenMiddlewareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_token = Config.API_TOKEN
        self.original_optional = Config.API_TOKEN_OPTIONAL
        Config.API_TOKEN = "secret"
        Config.API_TOKEN_OPTIONAL = False

    def tearDown(self) -> None:
        Config.API_TOKEN = self.original_token
        Config.API_TOKEN_OPTIONAL = self.original_optional

    def test_accepts_matching_bearer_token(self) -> None:
        events = asyncio.run(
            _call_middleware("/cline/v1/chat/completions", {"Authorization": "Bearer secret"})
        )
        self.assertEqual(events[0]["status"], 204)

    def test_accepts_x_api_key_header(self) -> None:
        events = asyncio.run(
            _call_middleware("/v1/chat/completions", {"x-api-key": "secret"})
        )
        self.assertEqual(events[0]["status"], 204)

    def test_accepts_anthropic_api_key_header(self) -> None:
        events = asyncio.run(
            _call_middleware("/v1/messages", {"anthropic-api-key": "secret"})
        )
        self.assertEqual(events[0]["status"], 204)

    def test_optional_token_allows_missing_header(self) -> None:
        Config.API_TOKEN_OPTIONAL = True
        events = asyncio.run(_call_middleware("/cline/v1/chat/completions"))
        self.assertEqual(events[0]["status"], 204)


if __name__ == "__main__":
    unittest.main()
