"""
Response completion detector — knows when ChatGPT has finished streaming.

Primary strategy: Wait for the COPY BUTTON to appear on the last assistant
message. This is the most reliable signal because ChatGPT only shows the
copy button after the response is fully generated.

Fallback strategies:
1. Stop button lifecycle (appear → disappear)
2. Text stability polling
"""

from __future__ import annotations

import asyncio

from patchright.async_api import Page

from src.selectors import Selectors
from src.browser.human import idle_mouse_movement
from src.log import setup_logging
from src.config import Config

log = setup_logging("detector")


async def count_assistant_messages(page: Page) -> int:
    """
    Count assistant messages on the page.

    Includes both standard text responses (data-message-author-role="assistant")
    and image/agent turns (which use class="agent-turn" without the role attr).
    """
    count = await page.evaluate("""
        () => {
            // Standard assistant messages
            const textMsgs = document.querySelectorAll(
                '[data-message-author-role="assistant"]'
            );
            // Agent/image turns (no role attr, but have .agent-turn class)
            const agentTurns = document.querySelectorAll('.agent-turn');
            // Count unique turns (dedup via Set of ancestor articles)
            const articles = new Set();
            for (const el of [...textMsgs, ...agentTurns]) {
                let container = el;
                for (let i = 0; i < 15; i++) {
                    if (!container.parentElement) break;
                    container = container.parentElement;
                    if (container.tagName === 'ARTICLE') {
                        articles.add(container);
                        break;
                    }
                }
            }
            return articles.size;
        }
    """)
    return count or 0


async def _detect_image_in_latest_turn(page: Page) -> bool:
    """
    Check if the latest conversation turn contains a generated image.

    ChatGPT DALL-E image responses don't have data-message-author-role.
    They appear in the last article turn with img[alt="Generated image"]
    or inside a div[id^="image-"] container.
    """
    return await page.evaluate("""
        () => {
            const articles = document.querySelectorAll('article');
            if (articles.length === 0) return false;
            const last = articles[articles.length - 1];

            // Check for generated images
            const imgs = last.querySelectorAll('img[alt="Generated image"]');
            if (imgs.length > 0) return true;

            // Check for image containers
            const containers = last.querySelectorAll('div[id^="image-"]');
            if (containers.length > 0) return true;

            // Check for large images from chatgpt backend
            const allImgs = last.querySelectorAll('img');
            for (const img of allImgs) {
                const w = img.naturalWidth || img.width || 0;
                const src = img.src || '';
                if (w > 200 && (
                    src.includes('backend-api/estuary') ||
                    src.includes('chatgpt.com') && w > 500
                )) {
                    return true;
                }
            }

            return false;
        }
    """) or False


async def _count_copy_buttons(page: Page) -> int:
    """
    Count copy buttons that belong to ASSISTANT messages only.

    ChatGPT may also show copy/edit buttons on user turns, so we must
    only count buttons that are within an assistant turn container.
    Uses JavaScript to walk the DOM and check the context of each button.
    """
    count = await page.evaluate("""
        () => {
            // Find all copy buttons on the page
            const buttons = document.querySelectorAll(
                'button[data-testid="copy-turn-action-button"], button[aria-label="Copy"]'
            );
            let assistantCount = 0;
            for (const btn of buttons) {
                // Walk up to find the turn container
                let el = btn;
                let isAssistant = false;
                for (let i = 0; i < 15; i++) {
                    if (!el.parentElement) break;
                    el = el.parentElement;
                    // Check if this turn contains an assistant message
                    if (el.querySelector('[data-message-author-role="assistant"]')) {
                        isAssistant = true;
                        break;
                    }
                    // Stop at article boundary
                    if (el.tagName === 'ARTICLE') break;
                }
                if (isAssistant) assistantCount++;
            }
            return assistantCount;
        }
    """)
    return count or 0


async def wait_for_response_complete(
    page: Page,
    expected_msg_count: int | None = None,
    timeout_ms: int | None = None,
) -> bool:
    """
    Wait until ChatGPT finishes generating its response.

    Strategy (in order):
    1. Count copy buttons before. Wait for a NEW copy button to appear.
       The copy button only shows after the response is fully streamed.
    2. If copy-button detection fails, use stop-button lifecycle.
    3. If that also fails, fall back to text stability polling.

    Returns True if response completed, False if timed out.
    """
    timeout = timeout_ms or Config.RESPONSE_TIMEOUT
    log.info(f"Waiting for response (timeout: {timeout}ms)...")

    # Count copy buttons BEFORE the response starts
    pre_copy_count = await _count_copy_buttons(page)
    log.debug(f"Copy buttons before send: {pre_copy_count}")

    # Phase 0: Wait for a new assistant message OR image turn to appear
    if expected_msg_count is not None:
        log.debug(f"Waiting for assistant message #{expected_msg_count}...")
        waited = 0
        while waited < 30000:
            current_count = await count_assistant_messages(page)
            if current_count >= expected_msg_count:
                log.debug(f"New assistant message appeared (count: {current_count})")
                break
            await asyncio.sleep(0.5)
            waited += 500

    # Strategy 1: Wait for a NEW copy button OR an image (definitive signals)
    log.debug("Waiting for new copy button or image...")
    completed = await _wait_for_copy_button_or_image(page, pre_copy_count, timeout)
    if completed == "copy":
        log.info("Response complete — copy button appeared")
        return True
    elif completed == "image":
        log.info("Response complete — generated image detected")
        return True

    # Strategy 2: Stop button lifecycle (fallback)
    log.info("Copy button not detected, trying stop-button strategy...")
    try:
        result = await _wait_via_stop_button(page, timeout)
        if result:
            # Double-check with a quick copy-button check
            await asyncio.sleep(2)
            post_copy = await _count_copy_buttons(page)
            if post_copy > pre_copy_count:
                log.info("Confirmed via copy button after stop-button strategy")
            return True
    except Exception as e:
        log.debug(f"Stop button strategy failed: {e}")

    # Strategy 3: Text stability (last resort)
    log.info("Falling back to text-stability detection...")
    try:
        return await _wait_via_text_stability(page, timeout)
    except Exception as e:
        log.error(f"All strategies failed: {e}")
        return False


async def _wait_for_copy_button_or_image(
    page: Page, pre_count: int, timeout_ms: int
) -> str | None:
    """
    Wait for either:
    - A new copy button (text response completed), OR
    - A generated image (DALL-E image response completed)

    Returns "copy", "image", or None if timed out.
    """
    elapsed = 0
    poll_interval = 1.0  # seconds
    heartbeat = 10

    while elapsed * 1000 < timeout_ms:
        # Check for new copy button
        current_count = await _count_copy_buttons(page)
        if current_count > pre_count:
            log.debug(f"New copy button detected (was {pre_count}, now {current_count})")
            return "copy"

        # Check for generated image in latest turn
        has_image = await _detect_image_in_latest_turn(page)
        if has_image:
            # Wait a bit for the image to fully load
            await asyncio.sleep(2)
            log.debug("Generated image detected in latest turn")
            return "image"

        if elapsed > 0 and elapsed % heartbeat == 0:
            log.debug(f"Still waiting for copy button or image... ({int(elapsed)}s)")
            await idle_mouse_movement(page)

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    log.warning(f"Neither copy button nor image found after {int(elapsed)}s")
    return None


async def _wait_via_stop_button(page: Page, timeout_ms: int) -> bool:
    """
    Wait for the stop button to appear (response started), then disappear
    (response finished).
    """
    stop_selector = ", ".join(Selectors.STOP_BUTTON)
    log.debug("Waiting for stop button to appear...")

    try:
        await page.wait_for_selector(stop_selector, state="visible", timeout=15000)
        log.info("Stop button appeared — response is streaming")
    except Exception:
        log.debug("Stop button never appeared (short response or selector changed)")
        return False

    log.debug("Waiting for stop button to disappear...")
    heartbeat_interval = 10
    elapsed = 0

    while elapsed * 1000 < timeout_ms:
        try:
            await page.wait_for_selector(
                stop_selector, state="hidden", timeout=heartbeat_interval * 1000
            )
            log.info("Stop button disappeared — streaming done")
            return True
        except Exception:
            elapsed += heartbeat_interval
            log.debug(f"Still streaming... ({elapsed}s elapsed)")
            await idle_mouse_movement(page)

    log.warning(f"Timed out after {elapsed}s waiting for stop button")
    return False


async def _wait_via_text_stability(page: Page, timeout_ms: int) -> bool:
    """
    Last resort: Poll the last assistant message text and wait until
    it stops changing for 5 consecutive seconds.
    """
    js_code = """
    () => {
        // Try standard assistant messages first
        const msgs = document.querySelectorAll('[data-message-author-role="assistant"]');
        if (msgs.length > 0) {
            const last = msgs[msgs.length - 1];
            return last.innerText || last.textContent || '';
        }
        // Fallback: check agent turns (image responses)
        const agents = document.querySelectorAll('.agent-turn');
        if (agents.length > 0) {
            const last = agents[agents.length - 1];
            return last.innerText || last.textContent || '';
        }
        // Last resort: check last article
        const articles = document.querySelectorAll('article');
        if (articles.length > 0) {
            const last = articles[articles.length - 1];
            return last.innerText || last.textContent || '';
        }
        return null;
    }
    """

    stable_count = 0
    required_stable = 5  # 5 consecutive stable seconds
    last_text = ""
    elapsed = 0
    poll_interval = 1.0

    while elapsed * 1000 < timeout_ms:
        try:
            current_text = await page.evaluate(js_code)
            if current_text is None:
                stable_count = 0
            elif current_text == last_text and len(current_text) > 0:
                stable_count += 1
                log.debug(f"Text stable ({stable_count}/{required_stable})")
                if stable_count >= required_stable:
                    log.info("Response text stabilized — complete")
                    return True
            else:
                stable_count = 0
                last_text = current_text
        except Exception as e:
            log.debug(f"Polling error: {e}")

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    log.warning(f"Text stability timed out after {int(elapsed)}s")
    return False


async def extract_last_response_via_copy(page: Page) -> str:
    """
    Extract the last assistant response by clicking the copy button.

    This is the most reliable extraction method because:
    - It copies the exact markdown content (tables, code blocks, formatting)
    - It's what the user would get if they clicked "Copy" manually
    - It works regardless of how the DOM renders the content

    For image responses (no copy button), falls back to DOM text extraction
    from the turn container.

    Falls back to DOM text extraction if copy button approach fails.
    """
    log.debug("Attempting extraction via copy button...")

    try:
        # Strategy A: Find the last assistant message, then locate the copy
        # button near it. ChatGPT structures each turn as an article element.
        last_assistant = None
        for selector in Selectors.ASSISTANT_MESSAGE:
            elements = await page.query_selector_all(selector)
            if elements:
                last_assistant = elements[-1]
                break

        # If no standard assistant message, check for agent turns
        if not last_assistant:
            agents = await page.query_selector_all(".agent-turn")
            if agents:
                last_assistant = agents[-1]

        if last_assistant:
            # Grant clipboard permissions BEFORE clicking
            await page.context.grant_permissions(["clipboard-read", "clipboard-write"])
            await page.evaluate("navigator.clipboard.writeText('')")

            # The copy button lives in the same turn container as the message.
            # Walk up to the closest article/turn container, then find the copy
            # button inside it.
            copy_button = await page.evaluate("""
                (assistantEl) => {
                    // Walk up to find the turn/article wrapper
                    let container = assistantEl;
                    for (let i = 0; i < 10; i++) {
                        if (!container.parentElement) break;
                        container = container.parentElement;
                        // ChatGPT wraps each turn in an article or a div with data-testid
                        if (container.tagName === 'ARTICLE' ||
                            container.getAttribute('data-testid')?.includes('conversation-turn')) {
                            break;
                        }
                    }
                    // Find copy button within this container
                    const btn = container.querySelector(
                        'button[data-testid="copy-turn-action-button"], button[aria-label="Copy"]'
                    );
                    if (btn) {
                        btn.click();
                        return true;
                    }
                    return false;
                }
            """, last_assistant)

            if copy_button:
                await asyncio.sleep(0.8)

                content = await page.evaluate("navigator.clipboard.readText()")
                if content and content.strip():
                    log.info(f"Extracted via copy button (scoped): {len(content)} chars")
                    return content.strip()
                else:
                    log.debug("Clipboard empty after scoped copy button click")

        # Strategy B: Fallback — try clicking all copy buttons, take the last one
        # that belongs to an assistant message
        for selector in Selectors.COPY_BUTTON:
            buttons = await page.query_selector_all(selector)
            if buttons:
                # Grant clipboard permissions first
                await page.context.grant_permissions(["clipboard-read", "clipboard-write"])

                # Try from last button backwards
                for btn in reversed(buttons):
                    await page.evaluate("navigator.clipboard.writeText('')")
                    await page.evaluate("btn => btn.click()", btn)
                    await asyncio.sleep(0.8)

                    content = await page.evaluate("navigator.clipboard.readText()")
                    if content and content.strip():
                        log.info(f"Extracted via copy button (fallback): {len(content)} chars")
                        return content.strip()

    except Exception as e:
        log.warning(f"Copy button extraction failed: {e}")

    # Fallback: DOM text extraction
    log.info("Falling back to DOM text extraction...")
    return await _extract_via_dom(page)


async def _extract_via_dom(page: Page) -> str:
    """
    Fallback extraction: read innerText from the last assistant message DOM.
    Handles both standard messages and agent/image turns.
    """
    # Try standard assistant markdown containers
    for selector in Selectors.ASSISTANT_MARKDOWN:
        try:
            elements = await page.query_selector_all(selector)
            if elements:
                last = elements[-1]
                text = await last.inner_text()
                if text and text.strip():
                    log.debug(f"Extracted via DOM ({selector}): {len(text)} chars")
                    return text.strip()
        except Exception:
            continue

    # Try standard assistant messages
    for selector in Selectors.ASSISTANT_MESSAGE:
        try:
            elements = await page.query_selector_all(selector)
            if elements:
                last = elements[-1]
                text = await last.inner_text()
                if text and text.strip():
                    log.debug(f"Extracted via DOM ({selector}): {len(text)} chars")
                    return text.strip()
        except Exception:
            continue

    # Try agent turns (image/tool responses)
    try:
        agents = await page.query_selector_all(".agent-turn")
        if agents:
            last = agents[-1]
            text = await last.inner_text()
            if text and text.strip():
                log.debug(f"Extracted via DOM (.agent-turn): {len(text)} chars")
                return text.strip()
    except Exception:
        pass

    # Last resort: try last article's text
    try:
        text = await page.evaluate("""
            () => {
                const articles = document.querySelectorAll('article');
                if (articles.length === 0) return '';
                const last = articles[articles.length - 1];
                return (last.innerText || '').trim();
            }
        """)
        if text:
            log.debug(f"Extracted via DOM (last article): {len(text)} chars")
            return text
    except Exception:
        pass

    log.error("Could not extract any assistant response")
    return ""


# Keep old name as alias for backward compat
extract_last_response = extract_last_response_via_copy
