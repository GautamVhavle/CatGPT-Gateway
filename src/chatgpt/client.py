"""
ChatGPT client — core interaction logic.

Sends messages, waits for responses, manages conversations.
Handles selector fallbacks and integrates human-like behavior.
"""

from __future__ import annotations

import asyncio
import re
import time

from patchright.async_api import Page

from src.config import Config
from src.selectors import Selectors
from src.browser.human import human_type, human_click, thinking_pause, random_delay
from src.chatgpt.detector import (
    wait_for_response_complete,
    extract_last_response_via_copy,
    count_assistant_messages,
    get_latest_assistant_turn_signature,
    is_incomplete_response_text,
)
from src.chatgpt.image_handler import extract_images_from_response
from src.chatgpt.models import ChatResponse
from src.log import setup_logging

log = setup_logging("chatgpt_client")


class ChatGPTClient:
    """
    High-level client for interacting with the ChatGPT web interface.

    Requires a Playwright Page that is already logged in and on chatgpt.com.
    """

    def __init__(self, page: Page) -> None:
        self._page = page
        self._setup_network_logging()

    def _setup_network_logging(self) -> None:
        """Monitor network requests, WebSockets, and JS errors for debugging."""
        # Only log important API calls at INFO; sentinel/ping/heartbeat at DEBUG
        _important_paths = ("/f/conversation", "/conversations?", "/stream_status")

        def on_request(request):
            url = request.url
            if "backend-api" in url:
                if any(p in url for p in _important_paths):
                    log.info(f"NET REQ: {request.method} {url[:200]}")
                else:
                    log.debug(f"NET REQ: {request.method} {url[:200]}")

        async def on_response(response):
            url = response.url
            if "backend-api" in url:
                if any(p in url for p in _important_paths):
                    log.info(f"NET RESP: {response.status} {url[:200]}")
                else:
                    log.debug(f"NET RESP: {response.status} {url[:200]}")

        def on_request_failed(request):
            url = request.url
            failure = request.failure or "unknown"
            if "chrome-extension" not in url and "favicon" not in url:
                # Patchright internal injection is expected to fail
                if "patchright" in url:
                    log.debug(f"NET FAIL: {url[:150]} — {failure}")
                else:
                    log.warning(f"NET FAIL: {url[:150]} — {failure}")

        def on_console(msg):
            if msg.type == "error":
                log.info(f"JS ERROR: {msg.text[:300]}")
            elif msg.type == "warning":
                log.debug(f"JS WARNING: {msg.text[:300]}")

        def on_page_error(error):
            log.error(f"JS PAGE ERROR: {error}")

        def on_websocket(ws):
            log.debug(f"WS OPEN: {ws.url[:200]}")
            ws.on("framereceived", lambda payload: log.debug(f"WS RECV: {str(payload)[:200]}"))
            ws.on("framesent", lambda payload: log.debug(f"WS SEND: {str(payload)[:200]}"))
            ws.on("close", lambda _: log.debug(f"WS CLOSE: {ws.url[:200]}"))

        self._page.on("request", on_request)
        self._page.on("response", on_response)
        self._page.on("requestfailed", on_request_failed)
        self._page.on("console", on_console)
        self._page.on("pageerror", on_page_error)
        self._page.on("websocket", on_websocket)

    @property
    def page(self) -> Page:
        return self._page

    # ── Core: Send & Receive ────────────────────────────────────

    async def send_message(
        self,
        text: str,
        image_paths: list[str] | None = None,
        file_paths: list[str] | None = None,
        model: str | None = None,
    ) -> ChatResponse:
        """
        Send a message to ChatGPT and wait for the complete response.

        Args:
            text: The message text to send.
            image_paths: Optional list of local file paths to images to attach.
            file_paths: Optional list of local file paths to non-image files (PDF, etc.).
            model: Optional API model identifier. Browser selection is unchanged.

        Steps:
        1. Simulate thinking pause
        2. Upload images if provided
        3. Find and focus chat input
        4. Type message with human-like delays
        5. Click send
        6. Wait for response to complete
        7. Extract and return the response

        Returns ChatResponse with the assistant's reply and metadata.
        """
        all_attachments = (image_paths or []) + (file_paths or [])
        log.info(f"Sending message ({len(text)} chars, {len(all_attachments)} attachments): {text[:80]}...")
        start_time = time.time()

        # 0. Check page health — recover from DNS errors before trying to send
        page_error = await self._detect_page_error()
        if page_error:
            log.warning(f"Page error detected before send: {page_error}")
            raise RuntimeError(f"Page is in error state: {page_error}")

        # 0.5 Count existing assistant messages so we know when a new one appears
        pre_count = await count_assistant_messages(self._page)
        pre_turn_signature = await get_latest_assistant_turn_signature(self._page)
        log.debug(f"Assistant messages before send: {pre_count}")
        log.debug(f"Latest assistant turn before send: {pre_turn_signature}")

        # 0.5 Check for and dismiss any blocking dialogs/overlays
        await self._dismiss_overlays()

        # 1. Brief pause (human would take a moment to start typing)
        await random_delay(100, 300)

        # 1.5. Upload files/images if provided
        if all_attachments:
            await self._upload_files(all_attachments)

        # 2. Find the chat input (retry once after dismissing overlays if not found)
        input_selector = await self._find_selector(Selectors.CHAT_INPUT, "chat input")
        if not input_selector:
            # An overlay may have blocked it — dismiss and retry
            log.info("Chat input not found on first try, dismissing overlays and retrying...")
            await self._dismiss_overlays()
            await asyncio.sleep(1)
            input_selector = await self._find_selector(Selectors.CHAT_INPUT, "chat input")
        if not input_selector:
            raise RuntimeError("Could not find chat input element")

        # 3. Paste the message (all at once)
        await human_type(self._page, input_selector, text)

        # 4. Poll briefly for auto-submit (execCommand can trigger
        #    f/conversation automatically in the current frontend).
        #    If a new assistant turn appeared, skip the send button click.
        auto_submitted = False
        for _ in range(6):  # poll up to ~3s in 0.5s intervals
            await asyncio.sleep(0.5)
            post_count = await count_assistant_messages(self._page)
            if post_count > pre_count:
                auto_submitted = True
                break

        if auto_submitted:
            log.info("ChatGPT auto-submitted after text entry — skipping send button click")
        else:
            # No auto-submit — click the send button
            log.info("No auto-submit detected, clicking send button")
            sent = await self._click_send()
            if not sent:
                log.info("Send button not found, trying Enter key")
                await self._page.keyboard.press("Enter")

        # 5. Wait for response with message count awareness
        log.info("Waiting for ChatGPT response...")
        expected_count = pre_count + 1
        completed = await wait_for_response_complete(
            self._page,
            expected_msg_count=expected_count,
            previous_turn_signature=pre_turn_signature,
        )

        if not completed:
            log.warning("Response may not be complete (timeout)")

        # Small buffer after completion to let DOM settle
        await asyncio.sleep(0.2)

        # 6. Check for generated images in the response FIRST
        #    (image turns have no copy button, so we must detect images
        #    before trying copy-button extraction)
        images = await extract_images_from_response(self._page)
        has_images = len(images) > 0

        # 7. Extract text content
        if has_images:
            # Image responses don't have a copy button — extract text
            # from the turn's DOM instead (will get the image title/desc)
            response_text = await self._extract_image_turn_text(pre_turn_signature)
            log.info(f"Response contains {len(images)} generated image(s)")
            for img in images:
                log.info(f"  Image: {img.alt or img.prompt_title} → {img.local_path}")
        else:
            # Standard text response — use copy button (most reliable)
            response_text = await extract_last_response_via_copy(
                self._page,
                previous_turn_signature=pre_turn_signature,
            )

            # If extraction returned empty, retry a few times (DOM may not be settled)
            if not response_text.strip():
                log.warning("Empty response extracted — retrying after short wait")
                for retry in range(1, 4):
                    await asyncio.sleep(1.5 * retry)
                    response_text = await extract_last_response_via_copy(
                        self._page,
                        previous_turn_signature=pre_turn_signature,
                    )
                    if response_text.strip():
                        log.info(f"Got response on extraction retry {retry}")
                        break

            # If we only captured a transient status (e.g. "Pro thinking"),
            # keep waiting and retry extraction on the same new turn.
            if is_incomplete_response_text(response_text):
                log.warning("Extracted text looks incomplete/transient; retrying for final answer")
                for attempt in range(1, 3):
                    await asyncio.sleep(2)
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

        log.info(
            f"Response received ({elapsed_ms}ms, {len(response_text)} chars"
            f"{f', {len(images)} images' if has_images else ''}): "
            f"{response_text[:80]}..."
        )

        return ChatResponse(
            message=response_text,
            thread_id=thread_id,
            response_time_ms=elapsed_ms,
            images=images,
            has_images=has_images,
        )

    # ── Navigation ──────────────────────────────────────────────

    async def new_chat(self) -> None:
        """Start a new conversation.

        Strategy order:
        1. SPA button click (avoids DNS issues, preserves browser state)
        2. JavaScript location change (no DNS lookup needed if page is loaded)
        3. Full page.goto() (last resort — may fail with DNS errors)
        """
        # Already on a fresh chat — nothing to do
        if "chatgpt.com" in self._page.url:
            try:
                turn_count = await self._page.evaluate(
                    "document.querySelectorAll('[data-testid^=\"conversation-turn-\"]').length"
                )
                if turn_count == 0:
                    log.info("Already on a fresh chat — skipping navigation")
                    return
            except Exception:
                pass

        # Strategy 1: SPA button click
        for selector in Selectors.NEW_CHAT_BUTTON:
            try:
                btn = await self._page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click()
                    log.info(f"New chat via SPA button: {selector}")
                    await asyncio.sleep(1)
                    # Verify we're on a fresh chat
                    try:
                        turn_count = await self._page.evaluate(
                            "document.querySelectorAll('[data-testid^=\"conversation-turn-\"]').length"
                        )
                        if turn_count == 0:
                            await self._wait_for_chat_input()
                            return
                    except Exception:
                        pass
            except Exception:
                continue

        # Strategy 2: JavaScript navigation (avoids DNS lookup)
        try:
            log.info("New chat via JS navigation...")
            await self._page.evaluate("window.location.href = '/'")
            await self._page.wait_for_load_state("domcontentloaded", timeout=15000)
            page_error = await self._detect_page_error()
            if not page_error:
                log.info("New chat started (JS navigation)")
                await self._wait_for_chat_input()
                return
        except Exception as e:
            log.warning(f"JS navigation failed: {e}")

        # Strategy 3: Full page.goto() — last resort
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            log.info(f"New chat via page.goto (attempt {attempt}/{max_attempts})...")
            try:
                await self._page.goto(
                    Config.CHATGPT_URL,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as e:
                log.warning(f"page.goto failed (attempt {attempt}): {e}")
                if attempt < max_attempts:
                    await asyncio.sleep(attempt * 3)
                    continue
                raise

            page_error = await self._detect_page_error()
            if page_error:
                log.error(f"Page error after goto (attempt {attempt}): {page_error}")
                if attempt < max_attempts:
                    await asyncio.sleep(attempt * 3)
                    continue
                raise RuntimeError(f"Page error persists after {max_attempts} attempts: {page_error}")

            log.info("New chat started (page.goto)")
            await self._wait_for_chat_input()
            return

    async def _wait_for_chat_input(self) -> None:
        """Wait for the chat input to become visible and interactive."""
        for selector in Selectors.CHAT_INPUT:
            try:
                await self._page.wait_for_selector(selector, timeout=10000, state="visible")
                log.debug(f"Chat input ready: {selector}")
                # Brief settle for React handlers to attach
                await asyncio.sleep(0.5)
                return
            except Exception:
                continue
        log.warning("Chat input not found — page may not be fully ready")

    async def _detect_page_error(self) -> str | None:
        """Check if the current page shows a browser or ChatGPT error."""
        try:
            return await self._page.evaluate(
                """
                () => {
                    const body = document.body ? document.body.innerText : '';
                    const title = document.title || '';
                    if (body.includes('DNS_PROBE_FINISHED_NXDOMAIN')) return 'DNS_PROBE_FINISHED_NXDOMAIN';
                    if (body.includes('ERR_NAME_NOT_RESOLVED')) return 'ERR_NAME_NOT_RESOLVED';
                    if (body.includes('ERR_CONNECTION_REFUSED')) return 'ERR_CONNECTION_REFUSED';
                    if (body.includes('ERR_INTERNET_DISCONNECTED')) return 'ERR_INTERNET_DISCONNECTED';
                    if (body.includes('ERR_CONNECTION_TIMED_OUT')) return 'ERR_CONNECTION_TIMED_OUT';
                    if (title.includes("can't be reached") || title.includes("is not available"))
                        return 'page_unreachable';
                    if (body.includes('Something went wrong')) return 'ChatGPT_error';
                    return null;
                }
                """
            )
        except Exception:
            return None

    async def navigate_to_thread(self, thread_id: str) -> None:
        """Navigate to an existing conversation thread."""
        url = f"{Config.CHATGPT_URL}/c/{thread_id}"
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
        for selector in Selectors.SIDEBAR_THREAD_LINKS:
            try:
                elements = await self._page.query_selector_all(selector)
                for el in elements:
                    href = await el.get_attribute("href") or ""
                    title = (await el.inner_text()).strip()
                    match = re.search(r"/c/([a-f0-9-]+)", href)
                    if match:
                        threads.append({
                            "id": match.group(1),
                            "title": title,
                            "url": f"{Config.CHATGPT_URL}{href}",
                        })
                if threads:
                    break
            except Exception as e:
                log.debug(f"Sidebar scrape with {selector} failed: {e}")

        log.info(f"Found {len(threads)} threads in sidebar")
        return threads

    # ── Private Helpers ─────────────────────────────────────────

    async def _extract_image_turn_text(self, previous_turn_signature: str | None = None) -> str:
        """
        Extract any text content from the latest turn (for image responses).

        Image turns may contain a title/description like:
        "Creating image • Adorable orange tabby kitten close-up"
        """
        text = await self._page.evaluate("""
            (previousSignature) => {
                const turns = document.querySelectorAll('section[data-testid^="conversation-turn-"]');
                if (turns.length === 0) return '';

                let last = null;
                for (let idx = turns.length - 1; idx >= 0; idx--) {
                    const turn = turns[idx];
                    const turnRole = turn.getAttribute('data-turn');
                    const hasAssistantRole = turnRole === 'assistant' ||
                        Boolean(turn.querySelector('[data-message-author-role="assistant"]'));
                    if (!hasAssistantRole) continue;

                    const stableId =
                        turn.getAttribute('data-turn-id') ||
                        turn.getAttribute('data-testid') ||
                        turn.id ||
                        '';
                    const signature = `${idx}:${stableId}`;
                    if (previousSignature && signature === previousSignature) {
                        return '';
                    }

                    last = turn;
                    break;
                }

                if (!last) return '';

                // Try to get descriptive text (not "ChatGPT said:" heading)
                const spans = last.querySelectorAll('span');
                const parts = [];
                for (const span of spans) {
                    const t = (span.innerText || '').trim();
                    if (t && t.length > 3 && t.length < 300 &&
                        !t.includes('ChatGPT') && !t.includes('said')) {
                        parts.push(t);
                    }
                }
                if (parts.length > 0) return parts.join(' ');

                // Fallback: full turn inner text
                const full = (last.innerText || '').trim();
                // Strip the "ChatGPT said:" prefix
                return full.replace(/^ChatGPT said:\\s*/i, '').trim();
            }
        """, previous_turn_signature)
        return text or ""

    async def _find_selector(self, selectors: list[str], name: str) -> str | None:
        """
        Try each selector in the fallback list. Return the first one that matches.
        """
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

    async def _dismiss_overlays(self) -> None:
        """Check for and dismiss any blocking dialogs/overlays on the page."""
        try:
            result = await self._page.evaluate(
                """
                () => {
                    const info = { dismissed: [], found: [] };

                    // Check for role="dialog" overlays
                    const dialogs = document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog[open]');
                    for (const d of dialogs) {
                        const text = (d.innerText || '').trim().substring(0, 200);
                        info.found.push('dialog: ' + text);

                        // Try to find and click dismiss/close buttons
                        const closeBtn = d.querySelector(
                            'button[aria-label="Close"], button[aria-label="Dismiss"], ' +
                            'button:has(svg[data-testid="close"]), button.close'
                        );
                        if (closeBtn) {
                            closeBtn.click();
                            info.dismissed.push('dialog-close');
                        }
                    }

                    // Check for "Continue generating" button
                    const allButtons = document.querySelectorAll('button');
                    for (const btn of allButtons) {
                        const btnText = (btn.innerText || '').trim().toLowerCase();
                        if (btnText.includes('continue generating')) {
                            btn.click();
                            info.dismissed.push('continue-generating');
                        }
                    }

                    // Check for rate limit or error banners
                    const banners = document.querySelectorAll('[class*="banner"], [class*="toast"], [class*="alert"]');
                    for (const b of banners) {
                        const text = (b.innerText || '').trim().substring(0, 200);
                        if (text) info.found.push('banner: ' + text);
                    }

                    return info;
                }
                """
            )
            if result and isinstance(result, dict):
                if result.get("dismissed"):
                    log.info(f"Dismissed overlays: {result['dismissed']}")
                if result.get("found"):
                    log.debug(f"Page overlays found: {result['found']}")
        except Exception as e:
            log.debug(f"Overlay check failed: {e}")

    async def _click_send(self) -> bool:
        """Try to click the send button using selector fallbacks."""
        # Check send button state before clicking
        btn_state = await self._page.evaluate(
            """
            () => {
                const selectors = [
                    'button[data-testid="send-button"]',
                    '#composer-submit-button',
                    "button[aria-label='Send prompt']",
                ];
                for (const sel of selectors) {
                    const btn = document.querySelector(sel);
                    if (btn) {
                        return {
                            selector: sel,
                            disabled: btn.disabled,
                            ariaDisabled: btn.getAttribute('aria-disabled'),
                            visible: btn.offsetParent !== null,
                            classes: btn.className.substring(0, 100),
                        };
                    }
                }
                return null;
            }
            """
        )
        log.debug(f"Send button state: {btn_state}")

        # Don't click a disabled send button — the input wasn't recognized
        if isinstance(btn_state, dict) and btn_state.get("disabled"):
            log.warning("Send button is disabled — text may not have been inserted properly")
            return False

        selector = await self._find_selector(Selectors.SEND_BUTTON, "send button")
        if selector:
            await human_click(self._page, selector)
            log.info(f"Send button clicked via: {selector}")
            return True
        return False

    async def _upload_files(self, file_paths: list[str]) -> None:
        """
        Upload files (images, PDFs, docs, etc.) to ChatGPT's input area.

        ChatGPT has a hidden <input type="file"> that accepts various file types.
        We set files on it directly (like drag-and-drop / file picker).
        """
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

        # Find the file input element — ChatGPT has a hidden <input type="file">
        file_input = None
        for selector in Selectors.FILE_UPLOAD_INPUT:
            try:
                elements = await self._page.query_selector_all(selector)
                if elements:
                    file_input = elements[0]
                    log.debug(f"Found file input: {selector}")
                    break
            except Exception:
                continue

        if file_input:
            # Set files directly on the input element
            await file_input.set_input_files(valid_paths)
            log.info(f"Set {len(valid_paths)} file(s) on file input")
        else:
            # Fallback: use page.set_input_files with a broad selector
            log.info("No file input found via selectors, trying broad input[type=file]")
            try:
                await self._page.set_input_files("input[type='file']", valid_paths)
                log.info(f"Set {len(valid_paths)} file(s) via broad selector")
            except Exception as e:
                log.error(f"Failed to upload files: {e}")
                raise RuntimeError(f"Could not upload files: {e}")

        # Wait for files to be processed/attached (thumbnails/badges appear)
        await asyncio.sleep(3)
        # Additional wait if multiple files
        if len(valid_paths) > 1:
            await asyncio.sleep(len(valid_paths))
        log.info("File upload complete")

    def _extract_thread_id(self) -> str:
        """Extract the thread/conversation ID from the current URL."""
        url = self._page.url
        match = re.search(r"/c/([a-f0-9-]+)", url)
        return match.group(1) if match else ""
