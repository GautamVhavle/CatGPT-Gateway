<p align="center">
  <img src="assets/catgpt_gatway_logo.jpeg" width="180" alt="CatGPT logo" />
</p>

<h1 align="center">CatGPT</h1>

<p align="center">
  <strong>A browser-backed, multi-protocol AI gateway for ChatGPT, Claude, and MiniMax.</strong><br />
  Connect OpenAI, Anthropic, Ollama, LangChain, and self-hosted clients to one persistent gateway.
</p>

<p align="center">
  <a href="https://github.com/TheBadFella/CatGPT/releases/latest"><img src="https://img.shields.io/github/v/release/TheBadFella/CatGPT?style=for-the-badge&color=1976D2" alt="Latest release" /></a>
  <a href="https://github.com/TheBadFella/CatGPT/pkgs/container/catgpt"><img src="https://img.shields.io/badge/GHCR-ready-00897B?style=for-the-badge&logo=docker&logoColor=white" alt="GHCR image" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/TheBadFella/CatGPT?style=for-the-badge&color=F9A825" alt="MIT license" /></a>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="#why-this-fork">Features</a> ·
  <a href="#fork-vs-upstream">Fork vs Upstream</a> ·
  <a href="docs/API.md">API</a> ·
  <a href="docs/SETUP.md">Setup</a> ·
  <a href="docs/ARCHITECTURE.md">Architecture</a>
</p>

---

CatGPT turns a logged-in browser session into familiar API endpoints. ChatGPT and Claude use a persistent, automated browser; MiniMax uses its official API while keeping the same gateway interface. It is designed for private, self-hosted integrations—not as an official provider API.

## Why This Fork

<table>
  <tr>
    <td width="50%" valign="top">
      <h3>🔌 Protocol Compatibility</h3>
      OpenAI Chat Completions and Responses, Anthropic Messages, Ollama chat/generate/embed, tool calling, and SSE-compatible responses.
    </td>
    <td width="50%" valign="top">
      <h3>⚡ Sessions & Scale</h3>
      Multi-tab concurrency, persistent <code>x-session-id</code> sessions, explicit thread targeting, async jobs, and app-scoped conversation isolation.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <h3>🖼️ Documents & Media</h3>
      Vision, file attachments, multipage extraction, JSON Schema output, ChatGPT image generation, and read-aloud audio capture.
    </td>
    <td width="50%" valign="top">
      <h3>🛡️ Resilience & Operations</h3>
      Long-prompt attachment fallback, configurable model effort, robust response detection, optional auth, health checks, and a non-root jlesage GUI.
    </td>
  </tr>
</table>

> [!TIP]
> Long-prompt fallback is enabled by default. If ChatGPT disables direct submission, CatGPT uploads the complete request as a temporary UTF-8 attachment. The flow has been validated with a 1.4-million-character request.

## Fork vs Upstream

Both projects share the core browser gateway: ChatGPT and Claude support, OpenAI Chat Completions, tool calling, vision and file inputs, image generation, multi-tab concurrency, persistent sessions, SSE compatibility, a terminal client, and Docker deployment.

This table focuses only on meaningful differences:

| Capability | This fork | Upstream |
|---|:---:|:---:|
| OpenAI Responses API | ✅ | — |
| Anthropic Messages adapter | ✅ | — |
| Ollama-compatible API | ✅ | — |
| MiniMax provider | ✅ | — |
| Async completion jobs | ✅ | — |
| App-scoped routes and thread isolation | ✅ | — |
| Explicit ChatGPT thread targeting | ✅ | Basic REST only |
| JSON Schema normalization | ✅ | — |
| Structured multipage extraction | ✅ | — |
| ChatGPT read-aloud audio capture | ✅ | — |
| Long-prompt attachment fallback | ✅ | — |
| Configurable model and effort routing | ✅ | — |
| Non-root jlesage browser GUI | ✅ | — |
| Live multi-tab preview dashboard | — | ✅ |

<sub>Comparison verified against <a href="https://github.com/GautamVhavle/CatGPT-Gateway">upstream</a> at commit <code>1771f5b</code>.</sub>

## Providers

| Provider | Connection | Model | Notable capabilities |
|---|---|---|---|
| ChatGPT | Persistent browser | `catgpt-browser` or configured GPT model | Images, vision, files, audio, model/effort switching |
| Claude | Persistent browser | `claude-browser` | Chat, vision, files, tools |
| MiniMax | Official API | `MiniMax-M2.7` | OpenAI-compatible text requests without a browser |

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/TheBadFella/CatGPT.git
cd CatGPT
cp .env.example .env
```

At minimum, change `CATGPT_API_KEY` and `CATGPT_VNC_PASSWORD` in `.env`. See [.env.example](.env.example) for common local and Compose settings.

### 2. Start the container

```bash
docker compose up -d
```

| Service | Address |
|---|---|
| Browser login and recovery | `http://localhost:5800` |
| API gateway | `http://localhost:8650` |

Open the browser GUI, enter the VNC password, and sign in to the selected browser provider once. The profile persists across restarts.

### 3. Send a request

```bash
curl http://localhost:8650/v1/chat/completions \
  -H "Authorization: Bearer $CATGPT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "catgpt-browser",
    "messages": [{"role": "user", "content": "Say hello from CatGPT."}]
  }'
```

## API Surfaces

| Client ecosystem | Primary endpoints |
|---|---|
| OpenAI | `/v1/chat/completions`, `/v1/responses`, `/v1/images/generations`, `/v1/models` |
| Anthropic | `/v1/messages` |
| Ollama | `/api/chat`, `/api/generate`, `/api/embed`, `/api/tags` |
| Native CatGPT | `/chat`, `/thread/{id}/chat`, `/thread/new`, `/threads`, `/status` |

Use `/{app_name}/v1/...` or `/{app_name}/api/...` routes to isolate applications such as Open WebUI, Mealie, Linkwarden, or internal agents. Use `thread_id` or `x-session-id` when the caller needs stable conversation continuity.

> [!NOTE]
> `stream=true` is protocol-compatible, but browser generation finishes before CatGPT emits the SSE or NDJSON response chunks. It is not live token forwarding from the provider.

## Essential Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PROVIDER` | `chatgpt` | Select `chatgpt`, `claude`, or `minimax` |
| `CATGPT_API_KEY` | `dummy123` | Bearer token used by Docker Compose |
| `CATGPT_VNC_PASSWORD` | `catgpt` | Browser GUI password |
| `MAX_CONCURRENT_REQUESTS` | `3` | Concurrent browser-backed requests |
| `CHATGPT_DEFAULT_MODEL` | Current UI selection | Default ChatGPT model mapping |
| `CHATGPT_LONG_PROMPT_FALLBACK` | `attachment` | Upload oversized prompts or use `error` for HTTP 413 |

See [.env.example](.env.example), [docker-compose.yml](docker-compose.yml), and the [Setup Guide](docs/SETUP.md) for advanced options. Add runtime-only Docker overrides under `services.catgpt.environment`.

## Documentation

| Guide | What it covers |
|---|---|
| [API Reference](docs/API.md) | Request formats, tools, vision, files, images, audio, and native routes |
| [Setup Guide](docs/SETUP.md) | Docker, local installation, login, persistence, and troubleshooting |
| [Model Switching](docs/MODEL_SWITCHING.md) | ChatGPT model aliases, versions, and effort settings |
| [Architecture](docs/ARCHITECTURE.md) | Browser lifecycle, routing, extraction, and response detection |
| [Chrome Runbook](docs/CHROME_PLAYWRIGHT_RUNBOOK.md) | Browser automation diagnostics and recovery |

## Operational Notes

- Browser-backed requests take as long as the provider UI takes to answer.
- Provider UI updates can require selector or detector maintenance.
- Keep browser data, logs, and jlesage configuration on persistent volumes.
- Pull a new image with `docker compose pull && docker compose up -d`.

## Credits & License

CatGPT is a feature-focused fork of [GautamVhavle/CatGPT-Gateway](https://github.com/GautamVhavle/CatGPT-Gateway). Contributions and upstream improvements are credited through the shared Git history.

Released under the [MIT License](LICENSE).
