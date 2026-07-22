"""Интеграционные тесты wiring GracefulShutdownHandler в main() — W981.

Проверяют signal-safe SIGTERM-путь, единый shutdown-координатор, операции
сохранения/закрытия и корректную семантику ``shutdown_in_progress``.
"""

from __future__ import annotations

import signal
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = str(Path(__file__).parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.shutdown_handler import GracefulShutdownHandler  # noqa: E402


class TestShutdownInProgressBug(unittest.TestCase):
    """W975 MEDIUM: shutdown_in_progress должно быть True while shutdown runs."""

    def _make_handler(self) -> GracefulShutdownHandler:
        with tempfile.TemporaryDirectory() as d:
            h = GracefulShutdownHandler(data_dir=d)
        return h

    def test_shutdown_in_progress_false_before_start(self) -> None:
        """До вызова shutdown() — False (ничего не происходит)."""
        h = GracefulShutdownHandler(data_dir=None)
        status = h.get_shutdown_status()
        self.assertFalse(status["shutdown_in_progress"])

    def test_shutdown_in_progress_false_after_done(self) -> None:
        """После завершения shutdown() — False (уже закончен)."""
        h = GracefulShutdownHandler(data_dir=None)
        h.shutdown()
        status = h.get_shutdown_status()
        self.assertFalse(status["shutdown_in_progress"])

    def test_shutdown_in_progress_true_while_running(self) -> None:
        """Пока shutdown() выполняется в другом треде — True."""
        h = GracefulShutdownHandler(data_dir=None)

        barrier = threading.Barrier(2, timeout=5)
        in_progress_seen: list[bool] = []

        original_save_vocabulary = h._save_vocabulary

        def slow_save_vocabulary(service):  # type: ignore[no-untyped-def]
            # Синхронизация: главный тред читает статус, пока shutdown в процессе.
            barrier.wait()
            # Ждём пока главный тред прочитает статус.
            barrier.wait()
            original_save_vocabulary(service)

        h._save_vocabulary = slow_save_vocabulary  # type: ignore[method-assign]

        shutdown_thread = threading.Thread(target=h.shutdown, daemon=True)
        shutdown_thread.start()

        barrier.wait()  # ждём входа в slow_save_vocabulary
        in_progress_seen.append(h.get_shutdown_status()["shutdown_in_progress"])
        barrier.wait()  # отпускаем shutdown

        shutdown_thread.join(timeout=5)
        self.assertTrue(in_progress_seen[0], "shutdown_in_progress должно быть True пока shutdown выполняется")

    def test_shutdown_started_not_done_is_in_progress(self) -> None:
        """Прямой unit-test формулы: started=True, done=False → in_progress=True."""
        h = GracefulShutdownHandler(data_dir=None)
        # Симулируем: started но не done (событие не установлено).
        with h._lock:
            h._shutdown_started = True
        # _shutdown_done.is_set() == False по умолчанию.
        self.assertFalse(h._shutdown_done.is_set())
        status = h.get_shutdown_status()
        self.assertTrue(status["shutdown_in_progress"])

    def test_shutdown_started_and_done_not_in_progress(self) -> None:
        """started=True, done=True → in_progress=False (уже завершён)."""
        h = GracefulShutdownHandler(data_dir=None)
        with h._lock:
            h._shutdown_started = True
        h._shutdown_done.set()
        status = h.get_shutdown_status()
        self.assertFalse(status["shutdown_in_progress"])


class TestGracefulShutdownAllSteps(unittest.TestCase):
    """GracefulShutdownHandler.shutdown() выполняет все 6 шагов."""

    def _make_service(self) -> MagicMock:
        """Создаёт мок-сервис со всеми атрибутами, которые проверяет shutdown."""
        svc = MagicMock()

        # vocabulary — load() возвращает список слов
        svc.vocabulary.load.return_value = ["word1", "word2"]

        # _usage_tracker._persist callable
        svc._usage_tracker._persist = MagicMock()

        # _playback_tracker._save callable
        svc._playback_tracker._save = MagicMock()

        # store.maybe_compact callable, возвращает True
        svc.store.maybe_compact.return_value = True

        # _ipc_server.stop callable
        svc._ipc_server.stop = MagicMock()

        # _audit_logger.close callable
        svc._audit_logger.close = MagicMock()

        return svc

    def test_all_six_steps_called(self) -> None:
        """Все 6 шагов вызываются при штатном завершении."""
        with tempfile.TemporaryDirectory() as d:
            h = GracefulShutdownHandler(data_dir=d)
            svc = self._make_service()
            h._service = svc
            h.shutdown()

        # 1. Vocabulary save
        svc.vocabulary.load.assert_called_once()
        svc.vocabulary.save.assert_called_once_with(["word1", "word2"])

        # 2. Audit log flush
        svc._audit_logger.close.assert_called_once()

        # 3. Usage stats
        svc._usage_tracker._persist.assert_called_once()

        # 4. Playback stats
        svc._playback_tracker._save.assert_called_once()

        # 5. History compaction
        svc.store.maybe_compact.assert_called_once()

        # 6. Socket close
        svc._ipc_server.stop.assert_called_once()

    def test_idempotent_double_shutdown(self) -> None:
        """Повторный вызов shutdown() не вызывает шаги дважды."""
        with tempfile.TemporaryDirectory() as d:
            h = GracefulShutdownHandler(data_dir=d)
            svc = self._make_service()
            h._service = svc
            h.shutdown()
            h.shutdown()  # второй вызов — no-op

        svc.vocabulary.load.assert_called_once()
        svc._audit_logger.close.assert_called_once()

    def test_step_failure_continues_remaining(self) -> None:
        """Если один шаг бросает исключение — остальные шаги всё равно выполняются."""
        with tempfile.TemporaryDirectory() as d:
            h = GracefulShutdownHandler(data_dir=d)
            svc = self._make_service()
            # Vocabulary load взрывается
            svc.vocabulary.load.side_effect = OSError("disk full")
            h._service = svc
            h.shutdown()

        # Audit log всё равно должен быть закрыт
        svc._audit_logger.close.assert_called_once()
        # IPC socket всё равно закрыт
        svc._ipc_server.stop.assert_called_once()

    def test_shutdown_done_event_set_after_completion(self) -> None:
        """После shutdown() событие _shutdown_done установлено."""
        h = GracefulShutdownHandler(data_dir=None)
        h.shutdown()
        self.assertTrue(h._shutdown_done.is_set())

    def test_clean_flag_false_on_step_error(self) -> None:
        """Если шаг завершился с ошибкой — clean=False в статусе."""
        with tempfile.TemporaryDirectory() as d:
            h = GracefulShutdownHandler(data_dir=d)
            svc = self._make_service()
            svc.vocabulary.load.side_effect = RuntimeError("boom")
            h._service = svc
            h.shutdown()

        status = h.get_shutdown_status()
        self.assertFalse(status["clean"])

    def test_clean_flag_true_on_success(self) -> None:
        """Все шаги успешны → clean=True."""
        with tempfile.TemporaryDirectory() as d:
            h = GracefulShutdownHandler(data_dir=d)
            svc = self._make_service()
            h._service = svc
            h.shutdown()

        status = h.get_shutdown_status()
        self.assertTrue(status["clean"])


class TestShutdownHandlerWiredInMain(unittest.TestCase):
    """Проверяет единственное владение shutdown-путём в service.main().

    Подход: статический AST-анализ + grep исходника service.py.
    Не импортирует backend.service (несовместимо с Python 3.9 из-за slots=True).
    """

    SERVICE_PY = Path(__file__).parent.parent / "backend" / "service.py"

    def _source(self) -> str:
        return self.SERVICE_PY.read_text(encoding="utf-8")

    def _main_body(self) -> str:
        """Извлекает тело функции main() как строку."""
        import ast
        tree = ast.parse(self._source())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                lines = self._source().splitlines()
                start = node.lineno - 1
                end = node.end_lineno  # type: ignore[attr-defined]
                return "\n".join(lines[start:end])
        return ""

    def test_bind_called_in_main_without_signal_registration(self) -> None:
        """main() привязывает metadata-handler без временного перехвата сигналов."""
        body = self._main_body()
        self.assertIn(
            "_shutdown_handler.bind(",
            body,
            "main() должен привязать service к metadata-handler",
        )
        self.assertNotIn(
            "_shutdown_handler.register(",
            body,
            "legacy register() не должен перехватывать production-сигналы",
        )

    def test_signal_handler_only_requests_accept_loop_stop(self) -> None:
        """Signal callback не содержит teardown, lock, I/O или логирования."""
        body = self._main_body()
        self.assertIn(
            "server.request_stop_from_signal()",
            body,
            "Signal callback должен только попросить IPC-loop выйти",
        )
        signal_body = body.split("def _signal_handler", 1)[1].split(
            "signal.signal", 1
        )[0]
        for forbidden in ("shutdown(", "server.stop(", "service.close(", "logger."):
            self.assertNotIn(forbidden, signal_body)

    def test_single_coordinator_called_in_finally_block(self) -> None:
        """Весь teardown выполняется одной функцией только из finally."""
        import ast
        source = self._source()
        tree = ast.parse(source)

        # Находим функцию main()
        main_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                main_func = node
                break

        self.assertIsNotNone(main_func, "Функция main() не найдена в service.py")

        # Ищем Try-блок с finally в теле main()
        found_finally_shutdown = False
        for node in ast.walk(main_func):  # type: ignore[arg-type]
            if isinstance(node, ast.Try):
                # Python 3.9: финальные блоки — node.finalbody
                finalbody = getattr(node, "finalbody", [])
                lines = source.splitlines()
                for stmt in finalbody:
                    # Получаем строки finally-блока
                    end_line = stmt.end_lineno if hasattr(stmt, "end_lineno") else stmt.lineno
                    finally_code = "\n".join(lines[stmt.lineno - 1:end_line])
                    if "_shutdown_backend(" in finally_code:
                        found_finally_shutdown = True
                        break

        self.assertTrue(
            found_finally_shutdown,
            "finally-блок main() должен вызывать _shutdown_backend()",
        )

    def test_ipc_server_assigned_before_bind(self) -> None:
        """service._ipc_server = server происходит до bind() в main()."""
        body = self._main_body()
        ipc_assign_pos = body.find("._ipc_server = server")
        bind_pos = body.find("._shutdown_handler.bind(")

        self.assertGreater(
            ipc_assign_pos, -1,
            "main() должен содержать присваивание service._ipc_server = server",
        )
        self.assertGreater(
            bind_pos, -1,
            "main() должен содержать вызов _shutdown_handler.bind(service)",
        )
        self.assertLess(
            ipc_assign_pos,
            bind_pos,
            "_ipc_server должен присваиваться ДО вызова bind()",
        )


class TestShutdownHandlerRegisterAPI(unittest.TestCase):
    """Проверяет API GracefulShutdownHandler.register()."""

    def test_register_installs_sigterm_handler(self) -> None:
        """register() устанавливает обработчик SIGTERM."""
        h = GracefulShutdownHandler(data_dir=None)
        svc = MagicMock()

        installed: dict[int, object] = {}
        with patch("signal.signal", side_effect=lambda sig, hnd: installed.__setitem__(sig, hnd)):
            h.register(svc)

        self.assertIn(signal.SIGTERM, installed)
        self.assertIn(signal.SIGINT, installed)

    def test_register_sets_service(self) -> None:
        """После register() _service == переданный объект."""
        h = GracefulShutdownHandler(data_dir=None)
        svc = MagicMock()
        with patch("signal.signal"):
            h.register(svc)
        with h._lock:
            self.assertIs(h._service, svc)

    def test_register_rejects_service_without_signal_stop_request(self) -> None:
        """Несовместимый legacy-service не превращает SIGTERM в тихий no-op."""
        h = GracefulShutdownHandler(data_dir=None)
        svc = MagicMock()
        svc._ipc_server = None

        with patch("signal.signal") as install_signal:
            with self.assertRaises(TypeError):
                h.register(svc)
        install_signal.assert_not_called()
        self.assertIsNone(h._service)

    def test_register_rejects_ipc_without_full_stop(self) -> None:
        """Одной signal-метки недостаточно для доказуемой квиесценции."""
        h = GracefulShutdownHandler(data_dir=None)
        svc = MagicMock()
        svc._ipc_server = MagicMock(spec=["request_stop_from_signal"])

        with patch("signal.signal") as install_signal:
            with self.assertRaises(TypeError):
                h.register(svc)
        install_signal.assert_not_called()
        self.assertIsNone(h._service)

    def test_signal_handler_only_requests_ipc_stop(self) -> None:
        """Legacy register() также устанавливает request-only callback."""
        h = GracefulShutdownHandler(data_dir=None)
        svc = MagicMock()

        installed: dict[int, object] = {}
        with patch("signal.signal", side_effect=lambda sig, hnd: installed.__setitem__(sig, hnd)):
            h.register(svc)

        # Вызываем handler напрямую
        handler = installed[signal.SIGTERM]
        self.assertTrue(callable(handler))
        with patch.object(h, "shutdown") as shutdown:
            handler(signal.SIGTERM, None)  # type: ignore[call-arg]
            shutdown.assert_not_called()
        svc._ipc_server.request_stop_from_signal.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
