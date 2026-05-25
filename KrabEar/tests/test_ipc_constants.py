"""Wave 203 — тесты для ipc_constants.py.

Покрывают:
  - IPC_SOCKET_BACKLOG > 0
  - IPC_SOCKET_TIMEOUT_SEC в разумных пределах (0 < timeout <= 30 s)
  - IPC_MAX_MESSAGE_BYTES >= 1 MB (безопасный минимум для IPC payload)
  - Константы не мутируются в рантайме
  - IPC_PREVIEW_THREAD_TIMEOUT_SEC положительное число
  - IPC_SOCKET_PERMISSIONS — owner-only (0o600)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import backend.ipc_constants as ipc_constants
from backend.ipc_constants import (
    IPC_SOCKET_BACKLOG,
    IPC_SOCKET_TIMEOUT_SEC,
    IPC_MAX_MESSAGE_BYTES,
    IPC_PREVIEW_THREAD_TIMEOUT_SEC,
    IPC_SOCKET_PERMISSIONS,
)


class TestSocketBacklog(unittest.TestCase):
    """IPC_SOCKET_BACKLOG должен быть положительным целым числом."""

    def test_socket_backlog_is_positive(self):
        self.assertIsInstance(IPC_SOCKET_BACKLOG, int)
        self.assertGreater(IPC_SOCKET_BACKLOG, 0, "Backlog must be > 0")

    def test_socket_backlog_reasonable_range(self):
        # Разумный диапазон: 1 … 4096
        self.assertGreaterEqual(IPC_SOCKET_BACKLOG, 1)
        self.assertLessEqual(IPC_SOCKET_BACKLOG, 4096,
                             "Backlog above 4096 is unusual for a single IPC socket")

    def test_socket_backlog_is_integer_not_float(self):
        self.assertNotIsInstance(IPC_SOCKET_BACKLOG, float)


class TestSocketTimeout(unittest.TestCase):
    """IPC_SOCKET_TIMEOUT_SEC должен быть в разумных пределах."""

    def test_socket_timeout_is_positive(self):
        self.assertGreater(IPC_SOCKET_TIMEOUT_SEC, 0,
                           "Timeout must be > 0 (non-blocking poll interval)")

    def test_socket_timeout_reasonable(self):
        # Для accept() poll-интервала — обычно 0.1 … 5 секунд
        self.assertGreaterEqual(IPC_SOCKET_TIMEOUT_SEC, 0.05,
                                "Timeout below 50 ms causes busy-spin")
        self.assertLessEqual(IPC_SOCKET_TIMEOUT_SEC, 30.0,
                             "Timeout above 30 s would make shutdown very slow")

    def test_socket_timeout_is_numeric(self):
        self.assertIsInstance(IPC_SOCKET_TIMEOUT_SEC, (int, float))


class TestMaxMessageBytes(unittest.TestCase):
    """IPC_MAX_MESSAGE_BYTES должен быть >= 1 MB."""

    def test_max_message_bytes_at_least_1mb(self):
        one_mb = 1024 * 1024
        self.assertGreaterEqual(
            IPC_MAX_MESSAGE_BYTES, one_mb,
            f"MAX_MESSAGE_BYTES={IPC_MAX_MESSAGE_BYTES} is below 1 MB minimum"
        )

    def test_max_message_bytes_is_positive_integer(self):
        self.assertIsInstance(IPC_MAX_MESSAGE_BYTES, int)
        self.assertGreater(IPC_MAX_MESSAGE_BYTES, 0)

    def test_max_message_bytes_safe_upper_bound(self):
        # 512 MB верхняя граница здравомыслия для IPC payload
        max_reasonable = 512 * 1024 * 1024
        self.assertLessEqual(IPC_MAX_MESSAGE_BYTES, max_reasonable,
                             "MAX_MESSAGE_BYTES exceeds 512 MB — likely a mistake")

    def test_max_message_bytes_exact_value(self):
        """Текущее значение — ровно 1 MB = 1048576 байт."""
        self.assertEqual(IPC_MAX_MESSAGE_BYTES, 1024 * 1024)


class TestConstantsNotMutatedAtRuntime(unittest.TestCase):
    """Константы модуля не должны изменяться в рантайме."""

    def test_backlog_unchanged_after_multiple_imports(self):
        original = IPC_SOCKET_BACKLOG
        # Повторный импорт не должен изменить значение
        import importlib
        importlib.reload(ipc_constants)
        self.assertEqual(ipc_constants.IPC_SOCKET_BACKLOG, original)

    def test_timeout_unchanged_after_reload(self):
        original = IPC_SOCKET_TIMEOUT_SEC
        import importlib
        importlib.reload(ipc_constants)
        self.assertEqual(ipc_constants.IPC_SOCKET_TIMEOUT_SEC, original)

    def test_max_message_bytes_unchanged_after_reload(self):
        original = IPC_MAX_MESSAGE_BYTES
        import importlib
        importlib.reload(ipc_constants)
        self.assertEqual(ipc_constants.IPC_MAX_MESSAGE_BYTES, original)

    def test_constants_are_module_level_primitives(self):
        """Константы — простые числа, не изменяемые контейнеры."""
        for name, val in [
            ("IPC_SOCKET_BACKLOG", IPC_SOCKET_BACKLOG),
            ("IPC_SOCKET_TIMEOUT_SEC", IPC_SOCKET_TIMEOUT_SEC),
            ("IPC_MAX_MESSAGE_BYTES", IPC_MAX_MESSAGE_BYTES),
            ("IPC_PREVIEW_THREAD_TIMEOUT_SEC", IPC_PREVIEW_THREAD_TIMEOUT_SEC),
            ("IPC_SOCKET_PERMISSIONS", IPC_SOCKET_PERMISSIONS),
        ]:
            self.assertIsInstance(val, (int, float),
                                  f"{name} should be a numeric primitive, got {type(val)}")


class TestPreviewThreadTimeout(unittest.TestCase):
    """IPC_PREVIEW_THREAD_TIMEOUT_SEC для join() timeout — должен быть > 0."""

    def test_preview_thread_timeout_is_positive(self):
        self.assertGreater(IPC_PREVIEW_THREAD_TIMEOUT_SEC, 0.0)

    def test_preview_thread_timeout_reasonable_range(self):
        self.assertGreaterEqual(IPC_PREVIEW_THREAD_TIMEOUT_SEC, 0.5)
        self.assertLessEqual(IPC_PREVIEW_THREAD_TIMEOUT_SEC, 30.0)


class TestSocketPermissions(unittest.TestCase):
    """IPC_SOCKET_PERMISSIONS — Unix file mode для Unix socket."""

    def test_socket_permissions_owner_only(self):
        """Ожидаем 0o600 — чтение/запись только для владельца."""
        self.assertEqual(IPC_SOCKET_PERMISSIONS, 0o600,
                         "Unix socket should be owner-read-write only (0o600)")

    def test_socket_permissions_no_group_access(self):
        """Группа не должна иметь доступ к сокету."""
        group_bits = (IPC_SOCKET_PERMISSIONS >> 3) & 0o7
        self.assertEqual(group_bits, 0, "Group should have no permissions")

    def test_socket_permissions_no_world_access(self):
        """Other не должен иметь доступ к сокету."""
        other_bits = IPC_SOCKET_PERMISSIONS & 0o7
        self.assertEqual(other_bits, 0, "Others should have no permissions")

    def test_socket_permissions_is_integer(self):
        self.assertIsInstance(IPC_SOCKET_PERMISSIONS, int)


if __name__ == "__main__":
    unittest.main()
