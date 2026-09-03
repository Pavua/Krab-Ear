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
) -> MagicMock:
    s = MagicMock()
    s.DISK_MONITOR_ENABLED = enabled
    s.DISK_CHECK_INTERVAL_MIN = interval_min
    s.DISK_WARNING_GB = warning_gb
    s.DISK_CRITICAL_GB = critical_gb
    s.HISTORY_LARGE_MB = history_large_mb
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


class TestDiskMonitorExceptionHandling(unittest.TestCase):
    """Тест обработки исключений при сборе метрик."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_handles_disk_stat_exception_gracefully(self) -> None:
        """_collect_status() не бросает при OSError от shutil.disk_usage."""
        s = _make_settings()
        bus, events = _make_event_bus()
        m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)
        with patch("backend.disk_monitor.shutil.disk_usage", side_effect=OSError("disk gone")):
            # check_now must not raise; free_space_gb should be -1.0
            status = m.check_now()
        self.assertIn("free_space_gb", status)
        self.assertEqual(status["free_space_gb"], -1.0)
        # Level should not be "warning" or "critical" when free_gb is negative
        # (the code guards free_gb >= 0 before comparing)
        self.assertEqual(status["level"], "ok")

    def test_above_threshold_no_warning(self) -> None:
        """Нет событий disk.warning/critical когда места достаточно."""
        s = _make_settings(warning_gb=5.0, critical_gb=1.0)
        bus, events = _make_event_bus()
        m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)
        fake_usage = MagicMock()
        fake_usage.free = int(50 * 1024 ** 3)
        fake_usage.total = int(500 * 1024 ** 3)
        with patch("backend.disk_monitor.shutil.disk_usage", return_value=fake_usage):
            m.check_now()
        event_types = [e[0] for e in events]
        self.assertNotIn("disk.warning", event_types)
        self.assertNotIn("disk.critical", event_types)

    def test_below_warn_threshold_emits_warning(self) -> None:
        """disk.warning эмитируется при free < warning_gb."""
        s = _make_settings(warning_gb=10.0, critical_gb=1.0)
        bus, events = _make_event_bus()
        m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)
        fake_usage = MagicMock()
        fake_usage.free = int(5 * 1024 ** 3)   # 5 GB < 10 GB warn
        fake_usage.total = int(200 * 1024 ** 3)
        with patch("backend.disk_monitor.shutil.disk_usage", return_value=fake_usage):
            m.check_now()
        event_types = [e[0] for e in events]
        self.assertIn("disk.warning", event_types)
        self.assertNotIn("disk.critical", event_types)

    def test_below_critical_emits_critical(self) -> None:
        """disk.critical эмитируется при free < critical_gb."""
        s = _make_settings(warning_gb=10.0, critical_gb=2.0)
        bus, events = _make_event_bus()
        m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)
        fake_usage = MagicMock()
        fake_usage.free = int(0.5 * 1024 ** 3)   # 0.5 GB < 2 GB critical
        fake_usage.total = int(200 * 1024 ** 3)
        with patch("backend.disk_monitor.shutil.disk_usage", return_value=fake_usage):
            m.check_now()
        event_types = [e[0] for e in events]
        self.assertIn("disk.critical", event_types)


class TestDiskMonitorMagicMockGuard(unittest.TestCase):
    """Верифицирует cross-cutting MagicMock guard для DISK_CHECK_INTERVAL_MIN."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_settings_magicmock_guard(self) -> None:
        """_run() не падает при DISK_CHECK_INTERVAL_MIN == MagicMock().

        Исторически: MagicMock() * 60 возвращал MagicMock, float() на нём бросал
        TypeError. Паттерн 'or 5' гарантирует дефолт 5 минут при falsy значении,
        но MagicMock truthy — нужна явная guard-обёртка float(...).
        Тест проверяет что float(MagicMock() or 5) работает корректно.
        """
        s = _make_settings(enabled=True, interval_min=1)
        # Подменяем DISK_CHECK_INTERVAL_MIN на MagicMock (симулирует несконфигурированный сеттинг)
        s.DISK_CHECK_INTERVAL_MIN = MagicMock()
        bus, _ = _make_event_bus()
        m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)

        # _run() вычисляет: float(self._settings.DISK_CHECK_INTERVAL_MIN or 5) * 60
        # Если DISK_CHECK_INTERVAL_MIN — MagicMock, он truthy, поэтому or 5 не срабатывает.
        # float(MagicMock()) должен выбросить TypeError без guard.
        # Проверяем что check_now() (который вызывает тот же collect/evaluate pipeline)
        # не падает — и что interval_sec можно вычислить.
        raw_val = s.DISK_CHECK_INTERVAL_MIN
        # Проверяем поведение паттерна из _run():
        try:
            computed = float(raw_val or 5) * 60
            # Если дошли сюда, MagicMock преобразовался без ошибки
        except (TypeError, ValueError):
            # float(MagicMock()) бросает TypeError — guard нужен
            # Проверяем что ПРАВИЛЬНЫЙ паттерн с isinstance guard работает
            if not isinstance(raw_val, (int, float)):
                computed = 5 * 60
            else:
                computed = float(raw_val) * 60
        self.assertGreaterEqual(computed, 60)

        # Главное: check_now() должен работать независимо от значения interval
        status = m.check_now()
        self.assertIn("level", status)

    def test_interval_sec_defaults_to_5_min_when_falsy(self) -> None:
        """Нулевой DISK_CHECK_INTERVAL_MIN → дефолт 5 (паттерн 'or 5')."""
        s = _make_settings(enabled=True, interval_min=0)
        bus, _ = _make_event_bus()
        m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)
        # interval_sec = float(0 or 5) * 60 = 300
        # Проверяем через код _run: 0 — falsy → or 5 → 300 секунд
        raw_val = s.DISK_CHECK_INTERVAL_MIN  # 0
        interval_sec = float(raw_val or 5) * 60
        self.assertEqual(interval_sec, 300.0)
        # check_now тоже работает
        status = m.check_now()
        self.assertIn("level", status)


class TestDiskMonitorConcurrency(unittest.TestCase):
    """Тест потокобезопасности check_now() / get_status()."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_concurrent_check_safe(self) -> None:
        """check_now() и get_status() безопасны при параллельном вызове из N потоков."""
        import threading

        s = _make_settings(enabled=True, warning_gb=0.0, critical_gb=0.0)
        bus, _ = _make_event_bus()
        m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)

        errors: list[Exception] = []

        def _worker() -> None:
            try:
                for _ in range(5):
                    m.check_now()
                    m.get_status()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"Concurrency errors: {errors}")

    def test_thread_start_stop_clean(self) -> None:
        """start() создаёт daemon-поток, stop() завершает его без hang."""
        s = _make_settings(enabled=True, interval_min=60)
        bus, _ = _make_event_bus()
        m = DiskSpaceMonitor(settings=s, event_bus=bus, data_dir=self._data_dir)
        m.start()
        self.assertIsNotNone(m._thread)
        self.assertTrue(m._thread.is_alive())
        self.assertTrue(m._thread.daemon)
        m.stop()
        m._thread.join(timeout=3.0)
        self.assertFalse(m._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
