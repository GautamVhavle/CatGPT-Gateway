<p align="center">
  <img src="assets/catgpt_gatway_logo.jpeg" width="200" alt="CatGPT Gateway Logo" />
</p>

<h1 align="center">CatGPT Gateway</h1>

<p align="center">
  <strong>Turn your ChatGPT or Claude account into a fully working OpenAI-compatible API.</strong><br/>
  No API keys needed. Just your browser login.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> &bull;
  <a href="#providers">Providers</a> &bull;
  <a href="docs/API.md">API Docs</a> &bull;
  <a href="docs/SETUP.md">Full Setup Guide</a> &bull;
  <a href="docs/ARCHITECTURE.md">How It Works</a> &bull;
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue?style=flat-square" alt="Python 3.9+" />
  <img src="https://img.shields.io/badge/providers-Claude_%7C_ChatGPT-purple?style=flat-square" alt="Providers" />
  <img src="https://img.shields.io/badge/API-OpenAI_compatible-green?style=flat-square" alt="OpenAI Compatible" />
  <img src="https://img.shields.io/badge/docker-ready-blue?style=flat-square" alt="Docker" />
  <img src="https://img.shields.io/github/license/GautamVhavle/CatGPT-Gateway?style=flat-square" alt="MIT License" />
  <img src="https://img.shields.io/github/stars/GautamVhavle/CatGPT-Gateway?style=flat-square" alt="Stars" />
</p>

---

## What is this?

You already pay for ChatGPT Plus or have a free Claude account. But the official APIs cost extra and the free tiers are limited.

**CatGPT Gateway** turns your existing browser session into a fully functional OpenAI-compatible API server. It runs a real browser in the background, automates the web UI, and exposes everything through standard API endpoints that work with the OpenAI Python SDK, LangChain, and anything that speaks the OpenAI protocol.

```python
# This just works. Point any OpenAI client at your local gateway.
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="dummy123")

response = client.chat.completions.create(
    model="claude-browser",  # or "catgpt-browser" for ChatGPT
    messages=[{"role": "user", "content": "Hello from my own API!"}]
)
print(response.choices[0].message.content)
```

That's it. Your Claude or ChatGPT subscription just became an API.

---

## Features

| Feature | Claude | ChatGPT |
|---|:---:|:---:|
| Chat completions | Yes | Yes |
| Multi-turn conversations | Yes | Yes |
| Tool / function calling | Yes | Yes |
| Image input (vision) | Yes | Yes |
| File attachments (PDF, DOCX, etc.) | Yes | Yes |
| Image generation (DALL-E) | -- | Yes |
| Read-aloud audio download | -- | Yes |
| Interactive TUI (terminal chat) | Yes | Yes |
| OpenAI SDK compatible | Yes | Yes |
| LangChain compatible | Yes | Yes |
| Docker deployment | Yes | Yes |

---

## Providers

CatGPT Gateway supports two providers. Set `PROVIDER` in your `.env` file to switch.

### Claude (`PROVIDER=claude`)

Use your existing Anthropic account (free or Pro). The gateway connects to `claude.ai` and exposes Claude as an OpenAI-compatible API.

- Model ID: `claude-browser`
- Works with: Free tier, Pro, Team
- Image generation: Not supported (returns 501)

### ChatGPT (`PROVIDER=chatgpt`)

Use your existing OpenAI account (free or Plus). The gateway connects to `chatgpt.com` and exposes ChatGPT as an OpenAI-compatible API.

- Model ID: `catgpt-browser`
- Works with: Free tier, Plus, Team
- Image generation: Supported via DALL-E

---

## Quick Start

### Option 1: Docker (recommended)

```bash
# Clone the repo
git clone https://github.com/GautamVhavle/CatGPT-Gateway.git
cd CatGPT-Gateway

# Copy the example env and pick your provider
cp .env.example .env
# Edit .env -> set PROVIDER=claude or PROVIDER=chatgpt

# Build and start
docker compose up --build -d

# Open the browser UI to log in (one-time)
open http://localhost:6080/vnc.html
# Sign into Claude or ChatGPT using EMAIL + PASSWORD or Microsoft/Apple/magic link
# ⚠ Google login is blocked by Google in automated browser contexts — don't use it
# Close the noVNC tab when done - your session is saved to a Docker volume

# Verify it works
curl -H "Authorization: Bearer dummy123" http://localhost:8000/v1/models
```

### Option 2: Local (no Docker)

```bash
# Clone and setup
git clone https://github.com/GautamVhavle/CatGPT-Gateway.git
cd CatGPT-Gateway
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
patchright install chromium

# Copy env and pick your provider
cp .env.example .env

# First login (one-time - a browser window opens)
# Use email + password, Microsoft, Apple, or magic link — NOT Google OAuth
python scripts/first_login.py

# Start the API server
python -m src.api.server
# API is now live at http://localhost:8000
```

> For the full setup guide with Docker internals, Nix flake, systemd service, and troubleshooting, see [docs/SETUP.md](docs/SETUP.md).

---

## Usage

Once the server is running, you can use it with any OpenAI-compatible client.

### Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="dummy123")

# Simple chat
response = client.chat.completions.create(
    model="claude-browser",
    messages=[{"role": "user", "content": "Explain quantum computing in simple terms"}]
)
print(response.choices[0].message.content)
```

### Python (LangChain)

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model="claude-browser",
    base_url="http://localhost:8000/v1",
    api_key="dummy123",
)
response = llm.invoke("What are the best practices for REST API design?")
print(response.content)
```

### curl

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy123" \
  -d '{
    "model": "claude-browser",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Tool / Function Calling

Full round-trip tool calling works with both providers. Define tools, let the model call them, send results back.

```python
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool

@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"Sunny, 25C in {city}"

llm = ChatOpenAI(
    model="claude-browser",
    base_url="http://localhost:8000/v1",
    api_key="dummy123",
)

llm_with_tools = llm.bind_tools([get_weather])
response = llm_with_tools.invoke("What's the weather in Tokyo?")

# Model returns structured tool calls
print(response.tool_calls)
# [{'name': 'get_weather', 'args': {'city': 'Tokyo'}, 'id': 'call_...'}]

# Execute the tool and send results back
messages = [HumanMessage(content="What's the weather in Tokyo?"), response]
result = get_weather.invoke(response.tool_calls[0]["args"])
messages.append(ToolMessage(content=result, tool_call_id=response.tool_calls[0]["id"]))

final = llm_with_tools.invoke(messages)
print(final.content)
# "It's sunny and 25C in Tokyo!"
```

### Image Generation (ChatGPT only)

```python
response = client.images.generate(
    prompt="A cyberpunk cat hacking into a mainframe",
    model="dall-e-3",
    n=1,
    response_format="b64_json",
)
```

> For the complete API reference with vision input, file attachments, image generation, tool_choice, and the custom REST API, see [docs/API.md](docs/API.md).

---

## Configuration

Copy `.env.example` to `.env` and edit to your needs:

```bash
cp .env.example .env
```

Key settings:

| Variable | Default | Description |
|---|---|---|
| `PROVIDER` | `chatgpt` | Which provider to use: `chatgpt` or `claude` |
| `BROWSER_DATA_DIR` | `./browser_data` | Browser profile directory (keeps your login) |
| `API_TOKEN` | `dummy123` | Bearer token for API authentication |
| `API_PORT` | `8000` | Port the API server listens on |
| `HEADLESS` | `false` | Run browser without display (not recommended) |

> See [.env.example](.env.example) for all available settings with descriptions.

---

## Testing

All test scripts auto-detect the active provider from your `.env` file.

```bash
source .venv/bin/activate

# Start the server (if not already running)
python -m src.api.server &

# Run individual test suites
python scripts/test_phase1.py           # Basic send/receive
python scripts/test_multi_turn.py       # Multi-turn conversations
python scripts/test_robust.py           # Tables, code blocks, long responses
python scripts/test_images.py           # Image detection
python scripts/test_read_aloud.py       # ChatGPT read-aloud audio capture
python scripts/test_langchain_tools.py  # LangChain + tool calling (needs server running)
```

Both providers have been tested and verified. See [docs/TEST_REPORT.md](docs/TEST_REPORT.md) for full results with inputs, outputs, and timings.

---

## How It Works (short version)

```
Your app (OpenAI SDK / LangChain / curl)
    |
    v
CatGPT Gateway (FastAPI on port 8000)
    |
    v
Real Chromium browser (automated via Patchright)
    |
    v
claude.ai or chatgpt.com (your logged-in session)
    |
    v
Response extracted from the page, formatted as OpenAI JSON, returned to your app
```

The gateway runs a real browser session with anti-detection measures (stealth patches, human-like typing, viewport jitter, persistent cookies). It types your message into the chat input, waits for the response to complete, extracts the text, and returns it in the standard OpenAI response format.

Tool calling is implemented via prompt engineering: tool definitions are injected as structured instructions, the model outputs JSON tool calls, and the gateway parses them into the proper OpenAI `tool_calls` format.

> For the full deep dive (browser lifecycle, stealth techniques, response detection, DOM selectors, Docker internals), see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Known Limitations

- **No streaming** - Responses are returned all at once after completion. `stream=true` returns a 400 error.
- **Single concurrency** - One request at a time (browser is single-threaded). Requests are queued.
- **Response time** - Each request takes 5-30s depending on response length (real browser round-trip).
- **Session expiry** - Browser sessions expire after days/weeks. Re-login via noVNC or `first_login.py`.
- **UI changes** - If Claude or ChatGPT update their HTML, selectors may need updating. All selectors are centralized in `selectors.py` for easy fixes.
- **Tool calling** - Works via prompt engineering, not native API. Reliable for 1-7 tools. Very complex schemas may occasionally need a retry.

---

## Contributing

Contributions are welcome! Whether it's fixing a broken selector, adding a new provider, improving detection logic, or writing docs, we'd love your help.

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on how to get started.

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

<p align="center">
  If you find this project useful, consider giving it a star. It helps others discover it and keeps the project going.<br/>
  <a href="https://github.com/GautamVhavle/CatGPT-Gateway">
    <img src="https://img.shields.io/github/stars/GautamVhavle/CatGPT-Gateway?style=social" alt="Star on GitHub" />
  </a>
</p>
