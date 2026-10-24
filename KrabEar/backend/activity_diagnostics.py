"""Пассивная component metadata: unknown не превращается в owner-idle/grant."""
from __future__ import annotations

import os
import threading
import uuid
import weakref
from functools import wraps
from typing import Any, Callable


_OWNER_EPOCH = uuid.uuid4().hex


def _after_fork() -> None:
    global _OWNER_EPOCH
    _OWNER_EPOCH = uuid.uuid4().hex


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def process_identity() -> dict:
    """Epoch инкарнации процесса, включая fork без повторного импорта."""
    return {"owner_pid": os.getpid(), "owner_epoch": _OWNER_EPOCH}


def unknown_component(reason: str = "metadata_unavailable") -> dict:
    return {"state": "unknown", "known": False, "busy_reasons": [reason]}


def nonblocking_snapshot(locks: tuple, read: Callable[[], dict]) -> dict:
    """Ни одного ожидания owner lock; read содержит только узкую metadata."""
    acquired = []
    try:
        for lock in locks:
            if not lock.acquire(blocking=False):
                return unknown_component("lock_contended")
            acquired.append(lock)
        return read()
    except Exception:
        return unknown_component()
    finally:
        for lock in reversed(acquired):
            lock.release()


def component_activity(service: Any) -> dict:
    try:
        result = service.activity_snapshot()
        if (not isinstance(result, dict) or type(result.get("known")) is not bool
                or result.get("state") not in ("idle", "busy", "unknown")):
            return unknown_component()
        return result
    except Exception:
        return unknown_component()


def owner_activity_snapshot(owner: str, components: dict, tracked: list[str]) -> dict:
    """Полное idle/released/CAS неизвестно до охвата ВСЕХ admission/warmup путей."""
    return {"schema": 1, "owner": owner, **process_identity(), "advisory": True,
            "generation": None, "generation_known": False,
            "state": "busy" if any(c.get("state") == "busy" for c in components.values()) else "unknown",
            "components": components,
            "coverage": {"complete": False, "tracked": tracked,
                         "untracked": ["background_warmup", "batch_stt", "other_admission"]}}


def ipc_activity_snapshot(recording: Any, meeting: Any) -> dict:
    return owner_activity_snapshot("backend", {
        "recording": component_activity(recording), "meeting": component_activity(meeting),
    }, ["recording", "finalizing", "meeting"])


def rest_activity_snapshot(observer: Any, tts: Any = None) -> dict:
    try:
        native = observer.snapshot()
        if (not isinstance(native, dict) or type(native.get("known")) is not bool
                or native.get("state") not in ("idle", "busy", "unknown")):
            raise ValueError("invalid activity metadata")
    except Exception:
        native = unknown_component()
    result = owner_activity_snapshot("rest", {
        "native_stt": native, "tts": component_activity(tts),
    }, ["native_stt_after_singleflight", "tts_handler"])
    result["coverage"]["untracked"].extend([
        "nested_adapter_futures", "ws_translation_emit", "tts_direct_synthesis",
    ])
    return result


class ActivityReservations:
    """Bounded observer callable/admission, не очередь и не разрешение lifecycle.

    Token принадлежит внешнему transcriber callable, а не HTTP response/client.
    Вложенные adapter futures и WS translation/emit остаются вне coverage. Здесь нет
    model lifecycle и возможности разрешить eviction. Generation относится
    только к этому tracked subset; overflow консервативно теряет coverage.
    """

    def __init__(self, *, generation_scope: str = "native_stt_after_singleflight",
                 completion_scope: str = "transcriber_callable",
                 busy_reason: str = "native_stt") -> None:
        # Только статические component labels из owning source, не request data.
        self._generation_scope = generation_scope
        self._completion_scope = completion_scope
        self._busy_reason = busy_reason
        self._reset_for_child()
        if hasattr(os, "register_at_fork"):
            reference = weakref.ref(self)

            def child_reset():
                observer = reference()
                if observer is not None:
                    observer._reset_for_child()

            os.register_at_fork(after_in_child=child_reset)

    def _reset_for_child(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[str, str]] = {}
        self._generation = 0
        self._coverage_lost = False

    def reserve(self, channel: str) -> str:
        token = uuid.uuid4().hex
        channel = channel if channel in ("post_stt", "native_ws") else "other"
        with self._lock:
            if len(self._tokens) < 256:
                self._tokens[token] = (channel, "reserved")
            else:
                self._coverage_lost = True
            self._generation += 1
        return token

    def _start(self, token: str) -> None:
        with self._lock:
            entry = self._tokens.get(token)
            if entry is not None:
                self._tokens[token] = (entry[0], "running")
                self._generation += 1

    def complete(self, token: str) -> None:
        with self._lock:
            if self._tokens.pop(token, None) is not None:
                self._generation += 1

    def mark_unknown(self, token: str) -> None:
        with self._lock:
            entry = self._tokens.get(token)
            if entry is not None and entry[1] != "unknown":
                self._tokens[token] = (entry[0], "unknown")
                self._generation += 1

    def run(self, token: str, fn: Callable, *args, **kwargs):
        self._start(token)
        try:
            return fn(*args, **kwargs)
        finally:
            self.complete(token)

    def tracked(self, channel: str) -> Callable:
        """Синхронный handler целиком, включая validation/отказ и finally cleanup."""
        def decorate(fn):
            @wraps(fn)
            def observed(*args, **kwargs):
                return self.run(self.reserve(channel), fn, *args, **kwargs)
            return observed
        return decorate

    def follow_future(self, token: str, future: Any) -> None:
        def done(completed):
            try:
                if completed.done() is True:
                    self.complete(token)
                else:
                    self.mark_unknown(token)
            except Exception:
                self.mark_unknown(token)

        try:
            future.add_done_callback(done)
        except Exception:
            # Нестандартный Future: нельзя считать timeout/cancel завершением.
            self.mark_unknown(token)

    def slot_callbacks(self, acquire: Callable, release: Callable, channel: str) -> tuple:
        """WS держит token до finally STT-участка, до translation/emit; не ws.close()."""
        local = threading.local()

        def tracked_acquire(timeout):
            if not acquire(timeout):
                return False
            token = self.reserve(channel)
            self._start(token)
            local.token = token
            return True

        def tracked_release():
            token = getattr(local, "token", None)
            if token is not None:
                del local.token
            try:
                release()
            finally:
                if token is not None:
                    self.complete(token)

        return tracked_acquire, tracked_release

    def snapshot(self) -> dict:
        def read():
            states = [state for _, state in self._tokens.values()]
            unknown = states.count("unknown")
            known = not self._coverage_lost and unknown == 0
            return {"known": known, "state": "busy" if states else ("idle" if known else "unknown"),
                    "reservations": len(states), "queued": states.count("reserved"),
                    "inflight": states.count("running"), "unknown": unknown,
                    "generation": self._generation, "generation_scope": self._generation_scope,
                    "completion_scope": self._completion_scope,
                    "coverage_lost": self._coverage_lost,
                    "busy_reasons": [self._busy_reason] if states else []}

        return nonblocking_snapshot((self._lock,), read)
