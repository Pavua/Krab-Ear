"""Тесты для SystemMonitor — мониторинг системных ресурсов без внешних зависимостей."""

from backend.system_monitor import SystemMonitor
import sys
import os
import unittest
from unittest.mock import patch

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

    def test_vm_stat_returns_dict(self):
        result = SystemMonitor._vm_stat()
        self.assertIsInstance(result, dict)

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


if __name__ == "__main__":
    unittest.main()
