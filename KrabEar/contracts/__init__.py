"""Контрактные модели событий Krab Ear.

Публичный API модуля — импортируйте модели и утилиты отсюда:

    from contracts import SttFinal, EventType, parse_and_validate
"""

from contracts.envelope import (
    KrabEventEnvelope,
    UnknownEventType,
    parse_and_validate,
    parse_event,
)
from contracts.registry import EVENT_SCHEMA_MAP, EventType
from contracts.history_events import AutoSummaryEvent, MarkdownExportEvent
from contracts.hotword_events import HotwordDetected
from contracts.live_subs_events import LiveSubsResult
from contracts.stt_events import SttFailed, SttFinal, SttPartial
from contracts.translation_events import TranslationCompleted, TranslationFailed

__all__ = [
    "AutoSummaryEvent",
    "EVENT_SCHEMA_MAP",
    "EventType",
    "HotwordDetected",
    "KrabEventEnvelope",
    "LiveSubsResult",
    "MarkdownExportEvent",
    "SttFailed",
    "SttFinal",
    "SttPartial",
    "TranslationCompleted",
    "TranslationFailed",
    "UnknownEventType",
    "parse_and_validate",
    "parse_event",
]
