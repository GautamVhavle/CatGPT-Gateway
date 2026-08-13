"""
FastAPI server — serves ChatGPT as an API.

Launches the browser on startup, shuts it down on exit.

Usage:
    python -m src.api.server
    # or
    uvicorn src.api.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.browser.manager import BrowserManager
from src.browser.auto_login import ensure_logged_in
from src.chatgpt.client import ChatGPTClient
from src.claude.client import ClaudeClient
from src.minimax.client import MiniMaxClient
from src.config import Config
from src.api.routes import router, set_client
from src.api.openai_routes import openai_router, set_openai_client
from src.log import setup_logging

log = setup_logging("api_server")

# Global instances — needed for lifespan
_browser: BrowserManager | None = None
_client: ChatGPTClient | ClaudeClient | MiniMaxClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the active provider and release its resources on shutdown."""
    global _browser, _client

    Config.validate_provider()
    target_url = Config.provider_url()
    provider_name = Config.provider_name()
    log.info(f"Provider: {provider_name} ({target_url})")

    if Config.PROVIDER == "minimax":
        _browser = None
        _client = MiniMaxClient()
    else:
        log.info("Starting browser for API server...")
        _browser = BrowserManager()
        page = await _browser.start()

        # Navigate with retries (DNS can be slow in Docker)
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            try:
                log.info(f"Navigation attempt {attempt}/{max_retries} to {target_url}")
                await _browser.navigate(target_url)
                break
            except Exception as e:
                log.warning(f"Navigation attempt {attempt} failed: {e}")
                if attempt == max_retries:
                    log.error("All navigation attempts failed")
                    raise
                wait_time = attempt * 5  # 5s, 10s, 15s, 20s
                log.info(f"Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)

        # Apply stealth patches AFTER the first navigation.
        # In Docker, applying stealth init scripts before navigation
        # causes Chrome's DNS resolver to fail (ERR_NAME_NOT_RESOLVED).
        await _browser.apply_stealth_patches()

        await asyncio.sleep(3)

        if not await _browser.is_logged_in():
            log.info("Not logged in — starting auto-login flow...")
            logged_in = await ensure_logged_in(_browser)
            if not logged_in:
                log.error("Login failed after auto-login attempt")
                raise RuntimeError(f"Could not log in to {provider_name}")

        if Config.PROVIDER == "claude":
            _client = ClaudeClient(page)
        else:
            _client = ChatGPTClient(page)

    set_client(_client, _browser)
    set_openai_client(_client, _browser)
    log.info(f"API server ready for {provider_name}")

    yield  # Server is running

    if _browser is not None:
        log.info("Shutting down — closing browser...")
        await _browser.close()
        log.info("Browser closed")


app = FastAPI(
    title="CatGPT Gateway API",
    description="Provider gateway with OpenAI-compatible routes.",
    version="1.0.0",
    lifespan=lifespan,
)

# ── Bearer Token Auth Middleware ────────────────────────────────
class BearerTokenMiddleware:
    """
    Pure ASGI middleware for Bearer token auth.

    Uses raw ASGI protocol instead of BaseHTTPMiddleware to avoid the
    Python 3.9 event-loop mismatch bug that corrupts asyncio.Lock
    when exceptions propagate through BaseHTTPMiddleware's task group.

    Skips auth for /docs, /openapi.json, and health-check paths.
    """

    OPEN_PATHS = {b"/docs", b"/redoc", b"/openapi.json", b"/healthz", b"/preview", b"/v1/preview", b"/v1/screenshot.png"}

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token = Config.API_TOKEN
        if not token:
            await self.app(scope, receive, send)
            return

        path_str = (scope.get("path") or "").rstrip("/")
        if (
            path_str in {"", "/docs", "/redoc", "/openapi.json", "/healthz", "/preview", "/v1/preview", "/v1/screenshot.png", "/v1/tabs", "/favicon.ico"}
            or path_str.startswith("/preview")
            or path_str.startswith("/v1/screenshot")
            or path_str.startswith("/v1/tabs")
        ):
            await self.app(scope, receive, send)
            return

        # Extract Authorization or x-api-key header
        headers = dict(scope.get("headers", []))
        auth_value = headers.get(b"authorization", b"").decode()

        provided = ""
        if auth_value.startswith("Bearer "):
            provided = auth_value[7:]
        elif b"x-api-key" in headers:
            provided = headers[b"x-api-key"].decode()
        elif b"anthropic-api-key" in headers:
            provided = headers[b"anthropic-api-key"].decode()

        if provided != token:
            response = JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Invalid or missing API token. Set Authorization: Bearer <API_TOKEN>",
                        "type": "auth_error",
                    }
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


app.add_middleware(BearerTokenMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(openai_router)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Unauthenticated health-check for Docker / load-balancers."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.api.server:app",
        host=Config.API_HOST,
        port=Config.API_PORT,
        log_level="info",
    )
