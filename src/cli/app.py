"""
CATGPT — Control · Agitate · Test

Full-screen Textual TUI for ChatGPT browser automation.
GitHub Dark theme with animated splash, scrollable chat, keyboard shortcuts.

Commands:
  /new          Start a new conversation
  /threads      List recent threads from the sidebar
  /thread <id>  Switch to a specific thread
  /images       List downloaded DALL-E images
  /status       Show connection & session info
  /clear        Clear the chat display
  /help         Show available commands
  /exit, /quit  Close browser and exit

Shortcuts:
  Ctrl+N   New chat        Ctrl+T   List threads
  Ctrl+L   Clear chat      Ctrl+C   Quit
"""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import datetime

import typer
from rich.markdown import Markdown

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, ScrollableContainer, Vertical
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Footer, Header, Input, Static

# -- Suppress console logs BEFORE any other src imports ----------
from src.log import suppress_console_logs

suppress_console_logs()

from src.browser.manager import BrowserManager
from src.chatgpt.client import ChatGPTClient
from src.chatgpt.models import ChatResponse, ImageInfo
from src.config import Config
from src.log import setup_logging

log = setup_logging("cli", log_file="cli.log")
cli = typer.Typer(no_args_is_help=False, add_completion=False)

# -- Constants ---------------------------------------------------
VERSION = "2.1.0"
APP_NAME = "CATGPT"
APP_TAGLINE = "Control · Agitate · Test"

CAT_ART = """
      /\\_/\\
     ( ● . ● )
      > △ <
     /|   |\\
    (_|   |_)
"""

LOGO_TEXT = """
 ██████╗  █████╗ ████████╗ ██████╗ ██████╗ ████████╗
██╔════╝ ██╔══██╗╚══██╔══╝██╔════╝ ██╔══██╗╚══██╔══╝
██║     ███████║   ██║   ██║  ███╗██████╔╝   ██║
██║     ██╔══██║   ██║   ██║   ██║██╔═══╝    ██║
╚██████╗██║  ██║   ██║   ╚██████╔╝██║        ██║
 ╚═════╝╚═╝  ╚═╝   ╚═╝    ╚═════╝ ╚═╝        ╚═╝
"""

WELCOME_TEXT = """[bold #58a6ff]─── Welcome to CATGPT ───[/]

[#8b949e]ChatGPT browser automation powered by Playwright.[/]
[#8b949e]Type a message below or use commands to get started.[/]

[bold #e6edf3]Quick Start[/]
  [#58a6ff]/help[/]     [#8b949e]│[/] Show all commands
  [#58a6ff]/new[/]      [#8b949e]│[/] Start fresh conversation
  [#58a6ff]/threads[/]  [#8b949e]│[/] Browse recent chats
  [#58a6ff]/status[/]   [#8b949e]│[/] Connection details

[bold #e6edf3]Shortcuts[/]
  [bold #6e7681]Ctrl+N[/]  New chat   [bold #6e7681]Ctrl+T[/]  Threads
  [bold #6e7681]Ctrl+L[/]  Clear      [bold #6e7681]Ctrl+C[/]  Quit
"""


# ================================================================
#  MESSAGE WIDGETS
# ================================================================


class UserMessage(Widget):
    """User message with blue accent bar."""

    DEFAULT_CLASSES = "user-msg"

    def __init__(self, text: str, msg_num: int) -> None:
        super().__init__()
        self._text = text
        self._num = msg_num

    def compose(self) -> ComposeResult:
        display = self._text if len(self._text) <= 500 else self._text[:497] + "\u2026"
        yield Static(f"  You  \u00b7  #{self._num}", classes="user-msg-header")
        yield Static(display, classes="user-msg-body")


class AssistantMessage(Widget):
    """Assistant response with green accent bar and markdown rendering."""

    DEFAULT_CLASSES = "assistant-msg"

    def __init__(self, text: str, time_ms: int) -> None:
        super().__init__()
        self._text = text
        self._time_ms = time_ms

    def compose(self) -> ComposeResult:
        time_str = (
            f"{self._time_ms / 1000:.1f}s"
            if self._time_ms >= 1000
            else f"{self._time_ms}ms"
        )
        yield Static(f"  {APP_NAME}", classes="assistant-msg-header")
        if self._text.strip():
            yield Static(Markdown(self._text), classes="assistant-msg-body")
        else:
            yield Static("[dim]No text content[/]", classes="assistant-msg-body")
        yield Static(
            f"{len(self._text)} chars \u00b7 {time_str}",
            classes="assistant-msg-footer",
        )


class ImageCard(Widget):
    """DALL-E image metadata card with purple accent."""

    DEFAULT_CLASSES = "image-card"

    def __init__(self, img: ImageInfo, index: int = 1) -> None:
        super().__init__()
        self._img = img
        self._index = index

    def compose(self) -> ComposeResult:
        title = self._img.prompt_title or self._img.alt or "Generated Image"
        yield Static(f"  \U0001f5bc  Image #{self._index}", classes="image-card-header")

        parts: list[str] = [f"[bold]{title}[/]", ""]
        if self._img.local_path:
            parts.append(f"  Saved:  {self._img.local_path}")
            try:
                size = os.path.getsize(self._img.local_path)
                size_str = (
                    f"{size / 1024 / 1024:.1f} MB"
                    if size >= 1024 * 1024
                    else f"{size / 1024:.1f} KB"
                )
                parts.append(f"  Size:   {size_str}")
            except OSError:
                pass
        else:
            parts.append("  [#f85149]Download failed[/]")

        if self._img.url:
            short = self._img.url[:60] + "\u2026" if len(self._img.url) > 60 else self._img.url
            parts.append(f"  URL:    [dim]{short}[/]")

        yield Static("\n".join(parts), classes="image-card-body")


class ThinkingIndicator(Static):
    """Shown while waiting for ChatGPT response."""

    DEFAULT_CLASSES = "thinking"

    def __init__(self) -> None:
        super().__init__(f"\u25cf  {APP_NAME} is thinking \u2026")


# ================================================================
#  SPLASH SCREEN
# ================================================================


class SplashScreen(Screen):
    """Animated splash with cat art and CATGPT logo. Auto-transitions 3s."""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="splash-container"):
                yield Static(CAT_ART, id="splash-cat")
                yield Static(LOGO_TEXT, id="splash-logo")
                yield Static(
                    f"\u2500\u2500\u2500  {APP_TAGLINE}  \u2500\u2500\u2500",
                    id="splash-tagline",
                )
                yield Static(
                    f"v{VERSION} \u00b7 browser automation \u00b7 playwright",
                    id="splash-version",
                )
                yield Static(
                    "press any key to continue",
                    id="splash-hint",
                )

    def on_mount(self) -> None:
        self.set_timer(3.0, self._go_to_chat)

    def on_key(self, _event: object) -> None:
        self._go_to_chat()

    def _go_to_chat(self) -> None:
        if self.app.screen is self:
            self.app.switch_screen("chat")


# ================================================================
#  CHAT SCREEN
# ================================================================


class ChatScreen(Screen):
    """Main chat interface \u2014 messages, input, keybindings."""

    BINDINGS = [
        Binding("ctrl+n", "new_chat", "New Chat", key_display="^N"),
        Binding("ctrl+t", "threads", "Threads", key_display="^T"),
        Binding("ctrl+l", "clear_chat", "Clear", key_display="^L"),
        Binding("ctrl+c", "quit_app", "Quit", key_display="^C", priority=True),
    ]

    # -- State ---------------------------------------------------

    def __init__(self) -> None:
        super().__init__()
        self.browser: BrowserManager | None = None
        self.client: ChatGPTClient | None = None
        self.connected: bool = False
        self.thread_id: str = ""
        self.msg_count: int = 0
        self.last_time_ms: int = 0
        self.total_images: int = 0
        self.session_start: datetime = datetime.now()
        self._is_busy: bool = False

        # Single event loop for ALL Playwright operations
        self._browser_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_browser_loop, daemon=True
        )
        self._loop_thread.start()

    def _run_browser_loop(self) -> None:
        """Run the shared browser event loop forever in a daemon thread."""
        asyncio.set_event_loop(self._browser_loop)
        self._browser_loop.run_forever()

    def _run_async(self, coro: object) -> object:
        """Submit a coroutine to the shared browser loop and block until done."""
        future = asyncio.run_coroutine_threadsafe(coro, self._browser_loop)  # type: ignore[arg-type]
        return future.result()

    # -- Layout --------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(self._build_status_text(), id="status-bar")
        with Vertical(id="chat-container"):
            with ScrollableContainer(id="chat-log"):
                yield Static(
                    "\u25cf  Connecting to ChatGPT \u2026", classes="system-msg"
                )
        yield Input(
            placeholder="Message CATGPT \u2026  (/help for commands)",
            id="chat-input",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.app.title = APP_NAME
        self.app.sub_title = APP_TAGLINE
        container = self.query_one("#chat-container", Vertical)
        container.border_title = f" \U0001f431  {APP_NAME} "
        self._connect()

    @property
    def chat_log(self) -> ScrollableContainer:
        """Cached access to the chat log container."""
        return self.query_one("#chat-log", ScrollableContainer)

    # -- Browser Connection (async worker) -----------------------

    @work(exclusive=True, thread=True, name="connect")
    def _connect(self) -> None:
        """Launch browser and connect to ChatGPT in a background thread."""

        async def _do_connect() -> tuple[BrowserManager, ChatGPTClient, str]:
            from src.browser.auto_login import ensure_logged_in

            browser = BrowserManager()
            page = await browser.start()
            await browser.navigate(Config.CHATGPT_URL)
            # Apply stealth AFTER navigation (avoids DNS failure in Docker)
            await browser.apply_stealth_patches()
            await asyncio.sleep(3)
            if not await browser.is_logged_in():
                logged_in = await ensure_logged_in(browser)
                if not logged_in:
                    raise RuntimeError(
                        "Could not log in to ChatGPT"
                    )
            client = ChatGPTClient(page)
            tid = client._extract_thread_id()
            return browser, client, tid

        try:
            browser, client, tid = self._run_async(_do_connect())
            self.browser = browser
            self.client = client
            self.thread_id = tid
            self.connected = True
            self.app.call_from_thread(self._on_connected)
        except Exception as exc:
            log.error(f"Connection failed: {exc}", exc_info=True)
            self.app.call_from_thread(self._on_connect_error, str(exc))

    def _on_connected(self) -> None:
        chat_log = self.chat_log
        chat_log.remove_children()
        if self.thread_id:
            chat_log.mount(
                Static(
                    f"[#3fb950]\u2713[/]  Connected \u2014 resuming thread [#58a6ff]{self.thread_id[:12]}\u2026[/]",
                    classes="system-success",
                )
            )
        chat_log.mount(Static(WELCOME_TEXT, classes="welcome-card"))
        self._refresh_status()
        self.query_one("#chat-input", Input).focus()

    def _on_connect_error(self, error: str) -> None:
        chat_log = self.chat_log
        chat_log.remove_children()
        chat_log.mount(Static(f"[#f85149]\u2717[/]  {error}", classes="system-error"))

    # -- Input Handling ------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            cmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            self._dispatch_command(cmd, args)
        else:
            self._send_user_message(text)

    # -- Send Message --------------------------------------------

    def _send_user_message(self, text: str) -> None:
        if self._is_busy:
            self._mount_system("[#d29922]\u26a0[/]  Please wait for the current response \u2026", "system-error")
            return
        if not self.connected or not self.client:
            self._mount_system("[#d29922]\u26a0[/]  Not connected yet \u2014 please wait \u2026", "system-error")
            return

        self.msg_count += 1
        chat_log = self.chat_log
        chat_log.mount(UserMessage(text, self.msg_count))
        thinking = ThinkingIndicator()
        chat_log.mount(thinking)
        chat_log.scroll_end(animate=False)

        self._is_busy = True
        self._do_send(text, thinking)

    @work(exclusive=True, thread=True, name="send")
    def _do_send(self, text: str, thinking: ThinkingIndicator) -> None:
        async def _send() -> ChatResponse:
            assert self.client is not None
            return await self.client.send_message(text)

        try:
            response = self._run_async(_send())
            self.app.call_from_thread(self._on_response, response, thinking)
        except Exception as exc:
            log.error(f"Send failed: {exc}", exc_info=True)
            self.app.call_from_thread(self._on_send_error, str(exc), thinking)

    def _on_response(self, response: ChatResponse, thinking: ThinkingIndicator) -> None:
        thinking.remove()
        chat_log = self.chat_log

        # Images
        if response.has_images:
            for i, img in enumerate(response.images, 1):
                chat_log.mount(ImageCard(img, index=i))
                self.total_images += 1

        # Text
        if response.message.strip():
            chat_log.mount(
                AssistantMessage(response.message, response.response_time_ms)
            )
        elif not response.has_images:
            chat_log.mount(
                AssistantMessage("[No response text]", response.response_time_ms)
            )

        self.last_time_ms = response.response_time_ms
        self.thread_id = response.thread_id or self.thread_id
        self._is_busy = False
        self._refresh_status()
        chat_log.scroll_end(animate=False)

    def _on_send_error(self, error: str, thinking: ThinkingIndicator) -> None:
        thinking.remove()
        self.msg_count = max(0, self.msg_count - 1)
        self._is_busy = False
        self._mount_system(f"[#f85149]\u2717[/]  {error}", "system-error")

    # -- Command Dispatch ----------------------------------------

    def _dispatch_command(self, cmd: str, args: str) -> None:
        commands: dict[str, object] = {
            "/exit": lambda: self.action_quit_app(),
            "/quit": lambda: self.action_quit_app(),
            "/q": lambda: self.action_quit_app(),
            "/help": lambda: self._show_help(),
            "/clear": lambda: self.action_clear_chat(),
            "/new": lambda: self.action_new_chat(),
            "/threads": lambda: self.action_threads(),
            "/images": lambda: self._show_images(),
            "/status": lambda: self._show_status(),
            "/thread": lambda: self._switch_thread(args),
        }
        handler = commands.get(cmd)
        if handler:
            handler()
        else:
            self._mount_system(f"[#f85149]\u2717[/]  Unknown command: {cmd} \u2014 type /help", "system-error")

    # -- /help ---------------------------------------------------

    def _show_help(self) -> None:
        lines = [
            "[bold #58a6ff]\u2500\u2500\u2500 CATGPT Commands \u2500\u2500\u2500[/]\n",
            "  [bold #3fb950]/new[/]            Start a fresh conversation",
            "  [bold #3fb950]/threads[/]        List recent threads from sidebar",
            "  [bold #3fb950]/thread <id>[/]    Switch to an existing thread",
            "  [bold #3fb950]/images[/]         List all downloaded DALL-E images",
            "  [bold #3fb950]/status[/]         Show connection & session details",
            "  [bold #3fb950]/clear[/]          Clear the chat display",
            "  [bold #3fb950]/help[/]           Show this help panel",
            "  [bold #3fb950]/exit[/]           Close browser and exit",
            "",
            "[bold #58a6ff]\u2500\u2500\u2500 Keyboard Shortcuts \u2500\u2500\u2500[/]\n",
            "  [bold #6e7681]Ctrl+N[/]  New chat       [bold #6e7681]Ctrl+T[/]  Threads",
            "  [bold #6e7681]Ctrl+L[/]  Clear chat     [bold #6e7681]Ctrl+C[/]  Quit",
            "",
            "[dim italic]  Tip: Ask ChatGPT to 'generate an image of ...' for DALL-E",
            "  Tip: Images auto-download to downloads/images/",
            "  Tip: All logs saved to logs/ (clean TUI, full debug in files)[/]",
        ]
        self._mount_system("\n".join(lines), "system-info-block")

    # -- /images -------------------------------------------------

    def _show_images(self) -> None:
        images_dir = Config.IMAGES_DIR
        if not images_dir.exists():
            self._mount_system("[#8b949e]No images downloaded yet.[/]", "system-msg")
            return

        files = sorted(
            images_dir.glob("*.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not files:
            self._mount_system("[#8b949e]No images downloaded yet.[/]", "system-msg")
            return

        lines = [f"[bold #bc8cff]\u2500\u2500\u2500 Downloaded Images ({len(files)}) \u2500\u2500\u2500[/]\n"]
        for i, f in enumerate(files[:20], 1):
            size = f.stat().st_size
            size_str = (
                f"{size / 1024 / 1024:.1f} MB"
                if size >= 1024 * 1024
                else f"{size / 1024:.1f} KB"
            )
            mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            lines.append(f"  [#6e7681]{i:>2}.[/] [#58a6ff]{f.name}[/]  [#3fb950]{size_str:>8}[/]  [#6e7681]{mtime}[/]")

        lines.append(f"\n[#6e7681]  Folder: {images_dir}[/]")
        self._mount_system("\n".join(lines), "system-info-block")

    # -- /status -------------------------------------------------

    def _show_status(self) -> None:
        elapsed = datetime.now() - self.session_start
        elapsed_str = f"{int(elapsed.total_seconds() // 60)}m {int(elapsed.total_seconds() % 60)}s"
        conn = "[#3fb950]\u25cf Connected[/]" if self.connected else "[#f85149]\u25cf Disconnected[/]"

        lines = [
            "[bold #58a6ff]\u2500\u2500\u2500 CATGPT Status \u2500\u2500\u2500[/]\n",
            f"  Connection   {conn}",
            f"  Thread       [#58a6ff]{self.thread_id or '(new chat)'}[/]",
            f"  Messages     {self.msg_count}",
            f"  Images       {self.total_images}",
            f"  Session      {elapsed_str}",
            f"  Browser      [#6e7681]{Config.BROWSER_DATA_DIR}[/]",
            f"  Logs         [#6e7681]{Config.LOG_DIR}[/]",
            f"  Images Dir   [#6e7681]{Config.IMAGES_DIR}[/]",
        ]
        self._mount_system("\n".join(lines), "system-info-block")

    # -- /threads ------------------------------------------------

    def action_threads(self) -> None:
        if not self.connected or not self.client:
            self._mount_system("[#d29922]\u26a0[/]  Not connected yet", "system-error")
            return
        self._mount_system("\u25cf  Loading threads \u2026", "system-msg")
        self._do_list_threads()

    @work(exclusive=True, thread=True, name="threads")
    def _do_list_threads(self) -> None:
        async def _list() -> list[dict]:
            assert self.client is not None
            return await self.client.list_threads()

        try:
            threads = self._run_async(_list())
            self.app.call_from_thread(self._on_threads_loaded, threads)
        except Exception as exc:
            log.error(f"List threads failed: {exc}", exc_info=True)
            self.app.call_from_thread(
                self._mount_system, f"[#f85149]\u2717[/]  {exc}", "system-error"
            )

    def _on_threads_loaded(self, threads: list[dict]) -> None:
        if not threads:
            self._mount_system("[#8b949e]No threads found in sidebar.[/]", "system-msg")
            return
        lines = [f"[bold #58a6ff]\u2500\u2500\u2500 Recent Threads ({len(threads)}) \u2500\u2500\u2500[/]\n"]
        for i, t in enumerate(threads[:15], 1):
            lines.append(f"  [#6e7681]{i:>2}.[/] [#58a6ff]{t['id'][:24]}[/]  {t['title']}")
        lines.append("\n[#6e7681]  Use /thread <id> to switch[/]")
        self._mount_system("\n".join(lines), "system-info-block")

    # -- /new ----------------------------------------------------

    def action_new_chat(self) -> None:
        if not self.connected or not self.client:
            self._mount_system("[#d29922]\u26a0[/]  Not connected yet", "system-error")
            return
        self._mount_system("\u25cf  Starting new chat \u2026", "system-msg")
        self._do_new_chat()

    @work(exclusive=True, thread=True, name="new_chat")
    def _do_new_chat(self) -> None:
        async def _new() -> None:
            assert self.client is not None
            await self.client.new_chat()

        try:
            self._run_async(_new())
            self.msg_count = 0
            self.last_time_ms = 0
            self.thread_id = ""
            self.app.call_from_thread(self._on_new_chat)
        except Exception as exc:
            log.error(f"New chat failed: {exc}", exc_info=True)
            self.app.call_from_thread(
                self._mount_system, f"[#f85149]\u2717[/]  {exc}", "system-error"
            )

    def _on_new_chat(self) -> None:
        chat_log = self.chat_log
        chat_log.remove_children()
        chat_log.mount(
            Static("[#3fb950]\u2713[/]  New conversation started \u2014 type a message", classes="system-success")
        )
        self._refresh_status()

    # -- /thread <id> --------------------------------------------

    def _switch_thread(self, tid: str) -> None:
        tid = tid.strip()
        if not tid:
            self._mount_system("[#f85149]\u2717[/]  Usage: /thread <thread-id>", "system-error")
            return
        if not self.connected or not self.client:
            self._mount_system("[#d29922]\u26a0[/]  Not connected yet", "system-error")
            return
        self._mount_system(f"\u25cf  Switching to {tid[:12]}\u2026", "system-msg")
        self._do_switch_thread(tid)

    @work(exclusive=True, thread=True, name="switch_thread")
    def _do_switch_thread(self, tid: str) -> None:
        async def _switch() -> None:
            assert self.client is not None
            await self.client.navigate_to_thread(tid)

        try:
            self._run_async(_switch())
            self.msg_count = 0
            self.last_time_ms = 0
            self.thread_id = tid
            self.app.call_from_thread(self._on_thread_switched, tid)
        except Exception as exc:
            log.error(f"Switch thread failed: {exc}", exc_info=True)
            self.app.call_from_thread(
                self._mount_system, f"[#f85149]\u2717[/]  {exc}", "system-error"
            )

    def _on_thread_switched(self, tid: str) -> None:
        self._mount_system(f"[#3fb950]\u2713[/]  Switched to thread [#58a6ff]{tid[:12]}\u2026[/]", "system-success")
        self._refresh_status()

    # -- /clear & Ctrl+L ----------------------------------------

    def action_clear_chat(self) -> None:
        chat_log = self.chat_log
        chat_log.remove_children()
        chat_log.mount(Static("[#8b949e]Chat cleared.[/]", classes="system-msg"))

    # -- Quit (Ctrl+C) ------------------------------------------

    def action_quit_app(self) -> None:
        self._do_quit()

    @work(exclusive=True, thread=True, name="quit")
    def _do_quit(self) -> None:
        async def _close() -> None:
            if self.browser:
                await self.browser.close()

        try:
            self._run_async(_close())
        except Exception as exc:
            log.error(f"Browser close error: {exc}")
        finally:
            self._browser_loop.call_soon_threadsafe(self._browser_loop.stop)
            self.app.call_from_thread(self.app.exit)

    # -- Helpers -------------------------------------------------

    def _mount_system(self, text: str, css_class: str = "system-msg") -> None:
        """Mount a system message into the chat log."""
        chat_log = self.chat_log
        chat_log.mount(Static(text, classes=css_class))
        chat_log.scroll_end(animate=False)

    def _build_status_text(self) -> str:
        """Build the single-line status bar string."""
        if self.connected:
            conn = "[#3fb950]\u25cf[/] connected"
        else:
            conn = "[#f85149]\u25cf[/] connecting\u2026"
        tid = (
            f"[#58a6ff]{self.thread_id[:8]}\u2026[/]"
            if self.thread_id
            else "[#6e7681]new chat[/]"
        )
        msg = f"msgs: {self.msg_count}"
        time_str = (
            f"[#3fb950]{self.last_time_ms}ms[/]" if self.last_time_ms > 0 else "[#6e7681]\u2014[/]"
        )
        parts = [conn, tid, msg, time_str]
        if self.total_images > 0:
            parts.append(f"[#bc8cff]\U0001f5bc {self.total_images}[/]")
        return "  \u2502  ".join(parts)

    def _refresh_status(self) -> None:
        """Update the status bar widget."""
        try:
            bar = self.query_one("#status-bar", Static)
            bar.update(self._build_status_text())
        except Exception:
            pass


# ================================================================
#  APP
# ================================================================


class CatGPTApp(App):
    """CATGPT \u2014 Full-screen TUI for ChatGPT browser automation."""

    TITLE = APP_NAME
    SUB_TITLE = APP_TAGLINE
    CSS_PATH = "catgpt.tcss"

    SCREENS = {"chat": ChatScreen}

    def on_mount(self) -> None:
        self.push_screen(SplashScreen())


# ================================================================
#  ENTRY POINTS
# ================================================================


@cli.command()
def chat() -> None:
    """Start an interactive CATGPT session."""
    CatGPTApp().run()


def main() -> None:
    """Entry point."""
    cli()


if __name__ == "__main__":
    main()
