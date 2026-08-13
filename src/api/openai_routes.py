"""
OpenAI-compatible API routes.

Provides:
  POST /v1/chat/completions   — chat completions (with tool/function calling)
  GET  /v1/models             — list available models

All requests are serialized through an asyncio.Lock because the underlying
Playwright browser page is single-threaded.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from patchright.async_api import Page

from src.api.openai_schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ChoiceMessage,
    FunctionCallInfo,
    FunctionDefinition,
    ImageData,
    ImageGenerationRequest,
    ImagesResponse,
    ModelListResponse,
    ModelObject,
    ResponseFunctionCall,
    ResponseObject,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponsesRequest,
    ResponseUsage,
    ToolCall,
    ToolDefinition,
    UsageInfo,
)
from src.chatgpt.client import ChatGPTClient
from src.claude.client import ClaudeClient
from src.minimax.client import MiniMaxClient
from src.config import Config
from src.log import setup_logging

log = setup_logging("openai_routes")

openai_router = APIRouter()

# Global references — set by server.py at startup
_client: ChatGPTClient | ClaudeClient | MiniMaxClient | None = None
_browser: Any = None


class BrowserPagePool:
    """
    Manages a pool of clean, stateless browser pages (tabs) for concurrent requests.
    Each request executes in its own isolated page/tab, preventing crosstalk and prompt bloat.
    """

    def __init__(self, browser: Any = None, max_concurrency: int = 3):
        self._browser = browser
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._pool: asyncio.Queue[Page] = asyncio.Queue()
        self._max_concurrency = max_concurrency

    def set_browser(self, browser: Any) -> None:
        self._browser = browser

    @asynccontextmanager
    async def acquire_clean_page(self):
        """Acquire a fresh, stateless Page ready at ChatGPT / Claude."""
        if not Config.uses_browser():
            yield None
            return

        await self._semaphore.acquire()
        page: Page | None = None
        keep_page = True
        try:
            # 1. Borrow from pool or create new page
            try:
                page = self._pool.get_nowait()
            except asyncio.QueueEmpty:
                if self._browser and getattr(self._browser, "context", None):
                    page = await self._browser.new_page()
                elif self._browser and getattr(self._browser, "page", None):
                    page = self._browser.page
                else:
                    raise RuntimeError("Browser is not running")

            if page is None or page.is_closed():
                if self._browser and getattr(self._browser, "context", None):
                    page = await self._browser.new_page()
                else:
                    raise RuntimeError("Browser context unavailable")

            # 2. Ensure page is at the provider URL and in a fresh chat state
            target_url = Config.provider_url()
            current_url = page.url or ""

            if target_url not in current_url and not current_url.startswith(target_url):
                await page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
            else:
                try:
                    new_chat_btn = await page.query_selector(
                        "a[data-testid='create-new-chat-button'], a[href='/']"
                    )
                    if new_chat_btn:
                        await new_chat_btn.click()
                        await asyncio.sleep(0.3)
                    elif current_url != f"{target_url}/" and current_url != target_url:
                        await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                except Exception as e:
                    log.debug(f"Reset to fresh chat exception: {e}")

            yield page

        except Exception as e:
            keep_page = False
            if page and not page.is_closed():
                main_p = getattr(self._browser, "page", None)
                if page != main_p:
                    try:
                        await page.close()
                    except Exception:
                        pass
            raise e
        finally:
            if keep_page and page and not page.is_closed():
                try:
                    await self._pool.put(page)
                except Exception:
                    main_p = getattr(self._browser, "page", None)
                    if page != main_p:
                        await page.close()
            elif page and not page.is_closed():
                main_p = getattr(self._browser, "page", None)
                if page != main_p:
                    try:
                        await page.close()
                    except Exception:
                        pass
            self._semaphore.release()


_page_pool: BrowserPagePool | None = None


class PersistentSessionManager:
    """
    Manages persistent browser pages for sessions that require continuous conversation
    within the same ChatGPT thread (avoiding new chat navigations and CAPTCHAs).
    """

    def __init__(self, browser: Any = None):
        self._browser = browser
        self._pages: dict[str, Page] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._session_initialized: set[str] = set()

    def set_browser(self, browser: Any) -> None:
        self._browser = browser

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    @asynccontextmanager
    async def acquire_session_page(self, session_id: str):
        """Acquire the persistent Page bound to session_id with per-session locking."""
        lock = self._get_lock(session_id)
        await lock.acquire()
        page: Page | None = None
        try:
            page = self._pages.get(session_id)
            is_first_turn = session_id not in self._session_initialized

            if page is None or page.is_closed():
                if self._browser and getattr(self._browser, "context", None):
                    page = await self._browser.new_page()
                elif self._browser and getattr(self._browser, "page", None):
                    page = self._browser.page
                else:
                    raise RuntimeError("Browser is not running")

                target_url = Config.provider_url()
                current_url = page.url or ""
                if target_url not in current_url:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
                else:
                    try:
                        new_chat_btn = await page.query_selector(
                            "a[data-testid='create-new-chat-button'], a[href='/']"
                        )
                        if new_chat_btn:
                            await new_chat_btn.click()
                            await asyncio.sleep(0.3)
                    except Exception:
                        pass

                self._pages[session_id] = page
                self._session_initialized.add(session_id)
                is_first_turn = True

            yield page, is_first_turn

        finally:
            lock.release()


_session_manager: PersistentSessionManager | None = None


def _get_session_manager() -> PersistentSessionManager:
    global _session_manager
    if _session_manager is None:
        _session_manager = PersistentSessionManager(_browser)
    return _session_manager


def _extract_session_id(raw_request: Request | None, body_user: str | None = None) -> str | None:
    """Extract session ID from request headers or body user field."""
    if raw_request is not None:
        for header_key in ("x-session-id", "session-id", "x-thread-id", "thread-id"):
            val = raw_request.headers.get(header_key)
            if val and val.strip():
                return val.strip()
    if body_user and body_user.strip():
        return body_user.strip()
    return None


def _extract_persistent_prompt(
    messages: list[ChatMessage],
    is_first_turn: bool,
    has_tool_prompt: bool = False,
) -> tuple[str, list[ChatMessage]]:
    """
    In persistent mode, the webpage itself remembers history.
    If the client sent repeated full history, prune it down to the latest user message
    (plus system prompt if first turn) to prevent prompt duplication bloat.
    """
    if not messages:
        return "", []

    if is_first_turn:
        return _build_prompt(messages), messages

    # Subsequent turns: Extract latest user turn (and any tool results directly associated with it)
    latest_msgs: list[ChatMessage] = []
    for msg in reversed(messages):
        if msg.role in ("user", "tool"):
            latest_msgs.insert(0, msg)
            if msg.role == "user":
                break

    if not latest_msgs:
        latest_msgs = [messages[-1]]

    pruned_prompt = _build_prompt(latest_msgs)
    log.info(
        f"[Persistent Session] Pruned history: original {len(messages)} messages "
        f"-> {len(latest_msgs)} latest message(s) ({len(pruned_prompt)} chars)"
    )
    return pruned_prompt, latest_msgs


def _get_page_pool() -> BrowserPagePool:
    global _page_pool
    if _page_pool is None:
        _page_pool = BrowserPagePool(_browser, max_concurrency=Config.MAX_CONCURRENT_REQUESTS)
    return _page_pool


def _resolve_model_id(requested: str | None) -> str:
    """Resolve a request model against the active provider mapping."""
    try:
        return Config.resolve_model_id(requested)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def set_openai_client(
    client: ChatGPTClient | ClaudeClient | MiniMaxClient,
    browser: Any = None,
) -> None:
    """Called by server.py to inject the client and browser."""
    global _client, _browser, _page_pool, _session_manager
    _client = client
    _browser = browser
    _page_pool = BrowserPagePool(browser, max_concurrency=Config.MAX_CONCURRENT_REQUESTS)
    _session_manager = PersistentSessionManager(browser)


def _get_client() -> ChatGPTClient | ClaudeClient | MiniMaxClient:
    if _client is None:
        raise HTTPException(status_code=503, detail="Client not initialized")
    return _client


# ── Helpers ─────────────────────────────────────────────────────


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars per token)."""
    return max(1, len(text) // 4)


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

    # ── Dict form (from _extract_file_attachments) ──
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

    # ── String forms ──
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
                "image/gif": "gif", "application/pdf": "pdf",
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
        # HTTP URL — download it
        try:
            import urllib.request
            ext = "bin"
            for e in ["jpg", "jpeg", "webp", "gif", "png", "pdf", "txt", "csv", "docx", "xlsx"]:
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
            sys_text = _extract_content_text(system_msgs[0].content)
            if Config.PROVIDER == "claude":
                # Claude rejects "[System instruction: ...]" as prompt injection.
                # Present it as context instead.
                prefix = f"{sys_text}\n\n"
            else:
                prefix = f"[System instruction: {sys_text}]\n\n"
        user_text = _extract_content_text(non_system[0].content)
        return prefix + (user_text or "")

    # Multi-turn: build a transcript
    parts: list[str] = []
    for msg in messages:
        role = msg.role.capitalize()
        if msg.role == "system":
            if Config.PROVIDER == "claude":
                # For Claude, present system messages as context without the label
                text = _extract_content_text(msg.content)
                if text:
                    parts.append(text)
            else:
                text = _extract_content_text(msg.content)
                if text:
                    parts.append(f"System: {text}")
        elif msg.role == "tool":
            # Tool result — include both the call context and the result
            tool_content = _extract_content_text(msg.content)
            if Config.PROVIDER == "claude":
                parts.append(
                    f"The tool was executed and returned this result:\n{tool_content}\n\n"
                    f"Now use the result above to answer the user's original question in plain text."
                )
            else:
                parts.append(
                    f"[Tool result for {msg.tool_call_id or 'unknown'}]: {tool_content}\n\n"
                    f"Use the tool result to answer the user. Do NOT call tools again."
                )
        elif msg.role == "assistant" and msg.tool_calls:
            # Assistant requested tool calls — show what was called
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


def _build_tool_system_prompt(
    tools: list[ToolDefinition],
    tool_choice: str | dict | None = None,
) -> str:
    """
    Build a system-level instruction that tells the model about available tools.

    *tool_choice* controls how insistent the instructions are:
      - "auto" / None  — model decides whether to call a tool or answer directly
      - "required"     — model MUST call at least one tool
      - "none"         — caller should not call this function at all
      - {"type":"function","function":{"name":"X"}} — model MUST call that tool
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

    # ── Determine the decision instruction based on tool_choice ──
    forced_tool_name = None
    if isinstance(tool_choice, dict):
        # {"type": "function", "function": {"name": "X"}}
        forced_tool_name = (
            tool_choice.get("function", {}).get("name")
            if isinstance(tool_choice.get("function"), dict)
            else None
        )

    if forced_tool_name:
        decision = (
            f"You MUST call the function `{forced_tool_name}`. "
            f"Do NOT answer the question yourself — output only the JSON tool call."
        )
    elif tool_choice == "required":
        decision = (
            "You MUST call at least one of the available functions. "
            "Do NOT answer the question yourself — always output tool calls."
        )
    else:
        # "auto" or None — model decides
        decision = (
            "If the user's request can be fulfilled or assisted by one or more "
            "of the available functions, call the appropriate tool(s). "
            "If none of the tools are relevant, answer the user normally in plain text."
        )

    # ── Provider-specific prompt framing ──
    if Config.PROVIDER == "claude":
        return f"""You have access to external tools through a structured interface. {decision}

When calling tools, respond with ONLY a JSON code block — no text before or after it:

```json
{{"tool_calls": [{{"name": "<function_name>", "arguments": {{...}}}}]}}
```

Rules:
1. Output ONLY the JSON code block when calling tools. Do not add any commentary, explanation, or text outside the code block.
2. You may call multiple functions in one response by adding them to the array.
3. Use the exact parameter names and types shown in each function's schema.
4. When you receive tool results in a follow-up message, use them to give the user a natural, helpful answer. Do NOT output another JSON tool call for the same request.

Available functions:
{tools_json}

Example — single tool:
```json
{{"tool_calls": [{{"name": "get_current_time", "arguments": {{}}}}]}}
```

Example — multiple tools:
```json
{{"tool_calls": [{{"name": "weather_forecast", "arguments": {{"city": "Tokyo", "date": "today"}}}}, {{"name": "calculate_expression", "arguments": {{"expression": "2+2"}}}}]}}
```
"""
    else:
        return f"""You are in tool-calling mode. {decision}

When calling tools, output ONLY a JSON code block — no other text:

```json
{{"tool_calls": [{{"name": "<function_name>", "arguments": {{...}}}}]}}
```

Rules:
1. Output ONLY the JSON code block when calling tools. No explanation, no text before or after.
2. You may call multiple functions in one response by adding them to the array.
3. Use the exact parameter names and types from each function's schema.
4. When a follow-up message contains tool results, summarize them naturally for the user. Do NOT call tools again for the same request.
5. Do not refuse or say tools are unavailable — they are available through this interface.

Available functions:
{tools_json}

Example — single tool:
```json
{{"tool_calls": [{{"name": "get_current_time", "arguments": {{}}}}]}}
```

Example — multiple tools:
```json
{{"tool_calls": [{{"name": "weather_forecast", "arguments": {{"city": "Tokyo", "date": "today"}}}}, {{"name": "calculate_expression", "arguments": {{"expression": "2+2"}}}}]}}
```
"""


def _extract_json_object(text: str, anchor: str = "tool_calls") -> str | None:
    """
    Extract a JSON object containing *anchor* key from *text*.

    Uses two strategies:
      1. Look inside markdown code blocks (```json ... ```)
      2. Find the anchor key and walk outward using brace-depth tracking
         to handle arbitrarily nested JSON (arrays, nested objects, etc.)
    """
    # Strategy 1: code blocks — most reliable when the model obeys the prompt
    for m in re.finditer(r"```(?:json)?\s*\n?([\s\S]*?)\n?\s*```", text):
        candidate = m.group(1).strip()
        if anchor in candidate:
            try:
                parsed = json.loads(candidate)
                if anchor in parsed:
                    return candidate
            except json.JSONDecodeError:
                continue

    # Strategy 2: locate anchor, walk to balanced braces
    search_key = f'"{anchor}"'
    idx = text.find(search_key)
    if idx == -1:
        return None

    # Walk backward to the nearest '{'
    start = text.rfind("{", 0, idx)
    if start == -1:
        return None

    # Walk forward tracking brace depth, respecting JSON string literals
    depth = 0
    in_string = False
    i = start
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 2          # skip escaped char
                continue
            if c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except json.JSONDecodeError:
                        return None
        i += 1

    return None


def _parse_tool_calls(
    response_text: str, tools: list[ToolDefinition]
) -> list[ToolCall] | None:
    """
    Try to parse tool calls from the model's response text.

    Uses robust brace-matching extraction (handles nested JSON, arrays, etc.)
    then validates tool names against the provided tool definitions.
    Returns None if no valid tool calls are found.
    """
    json_str = _extract_json_object(response_text, "tool_calls")
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


# ── Routes ──────────────────────────────────────────────────────


@openai_router.get("/v1/models", response_model=ModelListResponse)
async def list_models() -> ModelListResponse:
    """List models exposed by the active provider."""
    owned_by = Config.provider_owner()
    return ModelListResponse(
        data=[
            ModelObject(id=model_id, owned_by=owned_by)
            for model_id in Config.provider_model_ids()
        ]
    )


@openai_router.post("/v1/images/generations", response_model=ImagesResponse)
async def create_image(
    request: ImageGenerationRequest,
) -> ImagesResponse:
    """
    OpenAI-compatible image generation endpoint.

    Sends the prompt to ChatGPT which uses DALL-E to generate images.
    Downloads the generated images and returns them in OpenAI format.
    Supports response_format='b64_json' (default) or 'url' (local file path).
    """
    import base64

    if not request.prompt:
        raise HTTPException(status_code=400, detail="prompt cannot be empty")

    if not Config.supports_image_generation():
        raise HTTPException(
            status_code=501,
            detail="Image generation is not supported by the active provider.",
        )

    client = _get_client()

    async with _get_page_pool().acquire_clean_page() as page:
        start_time = time.time()

        # Build an image-generation prompt.
        # n > 1: we ask ChatGPT to generate multiple images
        # size/quality/style hints are included but ChatGPT web may ignore them.
        prompt_parts = [f"Generate an image: {request.prompt}"]
        if request.n and request.n > 1:
            prompt_parts.append(f"Please generate {request.n} different images.")
        if request.size and request.size != "1024x1024":
            prompt_parts.append(f"Image size: {request.size}.")
        if request.quality == "hd":
            prompt_parts.append("Make it high-definition / highly detailed.")
        if request.style == "natural":
            prompt_parts.append("Use a natural, realistic style.")

        full_prompt = " ".join(prompt_parts)

        log.info(
            f"POST /v1/images/generations — prompt='{request.prompt[:80]}', "
            f"n={request.n}, size={request.size}, response_format={request.response_format}"
        )

        # Start a fresh conversation to avoid thread exhaustion
        await _ensure_fresh_chat()

        # Send to ChatGPT
        try:
            result = await client.send_message(full_prompt)
        except Exception as e:
            log.error(f"Provider error during image generation: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Provider error: {str(e)}")

        elapsed_ms = int((time.time() - start_time) * 1000)

        # Check if ChatGPT generated images
        if not result.images:
            # ChatGPT may have responded with text instead of generating an image.
            # This can happen when the model declines or gives a text description.
            log.warning(
                f"No images detected in response ({elapsed_ms}ms). "
                f"ChatGPT replied: {result.message[:200]}"
            )
            raise HTTPException(
                status_code=422,
                detail=(
                    f"ChatGPT did not generate an image. "
                    f"Model response: {result.message[:500]}"
                ),
            )

        # Build image data objects
        image_data_list: list[ImageData] = []
        for img_info in result.images:
            revised_prompt = img_info.prompt_title or img_info.alt or request.prompt

            if request.response_format == "b64_json":
                # Read the downloaded file and base64-encode it
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
                # response_format == "url" → return local file path as URL
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
            f"{elapsed_ms}ms, format={request.response_format}"
        )

        return ImagesResponse(data=image_data_list)


async def _stream_chat_completion_chunks(
    response_text: str | None,
    tool_calls: list[ToolCall] | None,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
):
    """
    Yield standard OpenAI SSE chunks for streaming /v1/chat/completions.
    """
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created_time = int(time.time())

    # 1) Initial chunk with role
    first_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created_time,
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {json.dumps(first_chunk)}\n\n"

    # 2) Text delta chunks
    if response_text:
        chunk_size = 20
        for i in range(0, len(response_text), chunk_size):
            chunk_content = response_text[i : i + chunk_size]
            chunk_data = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created_time,
                "model": model_id,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": chunk_content},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk_data)}\n\n"
            await asyncio.sleep(0.005)

    # 3) Tool call chunks
    if tool_calls:
        for idx, tc in enumerate(tool_calls):
            tc_data = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created_time,
                "model": model_id,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": idx,
                                    "id": tc.id,
                                    "type": "function",
                                    "function": {
                                        "name": tc.function.name,
                                        "arguments": tc.function.arguments,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(tc_data)}\n\n"

    # 4) Final chunk
    final_finish_reason = "tool_calls" if tool_calls else "stop"
    final_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created_time,
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": final_finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    yield f"data: {json.dumps(final_chunk)}\n\n"

    # 5) [DONE]
    yield "data: [DONE]\n\n"


@openai_router.post("/v1/chat/completions")
async def create_chat_completion(
    request: ChatCompletionRequest,
    raw_request: Request,
):
    """
    OpenAI-compatible chat completions endpoint.

    Converts the message array into a single prompt, sends it to ChatGPT
    via browser automation, and returns an OpenAI-formatted response.
    Supports streaming, tool/function calling, stateless execution, and persistent sessions.
    """
    # ── Validate ────────────────────────────────────────────

    if not request.messages:
        raise HTTPException(status_code=400, detail="messages array cannot be empty")

    if Config.PROVIDER == "minimax" and any(
        _contains_attachment(message.content) for message in request.messages
    ):
        raise HTTPException(
            status_code=501,
            detail="Attachments are not supported by the MiniMax provider.",
        )

    client = _get_client()
    model_id = _resolve_model_id(request.model)
    session_id = _extract_session_id(raw_request, getattr(request, "user", None))

    if session_id:
        page_cm = _get_session_manager().acquire_session_page(session_id)
        is_persistent = True
    else:
        @asynccontextmanager
        async def _stateless_cm():
            async with _get_page_pool().acquire_clean_page() as p:
                yield p, False
        page_cm = _stateless_cm()
        is_persistent = False

    async with page_cm as (page, is_first_turn):
        start_time = time.time()

        # ── Build the prompt ────────────────────────────────
        messages = list(request.messages)

        has_tool_prompt = False
        if request.tools and request.tool_choice != "none":
            tool_system = _build_tool_system_prompt(
                request.tools, tool_choice=request.tool_choice
            )
            messages.insert(0, ChatMessage(role="system", content=tool_system))
            has_tool_prompt = True

        if is_persistent:
            prompt, active_messages = _extract_persistent_prompt(
                messages, is_first_turn, has_tool_prompt
            )
        else:
            prompt = _build_prompt(messages)
            active_messages = messages

        log.info(
            f"POST /v1/chat/completions — model={model_id}, "
            f"{'persistent session=' + session_id if is_persistent else 'stateless'}, "
            f"prompt={len(prompt)} chars"
        )

        # ── Extract attachments from active messages ────────
        image_paths: list[str] = []
        file_paths: list[str] = []
        for msg in active_messages:
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

        # ── Send to provider ───────────────────────────────
        try:
            result = await client.send_message(
                prompt,
                image_paths=image_paths or None,
                file_paths=file_paths or None,
                model=model_id,
                stateless=not is_persistent,
                page=page,
            )
        except Exception as e:
            log.error(f"Provider error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Provider error: {str(e)}")

        response_text = result.message
        elapsed_ms = int((time.time() - start_time) * 1000)

        # ── Detect echo (extraction grabbed sent prompt instead of reply) ──
        _echo_markers = ["[System instruction:", "tool-calling mode", "Available functions:"]
        if (
            Config.uses_browser()
            and response_text
            and has_tool_prompt
            and any(m in response_text for m in _echo_markers)
        ):
            log.warning("Response appears to echo the sent prompt — retrying extraction")
            try:
                await asyncio.sleep(1.5)
                if Config.PROVIDER == "claude":
                    from src.claude.detector import extract_last_response_via_copy
                else:
                    from src.chatgpt.detector import extract_last_response_via_copy
                retry_text = await extract_last_response_via_copy(client.page)
                if retry_text and not any(m in retry_text for m in _echo_markers):
                    response_text = retry_text
                    log.info(f"Retry extraction succeeded: {len(response_text)} chars")
                else:
                    log.warning("Retry extraction still echoed — stripping system prefix")
                    # Last resort: try to find assistant content after the prompt
                    idx = response_text.rfind("\n\n")
                    if idx > 0:
                        tail = response_text[idx:].strip()
                        if tail and not tail.startswith("["):
                            response_text = tail
            except Exception as e:
                log.warning(f"Retry extraction failed: {e}")

        # ── Check for tool calls ────────────────────────────
        tool_calls = None
        finish_reason = "stop"

        if has_tool_prompt and request.tools:
            tool_calls = _parse_tool_calls(response_text, request.tools)
            if tool_calls:
                finish_reason = "tool_calls"
                # When the model calls tools, content should be null
                response_text = None

        # ── Build response ──────────────────────────────────
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
            f"tokens≈{response.usage.total_tokens}"
        )

        if request.stream:
            return StreamingResponse(
                _stream_chat_completion_chunks(
                    response_text,
                    tool_calls,
                    model_id,
                    prompt_tokens,
                    completion_tokens,
                ),
                media_type="text/event-stream",
            )

        return response


# ── Responses API (/v1/responses) ───────────────────────────────


def _responses_input_to_messages(
    input_data: str | list,
    instructions: str | None = None,
) -> list[ChatMessage]:
    """
    Convert Responses API `input` (string or item array) into a list of
    ChatMessage objects compatible with our existing _build_prompt().

    Handles:
      - Plain string → single user message
      - Array of message objects (role + content)
      - function_call items (assistant requested a tool)
      - function_call_output items (tool results)
    """
    messages: list[ChatMessage] = []

    # System prompt from `instructions`
    if instructions:
        messages.append(ChatMessage(role="system", content=instructions))

    # Simple string input
    if isinstance(input_data, str):
        messages.append(ChatMessage(role="user", content=input_data))
        return messages

    # Array of items
    for item in input_data:
        if isinstance(item, str):
            messages.append(ChatMessage(role="user", content=item))
            continue
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        role = item.get("role")

        if item_type == "function_call":
            # Assistant called a tool — record as assistant message with tool_calls
            name = item.get("name", "")
            arguments = item.get("arguments", "{}")
            call_id = item.get("call_id", f"call_{uuid.uuid4().hex[:24]}")
            messages.append(
                ChatMessage(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id=call_id,
                            type="function",
                            function=FunctionCallInfo(
                                name=name, arguments=arguments
                            ),
                        )
                    ],
                )
            )
        elif item_type == "function_call_output":
            # Tool result — map to role=tool
            call_id = item.get("call_id", "")
            output = item.get("output", "")
            messages.append(
                ChatMessage(
                    role="tool",
                    content=output,
                    tool_call_id=call_id,
                )
            )
        elif item_type == "message" or role:
            # Regular message item
            r = role or item.get("role", "user")
            # Map "developer" role to "system"
            if r == "developer":
                r = "system"
            content = item.get("content", "")
            # Content can be a list of content parts or a string
            if isinstance(content, list):
                # Extract text from content parts
                text_parts = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "input_text":
                            text_parts.append(part.get("text", ""))
                        elif part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        text_parts.append(part)
                content = "\n".join(text_parts) if text_parts else ""
            messages.append(ChatMessage(role=r, content=content))

    return messages


def _responses_tools_to_chat_tools(
    tools: list[dict],
) -> list[ToolDefinition]:
    """
    Convert flat Responses API tool definitions to nested Chat Completions
    ToolDefinition format so we can reuse _build_tool_system_prompt().

    Responses:  {"type": "function", "name": "X", "parameters": {...}}
    Chat:       {"type": "function", "function": {"name": "X", "parameters": {...}}}
    """
    result = []
    for tool in tools:
        if not isinstance(tool, dict):
            tool = tool.model_dump() if hasattr(tool, "model_dump") else dict(tool)
        if tool.get("type") != "function":
            continue
        result.append(
            ToolDefinition(
                type="function",
                function=FunctionDefinition(
                    name=tool.get("name", ""),
                    description=tool.get("description", ""),
                    parameters=tool.get("parameters", {}),
                ),
            )
        )
    return result


def _build_response_object(
    response_text: str | None,
    tool_calls: list[ToolCall] | None,
    request: "ResponsesRequest",
    prompt_tokens: int,
    completion_tokens: int,
    model_id: str,
) -> ResponseObject:
    """Build a full ResponseObject from the model output."""
    now = int(time.time())
    output: list = []
    output_text_val: str | None = None

    if tool_calls:
        for tc in tool_calls:
            output.append(
                ResponseFunctionCall(
                    name=tc.function.name,
                    arguments=tc.function.arguments,
                    call_id=tc.id,
                ).model_dump()
            )
    else:
        text = response_text or ""
        msg = ResponseOutputMessage(
            content=[ResponseOutputText(text=text)]
        )
        output.append(msg.model_dump())
        output_text_val = text

    usage = ResponseUsage(
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )

    # Reconstruct tools for the response envelope
    tools_echo = []
    if request.tools:
        for t in request.tools:
            tools_echo.append(
                t.model_dump() if hasattr(t, "model_dump") else dict(t)
            )

    return ResponseObject(
        created_at=now,
        completed_at=now,
        status="completed",
        model=model_id,
        instructions=request.instructions,
        max_output_tokens=request.max_output_tokens,
        output=output,
        output_text=output_text_val,
        temperature=request.temperature,
        top_p=request.top_p,
        tool_choice=request.tool_choice or "auto",
        tools=tools_echo,
        previous_response_id=request.previous_response_id,
        usage=usage,
        metadata=request.metadata or {},
    )


async def _stream_response_events(
    resp: ResponseObject,
    response_text: str | None,
    tool_calls: list[ToolCall] | None,
):
    """
    Yield SSE events for a streaming Responses API call.

    Since the browser backend doesn't truly stream, we emit the full
    response as a burst of events matching the OpenAI SSE contract:
      response.created → response.in_progress →
      output_item.added → content_part.added →
      output_text.delta (full text as one chunk) →
      output_text.done → content_part.done →
      output_item.done → response.completed
    """
    seq = 0
    resp_dict = resp.model_dump()

    def _event(event_type: str, data: dict) -> str:
        data["type"] = event_type
        data["sequence_number"] = seq
        return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

    # 1) response.created
    created_resp = dict(resp_dict)
    created_resp["status"] = "in_progress"
    created_resp["completed_at"] = None
    created_resp["output"] = []
    created_resp["output_text"] = None
    created_resp["usage"] = None
    yield _event("response.created", {"response": created_resp})
    seq += 1

    # 2) response.in_progress
    yield _event("response.in_progress", {"response": created_resp})
    seq += 1

    if tool_calls:
        # Emit function call output items
        for idx, tc in enumerate(tool_calls):
            fc_item = ResponseFunctionCall(
                name=tc.function.name,
                arguments=tc.function.arguments,
                call_id=tc.id,
            ).model_dump()

            # output_item.added
            fc_added = dict(fc_item)
            fc_added["status"] = "in_progress"
            yield _event("response.output_item.added", {
                "output_index": idx,
                "item": fc_added,
            })
            seq += 1

            # function_call_arguments.delta (one burst)
            yield _event("response.function_call_arguments.delta", {
                "item_id": fc_item["id"],
                "output_index": idx,
                "delta": tc.function.arguments,
            })
            seq += 1

            # function_call_arguments.done
            yield _event("response.function_call_arguments.done", {
                "item_id": fc_item["id"],
                "output_index": idx,
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            })
            seq += 1

            # output_item.done
            yield _event("response.output_item.done", {
                "output_index": idx,
                "item": fc_item,
            })
            seq += 1
    else:
        # Emit text message output
        text = response_text or ""
        msg = ResponseOutputMessage(
            content=[ResponseOutputText(text=text)]
        )
        msg_dict = msg.model_dump()

        # output_item.added (empty content)
        msg_added = dict(msg_dict)
        msg_added["status"] = "in_progress"
        msg_added["content"] = []
        yield _event("response.output_item.added", {
            "output_index": 0,
            "item": msg_added,
        })
        seq += 1

        # content_part.added
        yield _event("response.content_part.added", {
            "item_id": msg_dict["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        })
        seq += 1

        # output_text.delta — full text as one chunk
        if text:
            yield _event("response.output_text.delta", {
                "item_id": msg_dict["id"],
                "output_index": 0,
                "content_index": 0,
                "delta": text,
            })
            seq += 1

        # output_text.done
        yield _event("response.output_text.done", {
            "item_id": msg_dict["id"],
            "output_index": 0,
            "content_index": 0,
            "text": text,
        })
        seq += 1

        # content_part.done
        yield _event("response.content_part.done", {
            "item_id": msg_dict["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": text, "annotations": []},
        })
        seq += 1

        # output_item.done
        yield _event("response.output_item.done", {
            "output_index": 0,
            "item": msg_dict,
        })
        seq += 1

    # response.completed
    yield _event("response.completed", {"response": resp_dict})


@openai_router.post("/v1/responses")
async def create_response(
    request: ResponsesRequest,
    raw_request: Request,
):
    """
    OpenAI Responses API endpoint — compatible with Codex CLI.

    Accepts the Responses API format (flat tools, `input` field, `instructions`),
    translates to our internal format, sends to the browser, and returns a
    Responses-API-shaped response (or SSE stream).
    """
    # ── Validate ────────────────────────────────────────────
    if not request.input:
        raise HTTPException(status_code=400, detail="input cannot be empty")

    if Config.PROVIDER == "minimax" and _contains_attachment(request.input):
        raise HTTPException(
            status_code=501,
            detail="Attachments are not supported by the MiniMax provider.",
        )

    client = _get_client()
    model_id = _resolve_model_id(request.model)
    session_id = _extract_session_id(raw_request, getattr(request, "user", None))

    if session_id:
        page_cm = _get_session_manager().acquire_session_page(session_id)
        is_persistent = True
    else:
        @asynccontextmanager
        async def _stateless_cm():
            async with _get_page_pool().acquire_clean_page() as p:
                yield p, False
        page_cm = _stateless_cm()
        is_persistent = False

    async with page_cm as (page, is_first_turn):
        start_time = time.time()

        # ── Convert input to ChatMessage list ───────────────
        messages = _responses_input_to_messages(
            request.input, instructions=request.instructions
        )

        # ── Convert flat tools to nested format ─────────────
        chat_tools: list[ToolDefinition] | None = None
        has_tool_prompt = False
        if request.tools:
            raw_tools = [
                t.model_dump() if hasattr(t, "model_dump") else dict(t)
                for t in request.tools
            ]
            chat_tools = _responses_tools_to_chat_tools(raw_tools)
            if chat_tools and request.tool_choice != "none":
                tool_system = _build_tool_system_prompt(
                    chat_tools, tool_choice=request.tool_choice
                )
                messages.insert(
                    0, ChatMessage(role="system", content=tool_system)
                )
                has_tool_prompt = True

        if is_persistent:
            prompt, _ = _extract_persistent_prompt(
                messages, is_first_turn, has_tool_prompt
            )
        else:
            prompt = _build_prompt(messages)

        log.info(
            f"POST /v1/responses — model={model_id}, "
            f"{'persistent session=' + session_id if is_persistent else 'stateless'}, "
            f"prompt={len(prompt)} chars, stream={request.stream}"
        )

        # ── Send to provider ───────────────────────────────
        try:
            result = await client.send_message(
                prompt,
                model=model_id,
                stateless=not is_persistent,
                page=page,
            )
        except RuntimeError as e:
            err_msg = str(e).lower()
            if "error state" in err_msg or "could not find chat input" in err_msg:
                # Page has a DNS/navigation error or UI is broken — attempt recovery
                log.warning(f"Page error detected, attempting recovery: {e}")
                from src.api.server import _browser
                if _browser and await _browser.recover_page():
                    # Retry after recovery
                    try:
                        result = await client.send_message(
                            prompt,
                            model=model_id,
                            stateless=not Config.uses_browser(),
                        )
                    except Exception as e2:
                        log.error(f"Provider error after recovery: {e2}", exc_info=True)
                        raise HTTPException(
                            status_code=500, detail=f"Provider error: {str(e2)}"
                        )
                else:
                    raise HTTPException(
                        status_code=503, detail="Browser page is in error state and recovery failed"
                    )
            else:
                log.error(f"Provider error: {e}", exc_info=True)
                raise HTTPException(
                    status_code=500, detail=f"Provider error: {str(e)}"
                )
        except Exception as e:
            err_name = type(e).__name__
            # TargetClosedError means browser/page crashed — try recovery
            if "TargetClosed" in err_name or "closed" in str(e).lower():
                log.warning(f"Browser/page crashed ({err_name}), attempting recovery...")
                from src.api.server import _browser
                if _browser and await _browser.recover_page():
                    try:
                        result = await client.send_message(
                            prompt,
                            model=model_id,
                            stateless=not Config.uses_browser(),
                        )
                    except Exception as e2:
                        log.error(f"Provider error after crash recovery: {e2}", exc_info=True)
                        raise HTTPException(
                            status_code=500, detail=f"Provider error: {str(e2)}"
                        )
                else:
                    raise HTTPException(
                        status_code=503, detail=f"Browser crashed and recovery failed: {err_name}"
                    )
            else:
                log.error(f"Provider error: {e}", exc_info=True)
                raise HTTPException(
                    status_code=500, detail=f"Provider error: {str(e)}"
                )

        response_text = result.message
        elapsed_ms = int((time.time() - start_time) * 1000)

        # ── Detect echo ────────────────────────────────────
        _echo_markers = [
            "[System instruction:",
            "tool-calling mode",
            "Available functions:",
        ]
        if (
            Config.uses_browser()
            and response_text
            and has_tool_prompt
            and any(m in response_text for m in _echo_markers)
        ):
            log.warning(
                "Response appears to echo the sent prompt — retrying extraction"
            )
            try:
                await asyncio.sleep(1.5)
                if Config.PROVIDER == "claude":
                    from src.claude.detector import extract_last_response_via_copy
                else:
                    from src.chatgpt.detector import extract_last_response_via_copy

                retry_text = await extract_last_response_via_copy(client.page)
                if retry_text and not any(
                    m in retry_text for m in _echo_markers
                ):
                    response_text = retry_text
                    log.info(
                        f"Retry extraction succeeded: {len(response_text)} chars"
                    )
                else:
                    log.warning(
                        "Retry extraction still echoed — stripping system prefix"
                    )
                    idx = response_text.rfind("\n\n")
                    if idx > 0:
                        tail = response_text[idx:].strip()
                        if tail and not tail.startswith("["):
                            response_text = tail
            except Exception as e:
                log.warning(f"Retry extraction failed: {e}")

        # ── Check for tool calls ────────────────────────────
        tool_calls = None
        if has_tool_prompt and chat_tools:
            tool_calls = _parse_tool_calls(response_text, chat_tools)
            if tool_calls:
                response_text = None

        # ── Build response ──────────────────────────────────
        prompt_tokens = _estimate_tokens(prompt)
        completion_tokens = _estimate_tokens(response_text or "")

        resp = _build_response_object(
            response_text,
            tool_calls,
            request,
            prompt_tokens,
            completion_tokens,
            model_id,
        )

        log.info(
            f"Response: {elapsed_ms}ms, "
            f"tool_calls={len(tool_calls) if tool_calls else 0}, "
            f"tokens≈{resp.usage.total_tokens if resp.usage else 0}"
        )

        _increment_thread_count()

        # ── Stream or return ────────────────────────────────
        if request.stream:
            return StreamingResponse(
                _stream_response_events(resp, response_text, tool_calls),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        return resp.model_dump()


@openai_router.post("/v1/messages")
async def create_anthropic_message(request: Request):
    """
    Anthropic Messages API compatibility endpoint.
    Allows Claude Code CLI and other Anthropic SDK clients to use CatGPT Gateway.
    """
    body = await request.json()
    model = body.get("model", "claude-browser")
    messages = body.get("messages", [])
    system = body.get("system", "")
    stream = body.get("stream", False)

    chat_messages: list[ChatMessage] = []
    if system:
        if isinstance(system, list):
            sys_text = "\n".join(
                s.get("text", "") if isinstance(s, dict) else str(s)
                for s in system
            )
        else:
            sys_text = str(system)
        chat_messages.append(ChatMessage(role="system", content=sys_text))

    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    text_parts.append(part)
            content_str = "\n".join(text_parts)
        else:
            content_str = str(content)
        chat_messages.append(ChatMessage(role=role, content=content_str))

    client = _get_client()
    model_id = _resolve_model_id(model)
    session_id = _extract_session_id(request, body.get("user") or body.get("session_id"))

    if session_id:
        page_cm = _get_session_manager().acquire_session_page(session_id)
        is_persistent = True
    else:
        @asynccontextmanager
        async def _stateless_cm():
            async with _get_page_pool().acquire_clean_page() as p:
                yield p, False
        page_cm = _stateless_cm()
        is_persistent = False

    async with page_cm as (page, is_first_turn):
        start_time = time.time()
        if is_persistent:
            prompt, _ = _extract_persistent_prompt(chat_messages, is_first_turn)
        else:
            prompt = _build_prompt(chat_messages)

        log.info(
            f"POST /v1/messages — model={model_id}, "
            f"{'persistent session=' + session_id if is_persistent else 'stateless'}, "
            f"prompt={len(prompt)} chars"
        )

        chat_resp = await client.send_message(
            prompt, stateless=not is_persistent, page=page
        )
        response_text = chat_resp.message
        elapsed_ms = int((time.time() - start_time) * 1000)

        prompt_tokens = _estimate_tokens(prompt)
        completion_tokens = _estimate_tokens(response_text)
        _increment_thread_count()

        log.info(
            f"Anthropic Response: {elapsed_ms}ms, "
            f"tokens≈{prompt_tokens + completion_tokens}"
        )

        msg_id = f"msg_{uuid.uuid4().hex[:24]}"

        if stream:
            async def _stream_anthropic_events():
                # 1. message_start
                yield (
                    "event: message_start\n"
                    f"data: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'model': model_id, 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': prompt_tokens, 'output_tokens': 1}}})}\n\n"
                )
                # 2. content_block_start
                yield (
                    "event: content_block_start\n"
                    f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                )
                # 3. content_block_delta chunks
                chunk_size = 40
                for i in range(0, len(response_text), chunk_size):
                    chunk = response_text[i : i + chunk_size]
                    yield (
                        "event: content_block_delta\n"
                        f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': chunk}})}\n\n"
                    )
                    await asyncio.sleep(0.01)
                # 4. content_block_stop
                yield (
                    "event: content_block_stop\n"
                    f"data: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                )
                # 5. message_delta
                yield (
                    "event: message_delta\n"
                    f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': completion_tokens}})}\n\n"
                )
                # 6. message_stop
                yield (
                    "event: message_stop\n"
                    f"data: {json.dumps({'type': 'message_stop'})}\n\n"
                )

            return StreamingResponse(
                _stream_anthropic_events(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        return {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model_id,
            "content": [
                {
                    "type": "text",
                    "text": response_text,
                }
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
            },
        }

