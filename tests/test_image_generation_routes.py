from __future__ import annotations

import sys
import types
import unittest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    FastAPI = None
    TestClient = None


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

if "playwright_stealth" not in sys.modules:
    playwright_stealth_mod = types.ModuleType("playwright_stealth")

    class _FakeStealth:
        script_payload = ""

    playwright_stealth_mod.Stealth = _FakeStealth
    sys.modules["playwright_stealth"] = playwright_stealth_mod


try:
    from src.api import openai_routes as openai_routes_module
    from src.api.openai_routes import openai_router
    from src.chatgpt.models import ChatResponse, ImageInfo
except ModuleNotFoundError:
    openai_routes_module = None
    openai_router = None
    ChatResponse = None
    ImageInfo = None


class _StubImageClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def new_chat(self) -> None:
        return None

    def _extract_thread_id(self) -> str:
        return "thread-1"

    async def generate_image(
        self,
        prompt: str,
        n: int = 1,
        size: str = "1024x1024",
        quality: str = "standard",
        style: str = "vivid",
    ) -> ChatResponse:
        self.calls.append(
            {
                "prompt": prompt,
                "n": n,
                "size": size,
                "quality": quality,
                "style": style,
            }
        )
        return ChatResponse(
            message="image created",
            thread_id="thread-1",
            images=[
                ImageInfo(
                    url="https://example.test/generated.png",
                    local_path="",
                    alt="Generated image",
                    prompt_title="A test image",
                )
            ],
            has_images=True,
        )


class ImageGenerationRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        if FastAPI is None or TestClient is None or openai_router is None:
            self.skipTest("fastapi/pydantic test dependencies are not installed")
        app = FastAPI()
        app.include_router(openai_router)
        self.client = TestClient(app)
        self.stub = _StubImageClient()
        openai_routes_module._client = self.stub

    def tearDown(self) -> None:
        openai_routes_module._client = None

    def test_image_generation_url_response_uses_generate_image(self) -> None:
        response = self.client.post(
            "/v1/images/generations",
            json={
                "prompt": "paint a small red cabin",
                "n": 2,
                "size": "1024x1024",
                "quality": "hd",
                "style": "natural",
                "response_format": "url",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["data"][0]["url"], "https://example.test/generated.png")
        self.assertEqual(payload["data"][0]["revised_prompt"], "A test image")
        self.assertEqual(self.stub.calls[0]["prompt"], "paint a small red cabin")
        self.assertEqual(self.stub.calls[0]["n"], 2)
        self.assertEqual(self.stub.calls[0]["quality"], "hd")

    def test_image_generation_rejects_unknown_response_format(self) -> None:
        response = self.client.post(
            "/v1/images/generations",
            json={
                "prompt": "paint a small red cabin",
                "response_format": "json",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("response_format", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
