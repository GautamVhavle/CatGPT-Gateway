"""
DOM Observer — watches DOM changes in real-time for debugging / Phase 1 observation.

Injects a MutationObserver to track what changes when ChatGPT responds.
"""

from __future__ import annotations

from patchright.async_api import Page

from src.log import setup_logging

log = setup_logging("dom_observer")


class DOMObserver:
    """Observe and log DOM mutations on the ChatGPT page."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self._active = False

    async def start(self, target_selector: str = "main") -> None:
        """
        Inject a MutationObserver that logs DOM changes to the console.
        We capture those via page console listener.
        """
        if self._active:
            return

        self._page.on("console", self._on_console)

        await self._page.evaluate(f"""
        (() => {{
            if (window.__domObserver) window.__domObserver.disconnect();
            const target = document.querySelector('{target_selector}');
            if (!target) {{ console.log('[DOM_OBS] Target not found: {target_selector}'); return; }}

            const observer = new MutationObserver((mutations) => {{
                for (const m of mutations) {{
                    if (m.type === 'childList' && m.addedNodes.length > 0) {{
                        for (const node of m.addedNodes) {{
                            if (node.nodeType === 1) {{
                                const tag = node.tagName || 'unknown';
                                const cls = node.className || '';
                                const text = (node.textContent || '').slice(0, 100);
                                console.log('[DOM_OBS] ADDED ' + tag + '.' + cls + ' | ' + text);
                            }}
                        }}
                    }}
                }}
            }});

            observer.observe(target, {{ childList: true, subtree: true }});
            window.__domObserver = observer;
            console.log('[DOM_OBS] Observer started on: {target_selector}');
        }})();
        """)
        self._active = True
        log.info(f"DOM observer started on '{target_selector}'")

    async def stop(self) -> None:
        """Disconnect the MutationObserver."""
        if not self._active:
            return
        try:
            await self._page.evaluate("if(window.__domObserver) window.__domObserver.disconnect();")
        except Exception:
            pass
        self._active = False
        log.info("DOM observer stopped")

    def _on_console(self, msg) -> None:
        """Handle browser console messages from our observer."""
        text = msg.text
        if text.startswith("[DOM_OBS]"):
            log.debug(text)
