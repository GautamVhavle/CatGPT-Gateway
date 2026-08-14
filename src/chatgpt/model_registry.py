"""
Model registry helpers for browser-backed ChatGPT model switching.

Maps public API model ids to the visible labels shown in ChatGPT's model picker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.config import Config

PUBLIC_BROWSER_MODEL_ID = "catgpt-browser"
_AUTO_MODEL_IDS = {
    "",
    "auto",
    "default",
    "browser",
    PUBLIC_BROWSER_MODEL_ID,
}


@dataclass(frozen=True)
class BrowserModelOption:
    """A public API model id paired with the ChatGPT UI label to click."""

    public_id: str
    ui_label: str
    alternate_labels: tuple[str, ...] = ()
    setting_label: str = ""

    @property
    def ui_labels(self) -> tuple[str, ...]:
        """All visible labels that may identify this model in ChatGPT's UI."""
        return (self.ui_label, *self.alternate_labels)


def normalize_model_token(value: str) -> str:
    """Normalize model ids / labels for resilient matching."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _parse_model_aliases(raw: str) -> list[BrowserModelOption]:
    """Parse a comma-separated alias list like `gpt-5.5=GPT-5.5,gpt-5.6-sol=GPT-5.6 Sol`."""
    options: list[BrowserModelOption] = []
    seen: set[str] = set()

    for chunk in (raw or "").split(","):
        item = chunk.strip()
        if not item:
            continue

        if "=" in item:
            public_id, labels = item.split("=", 1)
        else:
            public_id, labels = item, item

        public_id = public_id.strip()
        parsed_labels = tuple(label.strip() for label in labels.split("|") if label.strip())
        ui_label = parsed_labels[0] if parsed_labels else ""
        alternate_labels = parsed_labels[1:]
        normalized = normalize_model_token(public_id)
        if not public_id or not ui_label or not normalized or normalized in seen:
            continue

        options.append(
            BrowserModelOption(
                public_id=public_id,
                ui_label=ui_label,
                alternate_labels=alternate_labels,
            )
        )
        seen.add(normalized)

    return options


def _parse_model_settings(raw: str) -> dict[str, str]:
    """Parse effort mappings like `gpt-5.6-sol-high=High,gpt-5.5=Instant`."""
    settings: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        item = chunk.strip()
        if not item or "=" not in item:
            continue
        model_key, setting_label = item.split("=", 1)
        normalized = normalize_model_token(model_key)
        setting_label = setting_label.strip()
        if normalized and setting_label:
            settings[normalized] = setting_label
    return settings


def _apply_model_settings(options: list[BrowserModelOption], raw_settings: str) -> list[BrowserModelOption]:
    """Attach optional picker setting labels to configured model options."""
    settings = _parse_model_settings(raw_settings)
    if not settings:
        return options

    configured: list[BrowserModelOption] = []
    for option in options:
        lookup_keys = [
            normalize_model_token(option.public_id),
            *(normalize_model_token(label) for label in option.ui_labels),
        ]
        setting_label = next((settings[key] for key in lookup_keys if key in settings), "")
        if not setting_label:
            configured.append(option)
            continue
        configured.append(
            BrowserModelOption(
                public_id=option.public_id,
                ui_label=option.ui_label,
                alternate_labels=option.alternate_labels,
                setting_label=setting_label,
            )
        )
    return configured


def list_switchable_models() -> list[BrowserModelOption]:
    """Return the configured explicit browser-switchable models."""
    options = _parse_model_aliases(Config.CHATGPT_MODEL_ALIASES)
    return _apply_model_settings(options, Config.CHATGPT_MODEL_SETTINGS)


def list_public_chat_models() -> list[str]:
    """Return public model ids exposed via `/v1/models`."""
    model_ids = [PUBLIC_BROWSER_MODEL_ID]
    model_ids.extend(option.public_id for option in list_switchable_models())
    return model_ids


def is_supported_chat_model(model: str) -> bool:
    """Return whether a request model is supported by the browser gateway."""
    normalized = normalize_model_token(model)
    if normalized in {normalize_model_token(v) for v in _AUTO_MODEL_IDS}:
        return True

    for option in list_switchable_models():
        labels = {normalize_model_token(label) for label in option.ui_labels}
        if normalized in {normalize_model_token(option.public_id), *labels}:
            return True

    return False


def resolve_requested_model(model: str) -> BrowserModelOption | None:
    """
    Resolve the requested public model id to a ChatGPT UI label.

    `catgpt-browser` and other auto aliases only trigger a model switch when
    `CHATGPT_DEFAULT_MODEL` is configured to one of the explicit models.
    """
    normalized = normalize_model_token(model)
    if normalized in {normalize_model_token(v) for v in _AUTO_MODEL_IDS}:
        default_model = (Config.CHATGPT_DEFAULT_MODEL or "").strip()
        if not default_model:
            return None
        normalized = normalize_model_token(default_model)

    for option in list_switchable_models():
        labels = {normalize_model_token(label) for label in option.ui_labels}
        if normalized in {normalize_model_token(option.public_id), *labels}:
            return option

    return None

