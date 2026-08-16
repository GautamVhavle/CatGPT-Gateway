"""
Centralized DOM selectors for Claude.ai.

All selectors live here so when Claude updates their UI, we only
change this one file. Each entry is a list of fallback selectors —
try them in order until one matches.
"""

from __future__ import annotations


class ClaudeSelectors:
    """CSS / Playwright selectors for claude.ai UI elements."""

    # ── Chat input ──────────────────────────────────────────────
    # Claude uses a tiptap/ProseMirror contenteditable div
    CHAT_INPUT = [
        "div[data-testid='chat-input']",
        "div[contenteditable='true'][role='textbox']",
        "div.ProseMirror[contenteditable='true']",
        "div[contenteditable='true']",
    ]

    # ── Send button ─────────────────────────────────────────────
    # When text is entered, Claude's voice-mode button morphs into send.
    SEND_BUTTON = [
        "button[data-testid='send-message']",
        "button[aria-label='Send message']",
        "button[aria-label='Send Message']",
        "fieldset button[type='submit']",
    ]

    # ── Assistant response messages ─────────────────────────────
    # Claude wraps each assistant turn in a div with data-is-streaming.
    # The inner text content has class font-claude-response.
    ASSISTANT_MESSAGE = [
        "div[data-is-streaming]",
        "div.font-claude-response",
    ]

    # ── Streaming / stop button (visible while generating) ─────
    STOP_BUTTON = [
        "button[data-testid='stop-button']",
        "button[aria-label='Stop response']",
        "button[aria-label='Stop']",
    ]

    # ── New chat ────────────────────────────────────────────────
    NEW_CHAT_BUTTON = [
        "a[href='/new']",
    ]

    # ── Sidebar conversation links ──────────────────────────────
    # Claude uses /chat/{uuid} URL pattern
    SIDEBAR_THREAD_LINKS = [
        "a[href^='/chat/']",
    ]

    # ── Login page detection ────────────────────────────────────
    # If user-menu-button exists, user is logged in.
    # If login elements appear, user is logged out.
    LOGIN_INDICATORS = [
        "button:has-text('Log in')",
        "button:has-text('Sign in')",
        "a:has-text('Log in')",
        "a:has-text('Sign in')",
    ]

    # ── Logged-in indicator (user menu) ─────────────────────────
    LOGGED_IN_INDICATORS = [
        "button[data-testid='user-menu-button']",
    ]

    # ── Markdown content inside assistant message ───────────────
    ASSISTANT_MARKDOWN = [
        "div.font-claude-response",
        "div.font-claude-response .standard-markdown",
        "p.font-claude-response-body",
    ]

    # ── User message ────────────────────────────────────────────
    USER_MESSAGE = [
        "div[data-testid='user-message']",
    ]

    # ── Copy button (appears on each completed assistant message) ──
    COPY_BUTTON = [
        "button[data-testid='action-bar-copy']",
        "button[aria-label='Copy']",
    ]

    # ── Retry / regenerate button ───────────────────────────────
    POST_RESPONSE_BUTTONS = [
        "button[data-testid='action-bar-retry']",
        "button[aria-label='Retry']",
    ]

    # ── File / attachment upload input ────────────────────────────
    FILE_UPLOAD_INPUT = [
        "input#chat-input-file-upload-onpage",
        "input[data-testid='file-upload']",
        "input[type='file']",
    ]

    ATTACHMENT_BADGE = [
        "[data-testid*='attachment']",
        "[class*='attachment']",
        "button[aria-label*='Remove']",
        "[data-testid*='file']",
    ]

    # Attach / upload button (opens file picker)
    ATTACH_BUTTON = [
        "button[aria-label='Add files, connectors, and more']",
        "button[aria-label='Attach files']",
    ]

    # ── Model selector ──────────────────────────────────────────
    MODEL_SELECTOR = [
        "button[data-testid='model-selector-dropdown']",
    ]

    # ── Generated images (Claude Artifacts, etc.) ─────────────
    ASSISTANT_IMAGE: list[str] = []
    IMAGE_CONTAINER: list[str] = []
    IMAGE_DOWNLOAD_BUTTON: list[str] = []
