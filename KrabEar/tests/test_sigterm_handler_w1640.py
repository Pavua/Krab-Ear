"""Регрессии W1640 для SIGTERM-владения и финального Sentry flush.

Проверяют request-only callback в ``service.main()``, порядок единственного
shutdown-координатора, отсутствие SIGTERM в observability-handler-ах и
безопасный no-op ``flush_sentry()`` без инициализированного SDK.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = str(Path(__file__).parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestFlushSentryHelper(unittest.TestCase):
    """flush_sentry() public helper — unit tests."""

    def test_noop_when_sentry_not_initialized(self) -> None:
        """flush_sentry() silently returns when _sentry_initialized is False."""
        import backend.observability as obs

        original = obs._sentry_initialized
        try:
            obs._sentry_initialized = False
            # Must not raise
            obs.flush_sentry()
            obs.flush_sentry(timeout=1.0)
        finally:
            obs._sentry_initialized = original

    def test_calls_sentry_sdk_flush_when_initialized(self) -> None:
        """flush_sentry() calls sentry_sdk.flush(timeout=...) when initialized."""
        import backend.observability as obs

        original = obs._sentry_initialized
        mock_sdk = MagicMock()

        try:
            obs._sentry_initialized = True
            with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
                obs.flush_sentry(timeout=3.0)
            mock_sdk.flush.assert_called_once_with(timeout=3.0)
        finally:
            obs._sentry_initialized = original

    def test_swallows_sdk_exception(self) -> None:
        """flush_sentry() never propagates an exception from sentry_sdk."""
        import backend.observability as obs

        original = obs._sentry_initialized
        mock_sdk = MagicMock()
        mock_sdk.flush.side_effect = RuntimeError("network error")

        try:
            obs._sentry_initialized = True
            with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
                # Must not raise
                obs.flush_sentry()
        finally:
            obs._sentry_initialized = original

    def test_default_timeout_is_two_seconds(self) -> None:
        """flush_sentry() default timeout is 2.0 s."""
        import backend.observability as obs

        original = obs._sentry_initialized
        mock_sdk = MagicMock()

        try:
            obs._sentry_initialized = True
            with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
                obs.flush_sentry()
            mock_sdk.flush.assert_called_once_with(timeout=2.0)
        finally:
            obs._sentry_initialized = original


class TestInstallSignalHandlersSkipsSigterm(unittest.TestCase):
    """install_signal_handlers() must NOT install a handler for SIGTERM (W1640)."""

    def setUp(self) -> None:
        import backend.observability as obs
        # Reset idempotency flag so we can call it fresh.
        if hasattr(obs.install_signal_handlers, "_installed"):
            del obs.install_signal_handlers._installed  # type: ignore[attr-defined]

    def test_sigterm_not_overridden_by_install_signal_handlers(self) -> None:
        """install_signal_handlers() does not touch SIGTERM; main() owns it."""
        import signal as sig
        import backend.observability as obs

        with patch.object(sig, "signal") as mock_signal:
            obs.install_signal_handlers()

        # Collect all signal numbers that were registered
        registered = {args[0] for args, _ in mock_signal.call_args_list}
        self.assertNotIn(
            sig.SIGTERM,
            registered,
            "install_signal_handlers() must not register a SIGTERM handler — "
            "main()._signal_handler owns SIGTERM (W1640)",
        )

    def test_installs_no_python_handler_at_all(self) -> None:
        """Ни один сигнал не получает Python-обработчик (инцидент 2026-08-07).

        Раньше здесь стояло ``assertIn(SIGABRT, registered)`` — тест закреплял
        ровно ту конструкцию, которая вешала прод: Python-колбэк на синхронный
        аварийный сигнал зацикливает сбойную инструкцию (CPython ставит флаг и
        возвращается) и дедлочится на локе внутри Sentry-флаша. Подробности и
        живой sample — ``tests/test_fault_signal_handler_2026_08_07.py``.
        """
        import signal as sig
        import backend.observability as obs

        with patch.object(sig, "signal") as mock_signal:
            obs.install_signal_handlers()

        registered = {args[0] for args, _ in mock_signal.call_args_list}
        self.assertEqual(
            registered, set(),
            "install_signal_handlers() снова регистрирует Python-обработчик "
            "сигнала; аварийные сигналы обслуживает faulthandler (C-уровень)",
        )


class TestSigtermRequestOnlyAndCoordinatorOrder(unittest.TestCase):
    """Проверяет фактический callback и исполняемый shutdown-helper."""

    SERVICE_PATH = Path(__file__).parent.parent / "backend" / "service.py"

    def _signal_handler_source(self) -> str:
        """Извлечь вложенный ``main._signal_handler`` через AST."""
        import ast

        source = self.SERVICE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_signal_handler":
                lines = source.splitlines()
                return "\n".join(lines[node.lineno - 1:node.end_lineno])
        self.fail("main._signal_handler не найден")

    def test_main_signal_handler_only_requests_ipc_stop(self) -> None:
        """Signal callback не делает teardown или Sentry I/O."""
        callback = self._signal_handler_source()
        self.assertIn("server.request_stop_from_signal()", callback)
        for forbidden in (
            "shutdown(",
            "server.stop(",
            "service.close(",
            "flush_sentry(",
            "logger.",
        ):
            self.assertNotIn(forbidden, callback)

    def test_coordinator_flushes_after_successful_metadata(self) -> None:
        """Sentry flush идёт после IPC, workers и metadata на green-path."""
        from backend.service import _shutdown_backend

        events: list[str] = []
        server = MagicMock()
        # Координатор передаёт явный бюджет дренажа (F1, приёмка 2026-07-23).
        server.stop.side_effect = lambda **_kw: events.append("ipc") or True
        service = MagicMock()
        service.close.side_effect = lambda: events.append("workers") or True
        handler = MagicMock()
        handler.shutdown.side_effect = (
            lambda **_kwargs: events.append("metadata") or True
        )

        self.assertTrue(
            _shutdown_backend(
                service,
                server,
                handler,
                flush_fn=lambda: events.append("flush_sentry"),
                exit_fn=MagicMock(),
            )
        )
        self.assertEqual(
            events,
            ["ipc", "workers", "metadata", "flush_sentry"],
        )

    def test_failed_ipc_flushes_before_hard_exit(self) -> None:
        """Даже fail-closed путь сначала отдаёт telemetry в Sentry."""
        import os
        from backend.service import _shutdown_backend

        events: list[str] = []
        exit_codes: list[int] = []
        server = MagicMock()
        server.stop.return_value = False
        service = MagicMock()
        handler = MagicMock()

        self.assertFalse(
            _shutdown_backend(
                service,
                server,
                handler,
                flush_fn=lambda: events.append("flush_sentry"),
                exit_fn=lambda code: (
                    events.append("hard_exit"),
                    exit_codes.append(code),
                ),
            )
        )
        self.assertEqual(events, ["flush_sentry", "hard_exit"])
        self.assertEqual(exit_codes, [os.EX_SOFTWARE])
        service.close.assert_not_called()
        handler.shutdown.assert_not_called()


class TestSigtermHandlerNoCrashWhenSentryUninitialized(unittest.TestCase):
    """Sentry flush безопасен без инициализированного SDK."""

    def test_flush_sentry_noop_uninitialized(self) -> None:
        """flush_sentry() is a no-op when _sentry_initialized is False."""
        import backend.observability as obs

        original = obs._sentry_initialized
        try:
            obs._sentry_initialized = False
            # Flush выполняет coordinator после teardown, а не signal callback.
            obs.flush_sentry()
        finally:
            obs._sentry_initialized = original


class TestFlushSentryExportedFromObservability(unittest.TestCase):
    """flush_sentry is publicly exported from observability module."""

    def test_flush_sentry_importable(self) -> None:
        """flush_sentry can be imported from backend.observability."""
        from backend.observability import flush_sentry  # noqa: F401

        self.assertTrue(callable(flush_sentry))

    def test_flush_sentry_imported_in_service(self) -> None:
        """service.py imports flush_sentry from observability."""
        import ast

        service_path = Path(__file__).parent.parent / "backend" / "service.py"
        source = service_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(service_path))

        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "observability" in node.module:
                    names = {alias.name for alias in node.names}
                    if "flush_sentry" in names:
                        found = True
                        break

        self.assertTrue(
            found,
            "service.py must import flush_sentry from backend.observability (W1640)",
        )


if __name__ == "__main__":
    unittest.main()
