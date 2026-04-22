"""TranscriptionQueue — очередь транскрипции нескольких аудиофайлов с приоритетами.

Позволяет ставить файлы в очередь, отменять задания и отслеживать статусы.
Обработка запускается внешне через process_next() — фоновый поток не создаётся.

Статусы заданий: pending → processing → completed / failed / cancelled
Приоритеты: 1 (наивысший) — 10 (наинизший). При равном приоритете — FIFO по времени постановки.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

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
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Все задания по job_id
        self._jobs: dict[str, TranscriptionJob] = {}

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
        """
        job = TranscriptionJob(file_path=file_path, priority=priority, label=label)
        with self._lock:
            self._jobs[job.job_id] = job
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

        Returns:
            True если статус успешно изменён, False если задание не найдено.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.status = STATUS_COMPLETED
            job.finished_at_iso = datetime.now(timezone.utc).isoformat()
            job.result = result
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
        return True

    # ------------------------------------------------------------------
    # IPC-обработчики (паттерн handle_* как в других сервисах)
    # ------------------------------------------------------------------

    def handle_enqueue(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: enqueue_transcription — добавить файл в очередь."""
        file_path = str(params.get("file_path", "")).strip()
        if not file_path:
            raise ValueError("Параметр file_path обязателен")
        try:
            priority = int(params.get("priority", PRIORITY_DEFAULT))
        except (TypeError, ValueError):
            raise ValueError(f"priority должен быть целым числом от {PRIORITY_MIN} до {PRIORITY_MAX}")
        label = str(params.get("label", ""))
        job_id = self.enqueue(file_path=file_path, priority=priority, label=label)
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
