"""Модели данных backend-сервиса Krab Ear.

Модуль используется сервисом IPC и хранилищем истории для единообразной
сериализации/десериализации объектов.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any
import uuid


@dataclass(slots=True)
class HistoryItem:
    """Одна запись транскрибации в истории."""

    id: str
    ts: str
    text: str
    paste_status: str
    source_text: str = ""
    translated_text: str = ""
    translation_mode: str = "off"
    source_lang: str = ""
    target_lang: str = ""
    translation_status: str = "not_requested"
    translation_engine: str = ""

    @classmethod
    def create(
        cls,
        text: str,
        paste_status: str = "failed",
        source_text: str = "",
        translated_text: str = "",
        translation_mode: str = "off",
        source_lang: str = "",
        target_lang: str = "",
        translation_status: str = "not_requested",
        translation_engine: str = "",
    ) -> "HistoryItem":
        """Создаёт новую запись с корректным идентификатором и временем."""
        return cls(
            id=str(uuid.uuid4()),
            ts=datetime.now().isoformat(timespec="seconds"),
            text=text,
            paste_status=paste_status,
            source_text=source_text.strip(),
            translated_text=translated_text.strip(),
            translation_mode=translation_mode.strip() or "off",
            source_lang=source_lang.strip(),
            target_lang=target_lang.strip(),
            translation_status=translation_status.strip() or "not_requested",
            translation_engine=translation_engine.strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        """Преобразует dataclass в сериализуемый словарь."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "HistoryItem":
        """Восстанавливает запись из JSON-словаря с мягкой валидацией."""
        return cls(
            id=str(payload.get("id", "")).strip(),
            ts=str(payload.get("ts", "")).strip(),
            text=str(payload.get("text", "")).strip(),
            paste_status=str(payload.get("paste_status", "failed")).strip() or "failed",
            source_text=str(payload.get("source_text", "")).strip(),
            translated_text=str(payload.get("translated_text", "")).strip(),
            translation_mode=str(payload.get("translation_mode", "off")).strip() or "off",
            source_lang=str(payload.get("source_lang", "")).strip(),
            target_lang=str(payload.get("target_lang", "")).strip(),
            translation_status=str(payload.get("translation_status", "not_requested")).strip() or "not_requested",
            translation_engine=str(payload.get("translation_engine", "")).strip(),
        )


DEFAULT_SETTINGS: dict[str, Any] = {
    "mode": "headless",
    "show_dock_icon": True,
    "auto_start_enabled": False,
    "auto_paste": True,
    "play_start_sound": True,
    "quality_profile": "balanced",
    "network_mode": "offline_default",
    "hotkey": "right_option_toggle",
    "hotkey_profile": "default",
    "history_policy": "unlimited",
    "history_page_size": 50,
    "history_text_density": "normal",
    "realtime_preview_enabled": True,
    "cleanup_profile": "soft",
    "translation_mode": "off",
    "translate_and_paste": False,
    "translation_style": "neutral",
    "translation_glossary": {},
    "text_templates": {
        "follow_up_ru": "Здравствуйте! Подтверждаю: {text}. Следующий шаг: {next_step}.",
        "follow_up_es": "Hola. Confirmo: {text}. Siguiente paso: {next_step}.",
    },
    "clipboard_mode": "always_copy",
    "audio_ducking_enabled": True,
    "audio_ducking_percent": 50,
    "overlay_opacity_percent": 45,
    "voice_gateway_url": "http://127.0.0.1:8090",
    "voice_gateway_api_key": "",
    "update_channel": "stable",
    "call_notify_default": True,
    "call_auto_summary": True,
    "call_budget_usd": 2.0,
    "call_quick_templates": [
        {
            "name": "Повтори медленно",
            "text": "Повторите, пожалуйста, медленнее.",
            "source_lang": "ru",
            "target_lang": "es",
        },
        {
            "name": "Жду ответ",
            "text": "Буду ждать вашего ответа до конца дня.",
            "source_lang": "ru",
            "target_lang": "ru",
        },
    ],
    "capture_source_mode": "mic",
    "ui_last_tab": "history",
    "history_focus_mode": True,
    "onboarding_completed": False,
}
