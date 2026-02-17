"""
Centralized DOM selectors for ChatGPT.

All selectors live here so when ChatGPT updates their UI, we only
change this one file. Each entry is a list of fallback selectors —
try them in order until one matches.
"""

from __future__ import annotations


class Selectors:
    """CSS / Playwright selectors for chatgpt.com UI elements."""

    # ── Chat input ──────────────────────────────────────────────
    CHAT_INPUT = [
        "#prompt-textarea",
        "div[contenteditable='true'][id='prompt-textarea']",
        "div[contenteditable='true']",
    ]

    # ── Send button ─────────────────────────────────────────────
    SEND_BUTTON = [
        "button[data-testid='send-button']",
        "button[aria-label='Send prompt']",
        "#prompt-textarea ~ button",
    ]

    # ── Assistant response messages ─────────────────────────────
    ASSISTANT_MESSAGE = [
        "div[data-message-author-role='assistant']",
        "[data-message-author-role='assistant']",
        "div.agent-turn",
    ]

    # ── Streaming / stop button (visible while generating) ─────
    STOP_BUTTON = [
        "button[aria-label='Stop generating']",
        "button[data-testid='stop-button']",
        "button.stop-button",
    ]

    # ── New chat ────────────────────────────────────────────────
    NEW_CHAT_BUTTON = [
        "a[data-testid='create-new-chat-button']",
        "a[href='/']",
        "nav a[href='/']",
    ]

    # ── Sidebar conversation links ──────────────────────────────
    SIDEBAR_THREAD_LINKS = [
        "nav a[href^='/c/']",
        "a[href^='/c/']",
    ]

    # ── Login page detection (if any of these appear, user is logged out) ──
    LOGIN_INDICATORS = [
        "button[data-testid='login-button']",
        "button:has-text('Log in')",
        "[data-testid='login-button']",
    ]

    # ── Markdown content inside assistant message ───────────────
    ASSISTANT_MARKDOWN = [
        "div[data-message-author-role='assistant'] .markdown",
        "div[data-message-author-role='assistant'] .prose",
        "div.agent-turn .markdown",
    ]

    # ── Regenerate / continue buttons (appear after response completes) ──
    POST_RESPONSE_BUTTONS = [
        "button:has-text('Regenerate')",
        "button:has-text('Continue generating')",
    ]

    # ── Copy button (appears on each completed assistant message) ──────
    # This is the most reliable completion signal — it only appears
    # after the full response has been generated.
    COPY_BUTTON = [
        "button[data-testid='copy-turn-action-button']",
        "button[aria-label='Copy']",
        "button:has(svg path[d*='M7'])[class*='rounded']",  # copy icon SVG
    ]

    # ── Generated images inside assistant responses ───────────────────
    # ChatGPT DALL-E image responses do NOT have data-message-author-role.
    # Instead, the image lives inside an article turn with class "agent-turn".
    # Images have alt="Generated image" and src from chatgpt.com/backend-api.
    # Image wrapper DIVs have id="image-{uuid}" and class group/imagegen-image.
    ASSISTANT_IMAGE = [
        "img[alt='Generated image']",
        "div[id^='image-'] img",
        "article img[alt='Generated image']",
        ".agent-turn img",
    ]

    # Image container identifiers (used for detection, not clicking)
    IMAGE_CONTAINER = [
        "div[id^='image-']",
        "div[class*='imagegen-image']",
    ]

    # Download button for generated images
    IMAGE_DOWNLOAD_BUTTON = [
        "a[aria-label='Download']",
        "a[download]",
    ]

    # ── File / attachment upload input ────────────────────────────
    FILE_UPLOAD_INPUT = [
        "input[type='file']",
        "input[data-testid='file-upload']",
        "input[accept*='image']",
        "input[accept*='application']",
    ]

    # Attach / upload button (opens file picker)
    ATTACH_BUTTON = [
        "button[aria-label='Attach files']",
        "button[data-testid='upload-button']",
        "button[aria-label='Upload file']",
    ]
