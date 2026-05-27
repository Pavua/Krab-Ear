"""JobTracker — потокобезопасное хранилище состояний асинхронных транскрибационных задач.

Используется `transcribe_paths_async` / `get_transcribe_progress` / `cancel_transcribe_job`.

Жизненный цикл задачи:
    create_job(total_files) -> job_id (status="queued")
    update(job_id, **fields)                  # worker обновляет current_file / current_stage
    mark_done(job_id, items, errors)          # status -> "done"
    mark_failed(job_id, error)                # status -> "failed"
    cancel(job_id)                            # ставит флаг; worker читает get(...)['cancel_requested']

Потокобезопасность:
    Все мутации под self._lock. Сохраняем время под блокировкой минимальным (<< 1ms) —
    только обновление словаря, без I/O.

Автоочистка:
    prune(max_age_sec) вызывается из create_job() и удаляет записи с
    status in {"done", "failed", "cancelled"}, чей finished_at старше max_age_sec.

W1182 F2 HIGH — zombie MLX worker fix:
    prune() теперь устанавливает cancel_event ДО удаления записи из _jobs.
    Worker держит прямую ссылку на threading.Event через get_cancel_event(),
    поэтому может наблюдать отмену даже после eviction основной записи из _jobs.
    cancel_events хранятся ещё _PRUNE_CANCEL_EVENT_TTL секунд после eviction
    (grace period), чтобы worker успел завершить текущую итерацию и выйти.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

# Время (в секундах), на которое cancel_event остаётся доступным после
# того, как основная запись удалена из _jobs при prune().
# Даёт воркеру возможность заметить отмену и завершить итерацию.
_PRUNE_CANCEL_EVENT_TTL: float = 2.0


class JobTracker:
    """Потокобезопасный реестр background-задач транскрибации."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        # cancel_events живут дольше основных записей (_PRUNE_CANCEL_EVENT_TTL
        # секунд после eviction) — zombie-worker fix W1182 F2.
        self._cancel_events: dict[str, threading.Event] = {}
        # Время eviction для отслеживания grace-period cleanup.
        self._evict_times: dict[str, float] = {}
        self._lock = threading.Lock()

    def create_job(self, total_files: int) -> str:
        """Создаёт новую задачу в статусе 'queued'.

        Попутно выполняет prune() для удаления устаревших завершённых задач.
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
        with self._lock:
            self._cancel_events[job_id] = threading.Event()
            self._jobs[job_id] = state
        return job_id

    def get_cancel_event(self, job_id: str) -> threading.Event | None:
        """Возвращает threading.Event для данного job_id.

        Worker должен получать ссылку сразу после create_job() и держать её
        всё время своей работы. Это позволяет наблюдать cancel даже после
        того, как prune() удалил основную запись из _jobs (W1182 F2 fix).

        Возвращает None, если job_id неизвестен (в том числе если grace-period
        истёк и событие уже удалено).
        """
        with self._lock:
            return self._cancel_events.get(job_id)

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

        Устанавливает cancel_requested в словаре задачи И threading.Event.
        Worker-потоки могут проверять оба канала.

        Возвращает True, если флаг установлен (задача существует и активна).
        Реальная смена статуса на 'cancelled' произойдёт в воркере между файлами.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            # Если задача уже завершена — отмена не имеет эффекта.
            if job.get("status") in ("done", "failed", "cancelled"):
                return False
            job["cancel_requested"] = True
            # Также устанавливаем threading.Event (W1182 F2).
            event = self._cancel_events.get(job_id)
            if event is not None:
                event.set()
            return True

    def prune(self, max_age_sec: int = 3600) -> None:
        """Удаляет давно завершённые задачи (done/failed/cancelled старше max_age_sec).

        W1182 F2 FIX — zombie MLX worker:
            До eviction устанавливает cancel_event для каждой stale-записи.
            Это гарантирует, что worker-поток, держащий ссылку на Event через
            get_cancel_event(), увидит отмену и завершится — не станет zombie,
            продолжающим потреблять MLX GPU.

            cancel_events удаляются только через _PRUNE_CANCEL_EVENT_TTL секунд
            после eviction основной записи (grace period для текущей итерации
            worker'а).

        Вызывается автоматически из create_job(). Не требует фонового GC-потока.
        """
        now = time.monotonic()
        threshold = now - max_age_sec
        terminal = {"done", "failed", "cancelled"}
        with self._lock:
            # 1. Найти stale-записи.
            stale = [
                jid
                for jid, job in self._jobs.items()
                if job.get("status") in terminal
                and (job.get("finished_at") or 0.0) < threshold
            ]
            # 2. Для каждой stale-записи: установить cancel_event ДО удаления.
            for jid in stale:
                event = self._cancel_events.get(jid)
                if event is not None:
                    event.set()
                self._jobs.pop(jid, None)
                # Записываем время eviction для grace-period cleanup.
                self._evict_times[jid] = now

            # 3. Очищаем cancel_events и evict_times для записей, чей
            #    grace-period истёк (evict_time + TTL < now).
            grace_threshold = now - _PRUNE_CANCEL_EVENT_TTL
            expired_events = [
                jid
                for jid, evict_time in self._evict_times.items()
                if evict_time < grace_threshold
            ]
            for jid in expired_events:
                self._cancel_events.pop(jid, None)
                self._evict_times.pop(jid, None)
