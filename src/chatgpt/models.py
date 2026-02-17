"""
Data models for ChatGPT interactions.
"""

from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field


class ImageInfo(BaseModel):
    """Metadata for a generated image."""
    url: str = Field(description="Original image URL from ChatGPT/DALL-E")
    alt: str = Field(default="", description="Alt text / image description")
    local_path: str = Field(default="", description="Local file path after download")
    prompt_title: str = Field(default="", description="Image generation title shown by ChatGPT")


class Message(BaseModel):
    """A single message in a conversation."""
    role: str = Field(description="'user' or 'assistant'")
    content: str = Field(description="Message text content")
    timestamp: datetime = Field(default_factory=datetime.now)
    images: list[ImageInfo] = Field(default_factory=list, description="Images in this message")


class ChatResponse(BaseModel):
    """Response from a chat interaction."""
    message: str = Field(description="Assistant's response text")
    thread_id: str = Field(default="", description="Conversation thread ID from URL")
    response_time_ms: int = Field(default=0, description="Time taken for response in ms")
    images: list[ImageInfo] = Field(default_factory=list, description="Generated images")
    has_images: bool = Field(default=False, description="Whether the response contains images")


class Thread(BaseModel):
    """A conversation thread."""
    id: str = Field(description="Thread ID (from URL /c/{id})")
    title: str = Field(default="", description="Thread title from sidebar")
    url: str = Field(default="", description="Full URL")
    messages: list[Message] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
