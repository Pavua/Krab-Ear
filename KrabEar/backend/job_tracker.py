"""JobTracker — потокобезопасное хранилище состояний асинхронных транскрибационных задач.

Используется `transcribe_paths_async` / `get_transcribe_progress` / `cancel_transcribe_job`.

Жизненный цикл задачи:
    create_job(total_files) -> job_id (status="queued")
    update(job_id, **fields)                  # worker обновляет current_file / current_stage
    mark_done(job_id, items, errors)          # status -> "done"
    mark_failed(job_id, error)                # status -> "failed"
    cancel(job_id)                            # ставит флаг; worker читает get(...)['cancel_requested']
                                              # И устанавливает cancel_event (threading.Event)

Потокобезопасность:
    Все мутации под self._lock. Сохраняем время под блокировкой минимальным (<< 1ms) —
    только обновление словаря, без I/O.

Автоочистка:
    prune(max_age_sec) вызывается из create_job() и удаляет записи с
    status in {"done", "failed", "cancelled"}, чей finished_at старше max_age_sec.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

logger = logging.getLogger("backend.job_tracker")

_PRUNE_CANCEL_EVENT_TTL = 3600.0  # 1 hour — cancel events stuck longer than this get evicted


class JobTracker:
    """Потокобезопасный реестр background-задач транскрибации."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancel_events_ts: dict[str, float] = {}  # monotonic timestamp when cancel was set
        self._lock = threading.Lock()

    def create_job(self, total_files: int) -> str:
        """Создаёт новую задачу в статусе 'queued'.

        Попутно выполняет prune() для удаления устаревших завершённых задач.
        Каждая задача получает свой threading.Event для немедленной отмены
        (доступен через get_cancel_event).
        """
        self.prune()
        job_id = f"j-{uuid.uuid4().hex[:8]}"
        now = time.monotonic()
        state: dict[str, Any] = {
            "job_id": job_id,
            "status": "queued",
            "current_file": "",
            "current_stage": "idle",
            "file_index": 0,
            "total_files": int(total_files),
            "processed": 0,
            "errors": [],
            "items": [],
            "started_at": now,
            "finished_at": None,
            "cancel_requested": False,
        }
        cancel_event = threading.Event()
        with self._lock:
            self._jobs[job_id] = state
            self._cancel_events[job_id] = cancel_event
        return job_id

    def update(self, job_id: str, **fields: Any) -> None:
        """Обновляет произвольные поля задачи. Без I/O — только словарь.

        Неизвестные job_id игнорируются (воркер продолжает работу).
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.update(fields)

    def mark_done(
        self,
        job_id: str,
        items: list[dict[str, Any]],
        errors: list[str],
    ) -> None:
        """Помечает задачу как успешно завершённую."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["status"] = "done"
            job["items"] = list(items)
            job["errors"] = list(errors)
            job["processed"] = len(items)
            job["current_stage"] = "idle"
            job["finished_at"] = time.monotonic()

    def mark_failed(self, job_id: str, error: str) -> None:
        """Помечает задачу как провалившуюся с глобальной ошибкой."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["status"] = "failed"
            errors = list(job.get("errors") or [])
            errors.append(str(error))
            job["errors"] = errors
            job["current_stage"] = "idle"
            job["finished_at"] = time.monotonic()

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Возвращает копию состояния задачи (или None).

        Копия возвращается под блокировкой, чтобы наблюдатель не читал
        полуобновлённое состояние. elapsed_sec пересчитывается тут же.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            snapshot = dict(job)
        # Пересчитываем производные поля вне блокировки.
        started_at = snapshot.get("started_at") or 0.0
        finished_at = snapshot.get("finished_at")
        now = finished_at if finished_at is not None else time.monotonic()
        snapshot["elapsed_sec"] = round(max(0.0, now - started_at), 3)
        return snapshot

    def cancel(self, job_id: str) -> bool:
        """Сигнализирует воркеру о запросе отмены.

        Возвращает True, если флаг установлен (задача существует и активна).
        Реальная смена статуса на 'cancelled' произойдёт в воркере между файлами.
        Устанавливает cancel_event (threading.Event) для немедленного пробуждения
        воркеров, ожидающих на event.wait().
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            # Если задача уже завершена — отмена не имеет эффекта.
            if job.get("status") in ("done", "failed", "cancelled"):
                return False
            job["cancel_requested"] = True
            self._cancel_events_ts[job_id] = time.monotonic()
            event = self._cancel_events.get(job_id)
        # Устанавливаем event вне блокировки — Event.set() потокобезопасен.
        if event is not None:
            event.set()
        return True

    def get_cancel_event(self, job_id: str) -> threading.Event | None:
        """Возвращает threading.Event для задачи job_id, или None если задача не найдена.

        Event устанавливается при вызове cancel(job_id). Воркеры могут использовать
        event.is_set() для быстрой проверки без блокировки словаря.
        Если задача была вытеснена из памяти (prune), возвращает None —
        вызывающий код должен упасть обратно на dict-полинг через job_tracker.get().
        """
        with self._lock:
            return self._cancel_events.get(job_id)

    def prune(
        self,
        max_age_sec: float = 3600,
        max_running_age_sec: float | None = None,
    ) -> int:
        """Удаляет давно завершённые задачи (done/failed/cancelled старше max_age_sec).

        Также удаляет задачи в статусе 'running', у которых started_at старше
        max_running_age_sec (по умолчанию None = не удалять). Это защищает от
        зависших воркеров, которые никогда не перейдут в терминальный статус.

        Возвращает количество удалённых задач.

        Вызывается автоматически из create_job(). Не требует фонового GC-потока.
        Также очищает соответствующие cancel_events и просроченные cancel event записи.
        """
        now = time.monotonic()
        threshold = now - max_age_sec
        terminal = {"done", "failed", "cancelled"}
        stale: list[str] = []

        with self._lock:
            for jid, job in self._jobs.items():
                status = job.get("status")
                if status in terminal and (job.get("finished_at") or 0.0) < threshold:
                    stale.append(jid)
                elif (
                    status == "running"
                    and max_running_age_sec is not None
                    and (job.get("started_at") or now) < now - max_running_age_sec
                ):
                    logger.warning(
                        "prune: evicting stale running job job_id=%s started_at=%.1f age_sec=%.0f",
                        jid,
                        job.get("started_at", 0.0),
                        now - (job.get("started_at") or now),
                    )
                    stale.append(jid)

            for jid in stale:
                self._jobs.pop(jid, None)
                self._cancel_events.pop(jid, None)
                self._cancel_events_ts.pop(jid, None)

            # Evict cancel events older than _PRUNE_CANCEL_EVENT_TTL
            # (covers orphaned events whose job was already removed)
            cancel_ttl_threshold = now - _PRUNE_CANCEL_EVENT_TTL
            for jid, evt_ts in list(self._cancel_events_ts.items()):
                if evt_ts < cancel_ttl_threshold:
                    self._cancel_events.pop(jid, None)
                    self._cancel_events_ts.pop(jid, None)

        return len(stale)
