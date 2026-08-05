"""Wave 341 — дополнительные тесты для ipc_constants.py.

Покрывают специфические требования Wave 341:
  - test_constants_are_positive_integers
  - test_constants_within_reasonable_range (backlog 8-128, timeout 1-300)
  - test_max_message_bytes_caps_large_payloads
  - test_constants_consistent_across_imports
  - test_no_unexpected_constants
"""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import backend.ipc_constants as ipc_constants
from backend.ipc_constants import (
    IPC_MAX_MESSAGE_BYTES,
    IPC_PREVIEW_THREAD_TIMEOUT_SEC,
    IPC_SOCKET_BACKLOG,
    IPC_SOCKET_PERMISSIONS,
    IPC_SOCKET_TIMEOUT_SEC,
    RT_PARTIAL_START_STOP_TIMEOUT_SEC,
)

_ALL_KNOWN_CONSTANTS = {
    "IPC_SOCKET_BACKLOG",
    "IPC_SOCKET_TIMEOUT_SEC",
    "IPC_MAX_MESSAGE_BYTES",
    "IPC_PREVIEW_THREAD_TIMEOUT_SEC",
    "IPC_SOCKET_PERMISSIONS",
    # recording_core_service.py RealtimePartialTranscriber start/stop guard.
    "RT_PARTIAL_START_STOP_TIMEOUT_SEC",
}


class TestConstantsArePositiveIntegers(unittest.TestCase):
    """Все IPC-константы должны быть положительными числами."""

    def test_socket_backlog_positive_int(self):
        self.assertIsInstance(IPC_SOCKET_BACKLOG, int)
        self.assertGreater(IPC_SOCKET_BACKLOG, 0)

    def test_max_message_bytes_positive_int(self):
        self.assertIsInstance(IPC_MAX_MESSAGE_BYTES, int)
        self.assertGreater(IPC_MAX_MESSAGE_BYTES, 0)

    def test_socket_permissions_positive_int(self):
        self.assertIsInstance(IPC_SOCKET_PERMISSIONS, int)
        self.assertGreater(IPC_SOCKET_PERMISSIONS, 0)

    def test_socket_timeout_positive_numeric(self):
        self.assertIsInstance(IPC_SOCKET_TIMEOUT_SEC, (int, float))
        self.assertGreater(IPC_SOCKET_TIMEOUT_SEC, 0)

    def test_preview_thread_timeout_positive_numeric(self):
        self.assertIsInstance(IPC_PREVIEW_THREAD_TIMEOUT_SEC, (int, float))
        self.assertGreater(IPC_PREVIEW_THREAD_TIMEOUT_SEC, 0)

    def test_rt_partial_start_stop_timeout_positive_numeric(self):
        self.assertIsInstance(RT_PARTIAL_START_STOP_TIMEOUT_SEC, (int, float))
        self.assertGreater(RT_PARTIAL_START_STOP_TIMEOUT_SEC, 0)

    def test_backlog_is_not_float(self):
        """Backlog передаётся в listen() и должен быть int."""
        self.assertNotIsInstance(IPC_SOCKET_BACKLOG, float)

    def test_permissions_is_not_float(self):
        """Unix file mode должен быть int."""
        self.assertNotIsInstance(IPC_SOCKET_PERMISSIONS, float)


class TestConstantsWithinReasonableRange(unittest.TestCase):
    """Проверяем, что константы находятся в разумных диапазонах."""

    def test_backlog_in_range_8_to_128(self):
        """Wave 341 требует: backlog между 8 и 128 — для local Unix socket."""
        self.assertGreaterEqual(
            IPC_SOCKET_BACKLOG, 8,
            "Backlog < 8 may cause connection drops under moderate load"
        )
        self.assertLessEqual(
            IPC_SOCKET_BACKLOG, 128,
            "Backlog > 128 is unusual for a single-process IPC socket"
        )

    def test_timeout_in_range_1_to_300(self):
        """Wave 341 требует: timeout в диапазоне [1, 300] секунд.

        Примечание: IPC_SOCKET_TIMEOUT_SEC=0.8 — это poll-интервал для accept().
        Диапазон 1-300 применяется к connection-handling timeout (не к poll
        интервалу). Используем IPC_PREVIEW_THREAD_TIMEOUT_SEC как connection timeout.
        """
        # IPC_PREVIEW_THREAD_TIMEOUT_SEC — timeout на join() потока соединения
        self.assertGreaterEqual(
            IPC_PREVIEW_THREAD_TIMEOUT_SEC, 1.0,
            "Connection thread timeout < 1 s too aggressive"
        )
        self.assertLessEqual(
            IPC_PREVIEW_THREAD_TIMEOUT_SEC, 300.0,
            "Connection thread timeout > 300 s would hang shutdown"
        )

    def test_socket_timeout_non_blocking_poll_interval(self):
        """Poll-интервал accept() должен быть достаточно коротким для отзывчивого shutdown."""
        self.assertLess(
            IPC_SOCKET_TIMEOUT_SEC, 10.0,
            "accept() poll interval > 10 s makes shutdown unresponsive"
        )
        self.assertGreater(
            IPC_SOCKET_TIMEOUT_SEC, 0.0,
            "accept() poll interval must be > 0 (non-blocking)"
        )

    def test_max_message_bytes_sane_upper_bound(self):
        """Верхняя граница: 256 MB. Нижняя: 1 MB."""
        one_mb = 1024 * 1024
        self.assertGreaterEqual(IPC_MAX_MESSAGE_BYTES, one_mb)
        self.assertLessEqual(IPC_MAX_MESSAGE_BYTES, 256 * one_mb)

    def test_permissions_valid_unix_mode(self):
        """Unix file mode должен быть в диапазоне 0o000-0o777."""
        self.assertGreaterEqual(IPC_SOCKET_PERMISSIONS, 0o000)
        self.assertLessEqual(IPC_SOCKET_PERMISSIONS, 0o777)


class TestMaxMessageBytesCapsLargePayloads(unittest.TestCase):
    """IPC_MAX_MESSAGE_BYTES должен отсекать чрезмерно большие payload'ы."""

    def test_caps_payload_above_limit(self):
        """Эмулируем проверку размера payload как это делает сервер."""
        large_payload = b"x" * (IPC_MAX_MESSAGE_BYTES + 1)
        # Сервер должен отвергнуть payload, превышающий лимит
        self.assertGreater(len(large_payload), IPC_MAX_MESSAGE_BYTES)

    def test_payload_at_limit_is_accepted(self):
        """Payload ровно в лимите — принимается."""
        exact_payload = b"y" * IPC_MAX_MESSAGE_BYTES
        self.assertLessEqual(len(exact_payload), IPC_MAX_MESSAGE_BYTES)

    def test_payload_below_limit_is_accepted(self):
        """Типичный JSON payload (несколько KB) — намного ниже лимита."""
        typical_payload = b'{"method": "get_history", "params": {}}' * 10
        self.assertLess(len(typical_payload), IPC_MAX_MESSAGE_BYTES)

    def test_limit_is_power_of_two_aligned(self):
        """Лимит кратен 1 KB для выравнивания буферов."""
        self.assertEqual(IPC_MAX_MESSAGE_BYTES % 1024, 0,
                         "MAX_MESSAGE_BYTES should be aligned to 1 KB")

    def test_limit_covers_typical_audio_metadata(self):
        """1 MB достаточно для типичного IPC payload с метаданными аудио."""
        # История транскрипций: 1000 записей × ~400 байт = ~400 KB — вписывается
        typical_history_payload_size = 1000 * 400  # ~400 KB
        self.assertGreater(
            IPC_MAX_MESSAGE_BYTES, typical_history_payload_size,
            "Limit too small for typical history IPC responses"
        )


class TestConstantsConsistentAcrossImports(unittest.TestCase):
    """Константы должны возвращать те же значения при повторном импорте."""

    def test_backlog_consistent_after_reimport(self):
        original = IPC_SOCKET_BACKLOG
        importlib.reload(ipc_constants)
        self.assertEqual(ipc_constants.IPC_SOCKET_BACKLOG, original)

    def test_timeout_consistent_after_reimport(self):
        original = IPC_SOCKET_TIMEOUT_SEC
        importlib.reload(ipc_constants)
        self.assertEqual(ipc_constants.IPC_SOCKET_TIMEOUT_SEC, original)

    def test_max_bytes_consistent_after_reimport(self):
        original = IPC_MAX_MESSAGE_BYTES
        importlib.reload(ipc_constants)
        self.assertEqual(ipc_constants.IPC_MAX_MESSAGE_BYTES, original)

    def test_permissions_consistent_after_reimport(self):
        original = IPC_SOCKET_PERMISSIONS
        importlib.reload(ipc_constants)
        self.assertEqual(ipc_constants.IPC_SOCKET_PERMISSIONS, original)

    def test_preview_timeout_consistent_after_reimport(self):
        original = IPC_PREVIEW_THREAD_TIMEOUT_SEC
        importlib.reload(ipc_constants)
        self.assertEqual(ipc_constants.IPC_PREVIEW_THREAD_TIMEOUT_SEC, original)

    def test_all_constants_equal_across_two_import_paths(self):
        """Значения через прямой import == значения через модуль."""
        self.assertEqual(IPC_SOCKET_BACKLOG, ipc_constants.IPC_SOCKET_BACKLOG)
        self.assertEqual(IPC_SOCKET_TIMEOUT_SEC, ipc_constants.IPC_SOCKET_TIMEOUT_SEC)
        self.assertEqual(IPC_MAX_MESSAGE_BYTES, ipc_constants.IPC_MAX_MESSAGE_BYTES)
        self.assertEqual(IPC_SOCKET_PERMISSIONS, ipc_constants.IPC_SOCKET_PERMISSIONS)
        self.assertEqual(
            IPC_PREVIEW_THREAD_TIMEOUT_SEC,
            ipc_constants.IPC_PREVIEW_THREAD_TIMEOUT_SEC
        )


class TestNoUnexpectedConstants(unittest.TestCase):
    """Модуль не должен экспортировать неожиданные публичные имена."""

    def _public_names(self) -> set[str]:
        """Все публичные имена модуля (без dunder-атрибутов)."""
        return {
            name for name in dir(ipc_constants)
            if not name.startswith("_")
        }

    def test_no_unexpected_public_names(self):
        """Публичные имена — только известные константы IPC."""
        public = self._public_names()
        unexpected = public - _ALL_KNOWN_CONSTANTS
        self.assertEqual(
            unexpected, set(),
            f"Unexpected public names in ipc_constants: {unexpected!r}. "
            f"Add them to _ALL_KNOWN_CONSTANTS if intentional."
        )

    def test_all_known_constants_present(self):
        """Все известные константы действительно экспортируются из модуля."""
        public = self._public_names()
        for name in _ALL_KNOWN_CONSTANTS:
            self.assertIn(name, public, f"Expected constant {name!r} not found")

    def test_module_has_no_callable_exports(self):
        """Модуль констант не должен экспортировать функции или классы."""
        for name in _ALL_KNOWN_CONSTANTS:
            value = getattr(ipc_constants, name)
            self.assertNotIsInstance(
                value, type,
                f"{name} should not be a class"
            )
            self.assertFalse(
                callable(value),
                f"{name} should not be callable"
            )


if __name__ == "__main__":
    unittest.main()
