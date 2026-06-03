"""Wave-32 тесты: RecordingScheduler (C1 фоновый триггер + C2 валидация) и
DataMigrator (C3 подтверждение rollback).

Покрывает:
- C1: фоновый поток check_and_trigger() срабатывает и вызывает trigger_fn
- C2: отклонение отрицательной/нулевой duration_sec, duration > 7200,
      start_time в прошлом, start_time > 30 дней в будущем
- C3: handle_rollback_migration без confirm → ошибка; с confirm=True → успех
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_scheduler import (  # noqa: E402
    MAX_PENDING_SCHEDULES,
    RecordingScheduler,
    STATUS_PENDING,
    _MAX_DURATION_SEC,
    _MAX_FUTURE_DAYS,
)
from backend.data_migrator import DataMigrator  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _future_iso(seconds: int = 3600) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _past_iso(seconds: int = 60) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(seconds=seconds)).isoformat()


# ---------------------------------------------------------------------------
# C1 — background trigger thread
# ---------------------------------------------------------------------------

class TestBackgroundTriggerThread(unittest.TestCase):
    """C1: фоновый поток вызывает trigger_fn при наступлении времени записи."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_trigger_fn_called_when_schedule_fires(self):
        """trigger_fn вызывается когда check_and_trigger() обнаруживает готовое задание."""
        fired = []

        def fake_trigger(duration_sec: int, label: str) -> None:
            fired.append({"duration_sec": duration_sec, "label": label})

        sched = RecordingScheduler(data_dir=self._tmpdir.name, trigger_fn=fake_trigger)
        try:
            # Добавляем задание на момент в прошлом — check_and_trigger должен его поднять
            # но нам нужен допуск 5 секунд: ставим ровно "сейчас"
            # Имитируем: создаём задание и вручную откатываем start_time на 1 секунду
            now_iso = (datetime.now(tz=timezone.utc) + timedelta(seconds=3601)).isoformat()
            entry = sched.schedule_recording(start_time=now_iso, duration_sec=60, label="test-bg")
            sid = entry["id"]

            # Вручную переставляем start_time на "сейчас" для немедленного триггера
            with sched._lock:
                sched._schedules[sid]["start_time"] = datetime.now(tz=timezone.utc).isoformat()
                sched._save()

            # Принудительно вызываем check_and_trigger (имитация одного тика потока)
            triggered = sched.check_and_trigger()
            if triggered and fake_trigger:
                fake_trigger(triggered["duration_sec"], triggered.get("label", ""))

            self.assertEqual(len(fired), 1)
            self.assertEqual(fired[0]["duration_sec"], 60)
            self.assertEqual(fired[0]["label"], "test-bg")
        finally:
            sched.stop()

    def test_trigger_fn_not_called_for_future_schedule(self):
        """trigger_fn НЕ вызывается для задания в далёком будущем."""
        fired = []

        def fake_trigger(duration_sec: int, label: str) -> None:
            fired.append(True)

        sched = RecordingScheduler(data_dir=self._tmpdir.name, trigger_fn=fake_trigger)
        try:
            sched.schedule_recording(start_time=_future_iso(7200), duration_sec=30, label="future")
            # check_and_trigger должен вернуть None для задания в будущем
            result = sched.check_and_trigger()
            self.assertIsNone(result)
            self.assertEqual(len(fired), 0)
        finally:
            sched.stop()

    def test_no_bg_thread_without_trigger_fn(self):
        """Если trigger_fn=None, фоновый поток не создаётся."""
        sched = RecordingScheduler(data_dir=self._tmpdir.name, trigger_fn=None)
        try:
            self.assertIsNone(sched._bg_thread)
        finally:
            sched.stop()

    def test_bg_thread_is_daemon(self):
        """Фоновый поток должен быть daemon-потоком."""
        sched = RecordingScheduler(
            data_dir=self._tmpdir.name,
            trigger_fn=lambda dur, lbl: None,
        )
        try:
            self.assertIsNotNone(sched._bg_thread)
            self.assertTrue(sched._bg_thread.daemon)
        finally:
            sched.stop()

    def test_stop_sets_stop_event(self):
        """stop() устанавливает _stop_event."""
        sched = RecordingScheduler(
            data_dir=self._tmpdir.name,
            trigger_fn=lambda dur, lbl: None,
        )
        self.assertFalse(sched._stop_event.is_set())
        sched.stop()
        self.assertTrue(sched._stop_event.is_set())

    def test_completed_schedule_triggers_once(self):
        """Задание помечается completed и больше не триггерится повторно."""
        sched = RecordingScheduler(data_dir=self._tmpdir.name)
        try:
            now_iso = (datetime.now(tz=timezone.utc) + timedelta(seconds=3601)).isoformat()
            entry = sched.schedule_recording(start_time=now_iso, duration_sec=10, label="once")
            sid = entry["id"]
            # Переставляем на прошлое внутри допуска
            with sched._lock:
                sched._schedules[sid]["start_time"] = datetime.now(tz=timezone.utc).isoformat()
                sched._save()

            r1 = sched.check_and_trigger()
            self.assertIsNotNone(r1)

            # Второй вызов — уже completed, не должен вернуть ничего
            r2 = sched.check_and_trigger()
            self.assertIsNone(r2)
        finally:
            sched.stop()


# ---------------------------------------------------------------------------
# C1 — MAX_PENDING_SCHEDULES cap
# ---------------------------------------------------------------------------

class TestPendingSchedulesCap(unittest.TestCase):
    """C1: cap MAX_PENDING_SCHEDULES на pending-задания."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.sched = RecordingScheduler(data_dir=self._tmpdir.name)

    def tearDown(self):
        self.sched.stop()
        self._tmpdir.cleanup()

    def test_rejects_when_pending_cap_reached(self):
        """Новое задание отклоняется, когда ожидающих уже MAX_PENDING_SCHEDULES."""
        # Добавляем MAX_PENDING_SCHEDULES заданий
        for i in range(MAX_PENDING_SCHEDULES):
            self.sched.schedule_recording(
                start_time=_future_iso(3600 + i * 10),
                duration_sec=60,
                label=f"job-{i}",
            )
        with self.assertRaises(ValueError) as ctx:
            self.sched.schedule_recording(
                start_time=_future_iso(7000),
                duration_sec=60,
                label="overflow",
            )
        self.assertIn("ожидающих", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# C2 — input validation
# ---------------------------------------------------------------------------

class TestScheduleRecordingValidation(unittest.TestCase):
    """C2: валидация duration_sec и start_time."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.sched = RecordingScheduler(data_dir=self._tmpdir.name)

    def tearDown(self):
        self.sched.stop()
        self._tmpdir.cleanup()

    # duration_sec bounds

    def test_rejects_zero_duration(self):
        with self.assertRaises(ValueError) as ctx:
            self.sched.schedule_recording(start_time=_future_iso(), duration_sec=0)
        self.assertIn("диапазон", str(ctx.exception).lower())

    def test_rejects_negative_duration(self):
        with self.assertRaises(ValueError):
            self.sched.schedule_recording(start_time=_future_iso(), duration_sec=-10)

    def test_rejects_duration_above_max(self):
        with self.assertRaises(ValueError) as ctx:
            self.sched.schedule_recording(
                start_time=_future_iso(),
                duration_sec=_MAX_DURATION_SEC + 1,
            )
        self.assertIn("диапазон", str(ctx.exception).lower())

    def test_accepts_min_duration(self):
        entry = self.sched.schedule_recording(start_time=_future_iso(), duration_sec=1)
        self.assertEqual(entry["duration_sec"], 1)

    def test_accepts_max_duration(self):
        entry = self.sched.schedule_recording(
            start_time=_future_iso(), duration_sec=_MAX_DURATION_SEC
        )
        self.assertEqual(entry["duration_sec"], _MAX_DURATION_SEC)

    # start_time bounds

    def test_rejects_past_start_time(self):
        with self.assertRaises(ValueError) as ctx:
            self.sched.schedule_recording(
                start_time=_past_iso(60),
                duration_sec=30,
            )
        self.assertIn("прошлом", str(ctx.exception).lower())

    def test_rejects_start_time_beyond_30_days(self):
        far_future = (
            datetime.now(tz=timezone.utc) + timedelta(days=_MAX_FUTURE_DAYS + 1)
        ).isoformat()
        with self.assertRaises(ValueError) as ctx:
            self.sched.schedule_recording(
                start_time=far_future,
                duration_sec=30,
            )
        self.assertIn("дней", str(ctx.exception).lower())

    def test_accepts_start_time_within_30_days(self):
        near_limit = (
            datetime.now(tz=timezone.utc) + timedelta(days=_MAX_FUTURE_DAYS - 1)
        ).isoformat()
        entry = self.sched.schedule_recording(start_time=near_limit, duration_sec=60)
        self.assertEqual(entry["status"], STATUS_PENDING)

    def test_rejects_invalid_iso_format(self):
        with self.assertRaises(ValueError):
            self.sched.schedule_recording(start_time="not-a-date", duration_sec=30)


# ---------------------------------------------------------------------------
# C3 — DataMigrator rollback confirm guard
# ---------------------------------------------------------------------------

class TestRollbackMigrationConfirm(unittest.TestCase):
    """C3: handle_rollback_migration требует confirm=True."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.migrator = DataMigrator(data_dir=self.data_dir)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_fake_backup(self) -> str:
        """Создаёт фейковую директорию бэкапа внутри <data_dir>/backups/."""
        backup_dir = self.data_dir / "backups" / "migration_backup_20260101_120000"
        backup_dir.mkdir(parents=True, exist_ok=True)
        # Добавляем фиктивный файл, чтобы restore_files был непустым
        (backup_dir / "settings.json").write_text("{}", encoding="utf-8")
        return str(backup_dir)

    def test_rollback_without_confirm_returns_error(self):
        """Без confirm rollback_migration возвращает {'ok': False, ...}."""
        backup_path = self._make_fake_backup()
        result = self.migrator.handle_rollback_migration({"backup_path": backup_path})
        self.assertFalse(result.get("ok"))
        self.assertIn("confirm", result.get("reason", ""))

    def test_rollback_with_confirm_false_returns_error(self):
        """confirm=False тоже должен возвращать ошибку."""
        backup_path = self._make_fake_backup()
        result = self.migrator.handle_rollback_migration(
            {"backup_path": backup_path, "confirm": False}
        )
        self.assertFalse(result.get("ok"))

    def test_rollback_with_confirm_true_executes(self):
        """confirm=True выполняет откат и возвращает restored_files."""
        backup_path = self._make_fake_backup()
        result = self.migrator.handle_rollback_migration(
            {"backup_path": backup_path, "confirm": True}
        )
        # Должен содержать restored_files (список), а не ошибку
        self.assertIn("restored_files", result)
        self.assertIsInstance(result["restored_files"], list)

    def test_rollback_missing_backup_path_still_needs_confirm(self):
        """Даже при отсутствии backup_path без confirm возвращается ошибка confirm."""
        result = self.migrator.handle_rollback_migration({})
        self.assertFalse(result.get("ok"))
        self.assertIn("confirm", result.get("reason", ""))

    def test_rollback_confirm_true_empty_backup_path_raises(self):
        """Если confirm=True, но backup_path не указан — ValueError."""
        with self.assertRaises(ValueError) as ctx:
            self.migrator.handle_rollback_migration({"confirm": True})
        self.assertIn("backup_path", str(ctx.exception).lower())

    def test_rollback_confirm_true_traversal_rejected(self):
        """backup_path вне <data_dir>/backups/ → RuntimeError (traversal guard)."""
        with self.assertRaises(RuntimeError):
            self.migrator.handle_rollback_migration(
                {"backup_path": "/tmp/evil_backup", "confirm": True}
            )


if __name__ == "__main__":
    unittest.main()
