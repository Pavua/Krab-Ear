"""Structured error bus for surfacing silent failures to the UI.

KrabError is a Pydantic model. ErrorBus is a thread-safe pusher that
dedupes per-code, keeps a ring buffer for the Diagnostics tab, and routes
to Sentry by severity tier (info=skip, warn=batch, error/critical=immediate).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, field_serializer

logger = logging.getLogger("KrabEar.Backend.ErrorBus")

Severity = Literal["info", "warn", "error", "critical"]
Component = Literal[
    "stt", "rewriter", "paste", "diarization",
    "translation", "mlx", "history", "vocabulary", "hotkey",
]


class KrabError(BaseModel):
    severity: Severity
    component: Component
    code: str
    message_user: str
    message_debug: str
    timestamp: datetime
    context: dict
    actionable: bool
    action_id: str | None

    @field_serializer("timestamp")
    def _serialise_timestamp(self, value: datetime, _info) -> str:
        # Ensure UTC offset is represented as +00:00, not Z, for IPC stability.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
