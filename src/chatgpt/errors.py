"""Typed errors raised while submitting prompts through the ChatGPT UI."""

from __future__ import annotations


class PromptTooLongError(RuntimeError):
    """The ChatGPT composer rejected a prompt because it is too large."""

    status_code = 413


class PromptAttachmentFallbackError(RuntimeError):
    """The long-prompt attachment fallback could not be completed."""

    status_code = 502
