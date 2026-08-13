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
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.browser.manager import BrowserManager
from src.browser.auto_login import can_prompt_for_login, ensure_logged_in
from src.chatgpt.client import ChatGPTClient
from src.claude.client import ClaudeClient
from src.minimax.client import MiniMaxClient
from src.config import Config
from src.api.ollama_routes import ollama_router
from src.api.routes import router, set_client
from src.api.openai_routes import openai_router, set_openai_client
from src.log import setup_logging

log = setup_logging("api_server", log_file="api_server.log")


class SuppressHealthyHealthzAccessFilter(logging.Filter):
    """Hide noisy successful /healthz access logs while keeping failures visible."""

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        try:
            message = record.getMessage()
        except Exception:
            return True

        if '"GET /healthz' in message and (' 200' in message or ' 204' in message):
            return False
        return True


def _install_uvicorn_access_filters() -> None:
    """Attach access-log filters once at process startup."""
    access_logger = logging.getLogger("uvicorn.access")
    if any(isinstance(f, SuppressHealthyHealthzAccessFilter) for f in access_logger.filters):
        return
    access_logger.addFilter(SuppressHealthyHealthzAccessFilter())


_install_uvicorn_access_filters()

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
        session = {"exists": False}
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
                wait_time = attempt * 5
                log.info(f"Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)

        await _browser.apply_stealth_patches()
        await asyncio.sleep(3)
        session = await _browser.get_session_info()

        if not await _browser.is_logged_in():
            log.info("Not logged in — starting auto-login flow...")
            logged_in = await ensure_logged_in(_browser, has_session=session["exists"])
            if not logged_in:
                if can_prompt_for_login():
                    log.error("Login failed after auto-login attempt")
                    raise RuntimeError(f"Could not log in to {provider_name}")
                log.warning(
                    "Login is still required, but startup is non-interactive. "
                    "API will remain online while the user signs in through noVNC/VNC."
                )
            else:
                session = await _browser.get_session_info()

        if Config.PROVIDER == "claude":
            _client = ClaudeClient(page)
        else:
            _client = ChatGPTClient(page)
    set_client(_client, _browser)
    set_openai_client(_client)

    # ── Startup banner ───────────────────────────────────────────
    W = 60
    sep = "=" * W

    # Session details
    if session["exists"]:
        expires = session.get("expires")
        exp_str = expires.astimezone().strftime("%Y-%m-%d %H:%M %Z") if expires else "no expiry"
        email_str = session.get("email") or "unknown"
        session_line = f"  Account  : {email_str}"
        expiry_line  = f"  Expires  : {exp_str}"
        session_status = "ACTIVE SESSION"
    else:
        session_line = "  Account  : (no session)"
        expiry_line  = "  Expires  : —"
        session_status = "NO SESSION"

    # Runtime flags
    token_masked = ("*" * len(Config.API_TOKEN)) if Config.API_TOKEN else "(disabled)"
    flags = [
        f"  API token    : {token_masked}",
        f"  Headless     : {Config.HEADLESS}",
        f"  Log level    : {Config.LOG_LEVEL}",
        f"  Provider     : {provider_name}",
        f"  Target URL   : {target_url}",
        f"  Resp timeout : {Config.RESPONSE_TIMEOUT} ms",
    ]

    # Endpoints
    host = f"http://{Config.API_HOST}:{Config.API_PORT}"
    endpoints = [
        ("POST", f"{host}/v1/chat/completions", "Chat completions"),
        ("POST", f"{host}/{{app_name}}/v1/chat/completions", "App-scoped chat completions"),
        ("POST", f"{host}/v1/chat/completions/async", "Async chat submit"),
        ("POST", f"{host}/{{app_name}}/v1/chat/completions/async", "App-scoped async submit"),
        ("GET ", f"{host}/v1/chat/completions/async/{{job_id}}", "Async chat status/result"),
        ("GET ", f"{host}/{{app_name}}/v1/chat/completions/async/{{job_id}}", "App-scoped async status/result"),
        ("POST", f"{host}/v1/images/generations", "Image generation"),
        ("POST", f"{host}/{{app_name}}/v1/images/generations", "App-scoped image generation"),
        ("GET ", f"{host}/v1/models", "List models"),
        ("GET ", f"{host}/{{app_name}}/v1/models", "App-scoped models"),
        ("POST", f"{host}/v1/responses", "Responses API"),
        ("POST", f"{host}/{{app_name}}/v1/responses", "App-scoped Responses API"),
        ("POST", f"{host}/api/chat", "Ollama-compatible chat"),
        ("POST", f"{host}/api/generate", "Ollama-compatible generation"),
        ("POST", f"{host}/api/embed", "Ollama-compatible embeddings"),
        ("GET ", f"{host}/api/tags", "Ollama model tags"),
        ("POST", f"{host}/api/show", "Ollama model metadata"),
        ("GET ", f"{host}/api/ps", "Ollama active models"),
        ("POST", f"{host}/chat", "Native chat"),
        ("POST", f"{host}/thread/new", "New thread"),
        ("GET ", f"{host}/threads", "List threads"),
        ("GET ", f"{host}/status", "Status"),
        ("GET ", f"{host}/healthz", "Health check (no auth)"),
        ("GET ", f"{host}/docs", "API docs (no auth)"),
    ]

    lines = [
        sep,
        "  CatGPT — READY".center(W),
        sep,
        f"  {session_status}",
        session_line,
        expiry_line,
        f"  Data dir : {Config.BROWSER_DATA_DIR}",
        "",
        "  RUNTIME FLAGS",
        *flags,
        "",
        "  ENDPOINTS",
        *[f"  {m}  {path:<42}  {desc}" for m, path, desc in endpoints],
        sep,
    ]

    for line in lines:
        log.info(line)


    yield  # Server is running

    if _browser is not None:
        log.info("Shutting down — closing browser...")
        await _browser.close()
        log.info("Browser closed")


app = FastAPI(
    title="CatGPT Gateway API",
    description=(
        "Browser automation API for ChatGPT. "
        "Sends messages via browser and returns responses."
    ),
    version="1.0.0",
    lifespan=lifespan,
    swagger_ui_parameters={"persistAuthorization": True},
)


def _custom_openapi():
    """Inject BearerAuth security scheme so Swagger UI shows the Authorize button."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema.setdefault("components", {})["securitySchemes"] = {
        "BearerAuth": {"type": "http", "scheme": "bearer"}
    }
    schema["security"] = [{"BearerAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi

# ── Bearer Token Auth Middleware ────────────────────────────────
class BearerTokenMiddleware:
    """
    Pure ASGI middleware for Bearer token auth.

    Uses raw ASGI protocol instead of BaseHTTPMiddleware to avoid the
    Python 3.9 event-loop mismatch bug that corrupts asyncio.Lock
    when exceptions propagate through BaseHTTPMiddleware's task group.

    Skips auth for /docs, /openapi.json, and health-check paths.
    """

    OPEN_PATHS = {b"/docs", b"/redoc", b"/openapi.json", b"/healthz"}

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

        path_str = scope.get("path", "")
        if path_str in {"/docs", "/redoc", "/openapi.json", "/healthz"}:
            await self.app(scope, receive, send)
            return

        # Check Authorization header
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        auth_header = headers.get("authorization", "")
        if Config.API_TOKEN_OPTIONAL and not auth_header:
            # Optional-auth mode: no header is allowed.
            await self.app(scope, receive, send)
            return

        if auth_header.startswith("Bearer "):
            provided = auth_header[7:].strip()
        else:
            provided = ""

        expected = token.strip()
        if provided != expected:
            client = scope.get("client")
            client_host = client[0] if isinstance(client, tuple) and client else "unknown"
            log.warning(f"Auth failed from {client_host}: invalid token")
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
app.include_router(ollama_router)


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

