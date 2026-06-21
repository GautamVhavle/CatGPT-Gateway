"""
Response completion detector for Claude.ai.

Primary strategy: detect completion using Claude's data-is-streaming attribute
and copy-button appearance, then extract from the latest assistant turn.
"""

from __future__ import annotations

import asyncio
import re

from patchright.async_api import Page

from src.browser.human import idle_mouse_movement
from src.log import setup_logging
from src.config import Config

log = setup_logging("claude_detector")


def normalize_assistant_text(text: str | None) -> str:
    """Normalize extracted assistant text for validation and comparisons."""
    cleaned = (text or "").strip()
    # Claude prefixes with "Claude responded: <title>" in sr-only heading
    cleaned = re.sub(r"^Claude responded:\s*.*?\n", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def is_incomplete_response_text(text: str | None) -> bool:
    """Heuristic: true when text looks like transient thinking/analyzing status."""
    cleaned = normalize_assistant_text(text)
    if not cleaned:
        return True

    lower = cleaned.lower()
    markers = [
        "thinking",
        "analyzing",
        "searching",
        "working on",
        "please wait",
    ]

    if any(marker in lower for marker in markers):
        if len(cleaned) < 240:
            return True
        if lower.startswith(tuple(markers)):
            return True

    return False


def _empty_snapshot() -> dict:
    return {
        "found": False,
        "index": -1,
        "signature": None,
        "hasCopyButton": False,
        "isStreaming": True,
        "text": "",
    }


async def _latest_assistant_turn_snapshot(page: Page) -> dict:
    """
    Return metadata for the latest assistant turn in Claude.ai.

    Claude wraps each assistant response in a div with data-is-streaming attribute.
    The sr-only h2 reads "Claude responded: <title>".
    """
    snapshot = await page.evaluate(
        """
        () => {
            // Claude assistant turns are divs with data-is-streaming attribute
            const turns = Array.from(document.querySelectorAll('div[data-is-streaming]'));

            if (turns.length === 0) {
                return {
                    found: false,
                    index: -1,
                    signature: null,
                    hasCopyButton: false,
                    isStreaming: true,
                    text: '',
                };
            }

            const last = turns[turns.length - 1];
            const idx = turns.length - 1;

            // Build a stable signature from the turn
            const h2 = last.querySelector('h2.sr-only');
            const h2Text = h2 ? h2.innerText.trim() : '';
            const signature = `${idx}:${h2Text.substring(0, 50)}`;

            const isStreaming = last.getAttribute('data-is-streaming') === 'true';

            const hasCopyButton = Boolean(
                last.querySelector('button[data-testid="action-bar-copy"], button[aria-label="Copy"]')
            );

            // Get the actual response text (not the sr-only heading)
            const responseDiv = last.querySelector('.font-claude-response');
            const text = responseDiv
                ? responseDiv.innerText.trim()
                : last.innerText.trim();

            return {
                found: true,
                index: idx,
                signature,
                hasCopyButton,
                isStreaming,
                text,
            };
        }
        """
    )

    if not isinstance(snapshot, dict):
        return _empty_snapshot()

    normalized = _empty_snapshot()
    normalized.update(snapshot)
    return normalized


async def get_latest_assistant_turn_signature(page: Page) -> str | None:
    """Return signature for the latest assistant turn, if available."""
    snapshot = await _latest_assistant_turn_snapshot(page)
    signature = snapshot.get("signature")
    return signature if isinstance(signature, str) and signature else None


async def count_assistant_messages(page: Page) -> int:
    """Count assistant turns (based on data-is-streaming divs)."""
    count = await page.evaluate(
        """
        () => {
            return document.querySelectorAll('div[data-is-streaming]').length;
        }
        """
    )
    return int(count or 0)


async def _count_copy_buttons(page: Page) -> int:
    """Count assistant turns that currently expose a copy button."""
    count = await page.evaluate(
        """
        () => {
            const turns = Array.from(document.querySelectorAll('div[data-is-streaming]'));
            let total = 0;
            for (const turn of turns) {
                if (turn.querySelector('button[data-testid="action-bar-copy"], button[aria-label="Copy"]')) {
                    total++;
                }
            }
            return total;
        }
        """
    )
    return int(count or 0)


async def _wait_for_new_turn_signature(
    page: Page,
    previous_turn_signature: str,
    timeout_ms: int,
) -> bool:
    """Wait until latest assistant-turn signature differs from previous one."""
    elapsed = 0
    poll_interval = Config.POLL_INTERVAL_MS / 1000
    heartbeat = 10

    while elapsed * 1000 < timeout_ms:
        snapshot = await _latest_assistant_turn_snapshot(page)
        signature = snapshot.get("signature")
        if isinstance(signature, str) and signature and signature != previous_turn_signature:
            log.debug(f"New assistant turn detected: {signature} (prev: {previous_turn_signature})")
            return True

        if elapsed > 0 and elapsed % heartbeat == 0:
            log.debug(f"Still waiting for new assistant turn... ({int(elapsed)}s)")

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    log.debug("Timed out waiting for a new assistant-turn signature")
    return False


async def wait_for_response_complete(
    page: Page,
    expected_msg_count: int | None = None,
    timeout_ms: int | None = None,
    previous_turn_signature: str | None = None,
) -> bool:
    """
    Wait until Claude finishes generating the current response.

    Primary signal: data-is-streaming attribute changes from "true" to "false".
    Secondary: copy button appears on the latest turn.
    """
    timeout = timeout_ms or Config.RESPONSE_TIMEOUT
    log.info(f"Waiting for response (timeout: {timeout}ms)...")

    pre_copy_count = await _count_copy_buttons(page)
    log.debug(f"Copy buttons before send: {pre_copy_count}")

    # Wait for new turn to appear
    if previous_turn_signature:
        log.debug(f"Previous assistant turn signature: {previous_turn_signature}")
        await _wait_for_new_turn_signature(page, previous_turn_signature, timeout_ms=30000)
    elif expected_msg_count is not None:
        log.debug(f"Waiting for assistant message #{expected_msg_count}...")
        waited = 0
        while waited < 30000:
            current_count = await count_assistant_messages(page)
            if current_count >= expected_msg_count:
                log.debug(f"Assistant message target reached (count: {current_count})")
                break
            await asyncio.sleep(0.5)
            waited += 500

    # Strategy 1: Wait for streaming to complete (most reliable for Claude)
    log.debug("Waiting for streaming to complete (data-is-streaming='false')...")
    completed = await _wait_for_streaming_complete(page, timeout, previous_turn_signature)
    if completed:
        log.info("Response complete — streaming finished")
        return True

    # Strategy 2: Wait for copy button
    log.debug("Waiting for copy button on latest assistant turn...")
    copy_detected = await _wait_for_copy_button(page, pre_copy_count, timeout, previous_turn_signature)
    if copy_detected:
        log.info("Response complete — copy button appeared on latest turn")
        return True

    # Strategy 3: Text stability fallback
    log.info("Falling back to text-stability detection...")
    try:
        return await _wait_via_text_stability(page, timeout, previous_turn_signature)
    except Exception as e:
        log.error(f"All strategies failed: {e}")
        return False


async def _wait_for_streaming_complete(
    page: Page,
    timeout_ms: int,
    previous_turn_signature: str | None = None,
) -> bool:
    """
    Wait for Claude's data-is-streaming attribute to become "false".
    This is the primary and most reliable completion signal for Claude.
    """
    elapsed = 0
    poll_interval = Config.POLL_INTERVAL_MS / 1000
    heartbeat = 10

    while elapsed * 1000 < timeout_ms:
        snapshot = await _latest_assistant_turn_snapshot(page)
        signature = snapshot.get("signature")
        is_new_turn = previous_turn_signature is None or (
            isinstance(signature, str) and signature != previous_turn_signature
        )

        if is_new_turn and snapshot.get("found") and not snapshot.get("isStreaming"):
            # Double-check by waiting a moment for DOM to settle
            await asyncio.sleep(0.5)
            verify = await _latest_assistant_turn_snapshot(page)
            if not verify.get("isStreaming"):
                log.debug(f"Streaming complete on turn {signature}")
                return True

        if elapsed > 0 and elapsed % heartbeat == 0:
            log.debug(f"Still streaming... ({int(elapsed)}s)")
            await idle_mouse_movement(page)

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    log.warning(f"Streaming did not complete after {int(elapsed)}s")
    return False


async def _wait_for_copy_button(
    page: Page,
    pre_count: int,
    timeout_ms: int,
    previous_turn_signature: str | None = None,
) -> bool:
    """Wait for copy button to appear on the latest assistant turn."""
    elapsed = 0
    poll_interval = Config.POLL_INTERVAL_MS / 1000

    while elapsed * 1000 < timeout_ms:
        snapshot = await _latest_assistant_turn_snapshot(page)
        signature = snapshot.get("signature")
        is_new_turn = previous_turn_signature is None or (
            isinstance(signature, str) and signature != previous_turn_signature
        )

        if is_new_turn and snapshot.get("hasCopyButton"):
            current_count = await _count_copy_buttons(page)
            log.debug(
                f"Copy button detected on latest turn {signature} "
                f"(copy-buttons: {pre_count} -> {current_count})"
            )
            return True

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    return False


async def _wait_via_text_stability(
    page: Page,
    timeout_ms: int,
    previous_turn_signature: str | None = None,
) -> bool:
    """
    Last resort: poll latest assistant-turn text and wait until stable.
    """
    stable_count = 0
    required_stable = 3
    last_text = ""
    elapsed = 0
    poll_interval = Config.POLL_INTERVAL_MS / 1000

    while elapsed * 1000 < timeout_ms:
        snapshot = await _latest_assistant_turn_snapshot(page)
        signature = snapshot.get("signature")
        text = snapshot.get("text") if isinstance(snapshot.get("text"), str) else ""

        if previous_turn_signature and signature == previous_turn_signature:
            stable_count = 0
            last_text = ""
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            continue

        if text and text == last_text:
            stable_count += 1
            log.debug(f"Text stable ({stable_count}/{required_stable})")
            if stable_count >= required_stable:
                if is_incomplete_response_text(text):
                    log.debug("Stable text looks like transient status; continuing wait")
                    stable_count = 0
                    last_text = text
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval
                    continue
                log.info("Response text stabilized — complete")
                return True
        else:
            stable_count = 0
            last_text = text

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    log.warning(f"Text stability timed out after {int(elapsed)}s")
    return False


async def extract_last_response_via_copy(
    page: Page,
    previous_turn_signature: str | None = None,
) -> str:
    """
    Extract latest assistant response by clicking copy on the latest turn.
    """
    log.debug("Attempting extraction via latest-turn copy button...")

    try:
        await page.context.grant_permissions(["clipboard-read", "clipboard-write"])

        if previous_turn_signature:
            await _wait_for_new_turn_signature(page, previous_turn_signature, timeout_ms=8000)

        pre_clipboard = await page.evaluate("navigator.clipboard.readText().catch(() => '')")
        await page.evaluate("navigator.clipboard.writeText('').catch(() => {})")

        click_result = await page.evaluate(
            """
            (previousSignature) => {
                const turns = Array.from(document.querySelectorAll('div[data-is-streaming]'));

                for (let idx = turns.length - 1; idx >= 0; idx--) {
                    const turn = turns[idx];

                    const h2 = turn.querySelector('h2.sr-only');
                    const h2Text = h2 ? h2.innerText.trim() : '';
                    const signature = `${idx}:${h2Text.substring(0, 50)}`;

                    if (previousSignature && signature === previousSignature) {
                        return { clicked: false, reason: 'stale-turn', signature };
                    }

                    const btn = turn.querySelector(
                        'button[data-testid="action-bar-copy"], button[aria-label="Copy"]'
                    );
                    if (!btn) {
                        return { clicked: false, reason: 'no-copy-button', signature };
                    }

                    btn.click();
                    return { clicked: true, reason: 'ok', signature };
                }

                return { clicked: false, reason: 'no-assistant-turn', signature: null };
            }
            """,
            previous_turn_signature,
        )

        if isinstance(click_result, dict) and click_result.get("clicked"):
            await asyncio.sleep(0.8)
            content = await page.evaluate("navigator.clipboard.readText().catch(() => '')")
            if content and content.strip() and content.strip() != str(pre_clipboard).strip():
                log.info(
                    "Extracted via copy button (latest-turn): "
                    f"{len(content)} chars, turn={click_result.get('signature')}"
                )
                return content.strip()
            log.debug("Clipboard unchanged/empty after latest-turn copy click")
        else:
            reason = click_result.get("reason") if isinstance(click_result, dict) else "unknown"
            log.debug(f"Latest-turn copy click not used: {reason}")

    except Exception as e:
        log.warning(f"Copy button extraction failed: {e}")

    log.info("Falling back to latest-turn DOM extraction...")
    return await _extract_via_dom(page, previous_turn_signature)


async def _extract_via_dom(
    page: Page,
    previous_turn_signature: str | None = None,
) -> str:
    """Fallback extraction: innerText from latest assistant turn only."""
    text = await page.evaluate(
        """
        (previousSignature) => {
            const turns = Array.from(document.querySelectorAll('div[data-is-streaming]'));

            for (let idx = turns.length - 1; idx >= 0; idx--) {
                const turn = turns[idx];

                const h2 = turn.querySelector('h2.sr-only');
                const h2Text = h2 ? h2.innerText.trim() : '';
                const signature = `${idx}:${h2Text.substring(0, 50)}`;

                if (previousSignature && signature === previousSignature) {
                    return '';
                }

                // Get text from the font-claude-response div
                const responseDiv = turn.querySelector('.font-claude-response');
                if (responseDiv) {
                    return responseDiv.innerText.trim();
                }

                // Fallback to full turn text, stripping sr-only heading
                const full = turn.innerText.trim();
                return full.replace(/^Claude responded:.*?\\n/i, '').trim();
            }
            return '';
        }
        """,
        previous_turn_signature,
    )
    cleaned = normalize_assistant_text(text or "")
    if cleaned:
        log.info(f"Extracted via DOM: {len(cleaned)} chars")
    else:
        log.warning("DOM extraction returned empty text")
    return cleaned
