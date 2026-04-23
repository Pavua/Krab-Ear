"""Модели событий Live Subtitles (Sprint 2B)."""

from __future__ import annotations

from pydantic import BaseModel


class LiveSubsResult(BaseModel):
    """Результат потоковой транскрипции + перевода для живых субтитров."""

    text: str
    translation: str | None = None
    start_ts: float
    end_ts: float
    language_detected: str | None = None
