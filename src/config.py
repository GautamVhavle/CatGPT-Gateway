"""
Centralized configuration — loads from .env with sensible defaults.
"""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

_CODE_ROOT = Path(__file__).resolve().parent.parent
_CWD = Path.cwd()

# Prefer the invocation directory as project root when running from
# a checkout (e.g. `nix run .#proxy` from repo root). Fall back to the
# code location (used for packaged/store execution).
if (_CWD / "src").exists() and (_CWD / "scripts").exists():
    _PROJECT_ROOT = _CWD
else:
    _PROJECT_ROOT = _CODE_ROOT

# Load .env from current working directory first, then from the
# resolved project root.
load_dotenv(_CWD / ".env")
load_dotenv(_PROJECT_ROOT / ".env")


class Config:
    """All project settings in one place."""

    SUPPORTED_PROVIDERS: tuple[str, ...] = ("chatgpt", "claude", "minimax")

    # Paths
    PROJECT_ROOT: Path = _PROJECT_ROOT
    BROWSER_DATA_DIR: Path = _PROJECT_ROOT / os.getenv("BROWSER_DATA_DIR", "browser_data")
    LOG_DIR: Path = _PROJECT_ROOT / os.getenv("LOG_DIR", "logs")
    IMAGES_DIR: Path = _PROJECT_ROOT / os.getenv("IMAGES_DIR", "downloads/images")

    # Browser & Concurrency
    HEADLESS: bool = os.getenv("HEADLESS", "false").lower() == "true"
    SLOW_MO: int = int(os.getenv("SLOW_MO", "25"))
    MAX_CONCURRENT_REQUESTS: int = int(os.getenv("MAX_CONCURRENT_REQUESTS", "3"))
    CHATGPT_URL: str = os.getenv("CHATGPT_URL", "https://chatgpt.com")
    CLAUDE_URL: str = os.getenv("CLAUDE_URL", "https://claude.ai")
    MINIMAX_BASE_URLS: dict[str, str] = {
        "global_en": "https://api.minimax.io/v1",
        "cn_zh": "https://api.minimaxi.com/v1",
    }
    MINIMAX_REGION: str = os.getenv("MINIMAX_REGION", "global_en").lower()
    MINIMAX_BASE_URL: str = (
        os.getenv("MINIMAX_BASE_URL", "").strip()
        or MINIMAX_BASE_URLS.get(MINIMAX_REGION, MINIMAX_BASE_URLS["global_en"])
    ).rstrip("/")
    MINIMAX_API_KEY: str = os.getenv("MINIMAX_API_KEY", "").strip()
    MINIMAX_MODEL_IDS: tuple[str, ...] = ("MiniMax-M2.7",)
    MINIMAX_MODEL: str = os.getenv(
        "MINIMAX_MODEL", MINIMAX_MODEL_IDS[0]
    ).strip()

    # Provider selection
    PROVIDER: str = os.getenv("PROVIDER", "chatgpt").lower()

    @classmethod
    def provider_url(cls) -> str:
        """Return the target URL for the active provider."""
        if cls.PROVIDER == "claude":
            return cls.CLAUDE_URL
        if cls.PROVIDER == "minimax":
            return cls.MINIMAX_BASE_URL
        return cls.CHATGPT_URL

    @classmethod
    def provider_name(cls) -> str:
        """Return the display name for the active provider."""
        names = {
            "chatgpt": "ChatGPT",
            "claude": "Claude",
            "minimax": "MiniMax",
        }
        return names.get(cls.PROVIDER, cls.PROVIDER)

    @classmethod
    def validate_provider(cls) -> None:
        """Validate provider-specific configuration before startup."""
        if cls.PROVIDER not in cls.SUPPORTED_PROVIDERS:
            supported = ", ".join(cls.SUPPORTED_PROVIDERS)
            raise ValueError(
                f"Unsupported provider '{cls.PROVIDER}'. Choose one of: {supported}"
            )
        if cls.PROVIDER == "minimax":
            base_url_override = os.getenv("MINIMAX_BASE_URL", "").strip()
            if (
                cls.MINIMAX_REGION not in cls.MINIMAX_BASE_URLS
                and not base_url_override
            ):
                supported = ", ".join(cls.MINIMAX_BASE_URLS)
                raise ValueError(
                    f"Unsupported MiniMax region '{cls.MINIMAX_REGION}'. "
                    f"Choose one of: {supported}"
                )
            if cls.MINIMAX_MODEL not in cls.MINIMAX_MODEL_IDS:
                supported = ", ".join(cls.MINIMAX_MODEL_IDS)
                raise ValueError(
                    f"Unsupported MiniMax model '{cls.MINIMAX_MODEL}'. "
                    f"Choose one of: {supported}"
                )

    @classmethod
    def uses_browser(cls) -> bool:
        """Return whether the active provider requires browser automation."""
        return cls.PROVIDER != "minimax"

    @classmethod
    def provider_model_ids(cls) -> tuple[str, ...]:
        """Return model IDs exposed by the active provider."""
        if cls.PROVIDER == "claude":
            return ("claude-browser",)
        if cls.PROVIDER == "minimax":
            return cls.MINIMAX_MODEL_IDS
        return ("catgpt-browser",)

    @classmethod
    def default_model_id(cls) -> str:
        """Return the default model ID for the active provider."""
        if cls.PROVIDER == "minimax":
            return cls.MINIMAX_MODEL
        return cls.provider_model_ids()[0]

    @classmethod
    def resolve_model_id(cls, requested: str | None) -> str:
        """Resolve request defaults and validate MiniMax model IDs."""
        if cls.PROVIDER != "minimax":
            return requested or cls.default_model_id()

        if not requested or requested in {"catgpt-browser", "claude-browser"}:
            return cls.MINIMAX_MODEL
        if requested not in cls.MINIMAX_MODEL_IDS:
            supported = ", ".join(cls.MINIMAX_MODEL_IDS)
            raise ValueError(
                f"Unsupported MiniMax model '{requested}'. Choose one of: {supported}"
            )
        return requested

    @classmethod
    def provider_owner(cls) -> str:
        """Return the owner label used by the models endpoint."""
        owners = {
            "chatgpt": "catgpt",
            "claude": "anthropic",
            "minimax": "minimax",
        }
        return owners.get(cls.PROVIDER, cls.PROVIDER)

    @classmethod
    def supports_image_generation(cls) -> bool:
        """Return whether the active provider supports the image route."""
        return cls.PROVIDER == "chatgpt"

    # Timeouts (ms)
    # RESPONSE_TIMEOUT: how long to wait for ChatGPT to finish generating.
    # Default is 20 minutes (1 200 000 ms) because deep-thinking / reasoning
    # models (o1, o3, o3-mini) can take 10-20 minutes for complex tasks.
    # Override via RESPONSE_TIMEOUT env var if you need a shorter limit.
    RESPONSE_TIMEOUT: int = int(os.getenv("RESPONSE_TIMEOUT", "1200000"))
    SELECTOR_TIMEOUT: int = int(os.getenv("SELECTOR_TIMEOUT", "10000"))

    # Maximum number of active browser tabs held open for persistent sessions.
    # When this limit is reached the least-recently-used idle tab is closed
    # (its conversation URL is preserved and the tab is reopened transparently
    # on the next request for that session).
    # Increase for more parallelism at the cost of more RAM.
    MAX_ACTIVE_TABS: int = int(os.getenv("MAX_ACTIVE_TABS", "4"))

    # Human simulation (ms)
    TYPING_SPEED_MIN: int = int(os.getenv("TYPING_SPEED_MIN", "50"))
    TYPING_SPEED_MAX: int = int(os.getenv("TYPING_SPEED_MAX", "150"))
    THINKING_PAUSE_MIN: int = int(os.getenv("THINKING_PAUSE_MIN", "500"))
    THINKING_PAUSE_MAX: int = int(os.getenv("THINKING_PAUSE_MAX", "1500"))
    # Completion poll interval — how often to check if response is ready (ms)
    POLL_INTERVAL_MS: int = int(os.getenv("POLL_INTERVAL_MS", "300"))

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "DEBUG")
    VERBOSE: bool = os.getenv("VERBOSE", "true").lower() == "true"

    # API (Phase 3)
    API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))
    RATE_LIMIT_SECONDS: int = int(os.getenv("RATE_LIMIT_SECONDS", "5"))
    API_TOKEN: str = os.getenv("API_TOKEN", "")  # Bearer token for API auth (empty = no auth)

    # VNC
    VNC_PASSWORD: str = os.getenv("VNC_PASSWORD", "catgpt")

    # Viewport base (will be jittered ±20px)
    VIEWPORT_WIDTH: int = 1280
    VIEWPORT_HEIGHT: int = 720

    @classmethod
    def ensure_dirs(cls) -> None:
        """Create required directories if they don't exist."""
        cls.BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        cls.LOG_DIR.mkdir(parents=True, exist_ok=True)
        cls.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
