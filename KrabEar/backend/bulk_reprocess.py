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
- Size cap (W21 MED-1): sf.info() gate rejects files whose frames×channels exceed
  MAX_AUDIO_FRAMES before any RAM allocation — prevents OOM on 1000-item batches.
- Path containment (W21 MED-2): audio_path resolved through allowlist (home / /tmp /
  tempdir / data_dir) before sf.read; paths outside allowlist are skipped+logged.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from core import stt_budget

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

# ---------------------------------------------------------------------------
# W21 MED-1: size cap — reject files whose frames×channels exceed this value
# before any sf.read RAM allocation.  At float32 (4 bytes) this is ~400 MB
# for a single read; the transient resampling copy is ~2× that.  Mirrors the
# cap used by core/waveform_generator.py (_MAX_FILE_FRAMES = 100_000_000).
# ---------------------------------------------------------------------------
MAX_AUDIO_FRAMES: int = 100_000_000  # ~34 min mono 48 kHz, ~17 min stereo 48 kHz


# ---------------------------------------------------------------------------
# W21 MED-2: path containment — mirrors audio_analytics_service._validate_audio_read_path
# ---------------------------------------------------------------------------

def _validate_audio_read_path(p: str, data_dir: Path | None = None) -> Path:
    """Raise ValueError if *p* resolves outside the audio-read allowlist.

    Allowed roots: home, /tmp, tempdir, data_dir (if provided).

    Returns the resolved Path (TOCTOU-safe: caller uses this resolved Path).
    """
    resolved = Path(p).expanduser().resolve()
    allowed: list[Path] = [
        Path.home().resolve(),
        Path("/tmp").resolve(),
        Path(tempfile.gettempdir()).resolve(),
    ]
    if data_dir is not None:
        allowed.append(data_dir.resolve())

    if any(resolved == root or resolved.is_relative_to(root) for root in allowed):
        return resolved

    raise ValueError(
        f"bulk_reprocess: путь {resolved!s} находится за пределами разрешённых директорий. "
        f"Разрешённые корни: {[str(r) for r in allowed]}"
    )


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
        # W21: resolve data_dir once for path-containment checks.
        _data_dir = getattr(store, "data_dir", None)
        self._data_dir: Path | None = Path(_data_dir).resolve() if _data_dir else None
        # Защита от параллельных запусков. BackendService держит ОДИН общий singleton
        # self._bulk_reprocessor (service.py), а IPC — thread-per-connection. wave-25 перевёл
        # bulk_reprocess_start в HEAVY_METHODS (≤5/мин), но rate-limit НЕ заменяет этот guard:
        # два запроса в пределах окна всё ещё могут запустить два reprocess()-цикла по ОДНОМУ
        # набору кандидатов: (a) дубль MLX транскрибаций + гонка last-writer-wins на
        # update_history_item_text для одного id; (b) сломанная отмена — re-entry второго
        # запуска вызывал _reset_cancel()/clear() и стирал pending cancel первого, либо один
        # cancel() обрывал ОБА. Non-blocking acquire _run_lock гарантирует ровно один активный
        # цикл; второй вызов сразу возвращает error без старта второго цикла, не трогая
        # cancel-флаг активного запуска.
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
        """Загружает аудиофайл в массив numpy для передачи в transcriber.

        Guards (W21):
        - MED-2 (path containment): resolves audio_path through the allowlist
          before any I/O; raises ValueError for paths outside home/tmp/data_dir.
        - MED-1 (size cap): checks sf.info() frames×channels before sf.read;
          raises RuntimeError for files exceeding MAX_AUDIO_FRAMES to prevent OOM.
        """
        try:
            import soundfile as sf  # type: ignore

            # W21 MED-2: path containment — resolve and check allowlist before I/O.
            resolved_path = _validate_audio_read_path(audio_path, self._data_dir)

            # W21 MED-1: size cap — probe metadata without loading audio into RAM.
            info = sf.info(str(resolved_path))
            total_frames = info.frames * info.channels
            if total_frames > MAX_AUDIO_FRAMES:
                raise RuntimeError(
                    f"bulk_reprocess: аудиофайл слишком большой для загрузки в RAM "
                    f"({total_frames} frames×ch > лимит {MAX_AUDIO_FRAMES}): {resolved_path!s}"
                )

            data, sample_rate = sf.read(str(resolved_path), dtype="float32", always_2d=False)
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
        except (ValueError, RuntimeError):
            # Re-raise containment/size errors directly so the caller can skip+log.
            raise
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
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Запускает массовое перетранскрибирование истории.

        Args:
            filter_kwargs: Дополнительные параметры для фильтрации (зарезервировано).
            only_low_confidence: Пропускать записи с confidence >= threshold.
            threshold: Порог confidence (0.0–1.0).
            dry_run: Если True — только считает, без реального STT и сохранения.
            task_id: ID задачи для emit событий прогресса.
            settings: Опционально — текущие settings для privacy-mode check.
                      Если не передан, privacy-mode не проверяется.

        Returns:
            {
                "total": N,       # всего кандидатов (с аудио, не защищённых, не молодых)
                "reprocessed": M, # успешно перетранскрибировано
                "skipped": K,     # пропущено (confidence уже ок, нет улучшения, dry_run skip)
                "errors": [...],  # ошибки по конкретным записям
                "cancelled": bool,
            }
        """
        # D2 (wave-35 MED): refuse to bulk-reprocess when privacy mode is ON.
        # Privacy mode guarantees that no transcript cleartext can be read or
        # written by the backend.  Bulk reprocessing would: (a) read audio files
        # linked to existing history items, (b) run STT on them, and (c) persist
        # the new plaintext back into history_text_updates.ndjson — all of which
        # violate the privacy-mode contract.  We reject the entire run (not just
        # individual items) because the run cannot produce useful output anyway.
        if settings is not None and settings.get("privacy_mode_enabled"):
            return {
                "ok": False,
                "reason": "privacy_mode_active",
                "total": 0,
                "reprocessed": 0,
                "skipped": 0,
                "errors": [],
                "cancelled": False,
            }

        # Guard: refuse to run while active recording is in progress.
        # Competing with an ongoing recording for MLX GPU → potential SIGSEGV (PR #71 class).
        # Raises RuntimeError (W1043 contract); the IPC handler translates this into a
        # structured {"ok": False, "reason": "recording_active"} response (wave-25).
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
                # 🔴 Здесь НЕ берём mlx_lock/mlx_inter_process_lock вокруг
                # transcribe (было до 02.09.2026 как «defense-in-depth», PR #71).
                # Внешний захват самоблокировал ту самую работу, ради которой
                # брался: engine.transcribe отдаёт и адаптеры, и whisper в
                # ThreadPoolExecutor (engine.py: `_pool.submit(adapter_fn)`,
                # `_executor.submit(self._transcribe_model, ...)`), а рабочий
                # поток берёт ТОТ ЖЕ mlx_lock. RLock реентерабелен только для
                # своего потока — поток пула ждал породивший его поток. Для
                # GigaAM-MLX это не просто задержка: его ожидание ограничено
                # 25 с, после чего он уступает очередь, то есть каждая запись
                # пакета теряла 25 с и уезжала на резервный движок.
                # Инвариант «любой MLX-инференс под локом» сохранён ниже по
                # стеку: захват делает каждый MLX-путь сам — _transcribe_model
                # (whisper + RU-finetune), GigaAM-MLX/parakeet/voxtral-адаптеры,
                # AudioLanguageID._run_detect, set_quality_profile и пост-STT
                # mx.clear_cache(). Тот же класс дефекта в превью — #1972.
                import numpy as _np_budget
                # Спека 2026-08-26 §5: bulk-reprocess → batch-бюджет.
                # settings_get=None → дефолты модуля (у reprocessor'а нет
                # settings-коллаборатора; batch-дефолты достаточны).
                _dur_sec = (
                    len(audio_data) / 16000.0
                    if isinstance(audio_data, _np_budget.ndarray)
                    and len(audio_data) > 0
                    else None
                )
                with stt_budget.stt_budget_scope(
                    stt_budget.BATCH, audio_duration_sec=_dur_sec
                ):
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
