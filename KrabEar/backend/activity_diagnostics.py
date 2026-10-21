"""Пассивная component metadata: unknown не превращается в owner-idle/grant."""
from __future__ import annotations

import os
import uuid
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
