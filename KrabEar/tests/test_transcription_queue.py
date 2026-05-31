"""Тесты для TranscriptionQueue.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_transcription_queue.py -v
"""

import sys
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.transcription_queue import (
    TranscriptionQueue,
    TranscriptionJob,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    PRIORITY_DEFAULT,
    TERMINAL_MAX_COUNT,
    TERMINAL_RETENTION_SECONDS,
    RESULT_MAX_BYTES,
)


class TestTranscriptionJobInit(unittest.TestCase):
    """Тесты инициализации TranscriptionJob."""

    def test_job_creation_with_defaults(self):
        """Создание задания с параметрами по умолчанию."""
        job = TranscriptionJob(file_path="/path/to/audio.mp3")
        self.assertIsNotNone(job.job_id)
        self.assertEqual(job.file_path, "/path/to/audio.mp3")
        self.assertEqual(job.priority, PRIORITY_DEFAULT)
        self.assertEqual(job.label, "")
        self.assertEqual(job.status, STATUS_PENDING)
        self.assertIsNone(job.error)
        self.assertIsNone(job.result)

    def test_job_creation_with_priority_and_label(self):
        """Создание задания с приоритетом и меткой."""
        job = TranscriptionJob(
            file_path="/path/to/audio.mp3",
            priority=2,
            label="Meeting"
        )
        self.assertEqual(job.priority, 2)
        self.assertEqual(job.label, "Meeting")

    def test_job_file_path_strip(self):
        """Путь к файлу должен быть без пробелов."""
        job = TranscriptionJob(file_path="  /path/to/audio.mp3  ")
        self.assertEqual(job.file_path, "/path/to/audio.mp3")

    def test_job_invalid_empty_file_path(self):
        """Пустой файл вызывает ValueError."""
        with self.assertRaises(ValueError):
            TranscriptionJob(file_path="")

    def test_job_invalid_whitespace_only_file_path(self):
        """Файл с одними пробелами вызывает ValueError."""
        with self.assertRaises(ValueError):
            TranscriptionJob(file_path="   ")

    def test_job_invalid_priority_too_low(self):
        """Приоритет < PRIORITY_MIN вызывает ValueError."""
        with self.assertRaises(ValueError):
            TranscriptionJob(file_path="/path/to/audio.mp3", priority=0)

    def test_job_invalid_priority_too_high(self):
        """Приоритет > PRIORITY_MAX вызывает ValueError."""
        with self.assertRaises(ValueError):
            TranscriptionJob(file_path="/path/to/audio.mp3", priority=11)

    def test_job_to_dict(self):
        """job.to_dict() возвращает полный словарь."""
        job = TranscriptionJob(
            file_path="/path/to/audio.mp3",
            priority=3,
            label="Test"
        )
        job_dict = job.to_dict()
        self.assertEqual(job_dict["job_id"], job.job_id)
        self.assertEqual(job_dict["file_path"], "/path/to/audio.mp3")
        self.assertEqual(job_dict["priority"], 3)
        self.assertEqual(job_dict["label"], "Test")
        self.assertEqual(job_dict["status"], STATUS_PENDING)
        self.assertIsNone(job_dict["error"])
        self.assertIsNone(job_dict["result"])


class TestTranscriptionQueueEnqueueAndDequeue(unittest.TestCase):
    """Тесты постановки и извлечения из очереди."""

    def setUp(self):
        self.queue = TranscriptionQueue()

    def test_enqueue_single_job(self):
        """Добавление одного задания в очередь."""
        job_id = self.queue.enqueue("/path/to/audio1.mp3")
        self.assertIsNotNone(job_id)
        # Проверяем, что задание есть в очереди
        status = self.queue.get_status(job_id)
        self.assertEqual(status["file_path"], "/path/to/audio1.mp3")
        self.assertEqual(status["status"], STATUS_PENDING)

    def test_enqueue_multiple_jobs(self):
        """Добавление нескольких заданий в очередь."""
        job_id_1 = self.queue.enqueue("/path/to/audio1.mp3", priority=5)
        job_id_2 = self.queue.enqueue("/path/to/audio2.mp3", priority=3)
        job_id_3 = self.queue.enqueue("/path/to/audio3.mp3", priority=7)

        self.assertNotEqual(job_id_1, job_id_2)
        self.assertNotEqual(job_id_2, job_id_3)

        jobs = self.queue.list_queue()
        self.assertEqual(len(jobs), 3)

    def test_process_next_returns_highest_priority(self):
        """process_next() возвращает задание с наивысшим приоритетом."""
        self.queue.enqueue("/path/to/audio1.mp3", priority=5)
        job_id_2 = self.queue.enqueue("/path/to/audio2.mp3", priority=2)
        self.queue.enqueue("/path/to/audio3.mp3", priority=8)

        # Должно вернуться задание с приоритетом 2
        next_job = self.queue.process_next()
        self.assertEqual(next_job["job_id"], job_id_2)
        self.assertEqual(next_job["status"], STATUS_PROCESSING)

    def test_process_next_fifo_same_priority(self):
        """При одинаковом приоритете — FIFO по времени добавления."""
        job_id_1 = self.queue.enqueue("/path/to/audio1.mp3", priority=5)
        time.sleep(0.01)  # Малая задержка для гарантии порядка
        self.queue.enqueue("/path/to/audio2.mp3", priority=5)

        # Должно вернуться первое задание
        next_job = self.queue.process_next()
        self.assertEqual(next_job["job_id"], job_id_1)

    def test_process_next_empty_queue(self):
        """process_next() на пустой очереди возвращает None."""
        result = self.queue.process_next()
        self.assertIsNone(result)

    def test_process_next_no_pending_jobs(self):
        """process_next() когда нет pending заданий возвращает None."""
        job_id = self.queue.enqueue("/path/to/audio1.mp3")
        # Отмечаем задание как обработанное
        self.queue.mark_completed(job_id)
        # Теперь очередь пуста (нет pending)
        result = self.queue.process_next()
        self.assertIsNone(result)

    def test_process_next_skips_non_pending(self):
        """process_next() пропускает задания со статусом != pending."""
        job_id_1 = self.queue.enqueue("/path/to/audio1.mp3", priority=5)
        job_id_2 = self.queue.enqueue("/path/to/audio2.mp3", priority=3)

        # Первый process_next() вернёт job_id_2 (приоритет 3 выше чем 5)
        first_job = self.queue.process_next()
        self.assertEqual(first_job["job_id"], job_id_2)

        # Второй process_next() вернёт job_id_1 (единственное оставшееся pending)
        next_job = self.queue.process_next()
        self.assertEqual(next_job["job_id"], job_id_1)


class TestTranscriptionQueuePeekAndRemove(unittest.TestCase):
    """Тесты peek и remove операций."""

    def setUp(self):
        self.queue = TranscriptionQueue()

    def test_get_status_existing_job(self):
        """get_status() возвращает статус существующего задания."""
        job_id = self.queue.enqueue("/path/to/audio.mp3", priority=3, label="Test")
        status = self.queue.get_status(job_id)

        self.assertEqual(status["job_id"], job_id)
        self.assertEqual(status["file_path"], "/path/to/audio.mp3")
        self.assertEqual(status["priority"], 3)
        self.assertEqual(status["label"], "Test")
        self.assertEqual(status["status"], STATUS_PENDING)

    def test_get_status_nonexistent_job(self):
        """get_status() для несуществующего задания возвращает error."""
        status = self.queue.get_status("nonexistent-id")
        self.assertIn("error", status)
        self.assertEqual(status["error"], "not_found")

    def test_cancel_pending_job(self):
        """cancel() отменяет pending задание."""
        job_id = self.queue.enqueue("/path/to/audio.mp3")
        result = self.queue.cancel(job_id)

        self.assertTrue(result)
        status = self.queue.get_status(job_id)
        self.assertEqual(status["status"], STATUS_CANCELLED)

    def test_cancel_nonexistent_job(self):
        """cancel() несуществующего задания возвращает False."""
        result = self.queue.cancel("nonexistent-id")
        self.assertFalse(result)

    def test_cancel_processing_job_fails(self):
        """cancel() processing задания возвращает False."""
        job_id = self.queue.enqueue("/path/to/audio.mp3")
        self.queue.process_next()  # Переводим в processing

        result = self.queue.cancel(job_id)
        self.assertFalse(result)

        status = self.queue.get_status(job_id)
        self.assertEqual(status["status"], STATUS_PROCESSING)

    def test_cancel_completed_job_fails(self):
        """cancel() completed задания возвращает False."""
        job_id = self.queue.enqueue("/path/to/audio.mp3")
        self.queue.mark_completed(job_id)

        result = self.queue.cancel(job_id)
        self.assertFalse(result)


class TestTranscriptionQueueMarkCompletedFailed(unittest.TestCase):
    """Тесты mark_completed и mark_failed."""

    def setUp(self):
        self.queue = TranscriptionQueue()

    def test_mark_completed_with_result(self):
        """mark_completed() устанавливает статус и результат."""
        job_id = self.queue.enqueue("/path/to/audio.mp3")
        result = {"text": "Hello world", "confidence": 0.95}

        success = self.queue.mark_completed(job_id, result)
        self.assertTrue(success)

        status = self.queue.get_status(job_id)
        self.assertEqual(status["status"], STATUS_COMPLETED)
        self.assertEqual(status["result"], result)

    def test_mark_completed_without_result(self):
        """mark_completed() работает без результата."""
        job_id = self.queue.enqueue("/path/to/audio.mp3")
        success = self.queue.mark_completed(job_id)

        self.assertTrue(success)
        status = self.queue.get_status(job_id)
        self.assertEqual(status["status"], STATUS_COMPLETED)
        self.assertIsNone(status["result"])

    def test_mark_completed_nonexistent_job(self):
        """mark_completed() несуществующего задания возвращает False."""
        success = self.queue.mark_completed("nonexistent-id")
        self.assertFalse(success)

    def test_mark_failed_with_error(self):
        """mark_failed() устанавливает статус и сообщение об ошибке."""
        job_id = self.queue.enqueue("/path/to/audio.mp3")

        success = self.queue.mark_failed(job_id, error="File not found")
        self.assertTrue(success)

        status = self.queue.get_status(job_id)
        self.assertEqual(status["status"], STATUS_FAILED)
        self.assertEqual(status["error"], "File not found")

    def test_mark_failed_empty_error(self):
        """mark_failed() с пустой ошибкой устанавливает стандартное сообщение."""
        job_id = self.queue.enqueue("/path/to/audio.mp3")

        success = self.queue.mark_failed(job_id, error="")
        self.assertTrue(success)

        status = self.queue.get_status(job_id)
        self.assertEqual(status["status"], STATUS_FAILED)
        self.assertEqual(status["error"], "Неизвестная ошибка")

    def test_mark_failed_nonexistent_job(self):
        """mark_failed() несуществующего задания возвращает False."""
        success = self.queue.mark_failed("nonexistent-id", "Error")
        self.assertFalse(success)


class TestTranscriptionQueueStats(unittest.TestCase):
    """Тесты статистики и управления очередью."""

    def setUp(self):
        self.queue = TranscriptionQueue()

    def test_get_queue_stats_empty(self):
        """get_queue_stats() на пустой очереди."""
        stats = self.queue.get_queue_stats()

        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["processing"], 0)
        self.assertEqual(stats["completed"], 0)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["cancelled"], 0)
        self.assertEqual(stats["total"], 0)

    def test_get_queue_stats_mixed_statuses(self):
        """get_queue_stats() с заданиями в разных статусах."""
        self.queue.enqueue("/path/to/audio1.mp3")
        job_id_2 = self.queue.enqueue("/path/to/audio2.mp3")
        job_id_3 = self.queue.enqueue("/path/to/audio3.mp3")
        job_id_4 = self.queue.enqueue("/path/to/audio4.mp3")

        # Обработаем job_id_1
        self.queue.process_next()
        # Завершим job_id_2
        self.queue.mark_completed(job_id_2)
        # Ошибка в job_id_3
        self.queue.mark_failed(job_id_3)
        # Отменим job_id_4
        self.queue.cancel(job_id_4)

        stats = self.queue.get_queue_stats()
        self.assertEqual(stats["processing"], 1)
        self.assertEqual(stats["completed"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["cancelled"], 1)
        self.assertEqual(stats["total"], 4)

    def test_list_queue_sorted_by_priority_then_time(self):
        """list_queue() сортирует по приоритету, затем по времени добавления."""
        job_id_5a = self.queue.enqueue("/path/to/audio1.mp3", priority=5)
        time.sleep(0.01)
        job_id_5b = self.queue.enqueue("/path/to/audio2.mp3", priority=5)
        time.sleep(0.01)
        job_id_3 = self.queue.enqueue("/path/to/audio3.mp3", priority=3)

        jobs = self.queue.list_queue()

        # Должны идти в порядке: priority=3, потом priority=5 (FIFO)
        self.assertEqual(jobs[0]["job_id"], job_id_3)
        self.assertEqual(jobs[1]["job_id"], job_id_5a)
        self.assertEqual(jobs[2]["job_id"], job_id_5b)

    def test_list_queue_includes_all_statuses(self):
        """list_queue() включает задания во всех статусах."""
        self.queue.enqueue("/path/to/audio1.mp3")
        job_id_2 = self.queue.enqueue("/path/to/audio2.mp3")
        job_id_3 = self.queue.enqueue("/path/to/audio3.mp3")

        self.queue.process_next()
        self.queue.mark_completed(job_id_2)
        self.queue.mark_failed(job_id_3)

        jobs = self.queue.list_queue()

        # Должны быть все три задания
        self.assertEqual(len(jobs), 3)
        statuses = {job["status"] for job in jobs}
        self.assertIn(STATUS_PROCESSING, statuses)
        self.assertIn(STATUS_COMPLETED, statuses)
        self.assertIn(STATUS_FAILED, statuses)


class TestTranscriptionQueueIPCHandlers(unittest.TestCase):
    """Тесты IPC обработчиков (handle_*)."""

    def setUp(self):
        self.queue = TranscriptionQueue()

    def test_handle_enqueue_valid(self):
        """handle_enqueue() с валидными параметрами."""
        result = self.queue.handle_enqueue({
            "file_path": "/path/to/audio.mp3",
            "priority": 3,
            "label": "Test"
        })

        self.assertIn("job_id", result)
        self.assertIsNotNone(result["job_id"])

        status = self.queue.get_status(result["job_id"])
        self.assertEqual(status["priority"], 3)

    def test_handle_enqueue_missing_file_path(self):
        """handle_enqueue() без file_path вызывает ValueError."""
        with self.assertRaises(ValueError):
            self.queue.handle_enqueue({"priority": 3})

    def test_handle_enqueue_empty_file_path(self):
        """handle_enqueue() с пустым file_path вызывает ValueError."""
        with self.assertRaises(ValueError):
            self.queue.handle_enqueue({"file_path": "", "priority": 3})

    def test_handle_enqueue_invalid_priority(self):
        """handle_enqueue() с невалидным приоритетом вызывает ValueError."""
        with self.assertRaises(ValueError):
            self.queue.handle_enqueue({
                "file_path": "/path/to/audio.mp3",
                "priority": "not-a-number"
            })

    def test_handle_cancel_valid(self):
        """handle_cancel() отменяет задание."""
        job_id = self.queue.enqueue("/path/to/audio.mp3")
        result = self.queue.handle_cancel({"job_id": job_id})

        self.assertTrue(result["cancelled"])
        self.assertEqual(result["job_id"], job_id)

    def test_handle_cancel_missing_job_id(self):
        """handle_cancel() без job_id вызывает ValueError."""
        with self.assertRaises(ValueError):
            self.queue.handle_cancel({})

    def test_handle_get_status_valid(self):
        """handle_get_status() возвращает статус задания."""
        job_id = self.queue.enqueue("/path/to/audio.mp3")
        result = self.queue.handle_get_status({"job_id": job_id})

        self.assertEqual(result["job_id"], job_id)
        self.assertEqual(result["status"], STATUS_PENDING)

    def test_handle_get_status_missing_job_id(self):
        """handle_get_status() без job_id вызывает ValueError."""
        with self.assertRaises(ValueError):
            self.queue.handle_get_status({})

    def test_handle_list_queue(self):
        """handle_list_queue() возвращает очередь и статистику."""
        self.queue.enqueue("/path/to/audio1.mp3")
        self.queue.enqueue("/path/to/audio2.mp3")

        result = self.queue.handle_list_queue({})

        self.assertIn("jobs", result)
        self.assertIn("stats", result)
        self.assertEqual(len(result["jobs"]), 2)
        self.assertEqual(result["stats"]["total"], 2)


class TestTranscriptionQueueThreadSafety(unittest.TestCase):
    """Тесты потокобезопасности."""

    def setUp(self):
        self.queue = TranscriptionQueue()

    def test_concurrent_enqueue(self):
        """Одновременное добавление заданий должно быть безопасно."""
        job_ids = []
        lock = threading.Lock()

        def enqueue_job(idx):
            job_id = self.queue.enqueue(f"/path/to/audio{idx}.mp3")
            with lock:
                job_ids.append(job_id)

        threads = [threading.Thread(target=enqueue_job, args=(i,)) for i in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Все задания должны быть добавлены
        self.assertEqual(len(job_ids), 10)
        self.assertEqual(len(set(job_ids)), 10)  # Все уникальны

        stats = self.queue.get_queue_stats()
        self.assertEqual(stats["total"], 10)

    def test_concurrent_process_next(self):
        """Одновременные process_next() не должны возвращать одно задание дважды."""
        for i in range(5):
            self.queue.enqueue(f"/path/to/audio{i}.mp3")

        processed = []
        lock = threading.Lock()

        def process_one():
            job = self.queue.process_next()
            if job:
                with lock:
                    processed.append(job["job_id"])

        threads = [threading.Thread(target=process_one) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Каждое задание должно быть обработано один раз
        self.assertEqual(len(processed), 5)
        self.assertEqual(len(set(processed)), 5)  # Все уникальны


class TestTranscriptionQueuePeek(unittest.TestCase):
    """Тесты метода peek() — просмотр без удаления."""

    def setUp(self):
        self.queue = TranscriptionQueue()

    def test_peek_empty_queue_returns_none(self):
        """peek() на пустой очереди возвращает None."""
        result = self.queue.peek()
        self.assertIsNone(result)

    def test_peek_returns_highest_priority_job(self):
        """peek() возвращает задание с наибольшим приоритетом."""
        self.queue.enqueue("/path/to/audio1.mp3", priority=7)
        job_id_2 = self.queue.enqueue("/path/to/audio2.mp3", priority=2)
        result = self.queue.peek()
        self.assertIsNotNone(result)
        self.assertEqual(result["job_id"], job_id_2)

    def test_peek_does_not_change_status(self):
        """peek() не меняет статус задания."""
        job_id = self.queue.enqueue("/path/to/audio.mp3", priority=3)
        self.queue.peek()
        status = self.queue.get_status(job_id)
        self.assertEqual(status["status"], STATUS_PENDING)

    def test_peek_does_not_remove_job_from_queue(self):
        """После peek() задание остаётся в очереди."""
        self.queue.enqueue("/path/to/audio.mp3", priority=3)
        self.queue.peek()
        self.queue.peek()  # Второй peek — то же самое задание
        stats = self.queue.get_queue_stats()
        self.assertEqual(stats["pending"], 1)

    def test_peek_after_all_processed_returns_none(self):
        """peek() возвращает None когда все задания обработаны."""
        self.queue.enqueue("/path/to/audio.mp3")
        self.queue.process_next()
        result = self.queue.peek()
        self.assertIsNone(result)

    def test_peek_skips_non_pending_jobs(self):
        """peek() пропускает задания не в статусе pending."""
        self.queue.enqueue("/path/to/audio1.mp3", priority=2)
        job_id_2 = self.queue.enqueue("/path/to/audio2.mp3", priority=5)
        # Переводим job_id_1 в processing
        self.queue.process_next()
        # peek должен вернуть job_id_2 (единственное pending)
        result = self.queue.peek()
        self.assertIsNotNone(result)
        self.assertEqual(result["job_id"], job_id_2)

    def test_handle_peek_returns_job_dict(self):
        """handle_peek() возвращает словарь с ключом job."""
        job_id = self.queue.enqueue("/path/to/audio.mp3", priority=3)
        result = self.queue.handle_peek({})
        self.assertIn("job", result)
        self.assertIsNotNone(result["job"])
        self.assertEqual(result["job"]["job_id"], job_id)
        self.assertEqual(result["job"]["status"], STATUS_PENDING)

    def test_handle_peek_empty_queue(self):
        """handle_peek() на пустой очереди возвращает job=None."""
        result = self.queue.handle_peek({})
        self.assertIn("job", result)
        self.assertIsNone(result["job"])

    def test_peek_fifo_same_priority(self):
        """peek() при одинаковом приоритете возвращает первое добавленное задание."""
        job_id_1 = self.queue.enqueue("/path/to/audio1.mp3", priority=5)
        time.sleep(0.01)
        self.queue.enqueue("/path/to/audio2.mp3", priority=5)
        result = self.queue.peek()
        self.assertEqual(result["job_id"], job_id_1)


class TestTranscriptionQueuePersistence(unittest.TestCase):
    """Wave 159: опциональная персистентность TranscriptionQueue."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._persist_path = Path(self._tmp.name) / "queue.ndjson"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_persistence_disabled_by_default(self) -> None:
        """Без persist_path файл не создаётся — полностью in-memory."""
        q = TranscriptionQueue()
        q.enqueue("/tmp/audio.mp3")
        # Нет аргумента persist_path — никаких файлов на диске
        self.assertFalse(self._persist_path.exists())

    def test_enqueue_persists_to_disk(self) -> None:
        """enqueue() с persist_path создаёт NDJSON-файл."""
        q = TranscriptionQueue(persist_path=self._persist_path)
        q.enqueue("/tmp/audio.mp3")
        self.assertTrue(self._persist_path.exists())
        content = self._persist_path.read_text(encoding="utf-8").strip()
        self.assertTrue(len(content) > 0)

    def test_load_pending_at_init(self) -> None:
        """Pending-задания восстанавливаются при создании нового экземпляра."""
        q1 = TranscriptionQueue(persist_path=self._persist_path)
        jid1 = q1.enqueue("/tmp/audio1.mp3", priority=3, label="Meeting")
        jid2 = q1.enqueue("/tmp/audio2.mp3", priority=5)

        # Новый экземпляр читает файл
        q2 = TranscriptionQueue(persist_path=self._persist_path)
        stats = q2.get_queue_stats()
        self.assertEqual(stats["pending"], 2)

        s1 = q2.get_status(jid1)
        self.assertEqual(s1["file_path"], "/tmp/audio1.mp3")
        self.assertEqual(s1["priority"], 3)
        self.assertEqual(s1["label"], "Meeting")
        self.assertEqual(s1["status"], STATUS_PENDING)

        s2 = q2.get_status(jid2)
        self.assertEqual(s2["file_path"], "/tmp/audio2.mp3")

    def test_dequeue_removes_from_disk(self) -> None:
        """После cancel() задание исчезает из persisted файла."""
        q1 = TranscriptionQueue(persist_path=self._persist_path)
        jid = q1.enqueue("/tmp/audio.mp3")
        q1.cancel(jid)

        # Новый экземпляр не должен видеть это задание как pending
        q2 = TranscriptionQueue(persist_path=self._persist_path)
        self.assertEqual(q2.get_queue_stats()["pending"], 0)

    def test_completed_job_not_restored(self) -> None:
        """Completed/failed задания не персистируются и не восстанавливаются."""
        q1 = TranscriptionQueue(persist_path=self._persist_path)
        jid = q1.enqueue("/tmp/audio.mp3")
        q1.mark_completed(jid, result={"text": "hello"})

        q2 = TranscriptionQueue(persist_path=self._persist_path)
        self.assertEqual(q2.get_queue_stats()["pending"], 0)
        self.assertEqual(q2.get_queue_stats()["total"], 0)

    def test_corrupted_persist_file_handled_gracefully(self) -> None:
        """Повреждённый файл не роняет инициализацию — graceful degradation."""
        self._persist_path.write_text(
            '{"job_id":"abc"}\nNOT_JSON_LINE\n{"broken":',
            encoding="utf-8",
        )
        # Не должно бросать исключений
        q = TranscriptionQueue(persist_path=self._persist_path)
        # Валидная строка ("abc") не восстанавливается т.к. нет обязательных полей;
        # главное — инициализация не упала
        self.assertIsNotNone(q)

    def test_concurrent_save_safe(self) -> None:
        """Конкурентные enqueue() с persist_path не вызывают гонок/краш."""
        q = TranscriptionQueue(persist_path=self._persist_path)
        errors: list[Exception] = []

        def worker(idx: int) -> None:
            try:
                q.enqueue(f"/tmp/audio{idx}.mp3")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Исключения при конкурентном save: {errors}")
        # Все задания должны быть в очереди
        self.assertEqual(q.get_queue_stats()["pending"], 10)


class TestTranscriptionQueueEvictionW1722(unittest.TestCase):
    """W1722 — BUG 1: _jobs must not grow without bound (terminal-job eviction)."""

    def setUp(self):
        self.queue = TranscriptionQueue()

    # ------------------------------------------------------------------
    # Fail-before tests (verify the invariants we are enforcing)
    # ------------------------------------------------------------------

    def test_terminal_count_bounded_after_many_completions(self):
        """After N >> TERMINAL_MAX_COUNT completions, _jobs stays within cap.

        This test would FAIL on the unpatched code (dict grows to 500 entries).
        With the fix it must pass (_jobs <= TERMINAL_MAX_COUNT + active).
        """
        n = 500  # well above TERMINAL_MAX_COUNT (100)
        for i in range(n):
            jid = self.queue.enqueue(f"/tmp/audio_{i}.mp3")
            self.queue.mark_completed(jid, result={"text": f"transcript {i}"})

        with self.queue._lock:
            total_in_dict = len(self.queue._jobs)

        # No active (pending/processing) jobs remain — only terminal entries subject
        # to the cap.  The dict should be at most TERMINAL_MAX_COUNT.
        self.assertLessEqual(
            total_in_dict,
            TERMINAL_MAX_COUNT,
            f"_jobs has {total_in_dict} entries after {n} completions "
            f"(cap={TERMINAL_MAX_COUNT}) — unbounded growth detected",
        )

    def test_terminal_count_bounded_after_many_failures(self):
        """Same invariant for failed jobs (mark_failed path)."""
        n = 300
        for i in range(n):
            jid = self.queue.enqueue(f"/tmp/audio_{i}.mp3")
            self.queue.mark_failed(jid, error="test error")

        with self.queue._lock:
            total_in_dict = len(self.queue._jobs)

        self.assertLessEqual(total_in_dict, TERMINAL_MAX_COUNT)

    def test_terminal_count_bounded_after_many_cancellations(self):
        """Same invariant for cancelled jobs (cancel path)."""
        n = 200
        for i in range(n):
            jid = self.queue.enqueue(f"/tmp/audio_{i}.mp3")
            self.queue.cancel(jid)

        with self.queue._lock:
            total_in_dict = len(self.queue._jobs)

        self.assertLessEqual(total_in_dict, TERMINAL_MAX_COUNT)

    # ------------------------------------------------------------------
    # Pass-after tests (recent jobs remain queryable within retention window)
    # ------------------------------------------------------------------

    def test_recently_completed_job_still_queryable(self):
        """A job completed moments ago must still be accessible via get_status.

        Ensures we don't over-evict: the retention window (1 h by default) keeps
        freshly-finished jobs available for status polling by the caller.
        """
        # Enqueue 200 jobs and complete them all — fills well past the cap.
        jids = []
        for i in range(200):
            jids.append(self.queue.enqueue(f"/tmp/audio_{i}.mp3"))
        for jid in jids:
            self.queue.mark_completed(jid, result={"text": "ok"})

        # The LAST batch of TERMINAL_MAX_COUNT jobs should still be in the dict
        # (count-cap evicts oldest first).
        surviving = jids[-TERMINAL_MAX_COUNT:]
        for jid in surviving:
            status = self.queue.get_status(jid)
            self.assertNotEqual(
                status.get("error"), "not_found",
                f"Recently completed job {jid} was evicted prematurely",
            )

    def test_old_jobs_evicted_before_new_ones(self):
        """Count-cap evicts oldest-first; newest terminal jobs survive."""
        # Fill exactly TERMINAL_MAX_COUNT + 10 terminal jobs.
        overflow = 10
        all_jids = []
        for i in range(TERMINAL_MAX_COUNT + overflow):
            jid = self.queue.enqueue(f"/tmp/audio_{i}.mp3")
            all_jids.append(jid)
        for jid in all_jids:
            self.queue.mark_completed(jid)

        # The first `overflow` jobs (oldest) should have been evicted.
        for jid in all_jids[:overflow]:
            status = self.queue.get_status(jid)
            self.assertEqual(
                status.get("error"), "not_found",
                f"Old job {jid} should have been evicted by count cap",
            )

        # The last TERMINAL_MAX_COUNT jobs should still be present.
        for jid in all_jids[overflow:]:
            status = self.queue.get_status(jid)
            self.assertNotEqual(status.get("error"), "not_found",
                                f"Recent job {jid} should not be evicted yet")

    def test_time_based_eviction_respects_retention(self):
        """Jobs whose finished_at_monotonic is past the retention window are evicted.

        We patch finished_at_monotonic backward in time to simulate expiry without
        actually sleeping TERMINAL_RETENTION_SECONDS.
        """
        jid = self.queue.enqueue("/tmp/audio.mp3")
        self.queue.mark_completed(jid)

        # Confirm it is present immediately after completion.
        status = self.queue.get_status(jid)
        self.assertNotEqual(status.get("error"), "not_found")

        # Wind the finished_at_monotonic back past the retention window.
        import time as _time
        with self.queue._lock:
            job = self.queue._jobs.get(jid)
            if job is not None:
                job.finished_at_monotonic = (
                    _time.monotonic() - TERMINAL_RETENTION_SECONDS - 1
                )

        # Trigger eviction by completing a second job — mark_completed calls
        # _register_terminal → _evict_terminal_jobs internally.
        jid2 = self.queue.enqueue("/tmp/audio2.mp3")
        self.queue.mark_completed(jid2)

        # The manually-expired job should now be gone.
        status = self.queue.get_status(jid)
        self.assertEqual(
            status.get("error"), "not_found",
            "Expired job should be evicted after retention window passes",
        )

    def test_terminal_order_tracks_all_terminal_statuses(self):
        """_terminal_order is updated for completed, failed, and cancelled jobs."""
        jid_c = self.queue.enqueue("/tmp/c.mp3")
        jid_f = self.queue.enqueue("/tmp/f.mp3")
        jid_x = self.queue.enqueue("/tmp/x.mp3")

        self.queue.mark_completed(jid_c)
        self.queue.mark_failed(jid_f)
        self.queue.cancel(jid_x)

        with self.queue._lock:
            keys = list(self.queue._terminal_order.keys())

        self.assertIn(jid_c, keys)
        self.assertIn(jid_f, keys)
        self.assertIn(jid_x, keys)

    def test_eviction_summary_log_fires_on_count_cap_only(self):
        """Eviction debug log fires even when only the count cap triggers.

        Before the fix the logger.debug was inside the while-loop AND guarded by
        ``if expired:``, so it stayed silent for pure count-cap evictions (no
        time-expired jobs).  After the fix the single summary line fires whenever
        total_evicted > 0, regardless of which pass caused the eviction.
        """
        from unittest.mock import patch

        # Confirm no jobs have a past-retention finished_at_monotonic — only the
        # count cap should fire here.
        with patch.object(
            self.queue.__class__,
            "_evict_terminal_jobs",
            wraps=self.queue._evict_terminal_jobs,
        ):
            with self.assertLogs("KrabEar.Backend.TranscriptionQueue", level="DEBUG") as cm:
                # Fill exactly one past the cap so count-cap evicts one entry.
                for i in range(TERMINAL_MAX_COUNT + 1):
                    jid = self.queue.enqueue(f"/tmp/log_test_{i}.mp3")
                    self.queue.mark_completed(jid)

        # At least one eviction summary message must appear.
        eviction_msgs = [m for m in cm.output if "evicted" in m and "terminal" in m]
        self.assertTrue(
            len(eviction_msgs) >= 1,
            f"Expected eviction summary log but got: {cm.output[:5]}",
        )

    def test_active_jobs_not_affected_by_count_cap(self):
        """Active (pending/processing) jobs are never evicted by the count cap."""
        # Fill terminal cap completely.
        for i in range(TERMINAL_MAX_COUNT + 50):
            jid = self.queue.enqueue(f"/tmp/terminal_{i}.mp3")
            self.queue.mark_completed(jid)

        # Now add a pending and a processing job.
        pending_jid = self.queue.enqueue("/tmp/active_pending.mp3")
        processing_jid = self.queue.enqueue("/tmp/active_processing.mp3")
        self.queue.process_next()  # moves one to processing

        # Both active jobs must still be present regardless of cap pressure.
        pending_status = self.queue.get_status(pending_jid)
        processing_status = self.queue.get_status(processing_jid)

        # One of them transitioned to processing; the other is pending.
        statuses = {pending_status.get("status"), processing_status.get("status")}
        self.assertTrue(
            statuses.issubset({STATUS_PENDING, STATUS_PROCESSING}),
            f"Active jobs must not be evicted: {pending_status}, {processing_status}",
        )


class TestTranscriptionQueueResultSizeGuardW1722(unittest.TestCase):
    """W1722 — BUG 2: oversized result dicts must be truncated in mark_completed."""

    def setUp(self):
        self.queue = TranscriptionQueue()

    def test_normal_result_stored_verbatim(self):
        """Small result dicts (well under RESULT_MAX_BYTES) are stored as-is."""
        jid = self.queue.enqueue("/tmp/audio.mp3")
        result = {"text": "hello world", "confidence": 0.99}
        self.queue.mark_completed(jid, result=result)

        status = self.queue.get_status(jid)
        self.assertEqual(status["result"], result)
        self.assertNotIn("truncated", status.get("result", {}))

    def test_oversized_result_replaced_by_stub(self):
        """A result whose JSON encoding exceeds RESULT_MAX_BYTES becomes a stub.

        This test would NOT fail on unpatched code (it stores the big dict);
        with the fix the stored result must be the truncation stub.
        """
        # Build a result slightly larger than the limit.
        big_text = "x" * (RESULT_MAX_BYTES + 1024)
        big_result = {"text": big_text}

        jid = self.queue.enqueue("/tmp/big.mp3")
        self.queue.mark_completed(jid, result=big_result)

        status = self.queue.get_status(jid)
        stored = status.get("result")
        self.assertIsNotNone(stored, "result should not be None after mark_completed")
        self.assertTrue(
            stored.get("truncated") is True,
            f"Expected truncation stub, got: {str(stored)[:200]}",
        )
        self.assertIn("original_bytes", stored,
                      "stub must record the original byte count")
        self.assertGreater(stored["original_bytes"], RESULT_MAX_BYTES)

    def test_stub_original_bytes_is_accurate(self):
        """original_bytes in the stub must match the actual JSON-encoded size."""
        import json as _json
        big_text = "y" * (RESULT_MAX_BYTES + 2048)
        big_result = {"text": big_text}
        expected_bytes = len(_json.dumps(big_result, ensure_ascii=False).encode("utf-8"))

        jid = self.queue.enqueue("/tmp/big2.mp3")
        self.queue.mark_completed(jid, result=big_result)

        stored = self.queue.get_status(jid)["result"]
        self.assertEqual(stored["original_bytes"], expected_bytes)

    def test_none_result_stored_as_none(self):
        """mark_completed(result=None) must not trigger size guard logic."""
        jid = self.queue.enqueue("/tmp/audio.mp3")
        self.queue.mark_completed(jid, result=None)

        status = self.queue.get_status(jid)
        self.assertIsNone(status["result"])

    def test_borderline_result_not_truncated(self):
        """A result exactly at RESULT_MAX_BYTES must NOT be truncated."""
        import json as _json
        # Build a result whose encoded size is exactly RESULT_MAX_BYTES.
        # We use a simple string key and fill the value to hit the limit.
        # JSON overhead: '{"text": "..."}' = 11 bytes of structure.
        overhead = len(_json.dumps({"text": ""}).encode("utf-8"))
        fill_len = RESULT_MAX_BYTES - overhead
        borderline_result = {"text": "a" * fill_len}
        encoded_size = len(_json.dumps(borderline_result, ensure_ascii=False).encode("utf-8"))
        # Verify our arithmetic (should equal RESULT_MAX_BYTES exactly).
        self.assertEqual(encoded_size, RESULT_MAX_BYTES)

        jid = self.queue.enqueue("/tmp/border.mp3")
        self.queue.mark_completed(jid, result=borderline_result)

        stored = self.queue.get_status(jid)["result"]
        self.assertNotIn(
            "truncated", stored,
            "Borderline result (exactly at limit) must NOT be truncated",
        )


if __name__ == "__main__":
    unittest.main()
