"""
CATGPT — OpenAI-Compatible Terminal Chat

A beautiful full-screen TUI that talks to CatGPT Gateway (or any OpenAI-compatible API).
Uses the standard openai Python SDK — no browser management, no Playwright.

Configuration (env vars or CLI flags):
  CATGPT_API_URL    API base URL  (default: http://localhost:8000/v1)
  CATGPT_API_KEY    Bearer token  (default: dummy123)
  CATGPT_MODEL      Model name    (default: catgpt-browser)

Commands:
  /help              Show this help
  /new               Start a fresh conversation (clears history)
  /clear             Clear the display (history preserved)
  /system <text>     Set a system prompt for the session
  /model <name>      Switch to a different model
  /history           Show all conversation turns
  /export [file]     Export conversation to a markdown file
  /status            Show API config and session info
  /exit              Quit

Shortcuts:
  Ctrl+N   New conversation    Ctrl+E   Export
  Ctrl+L   Clear display       Ctrl+C   Quit
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import typer
from rich.markdown import Markdown

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, ScrollableContainer, Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Footer, Header, Input, Static

from src.log import suppress_console_logs

suppress_console_logs()

from src.log import setup_logging

log = setup_logging("cli", log_file="cli.log")
cli = typer.Typer(no_args_is_help=False, add_completion=False)

# ── Constants ────────────────────────────────────────────────────
VERSION = "6.1.0"
APP_NAME = "CATGPT"
APP_TAGLINE = "OpenAI-Compatible Terminal Chat"

THINKING_FRAMES = ["◐", "◓", "◑", "◒"]

CAT_ART = """\
      /\\_/\\
     ( ● . ● )
      > △ <
     /|   |\\
    (_|   |_)"""

LOGO_TEXT = """\
 ██████╗  █████╗ ████████╗ ██████╗ ██████╗ ████████╗
██╔════╝ ██╔══██╗╚══██╔══╝██╔════╝ ██╔══██╗╚══██╔══╝
██║      ███████║   ██║   ██║  ███╗██████╔╝   ██║
██║      ██╔══██║   ██║   ██║   ██║██╔═══╝    ██║
╚██████╗ ██║  ██║   ██║   ╚██████╔╝██║        ██║
 ╚═════╝ ╚═╝  ╚═╝   ╚═╝    ╚═════╝ ╚═╝        ╚═╝"""

WELCOME_TEMPLATE = """\
[bold #58a6ff]─── Welcome to CATGPT v{version} ───[/]

[#8b949e]Talking to:[/]  [#e6edf3]{url}[/]
[#8b949e]Model:[/]       [#58a6ff]{model}[/]

[bold #e6edf3]Commands[/]
  [#58a6ff]/help[/]              [#8b949e]│[/] Show all commands
  [#58a6ff]/new[/]               [#8b949e]│[/] Start fresh conversation
  [#58a6ff]/system <text>[/]     [#8b949e]│[/] Set a system prompt
  [#58a6ff]/model <name>[/]      [#8b949e]│[/] Switch model
  [#58a6ff]/history[/]           [#8b949e]│[/] View conversation turns
  [#58a6ff]/export [file][/]     [#8b949e]│[/] Export to markdown
  [#58a6ff]/status[/]            [#8b949e]│[/] API config & session info

[bold #e6edf3]Shortcuts[/]
  [bold #6e7681]Ctrl+N[/]  New chat    [bold #6e7681]Ctrl+E[/]  Export
  [bold #6e7681]Ctrl+L[/]  Clear       [bold #6e7681]Ctrl+C[/]  Quit
"""


# ================================================================
#  MESSAGE WIDGETS
# ================================================================


class UserMessage(Widget):
    """User message bubble with blue left bar."""

    DEFAULT_CLASSES = "user-msg"

    def __init__(self, text: str, turn: int) -> None:
        super().__init__()
        self._text = text
        self._turn = turn
        self._time = datetime.now().strftime("%H:%M:%S")

    def compose(self) -> ComposeResult:
        display = self._text if len(self._text) <= 600 else self._text[:597] + "…"
        words = len(self._text.split())
        yield Static(
            f"  You  ·  turn #{self._turn}  ·  {self._time}",
            classes="msg-header user-msg-header",
        )
        yield Static(display, classes="msg-body")
        yield Static(
            f"  {words} word{'s' if words != 1 else ''}",
            classes="msg-footer",
        )


class AssistantMessage(Widget):
    """Assistant response with green left bar and markdown rendering."""

    DEFAULT_CLASSES = "assistant-msg"

    def __init__(self, text: str, model: str, time_ms: int) -> None:
        super().__init__()
        self._text = text
        self._model = model
        self._time_ms = time_ms
        self._time = datetime.now().strftime("%H:%M:%S")

    def compose(self) -> ComposeResult:
        time_str = (
            f"{self._time_ms / 1000:.1f}s" if self._time_ms >= 1000 else f"{self._time_ms}ms"
        )
        words = len(self._text.split())
        yield Static(
            f"  {APP_NAME}  ·  {self._model}  ·  {self._time}",
            classes="msg-header assistant-msg-header",
        )
        if self._text.strip():
            yield Static(Markdown(self._text), classes="msg-body")
        else:
            yield Static("[dim]Empty response[/]", classes="msg-body")
        yield Static(
            f"  {words} word{'s' if words != 1 else ''}  ·  {time_str}",
            classes="msg-footer",
        )


class SystemPromptCard(Widget):
    """Displays the active system prompt."""

    DEFAULT_CLASSES = "system-prompt-card"

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        display = self._text if len(self._text) <= 300 else self._text[:297] + "…"
        yield Static("  ⚙  System Prompt Active", classes="msg-header system-prompt-header")
        yield Static(f"  {display}", classes="msg-body")


class ThinkingIndicator(Widget):
    """Animated spinner while waiting for the API response."""

    DEFAULT_CLASSES = "thinking-widget"

    frame: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        yield Static(
            f"  {THINKING_FRAMES[0]}  {APP_NAME} is thinking …",
            classes="thinking-label",
        )

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.12, self._tick)

    def _tick(self) -> None:
        self.frame = (self.frame + 1) % len(THINKING_FRAMES)

    def watch_frame(self, frame: int) -> None:
        try:
            self.query_one(".thinking-label", Static).update(
                f"  {THINKING_FRAMES[frame]}  {APP_NAME} is thinking …"
            )
        except Exception:
            pass

    def stop_animation(self) -> None:
        try:
            self._timer.stop()
        except Exception:
            pass


# ================================================================
#  SPLASH SCREEN
# ================================================================


class SplashScreen(Screen):
    """Animated splash screen. Auto-transitions after 2.5 s or on keypress."""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="splash-container"):
                yield Static(CAT_ART, id="splash-cat")
                yield Static(LOGO_TEXT, id="splash-logo")
                yield Static(f"───  {APP_TAGLINE}  ───", id="splash-tagline")
                yield Static(
                    f"v{VERSION}  ·  OpenAI-compatible  ·  any provider",
                    id="splash-version",
                )
                yield Static("press any key to skip", id="splash-hint")

    def on_mount(self) -> None:
        self.set_timer(2.5, self._go)

    def on_key(self, _: object) -> None:
        self._go()

    def _go(self) -> None:
        if self.app.screen is self:
            self.app.switch_screen("chat")


# ================================================================
#  CHAT SCREEN
# ================================================================


class ChatScreen(Screen):
    """Main chat interface — messages, input, keybindings."""

    BINDINGS = [
        Binding("ctrl+n", "new_chat",   "New Chat", key_display="^N"),
        Binding("ctrl+e", "export",     "Export",   key_display="^E"),
        Binding("ctrl+l", "clear_chat", "Clear",    key_display="^L"),
        Binding("ctrl+c", "quit_app",   "Quit",     key_display="^C", priority=True),
    ]

    # ── State ────────────────────────────────────────────────────

    def __init__(self) -> None:
        super().__init__()
        self.api_url = (
            os.getenv("CATGPT_API_URL")
            or os.getenv("OPENAI_API_BASE")
            or "http://localhost:8000/v1"
        )
        self.api_key = (
            os.getenv("CATGPT_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or "dummy123"
        )
        self.model = os.getenv("CATGPT_MODEL") or "catgpt-browser"

        self.messages: list[dict] = []       # full OpenAI-format conversation history
        self.system_prompt: str | None = None
        self.turn_count = 0
        self.last_time_ms = 0
        self.session_start = datetime.now()
        self._is_busy = False
        self._openai = None                  # openai.AsyncOpenAI — set in on_mount

    def on_mount(self) -> None:
        import openai

        self._openai = openai.AsyncOpenAI(
            base_url=self.api_url,
            api_key=self.api_key,
        )
        self.app.title = APP_NAME
        self.app.sub_title = f"{self.model}  ·  {self.api_url}"
        self.query_one("#chat-container", Vertical).border_title = f" 🐱  {APP_NAME} "
        self._show_welcome()
        self._check_connection()
        self.query_one("#chat-input", Input).focus()

    # ── Layout ───────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(self._status_text(), id="status-bar")
        with Vertical(id="chat-container"):
            with ScrollableContainer(id="chat-log"):
                pass
        yield Input(
            placeholder="Message CATGPT …  (/help for commands)",
            id="chat-input",
        )
        yield Footer()

    @property
    def chat_log(self) -> ScrollableContainer:
        return self.query_one("#chat-log", ScrollableContainer)

    # ── Welcome & connection check ───────────────────────────────

    def _show_welcome(self) -> None:
        text = WELCOME_TEMPLATE.format(
            version=VERSION,
            url=self.api_url,
            model=self.model,
        )
        self.chat_log.mount(Static(text, classes="welcome-card"))

    @work(exclusive=False, name="check_conn")
    async def _check_connection(self) -> None:
        try:
            result = await self._openai.models.list()
            names = [m.id for m in result.data]
            if names and self.model not in names:
                self.model = names[0]
                self.app.sub_title = f"{self.model}  ·  {self.api_url}"
            model_list = "  ".join(f"[#58a6ff]{n}[/]" for n in names[:5])
            self._mount_system(
                f"[#3fb950]✓[/]  Connected to [#58a6ff]{self.api_url}[/]\n"
                f"  Available: {model_list}",
                "system-success",
            )
        except Exception as exc:
            self._mount_system(
                f"[#f85149]✗[/]  Cannot reach [#58a6ff]{self.api_url}[/]\n"
                f"  Is the server running?  [#6e7681]{exc}[/]",
                "system-error",
            )
        self._refresh_status()

    # ── Input handling ───────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            self._dispatch_command(parts[0].lower(), parts[1] if len(parts) > 1 else "")
        else:
            self._send(text)

    # ── Send message ─────────────────────────────────────────────

    def _send(self, text: str) -> None:
        if self._is_busy:
            self._mount_system("⚠  Please wait for the current response …", "system-warn")
            return

        self.turn_count += 1
        self.chat_log.mount(UserMessage(text, self.turn_count))
        thinking = ThinkingIndicator()
        self.chat_log.mount(thinking)
        self.chat_log.scroll_end(animate=False)
        self._is_busy = True
        self._refresh_status()
        self._do_send(text, thinking)

    @work(exclusive=True, name="send")
    async def _do_send(self, text: str, thinking: ThinkingIndicator) -> None:
        self.messages.append({"role": "user", "content": text})

        payload: list[dict] = []
        if self.system_prompt:
            payload.append({"role": "system", "content": self.system_prompt})
        payload.extend(self.messages)

        try:
            t0 = datetime.now()
            resp = await self._openai.chat.completions.create(
                model=self.model,
                messages=payload,
            )
            elapsed_ms = int((datetime.now() - t0).total_seconds() * 1000)
            content = resp.choices[0].message.content or ""
            self.messages.append({"role": "assistant", "content": content})
            self.last_time_ms = elapsed_ms
            thinking.stop_animation()
            thinking.remove()
            self.chat_log.mount(AssistantMessage(content, self.model, elapsed_ms))

        except Exception as exc:
            log.error(f"API call failed: {exc}", exc_info=True)
            self.messages.pop()
            self.turn_count = max(0, self.turn_count - 1)
            thinking.stop_animation()
            thinking.remove()
            self._mount_system(f"[#f85149]✗[/]  {exc}", "system-error")

        self._is_busy = False
        self._refresh_status()
        self.chat_log.scroll_end(animate=False)

    # ── Command dispatch ─────────────────────────────────────────

    def _dispatch_command(self, cmd: str, args: str) -> None:
        dispatch: dict[str, object] = {
            "/exit":    lambda: self.action_quit_app(),
            "/quit":    lambda: self.action_quit_app(),
            "/q":       lambda: self.action_quit_app(),
            "/help":    lambda: self._show_help(),
            "/clear":   lambda: self.action_clear_chat(),
            "/new":     lambda: self.action_new_chat(),
            "/history": lambda: self._show_history(),
            "/export":  lambda: self._export_command(args),
            "/status":  lambda: self._show_status(),
            "/system":  lambda: self._set_system(args),
            "/model":   lambda: self._set_model(args),
        }
        handler = dispatch.get(cmd)
        if handler:
            handler()  # type: ignore[operator]
        else:
            self._mount_system(
                f"[#f85149]✗[/]  Unknown command: [bold]{cmd}[/] — type /help",
                "system-error",
            )

    # ── /help ────────────────────────────────────────────────────

    def _show_help(self) -> None:
        lines = [
            "[bold #58a6ff]─── CATGPT Commands ───[/]\n",
            "  [#58a6ff]/new[/]               Start fresh (clears history & system prompt)",
            "  [#58a6ff]/clear[/]             Clear the display  (history preserved)",
            "  [#58a6ff]/system <text>[/]     Set a system prompt for this session",
            "  [#58a6ff]/model <name>[/]      Switch model  (e.g. /model gpt-5.6-sol-high)",
            "  [#58a6ff]/history[/]           Show all conversation turns",
            "  [#58a6ff]/export [file][/]     Export conversation to markdown",
            "  [#58a6ff]/status[/]            API config & session info",
            "  [#58a6ff]/help[/]              Show this help",
            "  [#58a6ff]/exit[/]              Quit",
            "",
            "[bold #58a6ff]─── Shortcuts ───[/]\n",
            "  [bold #6e7681]Ctrl+N[/]  New chat     [bold #6e7681]Ctrl+E[/]  Export markdown",
            "  [bold #6e7681]Ctrl+L[/]  Clear        [bold #6e7681]Ctrl+C[/]  Quit",
            "",
            "[dim italic]  Tip: /system 'You are a senior Python engineer' sets a persistent persona",
            "  Tip: /clear keeps history — the model still remembers previous turns",
            "  Tip: Set CATGPT_API_URL / CATGPT_API_KEY / CATGPT_MODEL as env vars[/]",
        ]
        self._mount_system("\n".join(lines), "system-info-block")

    # ── /status ──────────────────────────────────────────────────

    def _show_status(self) -> None:
        elapsed = datetime.now() - self.session_start
        m, s = divmod(int(elapsed.total_seconds()), 60)
        masked_key = (
            self.api_key[:4] + "•" * max(0, len(self.api_key) - 4)
            if len(self.api_key) > 4
            else "•" * len(self.api_key)
        )
        sys_display = (
            f"[#d29922]{self.system_prompt[:60]}{'…' if len(self.system_prompt) > 60 else ''}[/]"
            if self.system_prompt
            else "[#6e7681]not set[/]"
        )
        lines = [
            "[bold #58a6ff]─── CATGPT Status ───[/]\n",
            f"  API URL         [#58a6ff]{self.api_url}[/]",
            f"  Model           [#58a6ff]{self.model}[/]",
            f"  Auth token      [#6e7681]{masked_key}[/]",
            f"  Turns           {self.turn_count}",
            f"  History msgs    {len(self.messages)}",
            f"  System prompt   {sys_display}",
            (
                f"  Last response   [#3fb950]{self.last_time_ms}ms[/]"
                if self.last_time_ms
                else "  Last response   [#6e7681]—[/]"
            ),
            f"  Session uptime  {m}m {s}s",
        ]
        self._mount_system("\n".join(lines), "system-info-block")

    # ── /history ─────────────────────────────────────────────────

    def _show_history(self) -> None:
        if not self.messages:
            self._mount_system("[#8b949e]No conversation history yet.[/]", "system-msg")
            return
        lines = [
            f"[bold #58a6ff]─── Conversation History ({len(self.messages)} messages) ───[/]\n"
        ]
        for i, msg in enumerate(self.messages, 1):
            role = msg["role"]
            content = str(msg.get("content") or "")
            preview = content[:90].replace("\n", " ")
            if len(content) > 90:
                preview += "…"
            color, icon = {
                "user":      ("#58a6ff", "▶"),
                "assistant": ("#3fb950", "◀"),
            }.get(role, ("#d29922", "⚙"))
            lines.append(
                f"  [{color}]{i:>2}. {icon} {role:<10}[/]  [#8b949e]{preview}[/]"
            )
        lines.append("\n[#6e7681]  Use /new to clear history, /export to save it[/]")
        self._mount_system("\n".join(lines), "system-info-block")

    # ── /system ──────────────────────────────────────────────────

    def _set_system(self, text: str) -> None:
        text = text.strip()
        if not text:
            self.system_prompt = None
            self._mount_system("[#8b949e]System prompt cleared.[/]", "system-msg")
            self._refresh_status()
            return
        self.system_prompt = text
        self.chat_log.mount(SystemPromptCard(text))
        self.chat_log.scroll_end(animate=False)
        self._mount_system(
            "[#3fb950]✓[/]  System prompt set — applies to all future messages.",
            "system-success",
        )
        self._refresh_status()

    # ── /model ───────────────────────────────────────────────────

    def _set_model(self, name: str) -> None:
        name = name.strip()
        if not name:
            self._mount_system(
                f"[#8b949e]Current model: [#58a6ff]{self.model}[/]  "
                "[#6e7681]Use /model <name> to switch[/]",
                "system-msg",
            )
            return
        old = self.model
        self.model = name
        self.app.sub_title = f"{self.model}  ·  {self.api_url}"
        self._refresh_status()
        self._mount_system(
            f"[#3fb950]✓[/]  Switched: [#6e7681]{old}[/] → [#58a6ff]{name}[/]",
            "system-success",
        )

    # ── /export ──────────────────────────────────────────────────

    def _export_command(self, filename: str) -> None:
        if not self.messages:
            self._mount_system("[#8b949e]Nothing to export yet.[/]", "system-msg")
            return
        self._do_export(filename.strip())

    @work(exclusive=False, name="export")
    async def _do_export(self, filename: str) -> None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = filename or f"catgpt-export-{ts}.md"
        if not name.endswith(".md"):
            name += ".md"
        path = Path(name) if ("/" in name or "\\" in name) else Path.cwd() / name

        lines = [
            f"# CATGPT Export — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
            f"**Model:** `{self.model}`  ",
            f"**API:** `{self.api_url}`  ",
            f"**Turns:** {self.turn_count}  ",
        ]
        if self.system_prompt:
            lines += [f"\n**System Prompt:**\n> {self.system_prompt}\n"]
        lines.append("\n---\n")
        for msg in self.messages:
            role = msg["role"].title()
            content = msg.get("content") or ""
            lines.append(f"\n### {role}\n\n{content}\n")

        try:
            path.write_text("\n".join(lines), encoding="utf-8")
            self._mount_system(
                f"[#3fb950]✓[/]  Exported {len(self.messages)} messages → [#58a6ff]{path}[/]",
                "system-success",
            )
        except Exception as exc:
            self._mount_system(f"[#f85149]✗[/]  Export failed: {exc}", "system-error")

    # ── Actions (keybindings) ────────────────────────────────────

    def action_new_chat(self) -> None:
        self.messages.clear()
        self.turn_count = 0
        self.last_time_ms = 0
        self.system_prompt = None
        self.chat_log.remove_children()
        self.chat_log.mount(
            Static(
                "[#3fb950]✓[/]  New conversation — history cleared.",
                classes="system-success",
            )
        )
        self._refresh_status()

    def action_clear_chat(self) -> None:
        self.chat_log.remove_children()
        self.chat_log.mount(
            Static(
                "[#8b949e]Display cleared.  "
                "[dim]History still active — use /new to fully reset.[/]",
                classes="system-msg",
            )
        )

    def action_export(self) -> None:
        if not self.messages:
            self._mount_system("[#8b949e]Nothing to export yet.[/]", "system-msg")
            return
        self._do_export("")

    def action_quit_app(self) -> None:
        self.app.exit()

    # ── Helpers ──────────────────────────────────────────────────

    def _mount_system(self, text: str, css_class: str = "system-msg") -> None:
        self.chat_log.mount(Static(text, classes=css_class))
        self.chat_log.scroll_end(animate=False)

    def _status_text(self) -> str:
        conn = "[#d29922]● thinking[/]" if self._is_busy else "[#3fb950]● ready[/]"
        model = f"[#58a6ff]{self.model}[/]"
        turns = f"turn {self.turn_count}"
        msgs = f"[#6e7681]{len(self.messages)} msgs[/]"
        time_str = (
            f"[#3fb950]{self.last_time_ms}ms[/]" if self.last_time_ms else "[#6e7681]—[/]"
        )
        sys_ind = "  [#d29922]⚙ system[/]" if self.system_prompt else ""
        return f"  {conn}  │  {model}  │  {turns}  │  {msgs}  │  {time_str}{sys_ind}"

    def _refresh_status(self) -> None:
        try:
            self.query_one("#status-bar", Static).update(self._status_text())
        except Exception:
            pass


# ================================================================
#  APP
# ================================================================


class CatGPTApp(App):
    """CATGPT — OpenAI-Compatible Terminal Chat."""

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
def chat(
    api_url: str = typer.Option(None, "--api-url", "-u", help="API base URL"),
    api_key: str = typer.Option(None, "--api-key", "-k", help="Bearer token"),
    model: str = typer.Option(None, "--model", "-m", help="Model name"),
) -> None:
    """Start an interactive CATGPT terminal session."""
    if api_url:
        os.environ["CATGPT_API_URL"] = api_url
    if api_key:
        os.environ["CATGPT_API_KEY"] = api_key
    if model:
        os.environ["CATGPT_MODEL"] = model
    CatGPTApp().run()


def main() -> None:
    """Entry point."""
    cli()


if __name__ == "__main__":
    main()
