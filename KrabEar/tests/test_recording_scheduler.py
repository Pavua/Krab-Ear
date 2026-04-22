"""Тесты для RecordingScheduler."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# noqa: E402
from backend.recording_scheduler import RecordingScheduler, STATUS_PENDING, STATUS_COMPLETED, STATUS_CANCELLED  # noqa: E402


def _future_iso(seconds: int = 3600) -> str:
    """Возвращает ISO строку в будущем."""
    dt = datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)
    return dt.isoformat()


def _past_iso(seconds: int = 1) -> str:
    """Возвращает ISO строку в прошлом."""
    dt = datetime.now(tz=timezone.utc) - timedelta(seconds=seconds)
    return dt.isoformat()


class TestScheduleRecording(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.sched = RecordingScheduler(data_dir=self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_schedule_returns_entry_with_id(self):
        entry = self.sched.schedule_recording(start_time=_future_iso(), duration_sec=60)
        self.assertIn("id", entry)
        self.assertEqual(entry["status"], STATUS_PENDING)
        self.assertEqual(entry["duration_sec"], 60)

    def test_schedule_with_label(self):
        entry = self.sched.schedule_recording(start_time=_future_iso(), duration_sec=30, label="Встреча")
        self.assertEqual(entry["label"], "Встреча")

    def test_schedule_invalid_duration_raises(self):
        with self.assertRaises(ValueError):
            self.sched.schedule_recording(start_time=_future_iso(), duration_sec=0)
        with self.assertRaises(ValueError):
            self.sched.schedule_recording(start_time=_future_iso(), duration_sec=-5)

    def test_schedule_invalid_start_time_raises(self):
        with self.assertRaises(ValueError):
            self.sched.schedule_recording(start_time="not-a-date", duration_sec=60)

    def test_cancel_pending_returns_true(self):
        entry = self.sched.schedule_recording(start_time=_future_iso(), duration_sec=60)
        result = self.sched.cancel_scheduled(entry["id"])
        self.assertTrue(result)
        items = self.sched.list_scheduled()
        self.assertEqual(items[0]["status"], STATUS_CANCELLED)

    def test_cancel_nonexistent_returns_false(self):
        result = self.sched.cancel_scheduled("nonexistent-id")
        self.assertFalse(result)

    def test_cancel_already_cancelled_returns_false(self):
        entry = self.sched.schedule_recording(start_time=_future_iso(), duration_sec=60)
        self.sched.cancel_scheduled(entry["id"])
        result = self.sched.cancel_scheduled(entry["id"])
        self.assertFalse(result)

    def test_list_scheduled_returns_all(self):
        self.sched.schedule_recording(start_time=_future_iso(3600), duration_sec=60)
        self.sched.schedule_recording(start_time=_future_iso(7200), duration_sec=120, label="Second")
        items = self.sched.list_scheduled()
        self.assertEqual(len(items), 2)

    def test_list_scheduled_sorted_by_start_time(self):
        self.sched.schedule_recording(start_time=_future_iso(7200), duration_sec=60, label="Later")
        self.sched.schedule_recording(start_time=_future_iso(1800), duration_sec=60, label="Earlier")
        items = self.sched.list_scheduled()
        self.assertEqual(items[0]["label"], "Earlier")
        self.assertEqual(items[1]["label"], "Later")

    def test_get_next_scheduled_returns_soonest_pending(self):
        self.sched.schedule_recording(start_time=_future_iso(7200), duration_sec=60, label="Far")
        self.sched.schedule_recording(start_time=_future_iso(1800), duration_sec=60, label="Near")
        next_item = self.sched.get_next_scheduled()
        self.assertIsNotNone(next_item)
        self.assertEqual(next_item["label"], "Near")

    def test_get_next_scheduled_none_when_empty(self):
        result = self.sched.get_next_scheduled()
        self.assertIsNone(result)

    def test_get_next_scheduled_ignores_cancelled(self):
        entry = self.sched.schedule_recording(start_time=_future_iso(1800), duration_sec=60)
        self.sched.cancel_scheduled(entry["id"])
        result = self.sched.get_next_scheduled()
        self.assertIsNone(result)

    def test_check_and_trigger_fires_past_entry(self):
        # Создаём запись с start_time в прошлом (1 секунду назад)
        self.sched.schedule_recording(start_time=_past_iso(1), duration_sec=60, label="Now")
        result = self.sched.check_and_trigger()
        self.assertIsNotNone(result)
        self.assertEqual(result["label"], "Now")
        self.assertEqual(result["duration_sec"], 60)

    def test_check_and_trigger_marks_completed(self):
        self.sched.schedule_recording(start_time=_past_iso(1), duration_sec=30)
        self.sched.check_and_trigger()
        items = self.sched.list_scheduled()
        self.assertEqual(items[0]["status"], STATUS_COMPLETED)

    def test_check_and_trigger_none_when_future(self):
        self.sched.schedule_recording(start_time=_future_iso(3600), duration_sec=60)
        result = self.sched.check_and_trigger()
        self.assertIsNone(result)

    def test_check_and_trigger_none_when_empty(self):
        result = self.sched.check_and_trigger()
        self.assertIsNone(result)


class TestRecordingSchedulerPersistence(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_persists_to_json_file(self):
        sched = RecordingScheduler(data_dir=self._tmpdir.name)
        sched.schedule_recording(start_time=_future_iso(), duration_sec=60, label="Persist Test")
        json_file = Path(self._tmpdir.name) / "scheduled_recordings.json"
        self.assertTrue(json_file.exists())
        data = json.loads(json_file.read_text())
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["label"], "Persist Test")

    def test_loads_existing_schedules_on_init(self):
        sched1 = RecordingScheduler(data_dir=self._tmpdir.name)
        entry = sched1.schedule_recording(start_time=_future_iso(), duration_sec=90, label="Reloaded")
        schedule_id = entry["id"]

        sched2 = RecordingScheduler(data_dir=self._tmpdir.name)
        items = sched2.list_scheduled()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], schedule_id)
        self.assertEqual(items[0]["label"], "Reloaded")


class TestRecordingSchedulerIPCHandlers(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.sched = RecordingScheduler(data_dir=self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_handle_schedule_recording(self):
        result = self.sched.handle_schedule_recording({
            "start_time": _future_iso(),
            "duration_sec": 45,
            "label": "IPC Test",
        })
        self.assertIn("schedule", result)
        self.assertEqual(result["schedule"]["duration_sec"], 45)

    def test_handle_schedule_recording_missing_start_time(self):
        with self.assertRaises(ValueError):
            self.sched.handle_schedule_recording({"duration_sec": 60})

    def test_handle_cancel_scheduled_recording(self):
        entry = self.sched.schedule_recording(start_time=_future_iso(), duration_sec=60)
        result = self.sched.handle_cancel_scheduled_recording({"schedule_id": entry["id"]})
        self.assertTrue(result["cancelled"])

    def test_handle_cancel_missing_id_raises(self):
        with self.assertRaises(ValueError):
            self.sched.handle_cancel_scheduled_recording({})

    def test_handle_list_scheduled_recordings(self):
        self.sched.schedule_recording(start_time=_future_iso(), duration_sec=60)
        result = self.sched.handle_list_scheduled_recordings({})
        self.assertIn("schedules", result)
        self.assertIn("count", result)
        self.assertEqual(result["count"], 1)

    def test_handle_list_scheduled_recordings_empty(self):
        result = self.sched.handle_list_scheduled_recordings({})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["schedules"], [])


class TestCheckAndTriggerAdvanced(unittest.TestCase):
    """Advanced run_due (check_and_trigger) scenarios."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.sched = RecordingScheduler(data_dir=self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_trigger_only_fires_once_per_job(self):
        """Once triggered, a job is completed and won't fire again."""
        self.sched.schedule_recording(start_time=_past_iso(1), duration_sec=60)
        result1 = self.sched.check_and_trigger()
        self.assertIsNotNone(result1)
        result2 = self.sched.check_and_trigger()
        self.assertIsNone(result2)

    def test_trigger_returns_correct_duration_and_label(self):
        self.sched.schedule_recording(
            start_time=_past_iso(1), duration_sec=120, label="TestLabel"
        )
        result = self.sched.check_and_trigger()
        self.assertIsNotNone(result)
        self.assertEqual(result["duration_sec"], 120)
        self.assertEqual(result["label"], "TestLabel")

    def test_trigger_returns_job_id(self):
        entry = self.sched.schedule_recording(start_time=_past_iso(1), duration_sec=60)
        result = self.sched.check_and_trigger()
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], entry["id"])

    def test_future_job_not_triggered(self):
        self.sched.schedule_recording(start_time=_future_iso(3600), duration_sec=60)
        result = self.sched.check_and_trigger()
        self.assertIsNone(result)
        # Status must remain pending
        items = self.sched.list_scheduled()
        self.assertEqual(items[0]["status"], STATUS_PENDING)

    def test_cancelled_job_not_triggered(self):
        entry = self.sched.schedule_recording(start_time=_past_iso(1), duration_sec=60)
        self.sched.cancel_scheduled(entry["id"])
        result = self.sched.check_and_trigger()
        self.assertIsNone(result)

    def test_multiple_jobs_only_due_one_triggered(self):
        """When multiple jobs exist, only the due one is triggered."""
        self.sched.schedule_recording(start_time=_future_iso(3600), duration_sec=30, label="future")
        self.sched.schedule_recording(start_time=_past_iso(1), duration_sec=60, label="due_now")
        result = self.sched.check_and_trigger()
        self.assertIsNotNone(result)
        self.assertEqual(result["label"], "due_now")
        # Future job still pending
        items = self.sched.list_scheduled()
        future_items = [i for i in items if i["label"] == "future"]
        self.assertEqual(future_items[0]["status"], STATUS_PENDING)

    def test_multiple_past_jobs_triggers_one_at_a_time(self):
        """Multiple overdue jobs: each call triggers one, not all at once."""
        self.sched.schedule_recording(start_time=_past_iso(2), duration_sec=30, label="a")
        self.sched.schedule_recording(start_time=_past_iso(3), duration_sec=30, label="b")
        r1 = self.sched.check_and_trigger()
        r2 = self.sched.check_and_trigger()
        r3 = self.sched.check_and_trigger()
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)
        self.assertIsNone(r3)

    def test_completed_jobs_appear_in_list(self):
        """Completed jobs remain in list_scheduled with completed status."""
        self.sched.schedule_recording(start_time=_past_iso(1), duration_sec=60)
        self.sched.check_and_trigger()
        items = self.sched.list_scheduled()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], STATUS_COMPLETED)


class TestScheduleWithLabelAsProfile(unittest.TestCase):
    """Test that label/profile field is stored and retrieved correctly."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.sched = RecordingScheduler(data_dir=self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_empty_label_stored(self):
        entry = self.sched.schedule_recording(start_time=_future_iso(), duration_sec=60)
        self.assertEqual(entry["label"], "")

    def test_label_persisted(self):
        self.sched.schedule_recording(
            start_time=_future_iso(), duration_sec=60, label="meeting"
        )
        sched2 = RecordingScheduler(data_dir=self._tmpdir.name)
        items = sched2.list_scheduled()
        self.assertEqual(items[0]["label"], "meeting")

    def test_list_includes_all_statuses(self):
        """list_scheduled returns pending, cancelled, and completed jobs."""
        self.sched.schedule_recording(start_time=_future_iso(3600), duration_sec=60, label="pending_job")
        e2 = self.sched.schedule_recording(start_time=_future_iso(7200), duration_sec=60, label="cancel_job")
        self.sched.schedule_recording(start_time=_past_iso(1), duration_sec=60, label="done_job")
        self.sched.cancel_scheduled(e2["id"])
        self.sched.check_and_trigger()

        items = self.sched.list_scheduled()
        self.assertEqual(len(items), 3)
        statuses = {i["label"]: i["status"] for i in items}
        self.assertEqual(statuses["pending_job"], STATUS_PENDING)
        self.assertEqual(statuses["cancel_job"], STATUS_CANCELLED)
        self.assertEqual(statuses["done_job"], STATUS_COMPLETED)

    def test_created_at_is_set(self):
        entry = self.sched.schedule_recording(start_time=_future_iso(), duration_sec=60)
        self.assertIn("created_at", entry)
        # Must parse as ISO8601
        dt = datetime.fromisoformat(entry["created_at"].replace("Z", "+00:00"))
        self.assertIsNotNone(dt)

    def test_schedule_recording_returns_all_expected_fields(self):
        entry = self.sched.schedule_recording(
            start_time=_future_iso(), duration_sec=90, label="full_test"
        )
        for field in ("id", "start_time", "duration_sec", "label", "status", "created_at"):
            self.assertIn(field, entry, f"Missing field: {field}")

    def test_cancel_preserves_other_jobs(self):
        e1 = self.sched.schedule_recording(start_time=_future_iso(1800), duration_sec=30, label="keep")
        e2 = self.sched.schedule_recording(start_time=_future_iso(3600), duration_sec=60, label="cancel")
        self.sched.cancel_scheduled(e2["id"])
        items = self.sched.list_scheduled()
        self.assertEqual(len(items), 2)
        keep_items = [i for i in items if i["id"] == e1["id"]]
        self.assertEqual(len(keep_items), 1)
        self.assertEqual(keep_items[0]["status"], STATUS_PENDING)


if __name__ == "__main__":
    unittest.main()
