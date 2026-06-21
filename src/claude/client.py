"""
Claude client — core interaction logic for claude.ai.

Sends messages, waits for responses, manages conversations.
Handles selector fallbacks and integrates human-like behavior.

Same interface as ChatGPTClient so the API layer is provider-agnostic.
"""

from __future__ import annotations

import asyncio
import re
import time

from patchright.async_api import Page

from src.config import Config
from src.claude.selectors import ClaudeSelectors
from src.browser.human import human_type, human_click, random_delay
from src.claude.detector import (
    wait_for_response_complete,
    extract_last_response_via_copy,
    count_assistant_messages,
    get_latest_assistant_turn_signature,
    is_incomplete_response_text,
)
from src.chatgpt.models import ChatResponse
from src.log import setup_logging

log = setup_logging("claude_client")


class ClaudeClient:
    """
    High-level client for interacting with the Claude web interface.

    Requires a Playwright Page that is already logged in and on claude.ai.
    Same interface as ChatGPTClient for provider-agnostic API usage.
    """

    def __init__(self, page: Page) -> None:
        self._page = page

    @property
    def page(self) -> Page:
        return self._page

    # ── Core: Send & Receive ────────────────────────────────────

    async def send_message(
        self,
        text: str,
        image_paths: list[str] | None = None,
        file_paths: list[str] | None = None,
        read_aloud: bool = False,
    ) -> ChatResponse:
        """
        Send a message to Claude and wait for the complete response.

        Args:
            text: The message text to send.
            image_paths: Optional list of local file paths to images to attach.
            file_paths: Optional list of local file paths to non-image files.
            read_aloud: Accepted for API compatibility. Not currently supported for Claude.

        Returns ChatResponse with the assistant's reply and metadata.
        """
        all_attachments = (image_paths or []) + (file_paths or [])
        log.info(f"Sending message ({len(text)} chars, {len(all_attachments)} attachments): {text[:80]}...")
        start_time = time.time()

        # 0. Count existing assistant messages so we know when a new one appears
        pre_count = await count_assistant_messages(self._page)
        pre_turn_signature = await get_latest_assistant_turn_signature(self._page)
        log.debug(f"Assistant messages before send: {pre_count}")
        log.debug(f"Latest assistant turn before send: {pre_turn_signature}")

        # 1. Brief pause (human would take a moment to start typing)
        await random_delay(250, 700)

        # 1.5. Upload files/images if provided
        if all_attachments:
            await self._upload_files(all_attachments)

        # 2. Find the chat input
        input_selector = await self._find_selector(ClaudeSelectors.CHAT_INPUT, "chat input")
        if not input_selector:
            raise RuntimeError("Could not find chat input element")

        # 3. Paste the message
        await human_type(self._page, input_selector, text)

        # Small pause after pasting
        await random_delay(150, 350)

        # 4. Send the message
        sent = await self._click_send()
        if not sent:
            # Fallback: try pressing Enter
            log.info("Send button not found, trying Enter key")
            await self._page.keyboard.press("Enter")

        # 5. Wait for response
        log.info("Waiting for Claude response...")
        expected_count = pre_count + 1
        completed = await wait_for_response_complete(
            self._page,
            expected_msg_count=expected_count,
            previous_turn_signature=pre_turn_signature,
        )

        if not completed:
            log.warning("Response may not be complete (timeout)")

        # Small buffer after completion to let DOM settle
        await asyncio.sleep(0.5)

        # 6. Extract text content (Claude doesn't generate images like DALL-E)
        response_text = await extract_last_response_via_copy(
            self._page,
            previous_turn_signature=pre_turn_signature,
        )

        # If we only captured a transient status, retry
        if is_incomplete_response_text(response_text):
            log.warning("Extracted text looks incomplete/transient; retrying for final answer")
            for attempt in range(1, 3):
                await asyncio.sleep(4)
                await wait_for_response_complete(
                    self._page,
                    timeout_ms=90000,
                    previous_turn_signature=pre_turn_signature,
                )
                retry_text = await extract_last_response_via_copy(
                    self._page,
                    previous_turn_signature=pre_turn_signature,
                )

                if retry_text and not is_incomplete_response_text(retry_text):
                    response_text = retry_text
                    log.info(f"Recovered final response text on retry {attempt}")
                    break

                if retry_text:
                    response_text = retry_text
                log.warning(f"Retry {attempt} still incomplete/transient")

        elapsed_ms = int((time.time() - start_time) * 1000)
        thread_id = self._extract_thread_id()
        if read_aloud:
            log.warning("read_aloud=True requested, but Claude read-aloud audio is not implemented")

        log.info(
            f"Response received ({elapsed_ms}ms, {len(response_text)} chars): "
            f"{response_text[:80]}..."
        )

        return ChatResponse(
            message=response_text,
            thread_id=thread_id,
            response_time_ms=elapsed_ms,
            images=[],
            has_images=False,
            audio=None,
            has_audio=False,
        )

    # ── Navigation ──────────────────────────────────────────────

    async def new_chat(self) -> None:
        """Start a new conversation by navigating to /new."""
        log.info("Starting new chat...")
        url = Config.CLAUDE_URL.rstrip("/") + "/new"
        await self._page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(1.5)

        # Wait for the chat input to be visible
        for selector in ClaudeSelectors.CHAT_INPUT:
            try:
                await self._page.wait_for_selector(selector, timeout=10000, state="visible")
                log.debug(f"Chat input ready: {selector}")
                break
            except Exception:
                continue

        await random_delay(300, 600)
        log.info("New chat started (navigated to /new)")

    async def navigate_to_thread(self, thread_id: str) -> None:
        """Navigate to an existing conversation thread."""
        url = f"{Config.CLAUDE_URL.rstrip('/')}/chat/{thread_id}"
        log.info(f"Navigating to thread: {thread_id}")
        await self._page.goto(url, wait_until="domcontentloaded")
        await random_delay(800, 1500)
        log.info(f"Thread {thread_id} loaded")

    async def get_current_thread_url(self) -> str:
        """Get the current page URL (contains thread ID if in a conversation)."""
        return self._page.url

    # ── Sidebar ─────────────────────────────────────────────────

    async def list_threads(self) -> list[dict]:
        """
        Scrape the sidebar for recent conversation threads.

        Returns a list of dicts: [{id, title, url}, ...]
        """
        threads = []
        for selector in ClaudeSelectors.SIDEBAR_THREAD_LINKS:
            try:
                elements = await self._page.query_selector_all(selector)
                for el in elements:
                    href = await el.get_attribute("href") or ""
                    title = (await el.inner_text()).strip()
                    # Claude uses /chat/{uuid}
                    match = re.search(r"/chat/([a-f0-9-]+)", href)
                    if match:
                        threads.append({
                            "id": match.group(1),
                            "title": title,
                            "url": f"{Config.CLAUDE_URL.rstrip('/')}{href}",
                        })
                if threads:
                    break
            except Exception as e:
                log.debug(f"Sidebar scrape with {selector} failed: {e}")

        log.info(f"Found {len(threads)} threads in sidebar")
        return threads

    # ── Private Helpers ─────────────────────────────────────────

    async def _find_selector(self, selectors: list[str], name: str) -> str | None:
        """Try each selector in the fallback list. Return the first one that matches."""
        for selector in selectors:
            try:
                el = await self._page.wait_for_selector(
                    selector,
                    timeout=Config.SELECTOR_TIMEOUT,
                    state="visible",
                )
                if el:
                    log.debug(f"Found {name} via: {selector}")
                    return selector
            except Exception:
                log.debug(f"Selector miss for {name}: {selector}")
                continue

        log.warning(f"No working selector found for: {name}")
        return None

    async def _click_send(self) -> bool:
        """Try to click the send button using selector fallbacks."""
        selector = await self._find_selector(ClaudeSelectors.SEND_BUTTON, "send button")
        if selector:
            await human_click(self._page, selector)
            log.debug("Send button clicked")
            return True
        return False

    async def _upload_files(self, file_paths: list[str]) -> None:
        """Upload files to Claude's input area."""
        from pathlib import Path

        valid_paths = []
        for p in file_paths:
            path = Path(p)
            if path.exists() and path.is_file():
                valid_paths.append(str(path.resolve()))
            else:
                log.warning(f"File not found, skipping: {p}")

        if not valid_paths:
            log.warning("No valid files to upload")
            return

        log.info(f"Uploading {len(valid_paths)} file(s)...")

        # Find the file input element
        file_input = None
        for selector in ClaudeSelectors.FILE_UPLOAD_INPUT:
            try:
                elements = await self._page.query_selector_all(selector)
                if elements:
                    file_input = elements[0]
                    log.debug(f"Found file input: {selector}")
                    break
            except Exception:
                continue

        if file_input:
            await file_input.set_input_files(valid_paths)
            log.info(f"Set {len(valid_paths)} file(s) on file input")
        else:
            log.info("No file input found via selectors, trying broad input[type=file]")
            try:
                await self._page.set_input_files("input[type='file']", valid_paths)
                log.info(f"Set {len(valid_paths)} file(s) via broad selector")
            except Exception as e:
                log.error(f"Failed to upload files: {e}")
                raise RuntimeError(f"Could not upload files: {e}")

        # Wait for files to be processed
        await asyncio.sleep(3)
        if len(valid_paths) > 1:
            await asyncio.sleep(len(valid_paths))
        log.info("File upload complete")

    def _extract_thread_id(self) -> str:
        """Extract the thread/conversation ID from the current URL."""
        url = self._page.url
        # Claude uses /chat/{uuid}
        match = re.search(r"/chat/([a-f0-9-]+)", url)
        return match.group(1) if match else ""
