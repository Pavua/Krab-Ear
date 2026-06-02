"""BulkReprocessor — массовое перетранскрибирование истории с текущими настройками STT.

Сценарий: пользователь накопил историю транскрипций. После добавления новых STT-фич
(multipass, denoising, RU fine-tune, GigaAM и т.п.) старые записи остались с худшим
качеством. BulkReprocessor позволяет перетранскрибировать их все разом.

Гарантии:
- Всегда сохраняет предыдущий текст через TranscriptVersionManager (нет потери данных).
- Новый текст применяется только если confidence вырос (или only_low_confidence=False).
- Cancellable: проверяет _cancel_event перед каждой записью.
- Dry-run: планирует, что будет сделано, без реальных изменений.
- Hard limit 1000 записей за один запуск.
- Skip: записи младше 1 часа, записи без аудиофайла, защищённые записи.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from backend.state_store import StateStore
    from backend.transcriber import Transcriber
    from backend.transcript_versioning import TranscriptVersionManager
    from backend.event_bus import EventBus

logger = logging.getLogger("KrabEar.Backend.BulkReprocess")

# Абсолютный лимит: не более N записей за один запуск.
HARD_LIMIT = 1000
# Записи моложе этого порога пропускаются (секунды).
MIN_AGE_SEC = 3600  # 1 час


class BulkReprocessor:
    """Массовое перетранскрибирование истории с текущими настройками STT.

    Параметры конструктора:
        store: Хранилище истории (StateStore).
        transcriber: Движок STT (Transcriber).
        version_manager: Менеджер версий транскрипций (TranscriptVersionManager).
        event_bus: Шина событий для emit прогресса (опционально).
        batch_size: Частота emit событий прогресса (каждые N записей).
    """

    def __init__(
        self,
        store: "StateStore",
        transcriber: "Transcriber",
        version_manager: "TranscriptVersionManager",
        event_bus: "EventBus | None" = None,
        batch_size: int = 5,
        is_recording_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.store = store
        self.transcriber = transcriber
        self.version_manager = version_manager
        self.event_bus = event_bus
        self.batch_size = max(1, int(batch_size))
        self._cancel_event = threading.Event()
        self._is_recording_fn = is_recording_fn
        # Защита от параллельных запусков. BackendService держит ОДИН общий singleton
        # self._bulk_reprocessor (service.py:530), а IPC — thread-per-connection, причём
        # bulk_reprocess_start НЕ в HEAVY_METHODS (light-лимит 120/мин). Два клиента могли
        # запустить два reprocess()-цикла по ОДНОМУ набору кандидатов: (a) дубль MLX
        # транскрибаций + гонка last-writer-wins на update_history_item_text для одного id;
        # (b) сломанная отмена — re-entry второго запуска вызывал _reset_cancel()/clear() и
        # стирал pending cancel первого, либо один cancel() обрывал ОБА. Non-blocking acquire
        # _run_lock гарантирует ровно один активный цикл; второй вызов сразу возвращает error
        # без старта второго цикла, не трогая cancel-флаг активного запуска.
        self._run_lock = threading.Lock()

    def cancel(self) -> None:
        """Запрашивает отмену текущего запуска (проверяется между записями)."""
        self._cancel_event.set()

    def is_running(self) -> bool:
        """True, если в данный момент идёт reprocess()-цикл (без побочных эффектов)."""
        # Неблокирующая проба _run_lock: если получили — цикла нет, сразу отпускаем.
        if self._run_lock.acquire(blocking=False):
            self._run_lock.release()
            return False
        return True

    def _reset_cancel(self) -> None:
        """Сбрасывает флаг отмены перед новым запуском.

        Безопасность отмены: сброс разрешён ТОЛЬКО когда нет активного цикла (т.е. _run_lock
        свободен). Иначе re-entry-попытка второго клиента могла бы стереть pending cancel для
        уже идущего запуска (исходный баг). Владелец активного цикла сбрасывает cancel сам,
        напрямую через _cancel_event.clear() под удерживаемым _run_lock в reprocess(), минуя
        этот публичный метод.
        """
        if self.is_running():
            # Цикл уже идёт (lock держит активный владелец) — не трогаем cancel.
            logger.debug("Bulk reprocess: _reset_cancel пропущен — цикл активен.")
            return
        self._cancel_event.clear()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _item_age_sec(self, item_ts: str) -> float:
        """Возвращает возраст записи в секундах."""
        try:
            dt = datetime.fromisoformat(item_ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds()
        except (ValueError, TypeError):
            return float("inf")

    def _load_audio(self, audio_path: str) -> Any:
        """Загружает аудиофайл в массив numpy для передачи в transcriber."""
        try:
            import soundfile as sf  # type: ignore
            data, sample_rate = sf.read(audio_path, dtype="float32", always_2d=False)
            # Whisper ожидает моно-float32.
            if data.ndim > 1:
                data = data.mean(axis=1)
            # Ресемплируем до 16 kHz если нужно.
            if sample_rate != 16000:
                import numpy as np
                target_len = int(len(data) * 16000 / sample_rate)
                data = np.interp(
                    np.linspace(0, len(data), target_len),
                    np.arange(len(data)),
                    data,
                )
            return data
        except Exception as exc:
            raise RuntimeError(f"Не удалось загрузить аудио {audio_path!r}: {exc}") from exc

    def _emit_progress(
        self,
        task_id: str,
        processed: int,
        total: int,
        reprocessed: int,
        skipped: int,
        errors: list[str],
    ) -> None:
        """Эмитирует событие прогресса через event_bus."""
        if self.event_bus is None:
            return
        try:
            self.event_bus.emit("bulk_reprocess_progress", {
                "task_id": task_id,
                "processed": processed,
                "total": total,
                "reprocessed": reprocessed,
                "skipped": skipped,
                "error_count": len(errors),
            })
        except Exception as exc:
            logger.warning("Не удалось эмитировать прогресс bulk_reprocess: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reprocess(
        self,
        filter_kwargs: dict[str, Any] | None = None,
        *,
        only_low_confidence: bool = True,
        threshold: float = 0.7,
        dry_run: bool = False,
        task_id: str = "",
    ) -> dict[str, Any]:
        """Запускает массовое перетранскрибирование истории.

        Args:
            filter_kwargs: Дополнительные параметры для фильтрации (зарезервировано).
            only_low_confidence: Пропускать записи с confidence >= threshold.
            threshold: Порог confidence (0.0–1.0).
            dry_run: Если True — только считает, без реального STT и сохранения.
            task_id: ID задачи для emit событий прогресса.

        Returns:
            {
                "total": N,       # всего кандидатов (с аудио, не защищённых, не молодых)
                "reprocessed": M, # успешно перетранскрибировано
                "skipped": K,     # пропущено (confidence уже ок, нет улучшения, dry_run skip)
                "errors": [...],  # ошибки по конкретным записям
                "cancelled": bool,
            }
        """
        # Guard: refuse to run while active recording is in progress.
        # Competing with an ongoing recording for MLX GPU → potential SIGSEGV (PR #71 class).
        if self._is_recording_fn is not None and self._is_recording_fn():
            raise RuntimeError("bulk_reprocess refused: active recording in progress")

        # Guard: ровно один активный цикл на общий singleton (см. __init__).
        # Non-blocking acquire — второй параллельный вызов сразу возвращает error без старта
        # второго цикла и БЕЗ сброса cancel-флага активного запуска (иначе re-entry стёр бы
        # pending cancel или один cancel оборвал бы оба прохода).
        if not self._run_lock.acquire(blocking=False):
            logger.warning("Bulk reprocess: запуск отклонён — цикл уже выполняется.")
            return {
                "error": "bulk_reprocess already running",
                "total": 0,
                "reprocessed": 0,
                "skipped": 0,
                "errors": [],
                "cancelled": False,
            }
        try:
            return self._run_locked(
                only_low_confidence=only_low_confidence,
                threshold=threshold,
                dry_run=dry_run,
                task_id=task_id,
            )
        finally:
            self._run_lock.release()

    def _run_locked(
        self,
        *,
        only_low_confidence: bool,
        threshold: float,
        dry_run: bool,
        task_id: str,
    ) -> dict[str, Any]:
        """Тело перетранскрибирования. Вызывается ТОЛЬКО под удерживаемым _run_lock.

        Контракт отмены: cancel-флаг сбрасывается здесь напрямую (clear()), потому что мы —
        единственный активный владелец цикла (lock держим). Публичный _reset_cancel() при этом
        для других потоков становится no-op (см. его docstring), поэтому re-entry не сотрёт
        cancel, запрошенный для этого прохода.
        """
        self._cancel_event.clear()
        errors: list[str] = []
        reprocessed = 0
        skipped = 0
        cancelled = False

        # Загружаем все активные записи.
        with self.store._lock():
            all_items = self.store._load_active_items_unlocked()

        # Фильтруем кандидатов.
        candidates = []
        for item in all_items:
            # Skip: нет audio_path.
            audio_path = str(getattr(item, "audio_path", "") or "").strip()
            if not audio_path or not os.path.isfile(audio_path):
                continue

            # Skip: защищённая запись.
            if getattr(item, "is_protected", False):
                continue

            # Skip: запись моложе 1 часа.
            if self._item_age_sec(item.ts) < MIN_AGE_SEC:
                continue

            candidates.append((item, audio_path))

        # Hard limit.
        if len(candidates) > HARD_LIMIT:
            logger.warning(
                "Bulk reprocess: %d кандидатов превышают hard limit %d; обрезаем.",
                len(candidates),
                HARD_LIMIT,
            )
            candidates = candidates[:HARD_LIMIT]

        total = len(candidates)
        logger.info(
            "Bulk reprocess start: total=%d dry_run=%s only_low_conf=%s threshold=%.2f",
            total,
            dry_run,
            only_low_confidence,
            threshold,
        )

        for idx, (item, audio_path) in enumerate(candidates):
            # Cancellation check.
            if self._cancel_event.is_set():
                cancelled = True
                logger.info("Bulk reprocess отменён на записи %d/%d.", idx + 1, total)
                break

            item_id = item.id
            old_confidence = item.confidence if item.confidence is not None else 0.0

            # Фильтр по confidence.
            if only_low_confidence and item.confidence is not None and item.confidence >= threshold:
                skipped += 1
                logger.debug(
                    "Bulk reprocess skip (conf=%.3f >= %.3f): %s", item.confidence, threshold, item_id
                )
                # Emit прогресс.
                if (idx + 1) % self.batch_size == 0:
                    self._emit_progress(task_id, idx + 1, total, reprocessed, skipped, errors)
                continue

            if dry_run:
                # В dry_run считаем как "reprocessed" (что будет сделано).
                reprocessed += 1
                if (idx + 1) % self.batch_size == 0:
                    self._emit_progress(task_id, idx + 1, total, reprocessed, skipped, errors)
                continue

            # Реальное перетранскрибирование.
            try:
                audio_data = self._load_audio(audio_path)
                # Defense-in-depth: serialise MLX GPU access even if the caller
                # didn't inject is_recording_fn.  Per project rule (PR #71).
                # W1635: also acquire mlx_inter_process_lock for cross-process safety.
                from core.mlx_lock import mlx_lock
                from core.mlx_inter_lock import mlx_inter_process_lock
                with mlx_inter_process_lock(), mlx_lock():  # W1635: cross-process flock + intra-process RLock
                    result = self.transcriber.transcribe(
                        audio_data,
                        quality_profile="balanced",
                        cleanup_profile="soft",
                        lang_hint=item.source_lang or None,
                    )
                new_text = str(result.get("text") or "").strip()
                new_confidence = float(result.get("confidence") or 0.0)

                if not new_text:
                    skipped += 1
                    errors.append(f"{item_id}: пустой результат STT")
                    if (idx + 1) % self.batch_size == 0:
                        self._emit_progress(task_id, idx + 1, total, reprocessed, skipped, errors)
                    continue

                # Применяем только если новый confidence выше.
                if only_low_confidence and new_confidence <= old_confidence:
                    skipped += 1
                    logger.debug(
                        "Bulk reprocess skip (no improvement %.3f -> %.3f): %s",
                        old_confidence,
                        new_confidence,
                        item_id,
                    )
                    if (idx + 1) % self.batch_size == 0:
                        self._emit_progress(task_id, idx + 1, total, reprocessed, skipped, errors)
                    continue

                # Сохраняем старую версию перед обновлением.
                if item.text:
                    try:
                        self.version_manager.save_version(
                            item_id, item.text, source="stt_raw"
                        )
                    except Exception as ve:
                        logger.warning(
                            "Не удалось сохранить старую версию для %s: %s", item_id, ve
                        )

                # Сохраняем новую версию (новый текст).
                try:
                    self.version_manager.save_version(
                        item_id, new_text, source="stt_cleaned"
                    )
                except Exception as ve:
                    logger.warning(
                        "Не удалось сохранить новую версию для %s: %s", item_id, ve
                    )

                # Обновляем текст и confidence в хранилище.
                self.store.update_history_item_text(item_id, new_text, confidence=new_confidence)
                reprocessed += 1
                logger.info(
                    "Bulk reprocess OK %s: conf %.3f -> %.3f",
                    item_id,
                    old_confidence,
                    new_confidence,
                )

            except Exception as exc:
                err_msg = f"{item_id}: {exc}"
                errors.append(err_msg)
                logger.warning("Bulk reprocess error %s: %s", item_id, exc)

            # Emit прогресс каждые batch_size записей.
            if (idx + 1) % self.batch_size == 0:
                self._emit_progress(task_id, idx + 1, total, reprocessed, skipped, errors)

        # Финальный emit.
        self._emit_progress(task_id, total, total, reprocessed, skipped, errors)

        return {
            "total": total,
            "reprocessed": reprocessed,
            "skipped": skipped,
            "errors": errors,
            "cancelled": cancelled,
        }
