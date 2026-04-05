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
from contracts.stt_events import SttFailed, SttFinal, SttPartial
from contracts.translation_events import TranslationCompleted, TranslationFailed

__all__ = [
    "EventType",
    "EVENT_SCHEMA_MAP",
    "KrabEventEnvelope",
    "UnknownEventType",
    "SttPartial",
    "SttFinal",
    "SttFailed",
    "TranslationCompleted",
    "TranslationFailed",
    "parse_event",
    "parse_and_validate",
]
