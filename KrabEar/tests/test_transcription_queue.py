"""Unit-тесты для TranscriptionQueue."""

from __future__ import annotations
from backend.transcription_queue import (
    TranscriptionQueue,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    PRIORITY_DEFAULT,
    PRIORITY_MIN,
    PRIORITY_MAX,
)

import sys
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class EnqueueTestCase(unittest.TestCase):
    """Тесты постановки заданий в очередь."""

    def setUp(self) -> None:
        self.q = TranscriptionQueue()

    def test_enqueue_returns_job_id(self) -> None:
        job_id = self.q.enqueue("/tmp/audio.wav")
        self.assertIsInstance(job_id, str)
        self.assertTrue(job_id)

    def test_enqueue_default_priority(self) -> None:
        job_id = self.q.enqueue("/tmp/audio.wav")
        status = self.q.get_status(job_id)
        self.assertEqual(status["priority"], PRIORITY_DEFAULT)

    def test_enqueue_custom_priority(self) -> None:
        job_id = self.q.enqueue("/tmp/audio.wav", priority=1)
        status = self.q.get_status(job_id)
        self.assertEqual(status["priority"], 1)

    def test_enqueue_with_label(self) -> None:
        job_id = self.q.enqueue("/tmp/audio.wav", label="interview")
        status = self.q.get_status(job_id)
        self.assertEqual(status["label"], "interview")

    def test_enqueue_invalid_priority_too_low_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.q.enqueue("/tmp/audio.wav", priority=0)

    def test_enqueue_invalid_priority_too_high_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.q.enqueue("/tmp/audio.wav", priority=11)

    def test_enqueue_empty_file_path_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.q.enqueue("")

    def test_enqueue_whitespace_file_path_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.q.enqueue("   ")

    def test_enqueue_initial_status_is_pending(self) -> None:
        job_id = self.q.enqueue("/tmp/audio.wav")
        status = self.q.get_status(job_id)
        self.assertEqual(status["status"], STATUS_PENDING)

    def test_enqueue_unique_job_ids(self) -> None:
        ids = {self.q.enqueue("/tmp/audio.wav") for _ in range(10)}
        self.assertEqual(len(ids), 10)


class CancelTestCase(unittest.TestCase):
    """Тесты отмены заданий."""

    def setUp(self) -> None:
        self.q = TranscriptionQueue()

    def test_cancel_pending_job_returns_true(self) -> None:
        job_id = self.q.enqueue("/tmp/audio.wav")
        result = self.q.cancel(job_id)
        self.assertTrue(result)

    def test_cancel_sets_status_cancelled(self) -> None:
        job_id = self.q.enqueue("/tmp/audio.wav")
        self.q.cancel(job_id)
        status = self.q.get_status(job_id)
        self.assertEqual(status["status"], STATUS_CANCELLED)

    def test_cancel_nonexistent_job_returns_false(self) -> None:
        result = self.q.cancel("non-existent-id")
        self.assertFalse(result)

    def test_cancel_already_cancelled_returns_false(self) -> None:
        job_id = self.q.enqueue("/tmp/audio.wav")
        self.q.cancel(job_id)
        result = self.q.cancel(job_id)
        self.assertFalse(result)

    def test_cancel_processing_job_returns_false(self) -> None:
        job_id = self.q.enqueue("/tmp/audio.wav")
        self.q.process_next()
        result = self.q.cancel(job_id)
        self.assertFalse(result)

    def test_cancel_completed_job_returns_false(self) -> None:
        job_id = self.q.enqueue("/tmp/audio.wav")
        self.q.process_next()
        self.q.mark_completed(job_id)
        result = self.q.cancel(job_id)
        self.assertFalse(result)


class GetStatusTestCase(unittest.TestCase):
    """Тесты получения статуса задания."""

    def setUp(self) -> None:
        self.q = TranscriptionQueue()

    def test_get_status_contains_required_fields(self) -> None:
        job_id = self.q.enqueue("/tmp/audio.wav")
        status = self.q.get_status(job_id)
        for field in ("job_id", "file_path", "priority", "label", "status", "created_at"):
            self.assertIn(field, status)

    def test_get_status_nonexistent_returns_not_found(self) -> None:
        status = self.q.get_status("bad-id")
        self.assertIn("error", status)
        self.assertEqual(status["error"], "not_found")

    def test_get_status_file_path_matches(self) -> None:
        job_id = self.q.enqueue("/tmp/test.mp3")
        status = self.q.get_status(job_id)
        self.assertEqual(status["file_path"], "/tmp/test.mp3")


class ListQueueTestCase(unittest.TestCase):
    """Тесты вывода списка заданий."""

    def setUp(self) -> None:
        self.q = TranscriptionQueue()

    def test_list_queue_empty(self) -> None:
        result = self.q.list_queue()
        self.assertEqual(result, [])

    def test_list_queue_returns_all_jobs(self) -> None:
        self.q.enqueue("/tmp/a.wav")
        self.q.enqueue("/tmp/b.wav")
        self.q.enqueue("/tmp/c.wav")
        result = self.q.list_queue()
        self.assertEqual(len(result), 3)

    def test_list_queue_sorted_by_priority(self) -> None:
        self.q.enqueue("/tmp/low.wav", priority=9)
        self.q.enqueue("/tmp/high.wav", priority=1)
        self.q.enqueue("/tmp/mid.wav", priority=5)
        result = self.q.list_queue()
        priorities = [r["priority"] for r in result]
        self.assertEqual(priorities, sorted(priorities))

    def test_list_queue_same_priority_fifo_order(self) -> None:
        id1 = self.q.enqueue("/tmp/first.wav", priority=5)
        id2 = self.q.enqueue("/tmp/second.wav", priority=5)
        result = self.q.list_queue()
        self.assertEqual(result[0]["job_id"], id1)
        self.assertEqual(result[1]["job_id"], id2)


class GetQueueStatsTestCase(unittest.TestCase):
    """Тесты статистики очереди."""

    def setUp(self) -> None:
        self.q = TranscriptionQueue()

    def test_stats_empty_queue(self) -> None:
        stats = self.q.get_queue_stats()
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["processing"], 0)
        self.assertEqual(stats["completed"], 0)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["cancelled"], 0)
        self.assertEqual(stats["total"], 0)

    def test_stats_counts_pending(self) -> None:
        self.q.enqueue("/tmp/a.wav")
        self.q.enqueue("/tmp/b.wav")
        stats = self.q.get_queue_stats()
        self.assertEqual(stats["pending"], 2)
        self.assertEqual(stats["total"], 2)

    def test_stats_counts_processing(self) -> None:
        self.q.enqueue("/tmp/a.wav")
        self.q.process_next()
        stats = self.q.get_queue_stats()
        self.assertEqual(stats["processing"], 1)
        self.assertEqual(stats["pending"], 0)

    def test_stats_counts_completed(self) -> None:
        job_id = self.q.enqueue("/tmp/a.wav")
        self.q.process_next()
        self.q.mark_completed(job_id)
        stats = self.q.get_queue_stats()
        self.assertEqual(stats["completed"], 1)

    def test_stats_counts_failed(self) -> None:
        job_id = self.q.enqueue("/tmp/a.wav")
        self.q.process_next()
        self.q.mark_failed(job_id, "STT error")
        stats = self.q.get_queue_stats()
        self.assertEqual(stats["failed"], 1)

    def test_stats_counts_cancelled(self) -> None:
        job_id = self.q.enqueue("/tmp/a.wav")
        self.q.cancel(job_id)
        stats = self.q.get_queue_stats()
        self.assertEqual(stats["cancelled"], 1)

    def test_stats_total_equals_sum(self) -> None:
        id1 = self.q.enqueue("/tmp/a.wav")
        id2 = self.q.enqueue("/tmp/b.wav")
        self.q.enqueue("/tmp/c.wav")
        self.q.process_next()
        self.q.cancel(id2)
        self.q.mark_completed(id1)
        stats = self.q.get_queue_stats()
        self.assertEqual(
            stats["total"],
            stats["pending"] + stats["processing"] + stats["completed"] + stats["failed"] + stats["cancelled"],
        )


class ProcessNextTestCase(unittest.TestCase):
    """Тесты метода process_next."""

    def setUp(self) -> None:
        self.q = TranscriptionQueue()

    def test_process_next_empty_queue_returns_none(self) -> None:
        result = self.q.process_next()
        self.assertIsNone(result)

    def test_process_next_returns_job_dict(self) -> None:
        self.q.enqueue("/tmp/audio.wav")
        result = self.q.process_next()
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], STATUS_PROCESSING)

    def test_process_next_picks_highest_priority(self) -> None:
        self.q.enqueue("/tmp/low.wav", priority=9)
        self.q.enqueue("/tmp/high.wav", priority=1)
        result = self.q.process_next()
        self.assertEqual(result["file_path"], "/tmp/high.wav")

    def test_process_next_sets_started_at(self) -> None:
        self.q.enqueue("/tmp/audio.wav")
        result = self.q.process_next()
        self.assertIsNotNone(result["started_at"])

    def test_process_next_skips_processing_jobs(self) -> None:
        self.q.enqueue("/tmp/a.wav")
        self.q.enqueue("/tmp/b.wav")
        first = self.q.process_next()
        second = self.q.process_next()
        # Both should be different jobs
        self.assertNotEqual(first["job_id"], second["job_id"])

    def test_process_next_only_pending_after_all_processing(self) -> None:
        self.q.enqueue("/tmp/a.wav")
        self.q.process_next()
        # Queue now has one processing job, no pending
        result = self.q.process_next()
        self.assertIsNone(result)

    def test_process_next_fifo_same_priority(self) -> None:
        id1 = self.q.enqueue("/tmp/first.wav", priority=5)
        _id2 = self.q.enqueue("/tmp/second.wav", priority=5)  # noqa: F841
        first = self.q.process_next()
        self.assertEqual(first["job_id"], id1)


class MarkCompletedFailedTestCase(unittest.TestCase):
    """Тесты mark_completed и mark_failed."""

    def setUp(self) -> None:
        self.q = TranscriptionQueue()

    def test_mark_completed_sets_status(self) -> None:
        job_id = self.q.enqueue("/tmp/audio.wav")
        self.q.process_next()
        self.q.mark_completed(job_id, result={"text": "hello"})
        status = self.q.get_status(job_id)
        self.assertEqual(status["status"], STATUS_COMPLETED)
        self.assertEqual(status["result"]["text"], "hello")

    def test_mark_completed_nonexistent_returns_false(self) -> None:
        ok = self.q.mark_completed("bad-id")
        self.assertFalse(ok)

    def test_mark_completed_sets_finished_at(self) -> None:
        job_id = self.q.enqueue("/tmp/audio.wav")
        self.q.process_next()
        self.q.mark_completed(job_id)
        status = self.q.get_status(job_id)
        self.assertIsNotNone(status["finished_at"])

    def test_mark_failed_sets_status_and_error(self) -> None:
        job_id = self.q.enqueue("/tmp/audio.wav")
        self.q.process_next()
        self.q.mark_failed(job_id, error="Model unavailable")
        status = self.q.get_status(job_id)
        self.assertEqual(status["status"], STATUS_FAILED)
        self.assertEqual(status["error"], "Model unavailable")

    def test_mark_failed_nonexistent_returns_false(self) -> None:
        ok = self.q.mark_failed("bad-id")
        self.assertFalse(ok)


class IPCHandlersTestCase(unittest.TestCase):
    """Тесты IPC-обработчиков (handle_*)."""

    def setUp(self) -> None:
        self.q = TranscriptionQueue()

    def test_handle_enqueue_returns_job_id(self) -> None:
        result = self.q.handle_enqueue({"file_path": "/tmp/audio.wav", "priority": 3})
        self.assertIn("job_id", result)
        self.assertTrue(result["job_id"])

    def test_handle_enqueue_missing_file_path_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.q.handle_enqueue({})

    def test_handle_enqueue_invalid_priority_type_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.q.handle_enqueue({"file_path": "/tmp/audio.wav", "priority": "abc"})

    def test_handle_cancel_success(self) -> None:
        job_id = self.q.enqueue("/tmp/audio.wav")
        result = self.q.handle_cancel({"job_id": job_id})
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["job_id"], job_id)

    def test_handle_cancel_missing_job_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.q.handle_cancel({})

    def test_handle_get_status_returns_job(self) -> None:
        job_id = self.q.enqueue("/tmp/audio.wav")
        result = self.q.handle_get_status({"job_id": job_id})
        self.assertEqual(result["job_id"], job_id)

    def test_handle_get_status_missing_job_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.q.handle_get_status({})

    def test_handle_list_queue_returns_jobs_and_stats(self) -> None:
        self.q.enqueue("/tmp/audio.wav")
        result = self.q.handle_list_queue({})
        self.assertIn("jobs", result)
        self.assertIn("stats", result)
        self.assertEqual(len(result["jobs"]), 1)

    def test_handle_enqueue_label_optional(self) -> None:
        result = self.q.handle_enqueue({"file_path": "/tmp/audio.wav"})
        job_id = result["job_id"]
        status = self.q.get_status(job_id)
        self.assertEqual(status["label"], "")


class ThreadSafetyTestCase(unittest.TestCase):
    """Тесты потокобезопасности."""

    def test_concurrent_enqueue(self) -> None:
        q = TranscriptionQueue()
        ids: list[str] = []
        lock = threading.Lock()

        def enqueue_worker() -> None:
            job_id = q.enqueue("/tmp/audio.wav", priority=5)
            with lock:
                ids.append(job_id)

        threads = [threading.Thread(target=enqueue_worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(ids), 20)
        self.assertEqual(len(set(ids)), 20)  # все job_id уникальны

    def test_concurrent_process_next_no_double_processing(self) -> None:
        q = TranscriptionQueue()
        # Добавляем 5 заданий
        for i in range(5):
            q.enqueue(f"/tmp/audio_{i}.wav")

        processed_ids: list[str] = []
        lock = threading.Lock()

        def process_worker() -> None:
            job = q.process_next()
            if job is not None:
                with lock:
                    processed_ids.append(job["job_id"])

        threads = [threading.Thread(target=process_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Не должно быть дублей
        self.assertEqual(len(processed_ids), len(set(processed_ids)))
        # Обработано не больше, чем было заданий
        self.assertLessEqual(len(processed_ids), 5)


class PriorityOrderTestCase(unittest.TestCase):
    """Тесты корректности порядка приоритетов."""

    def test_priority_boundaries_valid(self) -> None:
        q = TranscriptionQueue()
        id_min = q.enqueue("/tmp/a.wav", priority=PRIORITY_MIN)
        id_max = q.enqueue("/tmp/b.wav", priority=PRIORITY_MAX)
        self.assertIsNotNone(id_min)
        self.assertIsNotNone(id_max)

    def test_process_next_respects_priority_ordering(self) -> None:
        q = TranscriptionQueue()
        expected_order = [1, 2, 3, 7, 9]
        # Добавляем в обратном порядке
        for p in reversed(expected_order):
            q.enqueue(f"/tmp/{p}.wav", priority=p)

        actual_order = []
        while True:
            job = q.process_next()
            if job is None:
                break
            actual_order.append(job["priority"])

        self.assertEqual(actual_order, expected_order)

    def test_stats_after_full_lifecycle(self) -> None:
        q = TranscriptionQueue()
        id1 = q.enqueue("/tmp/a.wav")
        id2 = q.enqueue("/tmp/b.wav")
        id3 = q.enqueue("/tmp/c.wav")
        q.cancel(id3)
        q.process_next()   # id1 processing
        q.process_next()   # id2 processing (id3 is cancelled, skip)
        q.mark_completed(id1)
        q.mark_failed(id2, "error")
        stats = q.get_queue_stats()
        self.assertEqual(stats["completed"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["cancelled"], 1)
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["processing"], 0)
        self.assertEqual(stats["total"], 3)


if __name__ == "__main__":
    unittest.main()
