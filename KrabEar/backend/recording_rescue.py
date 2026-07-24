"""Восстановление незавершённых записей на старте backend (R1 Фаза 1, Task 4).

Сканирует ``<data_dir>/rescue/`` за ``.f32.part``-файлами, оставшимися от
предыдущей жизни процесса (см. ``backend/recording_spill.py``), собирает их
в WAV, уведомляет пользователя через ErrorBus и (вне privacy-режима)
транскрибирует через уже сконструированный ``RecordingCoreService``.
Единая точка входа — ``run_rescue_scan`` — вызывается фоновым тредом из
``service.py.__init__`` и НИКОГДА не бросает исключений наружу.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.recording_spill import finalize_part_to_wav

logger = logging.getLogger("KrabEar.Backend.RecordingRescue")

# Restart-шторм-гард: одновременно может выполняться только один скан.
_scan_lock = threading.Lock()

# Лимит на проход — единичный рестарт не должен утонуть в сотнях старых
# файлов; остаток подберёт следующий рестарт (спека §4.2).
_MAX_PER_PASS = 10

_RESCUED_COLLECTION_NAME = "Восстановленные записи"


def _read_source(part_path: Path) -> str:
    meta_path = part_path.with_name(part_path.name.replace(".f32.part", ".meta.json"))
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return str(meta.get("source", "unknown"))
    except Exception:
        return "unknown"


def _push_rescued_notice(error_bus: Any, wav_name: str, source: str) -> None:
    if error_bus is None:
        return
    try:
        from backend.error_bus import KrabError
        from backend.error_codes import ERROR_REGISTRY
        entry = ERROR_REGISTRY.get("audio.recording_rescued", {})
        err = KrabError(
            severity=entry.get("severity", "warn"),
            component="audio",
            code="audio.recording_rescued",
            message_user=entry.get(
                "user_msg_ru",
                "Найдена незавершённая запись — аудио восстановлено после сбоя",
            ),
            message_debug=f"rescued wav={wav_name} source={source}",
            timestamp=datetime.now(timezone.utc),
            # БЕЗ абсолютного пути в user-контексте — только имя файла.
            context={"wav": wav_name, "source": source},
            actionable=entry.get("actionable", False),
            action_id=entry.get("action_id"),
        )
        error_bus.push(err)
    except Exception:
        logger.exception("recording_rescue: error_bus.push провалился")


def _add_to_rescued_collection(collection_manager: Any, item_id: str) -> None:
    """Добавить восстановленную запись в коллекцию-украшение. Fail-open."""
    existing_names = {c.get("name") for c in collection_manager.list_collections()}
    if _RESCUED_COLLECTION_NAME not in existing_names:
        try:
            collection_manager.create_collection(_RESCUED_COLLECTION_NAME)
        except Exception:
            pass  # гонка с другим потоком / уже существует — не критично
    collection_manager.add_to_collection(_RESCUED_COLLECTION_NAME, item_id)


def _process_one(
    part_path: Path,
    recording_core: Any,
    error_bus: Any,
    settings_get: Callable[[str, Any], Any],
    collection_manager: Any,
    result: dict[str, int],
) -> None:
    source = _read_source(part_path)
    wav_path = finalize_part_to_wav(part_path)
    if wav_path is None:
        return
    result["rescued"] += 1
    _push_rescued_notice(error_bus, wav_path.name, source)

    if bool(settings_get("privacy_mode_enabled", False)):
        # Privacy-режим: WAV остаётся на диске, транскрипцию не запускаем.
        result["kept_wavs"] += 1
        return

    try:
        resp = recording_core.handle_transcribe_paths({"paths": [str(wav_path)]})
    except Exception:
        logger.exception("recording_rescue: handle_transcribe_paths провалился для %s", wav_path.name)
        result["kept_wavs"] += 1
        return

    processed = isinstance(resp, dict) and resp.get("processed", 0) > 0 and not resp.get("errors")
    if not processed:
        logger.warning("recording_rescue: транскрибация %s не дала результата: %r", wav_path.name, resp)
        result["kept_wavs"] += 1
        return

    result["transcribed"] += 1
    try:
        item_id = resp["items"][0]["history_id"]
    except Exception:
        item_id = None
    if item_id and collection_manager is not None:
        try:
            _add_to_rescued_collection(collection_manager, item_id)
        except Exception:
            logger.warning(
                "recording_rescue: не удалось добавить %s в коллекцию восстановленных",
                item_id, exc_info=True,
            )
    try:
        wav_path.unlink(missing_ok=True)
    except Exception:
        logger.warning("recording_rescue: не удалось удалить %s после транскрибации", wav_path.name, exc_info=True)


def run_rescue_scan(
    rescue_dir: "Path | str",
    recording_core: Any,
    error_bus: Any,
    settings_get: Callable[[str, Any], Any],
    collection_manager: Any,
) -> dict[str, int]:
    """Найти и восстановить незавершённые записи прошлой жизни процесса.

    Single-flight (module-level lock): конкурентный вызов немедленно
    возвращает нулевые счётчики — защита от шторма при быстрых рестартах.
    Никогда не бросает исключений: любая ошибка — WARN/exception-лог и
    переход к следующему файлу.
    """
    result = {"rescued": 0, "transcribed": 0, "kept_wavs": 0}
    if not _scan_lock.acquire(blocking=False):
        logger.info("recording_rescue: скан уже выполняется другим вызовом — пропуск")
        return result
    try:
        rescue_dir = Path(rescue_dir)
        if not rescue_dir.is_dir():
            return result
        try:
            parts = sorted(rescue_dir.glob("*.f32.part"))[:_MAX_PER_PASS]
        except Exception:
            logger.exception("recording_rescue: не удалось перечислить %s", rescue_dir)
            return result
        for part_path in parts:
            try:
                _process_one(part_path, recording_core, error_bus, settings_get, collection_manager, result)
            except Exception:
                logger.exception("recording_rescue: обработка %s провалилась", part_path.name)
    except Exception:
        logger.exception("recording_rescue: run_rescue_scan провалился целиком")
    finally:
        _scan_lock.release()
    return result
