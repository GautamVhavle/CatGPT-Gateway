"""
Network Recorder — captures and logs network requests for Phase 1 observation.

Records API calls ChatGPT makes so we understand the request patterns.
"""

from __future__ import annotations

from patchright.async_api import Page, Request, Response

from src.log import setup_logging

log = setup_logging("network")


class NetworkRecorder:
    """Record network activity on a Playwright page."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self._requests: list[dict] = []
        self._active = False

    def start(self) -> None:
        """Start recording network requests and responses."""
        if self._active:
            return
        self._page.on("request", self._on_request)
        self._page.on("response", self._on_response)
        self._active = True
        log.info("Network recorder started")

    def stop(self) -> None:
        """Stop recording (note: Playwright doesn't support removing listeners easily)."""
        self._active = False
        log.info(f"Network recorder stopped — captured {len(self._requests)} requests")

    def _on_request(self, request: Request) -> None:
        if not self._active:
            return
        url = request.url
        # Only log interesting API calls, skip static assets
        if any(k in url for k in ["backend-api", "conversation", "auth", "sentinel"]):
            entry = {
                "method": request.method,
                "url": url,
                "type": request.resource_type,
            }
            self._requests.append(entry)
            log.debug(f"REQ  {request.method} {url[:120]}")

    def _on_response(self, response: Response) -> None:
        if not self._active:
            return
        url = response.url
        if any(k in url for k in ["backend-api", "conversation", "auth", "sentinel"]):
            log.debug(f"RESP {response.status} {url[:120]}")

    def get_captured(self) -> list[dict]:
        """Return all captured request entries."""
        return list(self._requests)

    def clear(self) -> None:
        """Clear captured requests."""
        self._requests.clear()
