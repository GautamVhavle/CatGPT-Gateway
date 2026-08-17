# Contributing to CatGPT Gateway

Thanks for your interest in contributing! This project is open source and we welcome all kinds of contributions: bug fixes, new features, documentation improvements, and new provider integrations.

---

## Getting Started

1. **Fork** the repo on GitHub
2. **Clone** your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/CatGPT-Gateway.git
   cd CatGPT-Gateway
   ```
3. **Set up** the development environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   patchright install chromium
   cp .env.example .env
   ```
4. **Create a branch** for your changes:
   ```bash
   git checkout -b feature/your-feature-name
   ```

---

## Development Workflow

### Running Locally

```bash
# Start the API server
python -m src.api.server

# Run tests
python scripts/test_phase1.py
python scripts/test_langchain_tools.py
```

### Testing

Before submitting a PR, run the relevant test scripts to make sure nothing is broken:

```bash
python scripts/test_phase1.py           # Basic pipeline
python scripts/test_multi_turn.py       # Multi-turn conversations
python scripts/test_robust.py           # Edge cases
python scripts/test_images.py           # Image detection
python scripts/test_langchain_tools.py  # LangChain + tool calling (needs server running)
```

All test scripts auto-detect the provider from your `.env` file.

---

## What to Contribute

### Broken Selectors

ChatGPT and Claude update their web UIs frequently. When selectors break, they need updating.

- **ChatGPT selectors**: `src/selectors.py`
- **Claude selectors**: `src/claude/selectors.py`

Each selector is a list of CSS selectors tried in order. Add new selectors at the top and keep old ones as fallbacks.

### New Providers

Want to add support for Gemini, Copilot, or another web-based AI? Follow the pattern in `src/claude/`:

1. Create a new directory: `src/your_provider/`
2. Implement `client.py` with `send_message()`, `new_chat()`, and file upload
3. Implement `detector.py` for response completion detection
4. Implement `selectors.py` with the provider's DOM selectors
5. Add the provider option to `src/config.py`
6. Add provider handling to `src/api/openai_routes.py`
7. Add provider handling to `src/api/server.py`

### Bug Fixes

If you find a bug, please open an issue first describing the problem. If you have a fix, feel free to submit a PR directly.

### Documentation

Docs live in `docs/` and the root `README.md`. Improvements, corrections, and additional examples are always welcome.

---

## Code Style

- Python 3.9+ compatible
- Use type hints where reasonable
- Keep functions focused and small
- Follow existing patterns in the codebase
- No em dashes in documentation

---

## Pull Request Guidelines

1. **One feature per PR.** Keep changes focused.
2. **Describe what changed** in the PR description.
3. **Test your changes** with at least `test_phase1.py` and the relevant test scripts.
4. **Don't commit sensitive data.** No `.env` files, no `browser_data/`, no cookies, no API keys.
5. **Don't break existing functionality.** Run the test suite on at least one provider.

---

## Reporting Issues

When opening an issue, please include:

- Your OS and Python version
- Provider (Claude or ChatGPT)
- Whether you're using Docker or running locally
- The error message or unexpected behavior
- Steps to reproduce

---

## Project Structure

Quick reference for where to find things:

| What | Where |
|---|---|
| API endpoints | `src/api/openai_routes.py`, `src/api/routes.py` |
| OpenAI schemas | `src/api/openai_schemas.py` |
| ChatGPT client | `src/chatgpt/client.py` |
| Claude client | `src/claude/client.py` |
| DOM selectors | `src/selectors.py`, `src/claude/selectors.py` |
| Browser management | `src/browser/manager.py` |
| Configuration | `src/config.py` |
| Docker setup | `docker/10-catgpt-init.sh`, `docker/startapp.sh`, `docker/Dockerfile` |
| Tests | `scripts/test_*.py` |
| Documentation | `docs/` |

---

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
