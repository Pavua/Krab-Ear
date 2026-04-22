"""Тесты для SystemMonitor — мониторинг системных ресурсов без внешних зависимостей."""

from backend.system_monitor import SystemMonitor
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestGetSystemInfoKeys(unittest.TestCase):
    """Проверяет, что get_system_info возвращает все обязательные ключи."""

    def test_all_required_keys_present(self):
        monitor = SystemMonitor()
        info = monitor.get_system_info()
        required_keys = [
            "cpu_percent",
            "memory_used_gb",
            "memory_total_gb",
            "memory_percent",
            "disk_free_gb",
            "disk_total_gb",
            "python_memory_mb",
            "process_cpu_percent",
            "gpu_name",
            "macos_version",
            "uptime_hours",
            "load_average",
        ]
        for key in required_keys:
            self.assertIn(key, info, f"Отсутствует ключ: {key}")

    def test_load_average_is_list_of_three(self):
        monitor = SystemMonitor()
        info = monitor.get_system_info()
        self.assertIsInstance(info["load_average"], list)
        self.assertEqual(len(info["load_average"]), 3)

    def test_numeric_values_are_non_negative(self):
        monitor = SystemMonitor()
        info = monitor.get_system_info()
        numeric_keys = [
            "cpu_percent", "memory_used_gb", "memory_total_gb",
            "memory_percent", "disk_free_gb", "disk_total_gb",
            "python_memory_mb", "process_cpu_percent", "uptime_hours",
        ]
        for key in numeric_keys:
            self.assertGreaterEqual(
                info[key], 0.0, f"Значение {key}={info[key]} должно быть >= 0"
            )


class TestGetSystemInfoRanges(unittest.TestCase):
    """Проверяет диапазоны значений возвращаемых метрик."""

    def test_cpu_percent_within_0_100(self):
        monitor = SystemMonitor()
        info = monitor.get_system_info()
        self.assertLessEqual(info["cpu_percent"], 100.0)
        self.assertGreaterEqual(info["cpu_percent"], 0.0)

    def test_memory_percent_within_0_100(self):
        monitor = SystemMonitor()
        info = monitor.get_system_info()
        self.assertLessEqual(info["memory_percent"], 100.0)
        self.assertGreaterEqual(info["memory_percent"], 0.0)

    def test_disk_free_leq_disk_total(self):
        monitor = SystemMonitor()
        info = monitor.get_system_info()
        # Если total > 0, то free <= total
        if info["disk_total_gb"] > 0:
            self.assertLessEqual(info["disk_free_gb"], info["disk_total_gb"])

    def test_gpu_name_is_string(self):
        monitor = SystemMonitor()
        info = monitor.get_system_info()
        self.assertIsInstance(info["gpu_name"], str)
        self.assertGreater(len(info["gpu_name"]), 0)

    def test_macos_version_is_string(self):
        monitor = SystemMonitor()
        info = monitor.get_system_info()
        self.assertIsInstance(info["macos_version"], str)
        self.assertGreater(len(info["macos_version"]), 0)


class TestIsResourceConstrained(unittest.TestCase):
    """Тесты для метода is_resource_constrained."""

    def test_not_constrained_when_memory_low_and_disk_plenty(self):
        monitor = SystemMonitor()
        with patch.object(monitor, "get_system_info", return_value={
            "memory_percent": 50.0,
            "disk_free_gb": 100.0,
        }):
            self.assertFalse(monitor.is_resource_constrained())

    def test_constrained_when_memory_over_80(self):
        monitor = SystemMonitor()
        with patch.object(monitor, "get_system_info", return_value={
            "memory_percent": 85.0,
            "disk_free_gb": 50.0,
        }):
            self.assertTrue(monitor.is_resource_constrained())

    def test_constrained_when_disk_below_1gb(self):
        monitor = SystemMonitor()
        with patch.object(monitor, "get_system_info", return_value={
            "memory_percent": 40.0,
            "disk_free_gb": 0.5,
        }):
            self.assertTrue(monitor.is_resource_constrained())

    def test_constrained_when_both_conditions_true(self):
        monitor = SystemMonitor()
        with patch.object(monitor, "get_system_info", return_value={
            "memory_percent": 95.0,
            "disk_free_gb": 0.1,
        }):
            self.assertTrue(monitor.is_resource_constrained())

    def test_boundary_memory_exactly_80_not_constrained(self):
        monitor = SystemMonitor()
        with patch.object(monitor, "get_system_info", return_value={
            "memory_percent": 80.0,
            "disk_free_gb": 10.0,
        }):
            # 80.0 не > 80.0, значит не ограничен
            self.assertFalse(monitor.is_resource_constrained())

    def test_boundary_disk_exactly_1gb_not_constrained(self):
        monitor = SystemMonitor()
        with patch.object(monitor, "get_system_info", return_value={
            "memory_percent": 50.0,
            "disk_free_gb": 1.0,
        }):
            # 1.0 не < 1.0, значит не ограничен
            self.assertFalse(monitor.is_resource_constrained())

    def test_returns_false_on_exception(self):
        monitor = SystemMonitor()
        with patch.object(monitor, "get_system_info", side_effect=RuntimeError("fail")):
            self.assertFalse(monitor.is_resource_constrained())


class TestSysctlHelper(unittest.TestCase):
    """Тесты вспомогательного метода _sysctl."""

    def test_sysctl_returns_string(self):
        result = SystemMonitor._sysctl("hw.memsize")
        self.assertIsInstance(result, str)

    def test_sysctl_invalid_key_returns_empty(self):
        result = SystemMonitor._sysctl("nonexistent.key.xyz")
        self.assertEqual(result, "")


class TestVmStatHelper(unittest.TestCase):
    """Тесты метода _vm_stat."""

    @unittest.skipUnless(sys.platform == "darwin", "vm_stat is macOS-only")
    def test_vm_stat_returns_dict(self):
        result = SystemMonitor._vm_stat()
        self.assertIsInstance(result, dict)

    @unittest.skipUnless(sys.platform == "darwin", "vm_stat is macOS-only")
    def test_vm_stat_contains_pages_free(self):
        result = SystemMonitor._vm_stat()
        # На macOS ключ "Pages free" должен присутствовать
        self.assertIn("Pages free", result)
        self.assertIsInstance(result["Pages free"], int)


class TestGetSystemInfoRobustness(unittest.TestCase):
    """Тесты устойчивости при сбоях подсистем."""

    def test_returns_dict_even_if_subprocess_fails(self):
        """При полном отказе subprocess должен вернуться словарь с нулями."""
        monitor = SystemMonitor()
        with patch("subprocess.run", side_effect=OSError("no subprocess")):
            info = monitor.get_system_info()
        self.assertIsInstance(info, dict)
        self.assertIn("cpu_percent", info)
        self.assertIn("memory_total_gb", info)

    def test_python_memory_mb_reflects_current_process(self):
        """python_memory_mb должна быть > 0 (процесс запущен)."""
        monitor = SystemMonitor()
        info = monitor.get_system_info()
        self.assertGreater(info["python_memory_mb"], 0.0)


class TestSnapshot(unittest.TestCase):
    """Тесты для метода snapshot() — компактный снимок ресурсов."""

    def _make_info(self, cpu=25.0, mem=50.0, disk_total=500.0, disk_free=200.0):
        return {
            "cpu_percent": cpu,
            "memory_percent": mem,
            "disk_total_gb": disk_total,
            "disk_free_gb": disk_free,
        }

    def test_snapshot_returns_required_keys(self):
        monitor = SystemMonitor()
        with patch.object(monitor, "get_system_info", return_value=self._make_info()):
            snap = monitor.snapshot()
        for key in ("cpu_percent", "ram_percent", "disk_percent", "gpu_percent"):
            self.assertIn(key, snap)

    def test_snapshot_cpu_percent_range(self):
        monitor = SystemMonitor()
        with patch.object(monitor, "get_system_info", return_value=self._make_info(cpu=42.5)):
            snap = monitor.snapshot()
        self.assertEqual(snap["cpu_percent"], 42.5)
        self.assertGreaterEqual(snap["cpu_percent"], 0.0)
        self.assertLessEqual(snap["cpu_percent"], 100.0)

    def test_snapshot_ram_percent_range(self):
        monitor = SystemMonitor()
        with patch.object(monitor, "get_system_info", return_value=self._make_info(mem=77.0)):
            snap = monitor.snapshot()
        self.assertEqual(snap["ram_percent"], 77.0)
        self.assertGreaterEqual(snap["ram_percent"], 0.0)
        self.assertLessEqual(snap["ram_percent"], 100.0)

    def test_snapshot_disk_percent_computed_correctly(self):
        monitor = SystemMonitor()
        # 200 GB free of 500 GB total => 60% used
        with patch.object(monitor, "get_system_info", return_value=self._make_info(
            disk_total=500.0, disk_free=200.0
        )):
            snap = monitor.snapshot()
        self.assertAlmostEqual(snap["disk_percent"], 60.0, places=1)

    def test_snapshot_disk_percent_zero_when_no_total(self):
        monitor = SystemMonitor()
        with patch.object(monitor, "get_system_info", return_value=self._make_info(
            disk_total=0.0, disk_free=0.0
        )):
            snap = monitor.snapshot()
        self.assertEqual(snap["disk_percent"], 0.0)

    def test_snapshot_disk_percent_within_0_100(self):
        monitor = SystemMonitor()
        with patch.object(monitor, "get_system_info", return_value=self._make_info(
            disk_total=100.0, disk_free=5.0
        )):
            snap = monitor.snapshot()
        self.assertGreaterEqual(snap["disk_percent"], 0.0)
        self.assertLessEqual(snap["disk_percent"], 100.0)

    def test_snapshot_gpu_percent_is_none_without_dedicated_gpu(self):
        """На Apple Silicon / без psutil gpu_percent должен быть None."""
        monitor = SystemMonitor()
        with patch.object(monitor, "get_system_info", return_value=self._make_info()):
            snap = monitor.snapshot()
        # gpu_percent всегда None, пока нет явного GPU-счётчика
        self.assertIsNone(snap["gpu_percent"])

    def test_snapshot_gpu_percent_none_when_psutil_absent(self):
        """Если psutil не установлен, gpu_percent = None (не Exception)."""
        monitor = SystemMonitor()
        with patch.object(monitor, "get_system_info", return_value=self._make_info()):
            with patch("builtins.__import__", side_effect=lambda name, *a, **k: (
                (_ for _ in ()).throw(ImportError(name)) if name == "psutil" else __import__(name, *a, **k)
            )):
                snap = monitor.snapshot()
        self.assertIsNone(snap["gpu_percent"])

    def test_snapshot_returns_live_values(self):
        """Без моков snapshot() должен вернуть словарь с числовыми значениями."""
        monitor = SystemMonitor()
        snap = monitor.snapshot()
        self.assertIsInstance(snap["cpu_percent"], float)
        self.assertIsInstance(snap["ram_percent"], float)
        self.assertIsInstance(snap["disk_percent"], float)
        # gpu_percent может быть None
        self.assertTrue(snap["gpu_percent"] is None or isinstance(snap["gpu_percent"], float))


class TestSnapshotMockedPsutil(unittest.TestCase):
    """Тесты snapshot() при доступном psutil — проверяем что gpu_percent остаётся None.

    psutil не предоставляет GPU percent, поэтому даже при наличии библиотеки
    значение должно быть None.
    """

    def test_snapshot_with_psutil_present_gpu_still_none(self):
        monitor = SystemMonitor()
        mock_psutil = MagicMock()
        mock_psutil.__name__ = "psutil"
        fake_info = {
            "cpu_percent": 10.0,
            "memory_percent": 30.0,
            "disk_total_gb": 200.0,
            "disk_free_gb": 150.0,
        }
        with patch.object(monitor, "get_system_info", return_value=fake_info):
            with patch.dict("sys.modules", {"psutil": mock_psutil}):
                snap = monitor.snapshot()
        self.assertIsNone(snap["gpu_percent"])
        self.assertEqual(snap["cpu_percent"], 10.0)
        self.assertEqual(snap["ram_percent"], 30.0)

    def test_snapshot_all_fields_in_0_100_range(self):
        monitor = SystemMonitor()
        for cpu, mem, dt, df in [
            (0.0, 0.0, 100.0, 100.0),
            (100.0, 100.0, 100.0, 0.0),
            (50.0, 50.0, 200.0, 100.0),
        ]:
            fake_info = {
                "cpu_percent": cpu,
                "memory_percent": mem,
                "disk_total_gb": dt,
                "disk_free_gb": df,
            }
            with patch.object(monitor, "get_system_info", return_value=fake_info):
                snap = monitor.snapshot()
            self.assertGreaterEqual(snap["cpu_percent"], 0.0)
            self.assertLessEqual(snap["cpu_percent"], 100.0)
            self.assertGreaterEqual(snap["ram_percent"], 0.0)
            self.assertLessEqual(snap["ram_percent"], 100.0)
            self.assertGreaterEqual(snap["disk_percent"], 0.0)
            self.assertLessEqual(snap["disk_percent"], 100.0)


if __name__ == "__main__":
    unittest.main()
