"""Opt-in метаданные IPC в ограниченной памяти; без logging, I/O и ожидания lock.

Измеряет только серверные этапы. send_finished означает возврат sendall,
не получение/валидацию клиентом и не завершение фоновых аудио-операций.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import os
import re
import threading
import time
import uuid

from backend.plaintext_export_redaction import safe_request_id

_ID = re.compile(r"ear-diag-[0-9a-f]{32}\Z")
_METHODS = frozenset({"ping", "get_recording_state", "get_meeting_live_state"})
_STAGES = frozenset({
    "accepted", "connection_started", "request_decoded", "worker_submitted",
    "worker_started", "handler_started", "handler_finished",
    "privacy_check_started", "privacy_check_finished", "audit_started",
    "audit_finished", "dispatch_finished", "response_serialized",
    "send_started", "send_finished", "send_failed", "backstop_timeout",
})
_CURRENT: ContextVar[Trace | None] = ContextVar("ipc_timing", default=None)


def clock() -> tuple[int, int]:
    """Монотонные часы для длительностей, UTC epoch ns только для корреляции."""
    return time.monotonic_ns(), time.time_ns()


@dataclass
class Trace:
    store: TimingStore
    request_id: str
    method: str
    events: list[dict] = field(default_factory=list)
    lost: bool = False
    ambiguous: bool = False


class TimingStore:
    """64 последних ID × 24 этапа; ни один caller не ждёт collector-lock."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self.epoch = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._traces: OrderedDict[str, Trace] = OrderedDict()
        self._begin_lost = False

    def begin(self, payload: dict) -> Trace | None:
        try:
            rid = payload.get("id")
            method = payload.get("method")
            if (not isinstance(rid, str) or not _ID.fullmatch(rid)
                    or not isinstance(method, str) or method not in _METHODS
                    or set(payload) - {"id", "method", "params", "signature", "timestamp", "nonce"}
                    or type(payload.get("params", {})) is not dict or payload.get("params")
                    or any(rid in str(payload.get(k, "")) for k in ("signature", "nonce"))
                    or safe_request_id(payload) != rid):
                return None
            trace = Trace(self, rid, method)
            if not self._lock.acquire(blocking=False):
                # Нельзя исключить duplicate уже завершённого ID. Sticky на
                # epoch: не ждём lock и не создаём неограниченную очередь ID.
                self._begin_lost = True
                return None
            try:
                previous = self._traces.get(rid)
                if previous is not None:
                    previous.ambiguous = True
                    return None
                if len(self._traces) >= 64:
                    _, evicted = self._traces.popitem(last=False)
                    evicted.events.clear()
                    evicted.lost = True
                self._traces[rid] = trace
                return trace
            finally:
                self._lock.release()
        except Exception:
            # Ошибка самой диагностики не меняет исходный RPC.
            return None

    def record(self, trace: Trace | None, stage: str,
               stamp: tuple[int, int] | None = None) -> None:
        if trace is None or trace.store is not self or self.pid != os.getpid():
            return
        try:
            if stage not in _STAGES:
                trace.lost = True
                return
            mono, utc = stamp if stamp is not None else clock()
            if not self._lock.acquire(blocking=False):
                trace.lost = True
                return
            try:
                if self._traces.get(trace.request_id) is not trace:
                    trace.lost = True
                    return
                if len(trace.events) >= 24:
                    trace.lost = True
                    return
                trace.events.append({"stage": stage, "monotonic_ns": mono, "utc_ns": utc})
            finally:
                self._lock.release()
        except Exception:
            trace.lost = True

    def snapshot(self, params: dict) -> dict:
        rid = params.get("request_id")
        if not isinstance(rid, str) or not _ID.fullmatch(rid):
            raise ValueError("request_id должен быть ear-diag-<32 lowercase hex>")
        base = {"schema": 1, "owner_pid": self.pid, "owner_epoch": self.epoch,
                "request_id": rid, "known": False, "state": "not_found", "events": []}
        if not self._lock.acquire(blocking=False):
            return {**base, "state": "contended"}
        try:
            trace = self._traces.get(rid)
            if trace is None:
                return base
            events = [dict(e) for e in trace.events]
            stages = {e["stage"] for e in events}
            incomplete = trace.lost or trace.ambiguous or self._begin_lost
            if incomplete:
                state = "incomplete"
            elif "send_failed" in stages:
                state = "send_failed"
            elif "backstop_timeout" in stages:
                state = "backstop_timeout"
            elif "send_finished" in stages:
                state = "complete"
            else:
                state = "partial"
            return {**base, "method": trace.method, "known": not incomplete,
                    "state": state, "events": events}
        finally:
            self._lock.release()


STORE = TimingStore()


def record(trace: Trace | None, stage: str, stamp: tuple[int, int] | None = None) -> None:
    if isinstance(trace, Trace):
        trace.store.record(trace, stage, stamp)


def mark(stage: str) -> None:
    record(_CURRENT.get(), stage)


def current() -> Trace | None:
    return _CURRENT.get()


@contextmanager
def bind(trace: Trace | None):
    token = _CURRENT.set(trace)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def snapshot(params: dict) -> dict:
    return STORE.snapshot(params)


def _after_fork() -> None:
    global STORE
    STORE = TimingStore()
    _CURRENT.set(None)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
