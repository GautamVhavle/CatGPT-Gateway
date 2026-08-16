"""
Ollama-compatible API routes layered on top of the existing ChatGPT backend.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from src.api.ollama_registry import (
    build_copy_unsupported_payload,
    build_delete_status_payload,
    build_pull_status_payload,
    build_show_payload,
    generate_embeddings,
    get_ollama_profile,
    list_active_models_payload,
    list_tags_payload,
    mark_model_active,
)
from src.api.ollama_schemas import (
    OllamaChatMessage,
    OllamaChatRequest,
    OllamaCopyRequest,
    OllamaDeleteRequest,
    OllamaEmbedRequest,
    OllamaGenerateRequest,
    OllamaPullRequest,
    OllamaShowRequest,
)
from src.api.openai_routes import (
    _execute_chat_completion,
    _resolve_app_key,
    _validate_chat_request,
)
from src.api.openai_schemas import ChatCompletionRequest, ChatMessage
from src.log import setup_logging

log = setup_logging("ollama_routes")

ollama_router = APIRouter()


@ollama_router.get("/api/tags")
async def ollama_tags() -> dict:
    """List configured Ollama-visible model profiles."""
    return list_tags_payload()


@ollama_router.get("/{app_name}/api/tags")
async def ollama_tags_scoped(app_name: str) -> dict:
    _ = app_name
    return list_tags_payload()


@ollama_router.post("/api/show")
async def ollama_show(request: OllamaShowRequest) -> dict:
    """Return static metadata for a configured model profile."""
    profile = _require_profile(request.model)
    return build_show_payload(profile)


@ollama_router.post("/{app_name}/api/show")
async def ollama_show_scoped(app_name: str, request: OllamaShowRequest) -> dict:
    _ = app_name
    return await ollama_show(request)


@ollama_router.get("/api/ps")
async def ollama_ps() -> dict:
    """Return recently active models."""
    return list_active_models_payload()


@ollama_router.post("/api/ps")
async def ollama_ps_post() -> dict:
    """Ollama clients sometimes POST here; keep it compatible."""
    return list_active_models_payload()


@ollama_router.get("/{app_name}/api/ps")
async def ollama_ps_scoped(app_name: str) -> dict:
    _ = app_name
    return list_active_models_payload()


@ollama_router.post("/{app_name}/api/ps")
async def ollama_ps_scoped_post(app_name: str) -> dict:
    _ = app_name
    return list_active_models_payload()


@ollama_router.post("/api/chat")
async def ollama_chat(
    request: OllamaChatRequest,
    http_request: Request,
) -> Response:
    """Map Ollama chat requests to the existing OpenAI-compatible backend."""
    profile = _require_profile(request.model, capability="chat")
    openai_request = _chat_request_from_ollama(request)
    _validate_chat_request(openai_request)
    app_key = _resolve_app_key(openai_request, http_request)
    started_at = time.time()
    response = await _execute_chat_completion(
        openai_request,
        app_key_override=app_key,
        http_request=http_request,
    )
    mark_model_active(profile.name)
    return _ollama_chat_response(response, started_at, stream=request.stream)


@ollama_router.post("/{app_name}/api/chat")
async def ollama_chat_scoped(
    app_name: str,
    request: OllamaChatRequest,
    http_request: Request,
) -> Response:
    profile = _require_profile(request.model, capability="chat")
    openai_request = _chat_request_from_ollama(request)
    _validate_chat_request(openai_request)
    app_key = _resolve_app_key(openai_request, http_request, endpoint_app_name=app_name)
    started_at = time.time()
    response = await _execute_chat_completion(
        openai_request,
        app_key_override=app_key,
        http_request=http_request,
    )
    mark_model_active(profile.name)
    return _ollama_chat_response(response, started_at, stream=request.stream)


@ollama_router.post("/api/generate")
async def ollama_generate(
    request: OllamaGenerateRequest,
    http_request: Request,
) -> Response:
    """Map Ollama text generation requests to the existing chat backend."""
    profile = _require_profile(request.model, capability="chat")
    openai_request = _generate_request_from_ollama(request)
    _validate_chat_request(openai_request)
    app_key = _resolve_app_key(openai_request, http_request)
    started_at = time.time()
    response = await _execute_chat_completion(
        openai_request,
        app_key_override=app_key,
        http_request=http_request,
    )
    mark_model_active(profile.name)
    return _ollama_generate_response(response, started_at, stream=request.stream)


@ollama_router.post("/{app_name}/api/generate")
async def ollama_generate_scoped(
    app_name: str,
    request: OllamaGenerateRequest,
    http_request: Request,
) -> Response:
    profile = _require_profile(request.model, capability="chat")
    openai_request = _generate_request_from_ollama(request)
    _validate_chat_request(openai_request)
    app_key = _resolve_app_key(openai_request, http_request, endpoint_app_name=app_name)
    started_at = time.time()
    response = await _execute_chat_completion(
        openai_request,
        app_key_override=app_key,
        http_request=http_request,
    )
    mark_model_active(profile.name)
    return _ollama_generate_response(response, started_at, stream=request.stream)


@ollama_router.post("/api/embed")
async def ollama_embed(request: OllamaEmbedRequest) -> dict:
    """Return deterministic compatibility embeddings for configured models."""
    _require_profile(request.model)
    inputs = request.input if isinstance(request.input, list) else [request.input]
    return generate_embeddings(request.model, inputs)


@ollama_router.post("/{app_name}/api/embed")
async def ollama_embed_scoped(app_name: str, request: OllamaEmbedRequest) -> dict:
    _ = app_name
    return await ollama_embed(request)


@ollama_router.post("/api/embeddings")
async def ollama_embeddings_legacy(request: OllamaEmbedRequest) -> dict:
    """Legacy Ollama embeddings alias."""
    return await ollama_embed(request)


@ollama_router.post("/{app_name}/api/embeddings")
async def ollama_embeddings_legacy_scoped(app_name: str, request: OllamaEmbedRequest) -> dict:
    _ = app_name
    return await ollama_embed(request)


@ollama_router.post("/api/pull")
async def ollama_pull(request: OllamaPullRequest) -> Response:
    """Safety mock that succeeds for models present in our registry."""
    _require_profile(request.model)
    payload = build_pull_status_payload(request.model)
    if request.stream:
        return _ndjson_response([payload])
    return JSONResponse(payload, media_type="application/json")


@ollama_router.post("/{app_name}/api/pull")
async def ollama_pull_scoped(app_name: str, request: OllamaPullRequest) -> Response:
    _ = app_name
    return await ollama_pull(request)


@ollama_router.delete("/api/delete")
async def ollama_delete(request: OllamaDeleteRequest) -> dict:
    """Mock delete success so Ollama-based UIs don't error on unsupported ops."""
    name = request.model or request.name or ""
    if not name:
        raise HTTPException(status_code=400, detail="model or name is required")
    return build_delete_status_payload(name)


@ollama_router.delete("/{app_name}/api/delete")
async def ollama_delete_scoped(app_name: str, request: OllamaDeleteRequest) -> dict:
    _ = app_name
    return await ollama_delete(request)


@ollama_router.post("/api/copy")
async def ollama_copy(request: OllamaCopyRequest) -> JSONResponse:
    """Explicitly unsupported safety mock."""
    payload = build_copy_unsupported_payload(request.source, request.destination)
    return JSONResponse(payload, status_code=501, media_type="application/json")


@ollama_router.post("/{app_name}/api/copy")
async def ollama_copy_scoped(app_name: str, request: OllamaCopyRequest) -> JSONResponse:
    _ = app_name
    return await ollama_copy(request)


def _require_profile(name: str, capability: str | None = None):
    profile = get_ollama_profile(name)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Model '{name}' is not configured")
    if capability and profile.capability != capability:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{name}' is registered for {profile.capability}, not {capability}",
        )
    return profile


def _chat_request_from_ollama(request: OllamaChatRequest) -> ChatCompletionRequest:
    messages: list[ChatMessage] = []
    for msg in request.messages:
        messages.append(_chat_message_from_ollama(msg))
    if not messages:
        raise HTTPException(status_code=400, detail="messages array cannot be empty")
    return ChatCompletionRequest(
        model=request.model,
        messages=messages,
        stream=False,
        response_format=_map_ollama_format(request.format),
    )


def _generate_request_from_ollama(request: OllamaGenerateRequest) -> ChatCompletionRequest:
    messages: list[ChatMessage] = []
    if request.system:
        messages.append(ChatMessage(role="system", content=request.system))

    prompt_text = request.prompt or ""
    if request.template and not request.raw:
        prompt_text = f"[Template]\n{request.template}\n\n{prompt_text}"

    content = _build_user_content(prompt_text, request.images or [])
    messages.append(ChatMessage(role="user", content=content))

    return ChatCompletionRequest(
        model=request.model,
        messages=messages,
        stream=False,
        response_format=_map_ollama_format(request.format),
    )


def _chat_message_from_ollama(message: OllamaChatMessage) -> ChatMessage:
    content = _build_user_content(message.content or "", message.images or []) if message.images else (message.content or "")
    return ChatMessage(role=message.role, content=content)


def _build_user_content(text: str, images: list[str]) -> str | list[dict[str, Any]]:
    if not images:
        return text

    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    for image in images:
        url = image.strip()
        if not url:
            continue
        if not (url.startswith("data:") or url.startswith("http://") or url.startswith("https://")):
            url = f"data:image/png;base64,{url}"
        parts.append({"type": "image_url", "image_url": {"url": url}})

    return parts or text


def _map_ollama_format(value: Any) -> Any:
    if value in (None, "", False):
        return None
    if value == "json":
        return {"type": "json_object"}
    if isinstance(value, dict):
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "ollama_schema",
                "schema": value,
            },
        }
    return value


def _ollama_chat_response(response, started_at: float, stream: bool) -> Response:
    message_content = response.choices[0].message.content or ""
    elapsed_ns = _elapsed_ns(started_at)
    base_payload = {
        "model": response.model,
        "created_at": _created_at_iso(),
        "message": {
            "role": "assistant",
            "content": message_content,
        },
        "done_reason": response.choices[0].finish_reason,
        "done": True,
        "total_duration": elapsed_ns,
        "load_duration": 0,
        "prompt_eval_count": response.usage.prompt_tokens,
        "eval_count": response.usage.completion_tokens,
    }
    if not stream:
        return JSONResponse(base_payload, media_type="application/json")

    chunks = [
        {
            "model": response.model,
            "created_at": base_payload["created_at"],
            "message": {"role": "assistant", "content": message_content},
            "done": False,
        },
        {
            **base_payload,
            "message": {"role": "assistant", "content": ""},
        },
    ]
    return _ndjson_response(chunks)


def _ollama_generate_response(response, started_at: float, stream: bool) -> Response:
    text = response.choices[0].message.content or ""
    elapsed_ns = _elapsed_ns(started_at)
    base_payload = {
        "model": response.model,
        "created_at": _created_at_iso(),
        "response": text,
        "done_reason": response.choices[0].finish_reason,
        "done": True,
        "context": [],
        "total_duration": elapsed_ns,
        "load_duration": 0,
        "prompt_eval_count": response.usage.prompt_tokens,
        "eval_count": response.usage.completion_tokens,
    }
    if not stream:
        return JSONResponse(base_payload, media_type="application/json")

    chunks = [
        {
            "model": response.model,
            "created_at": base_payload["created_at"],
            "response": text,
            "done": False,
        },
        {
            **base_payload,
            "response": "",
        },
    ]
    return _ndjson_response(chunks)


def _ndjson_response(items: Iterable[dict[str, Any]]) -> StreamingResponse:
    def _iter_lines():
        for item in items:
            yield json.dumps(item, ensure_ascii=False) + "\n"

    return StreamingResponse(_iter_lines(), media_type="application/x-ndjson")


def _created_at_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _elapsed_ns(started_at: float) -> int:
    return int((time.time() - started_at) * 1_000_000_000)
