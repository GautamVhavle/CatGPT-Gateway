"""
Pydantic request/response schemas for the API.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Request body for sending a message."""
    message: str = Field(..., min_length=1, description="The message to send to ChatGPT")


class ImageInfoResponse(BaseModel):
    """Image metadata in API response."""
    url: str = Field("", description="Original image URL from ChatGPT/DALL-E")
    alt: str = Field("", description="Alt text / image description")
    local_path: str = Field("", description="Local file path after download")
    prompt_title: str = Field("", description="Image generation title shown by ChatGPT")


class ChatResponse(BaseModel):
    """Response body with ChatGPT's reply."""
    message: str = Field(..., description="ChatGPT's response text (markdown)")
    thread_id: str = Field("", description="Conversation thread ID")
    response_time_ms: int = Field(0, description="Time to generate the response in ms")
    images: list[ImageInfoResponse] = Field(default_factory=list, description="Generated images")
    has_images: bool = Field(False, description="Whether the response contains images")


class ThreadInfo(BaseModel):
    """Thread metadata."""
    id: str
    title: str
    url: str


class ThreadListResponse(BaseModel):
    """List of recent threads."""
    threads: list[ThreadInfo]


class StatusResponse(BaseModel):
    """Health check / status."""
    status: str = "ok"
    logged_in: bool = False
    current_thread: str = ""
