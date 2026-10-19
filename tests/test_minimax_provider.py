from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from fastapi import HTTPException

from src.api import openai_routes, server
from src.api.openai_schemas import ChatCompletionRequest, ChatMessage
from src.chatgpt.models import ChatResponse
from src.config import Config
from src.minimax.client import MiniMaxClient


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


class RecordingOpener:
    def __init__(self) -> None:
        self.request = None
        self.timeout = None

    def __call__(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Provider response",
                        }
                    }
                ]
            }
        )


class RecordingProviderClient:
    def __init__(self) -> None:
        self.model = None

    async def send_message(
        self,
        text: str,
        image_paths=None,
        file_paths=None,
        model: str | None = None,
        stateless: bool = False,
        page=None,
    ) -> ChatResponse:
        self.model = model
        return ChatResponse(message="Provider response", thread_id="thread-id")

    async def new_chat(self) -> None:
        return None


class MiniMaxProviderTests(unittest.TestCase):
    def test_target_configuration_exposes_both_regions_and_models(self) -> None:
        self.assertEqual(
            Config.MINIMAX_BASE_URLS,
            {
                "global_en": "https://api.minimax.io/v1",
                "cn_zh": "https://api.minimaxi.com/v1",
            },
        )
        self.assertEqual(
            Config.MINIMAX_MODEL_IDS,
            ("MiniMax-M3.1-Flash", "MiniMax-M3"),
        )

    def test_client_uses_selected_endpoint_and_model(self) -> None:
        opener = RecordingOpener()
        client = MiniMaxClient(
            api_key="test-key",
            base_url=Config.MINIMAX_BASE_URLS["cn_zh"],
            model="MiniMax-M2.7",
            opener=opener,
        )

        result = asyncio.run(client.send_message("Hello", model="MiniMax-M2.7"))

        self.assertEqual(result.message, "Provider response")
        self.assertTrue(result.thread_id)
        self.assertEqual(
            opener.request.full_url,
            "https://api.minimaxi.com/v1/chat/completions",
        )
        self.assertEqual(
            opener.request.get_header("Authorization"),
            "Bearer test-key",
        )
        payload = json.loads(opener.request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "MiniMax-M2.7")
        self.assertEqual(
            payload["messages"],
            [{"role": "user", "content": "Hello"}],
        )

    def test_current_models_reach_both_api_regions_unchanged(self) -> None:
        for endpoint in Config.MINIMAX_BASE_URLS.values():
            for model in Config.MINIMAX_MODEL_IDS:
                with self.subTest(endpoint=endpoint, model=model):
                    opener = RecordingOpener()
                    client = MiniMaxClient(api_key="test-key", base_url=endpoint, opener=opener)
                    asyncio.run(client.send_message("Hello", model=model))
                    self.assertEqual(opener.request.full_url, endpoint + "/chat/completions")
                    payload = json.loads(opener.request.data)
                    self.assertEqual(payload["model"], model)
                    self.assertEqual(set(payload), {"model", "messages"})

    def test_configured_legacy_and_custom_defaults_are_preserved(self) -> None:
        for model in ("MiniMax-M2.7", "custom-minimax-deployment"):
            with patch.object(Config, "PROVIDER", "minimax"), patch.object(Config, "MINIMAX_MODEL", model):
                Config.validate_provider()
                self.assertEqual(Config.resolve_model_id(None), model)
                self.assertEqual(Config.resolve_model_id(model), model)
                self.assertEqual(Config.provider_model_ids(), ("MiniMax-M3.1-Flash", "MiniMax-M3"))
                opener = RecordingOpener()
                client = MiniMaxClient(api_key="test-key", opener=opener)
                asyncio.run(client.send_message("Hello"))
                self.assertEqual(json.loads(opener.request.data)["model"], model)

    def test_unknown_request_model_is_rejected(self) -> None:
        with patch.object(Config, "PROVIDER", "minimax"):
            with self.assertRaises(ValueError):
                Config.resolve_model_id("typo-model")
        with self.assertRaises(ValueError):
            MiniMaxClient(api_key="test-key", model="typo-model")

    def test_non_minimax_provider_defaults_are_unchanged(self) -> None:
        for provider, model in (("chatgpt", "catgpt-browser"), ("claude", "claude-browser")):
            with patch.object(Config, "PROVIDER", provider):
                self.assertEqual(Config.provider_model_ids(), (model,))
                self.assertEqual(Config.default_model_id(), model)
                self.assertTrue(Config.uses_browser())

    def test_models_endpoint_lists_target_models(self) -> None:
        with patch.object(Config, "PROVIDER", "minimax"):
            response = asyncio.run(openai_routes.list_models())

        self.assertEqual(
            [model.id for model in response.data],
            ["MiniMax-M3.1-Flash", "MiniMax-M3"],
        )
        self.assertEqual(
            [model.owned_by for model in response.data],
            ["minimax", "minimax"],
        )

    def test_chat_route_maps_default_request_model(self) -> None:
        client = RecordingProviderClient()
        request = ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="Hello")]
        )
        previous_client = openai_routes._client

        try:
            openai_routes._client = client
            with patch.object(Config, "PROVIDER", "minimax"):
                response = asyncio.run(
                    openai_routes.create_chat_completion(request, None)
                )

            self.assertEqual(client.model, "MiniMax-M3.1-Flash")
            self.assertEqual(response.model, "MiniMax-M3.1-Flash")
        finally:
            openai_routes._client = previous_client

    def test_openai_requests_start_with_isolated_provider_history(self) -> None:
        opener = RecordingOpener()
        client = MiniMaxClient(api_key="test-key", opener=opener)
        previous_client = openai_routes._client

        try:
            openai_routes._client = client
            with patch.object(Config, "PROVIDER", "minimax"):
                asyncio.run(
                    openai_routes.create_chat_completion(
                        ChatCompletionRequest(
                            messages=[ChatMessage(role="user", content="first secret")]
                        ), None
                    )
                )
                asyncio.run(
                    openai_routes.create_chat_completion(
                        ChatCompletionRequest(
                            messages=[ChatMessage(role="user", content="second request")]
                        ), None
                    )
                )

            payload = json.loads(opener.request.data.decode("utf-8"))
            self.assertEqual(
                payload["messages"],
                [{"role": "user", "content": "second request"}],
            )
            self.assertEqual(client._messages, [])
        finally:
            openai_routes._client = previous_client

    def test_chat_route_rejects_minimax_attachments_before_provider(self) -> None:
        client = RecordingProviderClient()
        request = ChatCompletionRequest(
            messages=[
                ChatMessage(
                    role="user",
                    content=[
                        {"type": "text", "text": "Describe this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/image.png"},
                        },
                    ],
                )
            ]
        )
        previous_client = openai_routes._client
        openai_routes._client = client

        try:
            with patch.object(Config, "PROVIDER", "minimax"):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(openai_routes.create_chat_completion(request, None))

            self.assertEqual(raised.exception.status_code, 501)
            self.assertIsNone(client.model)
        finally:
            openai_routes._client = previous_client

    def test_startup_selects_direct_client_without_browser(self) -> None:
        fake_client = object()

        async def run_lifespan() -> None:
            async with server.lifespan(server.app):
                self.assertIsNone(server._browser)
                self.assertIs(server._client, fake_client)

        try:
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(server.Config, "PROVIDER", "minimax")
                )
                stack.enter_context(
                    patch.object(server.Config, "MINIMAX_API_KEY", "test-key")
                )
                stack.enter_context(
                    patch.object(server, "MiniMaxClient", return_value=fake_client)
                )
                stack.enter_context(
                    patch.object(
                        server.BrowserManager,
                        "start",
                        side_effect=AssertionError("browser should not start"),
                    )
                )
                set_client = stack.enter_context(patch.object(server, "set_client"))
                set_openai_client = stack.enter_context(
                    patch.object(server, "set_openai_client")
                )
                asyncio.run(run_lifespan())

            set_client.assert_called_once_with(fake_client, None)
            set_openai_client.assert_called_once_with(fake_client, None)
        finally:
            server._browser = None
            server._client = None


if __name__ == "__main__":
    unittest.main()
