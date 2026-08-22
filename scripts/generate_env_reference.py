#!/usr/bin/env python3
"""Generate the environment-variable reference and detect undocumented settings."""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "ENVIRONMENT.md"
COMPOSE = ROOT / "docker-compose.yml"

# name: (default outside Compose, purpose)
SECTIONS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    ("Provider and browser", (
        ("PROVIDER", "chatgpt", "Active provider: `chatgpt`, `claude`, or `minimax`."),
        ("CHATGPT_URL", "https://chatgpt.com", "ChatGPT browser target."),
        ("CLAUDE_URL", "https://claude.ai", "Claude browser target."),
        ("MINIMAX_REGION", "global_en", "MiniMax region: `global_en` or `cn_zh`."),
        ("MINIMAX_BASE_URL", "derived from region", "Optional MiniMax API base URL override."),
        ("MINIMAX_API_KEY", "empty", "MiniMax API key; required when MiniMax is selected."),
        ("MINIMAX_MODEL", "MiniMax-M2.7", "MiniMax model exposed by the gateway."),
        ("HEADLESS", "false", "Run the automated browser without a visible window."),
        ("BROWSER_CHANNEL", "chrome", "Playwright browser channel."),
        ("BROWSER_DATA_DIR", "browser_data", "Persistent browser-profile directory."),
        ("AUTO_LOGIN_INTERACTIVE", "auto", "Force (`true`) or disable (`false`) terminal login prompts; `auto` follows TTY availability."),
        ("SLOW_MO", "25", "Delay in milliseconds after Playwright operations."),
        ("MAX_CONCURRENT_REQUESTS", "3", "Maximum requests processed concurrently."),
        ("MAX_ACTIVE_TABS", "4", "Maximum pooled browser tabs; each session remains serialized."),
        ("DISPLAY_WIDTH", "1280", "Virtual-display width; the Compose profile uses 1366."),
        ("DISPLAY_HEIGHT", "720", "Virtual-display height; the Compose profile uses 768."),
        ("DISPLAY", "system managed", "X display address; `:99` is also used to detect the container runtime."),
    )),
    ("ChatGPT models and prompts", (
        ("CHATGPT_DEFAULT_MODEL", "empty", "Model selected when a request does not specify one."),
        ("CHATGPT_MODEL_ALIASES", "built-in alias map", "Comma-separated API-model to UI-label mappings."),
        ("CHATGPT_MODEL_SETTINGS", "built-in settings map", "Comma-separated API-model to reasoning-setting mappings."),
        ("CHATGPT_MODEL_SWITCH_TIMEOUT", "10000", "Model-switch timeout in milliseconds."),
        ("CHATGPT_MODEL_SWITCH_STRICT", "false", "Fail instead of continuing when model switching cannot be verified."),
        ("CHATGPT_LONG_PROMPT_FALLBACK", "attachment", "Long-prompt behavior: `attachment` or `error` (HTTP 413)."),
        ("CHATGPT_LONG_PROMPT_THRESHOLD", "0", "Character threshold for proactive attachment fallback; `0` waits for composer rejection."),
    )),
    ("Attachments and output", (
        ("ATTACHMENT_EXPAND_MULTIPAGE", "true", "Render multi-page document attachments into page images."),
        ("ATTACHMENT_MAX_PAGES", "24", "Maximum pages rendered from one attachment."),
        ("ATTACHMENT_RENDER_DPI", "144", "DPI used when rendering attachment pages."),
        ("LOG_DIR", "logs", "Log output directory."),
        ("IMAGES_DIR", "downloads/images", "Generated-image download directory."),
        ("AUDIO_DIR", "downloads/audio", "Generated-audio download directory."),
    )),
    ("API, routing, and security", (
        ("API_HOST", "0.0.0.0", "API listen address."),
        ("API_PORT", "8000", "API listen port."),
        ("API_TOKEN", "empty", "Bearer token; an empty value disables token authentication."),
        ("API_TOKEN_OPTIONAL", "false", "Allow unauthenticated requests even when a token is configured."),
        ("API_THREAD_CONTRACT_MODE", "false", "Cache large system instructions per thread and send compact reminders."),
        ("API_THREAD_CONTRACT_TTL_SECONDS", "3600", "Thread-contract cache lifetime."),
        ("API_APP_THREAD_MODE", "false", "Map `request.user` values to dedicated provider threads."),
        ("API_APP_THREAD_TTL_SECONDS", "86400", "App-thread mapping lifetime."),
        ("API_APP_THREAD_DELETE_EXPIRED", "false", "Delete expired app-thread conversations from the browser UI."),
        ("API_HEADER_ROW_MERGE_MODE", "false", "Merge header-only structured rows into the following item."),
        ("RATE_LIMIT_SECONDS", "5", "Minimum interval used by the gateway rate limiter."),
    )),
    ("Timing and logging", (
        ("RESPONSE_TIMEOUT", "120000", "Response timeout in milliseconds."),
        ("SELECTOR_TIMEOUT", "10000", "Browser-selector timeout in milliseconds."),
        ("TYPING_SPEED_MIN", "50", "Minimum simulated typing delay in milliseconds."),
        ("TYPING_SPEED_MAX", "150", "Maximum simulated typing delay in milliseconds."),
        ("THINKING_PAUSE_MIN", "500", "Minimum pre-submit pause in milliseconds."),
        ("THINKING_PAUSE_MAX", "1500", "Maximum pre-submit pause in milliseconds."),
        ("POLL_INTERVAL_MS", "300", "Completion polling interval in milliseconds."),
        ("LOG_LEVEL", "DEBUG", "Python logging level."),
        ("VERBOSE", "true", "Enable verbose diagnostic output."),
    )),
    ("Ollama compatibility", (
        ("OLLAMA_EMBEDDING_MODELS", "nomic-embed-text", "Comma-separated model IDs treated as embedding models."),
        ("OLLAMA_EMBEDDING_DIMENSIONS", "768", "Embedding vector size returned by the compatibility endpoint."),
        ("OLLAMA_ACTIVE_MODEL_TTL_SECONDS", "900", "How long active models remain listed."),
    )),
    ("Terminal client", (
        ("CATGPT_API_URL", "http://localhost:8000/v1", "OpenAI-compatible base URL used by the terminal client."),
        ("OPENAI_API_BASE", "empty", "Fallback API base URL used by the terminal client."),
        ("CATGPT_API_KEY", "OPENAI_API_KEY or dummy123", "Terminal-client token; also the Compose input for `API_TOKEN`."),
        ("OPENAI_API_KEY", "empty", "Fallback terminal-client token."),
        ("CATGPT_MODEL", "catgpt-browser", "Default terminal-client model."),
    )),
    ("Container GUI", (
        ("APP_NAME", "CatGPT", "Name displayed by the container GUI."),
        ("SECURE_CONNECTION", "0", "Enable HTTPS in the jlesage web GUI."),
        ("KEEP_APP_RUNNING", "1", "Keep the GUI container alive when the app exits."),
        ("USER_ID", "1000", "Container process user ID."),
        ("GROUP_ID", "1000", "Container process group ID."),
        ("VNC_PASSWORD", "catgpt", "Web GUI/VNC password."),
    )),
)

COMPOSE_INPUTS: tuple[tuple[str, str, str], ...] = (
    ("DOCKERDIR", ".", "Host directory under which persistent `appdata/catgpt` volumes are created."),
    ("CATGPT_USER_ID", "1000", "Host user ID mapped to container `USER_ID`."),
    ("CATGPT_GROUP_ID", "1000", "Host group ID mapped to container `GROUP_ID`."),
    ("CATGPT_API_KEY", "dummy123", "Value passed to container `API_TOKEN`."),
    ("CATGPT_VNC_PASSWORD", "catgpt", "Value passed to container `VNC_PASSWORD`."),
    ("PROVIDER", "chatgpt", "Provider passed through to the container."),
    ("MINIMAX_REGION", "global_en", "MiniMax region passed through to the container."),
    ("MINIMAX_BASE_URL", "empty", "Optional MiniMax base URL passed through to the container."),
    ("MINIMAX_API_KEY", "empty", "MiniMax API key passed through to the container."),
    ("MINIMAX_MODEL", "MiniMax-M2.7", "MiniMax model passed through to the container."),
    ("MAX_CONCURRENT_REQUESTS", "3", "Concurrency limit passed through to the container."),
    ("MAX_ACTIVE_TABS", "4", "Browser-tab limit passed through to the container."),
)

DYNAMIC_SOURCE_VARIABLES = {"DISPLAY_WIDTH", "DISPLAY_HEIGHT"}
COMPOSE_SUBSTITUTION_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::-([^}]*))?\}")
COMPOSE_KEY_RE = re.compile(r"^\s{6}([A-Z][A-Z0-9_]*):", re.MULTILINE)


def _python_environment_variables() -> set[str]:
    """Find literal os.getenv/os.environ.get reads in production Python code."""
    found: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            is_getenv = (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            )
            is_environ_get = (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "os"
            )
            if (is_getenv or is_environ_get) and isinstance(node.args[0], ast.Constant):
                name = node.args[0].value
                if isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                    found.add(name)
    return found


def discovered_variables() -> set[str]:
    compose = COMPOSE.read_text(encoding="utf-8")
    substitutions = {match.group(1) for match in COMPOSE_SUBSTITUTION_RE.finditer(compose)}
    container_keys = set(COMPOSE_KEY_RE.findall(compose))
    return _python_environment_variables() | DYNAMIC_SOURCE_VARIABLES | substitutions | container_keys


def catalogued_variables() -> set[str]:
    runtime = {name for _, rows in SECTIONS for name, _, _ in rows}
    return runtime | {name for name, _, _ in COMPOSE_INPUTS}


def validate_catalog() -> None:
    discovered = discovered_variables()
    catalogued = catalogued_variables()
    missing = sorted(discovered - catalogued)
    stale = sorted(catalogued - discovered)
    problems: list[str] = []
    if missing:
        problems.append("undocumented: " + ", ".join(missing))
    if stale:
        problems.append("not found in source or Compose: " + ", ".join(stale))
    if problems:
        raise ValueError("Environment-variable catalog drift: " + "; ".join(problems))


def _cell(value: str) -> str:
    if value == "empty":
        return "_empty_"
    return f"`{value.replace('|', '&#124;')}`"


def _table(rows: tuple[tuple[str, str, str], ...]) -> list[str]:
    output = ["| Variable | Default | Purpose |", "|---|---:|---|"]
    output.extend(f"| `{name}` | {_cell(default)} | {purpose} |" for name, default, purpose in rows)
    return output


def render() -> str:
    validate_catalog()
    lines = [
        "<!-- Generated by scripts/generate_env_reference.py; do not edit directly. -->",
        "# Environment Variable Reference",
        "",
        "This file is generated from a maintained catalog and checked against every environment-variable read in `src/` plus every variable declared or interpolated by `docker-compose.yml`.",
        "",
        "Regenerate it after adding or changing a setting:",
        "",
        "```bash",
        "python scripts/generate_env_reference.py",
        "python scripts/generate_env_reference.py --check",
        "```",
        "",
        "## Docker Compose `.env` inputs",
        "",
        "Compose reads a root `.env` file for `${...}` substitution. Start with only the values you want to change:",
        "",
        "```dotenv",
        "CATGPT_API_KEY=replace-me",
        "CATGPT_VNC_PASSWORD=replace-me",
        "CATGPT_USER_ID=1000",
        "CATGPT_GROUP_ID=1000",
        "```",
        "",
        "These are the variables consumed directly by `docker-compose.yml`:",
        "",
        *_table(COMPOSE_INPUTS),
        "",
        "> [!IMPORTANT]",
        "> A variable in `.env` is not automatically available inside the container. For a runtime variable not listed above, add it under `services.catgpt.environment` in `docker-compose.yml` (or a Compose override).",
        "",
        "## Direct and container runtime variables",
        "",
        "When running Python directly, set these in the shell or root `.env`. In Docker, pass overrides through the service's `environment` section.",
    ]
    for title, rows in SECTIONS:
        lines.extend(("", f"### {title}", "", *_table(rows)))
    lines.extend(("", "## Boolean values", "", "Boolean settings are enabled only by the case-insensitive value `true`, except `AUTO_LOGIN_INTERACTIVE`, which also accepts `1`, `yes`, and `on` (and their false equivalents).", ""))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the generated file is stale")
    args = parser.parse_args()
    content = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != content:
            print(f"{OUTPUT.relative_to(ROOT)} is stale; run {Path(__file__).name}")
            return 1
        print(f"{OUTPUT.relative_to(ROOT)} is up to date")
        return 0
    OUTPUT.write_text(content, encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
