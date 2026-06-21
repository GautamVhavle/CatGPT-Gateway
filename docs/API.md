# API Reference

CatGPT Gateway exposes an OpenAI-compatible API. Any client that works with the OpenAI API works here.

---

## Table of Contents

- [Base URL](#base-url)
- [Authentication](#authentication)
- [OpenAI-Compatible Endpoints](#openai-compatible-endpoints)
  - [Chat Completions](#chat-completions)
  - [Tool / Function Calling](#tool--function-calling)
  - [Image Input (Vision)](#image-input-vision)
  - [File Attachments](#file-attachments)
  - [Image Generation (ChatGPT only)](#image-generation-chatgpt-only)
  - [List Models](#list-models)
- [Custom REST API](#custom-rest-api)
- [TUI Terminal Client](#tui-terminal-client)
- [Provider Differences](#provider-differences)

---

## Base URL

```
http://localhost:8000/v1
```

## Authentication

Include the Bearer token (default `dummy123`) in every request:

```bash
Authorization: Bearer dummy123
```

With the OpenAI SDK:

```python
client = OpenAI(base_url="http://localhost:8000/v1", api_key="dummy123")
```

Open paths (no auth needed): `/docs`, `/redoc`, `/openapi.json`, `/healthz`

---

## OpenAI-Compatible Endpoints

### Chat Completions

**`POST /v1/chat/completions`**

Standard OpenAI chat completion request.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="dummy123")

response = client.chat.completions.create(
    model="claude-browser",  # or "catgpt-browser"
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is quantum computing?"}
    ]
)
print(response.choices[0].message.content)
```

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy123" \
  -d '{
    "model": "claude-browser",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

**Request body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `model` | string | yes | `claude-browser` or `catgpt-browser` |
| `messages` | array | yes | Array of message objects |
| `tools` | array | no | Tool/function definitions |
| `tool_choice` | string/object | no | `auto`, `none`, `required`, or specific function |
| `temperature` | float | no | Ignored (browser controls this) |
| `max_tokens` | int | no | Ignored |
| `stream` | bool | no | Must be `false` (streaming not supported) |
| `read_aloud` | bool | no | ChatGPT only. Opens `More actions` -> `Read aloud`, downloads the browser-generated audio, and returns it at `choices[0].message.audio`. |

**Response:**

```json
{
  "id": "chatcmpl-abc123...",
  "object": "chat.completion",
  "created": 1716025800,
  "model": "claude-browser",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Quantum computing uses quantum bits...",
        "tool_calls": null,
        "audio": null
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 25,
    "completion_tokens": 150,
    "total_tokens": 175
  }
}
```

---

### Read-Aloud Audio

For ChatGPT, add the custom `read_aloud: true` flag to generate speech from the browser UI after the assistant response is complete. The gateway clicks the latest response's `More actions` menu, selects `Read aloud`, captures the audio response from the browser, and saves it under `downloads/audio`.

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy123" \
  -d '{
    "model": "catgpt-browser",
    "messages": [{"role": "user", "content": "Reply with one short sentence."}],
    "read_aloud": true
  }'
```

The audio metadata is returned on the assistant message:

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "Here is one short sentence.",
      "audio": {
        "url": "https://chatgpt.com/...",
        "local_path": "downloads/audio/read_aloud_1716025800_abcd1234.mp3",
        "mime_type": "audio/mpeg",
        "size_bytes": 123456
      }
    }
  }]
}
```

Claude accepts the flag for compatibility but currently returns no audio.

Manual test:

```bash
python scripts/test_read_aloud.py
```

---

### Tool / Function Calling

Define tools in the request and the model will call them when appropriate.

**Request with tools:**

```python
response = client.chat.completions.create(
    model="claude-browser",
    messages=[{"role": "user", "content": "What's the weather in Paris?"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"}
                },
                "required": ["city"]
            }
        }
    }]
)
```

**Response when model calls a tool:**

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {
          "id": "call_a1b2c3d4e5f6...",
          "type": "function",
          "function": {
            "name": "get_weather",
            "arguments": "{\"city\": \"Paris\"}"
          }
        }
      ]
    },
    "finish_reason": "tool_calls"
  }]
}
```

**Sending tool results back:**

```python
# After executing the tool, send the result back
response = client.chat.completions.create(
    model="claude-browser",
    messages=[
        {"role": "user", "content": "What's the weather in Paris?"},
        {"role": "assistant", "tool_calls": [
            {"id": "call_a1b2c3...", "type": "function",
             "function": {"name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_a1b2c3...", "content": "Sunny, 25C"}
    ]
)
# Model responds with natural language summary
```

**LangChain example (full round-trip):**

```python
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool

@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"Sunny, 25C in {city}"

@tool
def add_numbers(a: int, b: int) -> str:
    """Add two numbers together."""
    return str(a + b)

llm = ChatOpenAI(model="claude-browser", base_url="http://localhost:8000/v1", api_key="dummy123")
llm_with_tools = llm.bind_tools([get_weather, add_numbers])

# Step 1: Model decides to call tools
response = llm_with_tools.invoke([
    HumanMessage(content="Weather in Tokyo and what's 42+58?")
])

# Step 2: Execute tools and send results
messages = [HumanMessage(content="Weather in Tokyo and what's 42+58?"), response]
tool_map = {"get_weather": get_weather, "add_numbers": add_numbers}

for tc in response.tool_calls:
    result = tool_map[tc["name"]].invoke(tc["args"])
    messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

# Step 3: Model summarizes results
final = llm_with_tools.invoke(messages)
print(final.content)
# "It's sunny and 25C in Tokyo, and 42 + 58 = 100."
```

**`tool_choice` options:**

| Value | Behavior |
|---|---|
| `"auto"` (default) | Model decides whether to call tools or answer directly |
| `"required"` | Model must call at least one tool |
| `"none"` | Tools are ignored, model answers directly |
| `{"type":"function","function":{"name":"X"}}` | Model must call the specified function |

---

### Image Input (Vision)

Send images using the standard OpenAI vision format.

```python
import base64

with open("photo.png", "rb") as f:
    img_b64 = base64.b64encode(f.read()).decode()

response = client.chat.completions.create(
    model="claude-browser",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "Describe this image in detail."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
        ]
    }]
)
```

**Multiple images:**

```python
response = client.chat.completions.create(
    model="claude-browser",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "Compare these two images."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img1_b64}"}},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img2_b64}"}},
        ]
    }]
)
```

HTTP URLs also work:

```python
{"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg"}}
```

---

### File Attachments

Send PDFs, DOCX, TXT, CSV, and other files via a custom `file` content type.

```python
import base64

with open("document.pdf", "rb") as f:
    pdf_b64 = base64.b64encode(f.read()).decode()

response = client.chat.completions.create(
    model="claude-browser",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "Summarize this PDF."},
            {"type": "file", "file": {
                "filename": "document.pdf",
                "data": pdf_b64,
                "mime_type": "application/pdf"
            }},
        ]
    }]
)
```

Alternative data-URL format:

```json
{"type": "file", "file": {"filename": "doc.pdf", "url": "data:application/pdf;base64,..."}}
```

---

### Image Generation (ChatGPT only)

**`POST /v1/images/generations`**

Generate images via DALL-E. Only available when `PROVIDER=chatgpt`. Returns HTTP 501 for Claude.

```python
response = client.images.generate(
    model="dall-e-3",
    prompt="A cyberpunk cat hacking a mainframe",
    n=1,
    size="1024x1024",
    response_format="b64_json",
)

# Save the image
import base64
with open("output.png", "wb") as f:
    f.write(base64.b64decode(response.data[0].b64_json))
```

```bash
curl -X POST http://localhost:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy123" \
  -d '{"prompt": "A cat in space", "n": 1, "response_format": "b64_json"}'
```

**Request parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `prompt` | string | required | Text description of the image |
| `model` | string | `dall-e-3` | Model name (ignored, uses ChatGPT's DALL-E) |
| `n` | int | `1` | Number of images (1-4) |
| `size` | string | `1024x1024` | Requested size (hint to ChatGPT) |
| `quality` | string | `standard` | `standard` or `hd` |
| `style` | string | `vivid` | `vivid` or `natural` |
| `response_format` | string | `b64_json` | `b64_json` or `url` (local file path) |

---

### List Models

**`GET /v1/models`**

Returns the available model based on the active provider.

```bash
curl http://localhost:8000/v1/models -H "Authorization: Bearer dummy123"
```

| Provider | Model ID | Owned By |
|---|---|---|
| Claude | `claude-browser` | `anthropic` |
| ChatGPT | `catgpt-browser` | `catgpt` |

---

## Custom REST API

In addition to the OpenAI-compatible endpoints, CatGPT exposes a simpler custom API:

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/chat` | Send a message in the current conversation |
| `POST` | `/thread/new` | Start a new conversation |
| `POST` | `/thread/{id}/chat` | Send a message in a specific thread |
| `GET` | `/threads` | List recent threads |
| `GET` | `/status` | Health check, login status, current thread |

```bash
# Chat in current thread
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy123" \
  -d '{"message": "Hello!"}'

# Chat and download read-aloud audio (ChatGPT only)
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy123" \
  -d '{"message": "Reply with one short sentence.", "read_aloud": true}'

# Start new thread
curl -X POST http://localhost:8000/thread/new \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy123" \
  -d '{"message": "New conversation"}'

# Check status
curl -H "Authorization: Bearer dummy123" http://localhost:8000/status
```

---

## TUI Terminal Client

CatGPT includes a terminal chat interface with a cyberpunk theme, built with Textual.

```bash
python -m src.cli.app
```

### Commands

| Command | Description |
|---|---|
| `/new` | Start a fresh conversation |
| `/threads` | List recent threads |
| `/thread <id>` | Switch to a thread |
| `/images` | List downloaded DALL-E images |
| `/status` | Connection details |
| `/clear` | Clear chat display |
| `/help` | Show commands |
| `/exit` | Quit |

Shortcuts: `Ctrl+N` (new), `Ctrl+T` (threads), `Ctrl+L` (clear), `Ctrl+Q` (quit)

---

## Provider Differences

| Behavior | Claude | ChatGPT |
|---|---|---|
| Model ID | `claude-browser` | `catgpt-browser` |
| Image generation | Not supported (501) | Supported (DALL-E) |
| Table rendering | Tab-separated text | Markdown with pipes |
| Avg response time | 15-20s | 7-10s |
| Tool calling prompt | Collaborative framing | Direct instruction |
| `tool_choice` support | Yes | Yes |
| Vision input | Yes | Yes |
| File attachments | Yes | Yes |
| Read-aloud audio | Not implemented | Supported via `read_aloud: true` |
