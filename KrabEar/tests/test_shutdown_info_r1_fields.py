"""Тесты R1 Task 5 — расширение shutdown_info.json форензическими полями.

Новые поля (аддитивные к last_shutdown_time/clean/elapsed_ms/errors):
signal, uptime_sec, recording_active, meeting_active, pid.

Новый файл — существующие shutdown-тесты (test_shutdown_handler.py,
test_shutdown_handler_deep.py, test_shutdown_handler_wired_in_main.py) НЕ
трогать (Global Constraints плана R1 "надёжность записи").

Ключевой инвариант (F1/F5 приёмки #1891, сохранён и расширен здесь):
GracefulShutdownHandler._signal_handler исполняется КАК SIGNAL CALLBACK ОС —
никаких локов/I/O/логирования внутри него. Кейс (d) проверяет это через AST
разбор тела метода (а не substring на весь исходник функции), потому что
substring на docstring/комментарии ложно краснит честную документацию
(правило CLAUDE.md: source-inspection тесты матчатся по AST, не substring).
"""

from __future__ import annotations

import ast
import inspect
import json
import signal
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shutdown_handler import GracefulShutdownHandler, _SHUTDOWN_INFO_FILE  # noqa: E402


# ---------------------------------------------------------------------------
# Фейки (по образцу test_shutdown_handler.py / test_shutdown_handler_deep.py)
# ---------------------------------------------------------------------------


class FakeIPCServer:
    def __init__(self):
        self.stopped = False
        self.signal_requests = 0

    def stop(self):
        self.stopped = True

    def request_stop_from_signal(self):
        self.signal_requests += 1


class FakeRecorder:
    """Duck-type реального AudioRecorder: атрибут _is_recording, БЕЗ property/лока."""

    def __init__(self, is_recording=False):
        self._is_recording = is_recording


class FakeMeetingService:
    """Duck-type MeetingSessionService: атрибут _session, БЕЗ property/лока."""

    def __init__(self, session=None):
        self._session = session


class FakeService:
    """Минимальный сервис — ipc-контракт + recorder/_meeting_svc для R1-полей."""

    def __init__(self, *, recording_active=False, meeting_active=False):
        self._ipc_server = FakeIPCServer()
        self.recorder = FakeRecorder(is_recording=recording_active)
        self._meeting_svc = FakeMeetingService(session=object() if meeting_active else None)


# ===========================================================================
# (a) _signal_handler заполняет _signal_context без локов/I/O
# ===========================================================================


class TestSignalHandlerContextCapture(unittest.TestCase):
    def test_signal_handler_captures_recording_active_and_requests_stop(self):
        handler = GracefulShutdownHandler(data_dir=None)
        svc = FakeService(recording_active=True, meeting_active=False)
        handler._service = svc

        handler._signal_handler(signal.SIGTERM, None)

        self.assertIsNotNone(handler._signal_context)
        self.assertEqual(handler._signal_context["signal"], signal.SIGTERM)
        self.assertTrue(handler._signal_context["recording_active"])
        self.assertFalse(handler._signal_context["meeting_active"])
        self.assertEqual(svc._ipc_server.signal_requests, 1)

    def test_signal_handler_captures_meeting_active(self):
        handler = GracefulShutdownHandler(data_dir=None)
        svc = FakeService(recording_active=False, meeting_active=True)
        handler._service = svc

        handler._signal_handler(signal.SIGINT, None)

        self.assertEqual(handler._signal_context["signal"], signal.SIGINT)
        self.assertFalse(handler._signal_context["recording_active"])
        self.assertTrue(handler._signal_context["meeting_active"])

    def test_signal_handler_neither_active_by_default(self):
        handler = GracefulShutdownHandler(data_dir=None)
        svc = FakeService(recording_active=False, meeting_active=False)
        handler._service = svc

        handler._signal_handler(signal.SIGTERM, None)

        self.assertFalse(handler._signal_context["recording_active"])
        self.assertFalse(handler._signal_context["meeting_active"])

    def test_signal_handler_no_service_does_not_raise(self):
        """Отсутствующий service — снимок с дефолтами, IPC-запрос некому слать."""
        handler = GracefulShutdownHandler(data_dir=None)
        handler._service = None

        handler._signal_handler(signal.SIGTERM, None)  # не должно бросить

        self.assertIsNotNone(handler._signal_context)
        self.assertEqual(handler._signal_context["signal"], signal.SIGTERM)
        self.assertFalse(handler._signal_context["recording_active"])
        self.assertFalse(handler._signal_context["meeting_active"])

    def test_signal_handler_missing_recorder_and_meeting_attrs_defaults_false(self):
        """service без recorder/_meeting_svc (например ранний старт) — не бросает."""
        handler = GracefulShutdownHandler(data_dir=None)

        class BareService:
            pass

        handler._service = BareService()
        handler._signal_handler(signal.SIGINT, None)

        self.assertFalse(handler._signal_context["recording_active"])
        self.assertFalse(handler._signal_context["meeting_active"])


# ===========================================================================
# (b) bind + _signal_handler(SIGTERM) + shutdown() → новые поля в файле
# ===========================================================================


class TestShutdownInfoR1FieldsWithSignal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _read_info(self) -> dict:
        return json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text(encoding="utf-8"))

    def test_sigterm_then_shutdown_records_signal_and_recording(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = FakeService(recording_active=True, meeting_active=True)
        handler.bind(svc)

        handler._signal_handler(signal.SIGTERM, None)
        handler.shutdown()

        data = self._read_info()
        self.assertEqual(data["signal"], "SIGTERM")
        self.assertTrue(data["recording_active"])
        self.assertTrue(data["meeting_active"])
        self.assertGreaterEqual(data["uptime_sec"], 0)
        self.assertIn("pid", data)
        self.assertIsInstance(data["pid"], int)
        self.assertGreater(data["pid"], 0)

    def test_sigint_then_shutdown_records_signal_name_and_inactive_flags(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = FakeService(recording_active=False, meeting_active=False)
        handler.bind(svc)

        handler._signal_handler(signal.SIGINT, None)
        handler.shutdown()

        data = self._read_info()
        self.assertEqual(data["signal"], "SIGINT")
        self.assertFalse(data["recording_active"])
        self.assertFalse(data["meeting_active"])

    def test_existing_keys_still_present_additive_schema(self):
        """Новые поля аддитивны — старые читатели shutdown_info.json не ломаются."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = FakeService(recording_active=False, meeting_active=False)
        handler.bind(svc)

        handler._signal_handler(signal.SIGTERM, None)
        handler.shutdown()

        data = self._read_info()
        for key in ("last_shutdown_time", "clean", "elapsed_ms", "errors"):
            self.assertIn(key, data)
        for key in ("signal", "uptime_sec", "recording_active", "meeting_active", "pid"):
            self.assertIn(key, data)


# ===========================================================================
# (c) shutdown() без предшествующего сигнала → signal == null
# ===========================================================================


class TestShutdownInfoR1FieldsWithoutSignal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_shutdown_without_signal_has_null_signal_field(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = FakeService(recording_active=False, meeting_active=False)
        handler.bind(svc)

        handler.shutdown()  # никакого _signal_handler() перед этим — close()-путь

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text(encoding="utf-8"))
        self.assertIsNone(data["signal"])
        self.assertFalse(data["recording_active"])
        self.assertFalse(data["meeting_active"])
        self.assertGreaterEqual(data["uptime_sec"], 0)
        self.assertIn("pid", data)


# ===========================================================================
# (d) source-contract: тело _signal_handler остаётся signal-safe
# ===========================================================================


class TestSignalHandlerSourceContract(unittest.TestCase):
    """AST-разбор тел ВСЕХ методов, исполняемых как OS signal callback.

    R1 Task 8 амендмент (2026-07-24): изначально эти проверки матчили только
    ``_signal_handler``. Task 8 вынес чувствительное тело (снятие форензического
    контекста без локов/I/O/логов) в отдельный ``_capture_signal_context`` —
    ``_signal_handler`` теперь лишь ДЕЛЕГИРУЕТ ему вызов. AST-обход НЕ
    спускается в тело вызываемого метода через узел ``Call`` — старые тесты,
    матчившие только ``_signal_handler``, стали бы test-validates-the-hole
    (проверяли пустую обёртку, а не реальный чувствительный код). Обе точки
    входа реально исполняются как OS-колбэк: ``_signal_handler`` — legacy
    путь ``register()`` (``signal.signal(..., self._signal_handler)``) и
    прямые вызовы в unit-тестах; ``_capture_signal_context`` — прод-путь
    (локальный колбэк в ``main()``, service.py, зовёт его явно). Проверяем ОБЕ.

    Substring-на-весь-исходник (как в test_shutdown_handler_wired_in_main.py
    для функции внутри main()) здесь НЕ подходит: методы несут докстринги,
    объясняющие ИМЕННО запрет на recorder.is_recording/locks/логи — честная
    документация содержит эти слова как текст. AST матчит реальные узлы
    (With/Call/Attribute), докстринг — просто Constant-строка и не участвует
    в этих узлах.
    """

    _SIGNAL_CALLBACK_METHODS = ("_signal_handler", "_capture_signal_context")

    @classmethod
    def _method_ast_node(cls, method_name: str) -> ast.FunctionDef:
        source = textwrap.dedent(inspect.getsource(GracefulShutdownHandler))
        tree = ast.parse(source)
        class_node = tree.body[0]
        assert isinstance(class_node, ast.ClassDef)
        for node in ast.walk(class_node):
            if isinstance(node, ast.FunctionDef) and node.name == method_name:
                return node
        raise AssertionError(f"{method_name} не найден в AST GracefulShutdownHandler")

    def test_no_with_statements(self):
        """Никаких context manager'ов (локов и т.п.) внутри signal callback."""
        for method_name in self._SIGNAL_CALLBACK_METHODS:
            with self.subTest(method=method_name):
                node = self._method_ast_node(method_name)
                with_nodes = [n for n in ast.walk(node) if isinstance(n, (ast.With, ast.AsyncWith))]
                self.assertEqual(with_nodes, [], "signal-safe метод не должен содержать `with`")

    def test_no_is_recording_property_access(self):
        """Запрещён доступ к recorder.is_recording (property, берущее lock).

        Разрешено getattr(recorder, "_is_recording", ...) — приватный атрибут
        читается напрямую, это НЕ ast.Attribute-узел с attr == "is_recording"
        (строка "_is_recording" — просто аргумент-константа вызова getattr).
        """
        for method_name in self._SIGNAL_CALLBACK_METHODS:
            with self.subTest(method=method_name):
                node = self._method_ast_node(method_name)
                bad_attrs = [
                    n for n in ast.walk(node)
                    if isinstance(n, ast.Attribute) and n.attr == "is_recording"
                ]
                self.assertEqual(bad_attrs, [], "нельзя брать recorder.is_recording (лочит) в signal-safe коде")

    def test_no_logger_calls(self):
        """Никакого logging внутри signal callback (может блокировать на локе форматтера)."""
        for method_name in self._SIGNAL_CALLBACK_METHODS:
            with self.subTest(method=method_name):
                node = self._method_ast_node(method_name)
                logger_calls = [
                    n for n in ast.walk(node)
                    if isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name)
                    and n.value.id == "logger"
                ]
                self.assertEqual(logger_calls, [], "signal-safe метод не должен звать logger.*")

    def test_no_file_io_or_json_calls(self):
        """Никакого I/O (open()) и сериализации (json.*) внутри signal callback."""
        for method_name in self._SIGNAL_CALLBACK_METHODS:
            with self.subTest(method=method_name):
                node = self._method_ast_node(method_name)
                for n in ast.walk(node):
                    if isinstance(n, ast.Call):
                        func = n.func
                        if isinstance(func, ast.Name) and func.id == "open":
                            self.fail(f"{method_name}: signal-safe метод не должен звать open()")
                        if (
                            isinstance(func, ast.Attribute)
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "json"
                        ):
                            self.fail(f"{method_name}: signal-safe метод не должен звать json.*")


if __name__ == "__main__":
    unittest.main()
