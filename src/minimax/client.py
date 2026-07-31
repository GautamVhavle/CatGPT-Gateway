"""MiniMax OpenAI-compatible provider client."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.chatgpt.models import ChatResponse
from src.config import Config
from src.log import setup_logging

log = setup_logging("minimax_client")


class MiniMaxClient:
    """High-level client for MiniMax's OpenAI-compatible chat API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        configured_api_key = (
            api_key if api_key is not None else Config.MINIMAX_API_KEY
        )
        self._api_key = configured_api_key.strip()
        if not self._api_key:
            raise RuntimeError(
                "MINIMAX_API_KEY is required when PROVIDER=minimax"
            )

        self._base_url = (base_url or Config.MINIMAX_BASE_URL).rstrip("/")
        self._model = model or Config.MINIMAX_MODEL
        if self._model not in Config.MINIMAX_MODEL_IDS:
            supported = ", ".join(Config.MINIMAX_MODEL_IDS)
            raise ValueError(
                f"Unsupported MiniMax model '{self._model}'. Choose one of: {supported}"
            )

        self._timeout_seconds = timeout_seconds or Config.RESPONSE_TIMEOUT / 1000
        self._opener = opener
        self._threads: dict[str, list[dict[str, str]]] = {}
        self._thread_id = ""
        self._messages: list[dict[str, str]] = []
        self._start_thread()

    async def send_message(
        self,
        text: str,
        image_paths: list[str] | None = None,
        file_paths: list[str] | None = None,
        model: str | None = None,
    ) -> ChatResponse:
        """Send a text message and return a normalized provider response."""
        if image_paths or file_paths:
            raise RuntimeError(
                "File attachments are not supported by the MiniMax provider client"
            )

        model_id = self._resolve_model(model)
        start_time = time.time()
        self._messages.append({"role": "user", "content": text})
        payload = {
            "model": model_id,
            "messages": list(self._messages),
        }

        try:
            response = await asyncio.to_thread(self._post_json, payload)
            response_text = self._extract_response_text(response)
        except Exception:
            self._messages.pop()
            raise

        self._messages.append({"role": "assistant", "content": response_text})
        elapsed_ms = int((time.time() - start_time) * 1000)
        log.info(
            f"Response received ({elapsed_ms}ms, {len(response_text)} chars, "
            f"model={model_id})"
        )
        return ChatResponse(
            message=response_text,
            thread_id=self._thread_id,
            response_time_ms=elapsed_ms,
            images=[],
            has_images=False,
        )

    async def new_chat(self) -> None:
        """Start a new local conversation history."""
        self._start_thread()

    async def navigate_to_thread(self, thread_id: str) -> None:
        """Switch to an existing local conversation history."""
        if thread_id not in self._threads:
            raise ValueError(f"Unknown thread ID: {thread_id}")
        self._thread_id = thread_id
        self._messages = self._threads[thread_id]

    async def get_current_thread_url(self) -> str:
        """Return an empty URL because API-backed threads are local."""
        return ""

    async def list_threads(self) -> list[dict[str, str]]:
        """List local conversation histories, newest first."""
        threads = []
        for thread_id, messages in reversed(self._threads.items()):
            first_user = next(
                (
                    message["content"]
                    for message in messages
                    if message["role"] == "user"
                ),
                "New chat",
            )
            threads.append(
                {
                    "id": thread_id,
                    "title": first_user[:80],
                    "url": "",
                }
            )
        return threads

    def _extract_thread_id(self) -> str:
        """Return the active local conversation ID."""
        return self._thread_id

    def _start_thread(self) -> None:
        self._thread_id = uuid.uuid4().hex
        self._messages = []
        self._threads[self._thread_id] = self._messages

    def _resolve_model(self, requested: str | None) -> str:
        model_id = requested or self._model
        if model_id not in Config.MINIMAX_MODEL_IDS:
            supported = ", ".join(Config.MINIMAX_MODEL_IDS)
            raise ValueError(
                f"Unsupported MiniMax model '{model_id}'. Choose one of: {supported}"
            )
        return model_id

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                raw_response = response.read()
        except HTTPError as exc:
            detail = self._extract_error_message(exc.read())
            raise RuntimeError(
                f"MiniMax API request failed with status {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(f"MiniMax API request failed: {exc.reason}") from exc

        try:
            parsed = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("MiniMax API returned an invalid JSON response") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("MiniMax API returned an unexpected response shape")
        return parsed

    @staticmethod
    def _extract_response_text(response: dict[str, Any]) -> str:
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                "MiniMax API response did not contain assistant content"
            ) from exc

        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            text = "".join(parts)
            if text:
                return text
        raise RuntimeError("MiniMax API response contained empty assistant content")

    @staticmethod
    def _extract_error_message(raw_response: bytes) -> str:
        try:
            parsed = json.loads(raw_response.decode("utf-8"))
            error = parsed.get("error", {}) if isinstance(parsed, dict) else {}
            message = error.get("message") if isinstance(error, dict) else None
            if isinstance(message, str) and message:
                return message
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        return "request failed"
