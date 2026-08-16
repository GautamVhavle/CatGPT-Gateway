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
        "#composer-submit-button",
        "button[aria-label='Send prompt']",
        "button[aria-label*='Send' i]",
        "#prompt-textarea ~ button",
    ]

    # Model picker trigger in ChatGPT's composer/header.
    MODEL_PICKER_BUTTON = [
        "button[data-testid='model-switcher-dropdown-button']",
        "button[aria-haspopup='menu']:has-text('Instant')",
        "button[aria-haspopup='menu']:has-text('Thinking')",
        "button[aria-haspopup='menu']:has-text('Medium')",
        "button[aria-haspopup='menu']:has-text('High')",
        "button[aria-haspopup='menu']:has-text('Extra High')",
        "button[aria-haspopup='menu']:has-text('Pro')",
        "button[aria-haspopup='menu']:has-text('Auto')",
        "button[aria-haspopup='menu']:has-text('5.')",
        "button[aria-haspopup='menu']:has-text('GPT')",
    ]

    # ── Assistant response messages ─────────────────────────────
    ASSISTANT_MESSAGE = [
        "div[data-message-author-role='assistant']",
        "[data-message-author-role='assistant']",
        "[data-testid^='conversation-turn-'] [data-message-author-role='assistant']",
        "[data-testid*='conversation-turn' i]",
        ".agent-turn",
        "section[data-turn='assistant']",
        "section[data-testid^='conversation-turn-']",
    ]

    # ── Streaming / stop button (visible while generating) ─────
    STOP_BUTTON = [
        "button[data-testid='stop-button']",
        "button[aria-label='Stop answering']",
        "button[aria-label='Stop generating']",
        "button[aria-label*='Stop' i]",
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
        "section[data-turn='assistant'] .markdown",
        "section[data-turn='assistant'] .prose",
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
        "button[data-testid*='copy-turn' i]",
        "button[aria-label='Copy message']",
        "button[aria-label='Copy response']",
    ]

    # ── Generated images inside assistant responses ───────────────────
    # ChatGPT DALL-E image responses do NOT have data-message-author-role.
    # Instead, the image lives inside an article turn with class "agent-turn".
    # Images have alt="Generated image" and src from chatgpt.com/backend-api.
    # Image wrapper DIVs have id="image-{uuid}" and class group/imagegen-image.
    ASSISTANT_IMAGE = [
        "img[alt='Generated image']",
        "img[alt*='generated' i]",
        "div[id^='image-'] img",
        "div[class*='imagegen-image'] img",
        "section[data-turn='assistant'] img[alt='Generated image']",
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
        "input[type='file']:not([accept*='image'])",
        "input[type='file']",
        "input[data-testid='file-upload']",
        "input#upload-photos",
        "input[accept*='image']",
    ]

    ATTACHMENT_BADGE = [
        "[data-testid*='attachment']",
        "[class*='attachment']",
        "button[aria-label*='Remove']",
        "[data-testid*='file']",
    ]

    # Attach / upload button (opens file picker)
    ATTACH_BUTTON = [
        "button[data-testid='composer-plus-btn']",
        "button[aria-label='Add files and more']",
        "button[aria-label='Attach files']",
    ]

    # ── Thread deletion (sidebar context menu → Delete → confirm) ──
    # Sidebar conversation item — bring up its context/overflow menu.
    SIDEBAR_THREAD_ITEM = [
        "nav a[href^='/c/']",
        "a[href^='/c/']",
    ]
    # Three-dot / overflow menu button on a sidebar conversation row.
    SIDEBAR_THREAD_MENU_BUTTON = [
        "button[data-testid='thread-menu-button']",
        "button[aria-label='More options']",
        "button[aria-label*='menu' i]",
        "button[aria-label*='actions' i]",
        "button[aria-haspopup='menu']",
    ]
    # "Delete" choice in the conversation context menu.
    THREAD_DELETE_OPTION = [
        "button:has-text('Delete')",
        "[role='menuitem']:has-text('Delete')",
        "div[role='menu'] button:has-text('Delete')",
        "div button:has-text('Delete')",
    ]
    # Confirm-delete action in the modal dialog.
    THREAD_CONFIRM_DELETE_BUTTON = [
        "button[data-testid='confirm-delete-button']",
        "button[data-testid='delete-confirm-button']",
        "button:has-text('Delete conversation')",
        "button:has-text('Delete chat')",
        "button:has-text('Delete thread')",
        "button[aria-label='Delete conversation']",
        "div[role='alertdialog'] button.bg-red-700",
        "div[role='alertdialog'] button:has-text('Delete')",
        "div[role='dialog'] button:has-text('Delete')",
    ]
