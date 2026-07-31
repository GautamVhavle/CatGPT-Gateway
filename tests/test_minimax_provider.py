from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

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
            ("MiniMax-M3", "MiniMax-M2.7"),
        )

    def test_client_uses_selected_endpoint_and_model(self) -> None:
        opener = RecordingOpener()
        client = MiniMaxClient(
            api_key="test-key",
            base_url=Config.MINIMAX_BASE_URLS["cn_zh"],
            model="MiniMax-M3",
            opener=opener,
        )

        result = asyncio.run(
            client.send_message("Hello", model="MiniMax-M2.7")
        )

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

    def test_models_endpoint_lists_target_models(self) -> None:
        with patch.object(Config, "PROVIDER", "minimax"):
            response = asyncio.run(openai_routes.list_models())

        self.assertEqual(
            [model.id for model in response.data],
            ["MiniMax-M3", "MiniMax-M2.7"],
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
        previous_lock = openai_routes._lock
        previous_count = openai_routes._thread_message_count
        previous_response_time = openai_routes._last_response_time

        try:
            openai_routes._client = client
            openai_routes._lock = None
            openai_routes._thread_message_count = 0
            openai_routes._last_response_time = 0.0
            with patch.object(Config, "PROVIDER", "minimax"):
                response = asyncio.run(
                    openai_routes.create_chat_completion(request)
                )

            self.assertEqual(client.model, "MiniMax-M3")
            self.assertEqual(response.model, "MiniMax-M3")
        finally:
            openai_routes._client = previous_client
            openai_routes._lock = previous_lock
            openai_routes._thread_message_count = previous_count
            openai_routes._last_response_time = previous_response_time

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
            set_openai_client.assert_called_once_with(fake_client)
        finally:
            server._browser = None
            server._client = None


if __name__ == "__main__":
    unittest.main()
