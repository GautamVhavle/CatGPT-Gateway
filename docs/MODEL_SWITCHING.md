# ChatGPT Model Switching

CatGPT supports browser-backed ChatGPT model switching. When a request includes
a configured model id, CatGPT opens the ChatGPT model picker, selects the
matching model and effort, waits for confirmation, and then sends the prompt.

On ChatGPT Business / Pro (August 2026), the composer button shows the current
effort (`Instant`, `Medium`, `High`, `Extra High`, or `Pro`). The compact menu
exposes a Power control; **Show advanced options** reveals:

- **Model:** `GPT-5.6 Sol`, `GPT-5.5` (`o3` is leaving August 26 and is not
  exposed by default)
- **Effort:** `Instant`, `Medium`, `High`, `Extra High`, `Pro`

## Quick Use

OpenAI-compatible request:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer dummy123" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.6-sol-high",
    "messages": [{"role": "user", "content": "Say which mode you are using."}]
  }'
```

Native request:

```bash
curl http://localhost:8000/chat \
  -H "Authorization: Bearer dummy123" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-5.5", "message": "Hello from a selected model."}'
```

List configured model ids:

```bash
curl http://localhost:8000/v1/models -H "Authorization: Bearer dummy123"
```

## Configuration

`CHATGPT_MODEL_ALIASES` maps public API ids to visible ChatGPT **Model** labels:

```env
CHATGPT_MODEL_ALIASES=gpt-5.6-sol=GPT-5.6 Sol|5.6 Sol|Instant,gpt-5.5=GPT-5.5|5.5
```

Format:

```text
public_id=Primary UI Label|Alternate UI Label|Another Alternate
```

`CHATGPT_MODEL_SETTINGS` maps those ids to the **Effort** submenu:

```env
CHATGPT_MODEL_SETTINGS=gpt-5.6-sol=Instant,gpt-5.6-sol-medium=Medium,gpt-5.6-sol-high=High,gpt-5.6-sol-extra-high=Extra High,gpt-5.6-sol-pro=Pro,gpt-5.5=Instant,gpt-5.5-medium=Medium,gpt-5.5-high=High,gpt-5.5-thinking=High,gpt-5.5-extra-high=Extra High,gpt-5.5-pro=Pro
```

`gpt-5.5-thinking` remains as a compatibility alias for GPT-5.5 + High.

If a request uses `catgpt-browser`, CatGPT keeps the current browser-selected
model unless `CHATGPT_DEFAULT_MODEL` is set:

```env
CHATGPT_DEFAULT_MODEL=gpt-5.6-sol-high
```

By default, if a configured model is not visible in the account's picker, CatGPT
logs the visible options and continues with the currently selected browser
model. To fail the request instead:

```env
CHATGPT_MODEL_SWITCH_STRICT=true
```

`CHATGPT_MODEL_SWITCH_TIMEOUT` is measured in milliseconds and controls how long
CatGPT waits for the selected model label to appear after clicking an option.
For example:

```env
CHATGPT_MODEL_SWITCH_TIMEOUT=10000  # 10 seconds
```

Do not set `CHATGPT_MODEL_SWITCH_TIMEOUT=1` expecting one second; that is 1 ms.

## Notes

- Model availability depends on the logged-in ChatGPT account and plan. Free/Go
  accounts may show Luna and a Think control instead of this Advanced picker.
- CatGPT expands **Show advanced options** when the compact Power menu is open,
  then clicks **Model** and **Effort**.
- UI labels change over time. Update `CHATGPT_MODEL_ALIASES` when ChatGPT
  renames picker items.
- The CLI also supports `/model <name>`, which changes the model id sent to the
  API for later messages.
- Issue #8 is implemented by `src/chatgpt/model_registry.py` and
  `ChatGPTClient.ensure_model()`.
