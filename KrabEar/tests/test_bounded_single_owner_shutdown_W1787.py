"""Регрессии W1787 для единственного bounded shutdown-координатора.

Файл фиксирует порядок IPC → workers → metadata и fail-closed поведение:
пока очередной владелец не подтвердил завершение, следующий ресурс не трогаем.
Все тесты используют только управляемые дубли и не запускают живой backend.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.service import _shutdown_backend  # noqa: E402


class TestBoundedSingleOwnerShutdown(unittest.TestCase):
    """Проверяет последовательность и аварийные границы координатора."""

    def _run(
        self,
        *,
        ipc_result=True,
        workers_result=True,
        metadata_result=True,
    ):
        events: list[str] = []
        exit_codes: list[int] = []

        def _stage(name: str, result):
            events.append(name)
            if isinstance(result, Exception):
                raise result
            return result

        server = MagicMock()
        server.stop.side_effect = lambda: _stage("ipc", ipc_result)

        service = MagicMock()
        service.close.side_effect = lambda: _stage("workers", workers_result)

        shutdown_handler = MagicMock()
        shutdown_handler.shutdown.side_effect = (
            lambda **_kwargs: _stage("metadata", metadata_result)
        )

        result = _shutdown_backend(
            service,
            server,
            shutdown_handler,
            flush_fn=lambda: events.append("flush"),
            exit_fn=lambda code: (
                events.append("exit"),
                exit_codes.append(code),
            ),
        )
        return (
            result,
            events,
            exit_codes,
            service,
            server,
            shutdown_handler,
        )

    def test_success_uses_ipc_workers_metadata_order_once(self) -> None:
        """Штатный путь проходит каждый ownership-этап ровно один раз."""
        result, events, exits, service, server, handler = self._run()

        self.assertTrue(result)
        self.assertEqual(events, ["ipc", "workers", "metadata", "flush"])
        self.assertEqual(exits, [])
        server.stop.assert_called_once_with()
        service.close.assert_called_once_with()
        handler.shutdown.assert_called_once_with(ipc_already_stopped=True)

    def test_ipc_false_flushes_and_exits_before_workers(self) -> None:
        """Недренированный handler запрещает close и persistence."""
        result, events, exits, service, server, handler = self._run(
            ipc_result=False
        )

        self.assertFalse(result)
        self.assertEqual(events, ["ipc", "flush", "exit"])
        self.assertEqual(exits, [os.EX_SOFTWARE])
        server.stop.assert_called_once_with()
        service.close.assert_not_called()
        handler.shutdown.assert_not_called()

    def test_ipc_exception_is_the_same_fail_closed_barrier(self) -> None:
        """Исключение IPC drain не должно проваливаться в Python-finalize."""
        result, events, exits, service, server, handler = self._run(
            ipc_result=RuntimeError("join failed")
        )

        self.assertFalse(result)
        self.assertEqual(events, ["ipc", "flush", "exit"])
        self.assertEqual(exits, [os.EX_SOFTWARE])
        server.stop.assert_called_once_with()
        service.close.assert_not_called()
        handler.shutdown.assert_not_called()

    def test_legacy_none_ipc_result_is_not_false_hung_evidence(self) -> None:
        """Старый дубль с None не превращается в ложный hard-exit."""
        result, events, exits, service, server, handler = self._run(
            ipc_result=None
        )

        self.assertTrue(result)
        self.assertEqual(events, ["ipc", "workers", "metadata", "flush"])
        self.assertEqual(exits, [])
        service.close.assert_called_once_with()
        handler.shutdown.assert_called_once_with(ipc_already_stopped=True)

    def test_worker_false_skips_metadata_and_hard_exits(self) -> None:
        """Живой audio/native worker не допускает закрытие общих metadata."""
        result, events, exits, service, _server, handler = self._run(
            workers_result=False
        )

        self.assertFalse(result)
        self.assertEqual(events, ["ipc", "workers", "flush", "exit"])
        self.assertEqual(exits, [os.EX_SOFTWARE])
        service.close.assert_called_once_with()
        handler.shutdown.assert_not_called()

    def test_worker_exception_skips_metadata_and_hard_exits(self) -> None:
        """Исключение close также запрещает дальнейшее закрытие ресурсов."""
        result, events, exits, service, _server, handler = self._run(
            workers_result=RuntimeError("worker close failed")
        )

        self.assertFalse(result)
        self.assertEqual(events, ["ipc", "workers", "flush", "exit"])
        self.assertEqual(exits, [os.EX_SOFTWARE])
        service.close.assert_called_once_with()
        handler.shutdown.assert_not_called()

    def test_metadata_false_flushes_before_hard_exit(self) -> None:
        """Неожиданный отказ metadata single-flight завершается fail-closed."""
        result, events, exits, _service, _server, handler = self._run(
            metadata_result=False
        )

        self.assertFalse(result)
        self.assertEqual(
            events,
            ["ipc", "workers", "metadata", "flush", "exit"],
        )
        self.assertEqual(exits, [os.EX_SOFTWARE])
        handler.shutdown.assert_called_once_with(ipc_already_stopped=True)

    def test_metadata_exception_flushes_before_hard_exit(self) -> None:
        """Неожиданное исключение metadata не обходит fail-closed policy."""
        result, events, exits, _service, _server, handler = self._run(
            metadata_result=RuntimeError("persist failed")
        )

        self.assertFalse(result)
        self.assertEqual(
            events,
            ["ipc", "workers", "metadata", "flush", "exit"],
        )
        self.assertEqual(exits, [os.EX_SOFTWARE])
        handler.shutdown.assert_called_once_with(ipc_already_stopped=True)


if __name__ == "__main__":
    unittest.main()
