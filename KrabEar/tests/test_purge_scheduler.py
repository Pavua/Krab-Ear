"""Тесты PurgeScheduler — авто-очистка старых записей истории.

Правила тест-файла:
  - НЕ импортирует mlx/mlx_whisper (mlx-masking CI trap).
  - Все классы с BackendService вызывают service.close() в tearDown.
  - SyncThread — duck-type без наследования от threading.Thread
    (SyncThread/TrackingThread atexit hang).
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup — standard for Krab Ear test files
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.purge_scheduler import PurgeScheduler  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeSettings:
    """Простое хранилище настроек с возможностью переопределения значений."""

    def __init__(self, **kwargs: Any) -> None:
        self._data: dict[str, Any] = {
            "auto_purge_enabled": False,
            "auto_purge_retention_days": 90,
            "auto_purge_check_interval_hours": 24,
        }
        self._data.update(kwargs)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value


# ---------------------------------------------------------------------------
# Unit tests — PurgeScheduler
# ---------------------------------------------------------------------------

class TestPurgeSchedulerInit(unittest.TestCase):
    """Проверяет инициализацию и базовое поведение планировщика."""

    def test_start_creates_daemon_thread(self) -> None:
        """start() должен создавать daemon-поток."""
        settings = _FakeSettings()
        purge_calls: list[int] = []

        def fake_purge(days: int) -> int:
            purge_calls.append(days)
            return 0

        sched = PurgeScheduler(settings_get=settings.get, purge_fn=fake_purge)
        sched.start()
        try:
            self.assertIsNotNone(sched._thread)
            self.assertTrue(sched._thread.is_alive())  # type: ignore[union-attr]
            self.assertTrue(sched._thread.daemon)  # type: ignore[union-attr]
        finally:
            sched.stop()

    def test_start_is_idempotent(self) -> None:
        """Повторный вызов start() не создаёт второй поток."""
        settings = _FakeSettings()
        sched = PurgeScheduler(settings_get=settings.get, purge_fn=lambda d: 0)
        sched.start()
        first_thread = sched._thread
        sched.start()
        try:
            self.assertIs(sched._thread, first_thread)
        finally:
            sched.stop()

    def test_stop_terminates_thread(self) -> None:
        """stop() завершает фоновый поток."""
        settings = _FakeSettings()
        sched = PurgeScheduler(settings_get=settings.get, purge_fn=lambda d: 0)
        sched.start()
        self.assertTrue(sched._thread.is_alive())  # type: ignore[union-attr]
        sched.stop()
        # После stop() поток должен завершиться
        self.assertFalse(
            sched._thread is not None and sched._thread.is_alive(),
            "PurgeScheduler thread should not be alive after stop()",
        )

    def test_stop_without_start_is_safe(self) -> None:
        """stop() без предшествующего start() не должен падать."""
        settings = _FakeSettings()
        sched = PurgeScheduler(settings_get=settings.get, purge_fn=lambda d: 0)
        sched.stop()  # не должно бросить исключение


class TestPurgeSchedulerExecution(unittest.TestCase):
    """Проверяет, что purge_fn вызывается при включённом auto_purge."""

    def test_purge_fn_called_when_enabled(self) -> None:
        """При auto_purge_enabled=True purge_fn должна быть вызвана."""
        purge_calls: list[int] = []
        barrier = threading.Event()

        def fake_purge(days: int) -> int:
            purge_calls.append(days)
            barrier.set()
            return 5

        settings = _FakeSettings(
            auto_purge_enabled=True,
            auto_purge_retention_days=30,
            # Минимальный интервал — 1 с, чтобы тест не ждал часами.
            auto_purge_check_interval_hours=1 / 3600,
        )

        sched = PurgeScheduler(settings_get=settings.get, purge_fn=fake_purge)
        sched.start()
        triggered = barrier.wait(timeout=3.0)
        sched.stop()

        self.assertTrue(triggered, "purge_fn should have been called within 3s")
        self.assertTrue(len(purge_calls) >= 1, "purge_fn called at least once")
        self.assertEqual(purge_calls[0], 30, "purge_fn should receive retention_days=30")

    def test_purge_fn_not_called_when_disabled(self) -> None:
        """При auto_purge_enabled=False purge_fn НЕ должна вызываться."""
        purge_calls: list[int] = []
        called_event = threading.Event()

        def fake_purge(days: int) -> int:
            purge_calls.append(days)
            called_event.set()
            return 0

        settings = _FakeSettings(
            auto_purge_enabled=False,
            # Мин интервал чтобы тест не застрял:
            auto_purge_check_interval_hours=1 / 3600,
        )

        sched = PurgeScheduler(settings_get=settings.get, purge_fn=fake_purge)
        sched.start()
        # Ждём чуть дольше интервала, но purge_fn звать не должны
        called_event.wait(timeout=1.5)
        sched.stop()

        self.assertEqual(len(purge_calls), 0, "purge_fn must NOT be called when disabled")

    def test_purge_fn_exception_does_not_crash_loop(self) -> None:
        """Исключение в purge_fn не должно убивать фоновый поток."""
        call_count = [0]
        second_call = threading.Event()

        def fake_purge(days: int) -> int:
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("simulated purge error")
            second_call.set()
            return 0

        settings = _FakeSettings(
            auto_purge_enabled=True,
            auto_purge_check_interval_hours=1 / 3600,
        )

        sched = PurgeScheduler(settings_get=settings.get, purge_fn=fake_purge)
        sched.start()
        triggered = second_call.wait(timeout=5.0)
        sched.stop()

        self.assertTrue(triggered, "scheduler should survive purge_fn exception and call again")

    def test_timeout_clamped_to_minimum(self) -> None:
        """Таймаут ожидания не должен быть ≤ 0 даже при некорректном значении."""
        # Устанавливаем 0 часов → должен быть зажат до 1.0 с.
        purge_triggered = threading.Event()

        def fake_purge(days: int) -> int:
            purge_triggered.set()
            return 0

        settings = _FakeSettings(
            auto_purge_enabled=True,
            auto_purge_check_interval_hours=0,  # некорректное — ≤0
        )

        sched = PurgeScheduler(settings_get=settings.get, purge_fn=fake_purge)
        sched.start()
        # Даже с hours=0 timeout зажат в 1.0 с, значит первый вызов будет через ~1 с.
        triggered = purge_triggered.wait(timeout=4.0)
        sched.stop()

        self.assertTrue(triggered, "purge_fn should be called even with invalid check_interval_hours=0")


class TestPurgeSchedulerGetStatus(unittest.TestCase):
    """Проверяет метод get_status()."""

    def test_get_status_returns_expected_keys(self) -> None:
        settings = _FakeSettings(
            auto_purge_enabled=True,
            auto_purge_retention_days=60,
            auto_purge_check_interval_hours=12,
        )
        sched = PurgeScheduler(settings_get=settings.get, purge_fn=lambda d: 0)
        status = sched.get_status()
        self.assertIn("enabled", status)
        self.assertIn("retention_days", status)
        self.assertIn("check_interval_hours", status)
        self.assertIn("running", status)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["retention_days"], 60)
        self.assertEqual(status["check_interval_hours"], 12.0)
        self.assertFalse(status["running"])  # ещё не запущен

    def test_get_status_running_true_when_started(self) -> None:
        settings = _FakeSettings()
        sched = PurgeScheduler(settings_get=settings.get, purge_fn=lambda d: 0)
        sched.start()
        try:
            status = sched.get_status()
            self.assertTrue(status["running"])
        finally:
            sched.stop()


class TestPurgeSchedulerSettingsIntegration(unittest.TestCase):
    """Проверяет, что настройки перечитываются при каждом тике."""

    def test_retention_days_read_fresh_each_tick(self) -> None:
        """purge_fn получает актуальные retention_days из настроек каждого тика."""
        latest_days: list[int] = []
        calls_received = threading.Event()

        def fake_purge(days: int) -> int:
            latest_days.append(days)
            calls_received.set()
            return 0

        settings = _FakeSettings(
            auto_purge_enabled=True,
            auto_purge_retention_days=45,
            auto_purge_check_interval_hours=1 / 3600,
        )

        sched = PurgeScheduler(settings_get=settings.get, purge_fn=fake_purge)
        sched.start()
        calls_received.wait(timeout=3.0)
        sched.stop()

        self.assertTrue(len(latest_days) >= 1)
        self.assertEqual(latest_days[0], 45)


# ---------------------------------------------------------------------------
# BackendService tearDown regression test
# ---------------------------------------------------------------------------

class TestBackendServiceCloseStopsPurgeScheduler(unittest.TestCase):
    """Регрессионный тест: BackendService.close() должен остановить PurgeScheduler.

    Критично для предотвращения daemon-thread teardown CI flake (#1782).
    """

    def setUp(self) -> None:
        import tempfile
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_close_stops_purge_scheduler(self) -> None:
        """После BackendService.close() purge scheduler thread не должен быть жив."""
        from backend.state_store import StateStore
        from backend.service import BackendService

        # Каноническая конструкция (как в test_backend_service.py): реальный
        # StateStore на temp-dir; recorder/transcriber/translator опциональны.
        store = StateStore(Path(self.tmp_dir) / "data")
        service = BackendService(store=store)
        try:
            # Планировщик создан и запущен в __init__.
            scheduler = getattr(service, "_purge_scheduler", None)
            self.assertIsNotNone(scheduler, "BackendService must have _purge_scheduler")
            self.assertTrue(
                scheduler._thread is not None and scheduler._thread.is_alive(),
                "PurgeScheduler thread should be running after construction",
            )

            # close() должен остановить поток (регрессия анти-флейка #1782).
            service.close()

            thread = scheduler._thread
            self.assertFalse(
                thread is not None and thread.is_alive(),
                "PurgeScheduler thread must not be alive after BackendService.close()",
            )
        finally:
            # Идемпотентно — гарантирует остановку daemon-тредов даже при падении
            # ассерта (иначе сам этот тест воспроизвёл бы флейк, который и проверяет).
            service.close()


if __name__ == "__main__":
    unittest.main()
