"""Конверт событий экосистемы Krab и функции валидации."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from contracts.registry import EVENT_SCHEMA_MAP, EventType


class UnknownEventType(Exception):
    """Тип события не зарегистрирован в реестре Krab Ear."""

    def __init__(self, event_type: str) -> None:
        self.event_type = event_type
        super().__init__(f"Unknown event type: {event_type}")


class KrabEventEnvelope(BaseModel):
    """Унифицированный конверт события экосистемы Krab."""

    type: str
    ts: datetime
    data: dict[str, Any]


def parse_event(raw: dict[str, Any]) -> KrabEventEnvelope:
    """Парсит сырой dict в конверт. Не валидирует data — только структуру."""
    return KrabEventEnvelope.model_validate(raw)


def parse_and_validate(raw: dict[str, Any]) -> tuple[EventType, BaseModel]:
    """Парсит конверт + валидирует data по реестру.

    Raises:
        UnknownEventType: тип события не зарегистрирован (чужой домен).
        ValidationError: data не соответствует схеме.
    """
    env = parse_event(raw)
    try:
        etype = EventType(env.type)
    except ValueError:
        raise UnknownEventType(env.type)
    model_cls = EVENT_SCHEMA_MAP[etype]
    return etype, model_cls.model_validate(env.data)
