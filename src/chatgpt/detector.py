"""
Response completion detector.

Primary strategy: detect completion on the newest assistant turn only,
then extract from that same turn. This avoids one-turn lag where a
previous assistant response is returned for the current request.
"""

from __future__ import annotations

import asyncio
import re

from patchright.async_api import Page
from patchright._impl._errors import TargetClosedError

from src.selectors import Selectors
from src.browser.human import idle_mouse_movement
from src.log import setup_logging
from src.config import Config

log = setup_logging("detector")


def normalize_assistant_text(text: str | None) -> str:
    """Normalize extracted assistant text for validation and comparisons."""
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^ChatGPT said:\s*", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^You said:\s*", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def is_incomplete_response_text(text: str | None) -> bool:
    """
    Heuristic: true when text looks like transient "thinking/searching" UI status.
    """
    cleaned = normalize_assistant_text(text)
    if not cleaned:
        return True

    lower = cleaned.lower()
    markers = [
        "pro thinking",
        "thinking",
        "searching for",
        "searching the web",
        "analyzing",
        "working on",
        "please wait",
        "gathering",
    ]

    if any(marker in lower for marker in markers):
        if len(cleaned) < 240:
            return True
        if lower.startswith(
            (
                "pro thinking",
                "thinking",
                "searching",
                "analyzing",
                "working on",
                "gathering",
            )
        ):
            return True

    return False


def _empty_snapshot() -> dict:
    return {
        "found": False,
        "index": -1,
        "signature": None,
        "hasCopyButton": False,
        "hasImage": False,
        "text": "",
    }


async def _dump_all_turns(page: Page) -> list[dict]:
    """Dump info about all conversation turns for debugging."""
    try:
        turns = await page.evaluate(
            """
            () => {
                const turns = Array.from(document.querySelectorAll('section[data-testid^="conversation-turn-"]'));
                return turns.map((turn, idx) => {
                    const role = turn.getAttribute('data-turn') || 'unknown';
                    const testid = turn.getAttribute('data-testid') || '';
                    const turnId = turn.getAttribute('data-turn-id') || '';
                    const text = (turn.innerText || '').trim().substring(0, 100);
                    const buttons = turn.querySelectorAll('button').length;
                    const copyBtn = Boolean(
                        turn.querySelector('button[aria-label="Copy response"]') ||
                        turn.querySelector('button[data-testid="copy-turn-action-button"]') ||
                        turn.querySelector('button[aria-label="Copy message"]')
                    );
                    const hasArticle = Boolean(turn.querySelector('article'));
                    const childTags = Array.from(turn.children).map(c => c.tagName).join(',');
                    return { idx, role, testid, turnId, text, buttons, copyBtn, hasArticle, childTags };
                });
            }
            """
        )
        return turns or []
    except Exception as e:
        log.debug(f"_dump_all_turns failed: {e}")
        return []


async def _latest_assistant_turn_snapshot(page: Page) -> dict:
    """
    Return metadata for the latest assistant turn (article ordered).

    signature format: "<article-index>:<stable-id>"
    where stable-id is best-effort from DOM attributes.
    """
    snapshot = await page.evaluate(
        """
        () => {
            const turns = Array.from(document.querySelectorAll('section[data-testid^="conversation-turn-"]'));

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

                const hasCopyButton = Boolean(
                    turn.querySelector('button[aria-label="Copy response"]') ||
                    turn.querySelector('button[data-testid="copy-turn-action-button"]') ||
                    turn.querySelector('button[aria-label="Copy message"]')
                );

                const hasImage = Boolean(
                    turn.querySelector('img[alt="Generated image"], div[id^="image-"] img, div[id^="image-"]')
                );

                const text = (turn.innerText || '').trim();

                return {
                    found: true,
                    index: idx,
                    signature: `${idx}:${stableId}`,
                    hasCopyButton,
                    hasImage,
                    text,
                };
            }

            return {
                found: false,
                index: -1,
                signature: null,
                hasCopyButton: false,
                hasImage: false,
                text: '',
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
    """Count assistant turns (article based, newest-UI friendly)."""
    count = await page.evaluate(
        """
        () => {
            const turns = Array.from(document.querySelectorAll('section[data-testid^="conversation-turn-"]'));

            let total = 0;
            for (const turn of turns) {
                const turnRole = turn.getAttribute('data-turn');
                const hasAssistantRole = turnRole === 'assistant' ||
                    Boolean(turn.querySelector('[data-message-author-role="assistant"]'));
                if (hasAssistantRole) total++;
            }
            return total;
        }
        """
    )
    return int(count or 0)


async def _detect_image_in_latest_turn(
    page: Page, previous_turn_signature: str | None = None
) -> bool:
    """Check if the newest assistant turn (not previous turn) contains an image."""
    snapshot = await _latest_assistant_turn_snapshot(page)
    signature = snapshot.get("signature")
    is_new_turn = previous_turn_signature is None or (
        isinstance(signature, str) and signature != previous_turn_signature
    )
    return bool(is_new_turn and snapshot.get("hasImage"))


async def _count_copy_buttons(page: Page) -> int:
    """Count assistant turns that currently expose a copy button."""
    count = await page.evaluate(
        """
        () => {
            const turns = Array.from(document.querySelectorAll('section[data-testid^="conversation-turn-"]'));

            let total = 0;
            for (const turn of turns) {
                const turnRole = turn.getAttribute('data-turn');
                const hasAssistantRole = turnRole === 'assistant' ||
                    Boolean(turn.querySelector('[data-message-author-role="assistant"]'));
                if (!hasAssistantRole) continue;
                const hasCopyButton =
                    turn.querySelector('button[aria-label="Copy response"]') ||
                    turn.querySelector('button[data-testid="copy-turn-action-button"]') ||
                    turn.querySelector('button[aria-label="Copy message"]');
                if (hasCopyButton) total++;
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
        if (
            isinstance(signature, str)
            and signature
            and signature != previous_turn_signature
        ):
            log.debug(
                f"New assistant turn detected: {signature} (prev: {previous_turn_signature})"
            )
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
    Wait until ChatGPT finishes generating the current response.

    Uses latest-turn alignment to avoid returning stale previous-turn output.
    """
    timeout = timeout_ms or Config.RESPONSE_TIMEOUT
    log.info(f"Waiting for response (timeout: {timeout}ms)...")

    pre_copy_count = await _count_copy_buttons(page)
    log.debug(f"Copy buttons before send: {pre_copy_count}")

    if previous_turn_signature:
        log.debug(f"Previous assistant turn signature: {previous_turn_signature}")
        await _wait_for_new_turn_signature(
            page, previous_turn_signature, timeout_ms=30000
        )
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

    log.debug("Waiting for copy button or image on latest assistant turn...")
    completed = await _wait_for_copy_button_or_image(
        page, timeout, previous_turn_signature
    )
    if completed == "copy":
        log.info("Response complete — copy button appeared on latest turn")
        return True
    if completed == "image":
        log.info("Response complete — generated image detected on latest turn")
        return True

    log.info("Copy/image completion not detected, trying stop-button strategy...")
    try:
        result = await _wait_via_stop_button(page, timeout)
        if result:
            return True
    except Exception as e:
        log.debug(f"Stop button strategy failed: {e}")

    log.info("Falling back to text-stability detection...")
    try:
        return await _wait_via_text_stability(page, timeout, previous_turn_signature)
    except Exception as e:
        log.error(f"All strategies failed: {e}")
        return False


async def _check_page_error(page: Page) -> str | None:
    """Check if the page is showing an error state (DNS failure, crash, etc.).

    Returns error description string if an error is detected, None otherwise.
    """
    try:
        error = await page.evaluate(
            """
            () => {
                // Chrome error pages
                const body = document.body ? document.body.innerText : '';
                if (body.includes('DNS_PROBE_FINISHED_NXDOMAIN')) return 'DNS_PROBE_FINISHED_NXDOMAIN';
                if (body.includes('ERR_NAME_NOT_RESOLVED')) return 'ERR_NAME_NOT_RESOLVED';
                if (body.includes('ERR_CONNECTION_REFUSED')) return 'ERR_CONNECTION_REFUSED';
                if (body.includes('ERR_INTERNET_DISCONNECTED')) return 'ERR_INTERNET_DISCONNECTED';
                if (body.includes('ERR_CONNECTION_TIMED_OUT')) return 'ERR_CONNECTION_TIMED_OUT';
                // ChatGPT error states
                if (body.includes('Something went wrong')) return 'ChatGPT_something_went_wrong';
                if (body.includes("We're experiencing high demand")) return 'ChatGPT_high_demand';
                if (document.title && document.title.includes('is not available')) return 'page_not_available';
                return null;
            }
            """
        )
        return error
    except Exception:
        return None


async def _wait_for_copy_button_or_image(
    page: Page,
    timeout_ms: int,
    previous_turn_signature: str | None = None,
) -> str | None:
    """
    Wait for either copy-button readiness or generated image on the latest turn.

    Returns "copy", "image", or None if timed out.

    NOTE: On current chatgpt.com, the per-turn action toolbar (including the
    Copy button) mounts as soon as the assistant turn starts streaming, not
    only once generation finishes -- so copy-button *presence* alone is not
    a reliable completion signal. A Stop-button visibility check was tried
    here previously, but Selectors.STOP_BUTTON can silently go stale (it
    may simply never match on the live DOM, which makes the check a no-op
    that always reports "not streaming"). To avoid depending on any single
    selector staying correct, this instead requires the turn's rendered
    text to be unchanged across a couple of consecutive polls -- i.e. the
    content has actually stopped growing -- before trusting the copy
    button as a real completion signal.
    """
    elapsed = 0
    poll_interval = Config.POLL_INTERVAL_MS / 1000
    heartbeat = 10
    next_heartbeat = heartbeat
    first_snapshot_logged = False

    required_stable_polls = 2
    stable_polls = 0
    last_text: str | None = None

    while elapsed * 1000 < timeout_ms:
        try:
            snapshot = await _latest_assistant_turn_snapshot(page)
        except TargetClosedError:
            log.error("Page/browser closed while waiting for response")
            return None
        except Exception as e:
            log.warning(f"Snapshot failed ({type(e).__name__}): {e}")
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            continue

        signature = snapshot.get("signature")
        is_new_turn = previous_turn_signature is None or (
            isinstance(signature, str) and signature != previous_turn_signature
        )

        # Log the first snapshot for diagnostics
        if not first_snapshot_logged and elapsed >= 2:
            first_snapshot_logged = True
            turn_text = (snapshot.get("text") or "")[:200]
            all_turns = await _dump_all_turns(page)
            log.debug(
                f"First snapshot at {int(elapsed)}s | "
                f"prev_sig={previous_turn_signature} cur_sig={signature} "
                f"is_new={is_new_turn} copy={snapshot.get('hasCopyButton')} "
                f"text[:{len(turn_text)}]={turn_text!r}"
            )
            log.debug(f"All turns ({len(all_turns)}): {all_turns}")

        if is_new_turn:
            text = snapshot.get("text") if isinstance(snapshot.get("text"), str) else ""

            if text and text == last_text:
                stable_polls += 1
            else:
                stable_polls = 0
                last_text = text

            if snapshot.get("hasCopyButton") and text:
                if stable_polls >= required_stable_polls:
                    current_count = await _count_copy_buttons(page)
                    log.debug(
                        f"Copy button detected and text stable on latest turn {signature} "
                        f"(copy-buttons: {current_count})"
                    )
                    return "copy"
                else:
                    log.debug(
                        f"Copy button present on latest turn {signature} but text still "
                        f"changing ({stable_polls}/{required_stable_polls} stable polls) "
                        "-- still streaming, continuing to wait"
                    )

        # Use snapshot data directly instead of a separate evaluate call
        if is_new_turn and snapshot.get("hasImage"):
            await asyncio.sleep(0.5)
            log.info(f"Generated image detected on latest turn {signature}")
            return "image"

        if elapsed >= next_heartbeat:
            next_heartbeat = elapsed + heartbeat
            # Diagnostic: log what we see on the latest assistant turn
            turn_text = (snapshot.get("text") or "")[:200]
            log.debug(
                f"Still waiting for copy/image... ({int(elapsed)}s) | "
                f"sig={signature} is_new={is_new_turn} "
                f"copy={snapshot.get('hasCopyButton')} "
                f"text[:{len(turn_text)}]={turn_text!r}"
            )

            # Dump all turn info for debugging
            all_turns = await _dump_all_turns(page)
            log.debug(f"All turns: {all_turns}")

            await idle_mouse_movement(page)

            # Check for page-level errors every heartbeat to fail fast
            page_error = await _check_page_error(page)
            if page_error:
                log.error(f"Page error detected while waiting: {page_error}")
                return None

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    # Diagnostic: save screenshot on timeout
    try:
        await page.screenshot(path="logs/detector_timeout.png")
        log.info("Saved timeout screenshot to logs/detector_timeout.png")
    except Exception as e:
        log.debug(f"Could not save timeout screenshot: {e}")

    log.warning(f"Neither copy button nor image found after {int(elapsed)}s")
    return None


async def _wait_via_stop_button(page: Page, timeout_ms: int) -> bool:
    """Wait for stop button appear -> disappear cycle."""
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


async def _wait_via_text_stability(
    page: Page,
    timeout_ms: int,
    previous_turn_signature: str | None = None,
) -> bool:
    """
    Last resort: poll latest assistant-turn text and wait until stable.

    If previous_turn_signature is provided, ignores stabilization on that old turn.
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
                if is_incomplete_response_text(text) and not bool(
                    snapshot.get("hasCopyButton")
                ):
                    log.debug(
                        "Stable text looks like transient thinking status; continuing wait"
                    )
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

    Never intentionally copies from previous_turn_signature when provided.
    """
    log.debug("Attempting extraction via latest-turn copy button...")

    try:
        await page.context.grant_permissions(["clipboard-read", "clipboard-write"])

        if previous_turn_signature:
            await _wait_for_new_turn_signature(
                page, previous_turn_signature, timeout_ms=8000
            )

        pre_clipboard = await page.evaluate(
            "navigator.clipboard.readText().catch(() => '')"
        )
        await page.evaluate("navigator.clipboard.writeText('').catch(() => {})")

        click_result = await page.evaluate(
            """
            (previousSignature) => {
                const turns = Array.from(document.querySelectorAll('section[data-testid^="conversation-turn-"]'));

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
                        return { clicked: false, reason: 'stale-turn', signature };
                    }

                    const btn =
                        turn.querySelector('button[aria-label="Copy response"]') ||
                        turn.querySelector('button[data-testid="copy-turn-action-button"]') ||
                        turn.querySelector('button[aria-label="Copy message"]');
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
            await asyncio.sleep(0.3)
            content = await page.evaluate(
                "navigator.clipboard.readText().catch(() => '')"
            )
            if (
                content
                and content.strip()
                and content.strip() != str(pre_clipboard).strip()
            ):
                log.info(
                    "Extracted via copy button (latest-turn): "
                    f"{len(content)} chars, turn={click_result.get('signature')}"
                )
                return content.strip()
            log.debug("Clipboard unchanged/empty after latest-turn copy click")
        else:
            reason = (
                click_result.get("reason")
                if isinstance(click_result, dict)
                else "unknown"
            )
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
            const turns = Array.from(document.querySelectorAll('section[data-testid^="conversation-turn-"]'));

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

                return (turn.innerText || '').trim();
            }

            return '';
        }
        """,
        previous_turn_signature,
    )

    if text and str(text).strip():
        cleaned = normalize_assistant_text(str(text))
        if is_incomplete_response_text(cleaned):
            log.debug(
                "Latest-turn DOM text looks incomplete/transient; waiting for a fuller reply"
            )
            return ""
        log.debug(f"Extracted via DOM (latest-turn): {len(cleaned)} chars")
        return cleaned

    log.error("Could not extract any latest assistant response")
    return ""


# Keep old name as alias for backward compat
extract_last_response = extract_last_response_via_copy
