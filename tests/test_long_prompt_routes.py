from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from fastapi import HTTPException

from src.api import routes
from src.api import openai_routes
from src.api.openai_schemas import ChatCompletionRequest, ChatMessage
from src.api.schemas import ChatRequest
from src.chatgpt.errors import PromptAttachmentFallbackError, PromptTooLongError


class _FailingClient:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def send_message(self, *_args, **_kwargs):
        raise self.error


class LongPromptRouteTests(IsolatedAsyncioTestCase):
    @staticmethod
    @asynccontextmanager
    async def _fake_page(_session):
        yield SimpleNamespace(page=object())

    async def _assert_status(self, error: Exception, status_code: int) -> None:
        client = _FailingClient(error)
        with (
            patch.object(routes, "_get_client", return_value=client),
            patch.object(routes, "acquire_browser_page", self._fake_page),
        ):
            with self.assertRaises(HTTPException) as context:
                await routes.chat(ChatRequest(message="long prompt"))

        self.assertEqual(context.exception.status_code, status_code)

    async def test_prompt_too_long_maps_to_http_413(self) -> None:
        await self._assert_status(PromptTooLongError("too long"), 413)

    async def test_attachment_fallback_failure_maps_to_http_502(self) -> None:
        await self._assert_status(PromptAttachmentFallbackError("upload failed"), 502)

    async def test_openai_chat_executor_maps_prompt_too_long_to_http_413(self) -> None:
        client = _FailingClient(PromptTooLongError("too long"))
        request = ChatCompletionRequest(
            model="catgpt-browser",
            messages=[ChatMessage(role="user", content="long prompt")],
        )
        with (
            patch.object(openai_routes, "_get_client", return_value=client),
            patch.object(openai_routes, "_resolve_model_id", return_value="catgpt-browser"),
            patch.object(openai_routes, "_tab_session_key", return_value=""),
            patch.object(openai_routes, "_bind_client", return_value=client),
            patch.object(openai_routes, "acquire_browser_page", self._fake_page),
            patch.object(openai_routes.Config, "uses_browser", return_value=False),
        ):
            with self.assertRaises(HTTPException) as context:
                await openai_routes._execute_chat_completion(request)

        self.assertEqual(context.exception.status_code, 413)

    async def test_async_job_preserves_prompt_error_status(self) -> None:
        job_id = "test-long-prompt-job"
        request = ChatCompletionRequest(
            model="catgpt-browser",
            messages=[ChatMessage(role="user", content="long prompt")],
        )
        job = openai_routes.ChatCompletionJobResponse(
            id=job_id,
            status="queued",
            model=request.model,
        )
        async with openai_routes._jobs_lock:
            openai_routes._jobs[job_id] = job

        async def fail_execution(*_args, **_kwargs):
            raise PromptTooLongError("too long")

        try:
            with patch.object(openai_routes, "_execute_chat_completion", fail_execution):
                await openai_routes._run_async_chat_job(job_id, request)
        finally:
            async with openai_routes._jobs_lock:
                openai_routes._jobs.pop(job_id, None)

        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_status_code, 413)


if __name__ == "__main__":
    import unittest

    unittest.main()
