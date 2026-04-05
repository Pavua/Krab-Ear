"""Реестр типов событий Krab Ear."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from contracts.stt_events import SttFailed, SttFinal, SttPartial
from contracts.translation_events import TranslationCompleted, TranslationFailed


class EventType(str, Enum):
    STT_PARTIAL = "stt.partial"
    STT_FINAL = "stt.final"
    STT_FAILED = "stt.failed"
    TRANSLATION_COMPLETED = "translation.completed"
    TRANSLATION_FAILED = "translation.failed"


EVENT_SCHEMA_MAP: dict[EventType, type[BaseModel]] = {
    EventType.STT_PARTIAL: SttPartial,
    EventType.STT_FINAL: SttFinal,
    EventType.STT_FAILED: SttFailed,
    EventType.TRANSLATION_COMPLETED: TranslationCompleted,
    EventType.TRANSLATION_FAILED: TranslationFailed,
}
