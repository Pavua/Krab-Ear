"""Тесты lifecycle-контракта realtime_partial и таймаута stop().

W1323: Validates:
  1. stop() default timeout is 30s (not 4s).
  2. _stop_requested flag is set True on stop().
  3. Warning logged when thread still alive after join timeout.
  4. Живой self._thread сохраняется после join timeout и блокирует restart.
  5. Разблокированный после timeout worker не публикует устаревший partial.
"""
import ast
import logging
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.realtime_partial import RealtimePartialTranscriber  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_transcriber_with_preview():
    t = MagicMock()
    t.transcribe_preview = MagicMock(return_value={"text": ""})
    return t


def _make_rpt(interval_sec: float = 0.05) -> RealtimePartialTranscriber:
    transcriber = _make_transcriber_with_preview()
    recorder = MagicMock()
    recorder.snapshot_audio.return_value = (MagicMock(size=0), 0.0)
    event_bus = MagicMock()
    return RealtimePartialTranscriber(
        transcriber=transcriber,
        recorder=recorder,
        event_bus=event_bus,
        interval_sec=interval_sec,
    )


# ---------------------------------------------------------------------------
# AST check: default timeout value in stop() signature
# ---------------------------------------------------------------------------

class TestStopTimeoutIncreasedTo30sAST(unittest.TestCase):
    """F1 AST-level: stop() default parameter is 30.0, not 4.0."""

    def _get_stop_default_timeout(self):
        src_path = os.path.join(_PROJECT_ROOT, "backend", "realtime_partial.py")
        with open(src_path) as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "RealtimePartialTranscriber":
                for item in ast.walk(node):
                    if isinstance(item, ast.FunctionDef) and item.name == "stop":
                        # defaults correspond to the last N args
                        for default in item.args.defaults:
                            if isinstance(default, ast.Constant) and isinstance(default.value, (int, float)):
                                return float(default.value)
        return None

    def test_stop_timeout_increased_to_30s(self):
        """stop() default timeout_sec must be 30.0."""
        val = self._get_stop_default_timeout()
        self.assertIsNotNone(val, "Could not find stop() default timeout parameter")
        self.assertEqual(
            val, 30.0,
            f"Expected stop() default timeout=30.0, got {val}",
        )


# ---------------------------------------------------------------------------
# Runtime behaviour tests
# ---------------------------------------------------------------------------

class TestStopRequestedFlagSet(unittest.TestCase):
    """stop() sets _stop_requested = True."""

    def test_stop_requested_flag_set(self):
        rpt = _make_rpt()
        self.assertFalse(rpt._stop_requested, "_stop_requested should be False before stop()")
        rpt.stop()
        self.assertTrue(rpt._stop_requested, "_stop_requested must be True after stop()")

    def test_stop_requested_flag_reset_on_start(self):
        """start() resets _stop_requested so a re-start works."""
        rpt = _make_rpt()
        self.addCleanup(rpt.stop)
        rpt.stop()
        self.assertTrue(rpt._stop_requested)
        # Now start again — flag should be cleared.
        # start() won't actually launch because transcriber.transcribe_preview is callable.
        # We need to patch is_running so start() proceeds past the guard.
        with patch.object(type(rpt), "is_running", new_callable=lambda: property(lambda self: False)):
            rpt.start("sess-reset")
        self.assertFalse(rpt._stop_requested, "_stop_requested must be reset by start()")

    def test_flag_initialized_false(self):
        rpt = _make_rpt()
        self.assertFalse(rpt._stop_requested)


class TestWarningLoggedIfThreadAliveAfterJoin(unittest.TestCase):
    """Warning is logged when thread is still alive after join timeout."""

    def test_warning_logged_if_thread_alive_after_join(self):
        rpt = _make_rpt()

        # Inject a mock thread that is always alive (simulates mlx_lock held).
        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = True  # never finishes
        rpt._thread = mock_thread
        rpt._session_id = "sess-warn"

        with self.assertLogs("KrabEar.RealtimePartial", level="WARNING") as log_ctx:
            rpt.stop(timeout_sec=0.01)  # tiny timeout to keep test fast

        messages = "\n".join(log_ctx.output)
        self.assertIn(
            "realtime_partial worker не завершился",
            messages,
            "Ожидалось предупреждение о незавершённом worker",
        )
        self.assertIn(
            "устаревший partial",
            messages,
            "Предупреждение должно объяснять риск устаревшего partial",
        )

    def test_no_warning_when_thread_stops_in_time(self):
        """No warning when thread terminates before timeout."""
        rpt = _make_rpt()

        mock_thread = MagicMock(spec=threading.Thread)
        # First call: alive (before join), second call: dead (after join)
        mock_thread.is_alive.side_effect = [True, False]
        rpt._thread = mock_thread
        rpt._session_id = "sess-ok"

        # Should not emit any WARNING-level log
        with patch.object(
            logging.getLogger("KrabEar.RealtimePartial"), "warning"
        ) as mock_warn:
            rpt.stop(timeout_sec=0.01)
        mock_warn.assert_not_called()


class TestThreadHandleAfterJoinTimeout(unittest.TestCase):
    """Живой handle сохраняется до подтверждённого завершения worker."""

    def test_thread_retained_after_join_timeout(self):
        rpt = _make_rpt()

        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = True  # simulates stuck thread
        rpt._thread = mock_thread

        with self.assertLogs("KrabEar.RealtimePartial", level="WARNING"):
            stopped = rpt.stop(timeout_sec=0.01)

        self.assertFalse(stopped)
        self.assertIs(rpt._thread, mock_thread)

        rpt.start("sess-restart-blocked")
        self.assertIs(rpt._thread, mock_thread)

    def test_thread_set_to_none_when_not_running(self):
        """stop() when thread is None leaves thread as None."""
        rpt = _make_rpt()
        self.assertIsNone(rpt._thread)
        self.assertTrue(rpt.stop())
        self.assertIsNone(rpt._thread)

    def test_thread_set_to_none_after_clean_stop(self):
        """Thread None after clean stop (thread exits in time)."""
        rpt = _make_rpt()

        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.side_effect = [True, False]
        rpt._thread = mock_thread

        self.assertTrue(rpt.stop(timeout_sec=0.01))
        self.assertIsNone(rpt._thread)


class TestNoStaleEmitAfterStopTimeout(unittest.TestCase):
    """Завершившийся после timeout STT-вызов не публикует старый partial."""

    def test_blocked_transcription_does_not_emit_after_stop(self):
        entered = threading.Event()
        release = threading.Event()
        transcriber = MagicMock()

        def _blocked_preview(**_kwargs):
            entered.set()
            release.wait(timeout=2.0)
            return {"text": "устаревший текст"}

        transcriber.transcribe_preview.side_effect = _blocked_preview
        recorder = MagicMock()
        recorder.snapshot_audio.return_value = (MagicMock(size=16), 1.0)
        event_bus = MagicMock()
        rpt = RealtimePartialTranscriber(
            transcriber=transcriber,
            recorder=recorder,
            event_bus=event_bus,
            interval_sec=0.1,
        )
        rpt.start("sess-timeout")
        self.addCleanup(rpt.stop, 1.0)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(timeout=1.0))

        with self.assertLogs("KrabEar.RealtimePartial", level="WARNING"):
            self.assertFalse(rpt.stop(timeout_sec=0.01))
        release.set()
        thread = rpt._thread
        self.assertIsNotNone(thread)
        assert thread is not None
        thread.join(timeout=1.0)
        self.assertTrue(rpt.stop(timeout_sec=0.1))
        event_bus.emit.assert_not_called()

    def test_snapshot_unblocked_after_stop_does_not_start_stt(self):
        """После stop() результат зависшего snapshot не запускает STT."""
        entered = threading.Event()
        release = threading.Event()
        transcriber = _make_transcriber_with_preview()
        recorder = MagicMock()

        def _blocked_snapshot(**_kwargs):
            entered.set()
            release.wait(timeout=2.0)
            return MagicMock(size=16), 1.0

        recorder.snapshot_audio.side_effect = _blocked_snapshot
        event_bus = MagicMock()
        rpt = RealtimePartialTranscriber(
            transcriber=transcriber,
            recorder=recorder,
            event_bus=event_bus,
            interval_sec=0.1,
        )
        rpt.start("sess-snapshot-timeout")
        self.addCleanup(rpt.stop, 1.0)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(timeout=1.0))

        with self.assertLogs("KrabEar.RealtimePartial", level="WARNING"):
            self.assertFalse(rpt.stop(timeout_sec=0.01))
        release.set()
        thread = rpt._thread
        self.assertIsNotNone(thread)
        assert thread is not None
        thread.join(timeout=1.0)
        self.assertTrue(rpt.stop(timeout_sec=0.1))
        transcriber.transcribe_preview.assert_not_called()


class TestWorkerChecksStopRequestedFlag(unittest.TestCase):
    """Worker thread exits quickly when _stop_requested is set."""

    def test_worker_exits_on_stop_requested(self):
        """Worker reads _stop_requested at each iteration and breaks early."""
        rpt = _make_rpt(interval_sec=60.0)  # long interval to expose bug if flag ignored

        # Manually poke _stop_requested before starting; worker should exit on first check.
        rpt._stop_requested = True
        rpt._stop_event.clear()
        rpt._session_id = "sess-flag"

        # Run worker in a real daemon thread; should exit almost immediately.
        done = threading.Event()

        def _patched_worker():
            rpt._worker()
            done.set()

        t = threading.Thread(target=_patched_worker, daemon=True)
        t.start()
        exited = done.wait(timeout=1.0)
        self.assertTrue(exited, "Worker should exit quickly when _stop_requested=True at start")

    def test_stop_sets_event_and_flag(self):
        """stop() sets both _stop_event and _stop_requested."""
        rpt = _make_rpt()
        self.assertFalse(rpt._stop_event.is_set())
        self.assertFalse(rpt._stop_requested)
        rpt.stop()
        self.assertTrue(rpt._stop_event.is_set())
        self.assertTrue(rpt._stop_requested)


if __name__ == "__main__":
    unittest.main()
