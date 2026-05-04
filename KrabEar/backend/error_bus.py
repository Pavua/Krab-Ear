"""Structured error bus for surfacing silent failures to the UI.

KrabError is a Pydantic model. ErrorBus is a thread-safe pusher that
dedupes per-code, keeps a ring buffer for the Diagnostics tab, and routes
to Sentry by severity tier (info=skip, warn=batch, error/critical=immediate).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
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


class ErrorBus:
    """Thread-safe error pusher with dedupe, ring buffer, and event emission.

    Parameters
    ----------
    event_bus:
        Object with an ``emit(event_name: str, payload: dict)`` method.
    registry:
        Mapping of error code → dedupe window in seconds.
        Codes absent from the registry fall back to ``default_dedupe_window_sec``.
    sentry_client:
        Reserved for Task 4; not used yet.
    default_dedupe_window_sec:
        Window applied when a code is not found in ``registry``.
    ring_buffer_size:
        Maximum number of recent errors kept in memory.
    """

    def __init__(
        self,
        event_bus,
        registry: dict,
        sentry_client=None,
        default_dedupe_window_sec: float = 30.0,
        ring_buffer_size: int = 200,
    ) -> None:
        self._event_bus = event_bus
        self._registry: dict[str, float] = registry
        self._sentry_client = sentry_client
        self._default_dedupe_window_sec = default_dedupe_window_sec
        self._ring: deque[KrabError] = deque(maxlen=ring_buffer_size)
        # code -> last_emitted monotonic timestamp
        self._last_emitted: dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def push(self, err: KrabError) -> bool:
        """Push an error onto the bus.

        Returns ``True`` if the error was emitted (i.e. not deduped),
        ``False`` if it was suppressed within the dedupe window.
        """
        with self._lock:
            now = time.monotonic()
            window = self._dedupe_window_for(err.code)
            last = self._last_emitted.get(err.code)
            if last is not None and (now - last) < window:
                return False
            self._last_emitted[err.code] = now
            self._ring.append(err)

        # Emit outside the lock so event_bus callbacks can't dead-lock us.
        payload = err.model_dump(mode="json")
        self._event_bus.emit("krab_error", payload)
        self._route_to_sentry(err)  # TODO(Task 4): real Sentry tier routing
        return True

    def list_recent(self, limit: int = 200) -> list[KrabError]:
        """Return up to *limit* most-recent errors (oldest first)."""
        with self._lock:
            items = list(self._ring)
        return items[-limit:] if limit < len(items) else items

    def clear(self) -> int:
        """Clear the ring buffer and dedupe state. Returns count cleared."""
        with self._lock:
            count = len(self._ring)
            self._ring.clear()
            self._last_emitted.clear()
        return count

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dedupe_window_for(self, code: str) -> float:
        """Return the dedupe window (seconds) for *code*, falling back to default."""
        return self._registry.get(code, self._default_dedupe_window_sec)

    def _route_to_sentry(self, err: KrabError) -> None:  # noqa: ARG002
        """No-op stub — Task 4 will replace this body with real Sentry tier routing."""
        # TODO(Task 4): real Sentry tier routing
        return None
