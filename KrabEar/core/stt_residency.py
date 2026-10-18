"""Наблюдение cached STT owner без загрузки моделей или child IPC."""
from __future__ import annotations

import re
import sys
from typing import Any


def public_model_id(value: Any) -> str | None:
    """Только имя/repo ID: локальные пути в metadata не передаём."""
    if not isinstance(value, str) or len(value) > 256:
        return None
    parts = value.split("/")
    if len(parts) not in (1, 2) or any(p in ("", ".", "..") for p in parts):
        return None
    return value if re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", value) else None


def empty_snapshot(state: str = "unloaded", transport: str | None = None) -> dict:
    return {"state": state, "model": None, "transport": transport, "pid": None, "inflight": None}


def whisper_snapshot() -> dict:
    """Снимок наблюдения, не гарантия готовности следующего запроса."""
    from core.mlx_whisper_session import mlx_whisper_worker_enabled, whisper_residency_snapshot

    if mlx_whisper_worker_enabled():
        return whisper_residency_snapshot()
    from core.mlx_lock import mlx_lock

    lock = mlx_lock()
    if not lock.acquire(blocking=False):
        return empty_snapshot("busy", "in_process")
    try:
        # Не импортируем mlx_whisper, не вызываем get_model и не вычисляем
        # bool/repr GPU object. Читаем только уже существующий holder.
        module = sys.modules.get("mlx_whisper.transcribe")
        holder = getattr(module, "ModelHolder", None)
        model = getattr(holder, "model", None)
        result = empty_snapshot("loaded" if model is not None else "unloaded", "in_process")
        result["model"] = public_model_id(getattr(holder, "model_path", None)) if model is not None else None
        return result
    finally:
        lock.release()
