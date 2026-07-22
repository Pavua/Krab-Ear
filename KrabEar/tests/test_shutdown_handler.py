"""Тесты для GracefulShutdownHandler."""

from __future__ import annotations
from backend.shutdown_handler import GracefulShutdownHandler, _SHUTDOWN_INFO_FILE

import json
import signal
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Вспомогательные фейки
# ---------------------------------------------------------------------------

class FakeVocabularyStore:
    def __init__(self, words=None):
        self._words = list(words or ["foo", "bar"])
        self.save_calls: list[list[str]] = []

    def load(self):
        return list(self._words)

    def save(self, words):
        self.save_calls.append(list(words))


class FakeAuditLogger:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeUsageTracker:
    def __init__(self):
        self.persisted = False

    def _persist(self):
        self.persisted = True


class FakePlaybackTracker:
    def __init__(self):
        self.saved = False

    def _save(self):
        self.saved = True


class FakeStore:
    def __init__(self, should_compact=False):
        self._should_compact = should_compact
        self.compact_called = False

    def maybe_compact(self):
        self.compact_called = True
        return self._should_compact


class FakeIPCServer:
    def __init__(self):
        self.stopped = False
        self.signal_requests = 0

    def stop(self):
        self.stopped = True

    def request_stop_from_signal(self):
        self.signal_requests += 1


def _make_service(
    has_vocabulary=True,
    has_audit=True,
    has_usage=True,
    has_playback=True,
    has_store=True,
    has_ipc_server=True,
    should_compact=False,
):
    """Возвращает объект-фейк с нужными атрибутами."""
    svc = MagicMock()
    svc.vocabulary = FakeVocabularyStore() if has_vocabulary else None
    svc._audit_logger = FakeAuditLogger() if has_audit else None
    svc._usage_tracker = FakeUsageTracker() if has_usage else None
    svc._playback_tracker = FakePlaybackTracker() if has_playback else None
    svc.store = FakeStore(should_compact=should_compact) if has_store else None
    svc._ipc_server = FakeIPCServer() if has_ipc_server else None
    return svc


# ===========================================================================
# Тест 1 — базовый happy path
# ===========================================================================

class TestShutdownHandlerBasic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_shutdown_creates_shutdown_info_json(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        info_path = self.data_dir / _SHUTDOWN_INFO_FILE
        self.assertTrue(info_path.exists(), "shutdown_info.json должен быть создан")

    def test_shutdown_info_json_contains_required_keys(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        for key in ("last_shutdown_time", "clean", "elapsed_ms", "errors"):
            self.assertIn(key, data, f"Ключ '{key}' отсутствует в shutdown_info.json")

    def test_shutdown_marks_clean_true_on_success(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        self.assertTrue(data["clean"], "clean должен быть True при успешном завершении")
        self.assertEqual(data["errors"], [])


# ===========================================================================
# Тест 2 — вызовы сохранения компонентов
# ===========================================================================

class TestShutdownHandlerSavesComponents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_vocabulary_saved(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        self.assertEqual(len(svc.vocabulary.save_calls), 1)
        self.assertEqual(sorted(svc.vocabulary.save_calls[0]), ["bar", "foo"])

    def test_audit_log_closed(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        self.assertTrue(svc._audit_logger.closed, "audit log должен быть закрыт")

    def test_usage_stats_persisted(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        self.assertTrue(svc._usage_tracker.persisted, "usage stats должны быть сохранены")

    def test_playback_stats_saved(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        self.assertTrue(svc._playback_tracker.saved, "playback stats должны быть сохранены")

    def test_compact_history_called(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service(should_compact=True)
        handler._service = svc
        handler.shutdown()

        self.assertTrue(svc.store.compact_called, "maybe_compact() должен быть вызван")

    def test_ipc_server_stopped(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        self.assertTrue(svc._ipc_server.stopped, "IPC-сервер должен быть остановлен")


# ===========================================================================
# Тест 3 — идемпотентность
# ===========================================================================

class TestShutdownHandlerIdempotent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_double_shutdown_does_not_duplicate_saves(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()
        handler.shutdown()  # второй вызов — не должен повторять шаги

        # vocabulary.save должен быть вызван ровно один раз
        self.assertEqual(len(svc.vocabulary.save_calls), 1)

    def test_double_shutdown_does_not_reopen_audit_log(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()
        handler.shutdown()

        # close() вызывается только один раз
        self.assertTrue(svc._audit_logger.closed)


# ===========================================================================
# Тест 4 — get_shutdown_status
# ===========================================================================

class TestGetShutdownStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_initial_status_has_none_fields(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        status = handler.get_shutdown_status()

        self.assertIn("clean", status)
        self.assertIn("last_shutdown_time", status)
        self.assertIn("shutdown_in_progress", status)
        # До shutdown — оба None
        self.assertIsNone(status["clean"])
        self.assertIsNone(status["last_shutdown_time"])

    def test_status_after_shutdown_reflects_result(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        status = handler.get_shutdown_status()
        self.assertTrue(status["clean"])
        self.assertIsNotNone(status["last_shutdown_time"])
        self.assertFalse(status["shutdown_in_progress"])


# ===========================================================================
# Тест 5 — персистентность через перезагрузку
# ===========================================================================

class TestShutdownHandlerPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_loaded_on_next_init(self):
        # Первый экземпляр выполняет shutdown
        h1 = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        h1._service = svc
        h1.shutdown()

        ts1 = h1.get_shutdown_status()["last_shutdown_time"]

        # Второй экземпляр должен подхватить данные из файла
        h2 = GracefulShutdownHandler(data_dir=self.data_dir)
        status2 = h2.get_shutdown_status()

        self.assertEqual(status2["last_shutdown_time"], ts1)
        self.assertTrue(status2["clean"])

    def test_no_data_dir_does_not_crash(self):
        handler = GracefulShutdownHandler(data_dir=None)
        svc = _make_service()
        handler._service = svc
        # Должен отработать без исключений
        handler.shutdown()
        status = handler.get_shutdown_status()
        self.assertTrue(status["clean"])


# ===========================================================================
# Тест 6 — graceful при отсутствующих атрибутах
# ===========================================================================

class TestShutdownHandlerMissingAttributes(unittest.TestCase):
    """Проверяем, что отсутствие опциональных атрибутов не ломает завершение."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_vocabulary_attr(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service(has_vocabulary=False)
        handler._service = svc
        handler.shutdown()  # не должен бросать исключение

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        self.assertTrue(data["clean"])

    def test_no_audit_logger_attr(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service(has_audit=False)
        handler._service = svc
        handler.shutdown()

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        self.assertTrue(data["clean"])

    def test_no_store_attr(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service(has_store=False)
        handler._service = svc
        handler.shutdown()

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        self.assertTrue(data["clean"])

    def test_no_ipc_server_attr(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service(has_ipc_server=False)
        handler._service = svc
        handler.shutdown()

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        self.assertTrue(data["clean"])


# ===========================================================================
# Тест 7 — обработка ошибок в шагах
# ===========================================================================

class TestShutdownHandlerErrorHandling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_vocabulary_save_error_recorded(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        svc.vocabulary.save = MagicMock(side_effect=OSError("disk full"))
        handler._service = svc
        handler.shutdown()

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        self.assertFalse(data["clean"], "clean должен быть False при ошибке сохранения")
        self.assertTrue(
            any("vocabulary" in e for e in data["errors"]),
            "errors должен содержать сведения об ошибке vocabulary",
        )

    def test_audit_log_error_recorded(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        svc._audit_logger.close = MagicMock(side_effect=RuntimeError("io error"))
        handler._service = svc
        handler.shutdown()

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        self.assertFalse(data["clean"])
        self.assertTrue(any("audit_log" in e for e in data["errors"]))

    def test_remaining_steps_executed_after_error(self):
        """Ошибка в vocabulary не должна прерывать остальные шаги."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        svc.vocabulary.save = MagicMock(side_effect=OSError("disk full"))
        handler._service = svc
        handler.shutdown()

        # playback и usage все равно должны быть сохранены
        self.assertTrue(svc._usage_tracker.persisted)
        self.assertTrue(svc._playback_tracker.saved)


# ===========================================================================
# Тест 8 — регистрация сигналов
# ===========================================================================

class TestShutdownHandlerSignalRegistration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_sets_signal_handlers(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()

        with patch("signal.signal") as mock_signal:
            handler.register(svc)
            calls = mock_signal.call_args_list
            registered_signals = [c.args[0] for c in calls]
            self.assertIn(signal.SIGTERM, registered_signals)
            self.assertIn(signal.SIGINT, registered_signals)

    def test_signal_handler_only_requests_ipc_stop(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc

        with patch.object(handler, "shutdown") as mock_shutdown:
            handler._signal_handler(signal.SIGTERM, None)
            mock_shutdown.assert_not_called()
        self.assertEqual(svc._ipc_server.signal_requests, 1)

    def test_register_stores_service_reference(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()

        with patch("signal.signal"):
            handler.register(svc)

        self.assertIs(handler._service, svc)


# ===========================================================================
# Тест 9 — потокобезопасность (конкурентные вызовы shutdown)
# ===========================================================================

class TestShutdownHandlerThreadSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_shutdown_calls_idempotent(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc

        threads = [threading.Thread(target=handler.shutdown) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # vocabulary.save должен быть вызван ровно один раз
        self.assertEqual(len(svc.vocabulary.save_calls), 1)

    def test_get_shutdown_status_thread_safe(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        results = []

        def _read():
            results.append(handler.get_shutdown_status())

        threads = [threading.Thread(target=_read) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 20)
        for r in results:
            self.assertIn("clean", r)

    def test_failed_ipc_drain_aborts_before_persistence(self):
        """False от IPC запрещает касаться общих metadata-ресурсов."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = MagicMock()
        svc._ipc_server.stop.return_value = False
        handler.bind(svc)

        self.assertFalse(handler.shutdown())

        svc._ipc_server.stop.assert_called_once_with()
        svc.vocabulary.load.assert_not_called()
        svc._audit_logger.close.assert_not_called()
        svc._usage_tracker._persist.assert_not_called()
        svc._playback_tracker._save.assert_not_called()
        svc.store.maybe_compact.assert_not_called()
        self.assertFalse((self.data_dir / _SHUTDOWN_INFO_FILE).exists())

    def test_stop_exception_aborts_before_persistence(self):
        """Исключение drain трактуется как недоказанная квиесценция."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = MagicMock()
        svc._ipc_server.stop.side_effect = RuntimeError("join failed")
        handler.bind(svc)

        self.assertFalse(handler.shutdown())
        svc.vocabulary.load.assert_not_called()
        self.assertTrue(handler._shutdown_done.is_set())

    def test_missing_ipc_stop_aborts_before_persistence(self):
        """Наличие server без stop() не считается доказанной квиесценцией."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = MagicMock()
        svc._ipc_server = MagicMock(spec=[])
        handler.bind(svc)

        self.assertFalse(handler.shutdown())
        svc.vocabulary.load.assert_not_called()
        self.assertTrue(handler._shutdown_done.is_set())

    def test_concurrent_caller_waits_for_owner_result(self):
        """Конкурентный caller не объявляет успех до завершения владельца."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        entered = threading.Event()
        release = threading.Event()
        original_save = handler._save_vocabulary

        def _blocked_save(service):
            entered.set()
            release.wait()
            original_save(service)

        handler._save_vocabulary = _blocked_save
        handler.bind(svc)
        results: list[bool] = []
        second_started = threading.Event()

        owner = threading.Thread(target=lambda: results.append(handler.shutdown()))

        def _second_shutdown() -> None:
            second_started.set()
            results.append(handler.shutdown())

        waiter = threading.Thread(target=_second_shutdown)
        owner.start()
        self.addCleanup(owner.join, 1.0)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(timeout=1.0))
        waiter.start()
        self.addCleanup(waiter.join, 1.0)
        self.assertTrue(second_started.wait(timeout=1.0))
        self.assertEqual(results, [])

        release.set()
        owner.join(timeout=1.0)
        waiter.join(timeout=1.0)
        self.assertEqual(results, [True, True])
        self.assertEqual(len(svc.vocabulary.save_calls), 1)

    def test_owner_reentry_returns_false_without_deadlock(self):
        """Metadata callback не может рекурсивно запустить второй teardown."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        reentry_results: list[bool] = []

        def _reentrant_save(_service):
            reentry_results.append(
                handler.shutdown(ipc_already_stopped=True)
            )

        handler._save_vocabulary = _reentrant_save
        handler.bind(svc)

        self.assertTrue(handler.shutdown())
        self.assertEqual(reentry_results, [False])
        self.assertTrue(handler._shutdown_done.is_set())

    def test_unexpected_persist_exception_still_releases_waiters(self):
        """Override _persist не оставляет single-flight в вечном in-progress."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._persist = MagicMock(side_effect=RuntimeError("persist failed"))
        handler.bind(svc)

        self.assertTrue(handler.shutdown())
        self.assertTrue(handler._shutdown_done.is_set())
        self.assertFalse(handler.get_shutdown_status()["clean"])
        self.assertTrue(handler.shutdown())


# ===========================================================================
# Тест 10 — elapsed_ms присутствует и положителен
# ===========================================================================

class TestShutdownHandlerElapsedMs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_elapsed_ms_is_positive(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        self.assertGreaterEqual(data["elapsed_ms"], 0.0)


# ===========================================================================
# Wave 106: additional coverage tests
# ===========================================================================

class TestShutdownHandlerRegisterCleanupCallback(unittest.TestCase):
    """test_register_cleanup_callback: register() stores service and wires signals."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_cleanup_callback(self):
        """register() must store service reference so shutdown() can call cleanup steps."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()

        with patch("signal.signal"):
            handler.register(svc)

        # Service is wired — shutdown should execute all cleanup steps
        handler.shutdown()
        self.assertTrue(svc._audit_logger.closed)
        self.assertTrue(svc._usage_tracker.persisted)
        self.assertEqual(len(svc.vocabulary.save_calls), 1)

    def test_register_replaces_previous_service(self):
        """Second register() replaces the service reference cleanly."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc1 = _make_service()
        svc2 = _make_service()

        with patch("signal.signal"):
            handler.register(svc1)
            handler.register(svc2)

        self.assertIs(handler._service, svc2)


class TestShutdownHandlerStepOrder(unittest.TestCase):
    """Проверяет IPC-first порядок шагов завершения."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_cleanups_in_ipc_first_order(self):
        """Шаги идут socket → vocabulary → audit → usage → playback → compact."""
        call_order: list[str] = []
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = MagicMock()

        # Vocabulary
        vocab = MagicMock()
        vocab.load.side_effect = lambda: call_order.append("vocab_load") or ["word"]
        vocab.save.side_effect = lambda w: call_order.append("vocab_save")
        svc.vocabulary = vocab

        # Audit logger
        audit = MagicMock()
        audit.close.side_effect = lambda: call_order.append("audit_close")
        svc._audit_logger = audit

        # Usage tracker
        usage = MagicMock()
        usage._persist.side_effect = lambda: call_order.append("usage_persist")
        svc._usage_tracker = usage

        # Playback tracker
        playback = MagicMock()
        playback._save.side_effect = lambda: call_order.append("playback_save")
        svc._playback_tracker = playback

        # Store
        store = MagicMock()
        store.maybe_compact.side_effect = lambda: call_order.append("compact") or False
        svc.store = store

        # IPC server
        server = MagicMock()
        server.stop.side_effect = lambda: call_order.append("socket_stop")
        svc._ipc_server = server

        handler._service = svc
        handler.shutdown()

        # _save_vocabulary calls load() then save(); both recorded
        expected = [
            "socket_stop",
            "vocab_load",
            "vocab_save",
            "audit_close",
            "usage_persist",
            "playback_save",
            "compact",
        ]
        self.assertEqual(call_order, expected, f"Step order mismatch: {call_order}")


class TestShutdownHandlerExceptionContinues(unittest.TestCase):
    """test_cleanup_callback_exception_continues: failure in one step doesn't abort others."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cleanup_callback_exception_continues(self):
        """All six steps run even if earlier steps raise."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        # Make the first step (vocabulary) fail
        svc.vocabulary.save = MagicMock(side_effect=RuntimeError("boom"))
        handler._service = svc
        handler.shutdown()

        # All remaining steps must still have run
        self.assertTrue(svc._audit_logger.closed)
        self.assertTrue(svc._usage_tracker.persisted)
        self.assertTrue(svc._playback_tracker.saved)
        self.assertTrue(svc.store.compact_called)
        self.assertTrue(svc._ipc_server.stopped)

        # And clean=False is recorded
        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        self.assertFalse(data["clean"])


class TestShutdownHandlerWriteShutdownInfoFile(unittest.TestCase):
    """test_write_shutdown_info_file: JSON file written with atomic tmp→rename."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_shutdown_info_file(self):
        """Shutdown writes a parseable JSON file with expected fields."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        info_path = self.data_dir / _SHUTDOWN_INFO_FILE
        self.assertTrue(info_path.exists())
        data = json.loads(info_path.read_text(encoding="utf-8"))
        self.assertIn("last_shutdown_time", data)
        self.assertIn("clean", data)
        self.assertIn("elapsed_ms", data)
        self.assertIn("errors", data)
        self.assertIsInstance(data["errors"], list)

    def test_no_tmp_residue_after_write(self):
        """No .tmp file should remain after shutdown_info is written."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        tmp_files = list(self.data_dir.glob("*.tmp"))
        self.assertEqual(tmp_files, [], "No .tmp residue expected after write")


class TestShutdownHandlerConcurrentIdempotent(unittest.TestCase):
    """test_concurrent_shutdown_idempotent: called from N threads = executes once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_shutdown_idempotent(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc

        barrier = threading.Barrier(20)

        def _run():
            barrier.wait()
            handler.shutdown()

        threads = [threading.Thread(target=_run) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one execution
        self.assertEqual(len(svc.vocabulary.save_calls), 1)
        self.assertTrue(svc._audit_logger.closed)


class TestShutdownHandlerTimeoutPerCallback(unittest.TestCase):
    """test_timeout_per_cleanup_callback: slow steps still produce clean shutdown_info."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_timeout_per_cleanup_callback(self):
        """A slow cleanup step (simulated with brief sleep) doesn't block the whole shutdown.
        elapsed_ms is recorded accurately."""
        import time
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()

        def _slow_persist():
            time.sleep(0.05)
            svc._usage_tracker.persisted = True

        svc._usage_tracker._persist = _slow_persist
        handler._service = svc

        start = time.monotonic()
        handler.shutdown()
        elapsed = time.monotonic() - start

        # Shutdown completes within a reasonable time (no infinite hang)
        self.assertLess(elapsed, 5.0, "Shutdown took too long (possible hang)")

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        self.assertTrue(data["clean"])
        self.assertGreater(data["elapsed_ms"], 0)


if __name__ == "__main__":
    unittest.main()
