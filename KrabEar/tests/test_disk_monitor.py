"""Тесты DiskSpaceMonitor."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend.disk_monitor import DiskSpaceMonitor


def _make_settings(
    enabled: bool = True,
    interval_min: int = 1,
    warning_gb: float = 5.0,
    critical_gb: float = 1.0,
    history_large_mb: int = 500,
    auto_cleanup_enabled: bool = False,
    auto_cleanup_days: int = 365,
) -> MagicMock:
    s = MagicMock()
    s.DISK_MONITOR_ENABLED = enabled
    s.DISK_CHECK_INTERVAL_MIN = interval_min
    s.DISK_WARNING_GB = warning_gb
    s.DISK_CRITICAL_GB = critical_gb
    s.HISTORY_LARGE_MB = history_large_mb
    s.AUTO_CLEANUP_ENABLED = auto_cleanup_enabled
    s.AUTO_CLEANUP_AFTER_DAYS = auto_cleanup_days
    return s


def _make_event_bus() -> tuple[MagicMock, list[tuple[str, dict]]]:
    """Возвращает (event_bus_mock, events_list)."""
    events: list[tuple[str, dict]] = []
    bus = MagicMock()

    def _emit(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))

    bus.emit.side_effect = _emit
    return bus, events


class TestDiskMonitorGetStatus(unittest.TestCase):
    """Тест get_status без запуска потока."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_monitor(self, **kwargs) -> DiskSpaceMonitor:
        s = _make_settings(**kwargs)
        bus, _ = _make_event_bus()
        return DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)

    def test_initial_status_is_empty(self) -> None:
        m = self._make_monitor()
        status = m.get_status()
        self.assertIn("enabled", status)

    def test_enabled_flag_reflected(self) -> None:
        m = self._make_monitor(enabled=False)
        status = m.get_status()
        self.assertFalse(status["enabled"])

    def test_enabled_flag_true(self) -> None:
        m = self._make_monitor(enabled=True)
        status = m.get_status()
        self.assertTrue(status["enabled"])


class TestDiskMonitorCheckNow(unittest.TestCase):
    """Тест check_now() — синхронная проверка."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_monitor(self, **kwargs) -> tuple[DiskSpaceMonitor, list]:
        s = _make_settings(**kwargs)
        bus, events = _make_event_bus()
        m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)
        return m, events

    def test_check_now_returns_status_fields(self) -> None:
        m, _ = self._make_monitor()
        status = m.check_now()
        for key in ("free_space_gb", "data_dir_mb", "history_mb", "transcripts_mb",
                    "level", "history_large", "last_check_ts"):
            self.assertIn(key, status, f"missing key: {key}")

    def test_check_now_level_ok_when_plenty_space(self) -> None:
        """С нулевыми порогами — уровень ok."""
        m, _ = self._make_monitor(warning_gb=0.0, critical_gb=0.0)
        status = m.check_now()
        self.assertEqual(status["level"], "ok")

    def test_warning_event_emitted_when_low_space(self) -> None:
        """disk.warning эмитится когда free < warning_gb."""
        m, events = self._make_monitor()
        fake_usage = MagicMock()
        fake_usage.free = int(3 * 1024 ** 3)   # 3 GB free
        fake_usage.total = int(100 * 1024 ** 3)
        with patch("backend.disk_monitor.shutil.disk_usage", return_value=fake_usage):
            m.check_now()

        event_types = [e[0] for e in events]
        self.assertIn("disk.warning", event_types)
        self.assertNotIn("disk.critical", event_types)

    def test_critical_event_emitted_when_very_low_space(self) -> None:
        """disk.critical эмитится когда free < critical_gb."""
        m, events = self._make_monitor()
        fake_usage = MagicMock()
        fake_usage.free = int(0.5 * 1024 ** 3)  # 0.5 GB free
        fake_usage.total = int(100 * 1024 ** 3)
        with patch("backend.disk_monitor.shutil.disk_usage", return_value=fake_usage):
            m.check_now()

        event_types = [e[0] for e in events]
        self.assertIn("disk.critical", event_types)

    def test_history_large_event_emitted_when_large_file(self) -> None:
        """disk.history_large эмитится когда history.ndjson > HISTORY_LARGE_MB."""
        m, events = self._make_monitor(history_large_mb=1)  # порог 1 MB
        history_path = self._data_dir / "history.ndjson"
        history_path.write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MB
        m.check_now()

        event_types = [e[0] for e in events]
        self.assertIn("disk.history_large", event_types)

    def test_history_small_no_large_event(self) -> None:
        """disk.history_large НЕ эмитится когда файл маленький."""
        m, events = self._make_monitor(history_large_mb=500)
        history_path = self._data_dir / "history.ndjson"
        history_path.write_bytes(b"x" * 100)
        m.check_now()

        event_types = [e[0] for e in events]
        self.assertNotIn("disk.history_large", event_types)

    def test_no_events_when_disabled(self) -> None:
        """Когда DISK_MONITOR_ENABLED=False — события не эмитируются."""
        m, events = self._make_monitor(enabled=False)
        fake_usage = MagicMock()
        fake_usage.free = int(0.1 * 1024 ** 3)
        fake_usage.total = int(100 * 1024 ** 3)
        with patch("backend.disk_monitor.shutil.disk_usage", return_value=fake_usage):
            m.check_now()

        self.assertEqual(events, [])

    def test_level_ok_returned_when_enough_space(self) -> None:
        m, events = self._make_monitor()
        fake_usage = MagicMock()
        fake_usage.free = int(20 * 1024 ** 3)  # 20 GB free
        fake_usage.total = int(100 * 1024 ** 3)
        with patch("backend.disk_monitor.shutil.disk_usage", return_value=fake_usage):
            status = m.check_now()

        self.assertEqual(status["level"], "ok")
        event_types = [e[0] for e in events]
        self.assertNotIn("disk.warning", event_types)
        self.assertNotIn("disk.critical", event_types)

    def test_warning_event_payload_has_threshold(self) -> None:
        m, events = self._make_monitor()
        fake_usage = MagicMock()
        fake_usage.free = int(3 * 1024 ** 3)  # 3 GB
        fake_usage.total = int(100 * 1024 ** 3)
        with patch("backend.disk_monitor.shutil.disk_usage", return_value=fake_usage):
            m.check_now()

        warning_events = [(t, p) for t, p in events if t == "disk.warning"]
        self.assertTrue(warning_events)
        payload = warning_events[0][1]
        self.assertIn("threshold_gb", payload)
        self.assertIn("free_space_gb", payload)
        self.assertIn("level", payload)

    def test_check_now_persists_in_get_status(self) -> None:
        """После check_now() get_status() возвращает обновлённые данные."""
        m, _ = self._make_monitor()
        m.check_now()
        status = m.get_status()
        self.assertIsNotNone(status.get("last_check_ts"))


class TestDiskMonitorStartStop(unittest.TestCase):
    """Тест start() / stop() потока."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_start_disabled_does_not_create_thread(self) -> None:
        s = _make_settings(enabled=False)
        bus, _ = _make_event_bus()
        m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)
        m.start()
        self.assertIsNone(m._thread)

    def test_stop_before_start_is_safe(self) -> None:
        s = _make_settings(enabled=True)
        bus, _ = _make_event_bus()
        m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)
        m.stop()  # should not raise

    def test_start_creates_daemon_thread(self) -> None:
        s = _make_settings(enabled=True, interval_min=60)
        bus, _ = _make_event_bus()
        m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)
        m.start()
        self.assertIsNotNone(m._thread)
        self.assertTrue(m._thread.daemon)
        m.stop()

    def test_stop_terminates_thread(self) -> None:
        s = _make_settings(enabled=True, interval_min=60)
        bus, _ = _make_event_bus()
        m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)
        m.start()
        thread = m._thread
        m.stop()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())

    def test_double_start_does_not_create_second_thread(self) -> None:
        s = _make_settings(enabled=True, interval_min=60)
        bus, _ = _make_event_bus()
        m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)
        m.start()
        first_thread = m._thread
        m.start()  # второй старт — должен быть no-op
        self.assertIs(m._thread, first_thread)
        m.stop()


class TestAutoCleanupHook(unittest.TestCase):
    """Тест авто-очистки при disk.critical."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_auto_cleanup_requested_when_critical_and_enabled(self) -> None:
        """disk.auto_cleanup_requested эмитится при critical + AUTO_CLEANUP_ENABLED."""
        s = _make_settings(auto_cleanup_enabled=True)
        bus, events = _make_event_bus()
        m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)

        fake_usage = MagicMock()
        fake_usage.free = int(0.1 * 1024 ** 3)  # критически мало
        fake_usage.total = int(100 * 1024 ** 3)
        with patch("backend.disk_monitor.shutil.disk_usage", return_value=fake_usage):
            m.check_now()

        event_types = [e[0] for e in events]
        self.assertIn("disk.auto_cleanup_requested", event_types)

    def test_no_auto_cleanup_when_disabled(self) -> None:
        """disk.auto_cleanup_requested НЕ эмитится когда AUTO_CLEANUP_ENABLED=False."""
        s = _make_settings(auto_cleanup_enabled=False)
        bus, events = _make_event_bus()
        m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)

        fake_usage = MagicMock()
        fake_usage.free = int(0.1 * 1024 ** 3)
        fake_usage.total = int(100 * 1024 ** 3)
        with patch("backend.disk_monitor.shutil.disk_usage", return_value=fake_usage):
            m.check_now()

        event_types = [e[0] for e in events]
        self.assertNotIn("disk.auto_cleanup_requested", event_types)


if __name__ == "__main__":
    unittest.main()
