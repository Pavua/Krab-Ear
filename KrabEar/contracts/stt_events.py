"""Модели STT-событий Krab Ear."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class SttPartial(BaseModel):
    """Промежуточный текст во время записи."""

    text: str
    duration_sec: float | None = None


class SttFinal(BaseModel):
    """Финальная транскрипция."""

    history_id: str
    text: str
    duration_sec: float
    language: str | None = None
    confidence: float | None = None
    segments: list[dict[str, Any]] = []


class SttFailed(BaseModel):
    """Ошибка транскрипции."""

    reason: str
    duration_sec: float = 0.0
