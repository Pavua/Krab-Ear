"""Наблюдение cached STT owner без загрузки моделей или child IPC."""
from __future__ import annotations

import sys
from typing import Any


# Точные публичные IDs стандартных Whisper-моделей из core/config.py.
# Произвольный repo-подобный текст может быть относительным private path.
_PUBLIC_MODEL_IDS = frozenset({
    "mlx-community/whisper-large-v3-mlx",
    "mlx-community/whisper-large-v3-turbo",
})


def public_model_id(value: Any) -> str | None:
    """Неизвестное происхождение raw path/name — неизвестная metadata."""
    return value if isinstance(value, str) and value in _PUBLIC_MODEL_IDS else None


def empty_snapshot(state: str = "unloaded", transport: str | None = None) -> dict:
    return {"state": state, "model": None, "transport": transport, "pid": None, "inflight": None}


def whisper_snapshot() -> dict:
    """Наблюдает обоих existing owners, независимо от текущей настройки."""
    from core.mlx_whisper_session import mlx_whisper_worker_enabled, whisper_residency_snapshot

    configured = "worker" if mlx_whisper_worker_enabled() else "in_process"
    in_process = _in_process_whisper_snapshot()
    worker = whisper_residency_snapshot()
    states = {in_process["state"], worker["state"]}
    # Не скрываем retained owner после hot reload transport. Unknown/busy
    # консервативнее loaded; unloaded требует двух наблюдений unloaded.
    state = next(value for value in ("busy", "unknown", "loaded", "unloaded") if value in states)
    return {"configured_transport": configured, "state": state,
            "in_process": in_process, "worker": worker}


def _in_process_whisper_snapshot() -> dict:
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
