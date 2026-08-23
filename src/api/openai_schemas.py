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


# ── Responses API (/v1/responses) ───────────────────────────────


class ResponsesToolDefinition(BaseModel):
    """Flat tool definition used by the Responses API.

    Unlike the Chat Completions API which nests under `function:`,
    the Responses API uses a flat format:
      {"type": "function", "name": "...", "parameters": {...}, "description": "..."}
    """
    type: str = "function"
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    strict: Optional[bool] = None


class ResponsesInputMessage(BaseModel):
    """A message in the Responses API input array."""
    role: str  # "user" | "assistant" | "system" | "developer"
    content: Union[str, List[Any]]


class ResponsesFunctionCallInput(BaseModel):
    """A function_call item in the Responses API input (assistant called a tool)."""
    type: str = "function_call"
    id: Optional[str] = None
    call_id: str
    name: str
    arguments: str


class ResponsesFunctionCallOutputInput(BaseModel):
    """A function_call_output item in the Responses API input (tool result)."""
    type: str = "function_call_output"
    call_id: str
    output: str


class ResponsesRequest(BaseModel):
    """Request body for POST /v1/responses."""
    model: str = "catgpt-browser"
    input: Union[str, List[Any]]  # string or array of messages/items
    instructions: Optional[str] = None  # system prompt
    tools: Optional[List[ResponsesToolDefinition]] = None
    tool_choice: Optional[Union[str, dict]] = None
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    top_p: Optional[float] = None
    previous_response_id: Optional[str] = None
    truncation: Optional[str] = None
    user: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    store: Optional[bool] = None


# ── Responses API output models ─────────────────────────────────


class ResponseOutputText(BaseModel):
    """Text content in a Responses API output message."""
    type: str = "output_text"
    text: str = ""
    annotations: List[Any] = Field(default_factory=list)


class ResponseOutputMessage(BaseModel):
    """A message output item in the Responses API."""
    id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:24]}")
    type: str = "message"
    role: str = "assistant"
    status: str = "completed"
    content: List[ResponseOutputText] = Field(default_factory=list)


class ResponseFunctionCall(BaseModel):
    """A function_call output item in the Responses API."""
    id: str = Field(default_factory=lambda: f"fc_{uuid.uuid4().hex[:24]}")
    type: str = "function_call"
    call_id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:24]}")
    name: str
    arguments: str
    status: str = "completed"


class ResponseUsage(BaseModel):
    """Usage info for the Responses API."""
    input_tokens: int = 0
    output_tokens: int = 0
    output_tokens_details: dict[str, int] = Field(
        default_factory=lambda: {"reasoning_tokens": 0}
    )
    total_tokens: int = 0


class ResponseObject(BaseModel):
    """The full response object returned by POST /v1/responses."""
    id: str = Field(default_factory=lambda: f"resp_{uuid.uuid4().hex[:24]}")
    object: str = "response"
    created_at: int = Field(default_factory=lambda: int(time.time()))
    status: str = "completed"
    completed_at: Optional[int] = None
    error: Optional[dict[str, Any]] = None
    incomplete_details: Optional[dict[str, Any]] = None
    instructions: Optional[str] = None
    max_output_tokens: Optional[int] = None
    model: str = "catgpt-browser"
    output: List[Any] = Field(default_factory=list)
    output_text: Optional[str] = None
    parallel_tool_calls: bool = True
    previous_response_id: Optional[str] = None
    temperature: Optional[float] = 1.0
    text: dict[str, Any] = Field(default_factory=lambda: {"format": {"type": "text"}})
    tool_choice: Optional[Union[str, dict]] = "auto"
    tools: List[Any] = Field(default_factory=list)
    top_p: Optional[float] = 1.0
    truncation: Optional[str] = "disabled"
    usage: Optional[ResponseUsage] = None
    user: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
