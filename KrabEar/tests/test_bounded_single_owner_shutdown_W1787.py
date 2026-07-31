"""Регрессии W1787 для единственного bounded shutdown-координатора.

Файл фиксирует порядок IPC → workers → metadata и fail-closed поведение:
пока очередной владелец не подтвердил завершение, следующий ресурс не трогаем.
Все тесты используют только управляемые дубли и не запускают живой backend.
"""

from __future__ import annotations

import os
import sys
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import backend.service as service_module  # noqa: E402
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
        server.stop.side_effect = lambda **_kw: _stage("ipc", ipc_result)

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
        server.stop.assert_called_once_with(timeout_sec=service_module._IPC_DRAIN_BUDGET_SEC)
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
        server.stop.assert_called_once_with(timeout_sec=service_module._IPC_DRAIN_BUDGET_SEC)
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
        server.stop.assert_called_once_with(timeout_sec=service_module._IPC_DRAIN_BUDGET_SEC)
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


class DrainBudgetTestCase(unittest.TestCase):
    """Приёмочное ревью 2026-07-23 (F1): бюджет дренажа IPC.

    ``handle_request`` выполняется В handler-потоке и включает весь STT-пайплайн
    (`handle_stop_recording`/`meeting_stop`/`transcribe_paths`) — секунды-минуты.
    С дефолтными 1.5 с координатор объявляет барьер недоказанным и делает
    ``os._exit`` ДО ``service.close()``/metadata: словарь, usage, playback,
    компактирование и ``shutdown_info.json`` теряются. Живой триггер есть:
    эскалация WakeWordWatchdog → forceRestartBackend → ``kickstart -k``
    минует ``safe_backend_restart.command``. ``ExitTimeOut=15`` в plist
    оставляет запас на 8 с дренажа + close() + metadata.
    """

    def test_coordinator_grants_explicit_drain_budget(self) -> None:
        service = MagicMock()
        service.close.return_value = True
        server = MagicMock()
        server.stop.return_value = True
        handler = MagicMock()
        handler.shutdown.return_value = True

        result = _shutdown_backend(
            service,
            server,
            handler,
            flush_fn=lambda: None,
            exit_fn=lambda code: None,
        )

        self.assertTrue(result)
        server.stop.assert_called_once()
        _, kwargs = server.stop.call_args
        budget = kwargs.get("timeout_sec")
        self.assertIsNotNone(
            budget,
            "координатор обязан задавать явный бюджет дренажа, а не дефолтные 1.5 с",
        )
        self.assertGreaterEqual(budget, 8.0, "бюджет мал для STT-запроса в handler-потоке")
        self.assertLessEqual(budget, 12.0, "бюджет обязан помещаться в ExitTimeOut=15")


class RestInProcessBeginShutdownOrderTestCase(unittest.TestCase):
    """S3/Задача 5, п.5: shutting_down взводится ДО IPCServer.stop().

    Порядок важен: 8с IPC-дренажа ниже обязаны работать ОДНОВРЕМЕННО как окно
    дренажа REST (допуск уже закрыт), а не последовательно после него —
    иначе бюджеты складываются и не помещаются в ExitTimeOut=15.
    """

    def test_begin_shutdown_called_before_ipc_stop(self) -> None:
        events: list[str] = []

        server = MagicMock()
        server.stop.side_effect = lambda **_kw: (events.append("ipc"), True)[1]

        service = MagicMock()
        service._rest_inprocess.begin_shutdown.side_effect = (
            lambda: events.append("rest_begin_shutdown")
        )
        service.close.side_effect = lambda: (events.append("workers"), True)[1]

        handler = MagicMock()
        handler.shutdown.side_effect = lambda **_kw: (events.append("metadata"), True)[1]

        result = _shutdown_backend(
            service, server, handler, flush_fn=lambda: None, exit_fn=lambda code: None,
        )

        self.assertTrue(result)
        self.assertEqual(
            events, ["rest_begin_shutdown", "ipc", "workers", "metadata"],
        )
        service._rest_inprocess.begin_shutdown.assert_called_once_with()

    def test_missing_rest_inprocess_does_not_break_shutdown(self) -> None:
        """service без _rest_inprocess (рубильник выключен) — не аварийная ветка."""
        service = MagicMock()
        service._rest_inprocess = None
        service.close.return_value = True
        server = MagicMock()
        server.stop.return_value = True
        handler = MagicMock()
        handler.shutdown.return_value = True

        result = _shutdown_backend(
            service, server, handler, flush_fn=lambda: None, exit_fn=lambda code: None,
        )
        self.assertTrue(result)

    def test_begin_shutdown_exception_does_not_abort_teardown(self) -> None:
        """Сбой begin_shutdown() — не повод пропускать IPC/workers/metadata."""
        service = MagicMock()
        service._rest_inprocess.begin_shutdown.side_effect = RuntimeError("boom")
        service.close.return_value = True
        server = MagicMock()
        server.stop.return_value = True
        handler = MagicMock()
        handler.shutdown.return_value = True

        result = _shutdown_backend(
            service, server, handler, flush_fn=lambda: None, exit_fn=lambda code: None,
        )
        self.assertTrue(result)
        service.close.assert_called_once_with()
        handler.shutdown.assert_called_once_with(ipc_already_stopped=True)


class HardExitObservabilityTestCase(unittest.TestCase):
    """Приёмочное ревью 2026-07-23 (F2): причина hard-exit обязана доехать.

    ``logger.critical`` порождает Sentry-событие через LoggingIntegration, но
    ставилось в очередь ПОСЛЕ ``flush_sentry()`` — а ``os._exit`` не даёт ни
    второго flush, ни atexit-хука, поэтому причина аварийного выхода терялась
    навсегда именно в том сценарии, ради наблюдаемости которого писался хелпер.
    """

    def test_flush_happens_after_critical_log(self) -> None:
        events: list[str] = []

        with unittest.mock.patch.object(
            service_module.logger, "critical", side_effect=lambda *a, **k: events.append("log")
        ):
            service_module._exit_without_python_finalize_if_worker_hung(
                False,
                exit_fn=lambda code: events.append("exit"),
                flush_fn=lambda: events.append("flush"),
            )

        self.assertEqual(
            events,
            ["log", "flush", "exit"],
            "flush обязан идти между критическим логом и os._exit",
        )


if __name__ == "__main__":
    unittest.main()
