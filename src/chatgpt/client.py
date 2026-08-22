"""
ChatGPT client — core interaction logic.

Sends messages, waits for responses, manages conversations.
Handles selector fallbacks and integrates human-like behavior.
"""

from __future__ import annotations

import asyncio
import copy
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Literal

from patchright.async_api import Page

from src.chatgpt.model_registry import (
    list_switchable_models,
    normalize_model_token,
    resolve_requested_model,
)
from src.config import Config
from src.selectors import Selectors
from src.browser.human import human_type, human_click, random_delay
from src.chatgpt.detector import (
    wait_for_response_complete,
    extract_last_response_via_copy,
    count_assistant_messages,
    get_latest_assistant_turn_signature,
    get_latest_user_turn_signature,
    is_incomplete_response_text,
    capture_response_diagnostics,
    _check_page_error,
)
from src.chatgpt.image_handler import extract_images_from_response
from src.chatgpt.audio_handler import generate_read_aloud_audio
from src.chatgpt.models import ChatResponse
from src.chatgpt.errors import PromptAttachmentFallbackError, PromptTooLongError
from src.log import setup_logging

log = setup_logging("chatgpt_client")

SendButtonState = Literal["clicked", "disabled", "missing"]
PromptSubmissionState = Literal["ready", "prompt-too-long", "disabled", "unknown"]


class ChatGPTClient:
    """
    High-level client for interacting with the ChatGPT web interface.

    Requires a Playwright Page that is already logged in and on chatgpt.com.
    """

    def __init__(self, page: Page) -> None:
        self._page = page
        self._last_model_label = ""
        self._last_model_version_label = ""
        self._last_model_setting_by_key: dict[str, str] = {}
        self._unavailable_model_keys: set[str] = set()
        self._recent_backend_events: list[dict] = []
        self._wire_backend_event_logger()

    @property
    def page(self) -> Page:
        return self._page

    def bind_page(self, page: Page | None) -> ChatGPTClient:
        """Return a client bound to a specific tab without mutating this instance."""
        if page is None or page is self._page:
            return self
        bound = copy.copy(self)
        bound._page = page
        return bound

    # ── Core: Send & Receive ────────────────────────────────────

    async def send_message(
        self,
        text: str,
        image_paths: list[str] | None = None,
        file_paths: list[str] | None = None,
        model: str | None = None,
        read_aloud: bool = False,
    ) -> ChatResponse:
        """
        Send a message to ChatGPT and wait for the complete response.

        Args:
            text: The message text to send.
            image_paths: Optional list of local file paths to images to attach.
            file_paths: Optional list of local file paths to non-image files (PDF, etc.).
            read_aloud: If True, trigger ChatGPT's "Read aloud" action and save audio.

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
        temporary_prompt_path: str | None = None
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
        pre_user_signature = await get_latest_user_turn_signature(self._page)
        log.debug(f"Assistant messages before send: {pre_count}")
        log.debug(f"Latest assistant turn before send: {pre_turn_signature}")
        log.debug(f"Latest user turn before send: {pre_user_signature}")

        # 1. Switch model if requested before interacting with the composer
        if model:
            await self.ensure_model(model)

        # 2. Brief pause (human would take a moment to start typing)
        await random_delay(250, 700)

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

        submitted_text = text
        try:
            # 3. Paste the message first so composer clear/delete cannot wipe attachments.
            await human_type(self._page, input_selector, text)

            prompt_state = await self._prompt_submission_state(text)
            if prompt_state == "prompt-too-long":
                if Config.CHATGPT_LONG_PROMPT_FALLBACK != "attachment":
                    raise PromptTooLongError(
                        "ChatGPT rejected the prompt as too long and the attachment fallback is disabled"
                    )

                temporary_prompt_path = self._create_prompt_attachment(text)
                attachment_name = Path(temporary_prompt_path).name
                submitted_text = (
                    f"Read the attached file `{attachment_name}` as the complete user request. "
                    "Follow its instructions exactly and use all of its content before answering."
                )
                log.info(
                    "Prompt is too long for the ChatGPT composer; using attachment fallback (%s)",
                    attachment_name,
                )
                await human_type(self._page, input_selector, submitted_text)
                all_attachments = [*all_attachments, temporary_prompt_path]

            # 3.5. Attach files after text is in the composer.
            if all_attachments:
                await self._upload_files(all_attachments)

            # Small pause after pasting (like a human reviewing before send)
            await random_delay(300, 600)

            auto_submitted = False
            sent = False
            if auto_submitted:
                log.info("ChatGPT auto-submitted after text entry — skipping send button click")
            else:
                # No auto-submit — click the send button
                log.info("No auto-submit detected, clicking send button")
                send_state = await self._click_send()
                sent = send_state == "clicked"
                if send_state == "missing":
                    log.info("Send button not found, trying Enter key")
                    await self._page.keyboard.press("Enter")
                elif send_state == "disabled":
                    log.warning("Send button is disabled; refusing to submit with Enter")
                    raise RuntimeError("ChatGPT send button is disabled; message was not submitted")

            submitted = await self._wait_for_message_submission(pre_user_signature, submitted_text)
            if not submitted:
                diagnostic_path = await capture_response_diagnostics(
                    self._page,
                    "message-not-submitted",
                    previous_turn_signature=pre_turn_signature,
                    extra={
                        "recent_backend_events": self._backend_events_snapshot(),
                        "prompt_length": len(text),
                        "sent_by_button": sent,
                        "prompt_submission_state": prompt_state,
                    },
                )
                detail = "Message was not submitted to ChatGPT"
                if diagnostic_path:
                    detail = f"{detail}; diagnostic={diagnostic_path}"
                raise RuntimeError(detail)

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
                await capture_response_diagnostics(
                    self._page,
                    "response-timeout",
                    previous_turn_signature=pre_turn_signature,
                    extra={
                        "recent_backend_events": self._backend_events_snapshot(),
                        "prompt_length": len(text),
                        "expected_assistant_count": expected_count,
                    },
                )

            # Small buffer after completion to let DOM settle
            await asyncio.sleep(1.0)

            # 6. Check for generated images in the response FIRST
            #    (image turns have no copy button, so we must detect images
            #    before trying copy-button extraction)
            images = await extract_images_from_response(
                self._page,
                previous_turn_signature=pre_turn_signature,
            )
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

                # ChatGPT can briefly expose status text like "thinking" as a turn.
                # Retry against the same new turn before giving that transient text back.
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

                if not response_text or is_incomplete_response_text(response_text):
                    await capture_response_diagnostics(
                        self._page,
                        "empty-or-incomplete-response",
                        previous_turn_signature=pre_turn_signature,
                        extra={
                            "recent_backend_events": self._backend_events_snapshot(),
                            "prompt_length": len(text),
                            "has_images": has_images,
                        },
                    )

            elapsed_ms = int((time.time() - start_time) * 1000)
            thread_id = self._extract_thread_id()
            audio = None

            if read_aloud and response_text:
                audio = await generate_read_aloud_audio(
                    self._page,
                    previous_turn_signature=pre_turn_signature,
                )

            log.info(
                f"Response received ({elapsed_ms}ms, {len(response_text)} chars"
                f"{f', {len(images)} images' if has_images else ''}"
                f"{', audio' if audio else ''}): "
                f"{response_text[:80]}..."
            )

            return ChatResponse(
                message=response_text,
                thread_id=thread_id,
                response_time_ms=elapsed_ms,
                images=images,
                has_images=has_images,
                audio=audio,
                has_audio=audio is not None,
            )
        finally:
            if temporary_prompt_path:
                try:
                    Path(temporary_prompt_path).unlink(missing_ok=True)
                    log.debug("Removed temporary long-prompt attachment: %s", temporary_prompt_path)
                except OSError as exc:
                    log.warning("Could not remove temporary long-prompt attachment %s: %s", temporary_prompt_path, exc)

    async def generate_image(
        self,
        prompt: str,
        n: int = 1,
        size: str = "1024x1024",
        quality: str = "standard",
        style: str = "vivid",
    ) -> ChatResponse:
        """Generate images through the ChatGPT web UI and download results."""
        count = max(1, min(int(n or 1), 4))
        prompt_parts = [
            f"Generate exactly {count} image{'s' if count != 1 else ''} from this prompt:",
            prompt.strip(),
        ]
        if size:
            prompt_parts.append(f"Requested size/aspect: {size}.")
        if quality:
            prompt_parts.append(f"Requested quality: {quality}.")
        if style:
            prompt_parts.append(f"Requested style: {style}.")
        prompt_parts.append("Return the generated image result, not just a text description.")

        return await self.send_message("\n\n".join(part for part in prompt_parts if part.strip()))

    async def ensure_model(self, requested_model: str) -> None:
        """Switch ChatGPT's model picker to the requested model when possible."""
        target = resolve_requested_model(requested_model)
        if target is None:
            log.debug(f"No explicit browser model switch needed for request model={requested_model!r}")
            return
        target_version_label = self._model_version_label_for_option(target)
        target_setting_label = self._model_setting_label_for_option(target)
        unavailable_keys = {
            key
            for key in (
                normalize_model_token(requested_model),
                normalize_model_token(target.public_id),
                *(normalize_model_token(label) for label in target.ui_labels),
            )
            if key
        }
        if self._unavailable_model_keys.intersection(unavailable_keys):
            detail = (
                f"ChatGPT model option '{target.ui_label}' was previously unavailable "
                "in this browser session"
            )
            self._handle_model_switch_failure(detail)
            return

        current_label = await self._detect_current_model_label()
        if current_label and self._label_matches_model_option(current_label, target):
            if (
                not self._model_version_needs_configure(target, target_version_label)
                and self._model_setting_is_current(target, target_setting_label)
            ):
                self._last_model_label = target.ui_label
                log.debug(f"ChatGPT model already selected: {target.ui_label}")
                return

        log.info(
            "Switching ChatGPT model: request=%s target=%s current=%s",
            requested_model,
            target.ui_label,
            current_label or self._last_model_label or "unknown",
        )

        opened = await self._open_model_picker(target.ui_label, current_label=current_label)
        if not opened:
            await self._dismiss_model_picker()
            self._handle_model_switch_failure(f"Could not open ChatGPT model picker for '{target.ui_label}'")
            return

        await asyncio.sleep(0.4)
        await self._expand_advanced_picker()
        await self._click_menu_text("Model")

        switched = await self._click_model_option(target.ui_labels)
        if not switched:
            more_models_opened = await self._click_menu_text("More models")
            if more_models_opened:
                await asyncio.sleep(0.3)
                switched = await self._click_model_option(target.ui_labels)

        if not switched:
            configure_opened = await self._click_menu_text("Advanced")
            if not configure_opened:
                configure_opened = await self._click_menu_text("Configure")
            if configure_opened:
                await asyncio.sleep(0.5)
                switched = await self._select_model_from_configure_dialog(
                    target.ui_labels,
                    target_version_label=target_version_label,
                )
                if switched and target_version_label:
                    self._last_model_version_label = target_version_label

        if not switched:
            self._unavailable_model_keys.update(unavailable_keys)
            visible_options = await self._collect_visible_model_options()
            detail = f"Could not find ChatGPT model option '{target.ui_label}' in the model picker"
            if visible_options:
                detail = f"{detail}; visible options included: {', '.join(visible_options[:12])}"
            await self._dismiss_model_picker()
            self._handle_model_switch_failure(detail)
            return

        if target_version_label and not self._model_version_is_current(target_version_label):
            version_configured = await self._ensure_configured_model_version(target_version_label)
            if not version_configured:
                visible_options = await self._collect_visible_model_options()
                detail = f"Could not configure ChatGPT model version '{target_version_label}'"
                if visible_options:
                    detail = f"{detail}; visible options included: {', '.join(visible_options[:12])}"
                await self._dismiss_model_picker()
                self._handle_model_switch_failure(detail)
                return
            self._last_model_version_label = target_version_label

        setting_applied = False
        if target_setting_label and not self._model_setting_is_current(target, target_setting_label):
            setting_configured = await self._ensure_model_setting(target, target_setting_label)
            if setting_configured is False:
                visible_options = await self._collect_visible_model_options()
                detail = f"Could not configure ChatGPT model setting '{target.ui_label} -> {target_setting_label}'"
                if visible_options:
                    detail = f"{detail}; visible options included: {', '.join(visible_options[:12])}"
                await self._dismiss_model_picker()
                self._handle_model_switch_failure(detail)
                return
            if setting_configured:
                self._last_model_setting_by_key[self._model_setting_cache_key(target)] = target_setting_label
                setting_applied = True

        if self._is_configure_only_version_option(target, target_version_label):
            confirmed = self._model_version_is_current(target_version_label)
            if not confirmed:
                self._handle_model_switch_failure(
                    f"Model switch to '{target.ui_label}' could not be confirmed via Configure"
                )
                return
        else:
            confirmation_labels = self._model_confirmation_labels(target, target_setting_label)
            confirmed = await self._wait_for_model_label(confirmation_labels)
            if not confirmed:
                current_after = await self._detect_current_model_label()
                if normalize_model_token(current_after) not in {
                    normalize_model_token(label) for label in confirmation_labels
                }:
                    await self._dismiss_model_picker()
                    await asyncio.sleep(1.0)
                    current_after = await self._detect_current_model_label()
                    if normalize_model_token(current_after) not in {
                        normalize_model_token(label) for label in confirmation_labels
                    }:
                        self._handle_model_switch_failure(
                            f"Model switch to '{target.ui_label}' could not be confirmed"
                        )
                        return

        await self._dismiss_model_picker()
        self._last_model_label = target.ui_label
        if target_version_label:
            self._last_model_version_label = target_version_label
        if target_setting_label and (setting_applied or self._model_setting_is_current(target, target_setting_label)):
            self._last_model_setting_by_key[self._model_setting_cache_key(target)] = target_setting_label
        log.info(f"Model switched to {target.ui_label}")

    # ── Navigation ──────────────────────────────────────────────

    async def new_chat(self) -> None:
        """Start a new conversation.

        Strategy order:
        1. SPA button click (avoids DNS issues, preserves browser state)
        2. JavaScript location change (no DNS lookup needed if page is loaded)
        3. Full page.goto() (last resort - may fail with DNS errors)
        """
        log.info("Starting new chat...")
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

        await random_delay(500, 1000)
        log.info("New chat started (navigated to home)")

    async def navigate_to_thread(self, thread_id: str) -> None:
        """Navigate to an existing conversation thread."""
        url = f"{Config.CHATGPT_URL}/c/{thread_id}"
        log.info(f"Navigating to thread: {thread_id}")
        await self._page.goto(url, wait_until="domcontentloaded")
        await random_delay(1500, 3000)
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

    async def delete_thread(self, thread_id: str) -> bool:
        """
        Delete a ChatGPT conversation thread via the web UI.

        Navigates to the thread, opens the sidebar context menu, clicks Delete,
        and confirms in the modal dialog. Returns True on success, False otherwise.

        This is best-effort: failures are logged but never raised.
        """
        log.info(f"Attempting to delete ChatGPT thread: {thread_id}")
        try:
            # Navigate to the thread so the sidebar item is visible/interactable
            await self.navigate_to_thread(thread_id)
            await asyncio.sleep(2)

            # Locate the sidebar thread item for this specific thread
            thread_href = f"/c/{thread_id}"
            thread_el = None
            for sel in Selectors.SIDEBAR_THREAD_ITEM:
                try:
                    elements = await self._page.query_selector_all(sel)
                    for el in elements:
                        href = (await el.get_attribute("href") or "").rstrip("/")
                        if thread_href in href or href.endswith(f"/c/{thread_id}"):
                            thread_el = el
                            break
                    if thread_el:
                        break
                except Exception:
                    continue

            if not thread_el:
                log.warning(f"Could not find sidebar item for thread {thread_id}")
                return False

            # Hover over the thread item to reveal the menu button
            try:
                await thread_el.hover()
                await asyncio.sleep(0.5)
            except Exception as e:
                log.debug(f"Hover on thread item failed (non-fatal): {e}")

            # Click the three-dot / overflow menu button
            menu_clicked = False
            for sel in Selectors.SIDEBAR_THREAD_MENU_BUTTON:
                try:
                    # Try within the thread item's parent row first
                    parent = await thread_el.evaluate_handle("el => el.closest('li') || el.closest('div[class*=\"group\"]')")
                    btn = await parent.query_selector(sel)
                    if btn:
                        await btn.click(timeout=3000)
                        menu_clicked = True
                        break
                except Exception:
                    continue

            if not menu_clicked:
                log.warning(f"Could not open context menu for thread {thread_id}")
                return False

            await asyncio.sleep(0.5)

            # Click "Delete" in the context menu
            delete_clicked = False
            for sel in Selectors.THREAD_DELETE_OPTION:
                try:
                    btn = await self._page.wait_for_selector(sel, timeout=3000, state="visible")
                    if btn:
                        await btn.click(timeout=3000)
                        delete_clicked = True
                        break
                except Exception:
                    continue

            if not delete_clicked:
                log.warning(f"Could not click Delete option for thread {thread_id}")
                return False

            await asyncio.sleep(0.5)

            # Confirm deletion in the modal dialog
            confirm_clicked = False
            for sel in Selectors.THREAD_CONFIRM_DELETE_BUTTON:
                try:
                    btn = await self._page.wait_for_selector(sel, timeout=3000, state="visible")
                    if btn:
                        await btn.click(timeout=3000)
                        confirm_clicked = True
                        break
                except Exception:
                    continue

            if not confirm_clicked:
                log.warning(f"Could not confirm deletion for thread {thread_id}")
                return False

            # Allow time for the deletion request to process
            await asyncio.sleep(2)
            log.info(f"Successfully deleted ChatGPT thread: {thread_id}")
            return True

        except Exception as e:
            log.warning(f"Failed to delete thread {thread_id}: {e}", exc_info=True)
            return False

    # ── Private Helpers ─────────────────────────────────────────

    def _wire_backend_event_logger(self) -> None:
        """Record recent ChatGPT backend responses for timeout diagnostics."""
        try:
            self._page.on("response", self._record_backend_response)
        except Exception as e:
            log.debug(f"Could not attach backend response logger: {e}")

    def _record_backend_response(self, response) -> None:
        """Best-effort synchronous Playwright response event handler."""
        try:
            url = getattr(response, "url", "") or ""
            if not any(marker in url for marker in ("backend-api", "conversation", "sentinel", "chat-requirements")):
                return
            status = getattr(response, "status", None)
            self._recent_backend_events.append(
                {
                    "ts": time.time(),
                    "status": status,
                    "url": url[:500],
                }
            )
            self._recent_backend_events = self._recent_backend_events[-80:]
        except Exception:
            return

    def _backend_events_snapshot(self) -> list[dict]:
        """Return recent backend events with small, serializable fields."""
        return list(self._recent_backend_events[-40:])

    async def _detect_page_error(self) -> str | None:
        """Return the current browser/page error state, if one is visible."""
        return await _check_page_error(self._page)

    async def _prompt_submission_state(self, text: str) -> PromptSubmissionState:
        """Classify the composer before attachments and submission."""
        if Config.CHATGPT_LONG_PROMPT_THRESHOLD and len(text) >= Config.CHATGPT_LONG_PROMPT_THRESHOLD:
            return "prompt-too-long"

        await asyncio.sleep(0.15)
        state = await self._composer_state()
        if not state:
            return "unknown"
        if state.get("promptTooLong"):
            return "prompt-too-long"

        send_button = state.get("sendButton")
        if isinstance(send_button, dict) and (
            send_button.get("disabled")
            or str(send_button.get("ariaDisabled") or "").lower() == "true"
        ):
            # ChatGPT currently does not always render a validation message for
            # oversized prompts. In the live UI it leaves the populated
            # composer intact and sets aria-disabled="true" on Send. Treat that
            # stable, non-generating state as the same prompt-too-long signal so
            # the attachment fallback can run. An active response can also
            # disable Send, so keep that case distinct.
            if str(state.get("composerText") or "").strip() and not state.get("hasStopButton"):
                return "prompt-too-long"
            return "disabled"
        return "ready"

    @staticmethod
    def _create_prompt_attachment(text: str) -> str:
        """Persist a prompt as a UTF-8 temporary file for ChatGPT upload."""
        file_descriptor, filename = tempfile.mkstemp(
            prefix="catgpt-long-prompt-",
            suffix=".txt",
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        except Exception as exc:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
            Path(filename).unlink(missing_ok=True)
            raise PromptAttachmentFallbackError(
                f"Could not create the temporary prompt attachment: {exc}"
            ) from exc
        return filename

    async def _wait_for_message_submission(
        self,
        previous_user_signature: str | None,
        sent_text: str,
        timeout_ms: int = 15000,
    ) -> bool:
        """
        Confirm that ChatGPT accepted the outgoing prompt.

        This catches stale send selectors and Enter-key-newline failures before
        the response detector spends a full timeout waiting for a reply.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        prompt_prefix = sent_text.strip()[:160]

        while time.monotonic() < deadline:
            latest_user_signature = await get_latest_user_turn_signature(self._page)
            if latest_user_signature and latest_user_signature != previous_user_signature:
                log.debug(f"Message submission confirmed by user turn: {latest_user_signature}")
                return True

            state = await self._composer_state()
            if not state:
                await asyncio.sleep(0.5)
                continue
            if state.get("hasStopButton"):
                log.debug("Message submission confirmed by visible stop button")
                return True

            composer_text = str(state.get("composerText") or "").strip()
            if not composer_text:
                log.debug("Message submission confirmed by cleared composer")
                return True

            if prompt_prefix and prompt_prefix not in composer_text:
                log.debug("Message submission likely confirmed by composer text change")
                return True

            await asyncio.sleep(0.5)

        log.warning("Timed out waiting for prompt submission confirmation")
        return False

    async def _composer_state(self) -> dict:
        """Read composer, validation, and send-button state from the page."""
        try:
            state = await self._page.evaluate(
                """
                () => {
                    const textOf = (el) => ((el && (el.innerText || el.textContent || el.value)) || '').trim();
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 &&
                            rect.height > 0 &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none';
                    };
                    const composer = document.querySelector(
                        "#prompt-textarea, div[contenteditable='true'][id='prompt-textarea'], div[contenteditable='true'], textarea"
                    );
                    const stopSelector = [
                        'button[data-testid="stop-button"]',
                        'button[aria-label="Stop answering"]',
                        'button[aria-label="Stop generating"]',
                        'button[aria-label*="stop" i]'
                    ].join(',');
                    const sendSelectors = [
                        'button[data-testid="send-button"]',
                        '#composer-submit-button',
                        "button[aria-label='Send prompt']",
                    ];
                    let sendButton = null;
                    for (const selector of sendSelectors) {
                        const candidate = document.querySelector(selector);
                        if (candidate) {
                            sendButton = {
                                selector,
                                disabled: Boolean(candidate.disabled),
                                ariaDisabled: candidate.getAttribute('aria-disabled'),
                                visible: isVisible(candidate),
                            };
                            break;
                        }
                    }
                    const validationNodes = Array.from(document.querySelectorAll(
                        '[role="alert"], [aria-live], [data-state="error"], [class*="error" i], [class*="alert" i]'
                    ));
                    const validationText = validationNodes
                        .filter(isVisible)
                        .map(textOf)
                        .filter(Boolean)
                        .join(' ')
                        .slice(-2000);
                    // The composer and its toast/validation area are normally
                    // near the end of body text. Limiting this avoids treating
                    // an old error in conversation history as current state.
                    const visibleText = ((document.body && document.body.innerText) || '').slice(-4000);
                    const promptTooLong = /(?:message|prompt|input).{0,50}too long|too long.{0,50}(?:message|prompt|input)|(?:exceed|exceeds|maximum).{0,50}(?:character|token|length|limit)|(?:character|token|length).{0,50}(?:limit|maximum)/i.test(
                        `${validationText} ${visibleText}`
                    );
                    return {
                        composerText: textOf(composer),
                        hasStopButton: Array.from(document.querySelectorAll(stopSelector)).some(isVisible),
                        sendButton,
                        validationText,
                        promptTooLong,
                    };
                }
                """
            )
            return state if isinstance(state, dict) else {}
        except Exception as e:
            log.debug(f"Composer state read failed: {e}")
            return {}

    async def _extract_image_turn_text(self, previous_turn_signature: str | None = None) -> str:
        """
        Extract any text content from the latest turn (for image responses).

        Image turns may contain a title/description like:
        "Creating image • Adorable orange tabby kitten close-up"
        """
        return await extract_last_response_via_copy(
            self._page,
            previous_turn_signature=previous_turn_signature,
        )

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

    async def _click_send(self) -> SendButtonState:
        """Try to click Send, distinguishing disabled from missing controls."""
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
        if isinstance(btn_state, dict) and (
            btn_state.get("disabled")
            or str(btn_state.get("ariaDisabled") or "").lower() == "true"
        ):
            log.warning("Send button is disabled — refusing to submit with Enter")
            return "disabled"

        selector = await self._find_selector(Selectors.SEND_BUTTON, "send button")
        if selector:
            await human_click(self._page, selector)
            log.info(f"Send button clicked via: {selector}")
            return "clicked"
        return "missing"

    async def _detect_current_model_label(self) -> str:
        """Best-effort detection of the currently selected ChatGPT model label."""
        known_labels = [
            label
            for option in list_switchable_models()
            for label in self._model_confirmation_labels(option, self._model_setting_label_for_option(option))
        ]
        if self._last_model_label and self._last_model_label not in known_labels:
            known_labels.append(self._last_model_label)
        if not known_labels:
            return ""

        label = await self._page.evaluate(
            r"""
            (knownLabels) => {
                const normalize = (value) =>
                    (value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 &&
                        rect.height > 0 &&
                        style.visibility !== "hidden" &&
                        style.display !== "none";
                };
                const wanted = (knownLabels || []).map((label) => ({
                    label,
                    normalized: normalize(label),
                })).filter((item) => item.normalized);
                const clickable = Array.from(document.querySelectorAll("button,[role='button'],[role='tab']"));
                const matches = [];
                for (const el of clickable) {
                    if (!isVisible(el)) continue;
                    const rect = el.getBoundingClientRect();
                    const primaryText = ((el.innerText || el.textContent || "")
                        .split(/\n+/)
                        .map((part) => part.trim())
                        .filter(Boolean)[0] || "").trim();
                    const rawText = [
                        primaryText,
                        el.innerText || "",
                        el.textContent || "",
                        el.getAttribute("aria-label") || "",
                        el.getAttribute("title") || "",
                    ].join(" ").trim();
                    const normalizedText = normalize(rawText);
                    const normalizedPrimary = normalize(primaryText);
                    if (!normalizedText) continue;
                    for (const wantedLabel of wanted) {
                        if (!normalizedText.includes(wantedLabel.normalized)) continue;
                        let score = 0;
                        if (normalizedPrimary === wantedLabel.normalized) score += 70;
                        else if (normalizedText === wantedLabel.normalized) score += 50;
                        else if (normalizedPrimary.startsWith(wantedLabel.normalized)) score += 35;
                        else if (normalizedText.startsWith(wantedLabel.normalized)) score += 25;
                        else score += 10;
                        score += Math.min(10, wantedLabel.normalized.length);
                        if (rect.top < 260) score += 20;
                        if (rect.left < 500) score += 10;
                        matches.push({
                            label: wantedLabel.label,
                            score,
                            top: rect.top,
                            left: rect.left,
                        });
                    }
                }
                matches.sort((a, b) => b.score - a.score || a.top - b.top || a.left - b.left);
                return matches.length ? matches[0].label : "";
            }
            """,
            known_labels,
        )
        return (label or "").strip()

    def _handle_model_switch_failure(self, detail: str) -> None:
        """Either raise or continue when the requested ChatGPT UI model is unavailable."""
        if Config.CHATGPT_MODEL_SWITCH_STRICT:
            raise RuntimeError(detail)

        log.warning(
            "%s; continuing with the currently selected ChatGPT model "
            "(set CHATGPT_MODEL_SWITCH_STRICT=true to fail instead)",
            detail,
        )

    def _model_version_label_for_option(self, option) -> str:
        """Return the Configure-dialog version label implied by a model option."""
        public_id = (getattr(option, "public_id", "") or "").strip().lower()
        match = re.match(r"gpt-(\d+)\.(\d+)", public_id)
        if match:
            return f"{match.group(1)}.{match.group(2)}"
        if re.match(r"o\d+", public_id):
            return public_id.split("-", 1)[0]
        return ""

    def _model_version_is_current(self, target_version_label: str) -> bool:
        """Return whether the last configured model version matches the target."""
        target = normalize_model_token(target_version_label)
        current = normalize_model_token(self._last_model_version_label)
        return bool(target and current and target == current)

    def _model_setting_label_for_option(self, option) -> str:
        """Return the configured Standard/Extended picker setting for a model option."""
        return (getattr(option, "setting_label", "") or "").strip()

    def _model_setting_cache_key(self, option) -> str:
        """Return the stable key used to remember a model's selected picker setting."""
        return normalize_model_token(getattr(option, "public_id", "") or getattr(option, "ui_label", ""))

    def _model_setting_is_current(self, option, target_setting_label: str) -> bool:
        """Return whether the requested model setting was already selected."""
        target = normalize_model_token(target_setting_label)
        if not target:
            return True
        current = normalize_model_token(self._last_model_setting_by_key.get(self._model_setting_cache_key(option), ""))
        return bool(current and current == target)

    def _model_confirmation_labels(self, option, target_setting_label: str = "") -> tuple[str, ...]:
        """Return visible labels that can confirm a successful model switch."""
        labels = list(getattr(option, "ui_labels", ()) or ())
        if target_setting_label:
            labels.insert(0, target_setting_label)
        return self._dedupe_model_labels(labels)

    def _model_setting_control_labels(self, option) -> tuple[str, ...]:
        """Return row labels to use when opening a model row's settings control."""
        labels = list(getattr(option, "ui_labels", ()) or ())
        public_id = (getattr(option, "public_id", "") or "").lower()
        if public_id.endswith("-thinking"):
            labels.insert(0, "Thinking")
        elif public_id.endswith("-pro"):
            labels.insert(0, "Pro")
        return self._dedupe_model_labels(labels)

    def _dedupe_model_labels(self, labels: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        """Return labels in order with normalized duplicates removed."""
        seen: set[str] = set()
        result: list[str] = []
        for label in labels:
            normalized = normalize_model_token(label)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(label)
        return tuple(result)

    def _model_version_needs_configure(self, option, target_version_label: str) -> bool:
        """
        Return whether a matching visible mode label is insufficient.

        ChatGPT's composer can show only the mode, e.g. `Instant`, while the
        configured model version inside Configure is `5.4`. For versioned
        public ids, confirm Configure at least once per process unless we
        already selected the same version.
        """
        if not target_version_label:
            return False
        if self._model_version_is_current(target_version_label):
            return False
        primary = normalize_model_token(getattr(option, "ui_label", ""))
        target_version = normalize_model_token(target_version_label)
        return primary != target_version

    def _is_configure_only_version_option(self, option, target_version_label: str) -> bool:
        """
        Return whether the requested option is a version selected inside Configure.

        ChatGPT can keep showing a top-level mode label after selecting a concrete
        version from Configure, so pure version requests confirm by configured
        version rather than by the composer button text.
        """
        target_version = normalize_model_token(target_version_label)
        primary = normalize_model_token(getattr(option, "ui_label", ""))
        return bool(target_version and primary == target_version)

    async def _dismiss_model_picker(self) -> None:
        """Close any open model picker menu/dialog before interacting with the composer."""
        for _ in range(2):
            try:
                await self._page.keyboard.press("Escape")
                await asyncio.sleep(0.1)
            except Exception:
                break

    async def _collect_visible_model_options(self) -> list[str]:
        """Return visible model-like labels from the currently open picker/dialog."""
        try:
            labels = await self._page.evaluate(
                r"""
                () => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 &&
                            rect.height > 0 &&
                            style.visibility !== "hidden" &&
                            style.display !== "none";
                    };
                    const selectors = [
                        "[role='menuitemradio']",
                        "[role='menuitem']",
                        "[role='option']",
                        "[role='radio']",
                        "[role='combobox']",
                        "[data-radix-collection-item]",
                        "button",
                        "li",
                    ];
                    const seen = new Set();
                    const labels = [];
                    const modelish = /(gpt|claude|o[0-9]|instant|medium|high|pro|effort|power|thinking|latest|model|mini|nano|5\.[0-9]|4\.[0-9])/i;
                    for (const selector of selectors) {
                        for (const el of document.querySelectorAll(selector)) {
                            if (!isVisible(el)) continue;
                            const text = ((el.innerText || el.textContent || "")
                                .split(/\n+/)
                                .map((part) => part.trim())
                                .filter(Boolean)[0] || "").trim();
                            if (!text || text.length > 80 || !modelish.test(text)) continue;
                            const key = text.toLowerCase().replace(/\s+/g, " ");
                            if (seen.has(key)) continue;
                            seen.add(key);
                            labels.push(text);
                            if (labels.length >= 20) return labels;
                        }
                    }
                    return labels;
                }
                """
            )
        except Exception as e:
            log.debug(f"Could not collect visible model options: {e}")
            return []

        if not isinstance(labels, list):
            return []
        return [str(label).strip() for label in labels if str(label).strip()]

    async def _model_picker_is_open(self) -> bool:
        """Return whether a model picker/menu is visibly open."""
        try:
            open_ = await self._page.evaluate(
                r"""
                () => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 &&
                            rect.height > 0 &&
                            style.visibility !== "hidden" &&
                            style.display !== "none";
                    };
                    const modelish = /(gpt|o[0-9]|instant|medium|high|pro|effort|advanced|power|thinking|latest|model|more|configure|5\.[0-9]|4\.[0-9])/i;
                    const selectors = [
                        "[role='menuitemradio']",
                        "[role='menuitem']",
                        "[role='option']",
                        "[role='radio']",
                        "[data-radix-collection-item]",
                    ];
                    for (const selector of selectors) {
                        for (const el of document.querySelectorAll(selector)) {
                            if (!isVisible(el)) continue;
                            const text = [
                                el.innerText || "",
                                el.textContent || "",
                                el.getAttribute("aria-label") || "",
                                el.getAttribute("title") || "",
                                el.getAttribute("data-testid") || "",
                            ].join(" ");
                            if (modelish.test(text)) return true;
                        }
                    }
                    return false;
                }
                """
            )
            return bool(open_)
        except Exception as e:
            log.debug(f"Could not check model picker open state: {e}")
            return False

    async def _open_model_picker(self, target_label: str, current_label: str = "") -> bool:
        """Open ChatGPT's model picker using the visible current-model button."""
        for selector in Selectors.MODEL_PICKER_BUTTON:
            try:
                el = await self._page.wait_for_selector(selector, timeout=1000, state="visible")
                if el:
                    await human_click(self._page, selector)
                    await asyncio.sleep(0.25)
                    if await self._model_picker_is_open():
                        return True
            except Exception:
                continue

        hints = [
            current_label,
            self._last_model_label,
            target_label,
            "Instant",
            "Thinking",
            "Latest",
            "Standard",
            "Extended",
            "Advanced",
            "Effort",
            "Configure",
            "Power",
            "model",
            "GPT",
            "o3",
            "o4",
        ]
        if await self._click_model_picker_button_by_text(hints):
            await asyncio.sleep(0.25)
            return await self._model_picker_is_open()

        return False

    async def _click_model_picker_button_by_text(self, hints: list[str]) -> bool:
        """Click the ChatGPT model picker trigger by current/target model text."""
        filtered_hints = [hint for hint in hints if (hint or "").strip()]
        if not filtered_hints:
            return False

        candidate = await self._page.evaluate(
            r"""
            (hints) => {
                const normalize = (value) =>
                    (value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
                const wanted = (hints || []).map(normalize).filter(Boolean);
                const modelish = /(gpt|o[0-9]|instant|medium|high|pro|effort|advanced|power|thinking|latest|auto|model|mini|nano|5\.[0-9]|4\.[0-9])/i;
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 &&
                        rect.height > 0 &&
                        style.visibility !== "hidden" &&
                        style.display !== "none";
                };
                const isInsideSidebar = (el) => Boolean(el.closest(
                    "nav, aside, [data-testid='sidebar'], [data-testid*='history' i]"
                ));
                const clickable = Array.from(document.querySelectorAll([
                    "button",
                    "[role='button']",
                    "[aria-haspopup='menu']",
                    "[data-testid*='model' i]",
                ].join(",")));
                const matches = [];
                for (const el of clickable) {
                    if (!isVisible(el)) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.top < 0 || rect.top > window.innerHeight - 40) continue;
                    if (rect.left < 240 && isInsideSidebar(el)) continue;

                    const primaryText = ((el.innerText || el.textContent || "")
                        .split(/\n+/)
                        .map((part) => part.trim())
                        .filter(Boolean)[0] || "").trim();
                    const text = [
                        primaryText,
                        el.innerText || "",
                        el.textContent || "",
                        el.getAttribute("aria-label") || "",
                        el.getAttribute("title") || "",
                        el.getAttribute("data-testid") || "",
                    ].join(" ").trim();
                    const normalizedText = normalize(text);
                    const normalizedPrimary = normalize(primaryText);
                    if (!normalizedText) continue;

                    let score = 0;
                    for (const hint of wanted) {
                        if (!normalizedText.includes(hint)) continue;
                        score = Math.max(
                            score,
                            normalizedPrimary === hint
                                ? 90
                                : normalizedText === hint
                                  ? 75
                                  : normalizedPrimary.startsWith(hint)
                                    ? 60
                                    : normalizedText.startsWith(hint)
                                      ? 50
                                      : 25,
                        );
                    }
                    if (modelish.test(text)) score += 25;
                    if ((el.getAttribute("aria-haspopup") || "").toLowerCase() === "menu") score += 40;
                    if ((el.getAttribute("data-testid") || "").toLowerCase().includes("model")) score += 35;
                    if (rect.left > 240) score += 15;
                    if (rect.top < 120 || rect.top > window.innerHeight / 2) score += 10;

                    if (score < 35) continue;
                    matches.push({
                        score,
                        top: rect.top,
                        left: rect.left,
                        x: rect.left + rect.width / 2,
                        y: rect.top + rect.height / 2,
                        label: primaryText,
                    });
                }
                matches.sort((a, b) => b.score - a.score || b.left - a.left || a.top - b.top);
                return matches[0] || null;
            }
            """,
            filtered_hints,
        )
        if not isinstance(candidate, dict) or "x" not in candidate or "y" not in candidate:
            return False

        await self._page.mouse.move(float(candidate["x"]), float(candidate["y"]), steps=8)
        await asyncio.sleep(0.05)
        await self._page.mouse.click(float(candidate["x"]), float(candidate["y"]))
        return True

    async def _click_top_button_by_text(self, hints: list[str]) -> bool:
        """Click a visible top-of-page button whose label best matches the hints."""
        filtered_hints = [hint for hint in hints if (hint or "").strip()]
        if not filtered_hints:
            return False

        candidate = await self._page.evaluate(
            r"""
            (hints) => {
                const normalize = (value) =>
                    (value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
                const wanted = (hints || []).map(normalize).filter(Boolean);
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 &&
                        rect.height > 0 &&
                        style.visibility !== "hidden" &&
                        style.display !== "none";
                };
                const clickable = Array.from(document.querySelectorAll("button,[role='button'],[role='tab']"));
                const matches = [];
                for (const el of clickable) {
                    if (!isVisible(el)) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.top > 320) continue;
                    const primaryText = ((el.innerText || el.textContent || "")
                        .split(/\n+/)
                        .map((part) => part.trim())
                        .filter(Boolean)[0] || "").trim();
                    const text = [
                        primaryText,
                        el.innerText || "",
                        el.textContent || "",
                        el.getAttribute("aria-label") || "",
                        el.getAttribute("title") || "",
                    ].join(" ").trim();
                    const normalizedText = normalize(text);
                    const normalizedPrimary = normalize(primaryText);
                    if (!normalizedText) continue;
                    let score = 0;
                    for (const hint of wanted) {
                        if (!normalizedText.includes(hint)) continue;
                        score = Math.max(
                            score,
                            normalizedPrimary === hint
                                ? 70
                                : normalizedText === hint
                                  ? 60
                                  : normalizedPrimary.startsWith(hint)
                                    ? 45
                                    : normalizedText.startsWith(hint)
                                      ? 40
                                      : 20,
                        );
                    }
                    if (!score) continue;
                    if (el.getAttribute("aria-haspopup")) score += 15;
                    if (rect.left < 500) score += 10;
                    matches.push({
                        score,
                        top: rect.top,
                        left: rect.left,
                        x: rect.left + rect.width / 2,
                        y: rect.top + rect.height / 2,
                    });
                }
                matches.sort((a, b) => b.score - a.score || a.top - b.top || a.left - b.left);
                const best = matches[0];
                return best || null;
            }
            """,
            filtered_hints,
        )
        if not isinstance(candidate, dict) or "x" not in candidate or "y" not in candidate:
            return False

        await self._page.mouse.move(float(candidate["x"]), float(candidate["y"]), steps=8)
        await asyncio.sleep(0.05)
        await self._page.mouse.click(float(candidate["x"]), float(candidate["y"]))
        return True

    async def _click_model_option(self, target_labels: tuple[str, ...]) -> bool:
        """Click a visible model option in an open picker/menu by its label."""
        for target_label in target_labels:
            if await self._click_menu_text(target_label):
                return True
        return False

    async def _ensure_model_setting(self, option, setting_label: str) -> bool | None:
        """Open the picker and select the requested effort/legacy setting."""
        if await self._ensure_advanced_effort(option, setting_label):
            return True

        await self._dismiss_model_picker()
        current_label = await self._detect_current_model_label()
        opened = await self._open_model_picker(option.ui_label, current_label=current_label)
        if not opened:
            return False

        await asyncio.sleep(0.3)
        settings_opened = await self._click_model_setting_control(self._model_setting_control_labels(option))
        if not settings_opened:
            target_version_label = self._model_version_label_for_option(option)
            if target_version_label:
                configured = await self._ensure_configured_model_setting(option, setting_label, target_version_label)
                if configured:
                    return True
                current_label = await self._detect_current_model_label()
                if self._label_matches_model_option(current_label, option):
                    log.warning(
                        "ChatGPT model setting '%s -> %s' was not visible; continuing with selected model",
                        option.ui_label,
                        setting_label,
                    )
                    return None
                return False
            return False

        await asyncio.sleep(0.3)
        if await self._click_model_option((setting_label,)):
            return True

        target_version_label = self._model_version_label_for_option(option)
        if target_version_label:
            configured = await self._ensure_configured_model_setting(option, setting_label, target_version_label)
            if configured:
                return True
            current_label = await self._detect_current_model_label()
            if self._label_matches_model_option(current_label, option):
                log.warning(
                    "ChatGPT model setting '%s -> %s' was not visible; continuing with selected model",
                    option.ui_label,
                    setting_label,
                )
                return None

        return False

    async def _expand_advanced_picker(self) -> bool:
        """Expand compact Power menu into Model / Effort rows when needed."""
        for label in ("Show advanced options", "Advanced"):
            if await self._click_menu_text(label):
                await asyncio.sleep(0.3)
                return True
        return False

    async def _ensure_advanced_effort(self, option, setting_label: str) -> bool:
        """Select an effort from the current Advanced -> Effort submenu."""
        await self._dismiss_model_picker()
        current_label = await self._detect_current_model_label()
        opened = await self._open_model_picker(option.ui_label, current_label=current_label)
        if not opened:
            return False

        await asyncio.sleep(0.3)
        await self._expand_advanced_picker()
        effort_opened = await self._click_menu_text("Effort")
        if not effort_opened:
            return False

        await asyncio.sleep(0.3)
        return await self._click_model_option((setting_label,))

    async def _ensure_configured_model_setting(
        self,
        option,
        setting_label: str,
        target_version_label: str,
    ) -> bool:
        """Open Configure and set Standard/Extended for a versioned Thinking/Pro row."""
        await self._dismiss_model_picker()
        current_label = await self._detect_current_model_label()
        opened = await self._open_model_picker(option.ui_label, current_label=current_label)
        if not opened:
            return False

        await asyncio.sleep(0.3)
        configure_opened = await self._expand_advanced_picker()
        if not configure_opened:
            configure_opened = await self._click_menu_text("Configure")
        if not configure_opened:
            return False

        await asyncio.sleep(0.5)
        dropdown_opened = await self._click_configure_model_combobox()
        if dropdown_opened:
            await asyncio.sleep(0.3)
            version_labels = self._configure_version_click_labels(option.ui_labels, target_version_label)
            await self._click_model_option(version_labels)
            await asyncio.sleep(0.4)

        settings_opened = await self._click_model_setting_control(self._model_setting_control_labels(option))
        if not settings_opened:
            return False

        await asyncio.sleep(0.3)
        return await self._click_model_option((setting_label,))

    async def _click_model_setting_control(self, target_labels: tuple[str, ...]) -> bool:
        """Click the settings control beside a Thinking/Pro model row."""
        labels = [label for label in target_labels if (label or "").strip()]
        if not labels:
            return False

        row = await self._page.evaluate(
            r"""
            (targetLabels) => {
                const normalize = (value) =>
                    (value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
                const wanted = (targetLabels || []).map(normalize).filter(Boolean);
                if (!wanted.length) return null;
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 &&
                        rect.height > 0 &&
                        style.visibility !== "hidden" &&
                        style.display !== "none";
                };
                const textFor = (el) => [
                    el.innerText || "",
                    el.textContent || "",
                    el.getAttribute("aria-label") || "",
                    el.getAttribute("title") || "",
                ].join(" ");
                const selectors = [
                    "[role='menuitemradio']",
                    "[role='menuitem']",
                    "[role='radio']",
                    "[data-testid^='model-switcher']",
                    "[data-radix-collection-item]",
                ];
                const rows = [];
                for (const selector of selectors) {
                    for (const el of document.querySelectorAll(selector)) {
                        if (!isVisible(el)) continue;
                        const normalizedText = normalize(textFor(el));
                        if (!normalizedText || !wanted.some((label) => normalizedText.includes(label))) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width < 40 || rect.height < 20 || rect.top > window.innerHeight - 24) continue;
                        rows.push({ el, rect });
                    }
                }
                rows.sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
                if (rows[0]) {
                    const rect = rows[0].rect;
                    return {
                        left: rect.left,
                        top: rect.top,
                        width: rect.width,
                        height: rect.height,
                        x: Math.max(rect.left + rect.width - 18, rect.left + rect.width * 0.75),
                        y: rect.top + rect.height / 2,
                    };
                }
                return null;
            }
            """,
            labels,
        )
        if not isinstance(row, dict) or "x" not in row or "y" not in row:
            return False

        await self._page.mouse.move(float(row["x"]), float(row["y"]), steps=8)
        await asyncio.sleep(0.25)

        candidate = await self._page.evaluate(
            r"""
            (row) => {
                const normalize = (value) =>
                    (value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 &&
                        rect.height > 0 &&
                        style.visibility !== "hidden" &&
                        style.display !== "none";
                };
                const textFor = (el) => [
                    el.innerText || "",
                    el.textContent || "",
                    el.getAttribute("aria-label") || "",
                    el.getAttribute("title") || "",
                    el.getAttribute("data-testid") || "",
                ].join(" ");
                const rowRight = row.left + row.width;
                const rowBottom = row.top + row.height;
                const controls = Array.from(document.querySelectorAll("button,[role='button'],[aria-label],[title],[data-testid]"))
                    .filter((el) => {
                        if (!isVisible(el)) return false;
                        const rect = el.getBoundingClientRect();
                        const centerY = rect.top + rect.height / 2;
                        return centerY >= row.top - 6 &&
                            centerY <= rowBottom + 6 &&
                            rect.left >= row.left + row.width * 0.45 &&
                            rect.left <= rowRight + 48;
                    });
                const scored = [];
                for (const control of controls) {
                    const rect = control.getBoundingClientRect();
                    const text = normalize(textFor(control));
                    let score = 0;
                    if (/settings|setting|customize|preferences|options|sliders|model/.test(text)) score += 80;
                    if (rect.width <= 48 && rect.height <= 48) score += 25;
                    if (rect.left >= row.left + row.width * 0.6) score += 25;
                    if (score) {
                        scored.push({
                            score,
                            x: rect.left + rect.width / 2,
                            y: rect.top + rect.height / 2,
                        });
                    }
                }
                scored.sort((a, b) => b.score - a.score);
                return scored[0] || null;
            }
            """,
            row,
        )
        if not isinstance(candidate, dict) or "x" not in candidate or "y" not in candidate:
            return False

        await self._page.mouse.move(float(candidate["x"]), float(candidate["y"]), steps=4)
        await asyncio.sleep(0.05)
        await self._page.mouse.click(float(candidate["x"]), float(candidate["y"]))
        return True

    async def _select_model_from_configure_dialog(
        self,
        target_labels: tuple[str, ...],
        target_version_label: str = "",
    ) -> bool:
        """Select a model from the Pro Intelligence -> Model dropdown dialog."""
        target_tokens = {normalize_model_token(label) for label in target_labels}
        if target_version_label:
            target_tokens.add(normalize_model_token(target_version_label))
        version_tokens = {
            token
            for token in target_tokens
            if re.match(r"^(5[0-9]+|o[0-9]+)$", token)
        }

        if version_tokens:
            dropdown_opened = await self._click_configure_model_combobox()
            if not dropdown_opened:
                return False

            await asyncio.sleep(0.4)
            version_labels = self._configure_version_click_labels(target_labels, target_version_label)
            version_selected = await self._click_model_option(version_labels)
            if not version_selected:
                return False

            await asyncio.sleep(0.4)
            radio_selected = await self._click_configure_radio_for_version(
                target_version_label or version_labels[0],
                target_labels=target_labels,
            )
            return radio_selected or version_selected

        return await self._click_model_option(target_labels)

    def _configure_version_click_labels(
        self,
        target_labels: tuple[str, ...],
        target_version_label: str = "",
    ) -> tuple[str, ...]:
        """Return labels to try when selecting a concrete model version."""
        ordered: list[str] = []
        if target_version_label:
            ordered.append(target_version_label)
        for label in target_labels:
            match = re.search(r"(?:gpt[- ]*)?((?:[45]\.\d+)|o\d+)", label, flags=re.IGNORECASE)
            if match:
                ordered.append(match.group(1))
            ordered.append(label)

        seen: set[str] = set()
        result: list[str] = []
        for label in ordered:
            normalized = normalize_model_token(label)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(label)
        return tuple(result)

    async def _ensure_configured_model_version(self, target_version_label: str) -> bool:
        """Open Configure and select the requested model-version label."""
        await self._dismiss_model_picker()
        current_label = await self._detect_current_model_label()
        opened = await self._open_model_picker(target_version_label, current_label=current_label)
        if not opened:
            return False

        await asyncio.sleep(0.3)
        configure_opened = await self._expand_advanced_picker()
        if not configure_opened:
            configure_opened = await self._click_menu_text("Configure")
        if not configure_opened:
            return False

        await asyncio.sleep(0.5)
        version_configured = await self._select_model_from_configure_dialog(
            (target_version_label,),
            target_version_label=target_version_label,
        )
        if version_configured:
            self._last_model_version_label = target_version_label
        return version_configured

    async def _click_configure_model_combobox(self) -> bool:
        """Open the model row in ChatGPT's Advanced/legacy Configure menu."""
        if await self._click_menu_text("Model"):
            return True

        dropdown_candidate = await self._page.evaluate(
            r"""
            () => {
                const normalize = (value) =>
                    (value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 &&
                        rect.height > 0 &&
                        style.visibility !== "hidden" &&
                        style.display !== "none";
                };
                const buttons = Array.from(document.querySelectorAll("[role='combobox'],button,[role='button'],[aria-haspopup]"))
                    .filter((el) => isVisible(el) && el.closest("[role='dialog'], [aria-modal='true']"));
                const candidates = [];
                for (const el of buttons) {
                    const text = normalize([
                        el.innerText || "",
                        el.textContent || "",
                        el.getAttribute("aria-label") || "",
                        el.getAttribute("title") || "",
                    ].join(" "));
                    const rect = el.getBoundingClientRect();
                    let score = 0;
                    if (text.match(/^(5[0-9]*|o[0-9]+)$/)) score += 80;
                    if ((el.getAttribute("role") || "") === "combobox") score += 120;
                    if (text.includes("model")) score += 40;
                    if (el.getAttribute("aria-haspopup")) score += 30;
                    if (rect.top < 360) score += 20;
                    if (rect.left > window.innerWidth / 2) score += 20;
                    if (score) {
                        candidates.push({
                            score,
                            top: rect.top,
                            left: rect.left,
                            x: rect.left + rect.width / 2,
                            y: rect.top + rect.height / 2,
                        });
                    }
                }
                if (!candidates.length) {
                    const dialogs = Array.from(document.querySelectorAll("[role='dialog'], [aria-modal='true']"))
                        .filter(isVisible);
                    for (const dialog of dialogs) {
                        const rows = Array.from(dialog.querySelectorAll("div"))
                            .filter((el) => isVisible(el));
                        for (const el of rows) {
                            const rawText = (el.innerText || el.textContent || "").trim();
                            const text = normalize(rawText);
                            if (!text.includes("model")) continue;
                            if (!/(5[0-9]|o[0-9]+)/.test(text)) continue;
                            const rect = el.getBoundingClientRect();
                            if (rect.height > 70 || rect.width < 120) continue;
                            candidates.push({
                                score: 70,
                                top: rect.top,
                                left: rect.left,
                                x: rect.left + rect.width - 28,
                                y: rect.top + rect.height / 2,
                            });
                        }
                    }
                }
                candidates.sort((a, b) => b.score - a.score || a.top - b.top || b.left - a.left);
                const best = candidates[0];
                return best || null;
            }
            """
        )
        if (
            not isinstance(dropdown_candidate, dict)
            or "x" not in dropdown_candidate
            or "y" not in dropdown_candidate
        ):
            return False

        await self._page.mouse.move(float(dropdown_candidate["x"]), float(dropdown_candidate["y"]), steps=8)
        await asyncio.sleep(0.05)
        await self._page.mouse.click(float(dropdown_candidate["x"]), float(dropdown_candidate["y"]))
        return True

    async def _click_configure_radio_for_version(
        self,
        target_version_token: str,
        target_labels: tuple[str, ...] = (),
    ) -> bool:
        """Click a Configure dialog radio row matching the requested mode/version."""
        candidate = await self._page.evaluate(
            r"""
            ({ targetVersionToken, targetLabels }) => {
                const normalize = (value) =>
                    (value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
                const target = normalize(targetVersionToken);
                if (!target) return null;
                const labels = (targetLabels || []).map(normalize).filter(Boolean);
                const wantsThinking = labels.some((label) => label.includes("thinking"));
                const wantsPro = labels.some((label) => label.includes("pro"));
                const wantsInstant = labels.some((label) => label.includes("instant"));
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 &&
                        rect.height > 0 &&
                        style.visibility !== "hidden" &&
                        style.display !== "none";
                };
                const radios = Array.from(document.querySelectorAll("[role='radio']"))
                    .filter(isVisible);
                const matches = [];
                for (const el of radios) {
                    const text = [
                        el.innerText || "",
                        el.textContent || "",
                        el.getAttribute("aria-label") || "",
                    ].join(" ");
                    const normalizedText = normalize(text);
                    if (!normalizedText.includes(target)) continue;
                    let score = 10;
                    if (wantsThinking && normalizedText.includes("thinking")) score += 100;
                    if (wantsPro && normalizedText.includes("pro")) score += 100;
                    if (wantsInstant && normalizedText.includes("instant")) score += 100;
                    if (!wantsThinking && !wantsPro && !wantsInstant && normalizedText.includes("instant")) {
                        score += 40;
                    }
                    const rect = el.getBoundingClientRect();
                    matches.push({
                        score,
                        top: rect.top,
                        left: rect.left,
                        x: rect.left + rect.width / 2,
                        y: rect.top + rect.height / 2,
                    });
                }
                matches.sort((a, b) => b.score - a.score || a.top - b.top || a.left - b.left);
                return matches[0] || null;
            }
            """,
            {
                "targetVersionToken": target_version_token,
                "targetLabels": list(target_labels),
            },
        )
        if not isinstance(candidate, dict) or "x" not in candidate or "y" not in candidate:
            return False

        await self._page.mouse.move(float(candidate["x"]), float(candidate["y"]), steps=8)
        await asyncio.sleep(0.05)
        await self._page.mouse.click(float(candidate["x"]), float(candidate["y"]))
        return True

    async def _click_menu_text(self, target_text: str) -> bool:
        """Click a visible menu-style element whose text matches the target."""
        for role in ("option", "menuitemradio", "menuitem", "radio"):
            try:
                locator = self._page.get_by_role(role, name=target_text, exact=True).first
                if await locator.count() > 0:
                    await locator.hover()
                    await asyncio.sleep(0.05)
                    await locator.click()
                    return True
            except Exception:
                continue

        candidate = await self._page.evaluate(
            r"""
            (targetText) => {
                const normalize = (value) =>
                    (value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
                const target = normalize(targetText);
                if (!target) return false;
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 &&
                        rect.height > 0 &&
                        style.visibility !== "hidden" &&
                        style.display !== "none";
                };
                const selectors = [
                    "[role='menuitemradio']",
                    "[role='menuitem']",
                    "[role='option']",
                    "[role='radio']",
                    "[role='button']",
                    "button",
                    "[data-radix-collection-item]",
                    "[cmdk-item]",
                    "li",
                    "div",
                ];
                const matches = [];
                for (const selector of selectors) {
                    for (const el of document.querySelectorAll(selector)) {
                        if (!isVisible(el)) continue;
                        const primaryText = ((el.innerText || el.textContent || "")
                            .split(/\n+/)
                            .map((part) => part.trim())
                            .filter(Boolean)[0] || "").trim();
                        const text = [
                            primaryText,
                            el.innerText || "",
                            el.textContent || "",
                            el.getAttribute("aria-label") || "",
                            el.getAttribute("title") || "",
                        ].join(" ").trim();
                        const normalizedText = normalize(text);
                        const normalizedPrimary = normalize(primaryText);
                        if (!normalizedText || !normalizedText.includes(target)) continue;
                        let score = 0;
                        if (normalizedPrimary === target) score += 80;
                        else if (normalizedText === target) score += 60;
                        else if (normalizedPrimary.startsWith(target)) score += 45;
                        else if (normalizedText.startsWith(target)) score += 40;
                        else score += 20;
                        const role = el.getAttribute("role") || "";
                        if (["menuitemradio", "menuitem", "option", "radio"].includes(role)) score += 25;
                        if ((el.getAttribute("data-testid") || "").includes("model-switcher")) score += 20;
                        const rect = el.getBoundingClientRect();
                        if (rect.top < 700) score += 10;
                        matches.push({
                            score,
                            top: rect.top,
                            left: rect.left,
                            area: rect.width * rect.height,
                            x: rect.left + rect.width / 2,
                            y: rect.top + rect.height / 2,
                        });
                    }
                }
                matches.sort((a, b) => b.score - a.score || a.area - b.area || a.top - b.top || a.left - b.left);
                const best = matches[0];
                return best || null;
            }
            """,
            target_text,
        )
        if not isinstance(candidate, dict) or "x" not in candidate or "y" not in candidate:
            return False

        await self._page.mouse.move(float(candidate["x"]), float(candidate["y"]), steps=8)
        await asyncio.sleep(0.05)
        await self._page.mouse.click(float(candidate["x"]), float(candidate["y"]))
        return True

    async def _wait_for_model_label(self, target_labels: tuple[str, ...]) -> bool:
        """Wait briefly until the current-model button reflects the requested label."""
        deadline = time.time() + (Config.CHATGPT_MODEL_SWITCH_TIMEOUT / 1000.0)
        while time.time() < deadline:
            current_label = await self._detect_current_model_label()
            current_normalized = normalize_model_token(current_label)
            if current_normalized in {normalize_model_token(label) for label in target_labels}:
                return True
            await asyncio.sleep(0.25)
        return False

    def _label_matches_model_option(self, label: str, option) -> bool:
        """Return whether a visible UI label matches a configured model option."""
        normalized = normalize_model_token(label)
        labels = self._model_confirmation_labels(option, self._model_setting_label_for_option(option))
        return bool(normalized and normalized in {normalize_model_token(item) for item in labels})

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

        has_non_images = any(
            Path(path).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
            for path in valid_paths
        )
        file_input = None
        file_inputs = await self._page.query_selector_all("input[type='file']")
        if has_non_images:
            for candidate in file_inputs:
                accept = (await candidate.get_attribute("accept") or "").lower()
                if "image" not in accept or "*" in accept:
                    file_input = candidate
                    log.debug("Using general file input (accept=%r)", accept)
                    break
        if file_input is None:
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

        badge_selector = ", ".join(Selectors.ATTACHMENT_BADGE)
        try:
            await self._page.wait_for_selector(
                badge_selector,
                timeout=8000,
                state="attached",
            )
            log.info("Attachment badge detected in composer")
        except Exception:
            log.debug("Attachment badge wait timed out, using fallback sleep")
            await asyncio.sleep(3)
            if len(valid_paths) > 1:
                await asyncio.sleep(len(valid_paths))
        log.info("File upload complete")

    def _extract_thread_id(self) -> str:
        """Extract the thread/conversation ID from the current URL."""
        url = self._page.url
        match = re.search(r"/c/([a-f0-9-]+)", url)
        return match.group(1) if match else ""
