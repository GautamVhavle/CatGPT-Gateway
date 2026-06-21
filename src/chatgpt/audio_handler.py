"""
Read-aloud audio handler for ChatGPT responses.

The ChatGPT UI can synthesize speech for an assistant turn via its
"Read aloud" action. This module clicks that action on the latest assistant
turn, captures the audio response from the browser, and saves it locally.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time

from patchright.async_api import Page

from src.chatgpt.models import AudioInfo
from src.config import Config
from src.log import setup_logging

log = setup_logging("audio_handler")


def _audio_extension(mime_type: str) -> str:
    """Return a filename extension for a browser audio content type."""
    mime = (mime_type or "").split(";")[0].strip().lower()
    return {
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/wav": "wav",
        "audio/wave": "wav",
        "audio/x-wav": "wav",
        "audio/ogg": "ogg",
        "audio/webm": "webm",
        "audio/aac": "aac",
        "audio/mp4": "m4a",
    }.get(mime, "mp3")


def _save_audio_bytes(audio_bytes: bytes, mime_type: str, url: str = "") -> AudioInfo:
    """Persist audio bytes and return response metadata."""
    Config.ensure_dirs()

    digest_source = url.encode() if url else audio_bytes[:256]
    digest = hashlib.md5(digest_source).hexdigest()[:12]
    filename = f"read_aloud_{int(time.time())}_{digest}.{_audio_extension(mime_type)}"
    local_path = Config.AUDIO_DIR / filename
    local_path.write_bytes(audio_bytes)

    log.info(f"Audio saved: {local_path} ({len(audio_bytes) / 1024:.1f} KB)")
    return AudioInfo(
        url=url,
        local_path=str(local_path),
        mime_type=mime_type,
        size_bytes=len(audio_bytes),
    )


def _looks_like_audio_response(response) -> bool:
    """Return True when a network response appears to contain synthesized audio."""
    headers = response.headers or {}
    content_type = headers.get("content-type", "").lower()
    url = response.url.lower()

    if content_type.startswith("audio/"):
        return True
    if content_type.startswith("text/") or content_type.startswith("application/json"):
        return False

    audio_url_markers = (
        "audio",
        "speech",
        "synthesize",
        "synthesis",
        "voice",
        "tts",
        "read-aloud",
        "read_aloud",
    )
    binary_content_types = (
        "application/octet-stream",
        "binary/octet-stream",
    )
    return (
        any(marker in url for marker in audio_url_markers)
        and (
            content_type.startswith(binary_content_types)
            or response.request.resource_type in {"media", "fetch"}
        )
    )


async def _click_read_aloud(page: Page, previous_turn_signature: str | None = None) -> bool:
    """
    Open the latest assistant turn's More actions menu and click Read aloud.

    ChatGPT currently hides Read aloud behind a More actions button on many
    layouts. The fallback path still supports layouts where Read aloud is
    visible directly on the turn toolbar.
    """
    clicked = await page.evaluate(
        """
        async ({previousSignature}) => {
            const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

            const turns = Array.from(document.querySelectorAll('section[data-testid^="conversation-turn-"]'));
            let latest = null;

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
                if (previousSignature && signature === previousSignature) continue;

                latest = turn;
                break;
            }

            if (!latest) return false;

            latest.scrollIntoView({block: 'center', inline: 'nearest'});
            latest.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
            await sleep(350);

            const normalizeLabel = (el) => [
                el.getAttribute('aria-label') || '',
                el.getAttribute('title') || '',
                el.getAttribute('data-testid') || '',
                el.innerText || '',
                el.textContent || '',
            ].join(' ').toLowerCase();

            const matchesReadAloud = (el) => {
                const label = normalizeLabel(el);
                return (
                    label.includes('read aloud') ||
                    label.includes('read-aloud') ||
                    label.includes('read_aloud') ||
                    label.includes('read out loud') ||
                    label.includes('speak') ||
                    label.includes('listen')
                );
            };

            const clickReadAloud = (root) => {
                const candidates = Array.from(root.querySelectorAll(
                    'button, [role="button"], [role="menuitem"], [cmdk-item]'
                ));
                const target = candidates.find(matchesReadAloud);
                if (!target) return false;
                target.click();
                return true;
            };

            const matchesMoreActions = (el) => {
                const label = normalizeLabel(el);
                return (
                    label.includes('more actions') ||
                    label.includes('more') ||
                    label.includes('options') ||
                    label.includes('actions') ||
                    label.includes('overflow')
                );
            };

            const clickMoreActions = async () => {
                const exact = latest.querySelector('button[aria-label="More actions"]');
                if (exact) {
                    exact.click();
                    await sleep(800);
                    return true;
                }

                const buttons = Array.from(latest.querySelectorAll('button, [role="button"]')).reverse();
                const menuButton = buttons.find(matchesMoreActions);
                if (!menuButton) return false;

                menuButton.click();
                await sleep(800);
                return true;
            };

            if (await clickMoreActions()) {
                if (clickReadAloud(document)) return true;
            }

            latest.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
            await sleep(350);
            if (clickReadAloud(latest)) return true;

            if (await clickMoreActions()) {
                await sleep(500);
                if (clickReadAloud(document)) return true;
            }

            return false;
        }
        """,
        {"previousSignature": previous_turn_signature},
    )
    return bool(clicked)


async def _audio_from_media_elements(page: Page) -> AudioInfo | None:
    """Fallback: fetch any audio/media element source that appeared on the page."""
    media_data = await page.evaluate(
        """
        async () => {
            const media = Array.from(document.querySelectorAll('audio, video')).reverse();
            for (const el of media) {
                const src = el.currentSrc || el.src || '';
                if (!src) continue;
                try {
                    const response = await fetch(src);
                    if (!response.ok) continue;
                    const blob = await response.blob();
                    const reader = new FileReader();
                    const dataUrl = await new Promise((resolve) => {
                        reader.onloadend = () => resolve(reader.result);
                        reader.readAsDataURL(blob);
                    });
                    return {
                        url: src,
                        mimeType: blob.type || response.headers.get('content-type') || 'audio/mpeg',
                        dataUrl,
                    };
                } catch (e) {
                    continue;
                }
            }
            return null;
        }
        """
    )

    if not media_data:
        return None

    data_url = media_data.get("dataUrl", "")
    if not isinstance(data_url, str) or "," not in data_url:
        return None

    header, b64data = data_url.split(",", 1)
    mime_type = media_data.get("mimeType") or "audio/mpeg"
    if header.startswith("data:") and ";" in header:
        mime_type = header.split(":", 1)[1].split(";", 1)[0] or mime_type

    return _save_audio_bytes(
        base64.b64decode(b64data),
        mime_type=mime_type,
        url=media_data.get("url", ""),
    )


async def generate_read_aloud_audio(
    page: Page,
    previous_turn_signature: str | None = None,
    timeout_ms: int = 45000,
) -> AudioInfo | None:
    """
    Trigger ChatGPT's read-aloud action and download the generated audio.

    Returns None when the UI does not expose the action or no audio response is
    observed before the timeout.
    """
    log.info("Requesting read-aloud audio from ChatGPT UI...")

    loop = asyncio.get_running_loop()
    response_future = loop.create_future()

    def on_response(response) -> None:
        if response_future.done():
            return
        if _looks_like_audio_response(response):
            response_future.set_result(response)

    page.on("response", on_response)
    try:
        clicked = await _click_read_aloud(page, previous_turn_signature)
        if not clicked:
            log.warning("Read-aloud button was not found on the latest response")
            return None

        response = await asyncio.wait_for(response_future, timeout=timeout_ms / 1000)
        body = await response.body()
        if not body:
            log.warning(f"Read-aloud response was empty: {response.url}")
            return None

        mime_type = response.headers.get("content-type", "audio/mpeg")
        if not mime_type.lower().startswith("audio/"):
            mime_type = "audio/mpeg"
        return _save_audio_bytes(body, mime_type=mime_type, url=response.url)

    except Exception as e:
        log.warning(f"Could not capture read-aloud network audio: {e}")
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    try:
        return await _audio_from_media_elements(page)
    except Exception as e:
        log.warning(f"Could not recover read-aloud audio from media elements: {e}")
        return None
