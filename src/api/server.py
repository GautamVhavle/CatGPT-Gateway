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
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.browser.manager import BrowserManager
from src.browser.auto_login import ensure_logged_in
from src.chatgpt.client import ChatGPTClient
from src.config import Config
from src.api.routes import router, set_client
from src.api.openai_routes import openai_router, set_openai_client
from src.log import setup_logging

log = setup_logging("api_server", log_file="api_server.log")

# Global instances — needed for lifespan
_browser: BrowserManager | None = None
_client: ChatGPTClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: launch browser. Shutdown: close it."""
    global _browser, _client

    log.info("Starting browser for API server...")
    _browser = BrowserManager()
    page = await _browser.start()

    # Navigate with retries (DNS can be slow in Docker)
    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            log.info(f"Navigation attempt {attempt}/{max_retries} to {Config.CHATGPT_URL}")
            await _browser.navigate(Config.CHATGPT_URL)
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
            raise RuntimeError("Could not log in to ChatGPT")

    _client = ChatGPTClient(page)
    set_client(_client, _browser)
    set_openai_client(_client)
    log.info("API server ready — browser launched, logged in")

    yield  # Server is running

    log.info("Shutting down — closing browser...")
    await _browser.close()
    log.info("Browser closed")


app = FastAPI(
    title="ChatGPT API",
    description=(
        "Browser automation API for ChatGPT. "
        "Sends messages via browser and returns responses."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# ── Bearer Token Auth Middleware ────────────────────────────────
class BearerTokenMiddleware(BaseHTTPMiddleware):
    """
    Require a Bearer token on all API requests when API_TOKEN is set.
    Skips auth for /docs, /openapi.json, and health-check paths.
    """

    OPEN_PATHS = {"/docs", "/redoc", "/openapi.json", "/healthz"}

    async def dispatch(self, request: Request, call_next):
        token = Config.API_TOKEN
        if not token:
            # No token configured — auth disabled
            return await call_next(request)

        path = request.url.path
        if path in self.OPEN_PATHS:
            return await call_next(request)

        # Check Authorization header
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:]
        else:
            provided = ""

        if provided != token:
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Invalid or missing API token. Set Authorization: Bearer <API_TOKEN>", "type": "auth_error"}},
            )

        return await call_next(request)


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
