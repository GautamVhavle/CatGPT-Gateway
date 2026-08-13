from __future__ import annotations

import sys
import types
import unittest
import importlib.util
from pathlib import Path
from unittest.mock import patch

# Provide a minimal patchright stub so client tests can import browser modules
# without requiring browser automation dependencies.
if "patchright" not in sys.modules and importlib.util.find_spec("patchright.async_api") is None:
    patchright_mod = types.ModuleType("patchright")
    async_api_mod = types.ModuleType("patchright.async_api")
    async_api_mod.Page = object
    sys.modules["patchright"] = patchright_mod
    sys.modules["patchright.async_api"] = async_api_mod

if "pydantic" not in sys.modules:
    pydantic_mod = types.ModuleType("pydantic")

    class _BaseModel:
        def __init__(self, **kwargs) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    _missing = object()

    def _Field(default=_missing, default_factory=None, **_kwargs):
        if default_factory is not None:
            return default_factory()
        if default is _missing:
            return None
        return default

    pydantic_mod.BaseModel = _BaseModel
    pydantic_mod.Field = _Field
    sys.modules["pydantic"] = pydantic_mod

from src.chatgpt.client import ChatGPTClient
from src.config import Config

try:
    from patchright.async_api import async_playwright
except ImportError:
    async_playwright = None


async def _noop_sleep(*_args, **_kwargs) -> None:
    return None


class _FakeKeyboard:
    def __init__(self) -> None:
        self.presses: list[str] = []

    async def press(self, key: str) -> None:
        self.presses.append(key)


class _FakeMouse:
    def __init__(self) -> None:
        self.clicks: list[tuple[float, float]] = []

    async def move(self, _x: float, _y: float, **_kwargs) -> None:
        return None

    async def click(self, x: float, y: float) -> None:
        self.clicks.append((x, y))


class _FakePage:
    def __init__(self, open_picker: bool = False, visible_options: list[str] | None = None) -> None:
        self.keyboard = _FakeKeyboard()
        self.mouse = _FakeMouse()
        self.open_picker = open_picker
        self.visible_options = visible_options or []
        self.evaluate_calls: list[object] = []
        self.wait_for_selector_calls = 0

    async def wait_for_selector(self, *_args, **_kwargs):
        self.wait_for_selector_calls += 1
        raise RuntimeError("selector unavailable")

    async def evaluate(self, _script: str, arg=None):
        self.evaluate_calls.append(arg)

        if isinstance(arg, list):
            # _click_top_button_by_text includes generic picker hints.
            if "Configure" in arg or "model" in arg:
                return {"x": 100, "y": 100} if self.open_picker else None
            # _detect_current_model_label passes configured model labels.
            return ""

        # _click_menu_text passes a string target and should not find one here.
        if isinstance(arg, str):
            return False

        # _collect_visible_model_options passes no arg.
        return self.visible_options


class _ConfigureOnlyClient(ChatGPTClient):
    def __init__(self, page) -> None:
        super().__init__(page)
        self.configure_calls: list[tuple[tuple[str, ...], str]] = []
        self.wait_for_label_calls = 0

    async def _detect_current_model_label(self) -> str:
        return "Thinking"

    async def _open_model_picker(self, _target_label: str, current_label: str = "") -> bool:
        return True

    async def _click_model_option(self, _target_labels: tuple[str, ...]) -> bool:
        return False

    async def _click_menu_text(self, target_text: str) -> bool:
        return target_text == "Configure"

    async def _select_model_from_configure_dialog(
        self,
        target_labels: tuple[str, ...],
        target_version_label: str = "",
    ) -> bool:
        self.configure_calls.append((target_labels, target_version_label))
        return True

    async def _wait_for_model_label(self, _target_labels: tuple[str, ...]) -> bool:
        self.wait_for_label_calls += 1
        return False

    async def _dismiss_model_picker(self) -> None:
        return None


class _SettingClient(ChatGPTClient):
    def __init__(self, page) -> None:
        super().__init__(page)
        self.setting_calls: list[tuple[str, str]] = []

    async def _detect_current_model_label(self) -> str:
        return "Instant"

    async def _open_model_picker(self, _target_label: str, current_label: str = "") -> bool:
        return True

    async def _click_model_option(self, _target_labels: tuple[str, ...]) -> bool:
        return True

    async def _ensure_model_setting(self, option, setting_label: str) -> bool:
        self.setting_calls.append((option.public_id, setting_label))
        return True

    async def _ensure_configured_model_version(self, target_version_label: str) -> bool:
        self._last_model_version_label = target_version_label
        return True

    async def _wait_for_model_label(self, _target_labels: tuple[str, ...]) -> bool:
        return True

    async def _dismiss_model_picker(self) -> None:
        return None


class _AdvancedEffortClient(ChatGPTClient):
    def __init__(self, page) -> None:
        super().__init__(page)
        self.actions: list[str] = []
        self.advanced_open = False

    async def _dismiss_model_picker(self) -> None:
        self.actions.append("dismiss")

    async def _detect_current_model_label(self) -> str:
        return "Instant"

    async def _open_model_picker(self, _target_label: str, current_label: str = "") -> bool:
        self.actions.append(f"open:{current_label}")
        return True

    async def _click_menu_text(self, target_text: str) -> bool:
        self.actions.append(f"menu:{target_text}")
        if target_text == "Advanced":
            self.advanced_open = True
            return True
        if target_text == "Effort":
            return self.advanced_open
        return False

    async def _click_model_option(self, target_labels: tuple[str, ...]) -> bool:
        self.actions.append(f"option:{target_labels[0]}")
        return True


class ChatGPTClientModelSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_picker_selects_advanced_effort(self) -> None:
        client = _AdvancedEffortClient(_FakePage())  # type: ignore[arg-type]
        option = types.SimpleNamespace(ui_label="GPT-5.6 Sol")

        with patch("src.chatgpt.client.asyncio.sleep", _noop_sleep):
            selected = await client._ensure_advanced_effort(option, "Extra High")

        self.assertTrue(selected)
        self.assertEqual(
            client.actions,
            [
                "dismiss",
                "open:Instant",
                "menu:Effort",
                "menu:Advanced",
                "menu:Effort",
                "option:Extra High",
            ],
        )

    async def test_missing_model_option_falls_back_and_caches_when_not_strict(self) -> None:
        page = _FakePage(open_picker=True, visible_options=["GPT-5", "o3"])
        client = ChatGPTClient(page)  # type: ignore[arg-type]

        with patch.object(Config, "CHATGPT_MODEL_SWITCH_STRICT", False), patch(
            "src.chatgpt.client.asyncio.sleep",
            _noop_sleep,
        ):
            await client.ensure_model("gpt-5.5")
            evaluate_calls_after_first_attempt = len(page.evaluate_calls)
            await client.ensure_model("gpt-5.5")

        self.assertIn("gpt55", client._unavailable_model_keys)
        self.assertEqual(len(page.evaluate_calls), evaluate_calls_after_first_attempt)
        self.assertEqual(page.keyboard.presses, ["Escape", "Escape"])

    async def test_model_picker_open_failure_raises_when_strict(self) -> None:
        page = _FakePage(open_picker=False)
        client = ChatGPTClient(page)  # type: ignore[arg-type]

        with patch.object(Config, "CHATGPT_MODEL_SWITCH_STRICT", True), patch(
            "src.chatgpt.client.asyncio.sleep",
            _noop_sleep,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await client.ensure_model("gpt-5.5")

        self.assertIn("Could not open ChatGPT model picker", str(ctx.exception))
        self.assertEqual(page.keyboard.presses, ["Escape", "Escape"])

    async def test_version_option_uses_configure_dropdown_confirmation(self) -> None:
        page = _FakePage(open_picker=True)
        client = _ConfigureOnlyClient(page)  # type: ignore[arg-type]

        with patch.object(Config, "CHATGPT_MODEL_ALIASES", "gpt-5.4=5.4|GPT-5.4"), patch.object(
            Config,
            "CHATGPT_MODEL_SWITCH_STRICT",
            True,
        ), patch("src.chatgpt.client.asyncio.sleep", _noop_sleep):
            await client.ensure_model("gpt-5.4")

        self.assertEqual(client.configure_calls, [(("5.4", "GPT-5.4"), "5.4")])
        self.assertEqual(client._last_model_label, "5.4")
        self.assertEqual(client._last_model_version_label, "5.4")
        self.assertEqual(client.wait_for_label_calls, 0)

    def test_configure_version_click_labels_prioritize_dotted_version(self) -> None:
        client = ChatGPTClient(_FakePage())  # type: ignore[arg-type]

        labels = client._configure_version_click_labels(("Instant", "Latest 5.5", "GPT-5.5"), "5.5")

        self.assertEqual(labels[:3], ("5.5", "Instant", "Latest 5.5"))

    def test_model_setting_control_labels_prefer_mode_row(self) -> None:
        client = ChatGPTClient(_FakePage())  # type: ignore[arg-type]
        option = types.SimpleNamespace(
            public_id="gpt-5.4-thinking",
            ui_labels=("Thinking 5.4", "5.4 Thinking", "GPT-5.4 Thinking"),
        )

        labels = client._model_setting_control_labels(option)

        self.assertEqual(labels[:2], ("Thinking", "Thinking 5.4"))

    async def test_model_setting_control_clicks_generic_thinking_row(self) -> None:
        if async_playwright is None:
            self.skipTest("patchright is not installed")

        playwright_context = async_playwright()
        playwright = await playwright_context.__aenter__()
        try:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except Exception as exc:
                self.skipTest(f"browser runtime is not available: {exc}")

            try:
                page = await (await browser.new_context()).new_page()
                await page.set_content(
                    """
                    <!doctype html>
                    <style>
                      [role=menu] { position: relative; width: 220px; padding: 8px; }
                      [role=menuitemradio] {
                        position: relative;
                        width: 180px;
                        height: 32px;
                        padding: 6px 40px 6px 10px;
                      }
                      button { position: absolute; right: 6px; top: 5px; width: 26px; height: 24px; }
                    </style>
                    <div role="menu">
                      <div role="menuitemradio">Instant</div>
                      <div role="menuitemradio">Thinking<button aria-label="Settings"></button></div>
                      <div role="menuitemradio">Pro<button aria-label="Settings"></button></div>
                    </div>
                    """
                )
                await page.evaluate(
                    """
                    () => {
                      window.clickedSetting = "";
                      document.querySelectorAll("button").forEach((el) => {
                        el.addEventListener("click", () => {
                          window.clickedSetting = el.parentElement.textContent.trim();
                        });
                      });
                    }
                    """
                )
                client = ChatGPTClient(page)  # type: ignore[arg-type]

                with patch("src.chatgpt.client.asyncio.sleep", _noop_sleep):
                    clicked = await client._click_model_setting_control(("Thinking", "Thinking 5.4"))
                clicked_setting = await page.evaluate("window.clickedSetting")

                self.assertTrue(clicked)
                self.assertEqual(clicked_setting, "Thinking")
            finally:
                await browser.close()
        finally:
            await playwright_context.__aexit__(None, None, None)

    async def test_configure_radio_prefers_requested_mode_for_version(self) -> None:
        if async_playwright is None:
            self.skipTest("patchright is not installed")

        playwright_context = async_playwright()
        playwright = await playwright_context.__aenter__()
        try:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except Exception as exc:
                self.skipTest(f"browser runtime is not available: {exc}")

            try:
                page = await (await browser.new_context()).new_page()
                await page.set_content(
                    """
                    <!doctype html>
                    <div role="dialog">
                      <div role="radio" data-mode="instant">Instant 5.4</div>
                      <div role="radio" data-mode="thinking">Thinking 5.4</div>
                      <div role="radio" data-mode="pro">Pro 5.4</div>
                    </div>
                    """
                )
                await page.evaluate(
                    """
                    () => {
                      window.clickedMode = "";
                      document.querySelectorAll("[role=radio]").forEach((el) => {
                        el.addEventListener("click", () => { window.clickedMode = el.dataset.mode; });
                      });
                    }
                    """
                )
                client = ChatGPTClient(page)  # type: ignore[arg-type]

                clicked = await client._click_configure_radio_for_version(
                    "5.4",
                    target_labels=("Thinking 5.4", "5.4 Thinking"),
                )
                clicked_mode = await page.evaluate("window.clickedMode")

                self.assertTrue(clicked)
                self.assertEqual(clicked_mode, "thinking")
            finally:
                await browser.close()
        finally:
            await playwright_context.__aexit__(None, None, None)

    async def test_model_setting_is_applied_for_configured_thinking_mode(self) -> None:
        page = _FakePage(open_picker=True)
        client = _SettingClient(page)  # type: ignore[arg-type]

        with patch.object(
            Config,
            "CHATGPT_MODEL_ALIASES",
            "gpt-5.5-thinking=Thinking|5.5 Thinking",
        ), patch.object(
            Config,
            "CHATGPT_MODEL_SETTINGS",
            "gpt-5.5-thinking=Extended",
        ), patch.object(
            Config,
            "CHATGPT_MODEL_SWITCH_STRICT",
            True,
        ), patch("src.chatgpt.client.asyncio.sleep", _noop_sleep):
            await client.ensure_model("gpt-5.5-thinking")

        self.assertEqual(client.setting_calls, [("gpt-5.5-thinking", "Extended")])
        self.assertEqual(client._last_model_setting_by_key["gpt55thinking"], "Extended")

    async def test_model_picker_fallback_clicks_version_labeled_composer_button(self) -> None:
        if async_playwright is None:
            self.skipTest("patchright is not installed")

        playwright_context = async_playwright()
        chrome_path = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        launch_options = {"headless": True}
        if chrome_path.exists():
            launch_options["executable_path"] = str(chrome_path)

        playwright = await playwright_context.__aenter__()
        try:
            try:
                browser = await playwright.chromium.launch(**launch_options)
            except Exception as exc:
                self.skipTest(f"browser runtime is not available: {exc}")

            try:
                page = await (await browser.new_context()).new_page()
                await page.set_content(
                    """
                    <!doctype html>
                    <main style="min-height: 720px">
                      <nav>
                        <button aria-haspopup="menu" style="position:absolute;left:6px;top:132px">Recents</button>
                      </nav>
                      <button id="model-picker" aria-haspopup="menu"
                              style="position:absolute;left:910px;top:420px;width:90px;height:36px">5.1</button>
                    </main>
                    """
                )
                await page.evaluate(
                    """
                    () => {
                      window.clicked = "";
                      document.querySelector("#model-picker").addEventListener("click", () => {
                        window.clicked = "model-picker";
                        const menu = document.createElement("div");
                        menu.setAttribute("role", "menuitem");
                        menu.textContent = "Thinking";
                        document.body.appendChild(menu);
                      });
                    }
                    """
                )
                client = ChatGPTClient(page)  # type: ignore[arg-type]

                clicked = await client._click_model_picker_button_by_text(["5.1", "Thinking", "model"])
                selected = await page.evaluate("window.clicked")

                self.assertTrue(clicked)
                self.assertEqual(selected, "model-picker")
            finally:
                await browser.close()
        finally:
            await playwright_context.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
