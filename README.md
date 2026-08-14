# CatGPT

CatGPT exposes a logged-in ChatGPT browser session as an OpenAI-compatible API.
It is meant for self-hosted use where you want apps such as Open WebUI,
LangChain, scripts, or internal tools to talk to ChatGPT through one persistent
browser profile.

## Table of Contents

1. [What is CatGPT](#what-is-catgpt)
2. [How to Setup on Docker](#how-to-setup-on-docker)
3. [Supported Models](#supported-models)
4. [Docker Environment Variables](#docker-environment-variables)
5. [Usage Examples](#usage-examples)
6. [Troubleshooting](#troubleshooting)

## What is CatGPT

CatGPT runs Chromium with a saved ChatGPT login, then wraps that browser session
with HTTP APIs that look like OpenAI and Ollama.

This version supports:

| Capability | Supported |
| --- | :---: |
| OpenAI `/v1/chat/completions` | Yes |
| OpenAI `/v1/responses` | Yes |
| Async chat jobs | Yes |
| OpenAI `/v1/images/generations` through ChatGPT image generation | Yes |
| Ollama `/api/chat`, `/api/generate`, `/api/tags` compatibility | Yes |
| Tool / function calling compatibility | Yes |
| Image input / vision requests | Yes |
| File attachments | Yes |
| App-scoped routes such as `/mealie/v1/chat/completions` | Yes |
| App-thread isolation using request identity | Yes |
| Model switching for current ChatGPT picker models | Yes |
| noVNC browser login UI | Yes |

The default browser-backed model id is `catgpt-browser`. You can also request
specific ChatGPT picker models listed below.

## How to Setup on Docker

The repository includes a production-style `docker-compose.yml` using the
published image:

```yaml
image: ghcr.io/thebadfella/catgpt:latest
```

1. Clone the repo.

```bash
git clone <repo-url> catgpt
cd catgpt
```

2. Create the host variables used by `docker-compose.yml`.

```bash
export DOCKERDIR=/path/to/docker
export CATGPT_API_KEY=change-this-api-token
export CATGPT_VNC_PASSWORD=change-this-vnc-password
```

If you prefer a `.env` file beside `docker-compose.yml`:

```env
DOCKERDIR=/path/to/docker
CATGPT_API_KEY=change-this-api-token
CATGPT_VNC_PASSWORD=change-this-vnc-password
```

3. Start CatGPT.

```bash
docker compose up -d
```

4. Log in to ChatGPT once.

Open:

```text
http://localhost:6080
```

Enter the VNC password from `CATGPT_VNC_PASSWORD`, then log in to ChatGPT inside
the browser. The session is saved in:

```text
${DOCKERDIR}/appdata/catgpt/browser
```

5. Check the API.

The compose file maps the API to host port `8650`.

```bash
curl -H "Authorization: Bearer $CATGPT_API_KEY" \
  http://localhost:8650/v1/models
```

6. Send a message.

```bash
curl -X POST http://localhost:8650/v1/chat/completions \
  -H "Authorization: Bearer $CATGPT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "catgpt-browser",
    "messages": [
      {"role": "user", "content": "Say hello from CatGPT in one sentence."}
    ]
  }'
```

After code changes, rebuild instead of only restarting:

```bash
docker compose up --build -d
```

## Supported Models

CatGPT exposes `catgpt-browser` plus the configured ChatGPT picker models. The
model switcher is driven by `CHATGPT_MODEL_ALIASES` and
`CHATGPT_MODEL_SETTINGS`, so you can update labels when ChatGPT changes the UI.

| API model id | Advanced model | Effort | Accepted aliases |
| --- | --- | --- | --- |
| `catgpt-browser` | Current selection, or `CHATGPT_DEFAULT_MODEL` when set | Current | `auto`, `default`, `browser` |
| `gpt-5.6-sol` | GPT-5.6 Sol | Instant | `GPT-5.6 Sol`, `5.6 Sol`, `Instant` |
| `gpt-5.6-sol-medium` | GPT-5.6 Sol | Medium | `GPT-5.6 Sol`, `5.6 Sol` |
| `gpt-5.6-sol-high` | GPT-5.6 Sol | High | `GPT-5.6 Sol`, `5.6 Sol` |
| `gpt-5.6-sol-extra-high` | GPT-5.6 Sol | Extra High | `GPT-5.6 Sol`, `5.6 Sol` |
| `gpt-5.6-sol-pro` | GPT-5.6 Sol | Pro | `GPT-5.6 Sol`, `5.6 Sol`, `Pro` |
| `gpt-5.5` | GPT-5.5 | Instant | `GPT-5.5`, `5.5` |
| `gpt-5.5-medium` | GPT-5.5 | Medium | `GPT-5.5`, `5.5` |
| `gpt-5.5-high` | GPT-5.5 | High | `GPT-5.5`, `5.5` |
| `gpt-5.5-thinking` | GPT-5.5 | High | `Thinking`, `5.5 Thinking` |
| `gpt-5.5-extra-high` | GPT-5.5 | Extra High | `GPT-5.5`, `5.5` |
| `gpt-5.5-pro` | GPT-5.5 | Pro | `5.5 Pro` |

Default configurable settings:

```env
CHATGPT_MODEL_SETTINGS=gpt-5.6-sol=Instant,gpt-5.6-sol-medium=Medium,gpt-5.6-sol-high=High,gpt-5.6-sol-extra-high=Extra High,gpt-5.6-sol-pro=Pro,gpt-5.5=Instant,gpt-5.5-medium=Medium,gpt-5.5-high=High,gpt-5.5-thinking=High,gpt-5.5-extra-high=Extra High,gpt-5.5-pro=Pro
```

Example: default all `catgpt-browser` requests to GPT-5.6 Sol with High effort.

```yaml
environment:
  CHATGPT_DEFAULT_MODEL: "gpt-5.6-sol-high"
```

## Docker Environment Variables

### Required by the included compose file

| Variable | Example | Purpose |
| --- | --- | --- |
| `DOCKERDIR` | `/mnt/user/appdata` | Host root for persisted browser data and logs |
| `CATGPT_API_KEY` | `change-me` | Value passed into container as `API_TOKEN` |
| `CATGPT_VNC_PASSWORD` | `change-me` | Value passed into container as `VNC_PASSWORD` |

### Browser and provider

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROVIDER` | `chatgpt` | Browser provider. Use `chatgpt` for CatGPT; `claude` is also wired in the codebase. |
| `CHATGPT_URL` | `https://chatgpt.com` | ChatGPT URL to open. |
| `CLAUDE_URL` | `https://claude.ai` | Claude URL when `PROVIDER=claude`. |
| `HEADLESS` | `false` | Keep `false` for Docker/noVNC. |
| `BROWSER_CHANNEL` | `chrome` | Browser channel. Use `chromium` to force bundled Chromium. |
| `BROWSER_DATA_DIR` | `browser_data` | Persistent browser profile path inside the container. |
| `SLOW_MO` | `25` | Browser automation delay in milliseconds. |
| `DISPLAY` | `:99` | X display used by the container. |
| `DISPLAY_WIDTH` | `1366` in compose | Virtual display width. |
| `DISPLAY_HEIGHT` | `768` in compose | Virtual display height. |
| `DISPLAY_DEPTH` | `24` in compose | Virtual display depth. |

### Model switching

| Variable | Default | Purpose |
| --- | --- | --- |
| `CHATGPT_DEFAULT_MODEL` | empty | Model to use when a request asks for `catgpt-browser`, `auto`, or `default`. |
| `CHATGPT_MODEL_ALIASES` | Current GPT-5.6 Sol and GPT-5.5 aliases | Comma-separated `model=label|alias` map for Advanced → Model choices. |
| `CHATGPT_MODEL_SETTINGS` | Instant/Medium/High/Extra High/Pro mappings | Comma-separated effort map used through Advanced → Effort. |
| `CHATGPT_MODEL_SWITCH_TIMEOUT` | `10000` | Milliseconds to wait for a model label after switching. |
| `CHATGPT_MODEL_SWITCH_STRICT` | `false` | Return an error when a configured model is not visible instead of continuing. |

### API

| Variable | Default | Purpose |
| --- | --- | --- |
| `API_HOST` | `0.0.0.0` | Server bind host inside the container. |
| `API_PORT` | `8000` | Server port inside the container. |
| `API_TOKEN` | empty in code, set from `CATGPT_API_KEY` in compose | Bearer token. Empty disables auth. |
| `API_TOKEN_OPTIONAL` | `false`, `true` in compose | Allow requests without a token while still accepting token auth. |
| `RATE_LIMIT_SECONDS` | `5` | Basic request pacing. |
| `API_THREAD_CONTRACT_MODE` | `false`, `true` in compose | Cache large system instructions in a thread and send compact reminders. |
| `API_THREAD_CONTRACT_TTL_SECONDS` | `3600` | Thread contract TTL. |
| `API_APP_THREAD_MODE` | `false`, `true` in compose | Route different apps/users to dedicated ChatGPT threads. |
| `API_APP_THREAD_TTL_SECONDS` | `86400` | App-thread mapping TTL. |
| `API_APP_THREAD_DELETE_EXPIRED` | `false`, `true` in compose | Delete expired CatGPT-owned app threads from ChatGPT UI. |
| `API_HEADER_ROW_MERGE_MODE` | `false`, `true` in compose | Merge header-only rows into the next row for structured JSON outputs. |

### Timeouts and interaction pacing

| Variable | Default | Purpose |
| --- | --- | --- |
| `RESPONSE_TIMEOUT` | `120000` | Max response wait in milliseconds. |
| `SELECTOR_TIMEOUT` | `10000` | DOM selector wait in milliseconds. |
| `POLL_INTERVAL_MS` | `300` | Response completion polling interval. |
| `TYPING_SPEED_MIN` | `50` | Minimum typing delay. |
| `TYPING_SPEED_MAX` | `150` | Maximum typing delay. |
| `THINKING_PAUSE_MIN` | `500`, `1000` in compose | Minimum pause before actions. |
| `THINKING_PAUSE_MAX` | `1500`, `3000` in compose | Maximum pause before actions. |

### Attachments, generated files, and Ollama compatibility

| Variable | Default | Purpose |
| --- | --- | --- |
| `ATTACHMENT_EXPAND_MULTIPAGE` | `true` | Expand supported multipage files for per-page extraction. |
| `ATTACHMENT_MAX_PAGES` | `24` | Max pages/frames to render from attachments. |
| `ATTACHMENT_RENDER_DPI` | `144` | Render DPI for attachment extraction. |
| `IMAGES_DIR` | `downloads/images` | Generated/downloaded image output path. |
| `AUDIO_DIR` | `downloads/audio` | Read-aloud audio output path. |
| `LOG_DIR` | `logs` | Log path. |
| `OLLAMA_EMBEDDING_MODELS` | `nomic-embed-text` | Names exposed as Ollama-compatible embedding models. |
| `OLLAMA_EMBEDDING_DIMENSIONS` | `768` | Dimension count for deterministic compatibility embeddings. |
| `OLLAMA_ACTIVE_MODEL_TTL_SECONDS` | `900` | TTL for `/api/ps` active model tracking. |

### VNC and logging

| Variable | Default | Purpose |
| --- | --- | --- |
| `VNC_PASSWORD` | `catgpt` | Password for `http://localhost:6080`. |
| `LOG_LEVEL` | `DEBUG` | Python logging level. |
| `VERBOSE` | `true` | Log to console as well as files. |

## Usage Examples

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8650/v1",
    api_key="change-this-api-token",
)

response = client.chat.completions.create(
    model="gpt-5.6-sol-high",
    messages=[
        {"role": "user", "content": "Summarize why browser-backed APIs are useful."}
    ],
)

print(response.choices[0].message.content)
```

### Responses API

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8650/v1", api_key="change-this-api-token")

response = client.responses.create(
    model="catgpt-browser",
    instructions="Be concise.",
    input="Give me three setup checks for CatGPT.",
)

print(response.output[0].content[0].text)
```

### Async job

```bash
JOB_ID="$(
  curl -s -X POST http://localhost:8650/v1/chat/completions/async \
    -H "Authorization: Bearer $CATGPT_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"catgpt-browser","messages":[{"role":"user","content":"Write a short checklist."}]}' |
  python -c "import json,sys; print(json.load(sys.stdin)['id'])"
)"

curl -H "Authorization: Bearer $CATGPT_API_KEY" \
  "http://localhost:8650/v1/chat/completions/async/$JOB_ID"
```

### Ollama-compatible clients

Point Ollama-compatible tools at:

```text
http://localhost:8650
```

Common endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/tags` | List chat and embedding model profiles |
| `POST /api/chat` | Chat request |
| `POST /api/generate` | Text generation request |
| `POST /api/embed` | Deterministic compatibility embedding shim |

### App-scoped routes

Use app-scoped paths when several apps share one CatGPT instance:

```text
http://localhost:8650/mealie/v1/chat/completions
http://localhost:8650/linkwarden/v1/chat/completions
http://localhost:8650/immich/v1/chat/completions
```

With `API_APP_THREAD_MODE=true`, CatGPT can keep those apps in separate ChatGPT
threads.

### Useful things to run through CatGPT

| Use case | Why CatGPT helps |
| --- | --- |
| Self-hosted apps needing an OpenAI-compatible URL | Use `base_url=http://catgpt:8000/v1` inside Docker networks. |
| Open WebUI / Ollama-style clients | Use `/api/chat` and `/api/tags` without running a real Ollama model. |
| Structured extraction from files | Send PDFs/images and request JSON output. |
| App-specific assistants | Use app-scoped URLs and app-thread mode to isolate context. |
| Occasional image generation | Use `/v1/images/generations` and let ChatGPT create/download the result. |
| Tool-calling prototypes | Use OpenAI-style tools with browser-backed ChatGPT responses. |

## Troubleshooting

| Problem | Fix |
| --- | --- |
| API says the browser is not initialized | Wait 30-60 seconds after container start, then check `docker logs catgpt --tail 100`. |
| Login expired | Open `http://localhost:6080` and log in again. |
| noVNC opens but ChatGPT is logged out | Log in inside noVNC; the profile persists in the mounted browser volume. |
| Model switch fails | Check `/v1/models`, then update `CHATGPT_MODEL_ALIASES` or set `CHATGPT_MODEL_SWITCH_STRICT=false`. |
| A code change is not visible | Run `docker compose up --build -d`. |
| Browser profile is stuck | Stop the container and remove stale browser lock files from the mounted browser directory. |
| Responses are slow | ChatGPT browser automation is serialized; one request uses the browser at a time. |

CatGPT intentionally does not support streaming. Requests return after the
browser response is complete.
