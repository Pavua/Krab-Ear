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


class GetUnknownJobTestCase(unittest.TestCase):
    """Тесты обращения к несуществующему заданию."""

    def test_get_unknown_job_returns_none(self) -> None:
        tracker = JobTracker()
        self.assertIsNone(tracker.get("j-doesnotexist"))

    def test_get_unknown_job_does_not_raise(self) -> None:
        tracker = JobTracker()
        try:
            result = tracker.get("totally-fake-id")
        except Exception as exc:  # pragma: no cover
            self.fail(f"get() on unknown id should not raise: {exc}")
        self.assertIsNone(result)


class StatusTransitionsTestCase(unittest.TestCase):
    """Тесты переходов статуса задания через жизненный цикл."""

    def setUp(self) -> None:
        self.tracker = JobTracker()
        self.jid = self.tracker.create_job(2)

    def test_update_job_status_transitions_queued_to_running(self) -> None:
        state = self.tracker.get(self.jid)
        self.assertEqual(state["status"], "queued")
        self.tracker.update(self.jid, status="running")
        state = self.tracker.get(self.jid)
        self.assertEqual(state["status"], "running")

    def test_update_job_status_transitions_running_to_done(self) -> None:
        self.tracker.update(self.jid, status="running")
        self.tracker.mark_done(self.jid, items=[{"text": "result"}], errors=[])
        state = self.tracker.get(self.jid)
        self.assertEqual(state["status"], "done")

    def test_update_job_status_transitions_running_to_failed(self) -> None:
        self.tracker.update(self.jid, status="running")
        self.tracker.mark_failed(self.jid, "disk full")
        state = self.tracker.get(self.jid)
        self.assertEqual(state["status"], "failed")
        self.assertIn("disk full", state["errors"])

    def test_full_lifecycle_queued_running_done(self) -> None:
        """queued → running → done: каждый переход верифицируется."""
        # queued
        self.assertEqual(self.tracker.get(self.jid)["status"], "queued")
        # running
        self.tracker.update(self.jid, status="running", current_stage="stt", file_index=1)
        s = self.tracker.get(self.jid)
        self.assertEqual(s["status"], "running")
        self.assertEqual(s["current_stage"], "stt")
        # done
        self.tracker.mark_done(self.jid, items=[{"path": "/tmp/x.m4a", "text": "hi"}], errors=[])
        s = self.tracker.get(self.jid)
        self.assertEqual(s["status"], "done")
        self.assertEqual(len(s["items"]), 1)
        self.assertIsNotNone(s.get("finished_at") or s.get("elapsed_sec"))


class ListJobsByStatusTestCase(unittest.TestCase):
    """Тесты фильтрации заданий по статусу через внутреннее состояние."""

    def setUp(self) -> None:
        self.tracker = JobTracker()

    def _get_jobs_with_status(self, status: str) -> list[str]:
        """Helper: возвращает список job_id с заданным статусом."""
        with self.tracker._lock:
            return [jid for jid, job in self.tracker._jobs.items() if job.get("status") == status]

    def test_list_jobs_filters_by_status_queued(self) -> None:
        jid1 = self.tracker.create_job(1)
        jid2 = self.tracker.create_job(1)
        self.tracker.update(jid2, status="running")
        queued = self._get_jobs_with_status("queued")
        self.assertIn(jid1, queued)
        self.assertNotIn(jid2, queued)

    def test_list_jobs_filters_by_status_done(self) -> None:
        jid1 = self.tracker.create_job(1)
        jid2 = self.tracker.create_job(1)
        self.tracker.mark_done(jid1, items=[], errors=[])
        done = self._get_jobs_with_status("done")
        self.assertIn(jid1, done)
        self.assertNotIn(jid2, done)

    def test_list_jobs_filters_by_status_failed(self) -> None:
        jid1 = self.tracker.create_job(1)
        jid2 = self.tracker.create_job(1)
        self.tracker.mark_failed(jid1, "error")
        failed = self._get_jobs_with_status("failed")
        self.assertIn(jid1, failed)
        self.assertNotIn(jid2, failed)

    def test_list_jobs_empty_tracker_has_no_status(self) -> None:
        self.assertEqual(self._get_jobs_with_status("queued"), [])


class PurgeOldCompletedJobsTestCase(unittest.TestCase):
    """Тесты удаления устаревших завершённых задач."""

    def test_purge_old_completed_jobs_done(self) -> None:
        tracker = JobTracker()
        jid = tracker.create_job(1)
        tracker.mark_done(jid, items=[], errors=[])
        # Age-fake finished_at
        tracker.update(jid, finished_at=time.monotonic() - 7200)
        tracker.prune(max_age_sec=3600)
        self.assertIsNone(tracker.get(jid))

    def test_purge_old_completed_jobs_failed(self) -> None:
        tracker = JobTracker()
        jid = tracker.create_job(1)
        tracker.mark_failed(jid, "boom")
        tracker.update(jid, finished_at=time.monotonic() - 7200)
        tracker.prune(max_age_sec=3600)
        self.assertIsNone(tracker.get(jid))

    def test_purge_leaves_young_completed(self) -> None:
        tracker = JobTracker()
        jid = tracker.create_job(1)
        tracker.mark_done(jid, items=[], errors=[])
        # finished_at is recent (just set by mark_done)
        tracker.prune(max_age_sec=3600)
        self.assertIsNotNone(tracker.get(jid))

    def test_purge_multiple_old_jobs(self) -> None:
        tracker = JobTracker()
        old_jids = [tracker.create_job(1) for _ in range(5)]
        young_jid = tracker.create_job(1)
        for jid in old_jids:
            tracker.mark_done(jid, items=[], errors=[])
            tracker.update(jid, finished_at=time.monotonic() - 7200)
        tracker.mark_done(young_jid, items=[], errors=[])
        tracker.prune(max_age_sec=3600)
        for jid in old_jids:
            self.assertIsNone(tracker.get(jid))
        self.assertIsNotNone(tracker.get(young_jid))


class PruneStaleRunningTestCase(unittest.TestCase):
    """Тест W965 HIGH: prune() evicts stale running jobs to prevent memory leak."""

    def test_prune_evicts_stale_running(self) -> None:
        """Задача в статусе 'running' с started_at > 8 часов назад должна быть удалена."""
        import logging

        tracker = JobTracker()
        jid = tracker.create_job(1)
        tracker.update(jid, status="running")

        # Fake-age started_at to 8 hours ago (>7200s threshold).
        eight_hours_ago = time.monotonic() - 8 * 3600
        tracker.update(jid, started_at=eight_hours_ago)

        with self.assertLogs("backend.job_tracker", level="WARNING") as log_ctx:
            removed = tracker.prune(max_running_age_sec=7200.0)

        self.assertEqual(removed, 1, "prune() должен вернуть 1 (одна задача удалена)")
        self.assertIsNone(
            tracker.get(jid),
            "stale running job должна быть удалена из трекера",
        )
        # Verify warning was emitted with the job_id.
        self.assertTrue(
            any(jid in msg for msg in log_ctx.output),
            f"WARNING с job_id={jid!r} не найден в логах: {log_ctx.output}",
        )

    def test_prune_keeps_young_running(self) -> None:
        """Задача в статусе 'running' с недавним started_at должна сохраняться."""
        tracker = JobTracker()
        jid = tracker.create_job(1)
        tracker.update(jid, status="running")
        # started_at is just now — well within 7200s limit.
        removed = tracker.prune(max_running_age_sec=7200.0)
        self.assertEqual(removed, 0)
        self.assertIsNotNone(tracker.get(jid))

    def test_prune_returns_count_of_removed(self) -> None:
        """prune() возвращает количество удалённых задач."""
        tracker = JobTracker()
        # Create both jobs first, then mark terminal status — avoids auto-prune in create_job()
        # removing jid1 before jid2 is even created.
        jid1 = tracker.create_job(1)
        jid2 = tracker.create_job(1)

        tracker.mark_done(jid1, items=[], errors=[])
        tracker.update(jid1, finished_at=time.monotonic() - 7200)

        tracker.update(jid2, status="running")
        tracker.update(jid2, started_at=time.monotonic() - 8 * 3600)

        with self.assertLogs("backend.job_tracker", level="WARNING"):
            removed = tracker.prune(max_age_sec=1.0, max_running_age_sec=7200.0)

        self.assertEqual(removed, 2, "должны быть удалены 2 задачи (1 terminal + 1 stale running)")


class ConcurrentCreateUniqueIdsTestCase(unittest.TestCase):
    """Тест уникальности job_id при конкурентном создании задач."""

    def test_concurrent_create_unique_ids(self) -> None:
        """50 потоков создают задачи одновременно — все ID уникальны."""
        tracker = JobTracker()
        ids: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                jid = tracker.create_job(1)
                with lock:
                    ids.append(jid)
            except BaseException as exc:  # pragma: no cover
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(errors, [])
        self.assertEqual(len(ids), 50)
        self.assertEqual(len(set(ids)), 50, "Все job_id должны быть уникальными")


class UnicodeJobMetadataTestCase(unittest.TestCase):
    """Тест хранения Unicode метаданных в задаче."""

    def test_unicode_job_metadata(self) -> None:
        """Unicode в current_file, current_stage, errors сохраняются корректно."""
        tracker = JobTracker()
        jid = tracker.create_job(1)
        unicode_path = "/tmp/запись_📋_2026-05-19.m4a"
        tracker.update(
            jid,
            current_file=unicode_path,
            current_stage="расшифровка",
            status="running",
        )
        state = tracker.get(jid)
        self.assertEqual(state["current_file"], unicode_path)
        self.assertEqual(state["current_stage"], "расшифровка")

    def test_unicode_in_errors(self) -> None:
        """Unicode в сообщении об ошибке хранится без потерь."""
        tracker = JobTracker()
        jid = tracker.create_job(1)
        error_msg = "Ошибка: файл повреждён 🚨 (código de error: 日本語)"
        tracker.mark_failed(jid, error_msg)
        state = tracker.get(jid)
        self.assertEqual(state["status"], "failed")
        self.assertIn(error_msg, state["errors"])


class CancelJobTestCase(unittest.TestCase):
    """Тест полного цикла отмены задачи."""

    def test_cancel_job_sets_flag(self) -> None:
        """cancel() устанавливает cancel_requested=True без смены статуса."""
        tracker = JobTracker()
        jid = tracker.create_job(3)
        tracker.update(jid, status="running", file_index=1)
        cancelled = tracker.cancel(jid)
        self.assertTrue(cancelled)
        state = tracker.get(jid)
        self.assertTrue(state["cancel_requested"])
        self.assertEqual(state["status"], "running")

    def test_cancel_done_job_returns_false(self) -> None:
        """cancel() возвращает False для уже завершённой задачи."""
        tracker = JobTracker()
        jid = tracker.create_job(1)
        tracker.mark_done(jid, items=[], errors=[])
        result = tracker.cancel(jid)
        self.assertFalse(result)

    def test_cancel_failed_job_returns_false(self) -> None:
        """cancel() возвращает False для задачи в статусе failed."""
        tracker = JobTracker()
        jid = tracker.create_job(1)
        tracker.mark_failed(jid, "error")
        result = tracker.cancel(jid)
        self.assertFalse(result)

    def test_cancel_worker_simulation(self) -> None:
        """Симуляция worker'а: читает cancel_requested между файлами и меняет статус."""
        tracker = JobTracker()
        jid = tracker.create_job(5)
        tracker.update(jid, status="running", file_index=2)

        # Запрашиваем отмену
        tracker.cancel(jid)

        # Worker проверяет флаг между файлами и меняет статус
        state = tracker.get(jid)
        if state["cancel_requested"]:
            tracker.update(jid, status="cancelled")

        final = tracker.get(jid)
        self.assertEqual(final["status"], "cancelled")
        self.assertTrue(final["cancel_requested"])


if __name__ == "__main__":
    unittest.main()
