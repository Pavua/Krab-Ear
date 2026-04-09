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
    paste_status: str = "failed"
    source_text: str = ""
    translated_text: str = ""
    translation_mode: str = "off"
    source_lang: str = ""
    target_lang: str = ""
    translation_status: str = "not_requested"
    translation_engine: str = ""
    chat_id: str = ""
    message_id: str = ""
    # D.10a: LLM rewrite tracking
    cleaned_text: str = ""
    llm_applied: bool = False
    llm_latency_ms: int = 0

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
        chat_id: str = "",
        message_id: str = "",
        cleaned_text: str = "",
        llm_applied: bool = False,
        llm_latency_ms: int = 0,
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
            chat_id=str(chat_id).strip(),
            message_id=str(message_id).strip(),
            cleaned_text=(cleaned_text or "").strip(),
            llm_applied=bool(llm_applied),
            llm_latency_ms=int(llm_latency_ms or 0),
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
            chat_id=str(payload.get("chat_id", "")).strip(),
            message_id=str(payload.get("message_id", "")).strip(),
            cleaned_text=str(payload.get("cleaned_text", "")).strip(),
            llm_applied=bool(payload.get("llm_applied", False)),
            llm_latency_ms=int(payload.get("llm_latency_ms", 0) or 0),
        )


from core.config import DEFAULT_SETTINGS
