"""TranscriptionQueue — очередь транскрипции нескольких аудиофайлов с приоритетами.

Позволяет ставить файлы в очередь, отменять задания и отслеживать статусы.
Обработка запускается внешне через process_next() — фоновый поток не создаётся.

Статусы заданий: pending → processing → completed / failed / cancelled
Приоритеты: 1 (наивысший) — 10 (наинизший). При равном приоритете — FIFO по времени постановки.

# LIVE — обработчики подключены в ipc_dispatch.py (W1767 re-activation, 2026-06-02).
#
# 4 IPC handler'а (enqueue_transcription, cancel_transcription, get_queue_status,
# list_transcription_queue) снова зарегистрированы в BackendService._dispatch_table
# через svc._transcription_queue (см. backend/ipc_dispatch.py строки 233-236).
#
# ВАЖНО: process_next() по-прежнему НЕ вызывается никаким фоновым потоком.
# Задания ставятся в очередь и висят в статусе pending, пока внешний клиент
# не вызовет process_next() вручную или не будет поднят daemon-воркер.
# Для активации обработки: добавить daemon-поток в BackendService, который
# поллит process_next() и диспатчит через _transcribe_paths_core.
#
# Memory-leak fixes (W1722):
#   BUG 1 — _jobs grew without bound: terminal-state jobs (completed/failed/cancelled)
#            are now evicted after TERMINAL_RETENTION_SECONDS (default 3600 s = 1 h) OR
#            when the total terminal-job count exceeds TERMINAL_MAX_COUNT (100).  The
#            eviction runs under the existing lock inside _evict_terminal_jobs(), called
#            at the start of every state-transition that can produce a terminal job.
#   BUG 2 — mark_completed() accepted arbitrarily large result dicts.  Results whose
#            JSON-serialised size exceeds RESULT_MAX_BYTES (512 KiB) are truncated to a
#            stub {"truncated": True, "original_bytes": N} to prevent a single large
#            transcription result from pinning megabytes of RAM per job.
#   BUG 3 (W1762) — enqueue() had no cap on pending+processing jobs, enabling an
#            unbounded-growth DoS from any caller reachable via the IPC socket.
#            Since process_next() has no production consumer the list would grow without
#            limit.  Fix: reject enqueue requests once _MAX_PENDING active
#            (pending+processing) jobs already exist, returning {"ok": False,
#            "error": "queue_full"} from handle_enqueue.  The cap is enforced inside
#            enqueue() via QueueFullError so the guard is also exercisable by unit tests
#            that bypass the IPC handler.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("KrabEar.Backend.TranscriptionQueue")

# Допустимые статусы заданий
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

PRIORITY_MIN = 1
PRIORITY_MAX = 10
PRIORITY_DEFAULT = 5

# Terminal-job eviction parameters (BUG 1 fix)
# Keep completed/failed/cancelled jobs for at most 1 hour after finishing.
TERMINAL_RETENTION_SECONDS: int = 3600
# Hard cap: never keep more than this many terminal jobs in memory at once.
TERMINAL_MAX_COUNT: int = 100

# Result size guard (BUG 2 fix)
# JSON-encoded result bytes above this threshold are replaced with a stub.
RESULT_MAX_BYTES: int = 512 * 1024  # 512 KiB

# Pending/processing job cap (BUG 3 fix, W1762)
# Maximum number of active (pending+processing) jobs allowed in the queue at once.
# Requests beyond this limit are rejected by enqueue() / handle_enqueue() to prevent
# unbounded memory growth when process_next() has no live consumer.
_MAX_PENDING: int = 1000


class QueueFullError(Exception):
    """Очередь достигла лимита активных заданий (_MAX_PENDING).

    Поднимается в enqueue() когда количество pending+processing заданий
    достигает _MAX_PENDING.  handle_enqueue() перехватывает это исключение
    и возвращает {"ok": False, "error": "queue_full"} без raise.
    """


class TranscriptionJob:
    """Одно задание в очереди транскрипции."""

    def __init__(
        self,
        file_path: str,
        priority: int = PRIORITY_DEFAULT,
        label: str = "",
    ) -> None:
        if not file_path or not file_path.strip():
            raise ValueError("file_path не может быть пустым")
        if not (PRIORITY_MIN <= priority <= PRIORITY_MAX):
            raise ValueError(
                f"priority должен быть от {PRIORITY_MIN} до {PRIORITY_MAX}, получено {priority}"
            )
        self.job_id: str = str(uuid.uuid4())
        self.file_path: str = file_path.strip()
        self.priority: int = priority
        self.label: str = label or ""
        self.status: str = STATUS_PENDING
        self.created_at: float = time.monotonic()
        self.created_at_iso: str = datetime.now(timezone.utc).isoformat()
        self.started_at_iso: str | None = None
        self.finished_at_iso: str | None = None
        # Monotonic clock timestamp set when job enters a terminal state; used by
        # the eviction sweep (_evict_terminal_jobs) for time-based retention.
        self.finished_at_monotonic: float | None = None
        self.error: str | None = None
        self.result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "file_path": self.file_path,
            "priority": self.priority,
            "label": self.label,
            "status": self.status,
            "created_at": self.created_at_iso,
            "started_at": self.started_at_iso,
            "finished_at": self.finished_at_iso,
            "error": self.error,
            "result": self.result,
        }


class TranscriptionQueue:
    """Потокобезопасная очередь транскрипции с приоритетами.

    Не создаёт фоновых потоков — обработка инициируется извне через process_next().

    Опциональная персистентность: при передаче persist_path pending-задания
    сохраняются в NDJSON-файл при каждой мутации и восстанавливаются при запуске.
    Задания в статусе processing/completed/failed/cancelled не сохраняются —
    только pending. Если persist_path=None — поведение полностью in-memory (default).
    """

    def __init__(self, persist_path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        # Все задания по job_id.
        # Active (pending/processing) jobs live here indefinitely.
        # Terminal (completed/failed/cancelled) jobs are evicted by
        # _evict_terminal_jobs() — see TERMINAL_RETENTION_SECONDS / TERMINAL_MAX_COUNT.
        self._jobs: dict[str, TranscriptionJob] = {}
        # Insertion-ordered set of terminal job_ids; used for O(1) oldest-first eviction.
        self._terminal_order: OrderedDict[str, None] = OrderedDict()
        self._persist_path: Optional[Path] = persist_path
        if self._persist_path is not None:
            self._load()

    # ------------------------------------------------------------------
    # Персистентность (опциональная)
    # ------------------------------------------------------------------

    def _save(self) -> None:
        """Сохраняет pending-задания в NDJSON-файл (если задан persist_path).

        Вызывается под self._lock — не захватывает лок повторно.
        Сохраняет только задания со статусом pending; остальные
        восстанавливать при старте смысла нет.
        """
        if self._persist_path is None:
            return
        pending = [j for j in self._jobs.values() if j.status == STATUS_PENDING]
        try:
            lines = "\n".join(json.dumps(j.to_dict(), ensure_ascii=False) for j in pending)
            self._persist_path.write_text(lines + "\n" if lines else "", encoding="utf-8")
        except Exception as exc:
            logger.error("TranscriptionQueue: не удалось сохранить очередь в %s: %s", self._persist_path, exc)

    def _load(self) -> None:
        """Восстанавливает pending-задания из NDJSON-файла при инициализации.

        Повреждённые строки пропускаются с предупреждением — graceful degradation.
        """
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            raw = self._persist_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("TranscriptionQueue: не удалось прочитать %s: %s", self._persist_path, exc)
            return
        restored = 0
        for lineno, line in enumerate(raw.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                job = TranscriptionJob(
                    file_path=data["file_path"],
                    priority=data.get("priority", PRIORITY_DEFAULT),
                    label=data.get("label", ""),
                )
                # Восстанавливаем оригинальный job_id и метаданные времени
                job.job_id = data["job_id"]
                job.created_at_iso = data.get("created_at", job.created_at_iso)
                # status оставляем pending (только pending сохраняются).
                # INVARIANT: only pending jobs are persisted today; if terminal-job
                # persistence is ever added, restored terminal jobs MUST also be
                # registered into _terminal_order here — otherwise they will never
                # be evicted by _evict_terminal_jobs and the memory leak re-opens.
                # Defensive guard: register any accidentally-restored terminal job now.
                self._jobs[job.job_id] = job
                if job.status not in (STATUS_PENDING, STATUS_PROCESSING):
                    self._terminal_order[job.job_id] = None
                restored += 1
            except Exception as exc:
                logger.warning(
                    "TranscriptionQueue: строка %d в %s пропущена (%s): %r",
                    lineno, self._persist_path, exc, line[:80],
                )
        if restored:
            logger.info("TranscriptionQueue: восстановлено %d pending-заданий из %s", restored, self._persist_path)

    # ------------------------------------------------------------------
    # Internal — eviction (BUG 1 fix, W1722)
    # ------------------------------------------------------------------

    def _evict_terminal_jobs(self) -> None:
        """Evict terminal-state jobs that have exceeded the retention window or count cap.

        Must be called under self._lock.  Runs in O(k) where k is the number of
        jobs evicted — typically 0 or 1 per call, so it is negligibly cheap.

        Strategy (dual):
          1. Time-based: evict any terminal job whose finished_at_monotonic is older
             than TERMINAL_RETENTION_SECONDS.  This guarantees status queries work
             for at least one hour after completion.
          2. Count-based: if more than TERMINAL_MAX_COUNT terminal jobs remain after
             time-based eviction, drop the oldest ones (by insertion order in
             _terminal_order) until the cap is satisfied.

        Both thresholds are applied on every state transition that creates a terminal
        job so the dict never grows beyond O(TERMINAL_MAX_COUNT + active_jobs).
        """
        now = time.monotonic()
        cutoff = now - TERMINAL_RETENTION_SECONDS

        # 1. Time-based pass — evict expired entries (oldest first in _terminal_order)
        expired: list[str] = []
        for jid in self._terminal_order:
            job = self._jobs.get(jid)
            if job is None:
                # Stale reference — clean up bookkeeping.
                expired.append(jid)
                continue
            finished = job.finished_at_monotonic
            if finished is None or finished <= cutoff:
                expired.append(jid)
            else:
                # OrderedDict preserves insertion order; once we hit a non-expired
                # entry all subsequent ones are also newer (FIFO insertion).
                break

        for jid in expired:
            self._jobs.pop(jid, None)
            self._terminal_order.pop(jid, None)

        # 2. Count-based cap — evict oldest until we are within TERMINAL_MAX_COUNT.
        count_evicted = 0
        while len(self._terminal_order) > TERMINAL_MAX_COUNT:
            oldest_jid, _ = self._terminal_order.popitem(last=False)
            self._jobs.pop(oldest_jid, None)
            count_evicted += 1

        # Summary log — fires whenever *any* eviction occurred (time-based, count-based,
        # or both).  A single line after both passes avoids per-entry log spam and ensures
        # the message is emitted even when only the count cap fires (no expired entries).
        total_evicted = len(expired) + count_evicted
        if total_evicted:
            logger.debug(
                "TranscriptionQueue: evicted %d terminal job(s) "
                "(time-based=%d count-based=%d)",
                total_evicted, len(expired), count_evicted,
            )

    def _register_terminal(self, job: "TranscriptionJob") -> None:
        """Record that *job* has just entered a terminal state.

        Must be called under self._lock, immediately after setting job.status.
        Sets finished_at_monotonic, appends job_id to _terminal_order, then
        triggers _evict_terminal_jobs to enforce retention limits.
        """
        job.finished_at_monotonic = time.monotonic()
        self._terminal_order[job.job_id] = None
        self._evict_terminal_jobs()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def enqueue(
        self,
        file_path: str,
        priority: int = PRIORITY_DEFAULT,
        label: str = "",
    ) -> str:
        """Добавляет файл в очередь транскрипции.

        Args:
            file_path: Путь к аудиофайлу.
            priority: Приоритет 1 (наивысший) — 10 (наинизший). По умолчанию 5.
            label: Опциональная метка для задания.

        Returns:
            job_id: Уникальный идентификатор задания.

        Raises:
            QueueFullError: если количество активных (pending+processing) заданий
                достигло _MAX_PENDING.  Caller должен сообщить об ошибке — не добавлять
                задание в очередь — чтобы не допустить неограниченного роста памяти.
        """
        # BUG 3 guard (W1762): считаем активные задания ДО создания нового объекта,
        # чтобы проверка и вставка выполнялись атомарно под локом.
        job = TranscriptionJob(file_path=file_path, priority=priority, label=label)
        with self._lock:
            active_count = sum(
                1 for j in self._jobs.values()
                if j.status in (STATUS_PENDING, STATUS_PROCESSING)
            )
            if active_count >= _MAX_PENDING:
                logger.warning(
                    "TranscriptionQueue: очередь переполнена — отклонено задание "
                    "file=%r (активных=%d лимит=%d)",
                    file_path, active_count, _MAX_PENDING,
                    extra={"event": "queue_full", "active": active_count, "limit": _MAX_PENDING},
                )
                raise QueueFullError(
                    f"Очередь заданий переполнена: {active_count}/{_MAX_PENDING} активных заданий"
                )
            self._jobs[job.job_id] = job
            self._save()
        logger.debug("Задание поставлено в очередь: job_id=%s file=%r priority=%d", job.job_id, file_path, priority)
        return job.job_id

    def cancel(self, job_id: str) -> bool:
        """Отменяет задание, если оно ещё не завершено.

        Args:
            job_id: Идентификатор задания.

        Returns:
            True если задание отменено, False если не найдено или нельзя отменить
            (уже в статусе completed/failed/cancelled или processing).
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status not in (STATUS_PENDING,):
                return False
            job.status = STATUS_CANCELLED
            job.finished_at_iso = datetime.now(timezone.utc).isoformat()
            self._register_terminal(job)
            self._save()
        logger.debug("Задание отменено: job_id=%s", job_id)
        return True

    def get_status(self, job_id: str) -> dict[str, Any]:
        """Возвращает текущее состояние задания.

        Args:
            job_id: Идентификатор задания.

        Returns:
            Словарь с полями задания или {"error": "not_found"}.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return {"error": "not_found", "job_id": job_id}
            return job.to_dict()

    def list_queue(self) -> list[dict[str, Any]]:
        """Возвращает список всех заданий очереди с их статусами.

        Сортировка: сначала по приоритету (1 = высший), затем по времени добавления.
        """
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: (j.priority, j.created_at))
        return [j.to_dict() for j in jobs]

    def get_queue_stats(self) -> dict[str, Any]:
        """Возвращает агрегированную статистику по очереди.

        Returns:
            Словарь с ключами: pending, processing, completed, failed, cancelled, total.
        """
        counts: dict[str, int] = {
            STATUS_PENDING: 0,
            STATUS_PROCESSING: 0,
            STATUS_COMPLETED: 0,
            STATUS_FAILED: 0,
            STATUS_CANCELLED: 0,
        }
        with self._lock:
            for job in self._jobs.values():
                if job.status in counts:
                    counts[job.status] += 1
        counts["total"] = sum(counts.values())
        return counts

    def process_next(self) -> dict[str, Any] | None:
        """Извлекает и помечает как 'processing' следующее задание с наибольшим приоритетом.

        Задания с одинаковым приоритетом обрабатываются в порядке FIFO (по времени добавления).
        Задания со статусом processing не возвращаются повторно.

        Returns:
            Словарь задания (job.to_dict()) или None если очередь пуста / нет pending заданий.
        """
        with self._lock:
            pending = [j for j in self._jobs.values() if j.status == STATUS_PENDING]
            if not pending:
                return None
            # Выбираем задание с наивысшим приоритетом (меньшее число = выше приоритет), затем FIFO
            pending.sort(key=lambda j: (j.priority, j.created_at))
            job = pending[0]
            job.status = STATUS_PROCESSING
            job.started_at_iso = datetime.now(timezone.utc).isoformat()
        logger.debug("Начата обработка задания: job_id=%s file=%r", job.job_id, job.file_path)
        return job.to_dict()

    def mark_completed(self, job_id: str, result: dict[str, Any] | None = None) -> bool:
        """Помечает задание как выполненное.

        Используется внешним обработчиком после успешной транскрипции.

        Args:
            job_id: Идентификатор задания.
            result: Опциональный словарь с результатами транскрипции.
                    Results whose JSON-encoded size exceeds RESULT_MAX_BYTES are
                    replaced with a compact stub to prevent large dicts from
                    pinning memory indefinitely (BUG 2 fix, W1722).

        Returns:
            True если статус успешно изменён, False если задание не найдено.
        """
        # BUG 2 guard: truncate oversized result before storing (outside the lock
        # to avoid holding the lock during json.dumps on a potentially large dict).
        # TOCTOU note: the job could be completed-then-evicted between this size-check
        # and the lock acquisition below, in which case mark_completed returns False
        # even though the result was valid.  This is intentional and acceptable —
        # evicted jobs are considered expired; the caller should treat False as
        # "job no longer tracked" rather than a data error.
        stored_result = result
        if result is not None:
            try:
                encoded = json.dumps(result, ensure_ascii=False)
                original_bytes = len(encoded.encode("utf-8"))
                if original_bytes > RESULT_MAX_BYTES:
                    logger.warning(
                        "TranscriptionQueue: result for job_id=%s is %d bytes "
                        "(limit %d) — storing truncation stub",
                        job_id, original_bytes, RESULT_MAX_BYTES,
                    )
                    stored_result = {"truncated": True, "original_bytes": original_bytes}
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "TranscriptionQueue: could not measure result size for job_id=%s: %s "
                    "— storing as-is",
                    job_id, exc,
                )

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.status = STATUS_COMPLETED
            job.finished_at_iso = datetime.now(timezone.utc).isoformat()
            job.result = stored_result
            self._register_terminal(job)
            self._save()
        return True

    def mark_failed(self, job_id: str, error: str = "") -> bool:
        """Помечает задание как неуспешное.

        Используется внешним обработчиком при ошибке транскрипции.

        Args:
            job_id: Идентификатор задания.
            error: Сообщение об ошибке.

        Returns:
            True если статус успешно изменён, False если задание не найдено.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.status = STATUS_FAILED
            job.finished_at_iso = datetime.now(timezone.utc).isoformat()
            job.error = error or "Неизвестная ошибка"
            self._register_terminal(job)
            self._save()
        return True

    # ------------------------------------------------------------------
    # IPC-обработчики (паттерн handle_* как в других сервисах)
    # ------------------------------------------------------------------

    def handle_enqueue(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: enqueue_transcription — добавить файл в очередь.

        Возвращает {"job_id": str} при успехе.
        Возвращает {"ok": False, "error": "queue_full"} если очередь переполнена
        (_MAX_PENDING активных заданий) — без raise, чтобы IPC-слой мог
        сериализовать ответ клиенту.
        Поднимает ValueError при некорректных параметрах.
        """
        file_path = str(params.get("file_path", "")).strip()
        if not file_path:
            raise ValueError("Параметр file_path обязателен")
        try:
            priority = int(params.get("priority", PRIORITY_DEFAULT))
        except (TypeError, ValueError):
            raise ValueError(f"priority должен быть целым числом от {PRIORITY_MIN} до {PRIORITY_MAX}")
        label = str(params.get("label", ""))
        try:
            job_id = self.enqueue(file_path=file_path, priority=priority, label=label)
        except QueueFullError:
            # Возвращаем структурированную ошибку — не бросаем исключение,
            # чтобы IPC-диспетчер мог сериализовать ответ и отправить клиенту.
            return {"ok": False, "error": "queue_full"}
        return {"job_id": job_id}

    def handle_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: cancel_transcription — отменить задание по job_id."""
        job_id = str(params.get("job_id", "")).strip()
        if not job_id:
            raise ValueError("Параметр job_id обязателен")
        cancelled = self.cancel(job_id)
        return {"cancelled": cancelled, "job_id": job_id}

    def handle_get_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_queue_status — статус задания по job_id."""
        job_id = str(params.get("job_id", "")).strip()
        if not job_id:
            raise ValueError("Параметр job_id обязателен")
        return self.get_status(job_id)

    def peek(self) -> dict[str, Any] | None:
        """Возвращает следующее pending-задание без изменения его статуса.

        Returns:
            Словарь задания (job.to_dict()) или None если нет pending заданий.
        """
        with self._lock:
            pending = [j for j in self._jobs.values() if j.status == STATUS_PENDING]
            if not pending:
                return None
            pending.sort(key=lambda j: (j.priority, j.created_at))
            return pending[0].to_dict()

    def handle_peek(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: peek_transcription_queue — следующее задание без снятия из очереди."""
        job = self.peek()
        return {"job": job}

    def handle_list_queue(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: list_transcription_queue — список всех заданий."""
        return {"jobs": self.list_queue(), "stats": self.get_queue_stats()}
