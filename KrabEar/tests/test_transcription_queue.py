"""Тесты для TranscriptionQueue.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_transcription_queue.py -v
"""

import sys
import os
import unittest
import time

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
        import threading
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

        import threading
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


if __name__ == "__main__":
    unittest.main()
