"""Модели событий перевода Krab Ear."""

from __future__ import annotations

from pydantic import BaseModel


class TranslationCompleted(BaseModel):
    """Перевод готов."""

    history_id: str
    source_text: str
    translated_text: str
    source_lang: str
    target_lang: str
    engine: str
    mode: str


class TranslationFailed(BaseModel):
    """Ошибка перевода."""

    history_id: str | None = None
    source_text: str
    reason: str
    source_lang: str | None = None
    target_lang: str | None = None
