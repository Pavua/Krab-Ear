"""Реестр типов событий Krab Ear."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from contracts.history_events import AutoSummaryEvent, MarkdownExportEvent
from contracts.hotword_events import HotwordDetected
from contracts.live_subs_events import LiveSubsResult
from contracts.stt_events import SttFailed, SttFinal, SttPartial
from contracts.translation_events import TranslationCompleted, TranslationFailed


class EventType(str, Enum):
    STT_PARTIAL = "stt.partial"
    STT_FINAL = "stt.final"
    STT_FAILED = "stt.failed"
    TRANSLATION_COMPLETED = "translation.completed"
    TRANSLATION_FAILED = "translation.failed"
    MARKDOWN_EXPORT = "markdown_export"
    AUTO_SUMMARY = "auto_summary"
    HOTWORD_DETECTED = "hotword.detected"
    LIVE_SUBS_RESULT = "live_subs.result"


EVENT_SCHEMA_MAP: dict[EventType, type[BaseModel]] = {
    EventType.STT_PARTIAL: SttPartial,
    EventType.STT_FINAL: SttFinal,
    EventType.STT_FAILED: SttFailed,
    EventType.TRANSLATION_COMPLETED: TranslationCompleted,
    EventType.TRANSLATION_FAILED: TranslationFailed,
    EventType.MARKDOWN_EXPORT: MarkdownExportEvent,
    EventType.AUTO_SUMMARY: AutoSummaryEvent,
    EventType.HOTWORD_DETECTED: HotwordDetected,
    EventType.LIVE_SUBS_RESULT: LiveSubsResult,
}
