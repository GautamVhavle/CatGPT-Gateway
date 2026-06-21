# Architecture

A deep dive into how CatGPT Gateway works under the hood.

---

## Table of Contents

- [Overview](#overview)
- [Browser Lifecycle](#browser-lifecycle)
- [Stealth and Anti-Detection](#stealth-and-anti-detection)
- [Message Flow](#message-flow)
- [Response Detection](#response-detection)
- [Tool Calling](#tool-calling)
- [File and Image Upload](#file-and-image-upload)
- [Echo Detection and Recovery](#echo-detection-and-recovery)
- [Selector Fallback System](#selector-fallback-system)
- [Provider Abstraction](#provider-abstraction)
- [Docker Stack](#docker-stack)

---

## Overview

```
Your app (OpenAI SDK / LangChain / curl)
    |
    v
FastAPI server (port 8000)
    |
    |-- openai_routes.py     Translates OpenAI requests to browser actions
    |-- openai_schemas.py    Pydantic models matching OpenAI spec
    v
Provider client (ChatGPTClient or ClaudeClient)
    |
    |-- client.py            send_message(), new_chat(), file upload
    |-- detector.py          Waits for response completion
    |-- selectors.py         DOM selectors for the provider's UI
    v
BrowserManager (Patchright / Playwright)
    |
    |-- stealth.py           Anti-detection patches
    |-- human.py             Human-like typing and clicking
    v
Real Chromium browser --> chatgpt.com or claude.ai
```

---

## Browser Lifecycle

1. **Launch**: `BrowserManager` creates a Patchright persistent browser context at `browser_data/` (or `browser_data_claude/`). This preserves cookies, login state, and Cloudflare clearance across restarts.

2. **DNS Pre-resolution (Docker only)**: Chrome's DNS resolver can fail inside Docker. The entrypoint script pre-resolves `chatgpt.com`, `cdn.oaistatic.com`, `claude.ai`, and related domains via Python's socket module and writes them to `/etc/hosts`. The browser also gets `--host-resolver-rules` flags.

3. **Navigate**: Opens the provider URL with retry logic (up to 5 attempts with exponential backoff).

4. **Stealth (deferred)**: Stealth patches are applied *after* first navigation to avoid breaking DNS. See the stealth section below.

5. **Login Check**: `ensure_logged_in()` checks for login indicators (chat input presence) and prompts if needed.

6. **Client Injection**: The provider client is created and injected into all API routers.

7. **Shutdown**: Browser closes gracefully on FastAPI shutdown.

---

## Stealth and Anti-Detection

The gateway uses multiple techniques to avoid bot detection:

| Technique | How | Where |
|---|---|---|
| Persistent Chrome profile | `launch_persistent_context()` retains cookies and Cloudflare clearance | `browser/manager.py` |
| playwright-stealth | Patches `navigator.webdriver`, WebGL, canvas, plugins | `browser/stealth.py` |
| Docker stealth workaround | Uses `page.evaluate()` instead of `add_init_script()` (the latter breaks DNS in Docker) | `browser/stealth.py` |
| Human-like typing | `keyboard.insert_text()` for paste-style input on contenteditable divs | `browser/human.py` |
| Mouse simulation | Hover before click, natural movement | `browser/human.py` |
| Random delays | 500-1200ms before typing, 300-600ms before sending | `browser/human.py` |
| Viewport jitter | +/- 20px randomization on each launch (1280x720 base) | `browser/manager.py` |
| Headful mode | Always runs with visible browser (headless is trivially detected) | `config.py` |
| Lock file cleanup | Auto-cleans stale `SingletonLock` files from crashed Chrome processes | `browser/manager.py` |

### Docker DNS Fix

`playwright-stealth`'s `add_init_script()` method causes Chrome to fail DNS resolution inside Docker containers. The fix in `stealth.py` uses `page.evaluate()` to inject stealth JS at runtime, and hooks `framenavigated` + `page` events to re-inject on every navigation.

---

## Message Flow

```
send_message(text, image_paths, file_paths, read_aloud)
|
|-- 1. Count existing assistant messages (pre_count)
|-- 2. Random delay (500-1200ms, human simulation)
|-- 3. Upload files if any
|      +-- set_input_files() on hidden <input type="file">
|      +-- Wait 3s + extra per file for processing
|-- 4. Find chat input via selector fallback
|-- 5. Paste text via keyboard.insert_text()
|-- 6. Random delay (300-600ms)
|-- 7. Click send button (or fallback to Enter key)
|-- 8. Wait for response completion (detector)
|-- 9. Sleep 1s for DOM to settle
|-- 10. Check for DALL-E images (ChatGPT only)
|-- 11. Extract response text
|       |-- Image response: DOM scraping
|       +-- Text response: copy button click
|-- 12. If read_aloud=True, click More actions -> Read aloud (ChatGPT only)
|       +-- Capture the browser audio response and save to downloads/audio
+-- 13. Return ChatResponse(message, thread_id, elapsed_ms, images, audio)
```

---

## Response Detection

The detector (`detector.py`) uses multiple strategies to know when the model finishes responding:

### Primary: Copy Button

The copy button only appears after the full response is generated. The detector waits for a copy button on the Nth assistant message (where N = expected count).

### Fallback: Stop Button Lifecycle

While streaming, a "Stop generating" button is visible. The detector watches for it to appear then disappear.

### Fallback: Text Stability

If neither button is found, the detector polls the last assistant message text. If it stays the same for 4+ consecutive checks (2s apart), the response is considered complete.

### Message Counting

Counts both `div[data-message-author-role='assistant']` (ChatGPT) and provider-specific elements. Image responses use different selectors than text responses.

---

## Tool Calling

The web UIs don't have native tool-calling APIs. CatGPT implements tool calling via prompt engineering.

### Flow

1. **Tool definitions** from the OpenAI request are converted into a system prompt describing each function's name, description, and parameter schema.

2. The system prompt instructs the model to output tool calls as structured JSON:
   ```json
   {"tool_calls": [{"name": "get_weather", "arguments": {"city": "Paris"}}]}
   ```

3. The prompt includes examples and rules:
   - Output ONLY the JSON code block when calling tools
   - No commentary before or after the JSON
   - When tool results come back, summarize naturally in plain text

4. **JSON extraction** uses a robust brace-depth tracker (not regex) that handles arbitrarily nested objects, arrays, and escaped strings.

5. Parsed tool calls are returned in standard OpenAI format with generated `call_` IDs.

6. **`tool_choice` support**:
   - `"auto"` (default): model decides whether to use tools
   - `"required"`: prompt says "you MUST call at least one tool"
   - `"none"`: tool prompt is not injected at all
   - `{"type":"function","function":{"name":"X"}}`: prompt says "you MUST call function X"

### Provider-Specific Prompts

- **Claude**: Uses collaborative framing ("You have access to external tools through a structured interface"). Avoids patterns that Claude's web UI flags as prompt injection.
- **ChatGPT**: Uses direct instruction ("You are in tool-calling mode").

### Multi-Turn Tool Calls

When tool results come back as `ToolMessage`s, the gateway builds a prompt transcript showing what was called and what was returned. The model sees the results and produces a natural language summary. The prompt explicitly says "Do NOT call tools again for the same request" to prevent loops.

---

## File and Image Upload

```
API Request (with image_url / file content parts)
|
|-- _extract_image_urls(content)           --> list of URLs/data-URLs
|-- _extract_file_attachments(content)     --> list of {filename, data_b64, mime_type}
|
|-- _download_file(url_or_dict)            --> local file path
|   |-- data: URL       --> base64 decode, save to /tmp/catgpt_files/
|   |-- http: URL       --> download via urllib
|   |-- dict             --> base64 decode with original filename
|   +-- local path       --> pass through
|
|-- image_paths + file_paths --> client.send_message(..., image_paths=, file_paths=)
|
+-- client._upload_files(all_paths)
    |-- Find <input type="file"> via selector
    |-- set_input_files(valid_paths)
    +-- Wait 3s + 1s per additional file
```

---

## Echo Detection and Recovery

Sometimes the copy-button extraction grabs the sent prompt instead of the response (race condition). The gateway detects and recovers:

1. Check if `response_text` contains known markers from the injected tool prompt (`"Available functions:"`, `"tool-calling mode"`, etc.)
2. If echo detected, wait 3 seconds and retry `extract_last_response_via_copy()`
3. If retry still echoes, strip the system prompt prefix and extract the tail content

---

## Selector Fallback System

All DOM selectors are defined as lists of fallbacks tried in order:

```python
CHAT_INPUT = [
    "#prompt-textarea",                                    # Primary
    "div[contenteditable='true'][id='prompt-textarea']",   # Specific
    "div[contenteditable='true']",                         # Broad fallback
]
```

When Claude or ChatGPT update their UI, only `selectors.py` needs changes. The `_find_selector()` method tries each selector with a short timeout and returns the first match.

Each provider has its own selectors file:
- `src/selectors.py` (ChatGPT selectors)
- `src/claude/selectors.py` (Claude selectors)

---

## Provider Abstraction

The gateway supports multiple providers through parallel client implementations:

```
src/chatgpt/
|-- client.py       ChatGPTClient(send_message, new_chat, ...)
|-- detector.py     ChatGPT-specific response detection
|-- selectors.py    (uses src/selectors.py)
|-- image_handler.py  DALL-E image detection
|-- audio_handler.py  Read-aloud audio capture/download
+-- models.py

src/claude/
|-- client.py       ClaudeClient(send_message, new_chat, ...)
|-- detector.py     Claude-specific response detection
+-- selectors.py    Claude DOM selectors
```

Both clients expose the same interface. The API layer (`openai_routes.py`) uses `Config.PROVIDER` to select the appropriate client at startup. Provider-specific logic (tool prompts, response parsing, image generation) is handled with simple `if Config.PROVIDER == "claude"` branches.

---

## Docker Stack

```
Docker Container
|
|-- Xvfb (:99)           Virtual framebuffer, Chrome renders here
|-- x11vnc (:5900)       VNC server capturing the Xvfb display
|-- noVNC (:6080)        WebSocket bridge for browser-based VNC access
+-- FastAPI (:8000)      API server
|
+-- All managed by supervisord
```

### Startup Sequence (entrypoint.sh)

1. Create directories
2. Clean stale Chrome lock files
3. Set up VNC password from `VNC_PASSWORD` env var
4. Pre-resolve DNS domains via Python, write to `/etc/hosts`
5. Verify Xvfb and Patchright Chromium
6. Start supervisord (all 4 services)

### Tech Stack

| Component | Library | Purpose |
|---|---|---|
| Browser automation | Patchright | Playwright fork for Chromium control |
| Anti-detection | playwright-stealth | Patch browser fingerprints |
| API framework | FastAPI | OpenAI-compatible + custom REST API |
| ASGI server | Uvicorn | Serve FastAPI app |
| Data validation | Pydantic | Request/response schemas |
| TUI framework | Textual | Terminal chat interface |
| Rich text | Rich | Markdown rendering |
| Config | python-dotenv | Environment variable loading |
| Container | Docker + Compose | Production deployment |
| Display server | Xvfb + x11vnc + noVNC | Virtual display + browser access |
| Process manager | supervisord | Manage container services |
