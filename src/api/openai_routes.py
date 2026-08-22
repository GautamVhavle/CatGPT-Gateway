"""
OpenAI-compatible API routes.

Provides:
  POST /v1/chat/completions   - chat completions (with tool/function calling)
  POST /v1/messages           - Anthropic Messages API adapter
  GET  /v1/models             - list available models

Browser work is coordinated through acquire_browser_page so independent
sessions can run in parallel tabs.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import hashlib
import json
import re
import time
import uuid
from urllib.parse import urlparse
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.api.openai_schemas import (
    ChatCompletionAsyncRequest,
    ChatCompletionJobResponse,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ChoiceMessage,
    AudioInfo,
    FunctionCallInfo,
    FunctionDefinition,
    ImageData,
    ImageGenerationRequest,
    ImagesResponse,
    ModelListResponse,
    ModelObject,
    ResponseInputItem,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseOutputToolCall,
    ResponsesRequest,
    ResponsesResponse,
    ResponsesUsageInfo,
    ToolCall,
    ToolDefinition,
    UsageInfo,)
from src.api.attachment_expander import (
    AttachmentPageDescriptor,
    build_attachment_context_note,
    expand_attachments_for_chatgpt,
)
from src.api.browser_gate import (
    CLEANUP_SESSION,
    acquire_browser_page,
    browser_access_lock,
)
from src.chatgpt.client import ChatGPTClient
from src.chatgpt.errors import PromptAttachmentFallbackError, PromptTooLongError
from src.claude.client import ClaudeClient
from src.minimax.client import MiniMaxClient
from src.chatgpt.model_registry import (
    PUBLIC_BROWSER_MODEL_ID,
    is_supported_chat_model,
    list_public_chat_models,
)
from src.config import Config
from src.log import setup_logging

log = setup_logging("openai_routes")

openai_router = APIRouter()

# Global reference - set by server.py at startup
ProviderClient = ChatGPTClient | ClaudeClient | MiniMaxClient
_client: ProviderClient | None = None

# Kept for compatibility with integrations that reset the legacy route state.
_lock: asyncio.Lock | None = None
_thread_message_count = 0
_last_response_time = 0.0

_jobs_lock = asyncio.Lock()
_jobs: dict[str, ChatCompletionJobResponse] = {}
_job_app_keys: dict[str, str] = {}
_cache_lock = asyncio.Lock()
_response_cache: dict[str, tuple[float, ChatCompletionResponse]] = {}
_contract_lock = asyncio.Lock()
_thread_contracts: dict[str, tuple[float, str]] = {}
_thread_user_contracts: dict[str, tuple[float, str, str]] = {}
_thread_last_user_text: dict[str, tuple[float, str]] = {}
_app_thread_lock = asyncio.Lock()


@dataclass(slots=True)
class _AppThreadMapping:
    last_used: float
    thread_id: str
    created_by_catgpt: bool = False


_app_threads: dict[str, _AppThreadMapping] = {}

MODEL_ID = PUBLIC_BROWSER_MODEL_ID
_CACHE_TTL_SECONDS = 600
_CACHE_MAX_ENTRIES = 256
_CONTRACT_TTL_SECONDS = max(60, Config.API_THREAD_CONTRACT_TTL_SECONDS)
_APP_THREAD_TTL_SECONDS = max(300, Config.API_APP_THREAD_TTL_SECONDS)
_APP_KEY_HEADERS = (
    "x-session-id",
    "session-id",
    "x-catgpt-app-key",
    "x-app-name",
    "x-client-name",
    "x-service-name",
    "x-application-name",
    "x-requested-with",
)
_THREAD_TITLE_TTL_SECONDS = 600
_thread_title_lock = asyncio.Lock()
_thread_titles: dict[str, tuple[float, str]] = {}


def set_openai_client(client: ProviderClient) -> None:
    """Called by server.py to inject the active provider client."""
    global _client
    _client = client


def _get_client() -> ProviderClient:
    if _client is None:
        raise HTTPException(status_code=503, detail="Provider client not initialized")
    return _client


def _bind_client(client: ProviderClient, page: Any | None) -> ProviderClient:
    """Bind a provider client to a leased tab when the pool supplied one."""
    bind_page = getattr(client, "bind_page", None)
    if page is None or not callable(bind_page):
        return client
    return bind_page(page)


def _tab_session_key(
    request: Any,
    http_request: Request | None,
    app_key: str = "",
) -> str | None:
    """Stable tab identity: session header, thread id, app key, then user."""
    if http_request is not None:
        for header_name in ("x-session-id", "session-id"):
            value = (http_request.headers.get(header_name) or "").strip()
            if value:
                return value
    thread_id = (getattr(request, "thread_id", None) or "").strip()
    if thread_id:
        return f"thread:{thread_id}"
    if app_key:
        return f"app:{app_key}"
    user = (getattr(request, "user", None) or "").strip()
    if user:
        return f"user:{user}"
    return None


def _latest_turn_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Keep system prompts plus the latest user turn and its tool results.

    ChatGPT already has prior turns when we stay on a thread, so resending the
    client's full history only bloats the composer.
    """
    if not messages:
        return []
    systems = [message for message in messages if message.role == "system"]
    latest: list[ChatMessage] = []
    for message in reversed(messages):
        if message.role in {"user", "tool"}:
            latest.insert(0, message)
            if message.role == "user":
                break
    if not latest:
        non_system = [message for message in messages if message.role != "system"]
        latest = non_system[-1:]
    return systems + latest


# -- Helpers -----------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars per token)."""
    return max(1, len(text) // 4)


def _model_dump_compat(model: Any, **kwargs) -> dict:
    """Pydantic v1/v2 compatible model dump helper."""
    if hasattr(model, "model_dump"):
        return model.model_dump(**kwargs)
    if hasattr(model, "dict"):
        safe_kwargs = {k: v for k, v in kwargs.items() if k != "mode"}
        return model.dict(**safe_kwargs)
    return dict(getattr(model, "__dict__", {}))


def _model_copy_compat(model: Any, **kwargs):
    """Pydantic v1/v2 compatible model copy helper."""
    if hasattr(model, "model_copy"):
        return model.model_copy(**kwargs)
    if hasattr(model, "copy"):
        return model.copy(**kwargs)
    cloned = copy.deepcopy(model) if kwargs.get("deep") else copy.copy(model)
    for key, value in (kwargs.get("update") or {}).items():
        setattr(cloned, key, value)
    return cloned


def _shrink_for_cache(value: Any) -> Any:
    """Reduce large strings to a digest so cache-key generation stays cheap."""
    if isinstance(value, str):
        if len(value) > 512:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            return f"sha256:{digest}:len:{len(value)}"
        return value
    if isinstance(value, list):
        return [_shrink_for_cache(v) for v in value]
    if isinstance(value, dict):
        return {k: _shrink_for_cache(v) for k, v in value.items()}
    return value


def _cache_key_for_request_with_app(request: ChatCompletionRequest, app_key: str) -> str:
    """
    Build a stable cache key with optional app partitioning.

    When app-thread mode is enabled, app-specific thread context can affect output,
    so app key must be part of the cache identity to avoid cross-app cache reuse.
    """
    payload = _model_dump_compat(request, mode="json", exclude={"stream", "user"})
    if Config.API_APP_THREAD_MODE and app_key:
        payload["_app_key"] = app_key
    compact_payload = _shrink_for_cache(payload)
    canonical = json.dumps(compact_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _host_from_header_url(value: str) -> str:
    """Extract normalized host:port from URL-like header values."""
    raw = (value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    host = (parsed.netloc or parsed.path or "").strip().lower()
    return host


def _normalize_key_part(value: str) -> str:
    """Normalize user/header-derived key parts for stable app routing keys."""
    cleaned = re.sub(r"\s+", " ", (value or "").strip().lower())
    return cleaned[:200]


def _derive_app_key(
    request: ChatCompletionRequest,
    http_request: Request | None,
    endpoint_app_name: str = "",
) -> str:
    """
    Derive an app routing key for app-thread mode.

    Priority:
    1) explicit endpoint app name (/{app_name}/v1/...)
    2) explicit OpenAI `user` field
    3) app-identifying headers
    4) Origin/Referer host
    5) User-Agent product token
    6) client IP (last fallback)
    """
    endpoint_name = (endpoint_app_name or "").strip()
    if endpoint_name:
        return f"endpoint:{_normalize_key_part(endpoint_name)}"

    explicit_user = (request.user or "").strip() if getattr(request, "user", None) else ""
    if explicit_user:
        return f"user:{_normalize_key_part(explicit_user)}"

    if http_request is None:
        return ""

    headers = http_request.headers

    for header_name in _APP_KEY_HEADERS:
        value = (headers.get(header_name) or "").strip()
        if value:
            return f"hdr:{header_name}:{_normalize_key_part(value)}"

    origin_host = _host_from_header_url(headers.get("origin", ""))
    if origin_host:
        return f"origin:{origin_host}"

    referer_host = _host_from_header_url(headers.get("referer", ""))
    if referer_host:
        return f"referer:{referer_host}"

    user_agent = (headers.get("user-agent") or "").strip()
    if user_agent:
        first_token = user_agent.split()[0].strip()
        product = first_token.split("/", 1)[0].strip().lower()
        if product and product != "mozilla":
            return f"ua:{_normalize_key_part(product)}"
        ua_hash = hashlib.sha256(user_agent.encode("utf-8")).hexdigest()[:16]
        return f"ua_hash:{ua_hash}"

    client_host = (http_request.client.host if http_request.client else "") or ""
    client_host = client_host.strip()
    if client_host:
        return f"ip:{client_host}"

    return ""


def _clone_cached_response(cached: ChatCompletionResponse) -> ChatCompletionResponse:
    """Return a fresh response object so ids/timestamps remain request-specific."""
    return _model_copy_compat(
        cached,
        deep=True,
        update={
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "created": int(time.time()),
        },
    )


def _prune_cache(now: float) -> None:
    """Prune expired entries and enforce size cap."""
    expired_keys = [key for key, (ts, _) in _response_cache.items() if now - ts > _CACHE_TTL_SECONDS]
    for key in expired_keys:
        _response_cache.pop(key, None)

    if len(_response_cache) <= _CACHE_MAX_ENTRIES:
        return

    ordered = sorted(_response_cache.items(), key=lambda item: item[1][0])
    overflow = len(_response_cache) - _CACHE_MAX_ENTRIES
    for key, _ in ordered[:overflow]:
        _response_cache.pop(key, None)


def _contract_hash(system_texts: list[str]) -> str:
    """Stable hash for thread-level system instruction contracts."""
    canonical = json.dumps(
        [_normalize_instruction_text(t) for t in system_texts if t.strip()],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _prune_thread_contracts(now: float) -> None:
    """Drop expired thread contract mappings."""
    expired = [tid for tid, (ts, _) in _thread_contracts.items() if now - ts > _CONTRACT_TTL_SECONDS]
    for tid in expired:
        _thread_contracts.pop(tid, None)
    expired_user = [tid for tid, (ts, _, _) in _thread_user_contracts.items() if now - ts > _CONTRACT_TTL_SECONDS]
    for tid in expired_user:
        _thread_user_contracts.pop(tid, None)
    expired_last = [tid for tid, (ts, _) in _thread_last_user_text.items() if now - ts > _CONTRACT_TTL_SECONDS]
    for tid in expired_last:
        _thread_last_user_text.pop(tid, None)


def _prune_app_threads(now: float) -> list[str]:
    """Drop expired app->thread mappings. Return owned thread ids eligible for deletion."""
    expired = [
        (app, mapping.thread_id, mapping.created_by_catgpt)
        for app, mapping in _app_threads.items()
        if now - mapping.last_used > _APP_THREAD_TTL_SECONDS
    ]
    for app, _, _ in expired:
        _app_threads.pop(app, None)
    # Deduplicate thread ids; one thread may be shared by multiple apps.
    seen: set[str] = set()
    expired_thread_ids: list[str] = []
    for _, tid, created_by_catgpt in expired:
        if created_by_catgpt and tid and tid not in seen:
            seen.add(tid)
            expired_thread_ids.append(tid)
    return expired_thread_ids


async def _maybe_delete_expired_app_threads(thread_ids: list[str]) -> None:
    """Best-effort deletion of expired app-tracked ChatGPT threads via the web UI.

    Acquires a cleanup tab (or the process lock when the pool is down) so
    deletion cannot race an in-flight request on the same page.
    """
    if not thread_ids or not Config.API_APP_THREAD_DELETE_EXPIRED:
        return
    try:
        client = _get_client()
    except Exception:
        return

    if not isinstance(client, ChatGPTClient):
        log.debug("App-thread deletion is only supported for ChatGPT provider")
        return

    async with acquire_browser_page(CLEANUP_SESSION) as lease:
        bound = _bind_client(client, lease.page)
        for tid in thread_ids:
            try:
                ok = await bound.delete_thread(tid)
                if ok:
                    log.info(f"Deleted expired app-tracked thread: {tid}")
                else:
                    log.warning(f"Could not delete expired app-tracked thread: {tid}")
            except Exception as e:
                log.warning(f"Failed to delete app-tracked thread {tid}: {e}")


def _prune_thread_titles(now: float) -> None:
    """Drop expired thread-title mappings."""
    expired = [tid for tid, (ts, _) in _thread_titles.items() if now - ts > _THREAD_TITLE_TTL_SECONDS]
    for tid in expired:
        _thread_titles.pop(tid, None)


def _display_app_name(app_key: str) -> str:
    """Return a human-friendly app name extracted from app routing key."""
    if not app_key:
        return "unknown"
    if app_key.startswith("user:"):
        return app_key.split(":", 1)[1]
    if app_key.startswith("hdr:"):
        parts = app_key.split(":", 2)
        if len(parts) == 3:
            return parts[2]
    if app_key.startswith("endpoint:"):
        return app_key.split(":", 1)[1]
    if ":" in app_key:
        return app_key.split(":", 1)[1]
    return app_key


async def _lookup_thread_title(client: ChatGPTClient, thread_id: str) -> str:
    """Best-effort lookup for a conversation title from sidebar threads."""
    if not thread_id:
        return ""

    now = time.time()
    async with _thread_title_lock:
        _prune_thread_titles(now)
        cached = _thread_titles.get(thread_id)
        if cached:
            return cached[1]

    try:
        threads = await client.list_threads()
    except Exception as e:
        log.debug(f"Thread title lookup skipped: {e}")
        return ""

    now = time.time()
    async with _thread_title_lock:
        _prune_thread_titles(now)
        for thread in threads:
            tid = (thread.get("id") or "").strip()
            title = (thread.get("title") or "").strip()
            if tid and title:
                _thread_titles[tid] = (now, title)
        matched = _thread_titles.get(thread_id)
        return matched[1] if matched else ""


def _build_contract_reminder_prompt(user_text: str, contract_id: str) -> str:
    """Compact prompt that reuses previously primed thread instructions."""
    short_id = contract_id[:12]
    return (
        f"[Contract {short_id}] Reuse the established instructions for this thread exactly.\n\n"
        f"{user_text}"
    )


def _build_user_contract_reminder_prompt(user_tail: str, contract_id: str, user_contract_id: str) -> str:
    """Compact prompt that reuses both system and repeated user-prefix instructions."""
    sys_id = contract_id[:12] if contract_id else "none"
    usr_id = user_contract_id[:12]
    return (
        f"[Contract {sys_id}/{usr_id}] Reuse the established instructions for this thread exactly. "
        f"Apply them to the new payload only.\n\n"
        f"{user_tail}"
    )


def _common_prefix_len(a: str, b: str) -> int:
    """Return the length of the common prefix between two strings."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _looks_like_instruction_prefix(prefix: str) -> bool:
    """
    Heuristic: detect instruction-heavy prefix text.

    Keeps optimization conservative so we avoid compressing arbitrary repeated prose.
    """
    normalized = _normalize_instruction_text(prefix)
    markers = (
        "[system instruction",
        "you must respond",
        "respond in json",
        "follow it strictly",
        "rules are",
        "json-schema",
        "$schema",
        "<text_content>",
    )
    return any(m in normalized for m in markers)


def _detect_user_prefix_contract(prev_text: str, curr_text: str) -> tuple[str, str] | None:
    """
    Detect a repeated leading user instruction block.

    Returns (prefix, tail) when confident; otherwise None.
    """
    if not prev_text or not curr_text:
        return None

    # First, try marker-aware split for instruction + payload formats.
    # This handles cases where payload body changes heavily while instructions remain fixed.
    for marker in ("<TEXT_CONTENT>", "<INPUT>", "INPUT:"):
        i_prev = prev_text.find(marker)
        i_curr = curr_text.find(marker)
        if i_prev < 0 or i_curr < 0:
            continue
        e_prev = prev_text.find("\n", i_prev)
        e_curr = curr_text.find("\n", i_curr)
        if e_prev < 0 or e_curr < 0:
            continue
        prefix_prev = prev_text[: e_prev + 1]
        prefix_curr = curr_text[: e_curr + 1]
        if prefix_prev != prefix_curr:
            continue
        if not _looks_like_instruction_prefix(prefix_prev):
            continue
        tail = curr_text[e_curr + 1 :].strip()
        if len(tail) >= 20:
            return prefix_prev, tail

    lcp_len = _common_prefix_len(prev_text, curr_text)
    if lcp_len < 400:
        return None

    min_len = min(len(prev_text), len(curr_text))
    if min_len <= 0:
        return None
    # Keep conservative ratio by default, but allow low-ratio cases when the
    # shared prefix itself is very large and instruction-like.
    ratio = lcp_len / min_len
    if ratio < 0.5:
        if lcp_len < 1200:
            return None
        if not _looks_like_instruction_prefix(prev_text[:lcp_len]):
            return None

    candidate = prev_text[:lcp_len]
    if "\n" in candidate:
        newline_idx = candidate.rfind("\n")
        if newline_idx >= 200:
            candidate = candidate[: newline_idx + 1]

    tail = curr_text[len(candidate) :].strip()
    if len(tail) < 20:
        return None
    if not _looks_like_instruction_prefix(candidate):
        return None

    return candidate, tail


def _user_contract_hash(system_texts: list[str], user_prefix: str) -> str:
    """Stable hash for combined system+user-prefix instruction contract."""
    canonical = json.dumps(
        {
            "system": [_normalize_instruction_text(t) for t in system_texts if t.strip()],
            "user_prefix": _normalize_instruction_text(user_prefix),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _extract_content_text(content) -> str:
    """Extract text from message content (handles both string and list format)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts) if parts else ""
    return str(content)


def _normalize_instruction_text(text: str) -> str:
    """Normalize instruction text for duplicate/equivalence checks."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _collect_system_texts(messages: list[ChatMessage]) -> list[str]:
    """Collect non-empty system message texts."""
    texts: list[str] = []
    for msg in messages:
        if msg.role != "system":
            continue
        text = _extract_content_text(msg.content).strip()
        if text:
            texts.append(text)
    return texts


def _dedupe_system_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Remove duplicate system messages while preserving order."""
    deduped: list[ChatMessage] = []
    seen: set[str] = set()

    for msg in messages:
        if msg.role != "system":
            deduped.append(msg)
            continue

        text = _extract_content_text(msg.content).strip()
        key = _normalize_instruction_text(text)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(msg)

    return deduped


def _looks_like_json_only_instruction(text: str) -> bool:
    """Heuristic detection for a generic JSON-only system instruction."""
    normalized = _normalize_instruction_text(text)
    return (
        "valid json only" in normalized
        and "markdown/code fences" in normalized
    )


def _has_equivalent_response_instruction(
    messages: list[ChatMessage], response_format_system: str
) -> bool:
    """Check if an equivalent structured-output instruction already exists."""
    target = _normalize_instruction_text(response_format_system)
    if not target:
        return False

    for text in _collect_system_texts(messages):
        normalized = _normalize_instruction_text(text)
        if normalized == target:
            return True

    # Generic json_object instruction can be considered equivalent even if phrasing differs.
    if "return exactly one json object" in target:
        return any(_looks_like_json_only_instruction(text) for text in _collect_system_texts(messages))

    return False


def _extract_image_urls(content) -> list[str]:
    """Extract image URLs from message content (OpenAI vision format)."""
    if not isinstance(content, list):
        return []
    urls = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "image_url":
            image_url = item.get("image_url", {})
            if isinstance(image_url, dict):
                url = image_url.get("url", "")
            else:
                url = str(image_url)
            if url:
                urls.append(url)
    return urls


def _extract_file_attachments(content) -> list[dict]:
    """
    Extract file attachments from message content.

    Supported content part format:
      {"type": "file", "file": {"filename": "test.pdf", "data": "base64...", "mime_type": "application/pdf"}}

    Also supports a shorthand data-URL style:
      {"type": "file", "file": {"filename": "test.pdf", "url": "data:application/pdf;base64,..."}}

    Returns list of dicts: [{"filename": str, "data_b64": str, "mime_type": str}, ...]
    """
    if not isinstance(content, list):
        return []
    files = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "file":
            continue
        file_info = item.get("file", {})
        if not isinstance(file_info, dict):
            continue
        filename = file_info.get("filename", "attachment")
        # Two ways to supply file data:
        # 1. data + mime_type  2. url (data-URL)
        data_b64 = file_info.get("data")
        mime_type = file_info.get("mime_type", "application/octet-stream")
        url = file_info.get("url", "")
        if not data_b64 and url.startswith("data:"):
            # Parse data URL
            try:
                header, data_b64 = url.split(",", 1)
                # header = "data:application/pdf;base64"
                if ":" in header and ";" in header:
                    mime_type = header.split(":")[1].split(";")[0]
            except ValueError:
                continue
        if data_b64:
            files.append({"filename": filename, "data_b64": data_b64, "mime_type": mime_type})
    return files


def _contains_attachment(content) -> bool:
    """Return whether OpenAI chat/responses content contains an attachment."""
    if isinstance(content, list):
        return any(_contains_attachment(item) for item in content)
    if not isinstance(content, dict):
        return False
    if content.get("type") in {"image_url", "file", "input_image", "input_file"}:
        return True
    return any(_contains_attachment(value) for value in content.values())


async def _download_file(url_or_data: str | dict, download_dir: str = "/tmp/catgpt_files") -> str | None:
    """
    Download / decode a file (image, PDF, etc.) from URL, base64 data URL,
    or a file attachment dict. Returns the local file path.
    """
    import base64
    import hashlib
    import os

    os.makedirs(download_dir, exist_ok=True)

    # -- Dict form (from _extract_file_attachments) --
    if isinstance(url_or_data, dict):
        try:
            filename = url_or_data.get("filename", "file")
            data_b64 = url_or_data["data_b64"]
            # Sanitize filename
            safe_name = re.sub(r"[^\w.\-]", "_", filename)
            hash_suffix = hashlib.md5(data_b64[:60].encode()).hexdigest()[:8]
            filepath = os.path.join(download_dir, f"{hash_suffix}_{safe_name}")
            with open(filepath, "wb") as f:
                f.write(base64.b64decode(data_b64))
            log.info(f"Decoded file attachment: {filepath}")
            return filepath
        except Exception as e:
            log.error(f"Failed to decode file attachment: {e}")
            return None

    # -- String forms --
    url = str(url_or_data)

    if url.startswith("data:"):
        # Base64 data URL: data:image/png;base64,iVBOR... or data:application/pdf;base64,...
        try:
            header, b64data = url.split(",", 1)
            # Detect extension from MIME type
            ext = "bin"
            mime = ""
            if ":" in header and ";" in header:
                mime = header.split(":")[1].split(";")[0]
            ext_map = {
                "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
                "image/gif": "gif", "image/tiff": "tiff", "application/pdf": "pdf",
                "text/plain": "txt", "text/csv": "csv",
                "application/json": "json",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
            }
            ext = ext_map.get(mime, mime.split("/")[-1] if "/" in mime else "bin")
            filename = f"file_{hashlib.md5(b64data[:100].encode()).hexdigest()[:12]}.{ext}"
            filepath = os.path.join(download_dir, filename)
            with open(filepath, "wb") as f:
                f.write(base64.b64decode(b64data))
            log.info(f"Decoded base64 file: {filepath}")
            return filepath
        except Exception as e:
            log.error(f"Failed to decode base64 data URL: {e}")
            return None
    elif url.startswith(("http://", "https://")):
        # HTTP URL - download it
        try:
            import urllib.request
            ext = "bin"
            for e in ["jpg", "jpeg", "webp", "gif", "png", "tif", "tiff", "pdf", "txt", "csv", "docx", "xlsx"]:
                if e in url.lower():
                    ext = e
                    break
            filename = f"file_{hashlib.md5(url.encode()).hexdigest()[:12]}.{ext}"
            filepath = os.path.join(download_dir, filename)
            urllib.request.urlretrieve(url, filepath)
            log.info(f"Downloaded file: {filepath}")
            return filepath
        except Exception as e:
            log.error(f"Failed to download file from {url}: {e}")
            return None
    elif os.path.isfile(url):
        # Local file path
        return url
    else:
        log.warning(f"Unknown file URL format: {url[:80]}")
        return None


def _build_prompt(messages: list[ChatMessage]) -> str:
    """
    Flatten an OpenAI-style message array into a single prompt string
    that we can paste into ChatGPT's input box.

    The browser already maintains conversation context within a thread,
    so for simple single-turn calls we just send the last user message.
    For multi-turn with system prompts or tool results, we build a
    formatted transcript.
    """
    # Simple case: only one user message (and optionally one system message)
    non_system = [m for m in messages if m.role != "system"]
    system_msgs = [m for m in messages if m.role == "system"]

    # If it's just one user message, send it directly
    if len(non_system) == 1 and non_system[0].role == "user":
        prefix = ""
        if system_msgs:
            sys_texts = []
            for msg in system_msgs:
                text = _extract_content_text(msg.content).strip()
                if text:
                    sys_texts.append(text)

            if len(sys_texts) == 1:
                prefix = f"[System instruction: {sys_texts[0]}]\n\n"
            elif sys_texts:
                combined = "\n".join(f"{idx + 1}. {text}" for idx, text in enumerate(sys_texts))
                prefix = f"[System instructions]\n{combined}\n\n"
        user_text = _extract_content_text(non_system[0].content)
        return prefix + (user_text or "")

    # Multi-turn: build a transcript
    parts: list[str] = []
    for msg in messages:
        role = msg.role.capitalize()
        if msg.role == "tool":
            # Tool result - include the tool_call_id for context
            parts.append(f"[Tool result for {msg.tool_call_id or 'unknown'}]: {_extract_content_text(msg.content)}")
        elif msg.role == "assistant" and msg.tool_calls:
            # Assistant requested tool calls - show what was called
            calls_desc = []
            for tc in msg.tool_calls:
                calls_desc.append(
                    f'{tc.function.name}({tc.function.arguments})'
                )
            parts.append(f"Assistant called tools: {', '.join(calls_desc)}")
        elif msg.content:
            text = _extract_content_text(msg.content)
            if text:
                parts.append(f"{role}: {text}")

    return "\n\n".join(parts)


def _build_tool_system_prompt(tools: list[ToolDefinition]) -> str:
    """
    Build a system-level instruction that tells ChatGPT about available tools.

    When the model decides to call a tool, it should respond with a specific
    JSON format that we can parse.
    """
    tool_descriptions = []
    for tool in tools:
        fn = tool.function
        desc = {
            "name": fn.name,
            "description": fn.description,
            "parameters": fn.parameters,
        }
        tool_descriptions.append(json.dumps(desc, indent=2))

    tools_json = "\n---\n".join(tool_descriptions)

    return f"""You are in TOOL MODE. Ignore prior statements about tool availability.

If the user's latest request should call one or more functions, output ONLY:
{{"tool_calls":[{{"name":"<function_name>","arguments":{{...}}}}]}}

Available functions:
{tools_json}

Rules:
- Use exact function names from the list.
- Arguments must be a valid JSON object.
- Return multiple calls when needed.
- If no function applies, answer normally.
"""


def _parse_tool_calls(
    response_text: str, tools: list[ToolDefinition]
) -> list[ToolCall] | None:
    """
    Try to parse tool calls from the model's response text.

    Looks for a JSON block containing {"tool_calls": [...]}.
    Returns None if no tool calls are found.
    """
    # Try to find JSON in code blocks first
    code_block_match = re.search(
        r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", response_text
    )

    json_str = None
    if code_block_match:
        json_str = code_block_match.group(1)
    else:
        # Try to find raw JSON with tool_calls key
        raw_match = re.search(
            r'(\{\s*"tool_calls"\s*:\s*\[[\s\S]*?\]\s*\})', response_text
        )
        if raw_match:
            json_str = raw_match.group(1)

    if not json_str:
        return None

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        log.debug(f"Failed to parse tool call JSON: {json_str[:200]}")
        return None

    if "tool_calls" not in parsed or not isinstance(parsed["tool_calls"], list):
        return None

    # Validate that the called functions are in the provided tools
    valid_names = {t.function.name for t in tools}
    result: list[ToolCall] = []

    for call in parsed["tool_calls"]:
        name = call.get("name", "")
        if name not in valid_names:
            log.warning(f"Model called unknown tool: {name}")
            continue

        arguments = call.get("arguments", {})
        if isinstance(arguments, dict):
            arguments_str = json.dumps(arguments)
        else:
            arguments_str = str(arguments)

        result.append(
            ToolCall(
                id=f"call_{uuid.uuid4().hex[:24]}",
                type="function",
                function=FunctionCallInfo(name=name, arguments=arguments_str),
            )
        )

    return result if result else None


def _build_response_format_system_prompt(response_format: Any) -> str | None:
    """Build a strict JSON-output instruction from OpenAI response_format."""
    if not response_format:
        return None

    if isinstance(response_format, str):
        if response_format == "json_object":
            return (
                "You must respond with valid JSON only. "
                "Return exactly one JSON object and no markdown/code fences."
            )
        return None

    if not isinstance(response_format, dict):
        return None

    rf_type = response_format.get("type")
    if rf_type == "json_object":
        return (
            "You must respond with valid JSON only. "
            "Return exactly one JSON object and no markdown/code fences."
        )

    if rf_type == "json_schema":
        schema_obj = response_format.get("json_schema", {})
        schema = schema_obj.get("schema") if isinstance(schema_obj, dict) else None
        strict = bool(schema_obj.get("strict")) if isinstance(schema_obj, dict) else False
        if schema:
            strict_text = " Follow it strictly." if strict else ""
            return (
                "You must respond with valid JSON only (no markdown/code fences). "
                "The JSON must satisfy this schema:" +
                f"\n{json.dumps(schema, ensure_ascii=False)}" +
                strict_text
            )
        return (
            "You must respond with valid JSON only. "
            "Return exactly one JSON object and no markdown/code fences."
        )

    return None


def _page_extraction_mode(request: ChatCompletionRequest) -> str:
    """Normalize the requested page-extraction mode string."""
    options = getattr(request, "page_extraction", None)
    mode = getattr(options, "mode", "") if options is not None else ""
    return str(mode or "").strip().lower()


def _build_page_extraction_response_format(
    page_descriptors: list[AttachmentPageDescriptor],
) -> dict[str, Any]:
    """Build a strict JSON schema for page-by-page extraction output."""
    if not page_descriptors:
        raise ValueError("page_descriptors cannot be empty")

    return {
        "type": "json_schema",
        "json_schema": {
            "name": "page_extraction",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "pages": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "page_index": {"type": "integer"},
                                "source_name": {"type": "string"},
                                "page_number": {"type": "integer"},
                                "text": {"type": "string"},
                            },
                            "required": ["page_index", "source_name", "page_number", "text"],
                        },
                    }
                },
                "required": ["pages"],
            },
        },
    }


def _build_page_extraction_note(page_descriptors: list[AttachmentPageDescriptor]) -> str:
    """Build a compact prompt prefix describing the required page-map output."""
    if not page_descriptors:
        return ""

    lines = [
        "[Per-page extraction]",
        f"- Return valid JSON only with a top-level `pages` array containing exactly {len(page_descriptors)} item(s).",
        "- Preserve each `page_index`, `source_name`, and `page_number` exactly as listed below.",
        "- Fill `text` with the extracted text for that page in reading order.",
        "- If a page is blank or unreadable, keep the item and return an empty string for `text`.",
        "- Keep the `pages` array in the same order as the numbered page map below.",
        "- If an original document is also attached as a fallback, use it only to help read the listed page map. Do not add extra pages.",
    ]
    for descriptor in page_descriptors:
        lines.append(
            f"- page_index={descriptor.page_index}: '{descriptor.source_name}' page {descriptor.page_number}."
        )
    return "\n".join(lines) + "\n\n"


def _extract_json_payload(text: str) -> Any | None:
    """Extract and parse a JSON object/array from model text."""
    if not text:
        return None

    stripped = text.strip()

    try:
        return json.loads(stripped)
    except Exception:
        pass

    block = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped)
    if block:
        candidate = block.group(1).strip()
        try:
            return json.loads(candidate)
        except Exception:
            pass

    first_obj = stripped.find("{")
    first_arr = stripped.find("[")
    candidates = [i for i in (first_obj, first_arr) if i >= 0]
    if not candidates:
        return None

    start = min(candidates)
    try:
        return json.loads(stripped[start:])
    except Exception:
        return None


def _coerce_payload_to_schema(payload: Any, schema: dict[str, Any]) -> Any:
    """Best-effort payload coercion guided by a JSON schema."""
    schema_type = schema.get("type")

    if schema_type == "object":
        if isinstance(payload, dict):
            return payload

        properties = schema.get("properties", {})
        if not isinstance(properties, dict) or not properties:
            return payload

        if isinstance(payload, list):
            array_keys = [
                key for key, prop in properties.items()
                if isinstance(prop, dict) and prop.get("type") == "array"
            ]
            if len(array_keys) == 1:
                return {array_keys[0]: payload}

            if len(properties) == 1:
                key = next(iter(properties))
                return {key: payload}

            required = schema.get("required", [])
            if isinstance(required, list):
                for key in required:
                    if key in properties:
                        return {key: payload}

        if len(properties) == 1:
            key = next(iter(properties))
            return {key: payload}

        return payload

    if schema_type == "array":
        if isinstance(payload, list):
            return payload

        if isinstance(payload, dict) and len(payload) == 1:
            only_value = next(iter(payload.values()))
            if isinstance(only_value, list):
                return only_value

        return [payload]

    return payload


def _coerce_to_response_schema(payload: Any, response_format: Any) -> Any:
    """Coerce common payload mismatches into the requested response format."""
    if not isinstance(response_format, dict):
        return payload

    rf_type = response_format.get("type")
    if rf_type == "json_object":
        if isinstance(payload, dict):
            return payload
        return {"data": payload}

    if rf_type != "json_schema":
        return payload

    json_schema = response_format.get("json_schema", {})
    if not isinstance(json_schema, dict):
        return payload

    schema = json_schema.get("schema")
    if not isinstance(schema, dict):
        return payload

    return _coerce_payload_to_schema(payload, schema)


def _is_effectively_empty_value(value: Any) -> bool:
    """Return True when a value is effectively empty for header-row detection."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _pick_note_field_name(item: dict[str, Any]) -> str | None:
    """Pick the best note/context field key in an item dict."""
    preferred = ("note", "notes", "context", "header", "section", "group")
    for key in preferred:
        if key in item:
            return key
    return None


def _header_text_from_row(item: Any) -> str | None:
    """
    Detect a header-only row, e.g. {"food": null, "quantity": null, "note": "TO SERVE"}.
    Returns header text if matched, else None.
    """
    if not isinstance(item, dict) or not item:
        return None

    note_key = _pick_note_field_name(item)
    if not note_key:
        return None

    note_val = item.get(note_key)
    if not isinstance(note_val, str) or not note_val.strip():
        return None

    for key, value in item.items():
        if key == note_key:
            continue
        if not _is_effectively_empty_value(value):
            return None

    return note_val.strip()


def _append_note(item: dict[str, Any], text: str) -> None:
    """Append note/context text to an item."""
    key = _pick_note_field_name(item) or "note"
    existing = item.get(key)
    if isinstance(existing, str) and existing.strip():
        item[key] = f"{text}; {existing.strip()}"
    else:
        item[key] = text


def _merge_header_rows_in_array(items: list[Any]) -> list[Any]:
    """Merge header-only rows into the next real row in an array."""
    merged: list[Any] = []
    pending_header: str | None = None

    for raw in items:
        header_text = _header_text_from_row(raw)
        if header_text:
            pending_header = f"{pending_header}; {header_text}" if pending_header else header_text
            continue

        if isinstance(raw, dict) and pending_header:
            item = dict(raw)
            _append_note(item, pending_header)
            merged.append(item)
            pending_header = None
            continue

        merged.append(raw)

    if pending_header:
        # No real row after header; preserve as standalone note row.
        merged.append({"note": pending_header})

    return merged


def _merge_header_rows(payload: Any) -> Any:
    """
    Merge header-only rows into next row for structured payloads.

    Supports:
    - root array of objects
    - root object with exactly one array field
    """
    if isinstance(payload, list):
        return _merge_header_rows_in_array(payload)

    if isinstance(payload, dict):
        array_keys = [k for k, v in payload.items() if isinstance(v, list)]
        if len(array_keys) == 1:
            key = array_keys[0]
            out = dict(payload)
            out[key] = _merge_header_rows_in_array(out[key])
            return out

    return payload


def _normalize_structured_content(response_text: str, response_format: Any) -> str:
    """Best-effort normalization to JSON string for structured output calls."""
    payload = _extract_json_payload(response_text)
    if payload is None:
        return response_text

    payload = _coerce_to_response_schema(payload, response_format)
    if Config.API_HEADER_ROW_MERGE_MODE:
        payload = _merge_header_rows(payload)
    return json.dumps(payload, ensure_ascii=False)


def _latest_user_text(messages: list[ChatMessage]) -> str:
    """Return the latest user text content from request messages."""
    for msg in reversed(messages):
        if msg.role == "user":
            return _extract_content_text(msg.content).strip()
    return ""


def _infer_expected_item_count(messages: list[ChatMessage]) -> int | None:
    """
    Infer expected item count from the latest user text.

    Used as a best-effort guard for structured extraction tasks where output
    cardinality should match input rows.

    Strategy:
    1) If user content is JSON, infer from its primary array cardinality.
    2) Otherwise, fallback to line counting only when input appears to be
       a compact line-item list (not an instruction-heavy prompt).
    """
    text = _latest_user_text(messages)
    if not text:
        return None

    # Preferred: JSON payloads (e.g., [...], {"items":[...]}, {"ingredients":[...]}).
    payload = _extract_json_payload(text)
    if payload is not None:
        json_count = _infer_primary_array_count(payload)
        if json_count is not None and json_count >= 1:
            return json_count

    # Heuristic fallback: only for line-item style prompts.
    if not _should_use_line_cardinality_fallback(text):
        return None

    count = 0
    for line in text.splitlines():
        cleaned = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
        if cleaned:
            count += 1

    return count if count >= 2 else None


def _should_use_line_cardinality_fallback(text: str) -> bool:
    """
    Decide whether plain-text line-count cardinality fallback is safe.

    Avoids instruction-heavy prompts (schemas, long docs, embedded templates)
    where line count does not represent expected output cardinality.
    """
    lower = (text or "").lower()
    if not lower:
        return False

    # Strong signals this is an instruction/template payload, not line items.
    instruction_markers = (
        "[system instruction",
        "[system instructions",
        "you must respond with valid json only",
        "$schema",
        "json-schema.org",
        "<text_content>",
        "table of contents",
        "project structure",
        "architecture",
    )
    if any(marker in lower for marker in instruction_markers):
        return False

    lines: list[str] = []
    short_lines = 0
    for line in text.splitlines():
        cleaned = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
        if not cleaned:
            continue
        lines.append(cleaned)
        if len(cleaned) <= 120:
            short_lines += 1

    if len(lines) < 2:
        return False

    # Very large/verbose payloads are likely instructions or article content.
    if len(text) > 2000 or len(lines) > 40:
        return False

    # Require mostly short item-like lines.
    return (short_lines / len(lines)) >= 0.8


def _infer_primary_array_count(payload: Any) -> int | None:
    """Infer the cardinality of the primary array in a structured payload."""
    if isinstance(payload, list):
        return len(payload)

    if isinstance(payload, dict):
        array_values = [v for v in payload.values() if isinstance(v, list)]
        if len(array_values) == 1:
            return len(array_values[0])

    return None


def _structured_cardinality_mismatch(
    messages: list[ChatMessage],
    response_text: str,
    expected_count: int | None = None,
) -> tuple[int, int] | None:
    """Return (expected, actual) if a clear structured cardinality mismatch exists."""
    expected = expected_count if expected_count is not None else _infer_expected_item_count(messages)
    if expected is None:
        return None

    payload = _extract_json_payload(response_text)
    if payload is None:
        return None

    actual = _infer_primary_array_count(payload)
    if actual is None:
        return None

    if expected != actual:
        return expected, actual
    return None


def _build_cardinality_retry_prompt(
    base_prompt: str,
    expected: int,
    actual: int,
    expectation_reason: str = "input line count",
    correction_rule: str | None = None,
) -> str:
    """Build a corrective retry prompt for structured cardinality mismatch."""
    if not correction_rule:
        correction_rule = (
            "Return valid JSON only, with exactly one output item per non-empty input line, "
            "preserving input order. Do not merge or drop lines."
        )
    return (
        f"{base_prompt}\n\n"
        "Correction: The previous JSON had the wrong number of items.\n"
        f"Expected output count is {expected} based on {expectation_reason}, but output count was {actual}.\n"
        f"{correction_rule}"
    )


def _validate_chat_request(request: ChatCompletionRequest) -> None:
    """Shared validation for chat completion request payloads."""
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages array cannot be empty")

    page_extraction_mode = _page_extraction_mode(request)
    if page_extraction_mode and page_extraction_mode != "structured":
        raise HTTPException(
            status_code=400,
            detail="Unsupported page_extraction.mode. Supported modes: structured",
        )

    if page_extraction_mode and request.response_format:
        raise HTTPException(
            status_code=400,
            detail="page_extraction.mode='structured' manages response_format automatically. Omit response_format.",
        )

    if Config.PROVIDER == "minimax" and any(
        _contains_attachment(message.content) for message in request.messages
    ):
        raise HTTPException(
            status_code=501,
            detail="Attachments are not supported by the MiniMax provider.",
        )

    _resolve_model_id(request.model)


def _resolve_model_id(requested: str | None) -> str:
    """Resolve and validate a model ID for the active provider."""
    if Config.PROVIDER == "minimax":
        try:
            return Config.resolve_model_id(requested)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not is_supported_chat_model(requested):
        supported = ", ".join(list_public_chat_models())
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported model '{requested}'. Supported models: {supported}",
        )
    return requested or Config.default_model_id()


def _resolve_app_key(
    request: ChatCompletionRequest,
    http_request: Request | None,
    endpoint_app_name: str = "",
) -> str:
    """Resolve app key when app-thread mode is enabled."""
    if not Config.API_APP_THREAD_MODE:
        return ""
    if http_request is None:
        return (endpoint_app_name or "").strip()
    return _derive_app_key(request, http_request, endpoint_app_name=endpoint_app_name)


def _resolve_image_app_key(
    request: ImageGenerationRequest,
    http_request: Request,
    endpoint_app_name: str = "",
) -> str:
    """Resolve app key for image generation when app-thread mode is enabled."""
    if not Config.API_APP_THREAD_MODE:
        return ""
    return _derive_app_key(request, http_request, endpoint_app_name=endpoint_app_name)  # type: ignore[arg-type]


async def _execute_image_generation(
    request: ImageGenerationRequest,
    app_key_override: str = "",
    http_request: Request | None = None,
) -> ImagesResponse:
    """Shared executor for generic and app-scoped image generation."""
    import base64

    if not request.prompt or not request.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt cannot be empty")

    response_format = (request.response_format or "b64_json").strip()
    if response_format not in {"b64_json", "url"}:
        raise HTTPException(status_code=400, detail="response_format must be 'b64_json' or 'url'")

    client = _get_client()
    if not hasattr(client, "generate_image"):
        raise HTTPException(status_code=422, detail="Image generation is only supported by the ChatGPT provider")

    _deletion_pending: list[str] = []
    app_key = (app_key_override or "").strip()
    session_key = _tab_session_key(request, http_request, app_key)

    try:
        async with acquire_browser_page(session_key) as lease:
            client = _bind_client(client, lease.page)
            start_time = time.time()
            app_name = _display_app_name(app_key)
            app_thread_created_by_catgpt = False

            if Config.API_APP_THREAD_MODE and app_key:
                log.info("OpenAI image app-thread key: %s", app_key)
                now_app = time.time()
                mapped_thread = ""
                expired_tids: list[str] = []
                async with _app_thread_lock:
                    expired_tids = _prune_app_threads(now_app)
                    mapped = _app_threads.get(app_key)
                    if mapped:
                        mapped_thread = mapped.thread_id
                        app_thread_created_by_catgpt = mapped.created_by_catgpt
                if expired_tids:
                    _deletion_pending.extend(expired_tids)
                if mapped_thread:
                    current_tid = client._extract_thread_id()
                    if current_tid != mapped_thread:
                        log.info(f"OpenAI image app-thread mode: app='{app_key}' -> thread {mapped_thread}")
                        await client.navigate_to_thread(mapped_thread)
                else:
                    log.info(
                        "OpenAI image app-thread mode: app='%s' has no mapped thread. New chat will be created.",
                        app_name,
                    )
                    await client.new_chat()
                    app_thread_created_by_catgpt = True
            elif Config.uses_browser() and not (session_key and not lease.is_first_turn):
                new_chat = getattr(client, "new_chat", None)
                if callable(new_chat):
                    await new_chat()

            log.info(
                "POST /v1/images/generations - model=%s, prompt=%r, n=%s, size=%s, response_format=%s",
                request.model,
                request.prompt[:80],
                request.n,
                request.size,
                response_format,
            )

            try:
                result = await client.generate_image(
                    request.prompt,
                    n=int(request.n or 1),
                    size=request.size or "1024x1024",
                    quality=request.quality or "standard",
                    style=request.style or "vivid",
                )
            except Exception as e:
                log.error(f"ChatGPT error during image generation: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"ChatGPT error: {str(e)}")

            elapsed_ms = int((time.time() - start_time) * 1000)

            if Config.API_APP_THREAD_MODE and app_key:
                thread_for_app = result.thread_id or client._extract_thread_id()
                if thread_for_app:
                    post_prune_expired: list[str] = []
                    async with _app_thread_lock:
                        post_prune_expired = _prune_app_threads(time.time())
                        _app_threads[app_key] = _AppThreadMapping(
                            time.time(),
                            thread_for_app,
                            created_by_catgpt=app_thread_created_by_catgpt,
                        )
                    if post_prune_expired:
                        _deletion_pending.extend(post_prune_expired)
                    log.info("Image app-thread mapping updated: app=%s -> thread=%s", app_name, thread_for_app)

            if not result.images:
                log.warning(
                    f"No images detected in response ({elapsed_ms}ms). "
                    f"ChatGPT replied: {result.message[:200]}"
                )
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "ChatGPT did not generate an image. "
                        f"Model response: {result.message[:500]}"
                    ),
                )

            image_data_list: list[ImageData] = []
            for img_info in result.images:
                revised_prompt = img_info.prompt_title or img_info.alt or request.prompt

                if response_format == "b64_json":
                    if img_info.local_path:
                        try:
                            with open(img_info.local_path, "rb") as f:
                                img_bytes = f.read()
                            b64 = base64.b64encode(img_bytes).decode("utf-8")
                            image_data_list.append(
                                ImageData(
                                    b64_json=b64,
                                    revised_prompt=revised_prompt,
                                )
                            )
                        except Exception as e:
                            log.error(f"Failed to read image file {img_info.local_path}: {e}")
                    else:
                        log.warning(f"Image has no local_path: {img_info.url[:80]}")
                else:
                    image_data_list.append(
                        ImageData(
                            url=img_info.local_path or img_info.url,
                            revised_prompt=revised_prompt,
                        )
                    )

            if not image_data_list:
                raise HTTPException(
                    status_code=500,
                    detail="Images were detected but could not be processed.",
                )

            log.info(
                f"Image generation complete: {len(image_data_list)} image(s), "
                f"{elapsed_ms}ms, format={response_format}"
            )

            return ImagesResponse(data=image_data_list)
    finally:
        if _deletion_pending:
            asyncio.create_task(_maybe_delete_expired_app_threads(_deletion_pending))


# -- Routes ------------------------------------------------------


@openai_router.get("/v1/models", response_model=ModelListResponse)
async def list_models() -> ModelListResponse:
    """List model IDs exposed by the active provider."""
    if Config.PROVIDER == "minimax":
        return ModelListResponse(
            data=[
                ModelObject(id=model_id, owned_by=Config.provider_owner())
                for model_id in Config.provider_model_ids()
            ]
        )
    return ModelListResponse(
        data=[ModelObject(id=model_id, owned_by="catgpt") for model_id in list_public_chat_models()]
    )


@openai_router.get("/{app_name}/v1/models", response_model=ModelListResponse)
async def list_models_scoped(app_name: str) -> ModelListResponse:
    """App-scoped alias for model listing."""
    _ = app_name
    return await list_models()


@openai_router.post("/v1/images/generations", response_model=ImagesResponse)
async def create_image(
    request: ImageGenerationRequest,
    http_request: Request,
) -> ImagesResponse:
    """OpenAI-compatible image generation endpoint."""
    app_key = _resolve_image_app_key(request, http_request)
    return await _execute_image_generation(
        request,
        app_key_override=app_key,
        http_request=http_request,
    )


@openai_router.post("/{app_name}/v1/images/generations", response_model=ImagesResponse)
async def create_image_scoped(
    app_name: str,
    request: ImageGenerationRequest,
    http_request: Request,
) -> ImagesResponse:
    """App-scoped alias for image generation."""
    app_key = _resolve_image_app_key(request, http_request, endpoint_app_name=app_name)
    return await _execute_image_generation(
        request,
        app_key_override=app_key,
        http_request=http_request,
    )


@openai_router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def create_chat_completion(
    request: ChatCompletionRequest,
    http_request: Request = None,
) -> ChatCompletionResponse:
    """
    OpenAI-compatible chat completions endpoint.

    Converts the message array into a single prompt, sends it to ChatGPT
    via browser automation, and returns an OpenAI-formatted response.
    Supports tool/function calling via prompt injection.
    """
    _validate_chat_request(request)
    app_key = _resolve_app_key(request, http_request)
    if request.stream:
        return await _stream_chat_completion(
            request,
            app_key_override=app_key,
            http_request=http_request,
        )
    return await _execute_chat_completion(
        request,
        app_key_override=app_key,
        http_request=http_request,
    )




@openai_router.post("/v1/responses", response_model=ResponsesResponse)
async def create_responses(
    request: ResponsesRequest,
    http_request: Request,
) -> ResponsesResponse:
    """OpenAI Responses API endpoint.

    Translates the request to a chat completion, executes it, and returns
    a Responses API format response. Reuses existing browser automation flow.
    """
    _validate_responses_request(request)
    app_key = _resolve_app_key(request, http_request)
    if request.stream:
        return await _stream_responses(
            request,
            app_key_override=app_key,
            http_request=http_request,
        )
    return await _execute_responses(
        request,
        app_key_override=app_key,
        http_request=http_request,
    )


@openai_router.post("/{app_name}/v1/responses", response_model=ResponsesResponse)
async def create_responses_scoped(
    app_name: str,
    request: ResponsesRequest,
    http_request: Request,
) -> ResponsesResponse:
    """App-scoped alias for Responses API (maps app name from URL path)."""
    _validate_responses_request(request)
    app_key = _resolve_app_key(request, http_request, endpoint_app_name=app_name)
    if request.stream:
        return await _stream_responses(
            request,
            app_key_override=app_key,
            http_request=http_request,
        )
    return await _execute_responses(
        request,
        app_key_override=app_key,
        http_request=http_request,
    )

@openai_router.post("/{app_name}/v1/chat/completions", response_model=ChatCompletionResponse)
async def create_chat_completion_scoped(
    app_name: str,
    request: ChatCompletionRequest,
    http_request: Request,
) -> ChatCompletionResponse:
    """App-scoped alias for chat completions (maps app name from URL path)."""
    _validate_chat_request(request)
    app_key = _resolve_app_key(request, http_request, endpoint_app_name=app_name)
    if request.stream:
        return await _stream_chat_completion(
            request,
            app_key_override=app_key,
            http_request=http_request,
        )
    return await _execute_chat_completion(
        request,
        app_key_override=app_key,
        http_request=http_request,
    )


def _anthropic_content_to_text(content: Any) -> str:
    """Flatten Anthropic message content blocks to a prompt string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return str(content)


def _anthropic_messages_to_chat_request(body: dict[str, Any]) -> ChatCompletionRequest:
    """Translate an Anthropic Messages body into a chat completion request."""
    messages: list[ChatMessage] = []
    system = body.get("system")
    if system:
        if isinstance(system, list):
            system_text = _anthropic_content_to_text(system)
        else:
            system_text = str(system)
        if system_text.strip():
            messages.append(ChatMessage(role="system", content=system_text))

    for item in body.get("messages") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        if role not in {"user", "assistant"}:
            role = "user"
        messages.append(ChatMessage(role=role, content=_anthropic_content_to_text(item.get("content"))))

    if not messages:
        raise HTTPException(status_code=400, detail="messages array cannot be empty")

    return ChatCompletionRequest(
        model=str(body.get("model") or Config.default_model_id()),
        messages=messages,
        max_tokens=body.get("max_tokens"),
        temperature=body.get("temperature"),
        top_p=body.get("top_p"),
        stream=bool(body.get("stream")),
        user=(body.get("user") or body.get("session_id") or None),
        thread_id=body.get("thread_id"),
    )


def _anthropic_message_payload(
    chat_response: ChatCompletionResponse,
    model_id: str,
) -> dict[str, Any]:
    text = chat_response.choices[0].message.content or ""
    prompt_tokens = chat_response.usage.prompt_tokens if chat_response.usage else 1
    completion_tokens = chat_response.usage.completion_tokens if chat_response.usage else _estimate_tokens(text)
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model_id,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
        },
    }


@openai_router.post("/v1/messages")
async def create_anthropic_message(http_request: Request):
    """Anthropic Messages API adapter for Claude Code CLI and similar clients."""
    try:
        body = await http_request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Request body must be JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    chat_request = _anthropic_messages_to_chat_request(body)
    _validate_chat_request(chat_request)
    app_key = _resolve_app_key(chat_request, http_request)
    chat_response = await _execute_chat_completion(
        chat_request,
        app_key_override=app_key,
        http_request=http_request,
    )
    payload = _anthropic_message_payload(chat_response, chat_response.model)

    if not chat_request.stream:
        return payload

    text = payload["content"][0]["text"]

    async def _events():
        yield (
            "event: message_start\n"
            f"data: {json.dumps({'type': 'message_start', 'message': {**payload, 'content': [], 'stop_reason': None}}, ensure_ascii=False)}\n\n"
        )
        yield (
            "event: content_block_start\n"
            f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}, ensure_ascii=False)}\n\n"
        )
        if text:
            yield (
                "event: content_block_delta\n"
                f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}}, ensure_ascii=False)}\n\n"
            )
        yield (
            "event: content_block_stop\n"
            f"data: {json.dumps({'type': 'content_block_stop', 'index': 0}, ensure_ascii=False)}\n\n"
        )
        yield (
            "event: message_delta\n"
            f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': payload['usage']['output_tokens']}}, ensure_ascii=False)}\n\n"
        )
        yield (
            "event: message_stop\n"
            f"data: {json.dumps({'type': 'message_stop'}, ensure_ascii=False)}\n\n"
        )

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# -- Responses API translation helpers --------------------------


def _responses_input_to_messages(
    input_data: str | list,
    instructions: str | None = None,
) -> list:
    """Convert Responses API input to ChatCompletionRequest messages list.

    Supports both string and list-of-input-item formats.
    Instructions field is prepended as a system message when present.
    """
    from src.api.openai_schemas import ChatMessage

    messages = []

    if instructions:
        messages.append(ChatMessage(role="system", content=instructions))

    def _normalize_content(content: Any):
        if isinstance(content, list):
            normalized_parts: list[dict[str, Any]] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type in {"input_text", "text"}:
                    normalized_parts.append({"type": "text", "text": part.get("text", "")})
                elif part_type in {"input_image", "image"}:
                    image_url = part.get("image_url") or part.get("url")
                    image_b64 = part.get("image_base64") or part.get("b64_json")
                    if image_b64 and not image_url:
                        image_url = f"data:image/png;base64,{image_b64}"
                    if image_url:
                        normalized_parts.append({"type": "image_url", "image_url": {"url": image_url}})
            return normalized_parts if normalized_parts else content
        return content

    if isinstance(input_data, str):
        messages.append(ChatMessage(role="user", content=input_data))
    elif isinstance(input_data, list):
        for item in input_data:
            if isinstance(item, dict):
                role = item.get("role") or "user"
                content = _normalize_content(item.get("content"))
            else:
                role = getattr(item, "role", "user") or "user"
                content = _normalize_content(getattr(item, "content", None))
            messages.append(ChatMessage(role=role, content=content))
    else:
        messages.append(ChatMessage(role="user", content=str(input_data)))

    return messages


def _responses_request_to_chat_request(resp_req: ResponsesRequest) -> ChatCompletionRequest:
    """Translate a ResponsesRequest into a ChatCompletionRequest for execution."""
    messages = _responses_input_to_messages(resp_req.input, resp_req.instructions)

    return ChatCompletionRequest(
        model=resp_req.model,
        messages=messages,
        tools=resp_req.tools,
        tool_choice=resp_req.tool_choice,
        temperature=resp_req.temperature,
        max_tokens=resp_req.max_output_tokens,
        top_p=resp_req.top_p,
        stream=resp_req.stream if resp_req.stream is not None else False,
        user=resp_req.user,
        read_aloud=bool(resp_req.read_aloud),
    )


def _responses_response_from_chat(
    chat_response: ChatCompletionResponse,
    model: str,
) -> ResponsesResponse:
    """Translate a ChatCompletionResponse into a Responses API response envelope.

    Converts choices[0].message.content into response output items.
    """
    output_items: list[ResponseOutputMessage | ResponseOutputToolCall] = []

    for choice in chat_response.choices:
        msg_text = choice.message.content or ""
        output_items.append(
            ResponseOutputMessage(
                content=[
                    ResponseOutputText(text=msg_text),
                ],
            )
        )
        tool_calls = choice.message.tool_calls or []
        for call in tool_calls:
            output_items.append(
                ResponseOutputToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=call.function.arguments,
                )
            )

    return ResponsesResponse(
        id=f"resp_{chat_response.id.split('-', 1)[-1]}" if "-" in chat_response.id else chat_response.id,
        model=model,
        output=output_items,
        usage=ResponsesUsageInfo(
            input_tokens=chat_response.usage.prompt_tokens,
            output_tokens=chat_response.usage.completion_tokens,
            total_tokens=chat_response.usage.total_tokens,
        ),
    )


def _responses_sse_event(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


def _chat_completion_sse_chunk(
    response: ChatCompletionResponse,
    delta: dict[str, Any],
    *,
    finish_reason: str | None = None,
) -> str:
    """Format one OpenAI chat.completion.chunk SSE data line."""
    chunk = {
        "id": response.id,
        "object": "chat.completion.chunk",
        "created": response.created,
        "model": response.model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    payload = json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))
    return f"data: {payload}\n\n"


async def _stream_chat_completion(
    request: ChatCompletionRequest,
    app_key_override: str = "",
    http_request: Request | None = None,
) -> StreamingResponse:
    """Return a Chat Completions SSE stream after the browser response completes.

    Browser automation cannot provide token deltas, but IDE clients (OpenCode,
    Cline, etc.) send stream=true and require text/event-stream. Emit the full
    assistant message as one or two chunks plus a terminal finish chunk.
    """

    async def _events():
        non_stream_request = request.model_copy(update={"stream": False})
        response = await _execute_chat_completion(
            non_stream_request,
            app_key_override=app_key_override,
            http_request=http_request,
        )
        choice = response.choices[0]
        message = choice.message

        yield _chat_completion_sse_chunk(
            response,
            {"role": "assistant", "content": ""},
        )

        if message.content:
            yield _chat_completion_sse_chunk(response, {"content": message.content})

        if message.tool_calls:
            tool_deltas: list[dict[str, Any]] = []
            for index, call in enumerate(message.tool_calls):
                tool_deltas.append(
                    {
                        "index": index,
                        "id": call.id,
                        "type": call.type,
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                )
            yield _chat_completion_sse_chunk(response, {"tool_calls": tool_deltas})

        yield _chat_completion_sse_chunk(
            response,
            {},
            finish_reason=choice.finish_reason,
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_responses(
    request: ResponsesRequest,
    app_key_override: str = "",
    http_request: Request | None = None,
) -> StreamingResponse:
    """Return a Responses API SSE stream after the browser response completes.

    Browser automation cannot provide token deltas, but some clients (notably chat
    UIs) send stream=true and require an event-stream response. Emit one full-text
    delta plus the completed response so those clients can consume the result.
    """

    async def _events():
        response = await _execute_responses(
            request,
            app_key_override=app_key_override,
            http_request=http_request,
        )
        response_dict = _model_dump_compat(response, mode="json")
        text = ""
        for item in response.output:
            if isinstance(item, ResponseOutputMessage):
                text += "".join(part.text for part in item.content)

        if text:
            yield _responses_sse_event(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "response_id": response.id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text,
                },
            )

        yield _responses_sse_event(
            "response.completed",
            {
                "type": "response.completed",
                "response": response_dict,
            },
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _validate_responses_request(request: ResponsesRequest) -> None:
    """Validate a Responses API request."""
    if not request.input:
        raise HTTPException(status_code=400, detail="input cannot be empty")

    _resolve_model_id(request.model)


async def _execute_responses(
    request: ResponsesRequest,
    app_key_override: str = "",
    http_request: Request | None = None,
) -> ResponsesResponse:
    """Shared executor for Responses API requests.

    Translates to ChatCompletionRequest, delegates to _execute_chat_completion,
    and translates back to Responses API format.
    """
    chat_request = _responses_request_to_chat_request(request)
    chat_request.stream = False
    chat_response = await _execute_chat_completion(
        chat_request,
        app_key_override=app_key_override,
        http_request=http_request,
    )
    return _responses_response_from_chat(chat_response, request.model)

async def _execute_chat_completion(
    request: ChatCompletionRequest,
    app_key_override: str = "",
    http_request: Request | None = None,
) -> ChatCompletionResponse:
    """Shared sync/async executor for chat completions."""
    client = _get_client()
    model_id = _resolve_model_id(request.model)
    app_key = (app_key_override or "").strip()
    session_key = _tab_session_key(request, http_request, app_key)

    # Track expired thread ids to delete after this request releases its tab.
    _deletion_pending: list[str] = []

    try:
        async with acquire_browser_page(session_key) as lease:
            client = _bind_client(client, lease.page)
            start_time = time.time()
            app_name = _display_app_name(app_key)
            explicit_thread_id = (request.thread_id or "").strip() if getattr(request, "thread_id", None) else ""
            routing_action = "reuse-current"
            continuing_thread = False
            app_thread_created_by_catgpt = False
            if Config.API_APP_THREAD_MODE and app_key:
                log.info("OpenAI app-thread key: %s", app_key)

            if explicit_thread_id:
                current_tid = client._extract_thread_id()
                if current_tid != explicit_thread_id:
                    log.info(f"OpenAI route: navigating to explicit thread {explicit_thread_id}")
                    await client.navigate_to_thread(explicit_thread_id)
                routing_action = "explicit-thread"
                continuing_thread = True
            elif Config.API_APP_THREAD_MODE and app_key:
                now_app = time.time()
                mapped_thread = ""
                expired_tids: list[str] = []
                async with _app_thread_lock:
                    expired_tids = _prune_app_threads(now_app)
                    mapped = _app_threads.get(app_key)
                    if mapped:
                        mapped_thread = mapped.thread_id
                        app_thread_created_by_catgpt = mapped.created_by_catgpt
                if expired_tids:
                    _deletion_pending.extend(expired_tids)
                if mapped_thread:
                    current_tid = client._extract_thread_id()
                    if current_tid != mapped_thread:
                        log.info(f"OpenAI app-thread mode: app='{app_key}' -> thread {mapped_thread}")
                        await client.navigate_to_thread(mapped_thread)
                    routing_action = "mapped-thread"
                    continuing_thread = True
                else:
                    log.info(
                        "OpenAI app-thread mode: app='%s' has no mapped thread. New chat will be created before prompt send.",
                        app_name,
                    )
                    await client.new_chat()
                    routing_action = "new-chat-for-app"
                    app_thread_created_by_catgpt = True
            elif session_key and not lease.is_first_turn:
                routing_action = "persistent-session"
                continuing_thread = True
            elif Config.uses_browser():
                log.info("No session identity: starting a fresh ChatGPT thread")
                await client.new_chat()
                routing_action = "new-chat-stateless"
            else:
                routing_action = "stateless-api"

            # -- Extract attachments from messages --------------
            image_paths: list[str] = []
            file_paths: list[str] = []
            for msg in request.messages:
                if msg.role == "user" and isinstance(msg.content, list):
                    image_urls = _extract_image_urls(msg.content)
                    for url in image_urls:
                        local_path = await _download_file(url)
                        if local_path:
                            image_paths.append(local_path)

                    file_attachments = _extract_file_attachments(msg.content)
                    for fa in file_attachments:
                        local_path = await _download_file(fa)
                        if local_path:
                            file_paths.append(local_path)

            all_attachment_paths = image_paths + file_paths
            if all_attachment_paths:
                log.info(f"Extracted {len(image_paths)} image(s) and {len(file_paths)} file(s) from request")

            expansion = expand_attachments_for_chatgpt(image_paths, file_paths)
            image_paths = expansion.image_paths
            file_paths = expansion.file_paths
            page_extraction_mode = _page_extraction_mode(request)
            page_extraction_expected_count: int | None = None
            effective_response_format = request.response_format
            prompt_prefixes: list[str] = []

            if expansion.notes:
                prompt_prefixes.append(build_attachment_context_note(expansion.notes))
                log.info(
                    "Attachment expansion: rendered %d page image(s), upload set now has %d image(s) and %d file(s)",
                    expansion.total_rendered_pages,
                    len(image_paths),
                    len(file_paths),
                )

            if page_extraction_mode == "structured":
                if not expansion.page_descriptors:
                    raise HTTPException(
                        status_code=400,
                        detail="page_extraction.mode='structured' requires at least one image or file attachment.",
                    )
                page_extraction_expected_count = len(expansion.page_descriptors)
                effective_response_format = _build_page_extraction_response_format(expansion.page_descriptors)
                prompt_prefixes.append(_build_page_extraction_note(expansion.page_descriptors))
                log.info(
                    "Per-page extraction mode enabled: %d logical page item(s)",
                    page_extraction_expected_count,
                )

            attachment_prefix = "".join(prefix for prefix in prompt_prefixes if prefix)

            # -- Build the prompt --------------------------------
            messages = list(request.messages)

            # If tools are provided, inject tool definitions as a system prompt
            if request.tools:
                tool_system = _build_tool_system_prompt(request.tools)
                # Prepend as the first system message
                messages.insert(0, ChatMessage(role="system", content=tool_system))

            # If structured output is requested, force strict JSON response
            response_format_system = _build_response_format_system_prompt(effective_response_format)
            if response_format_system and not _has_equivalent_response_instruction(messages, response_format_system):
                messages.insert(0, ChatMessage(role="system", content=response_format_system))

            messages = _dedupe_system_messages(messages)
            if continuing_thread:
                pruned = _latest_turn_messages(messages)
                if len(pruned) < len(messages):
                    log.info(
                        "Pruned conversation history for existing thread: %d -> %d messages",
                        len(messages),
                        len(pruned),
                    )
                    messages = pruned
            system_texts = [_extract_content_text(m.content) for m in messages if m.role == "system"]
            system_lengths = [len(t) for t in system_texts]
            system_previews = [_normalize_instruction_text(t)[:120] for t in system_texts]
            non_system = [m for m in messages if m.role != "system"]
            full_prompt = _build_prompt(messages)
            prompt = full_prompt
            used_thread_contract = False
            used_user_contract = False
            extract_thread_id = getattr(client, "_extract_thread_id", None)
            current_thread_id = extract_thread_id() if callable(extract_thread_id) else ""
            current_chat_name = await _lookup_thread_title(client, current_thread_id) if current_thread_id else ""
            chat_display = current_chat_name or ("New chat" if not current_thread_id else "Unknown")
            log.info(
                "Request context: app=%s, route=%s, thread=%s, chat_name=%s",
                app_name,
                routing_action,
                current_thread_id or "<new>",
                chat_display,
            )
            contract_id = ""
            user_contract_id = ""
            user_text = _extract_content_text(non_system[0].content) if len(non_system) == 1 else ""

            if (
                Config.API_THREAD_CONTRACT_MODE
                and system_texts
                and len(non_system) == 1
                and non_system[0].role == "user"
            ):
                contract_id = _contract_hash(system_texts)
                if current_thread_id:
                    now_contract = time.time()
                    async with _contract_lock:
                        _prune_thread_contracts(now_contract)
                        known = _thread_contracts.get(current_thread_id)
                        if known and known[1] == contract_id:
                            prompt = _build_contract_reminder_prompt(user_text, contract_id)
                            used_thread_contract = True
                            _thread_contracts[current_thread_id] = (now_contract, contract_id)

                        # Learn repeated user-instruction prefixes across turns.
                        prev_user = _thread_last_user_text.get(current_thread_id)
                        detected_prefix = None
                        if prev_user:
                            detected_prefix = _detect_user_prefix_contract(prev_user[1], user_text)
                        if detected_prefix:
                            prefix, tail = detected_prefix
                            candidate_user_contract_id = _user_contract_hash(system_texts, prefix)
                            known_user = _thread_user_contracts.get(current_thread_id)
                            if known_user and known_user[1] == candidate_user_contract_id and effective_response_format:
                                prompt = _build_user_contract_reminder_prompt(
                                    tail,
                                    contract_id,
                                    candidate_user_contract_id,
                                )
                                used_user_contract = True
                                used_thread_contract = False
                            # Store/refresh learned prefix contract for future turns.
                            _thread_user_contracts[current_thread_id] = (
                                now_contract,
                                candidate_user_contract_id,
                                prefix,
                            )
                            user_contract_id = candidate_user_contract_id

                        _thread_last_user_text[current_thread_id] = (now_contract, user_text)

            system_chars = sum(len(_extract_content_text(m.content)) for m in messages if m.role == "system")
            user_chars = sum(len(_extract_content_text(m.content)) for m in messages if m.role == "user")
            assistant_chars = sum(len(_extract_content_text(m.content)) for m in messages if m.role == "assistant")
            tool_chars = sum(len(_extract_content_text(m.content)) for m in messages if m.role == "tool")
            log.debug(
                "System prompt breakdown: count=%d lengths=%s previews=%s",
                len(system_texts),
                system_lengths,
                system_previews,
            )
            log.info(
                f"POST /v1/chat/completions - model={request.model}, "
                f"{len(request.messages)} messages, prompt={len(prompt)} chars "
                f"(system={system_chars}, user={user_chars}, assistant={assistant_chars}, tool={tool_chars})"
            )
            if used_thread_contract:
                log.info("Thread contract mode: compact prompt used for thread=%s", current_thread_id or "unknown")
            if used_user_contract:
                log.info(
                    "User prefix contract mode: compact prompt used for thread=%s contract=%s",
                    current_thread_id or "unknown",
                    user_contract_id[:12] if user_contract_id else "unknown",
                )

            if attachment_prefix:
                prompt = f"{attachment_prefix}{prompt}" if prompt else attachment_prefix.strip()
                full_prompt = f"{attachment_prefix}{full_prompt}" if full_prompt else attachment_prefix.strip()

            cache_key = _cache_key_for_request_with_app(request, app_key)
            now = time.time()
            async with _cache_lock:
                _prune_cache(now)
                cached_entry = _response_cache.get(cache_key)
                if cached_entry and now - cached_entry[0] <= _CACHE_TTL_SECONDS:
                    log.info("Response cache hit: returning cached completion")
                    return _clone_cached_response(cached_entry[1])

            # -- Send to ChatGPT --------------------------------
            try:
                send_kwargs = {
                    "image_paths": image_paths or None,
                    "file_paths": file_paths or None,
                    "model": model_id,
                }
                if Config.uses_browser():
                    send_kwargs["read_aloud"] = bool(request.read_aloud)
                else:
                    send_kwargs["stateless"] = True
                result = await client.send_message(prompt, **send_kwargs)
            except PromptTooLongError as e:
                log.warning("ChatGPT rejected an oversized prompt: %s", e)
                raise HTTPException(status_code=413, detail=str(e)) from e
            except PromptAttachmentFallbackError as e:
                log.error("Long-prompt attachment fallback failed: %s", e, exc_info=True)
                raise HTTPException(status_code=502, detail=str(e)) from e
            except Exception as e:
                log.error(f"ChatGPT error: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"ChatGPT error: {str(e)}")

            if Config.API_THREAD_CONTRACT_MODE and contract_id:
                thread_for_contract = result.thread_id or current_thread_id or client._extract_thread_id()
                if thread_for_contract:
                    async with _contract_lock:
                        _prune_thread_contracts(time.time())
                        _thread_contracts[thread_for_contract] = (time.time(), contract_id)
                        # Refresh user text tracking on resolved thread id too.
                        if user_text:
                            _thread_last_user_text[thread_for_contract] = (time.time(), user_text)

            if Config.API_APP_THREAD_MODE and app_key:
                thread_for_app = result.thread_id or client._extract_thread_id()
                if thread_for_app:
                    post_prune_expired: list[str] = []
                    async with _app_thread_lock:
                        post_prune_expired = _prune_app_threads(time.time())
                        _app_threads[app_key] = _AppThreadMapping(
                            time.time(),
                            thread_for_app,
                            created_by_catgpt=app_thread_created_by_catgpt,
                        )
                    # Best-effort deletion is scheduled after this request releases
                    # its tab lease, so cleanup cannot navigate mid-request.
                    if post_prune_expired:
                        _deletion_pending.extend(post_prune_expired)
                    thread_title = await _lookup_thread_title(client, thread_for_app)
                    log.info(
                        "App-thread mapping updated: app=%s -> thread=%s (%s)",
                        app_name,
                        thread_for_app,
                        thread_title or "title unavailable",
                    )

            response_text = result.message
            elapsed_ms = int((time.time() - start_time) * 1000)

            if (used_thread_contract or used_user_contract) and effective_response_format and _extract_json_payload(response_text) is None:
                mode_name = "user-prefix contract" if used_user_contract else "thread contract"
                log.warning("%s mode produced non-JSON content. Retrying once with full prompt.", mode_name.capitalize())
                try:
                    full_retry = await client.send_message(
                        full_prompt,
                        image_paths=image_paths or None,
                        file_paths=file_paths or None,
                        model=model_id,
                    )
                    response_text = full_retry.message
                    elapsed_ms = int((time.time() - start_time) * 1000)
                except Exception as e:
                    log.warning(f"Full-prompt fallback after contract mode failed: {e}")

            # -- Detect echo (extraction grabbed sent prompt instead of reply) --
            if response_text and "[System instruction:" in response_text and request.tools:
                log.warning("Response appears to echo the sent prompt - retrying extraction")
                try:
                    await asyncio.sleep(3)
                    from src.chatgpt.detector import extract_last_response_via_copy

                    retry_text = await extract_last_response_via_copy(client.page)
                    if retry_text and "[System instruction:" not in retry_text:
                        response_text = retry_text
                        log.info(f"Retry extraction succeeded: {len(response_text)} chars")
                    else:
                        log.warning("Retry extraction still echoed - stripping system prefix")
                        idx = response_text.rfind("\n\n")
                        if idx > 0:
                            tail = response_text[idx:].strip()
                            if tail and not tail.startswith("["):
                                response_text = tail
                except Exception as e:
                    log.warning(f"Retry extraction failed: {e}")

            # -- Check for tool calls ----------------------------
            tool_calls = None
            finish_reason = "stop"

            if effective_response_format and response_text:
                response_text = _normalize_structured_content(response_text, effective_response_format)
                mismatch = _structured_cardinality_mismatch(
                    request.messages,
                    response_text,
                    expected_count=page_extraction_expected_count,
                )
                if mismatch:
                    expected, actual = mismatch
                    log.warning(
                        "Structured cardinality mismatch detected (expected=%d, actual=%d). Retrying once.",
                        expected,
                        actual,
                    )
                    retry_prompt = _build_cardinality_retry_prompt(
                        prompt,
                        expected,
                        actual,
                        expectation_reason="the numbered page map" if page_extraction_expected_count is not None else "input line count",
                        correction_rule=(
                            "Return valid JSON only, with exactly one output item per numbered page-map entry, "
                            "preserving `page_index`, `source_name`, and `page_number` exactly and keeping the same order. "
                            "Do not merge, drop, or add pages."
                        )
                        if page_extraction_expected_count is not None
                        else None,
                    )
                    try:
                        retry_result = await client.send_message(
                            retry_prompt,
                            image_paths=image_paths or None,
                            file_paths=file_paths or None,
                            model=model_id,
                        )
                        retry_text = retry_result.message
                        retry_text = _normalize_structured_content(retry_text, effective_response_format)
                        retry_mismatch = _structured_cardinality_mismatch(
                            request.messages,
                            retry_text,
                            expected_count=page_extraction_expected_count,
                        )
                        if not retry_mismatch:
                            response_text = retry_text
                            elapsed_ms = int((time.time() - start_time) * 1000)
                            log.info("Structured cardinality retry succeeded")
                        else:
                            log.warning(
                                "Structured cardinality retry still mismatched (expected=%d, actual=%d)",
                                retry_mismatch[0],
                                retry_mismatch[1],
                            )
                    except Exception as e:
                        log.warning(f"Structured cardinality retry failed: {e}")

            if request.tools:
                tool_calls = _parse_tool_calls(response_text, request.tools)
                if tool_calls:
                    finish_reason = "tool_calls"
                    response_text = None

            # -- Build response ----------------------------------
            prompt_tokens = _estimate_tokens(prompt)
            completion_tokens = _estimate_tokens(response_text or "")

            response = ChatCompletionResponse(
                model=model_id,
                choices=[
                    Choice(
                        index=0,
                        message=ChoiceMessage(
                            role="assistant",
                            content=response_text,
                            tool_calls=tool_calls,
                            audio=(
                                AudioInfo(
                                    url=result.audio.url,
                                    local_path=result.audio.local_path,
                                    mime_type=result.audio.mime_type,
                                    size_bytes=result.audio.size_bytes,
                                )
                                if result.audio
                                else None
                            ),
                        ),
                        finish_reason=finish_reason,
                    )
                ],
                usage=UsageInfo(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                ),
            )

            log.info(
                f"Response: {elapsed_ms}ms, finish_reason={finish_reason}, "
                f"tokens~{response.usage.total_tokens}"
            )

            async with _cache_lock:
                _prune_cache(time.time())
                _response_cache[cache_key] = (time.time(), _model_copy_compat(response, deep=True))

            return response
    finally:
        if _deletion_pending:
            asyncio.create_task(_maybe_delete_expired_app_threads(_deletion_pending))


async def _run_async_chat_job(job_id: str, request: ChatCompletionRequest, app_key: str = "") -> None:
    """Background runner for async chat completion jobs."""
    async with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.status = "running"

    try:
        response = await _execute_chat_completion(request, app_key_override=app_key)
        async with _jobs_lock:
            job = _jobs.get(job_id)
            if job is not None:
                job.status = "completed"
                job.response = response
    except Exception as e:
        log.error(f"Async chat job failed ({job_id}): {e}", exc_info=True)
        async with _jobs_lock:
            job = _jobs.get(job_id)
            if job is not None:
                job.status = "failed"
                job.error = str(e)
                job.error_status_code = int(getattr(e, "status_code", 500))


async def _submit_async_chat_job(
    request: ChatCompletionAsyncRequest,
    http_request: Request,
    endpoint_app_name: str = "",
) -> ChatCompletionJobResponse:
    """Shared async chat submit logic for generic and app-scoped routes."""
    _validate_chat_request(request)
    _get_client()

    app_key = _resolve_app_key(request, http_request, endpoint_app_name=endpoint_app_name)

    job_id = f"chatjob-{uuid.uuid4().hex[:24]}"
    job = ChatCompletionJobResponse(
        id=job_id,
        status="queued",
        model=request.model,
    )

    async with _jobs_lock:
        _jobs[job_id] = job
        _job_app_keys[job_id] = app_key

    asyncio.create_task(_run_async_chat_job(job_id, request, app_key))
    return job


@openai_router.post("/v1/chat/completions/async", response_model=ChatCompletionJobResponse)
async def create_chat_completion_async(
    request: ChatCompletionAsyncRequest,
    http_request: Request,
) -> ChatCompletionJobResponse:
    """Submit an async chat completion job and return the job handle."""
    return await _submit_async_chat_job(request, http_request)


@openai_router.post("/{app_name}/v1/chat/completions/async", response_model=ChatCompletionJobResponse)
async def create_chat_completion_async_scoped(
    app_name: str,
    request: ChatCompletionAsyncRequest,
    http_request: Request,
) -> ChatCompletionJobResponse:
    """App-scoped alias for async chat submit."""
    return await _submit_async_chat_job(request, http_request, endpoint_app_name=app_name)


@openai_router.get("/v1/chat/completions/async/{job_id}", response_model=ChatCompletionJobResponse)
async def get_chat_completion_async_job(job_id: str) -> ChatCompletionJobResponse:
    """Get async chat completion job state and result."""
    async with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return job


@openai_router.get("/{app_name}/v1/chat/completions/async/{job_id}", response_model=ChatCompletionJobResponse)
async def get_chat_completion_async_job_scoped(
    app_name: str,
    job_id: str,
) -> ChatCompletionJobResponse:
    """
    App-scoped alias for async chat status/result.

    Enforces app/job ownership so parallel apps cannot read each other's jobs.
    """
    expected_key = f"endpoint:{_normalize_key_part(app_name)}"

    async with _jobs_lock:
        job = _jobs.get(job_id)
        job_key = _job_app_keys.get(job_id, "")

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if job_key and job_key != expected_key:
        raise HTTPException(status_code=404, detail="Job not found")

    return job
