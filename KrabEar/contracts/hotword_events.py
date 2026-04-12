"""Модели событий обнаружения горячих слов Krab Ear."""

from __future__ import annotations

from pydantic import BaseModel


class HotwordMatch(BaseModel):
    word: str
    position: int
    category: str
    context: str


class HotwordDetected(BaseModel):
    """Событие: горячие слова найдены в транскрипции."""

    history_id: str
    text: str
    matches: list[HotwordMatch]
