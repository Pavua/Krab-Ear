"""Модели событий истории Krab Ear."""

from __future__ import annotations

from pydantic import BaseModel


class MarkdownExportEvent(BaseModel):
    """Экспорт истории в Markdown."""

    entries: int
    chars: int
    copy_to_clipboard: bool


class AutoSummaryEvent(BaseModel):
    """Авто-суммаризация пакета истории."""

    items_processed: int
    total_words: int
    fallback: bool
    summary: str
