"""
OpenAI-compatible Pydantic schemas for /v1/chat/completions and /v1/models.

Mirrors the OpenAI Chat Completions API specification so that any OpenAI SDK
or LangChain client can talk to our browser-backed ChatGPT endpoint.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, List, Optional, Union

from pydantic import BaseModel, Field


# ── Tool / Function definitions ─────────────────────────────────


class FunctionDefinition(BaseModel):
    """Schema for a function the model may call."""
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolDefinition(BaseModel):
    """A tool the model may use (only 'function' type supported)."""
    type: str = "function"
    function: FunctionDefinition


class FunctionCallInfo(BaseModel):
    """Info about a specific function call made by the model."""
    name: str
    arguments: str  # JSON string


class ToolCall(BaseModel):
    """A tool call returned by the model."""
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:24]}")
    type: str = "function"
    function: FunctionCallInfo


# ── Messages ────────────────────────────────────────────────────


class ChatMessage(BaseModel):
    """A single message in the conversation.
    
    Content can be:
    - A simple string
    - A list of content parts (OpenAI vision format + file attachments):
      [
        {"type": "text", "text": "..."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
        {"type": "file", "file": {"filename": "doc.pdf", "data": "<base64>", "mime_type": "application/pdf"}}
      ]
    """
    role: str  # system | user | assistant | tool
    content: Optional[Union[str, List[Any]]] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None


# ── Request ─────────────────────────────────────────────────────


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request body."""
    model: str = "catgpt-browser"
    messages: list[ChatMessage]
    tools: Optional[list[ToolDefinition]] = None
    tool_choice: Optional[Union[str, dict]] = None  # "auto" | "none" | {"type":"function","function":{"name":"..."}}
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    stop: Optional[Union[str, List[str]]] = None
    stream: Optional[bool] = False
    n: Optional[int] = 1
    user: Optional[str] = None


# ── Response ────────────────────────────────────────────────────


class UsageInfo(BaseModel):
    """Token usage (estimated — we don't have real token counts)."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChoiceMessage(BaseModel):
    """The assistant's message in a choice."""
    role: str = "assistant"
    content: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None


class Choice(BaseModel):
    """A single completion choice."""
    index: int = 0
    message: ChoiceMessage
    finish_reason: str = "stop"  # "stop" | "tool_calls"


class ChatCompletionResponse(BaseModel):
    """OpenAI-compatible chat completion response."""
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:24]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "catgpt-browser"
    choices: list[Choice]
    usage: UsageInfo = Field(default_factory=UsageInfo)


# ── Models endpoint ─────────────────────────────────────────────


class ModelObject(BaseModel):
    """A model object for /v1/models."""
    id: str
    object: str = "model"
    created: int = 1700000000
    owned_by: str = "catgpt"


class ModelListResponse(BaseModel):
    """Response for GET /v1/models."""
    object: str = "list"
    data: list[ModelObject]


# ── Image Generation ────────────────────────────────────────────


class ImageGenerationRequest(BaseModel):
    """OpenAI-compatible image generation request (POST /v1/images/generations)."""
    prompt: str
    model: Optional[str] = "dall-e-3"
    n: Optional[int] = Field(default=1, ge=1, le=4)
    size: Optional[str] = "1024x1024"
    quality: Optional[str] = "standard"
    style: Optional[str] = "vivid"
    response_format: Optional[str] = "b64_json"  # "url" or "b64_json"
    user: Optional[str] = None


class ImageData(BaseModel):
    """A single generated image in the response."""
    url: Optional[str] = None
    b64_json: Optional[str] = None
    revised_prompt: Optional[str] = None


class ImagesResponse(BaseModel):
    """OpenAI-compatible image generation response."""
    created: int = Field(default_factory=lambda: int(time.time()))
    data: List[ImageData]
