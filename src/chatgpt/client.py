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
        stateless: bool = False,
        page: Page | None = None,
    ) -> ChatResponse:
        """
        Send a message to ChatGPT and wait for the complete response.
        """
        target_page = page or self._page
        all_attachments = (image_paths or []) + (file_paths or [])
        log.info(f"Sending message ({len(text)} chars, {len(all_attachments)} attachments): {text[:80]}...")
        start_time = time.time()

        # 0. Check page health
        page_error = await self._detect_page_error(target_page)
        if page_error:
            log.warning(f"Page error detected before send: {page_error}")
            raise RuntimeError(f"Page is in error state: {page_error}")

        # 0.5 Count existing assistant messages
        pre_count = await count_assistant_messages(target_page)
        pre_turn_signature = await get_latest_assistant_turn_signature(target_page)
        log.debug(f"Assistant messages before send: {pre_count}")
        log.debug(f"Latest assistant turn before send: {pre_turn_signature}")

        # 0.5 Dismiss blocking dialogs
        await self._dismiss_overlays(target_page)

        # 1. Brief pause
        await random_delay(100, 300)

        # 1.5. Upload files/images if provided
        if all_attachments:
            await self._upload_files(all_attachments, target_page)

        # 2. Find the chat input
        input_selector = await self._find_selector(Selectors.CHAT_INPUT, "chat input", target_page)
        if not input_selector:
            log.info("Chat input not found on first try, dismissing overlays and retrying...")
            await self._dismiss_overlays(target_page)
            await asyncio.sleep(1)
            input_selector = await self._find_selector(Selectors.CHAT_INPUT, "chat input", target_page)
        if not input_selector:
            raise RuntimeError("Could not find chat input element")

        # 3. Paste the message
        await human_type(target_page, input_selector, text)

        # 4. Poll briefly for auto-submit
        auto_submitted = False
        for _ in range(6):
            await asyncio.sleep(0.5)
            post_count = await count_assistant_messages(target_page)
            if post_count > pre_count:
                auto_submitted = True
                break

        if auto_submitted:
            log.info("ChatGPT auto-submitted after text entry — skipping send button click")
        else:
            log.info("No auto-submit detected, clicking send button")
            sent = await self._click_send(target_page)
            if not sent:
                log.info("Send button not found, trying Enter key")
                await target_page.keyboard.press("Enter")

        # 5. Wait for response
        log.info("Waiting for ChatGPT response...")
        expected_count = pre_count + 1
        completed = await wait_for_response_complete(
            target_page,
            expected_msg_count=expected_count,
            previous_turn_signature=pre_turn_signature,
        )

        if not completed:
            log.warning("Response may not be complete (timeout)")

        await asyncio.sleep(0.2)

        # 6. Check for generated images
        images = await extract_images_from_response(target_page)
        has_images = len(images) > 0

        # 7. Extract text content
        if has_images:
            response_text = await self._extract_image_turn_text(pre_turn_signature, target_page)
            log.info(f"Response contains {len(images)} generated image(s)")
            for img in images:
                log.info(f"  Image: {img.alt or img.prompt_title} → {img.local_path}")
        else:
            response_text = await extract_last_response_via_copy(
                target_page,
                previous_turn_signature=pre_turn_signature,
            )

            if not response_text.strip():
                log.warning("Empty response extracted — retrying after short wait")
                for retry in range(1, 4):
                    await asyncio.sleep(1.5 * retry)
                    response_text = await extract_last_response_via_copy(
                        target_page,
                        previous_turn_signature=pre_turn_signature,
                    )
                    if response_text.strip():
                        log.info(f"Got response on extraction retry {retry}")
                        break

            if is_incomplete_response_text(response_text):
                log.warning("Extracted text looks incomplete/transient; retrying for final answer")
                for attempt in range(1, 3):
                    await asyncio.sleep(2)
                    await wait_for_response_complete(
                        target_page,
                        timeout_ms=90000,
                        previous_turn_signature=pre_turn_signature,
                    )
                    retry_text = await extract_last_response_via_copy(
                        target_page,
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
        thread_id = self._extract_thread_id(target_page)

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

    async def _detect_page_error(self, page: Page | None = None) -> str | None:
        """Check if the current page shows a browser or ChatGPT error."""
        p = page or self._page
        try:
            return await p.evaluate(
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

    async def _extract_image_turn_text(
        self,
        previous_turn_signature: str | None = None,
        page: Page | None = None,
    ) -> str:
        p = page or self._page
        text = await p.evaluate("""
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

                const full = (last.innerText || '').trim();
                return full.replace(/^ChatGPT said:\\s*/i, '').trim();
            }
        """, previous_turn_signature)
        return text or ""

    async def _find_selector(
        self,
        selectors: list[str],
        name: str,
        page: Page | None = None,
    ) -> str | None:
        p = page or self._page
        for selector in selectors:
            try:
                el = await p.wait_for_selector(
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

    async def _dismiss_overlays(self, page: Page | None = None) -> None:
        p = page or self._page
        try:
            result = await p.evaluate(
                """
                () => {
                    const info = { dismissed: [], found: [] };
                    const dialogs = document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog[open]');
                    for (const d of dialogs) {
                        const text = (d.innerText || '').trim().substring(0, 200);
                        info.found.push('dialog: ' + text);
                        const closeBtn = d.querySelector(
                            'button[aria-label="Close"], button[aria-label="Dismiss"], ' +
                            'button:has(svg[data-testid="close"]), button.close'
                        );
                        if (closeBtn) {
                            closeBtn.click();
                            info.dismissed.push('dialog-close');
                        }
                    }
                    const allButtons = document.querySelectorAll('button');
                    for (const btn of allButtons) {
                        const btnText = (btn.innerText || '').trim().toLowerCase();
                        if (btnText.includes('continue generating')) {
                            btn.click();
                            info.dismissed.push('continue-generating');
                        }
                    }
                    return info;
                }
                """
            )
            if result and isinstance(result, dict):
                if result.get("dismissed"):
                    log.info(f"Dismissed overlays: {result['dismissed']}")
        except Exception as e:
            log.debug(f"Overlay check failed: {e}")

    async def _click_send(self, page: Page | None = None) -> bool:
        p = page or self._page
        btn_state = await p.evaluate(
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
        if isinstance(btn_state, dict) and btn_state.get("disabled"):
            log.warning("Send button is disabled — text may not have been inserted properly")
            return False

        selector = await self._find_selector(Selectors.SEND_BUTTON, "send button", p)
        if selector:
            await human_click(p, selector)
            log.info(f"Send button clicked via: {selector}")
            return True
        return False

    async def _upload_files(self, file_paths: list[str], page: Page | None = None) -> None:
        p = page or self._page
        from pathlib import Path
        valid_paths = []
        for path_str in file_paths:
            path = Path(path_str)
            if path.exists() and path.is_file():
                valid_paths.append(str(path.resolve()))
        if not valid_paths:
            return

        file_input = None
        for selector in Selectors.FILE_UPLOAD_INPUT:
            try:
                elements = await p.query_selector_all(selector)
                if elements:
                    file_input = elements[0]
                    break
            except Exception:
                continue

        if file_input:
            await file_input.set_input_files(valid_paths)
        else:
            try:
                await p.set_input_files("input[type='file']", valid_paths)
            except Exception as e:
                log.error(f"Failed to upload files: {e}")
                raise RuntimeError(f"Could not upload files: {e}")

        await asyncio.sleep(3)
        if len(valid_paths) > 1:
            await asyncio.sleep(len(valid_paths))

    def _extract_thread_id(self, page: Page | None = None) -> str:
        p = page or self._page
        url = p.url
        match = re.search(r"/c/([a-f0-9-]+)", url)
        return match.group(1) if match else ""
