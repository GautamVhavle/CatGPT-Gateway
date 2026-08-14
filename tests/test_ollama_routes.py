from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

# Provide minimal browser stubs before importing API modules.
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

from src.api.ollama_registry import build_show_payload, generate_embeddings, get_ollama_profile, list_ollama_profiles
from src.api.ollama_routes import ollama_router
from src.api.openai_schemas import ChatCompletionResponse, Choice, ChoiceMessage, UsageInfo


def _make_stub_chat_response(model: str = "catgpt-browser", text: str = "hello from catgpt") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        model=model,
        choices=[
            Choice(
                index=0,
                message=ChoiceMessage(role="assistant", content=text),
                finish_reason="stop",
            )
        ],
        usage=UsageInfo(prompt_tokens=10, completion_tokens=4, total_tokens=14),
    )


class OllamaRegistryTests(unittest.TestCase):
    def test_profiles_include_chat_and_embedding_models(self) -> None:
        with patch("src.api.ollama_registry.Config.OLLAMA_EMBEDDING_MODELS", "nomic-embed-text,mxbai-embed-large"):
            names = [profile.name for profile in list_ollama_profiles()]
        self.assertIn("catgpt-browser", names)
        self.assertIn("nomic-embed-text", names)
        self.assertIn("mxbai-embed-large", names)

    def test_show_payload_is_deterministic(self) -> None:
        profile = get_ollama_profile("catgpt-browser")
        assert profile is not None
        payload = build_show_payload(profile)
        self.assertEqual(payload["digest"], profile.digest)
        self.assertIn("FROM catgpt-browser", payload["modelfile"])

    def test_embeddings_are_deterministic(self) -> None:
        first = generate_embeddings("nomic-embed-text", ["hello world"])
        second = generate_embeddings("nomic-embed-text", ["hello world"])
        self.assertEqual(first["embeddings"], second["embeddings"])
        self.assertEqual(len(first["embeddings"][0]), 768)


class OllamaRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(ollama_router)
        self.client = TestClient(app)

    def test_tags_endpoint_returns_models(self) -> None:
        response = self.client.get("/api/tags")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("models", payload)
        self.assertTrue(any(item["name"] == "catgpt-browser" for item in payload["models"]))

    def test_embed_endpoint_returns_embedding_array(self) -> None:
        response = self.client.post("/api/embed", json={"model": "nomic-embed-text", "input": "hello"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["model"], "nomic-embed-text")
        self.assertEqual(len(payload["embeddings"]), 1)
        self.assertEqual(len(payload["embeddings"][0]), 768)

    def test_chat_stream_endpoint_returns_ndjson(self) -> None:
        async def _fake_execute(request, app_key_override="", http_request=None):
            _ = request, app_key_override
            return _make_stub_chat_response(text="streamed hello")

        with patch("src.api.ollama_routes._execute_chat_completion", side_effect=_fake_execute), patch(
            "src.api.ollama_routes._resolve_app_key",
            return_value="",
        ):
            response = self.client.post(
                "/api/chat",
                json={
                    "model": "catgpt-browser",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "application/x-ndjson")
        lines = [line for line in response.text.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertIn('"done": false', lines[0])
        self.assertIn('"done": true', lines[1])

    def test_generate_scoped_endpoint_maps_to_chat_backend(self) -> None:
        async def _fake_execute(request, app_key_override="", http_request=None):
            self.assertEqual(app_key_override, "endpoint:demo")
            self.assertEqual(request.messages[0].role, "user")
            return _make_stub_chat_response(model=request.model, text="generated text")

        with patch("src.api.ollama_routes._execute_chat_completion", side_effect=_fake_execute), patch(
            "src.api.ollama_routes._resolve_app_key",
            return_value="endpoint:demo",
        ):
            response = self.client.post(
                "/demo/api/generate",
                json={"model": "catgpt-browser", "prompt": "Write one sentence."},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["response"], "generated text")
        self.assertEqual(payload["model"], "catgpt-browser")


if __name__ == "__main__":
    unittest.main()
