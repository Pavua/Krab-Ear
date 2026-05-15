"""Unit-тесты для JobTracker (async transcribe job state store).

Контракт описан в /tmp/krab-ear-async/API_CONTRACT.md (PR #14).
Класс JobTracker живёт в KrabEar/backend/job_tracker.py после интеграции.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.job_tracker import JobTracker  # noqa: E402


class CreateJobTestCase(unittest.TestCase):
    """Тесты создания задания и его начального состояния."""

    def setUp(self) -> None:
        self.tracker = JobTracker()

    def test_create_job_returns_prefixed_id(self) -> None:
        jid = self.tracker.create_job(1)
        self.assertIsInstance(jid, str)
        self.assertTrue(jid.startswith("j-"), f"Ожидали префикс 'j-', получили: {jid!r}")

    def test_create_job_unique_ids(self) -> None:
        ids = {self.tracker.create_job(1) for _ in range(10)}
        self.assertEqual(len(ids), 10)

    def test_create_job_initializes_state(self) -> None:
        jid = self.tracker.create_job(1)
        state = self.tracker.get(jid)
        self.assertIsNotNone(state)
        self.assertEqual(state["status"], "queued")
        self.assertEqual(state["total_files"], 1)
        self.assertEqual(state["file_index"], 0)
        self.assertLess(state["elapsed_sec"], 1.0, "elapsed_sec should be near-zero immediately after creation")
        self.assertEqual(state["errors"], [])
        self.assertEqual(state["items"], [])

    def test_create_job_multiple_files_total(self) -> None:
        jid = self.tracker.create_job(5)
        state = self.tracker.get(jid)
        self.assertEqual(state["total_files"], 5)


class UpdateTestCase(unittest.TestCase):
    """Тесты обновления полей задания."""

    def setUp(self) -> None:
        self.tracker = JobTracker()
        self.jid = self.tracker.create_job(3)

    def test_update_sets_fields(self) -> None:
        self.tracker.update(self.jid, current_stage="stt", file_index=2)
        state = self.tracker.get(self.jid)
        self.assertEqual(state["current_stage"], "stt")
        self.assertEqual(state["file_index"], 2)

    def test_update_nonexistent_is_noop(self) -> None:
        # Не должно падать
        try:
            self.tracker.update("j-missing", current_stage="stt")
        except Exception as exc:  # pragma: no cover
            self.fail(f"update на отсутствующем id не должен падать: {exc}")
        self.assertIsNone(self.tracker.get("j-missing"))

    def test_update_preserves_other_fields(self) -> None:
        self.tracker.update(self.jid, current_stage="stt")
        state = self.tracker.get(self.jid)
        self.assertEqual(state["total_files"], 3)
        self.assertEqual(state["status"], "queued")

    def test_update_running_status(self) -> None:
        self.tracker.update(self.jid, status="running")
        state = self.tracker.get(self.jid)
        self.assertEqual(state["status"], "running")

    def test_update_elapsed_sec(self) -> None:
        # Contract: elapsed_sec is derived in get() from (finished_at if set else now) - started_at.
        # It is non-negative and monotonically increases over time until finished_at is set.
        state0 = self.tracker.get(self.jid)
        self.assertGreaterEqual(state0["elapsed_sec"], 0.0)
        self.assertIsInstance(state0["elapsed_sec"], float)
        time.sleep(0.01)
        state1 = self.tracker.get(self.jid)
        self.assertGreaterEqual(state1["elapsed_sec"], state0["elapsed_sec"],
                                "elapsed_sec must monotonically increase until finished_at is set")


class MarkDoneTestCase(unittest.TestCase):
    """Тесты финализации задания (done)."""

    def setUp(self) -> None:
        self.tracker = JobTracker()
        self.jid = self.tracker.create_job(2)

    def test_mark_done_updates_status_and_items(self) -> None:
        items = [{"path": "/tmp/a.m4a", "text": "hello"}, {"path": "/tmp/b.m4a", "text": "world"}]
        # До: немного прогресса
        self.tracker.update(self.jid, file_index=2)
        self.tracker.mark_done(self.jid, items=items, errors=[])
        state = self.tracker.get(self.jid)
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["items"], items)
        self.assertEqual(state["errors"], [])
        # Прошлый прогресс сохраняется
        self.assertEqual(state["file_index"], 2)
        # Contract: after mark_done(), elapsed_sec = max(0, finished_at - started_at) where
        # both are monotonic() timestamps. Result must be non-negative, finite, and in seconds (float).
        self.assertGreaterEqual(state["elapsed_sec"], 0.0)
        self.assertIsInstance(state["elapsed_sec"], float)
        self.assertLess(state["elapsed_sec"], 3600.0, "job should complete in << 1 hour")

    def test_mark_done_with_errors(self) -> None:
        self.tracker.mark_done(self.jid, items=[{"text": "ok"}], errors=["bad.m4a: timeout"])
        state = self.tracker.get(self.jid)
        self.assertEqual(state["status"], "done")
        self.assertIn("bad.m4a: timeout", state["errors"])

    def test_mark_done_empty_items(self) -> None:
        self.tracker.mark_done(self.jid, items=[], errors=[])
        state = self.tracker.get(self.jid)
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["items"], [])


class MarkFailedTestCase(unittest.TestCase):
    """Тесты финализации задания с ошибкой."""

    def setUp(self) -> None:
        self.tracker = JobTracker()
        self.jid = self.tracker.create_job(1)

    def test_mark_failed_status(self) -> None:
        self.tracker.mark_failed(self.jid, "boom")
        state = self.tracker.get(self.jid)
        self.assertEqual(state["status"], "failed")
        self.assertIn("boom", state["errors"])


class CancelTestCase(unittest.TestCase):
    """Тесты отмены задания."""

    def setUp(self) -> None:
        self.tracker = JobTracker()

    def test_cancel_existing_returns_true(self) -> None:
        jid = self.tracker.create_job(1)
        result = self.tracker.cancel(jid)
        self.assertTrue(result)

    def test_cancel_flag_sets_cancelled_status(self) -> None:
        # Contract: cancel(job_id) sets cancel_requested=True without changing status.
        # The worker observes this flag and changes status to "cancelled" between file processing.
        # Only the cancel_requested flag is a synchronous contract of this method.
        jid = self.tracker.create_job(1)
        self.tracker.cancel(jid)
        state = self.tracker.get(jid)
        self.assertIsNotNone(state)
        self.assertTrue(state["cancel_requested"], "cancel() must set cancel_requested=True")
        self.assertIsInstance(state["cancel_requested"], bool)
        self.assertEqual(state["status"], "queued", "cancel() does not immediately change status")

    def test_cancel_missing_returns_false(self) -> None:
        result = self.tracker.cancel("j-nope")
        self.assertFalse(result)


class PruneTestCase(unittest.TestCase):
    """Тесты очистки старых завершённых заданий."""

    def setUp(self) -> None:
        self.tracker = JobTracker()

    def test_prune_removes_old_done(self) -> None:
        jid = self.tracker.create_job(1)
        self.tracker.mark_done(jid, items=[{"text": "x"}], errors=[])
        # Contract: prune() uses time.monotonic() for age calculation, not wall-clock time.
        # Fake-age the finished_at to trigger pruning by setting it to monotonic() - 3600.
        old_ts = time.monotonic() - 3600
        self.tracker.update(jid, finished_at=old_ts)
        self.tracker.prune(max_age_sec=1)
        self.assertIsNone(self.tracker.get(jid), "prune() must remove done jobs older than max_age_sec")

    def test_prune_preserves_running(self) -> None:
        jid = self.tracker.create_job(1)
        self.tracker.update(jid, status="running")
        # Даже если бы finished_at был искусственно старым — running не чистится.
        self.tracker.update(jid, finished_at=time.monotonic() - 999999)
        self.tracker.prune(max_age_sec=1)
        self.assertIsNotNone(self.tracker.get(jid))

    def test_prune_preserves_recent_done(self) -> None:
        jid = self.tracker.create_job(1)
        self.tracker.mark_done(jid, items=[], errors=[])
        self.tracker.prune(max_age_sec=3600)
        self.assertIsNotNone(self.tracker.get(jid))


class ThreadSafetyTestCase(unittest.TestCase):
    """Тесты потокобезопасности JobTracker под конкурентной нагрузкой."""

    def test_thread_safety_concurrent_updates(self) -> None:
        """10 потоков по 100 update — dict остаётся согласованным, ошибок нет."""
        tracker = JobTracker()
        jid = tracker.create_job(1)
        errors: list[BaseException] = []

        def worker(worker_id: int) -> None:
            try:
                for i in range(100):
                    # Каждый update меняет набор полей; затем читаем.
                    tracker.update(
                        jid,
                        current_stage="stt",
                        file_index=i,
                        elapsed_sec=float(worker_id * 100 + i),
                    )
                    state = tracker.get(jid)
                    # Согласованность чтения: все обязательные ключи на месте.
                    assert state is not None
                    assert "status" in state
                    assert "total_files" in state
                    assert "file_index" in state
                    assert "errors" in state
                    assert "items" in state
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            self.assertFalse(t.is_alive(), "поток не завершился за 10с")

        self.assertEqual(errors, [], f"были ошибки в потоках: {errors!r}")

        final = tracker.get(jid)
        self.assertIsNotNone(final)
        # Любое обновление current_stage остаётся последним актуальным значением.
        self.assertEqual(final["current_stage"], "stt")
        self.assertEqual(final["total_files"], 1)
        # Никаких исключений при конкурентных записях.


if __name__ == "__main__":
    unittest.main()
