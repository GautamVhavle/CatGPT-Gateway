from __future__ import annotations

import unittest
from unittest.mock import patch

from src.chatgpt import model_registry


class ModelRegistryTests(unittest.TestCase):
    def test_public_models_include_browser_alias_and_configured_models(self) -> None:
        with patch.object(model_registry.Config, "CHATGPT_MODEL_ALIASES", "gpt-5.3=GPT-5.3,o3=o3"):
            self.assertEqual(
                model_registry.list_public_chat_models(),
                ["catgpt-browser", "gpt-5.3", "o3"],
            )

    def test_default_models_match_current_advanced_picker(self) -> None:
        self.assertIn("gpt-5.6-sol", model_registry.list_public_chat_models())
        self.assertIn("gpt-5.6-sol-medium", model_registry.list_public_chat_models())
        self.assertIn("gpt-5.6-sol-high", model_registry.list_public_chat_models())
        self.assertIn("gpt-5.6-sol-extra-high", model_registry.list_public_chat_models())
        self.assertIn("gpt-5.6-sol-pro", model_registry.list_public_chat_models())
        self.assertIn("gpt-5.5", model_registry.list_public_chat_models())
        self.assertIn("gpt-5.5-medium", model_registry.list_public_chat_models())
        self.assertIn("gpt-5.5-high", model_registry.list_public_chat_models())
        self.assertIn("gpt-5.5-thinking", model_registry.list_public_chat_models())
        self.assertIn("gpt-5.5-extra-high", model_registry.list_public_chat_models())
        self.assertIn("gpt-5.5-pro", model_registry.list_public_chat_models())
        self.assertNotIn("o3", model_registry.list_public_chat_models())
        self.assertTrue(model_registry.is_supported_chat_model("Instant"))
        self.assertTrue(model_registry.is_supported_chat_model("Thinking"))
        self.assertTrue(model_registry.is_supported_chat_model("Pro"))
        self.assertTrue(model_registry.is_supported_chat_model("GPT-5.6 Sol"))
        self.assertTrue(model_registry.is_supported_chat_model("5.5"))
        self.assertTrue(model_registry.is_supported_chat_model("GPT-5.5"))

    def test_alias_parser_supports_alternate_ui_labels(self) -> None:
        with patch.object(model_registry.Config, "CHATGPT_MODEL_ALIASES", "gpt-5.5=Instant|Latest 5.5|5.5|GPT-5.5"):
            resolved = model_registry.resolve_requested_model("gpt-5.5")
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(resolved.ui_label, "Instant")
            self.assertEqual(resolved.alternate_labels, ("Latest 5.5", "5.5", "GPT-5.5"))

    def test_model_settings_attach_to_public_id_or_ui_label(self) -> None:
        with patch.object(
            model_registry.Config,
            "CHATGPT_MODEL_ALIASES",
            "gpt-5.5-thinking=Thinking|5.5 Thinking,gpt-5.5-pro=Pro",
        ), patch.object(
            model_registry.Config,
            "CHATGPT_MODEL_SETTINGS",
            "gpt-5.5-thinking=Extended,Pro=Standard",
        ):
            thinking = model_registry.resolve_requested_model("gpt-5.5-thinking")
            pro = model_registry.resolve_requested_model("gpt-5.5-pro")

        self.assertIsNotNone(thinking)
        self.assertIsNotNone(pro)
        assert thinking is not None
        assert pro is not None
        self.assertEqual(thinking.setting_label, "Extended")
        self.assertEqual(pro.setting_label, "Standard")

    def test_supported_model_accepts_public_id_and_ui_label(self) -> None:
        with patch.object(model_registry.Config, "CHATGPT_MODEL_ALIASES", "gpt-5.4=5.4|GPT-5.4"):
            self.assertTrue(model_registry.is_supported_chat_model("gpt-5.4"))
            self.assertTrue(model_registry.is_supported_chat_model("GPT-5.4"))

    def test_resolve_requested_model_uses_default_for_browser_alias(self) -> None:
        with patch.object(model_registry.Config, "CHATGPT_MODEL_ALIASES", "gpt-5.3=GPT-5.3,o3=o3"), patch.object(
            model_registry.Config,
            "CHATGPT_DEFAULT_MODEL",
            "o3",
        ):
            resolved = model_registry.resolve_requested_model("catgpt-browser")
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(resolved.public_id, "o3")
            self.assertEqual(resolved.ui_label, "o3")

    def test_resolve_requested_model_returns_none_for_browser_alias_without_default(self) -> None:
        with patch.object(model_registry.Config, "CHATGPT_MODEL_ALIASES", "gpt-5.3=GPT-5.3"), patch.object(
            model_registry.Config,
            "CHATGPT_DEFAULT_MODEL",
            "",
        ):
            self.assertIsNone(model_registry.resolve_requested_model("catgpt-browser"))


if __name__ == "__main__":
    unittest.main()
