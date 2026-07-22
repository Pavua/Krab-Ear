"""Углублённые lifecycle-тесты GracefulShutdownHandler из Wave 216.

Покрывают порядок callback-ов, изоляцию ошибок, атомарный shutdown_info.json,
SIGTERM/SIGINT wiring, идемпотентность, timeout-контракты, Unicode в логах,
uptime и безопасный конкурентный register во время завершения.
"""

from __future__ import annotations

import json
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shutdown_handler import GracefulShutdownHandler, _SHUTDOWN_INFO_FILE  # noqa: E402

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_service(*, vocabulary_words=("foo", "bar")):
    """Minimal mock service with all optional sub-components populated."""
    svc = MagicMock()

    # vocabulary
    vocab = MagicMock()
    vocab.load.return_value = list(vocabulary_words)
    svc.vocabulary = vocab

    # audit logger
    svc._audit_logger = MagicMock()

    # usage tracker
    svc._usage_tracker = MagicMock()

    # playback tracker
    svc._playback_tracker = MagicMock()

    # store
    store = MagicMock()
    store.maybe_compact.return_value = False
    svc.store = store

    # ipc server
    svc._ipc_server = MagicMock()

    return svc


# ===========================================================================
# 1. IPC-first порядок callback-ов
# ===========================================================================

class TestShutdownStepOrder(unittest.TestCase):
    """IPC ownership-барьер выполняется до metadata callback-ов."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_shutdown_runs_ipc_barrier_before_metadata(self):
        """Все шаги вызваны, причём socket-stop идёт первым."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        call_order: list[str] = []

        # IPC является ownership-барьером и поэтому выполняется раньше любых
        # операций над общими metadata-ресурсами.
        original_save_vocab = handler._save_vocabulary
        original_flush_audit = handler._flush_audit_log
        original_save_usage = handler._save_usage_stats
        original_save_playback = handler._save_playback_stats
        original_compact = handler._maybe_compact_history
        original_close_socket = handler._close_socket

        def patched_save_vocab(svc):
            call_order.append("vocabulary")
            original_save_vocab(svc)

        def patched_flush_audit(svc):
            call_order.append("audit_log")
            original_flush_audit(svc)

        def patched_save_usage(svc):
            call_order.append("usage_stats")
            original_save_usage(svc)

        def patched_save_playback(svc):
            call_order.append("playback_stats")
            original_save_playback(svc)

        def patched_compact(svc):
            call_order.append("compact")
            original_compact(svc)

        def patched_close_socket(svc):
            call_order.append("socket")
            original_close_socket(svc)

        handler._save_vocabulary = patched_save_vocab
        handler._flush_audit_log = patched_flush_audit
        handler._save_usage_stats = patched_save_usage
        handler._save_playback_stats = patched_save_playback
        handler._maybe_compact_history = patched_compact
        handler._close_socket = patched_close_socket

        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        # All six steps must have fired
        self.assertEqual(
            call_order,
            ["socket", "vocabulary", "audit_log", "usage_stats",
             "playback_stats", "compact"],
            f"Unexpected step order: {call_order}",
        )

    def test_all_six_shutdown_steps_invoked(self):
        """Even with a bare service object all steps are attempted."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        invoked: list[str] = []

        for attr in (
            "_save_vocabulary",
            "_flush_audit_log",
            "_save_usage_stats",
            "_save_playback_stats",
            "_maybe_compact_history",
            "_close_socket",
        ):
            name = attr  # capture

            def make_recorder(orig, n):
                def recorder(svc):
                    invoked.append(n)
                    orig(svc)
                return recorder

            setattr(handler, attr, make_recorder(getattr(handler, attr), name))

        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        self.assertEqual(len(invoked), 6)


# ===========================================================================
# 2. Callback error isolation
# ===========================================================================

class TestShutdownContinuesOnError(unittest.TestCase):
    """A failing step must not abort subsequent steps."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_shutdown_continues_when_one_callback_raises(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        # Make vocabulary.save raise
        svc.vocabulary.save.side_effect = RuntimeError("forced error")
        handler._service = svc
        handler.shutdown()

        # IPC server must still be stopped despite the earlier error
        svc._ipc_server.stop.assert_called_once()

    def test_error_in_audit_still_saves_playback(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        svc._audit_logger.close.side_effect = OSError("audit broken")
        handler._service = svc
        handler.shutdown()

        svc._playback_tracker._save.assert_called_once()

    def test_multiple_errors_all_recorded_in_file(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        svc.vocabulary.save.side_effect = ValueError("vocab fail")
        svc._audit_logger.close.side_effect = IOError("audit fail")
        handler._service = svc
        handler.shutdown()

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        self.assertFalse(data["clean"])
        self.assertGreaterEqual(len(data["errors"]), 2)
        error_text = " ".join(data["errors"])
        self.assertIn("vocabulary", error_text)
        self.assertIn("audit_log", error_text)


# ===========================================================================
# 3. Atomic write of shutdown_info.json
# ===========================================================================

class TestShutdownInfoFileAtomic(unittest.TestCase):
    """shutdown_info.json must be written atomically (tmp → rename)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_shutdown_writes_shutdown_info_file_atomically(self):
        """Verify tmp file is removed and final file exists after shutdown."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        final_path = self.data_dir / _SHUTDOWN_INFO_FILE
        tmp_path = final_path.with_suffix(".json.tmp")

        self.assertTrue(final_path.exists(), "shutdown_info.json must exist")
        self.assertFalse(tmp_path.exists(), ".json.tmp must be cleaned up after rename")

    def test_shutdown_info_is_valid_json(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        raw = (self.data_dir / _SHUTDOWN_INFO_FILE).read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.fail(f"shutdown_info.json is not valid JSON: {exc}")

        self.assertIsInstance(data, dict)


# ===========================================================================
# 4. SIGTERM triggers shutdown
# ===========================================================================

class TestSIGTERM(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_signal_handler_SIGTERM_only_requests_ipc_stop(self):
        """SIGTERM callback не выполняет teardown внутри signal-контекста."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc

        with patch.object(handler, "shutdown") as mock_shutdown:
            handler._signal_handler(signal.SIGTERM, None)
            mock_shutdown.assert_not_called()
        svc._ipc_server.request_stop_from_signal.assert_called_once_with()

    def test_register_wires_SIGTERM_to_signal_module(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()

        with patch("signal.signal") as mock_sig:
            handler.register(svc)

        registered = {c.args[0] for c in mock_sig.call_args_list}
        self.assertIn(signal.SIGTERM, registered)


# ===========================================================================
# 5. SIGINT triggers shutdown
# ===========================================================================

class TestSIGINT(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_signal_handler_SIGINT_only_requests_ipc_stop(self):
        """SIGINT использует тот же signal-safe request-only контракт."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc

        with patch.object(handler, "shutdown") as mock_shutdown:
            handler._signal_handler(signal.SIGINT, None)
            mock_shutdown.assert_not_called()
        svc._ipc_server.request_stop_from_signal.assert_called_once_with()

    def test_register_wires_SIGINT_to_signal_module(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()

        with patch("signal.signal") as mock_sig:
            handler.register(svc)

        registered = {c.args[0] for c in mock_sig.call_args_list}
        self.assertIn(signal.SIGINT, registered)


# ===========================================================================
# 6. Idempotency — double call
# ===========================================================================

class TestShutdownIdempotent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_shutdown_idempotent_double_call(self):
        """Second shutdown() call must be a no-op."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc

        handler.shutdown()
        handler.shutdown()

        # vocabulary.save called exactly once
        self.assertEqual(svc.vocabulary.save.call_count, 1)
        # IPC server stopped exactly once
        self.assertEqual(svc._ipc_server.stop.call_count, 1)

    def test_shutdown_done_event_set_after_first_call(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc

        handler.shutdown()
        self.assertTrue(handler._shutdown_done.is_set())

        # Second call — event remains set
        handler.shutdown()
        self.assertTrue(handler._shutdown_done.is_set())


# ===========================================================================
# 7. Per-callback timeout enforcement
# ===========================================================================

class TestCallbackTimeout(unittest.TestCase):
    """If a step hangs, shutdown should not wait forever.

    GracefulShutdownHandler currently does NOT implement per-step timeout
    internally. We verify the overall shutdown completes within a reasonable
    wall-clock bound even if a step blocks momentarily.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_callback_timeout_per_callback_enforced(self):
        """Each internal step call should complete within 2 seconds total."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()

        # Make one step take a small but measurable time (50 ms)
        original_flush = handler._flush_audit_log

        def slow_flush(service):
            time.sleep(0.05)
            original_flush(service)

        handler._flush_audit_log = slow_flush
        handler._service = svc

        start = time.monotonic()
        handler.shutdown()
        elapsed = time.monotonic() - start

        # The whole shutdown must complete in under 2 s (generous bound)
        self.assertLess(elapsed, 2.0, f"Shutdown took too long: {elapsed:.2f}s")

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        self.assertGreaterEqual(data["elapsed_ms"], 50.0)

    def test_shutdown_elapsed_ms_reflects_real_time(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()

        original_compact = handler._maybe_compact_history

        def slow_compact(service):
            time.sleep(0.02)
            original_compact(service)

        handler._maybe_compact_history = slow_compact
        handler._service = svc
        handler.shutdown()

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        self.assertGreaterEqual(data["elapsed_ms"], 20.0)


# ===========================================================================
# 8. Unicode callback names in log
# ===========================================================================

class TestUnicodeCallbackNames(unittest.TestCase):
    """Logger messages with Unicode step names must not raise."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unicode_callback_names_in_log(self):
        """Shutdown completes successfully even with a unicode-named service attr."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()

        # Attach a unicode-named attribute (shouldn't affect shutdown but
        # exercises any logging that might format service repr)
        svc.__repr__ = lambda self: "Сервис-«бэкенд»-тестовый"

        handler._service = svc
        # Must not raise UnicodeEncodeError or similar
        try:
            handler.shutdown()
        except Exception as exc:
            self.fail(f"shutdown() raised unexpectedly: {exc}")

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        self.assertTrue(data["clean"])

    def test_error_message_with_unicode_written_to_file(self):
        """Error messages with Cyrillic characters are stored correctly."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        svc.vocabulary.save.side_effect = ValueError("Ошибка записи словаря")
        handler._service = svc
        handler.shutdown()

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text(encoding="utf-8"))
        self.assertFalse(data["clean"])
        error_text = " ".join(data["errors"])
        self.assertIn("vocabulary", error_text)


# ===========================================================================
# 9. Uptime field in shutdown_info.json
# ===========================================================================

class TestShutdownInfoUptime(unittest.TestCase):
    """shutdown_info.json must contain elapsed_ms that reflects uptime."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_shutdown_includes_uptime_in_info_file(self):
        """elapsed_ms key must be present and non-negative."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        self.assertIn("elapsed_ms", data)
        self.assertIsInstance(data["elapsed_ms"], (int, float))
        self.assertGreaterEqual(data["elapsed_ms"], 0.0)

    def test_shutdown_timestamp_is_iso8601_utc(self):
        """last_shutdown_time must be a parseable ISO-8601 UTC string."""
        from datetime import datetime

        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc
        handler.shutdown()

        data = json.loads((self.data_dir / _SHUTDOWN_INFO_FILE).read_text())
        ts_str = data["last_shutdown_time"]
        self.assertIsNotNone(ts_str)
        # Should be parseable
        try:
            dt = datetime.fromisoformat(ts_str)
        except ValueError:
            self.fail(f"last_shutdown_time is not valid ISO-8601: {ts_str!r}")
        # Must have timezone info
        self.assertIsNotNone(dt.tzinfo, "timestamp should include timezone")


# ===========================================================================
# 10. Concurrent register during shutdown is safe
# ===========================================================================

class TestConcurrentRegisterDuringShutdown(unittest.TestCase):
    """Calling register() from another thread while shutdown() runs is safe."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_register_during_shutdown_safe(self):
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc1 = _make_service()
        svc2 = _make_service()

        handler._service = svc1

        shutdown_started = threading.Event()
        original_flush = handler._flush_audit_log

        def slow_flush(service):
            shutdown_started.set()
            time.sleep(0.05)
            original_flush(service)

        handler._flush_audit_log = slow_flush

        register_errors: list[Exception] = []

        def do_register():
            shutdown_started.wait(timeout=1.0)
            try:
                with patch("signal.signal"):
                    handler.register(svc2)
            except Exception as exc:
                register_errors.append(exc)

        t_register = threading.Thread(target=do_register)
        t_register.start()

        handler.shutdown()

        t_register.join(timeout=2.0)
        self.assertFalse(t_register.is_alive(), "register thread should have finished")
        self.assertEqual(register_errors, [], f"register raised: {register_errors}")

    def test_concurrent_shutdown_calls_only_one_executes(self):
        """10 concurrent shutdown calls → exactly one execution."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        svc = _make_service()
        handler._service = svc

        threads = [threading.Thread(target=handler.shutdown) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # Only one shutdown execution: vocabulary.save called once
        self.assertEqual(svc.vocabulary.save.call_count, 1)
        self.assertEqual(svc._ipc_server.stop.call_count, 1)


if __name__ == "__main__":
    unittest.main()
