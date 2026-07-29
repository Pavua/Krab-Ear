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
    "translation", "mlx", "history", "vocabulary", "hotkey", "ipc",
    # Wave 60 additions
    "disk", "audio", "agent",
    # Wave 61 additions
    "vgw",
    # Wave 64 additions
    "system",
    # Wave 490 additions
    "startup",
    # R2: lifecycle владения общим recorder.
    "recording",
    # M2: встроенный в backend REST-сервер (rest.port_conflict).
    "rest",
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


class WarnBatcher:
    """Accumulates warn-severity errors per code and flushes to Sentry in batches.

    Flush is triggered when either:
    - ``len(buffer[code]) >= batch_size``, or
    - ``(now - first_seen[code]) >= window`` seconds have elapsed since first arrival.

    Thread-safe via its own internal Lock (independent of ErrorBus._lock).

    Parameters
    ----------
    sentry_client:
        Duck-typed Sentry client; must expose
        ``capture_message(message, level=..., tags=..., extras=...)``.
    batch_size:
        Number of accumulated errors per code that triggers a flush (default 10).
    window:
        Time window in seconds after which a non-empty batch is flushed (default 30.0).
    """

    def __init__(self, sentry_client, batch_size: int = 10, window: float = 30.0) -> None:
        self._sentry = sentry_client
        self._batch_size = batch_size
        self._window = window
        self._lock = threading.Lock()
        self._buffer: dict[str, list[KrabError]] = {}
        self._first_seen: dict[str, float] = {}

    def add(self, err: KrabError) -> None:
        """Add a warn error to the batch buffer for its code; flush if threshold reached."""
        code = err.code
        with self._lock:
            now = time.monotonic()
            if code not in self._buffer:
                self._buffer[code] = []
                self._first_seen[code] = now
            self._buffer[code].append(err)

            # Flush if batch_size reached
            should_flush = len(self._buffer[code]) >= self._batch_size
            # Flush if window elapsed since first seen
            if not should_flush and (now - self._first_seen[code]) >= self._window:
                should_flush = True

            if should_flush:
                self._flush_locked(code)

    def flush_all(self) -> int:
        """Flush all pending warn batches to Sentry immediately.

        Intended for use at process shutdown to prevent silently dropping
        accumulated warn-tier errors.  Acquires the internal lock, iterates
        all pending codes, sends each batch via ``_flush_locked``, then clears
        the internal state.

        Returns:
            Total number of individual ``KrabError`` objects that were flushed.
        """
        with self._lock:
            total = sum(len(v) for v in self._buffer.values())
            for code in list(self._buffer.keys()):
                self._flush_locked(code)
        return total

    def _flush_locked(self, code: str) -> None:
        """Flush the buffer for *code* to Sentry. Must be called while holding self._lock."""
        batch = self._buffer.pop(code, [])
        self._first_seen.pop(code, None)
        if not batch:
            return
        latest = batch[-1]
        count = len(batch)
        summary = f"[warn batch x{count}] {latest.message_debug}"
        self._sentry.capture_message(
            summary,
            level="warning",
            tags={"phase": "b", "code": code, "component": latest.component},
            extras={"count": count, **latest.context},
        )


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
        Duck-typed Sentry client; used for tier-based routing.
        Pass ``None`` to disable Sentry integration entirely.
    default_dedupe_window_sec:
        Window applied when a code is not found in ``registry``.
    ring_buffer_size:
        Maximum number of recent errors kept in memory.
    warn_batch_size:
        Number of warn errors per code before flushing to Sentry (default 10).
    warn_window_sec:
        Time window (seconds) after which a non-empty warn batch is flushed (default 30.0).
    """

    def __init__(
        self,
        event_bus,
        registry: dict,
        sentry_client=None,
        default_dedupe_window_sec: float = 30.0,
        ring_buffer_size: int = 200,
        warn_batch_size: int = 10,
        warn_window_sec: float = 30.0,
    ) -> None:
        self._event_bus = event_bus
        # Registry value may be a scalar window (legacy) or an _Entry-shaped
        # dict from backend.error_codes — both are accepted by
        # ``_dedupe_window_for``.
        self._registry: dict = registry
        self._sentry = sentry_client
        self._default_dedupe_window_sec = default_dedupe_window_sec
        self._ring: deque[KrabError] = deque(maxlen=ring_buffer_size)
        # Parallel deque of monotonically increasing sequence numbers, one per
        # ring entry (never reset — including across ``clear()`` — so a poller
        # that persisted a ``since_seq`` across a ring clear never mistakes an
        # old high seq for "already seen" once the ring refills from empty).
        self._ring_seq: deque[int] = deque(maxlen=ring_buffer_size)
        self._next_seq = 0
        # code -> last_emitted monotonic timestamp
        self._last_emitted: dict[str, float] = {}
        self._lock = threading.Lock()
        self._warn_batcher: WarnBatcher | None = (
            WarnBatcher(sentry_client, batch_size=warn_batch_size, window=warn_window_sec)
            if sentry_client is not None
            else None
        )

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
            self._next_seq += 1
            self._ring.append(err)
            self._ring_seq.append(self._next_seq)

        # Emit outside the lock so event_bus callbacks can't dead-lock us.
        # NOTE: this ``emit`` only reaches subscribers in THIS process's
        # in-memory EventBus. Production runs the IPC backend (where every
        # ``push()`` call happens) and the REST server (which hosts the
        # ``/v1/events`` SSE stream) as two separate OS processes with two
        # separate ``EventBus`` instances — there is no bridge between them,
        # so this event never reaches an SSE subscriber. The native agent
        # instead polls ``list_recent_since`` over the IPC socket (see
        # WakeWordPoller for the sibling pattern; native/.../ErrorBusPoller.swift).
        payload = err.model_dump(mode="json")
        self._event_bus.emit("krab_error", payload)
        self._route_to_sentry(err)
        return True

    def list_recent(self, limit: int = 200) -> list[KrabError]:
        """Return up to *limit* most-recent errors (oldest first)."""
        with self._lock:
            items = list(self._ring)
        return items[-limit:] if limit < len(items) else items

    def latest_seq(self) -> int:
        """Return the current sequence counter (cheap; no ring copy)."""
        with self._lock:
            return self._next_seq

    def list_recent_since(self, since_seq: int = 0, limit: int = 200) -> tuple[list[KrabError], int]:
        """Return errors with seq > *since_seq* (oldest first), plus the current latest seq.

        ``since_seq=0`` returns the full ring (same items as ``list_recent``).
        Used for IPC poll-based delivery: a caller bootstraps with
        ``since_seq=0`` to learn ``latest_seq`` without necessarily treating
        the returned backlog as "new" (that policy lives in the poller, not
        here — see ``ErrorBusTracker`` on the Swift side), then passes the
        previously returned ``latest_seq`` on each subsequent poll.
        """
        with self._lock:
            items = list(self._ring)
            seqs = list(self._ring_seq)
            latest = self._next_seq
        filtered = [err for err, seq in zip(items, seqs) if seq > since_seq]
        if limit < len(filtered):
            filtered = filtered[-limit:]
        return filtered, latest

    def clear(self) -> int:
        """Clear the ring buffer, dedupe state, and WarnBatcher state. Returns count cleared.

        ``_next_seq`` is intentionally NOT reset — it must stay monotonic for
        the life of the process so a poller's stale ``since_seq`` can never
        be misread as "newer than" a freshly re-pushed error.
        """
        with self._lock:
            count = len(self._ring)
            self._ring.clear()
            self._ring_seq.clear()
            self._last_emitted.clear()
        if self._warn_batcher is not None:
            with self._warn_batcher._lock:
                self._warn_batcher._buffer.clear()
                self._warn_batcher._first_seen.clear()
        return count

    def flush_all(self) -> int:
        """Flush all pending warn-tier batches to Sentry immediately.

        Delegates to ``WarnBatcher.flush_all()`` when a batcher is configured.
        Safe to call when Sentry is disabled (``_warn_batcher is None``).

        Returns:
            Number of individual ``KrabError`` objects flushed (0 if no batcher).
        """
        if self._warn_batcher is None:
            return 0
        return self._warn_batcher.flush_all()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dedupe_window_for(self, code: str) -> float:
        """Return the dedupe window (seconds) for *code*, falling back to default.

        Accepts two registry shapes:
        - Flat ``{code: seconds}`` (legacy / test fixtures).
        - Canonical ``ERROR_REGISTRY`` from ``backend.error_codes`` where each
          entry is a ``_Entry`` TypedDict containing ``dedupe_seconds``.
        """
        entry = self._registry.get(code)
        if entry is None:
            return self._default_dedupe_window_sec
        if isinstance(entry, dict):
            value = entry.get("dedupe_seconds", self._default_dedupe_window_sec)
            return float(value)
        return float(entry)

    def _route_to_sentry(self, err: KrabError) -> None:
        """Route error to Sentry according to severity tier.

        - info   → skip (never sent)
        - warn   → accumulate in WarnBatcher; flush at batch_size or window threshold
        - error  → immediate capture_message(level="error")
        - critical → immediate capture_message(level="critical")
        """
        if self._sentry is None or err.severity == "info":
            return
        if err.severity == "warn":
            if self._warn_batcher:
                self._warn_batcher.add(err)
            return
        # error / critical — immediate
        self._sentry.capture_message(
            err.message_debug,
            level=err.severity,
            tags={"phase": "b", "code": err.code, "component": err.component},
            extras=err.context,
        )
