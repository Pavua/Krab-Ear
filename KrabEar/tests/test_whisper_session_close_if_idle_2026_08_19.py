"""Memory Conductor T2 (2026-08-19): MLXWhisperSession in-flight hardening.

C-INFLIGHT: evictions atomic with in-flight work via the resident's OWN lock.
``self._lock`` (threading.Lock, non-reentrant) already guards ``_send()`` —
naive "close_if_idle takes the same lock" is fine ONLY if the inflight
counter is bumped OUTSIDE the request/response critical section (the gap
between ``start()`` returning and ``_send()`` grabbing the lock), otherwise
a resident mid-flight but between those two calls would look "idle" to a
conductor sweep. These tests pin that gap directly, plus the deadlock-free
close on the ``_send()`` write-failure path (W-#1872 class:
"close() taking a lock it's already inside" self-deadlocks).
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _fake_popen(stdout_line: str | None = "", returncode: int | None = None):
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()
    proc.stdout.readline.return_value = stdout_line if stdout_line is not None else ""
    proc.stderr.readline.return_value = ""
    proc.stderr.read.return_value = ""
    proc.poll.return_value = returncode
    proc.pid = 4242
    return proc


class CloseIfIdleInflightGateTest(unittest.TestCase):
    """(a) close_if_idle must never kill a proc while inflight > 0."""

    def tearDown(self):
        try:
            from core.mlx_whisper_session import reset_mlx_whisper_session

            reset_mlx_whisper_session()
        except Exception:
            pass

    def test_close_if_idle_false_in_start_to_send_gap(self):
        """Пин гонки C-INFLIGHT: лок свободен (start() уже завершён, _send()
        ещё не вошёл в свою критическую секцию), но inflight уже поднят —
        close_if_idle обязан отказать именно по счётчику, а не по локу."""
        from core.mlx_whisper_session import MLXWhisperSession

        session = MLXWhisperSession()
        proc = _fake_popen('{"ok": true, "result": {"text": "ok", "segments": []}}\n', None)

        reached_gap = threading.Event()
        release_gap = threading.Event()
        orig_send = session._send

        def paused_send(*args, **kwargs):
            reached_gap.set()
            self.assertTrue(release_gap.wait(timeout=5), "release_gap never fired")
            return orig_send(*args, **kwargs)

        with patch("core.mlx_whisper_session.subprocess.Popen", return_value=proc):
            session.start()
        session._send = paused_send

        result_holder: dict = {}

        def run():
            result_holder["result"] = session.transcribe(
                "/tmp/a.wav", {}, timeout_sec=5.0, model_name="turbo"
            )

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(reached_gap.wait(timeout=2.0), "transcribe never reached _send")
            # Лок в этот момент свободен (paused_send ещё не вызвал orig_send),
            # так что close_if_idle обязан получить его немедленно и сам
            # отказать по self._inflight > 0.
            self.assertEqual(session.inflight, 1)
            self.assertFalse(session.close_if_idle(0.0))
            self.assertIsNotNone(session._proc)
        finally:
            release_gap.set()
            worker.join(timeout=5.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result_holder["result"]["text"], "ok")
        self.assertEqual(session.inflight, 0)

    def test_close_if_idle_blocked_while_send_holds_lock_proc_survives(self):
        """Дополнение (не самодостаточное — см. докстринг модуля): пока
        _send() реально держит self._lock на чтении ответа, close_if_idle
        не может даже войти в тело — прод-инвариант «не убьёт процесс
        посреди отправки» держится самим локом, отдельно от счётчика."""
        from core.mlx_whisper_session import MLXWhisperSession

        session = MLXWhisperSession()
        proc = _fake_popen(None, None)

        send_started = threading.Event()
        release_send = threading.Event()

        def slow_readline():
            send_started.set()
            release_send.wait(timeout=5)
            return '{"ok": true, "result": {"text": "ok", "segments": []}}\n'

        proc.stdout.readline.side_effect = slow_readline

        with patch("core.mlx_whisper_session.subprocess.Popen", return_value=proc):
            session.start()

        worker = threading.Thread(
            target=lambda: session.transcribe("/tmp/a.wav", {}, timeout_sec=5.0, model_name="turbo")
        )
        worker.start()
        try:
            self.assertTrue(send_started.wait(timeout=2.0), "_send never reached readline")
            closer_done = threading.Event()

            def try_close():
                session.close_if_idle(0.0)
                closer_done.set()

            closer = threading.Thread(target=try_close)
            closer.start()
            # Лок держит _send() — close_if_idle обязан застрять на acquire,
            # а не проскочить и убить процесс, который сейчас "в полёте".
            self.assertFalse(closer_done.wait(timeout=0.3))
            self.assertIsNotNone(session._proc)
        finally:
            release_send.set()
            worker.join(timeout=5.0)
            closer.join(timeout=5.0)

        self.assertFalse(worker.is_alive())
        self.assertFalse(closer.is_alive())


class CloseIfIdleTerminatesWhenIdleTest(unittest.TestCase):
    """(b) close_if_idle True + процесс реально остановлен при простое >= порога."""

    def test_close_if_idle_true_terminates_proc_after_threshold(self):
        from core.mlx_whisper_session import MLXWhisperSession

        session = MLXWhisperSession()
        proc = _fake_popen("", None)
        with patch("core.mlx_whisper_session.subprocess.Popen", return_value=proc):
            session.start()
        session._last_used_ts = time.monotonic() - 100.0

        self.assertTrue(session.close_if_idle(1.0))

        self.assertIsNone(session._proc)
        proc.wait.assert_called()

    def test_close_if_idle_false_when_not_idle_enough(self):
        from core.mlx_whisper_session import MLXWhisperSession

        session = MLXWhisperSession()
        proc = _fake_popen("", None)
        with patch("core.mlx_whisper_session.subprocess.Popen", return_value=proc):
            session.start()
        session._last_used_ts = time.monotonic()

        self.assertFalse(session.close_if_idle(60.0))
        self.assertIsNotNone(session._proc)

    def test_close_if_idle_false_when_no_proc(self):
        from core.mlx_whisper_session import MLXWhisperSession

        session = MLXWhisperSession()
        self.assertFalse(session.close_if_idle(0.0))


class SendBrokenPipeNoDeadlockTest(unittest.TestCase):
    """(c) Ошибка внутри _send() (лок УЖЕ держится) обязана закрыться через
    _close_unlocked(), а не через lock-acquiring close() — иначе self-deadlock
    (W-#1872 class), здесь проявится как ~5с stall на таймауте close()."""

    def test_broken_pipe_on_write_closes_fast_without_deadlock(self):
        from core.mlx_whisper_session import MLXWhisperSession, MLXWorkerCrashed

        session = MLXWhisperSession()
        proc = _fake_popen("", None)
        proc.stdin.write.side_effect = BrokenPipeError()

        with patch("core.mlx_whisper_session.subprocess.Popen", return_value=proc):
            session.start()
            start_ts = time.monotonic()
            with self.assertRaises(MLXWorkerCrashed):
                session.transcribe("/tmp/a.wav", {}, timeout_sec=2.0, model_name="turbo")
            elapsed = time.monotonic() - start_ts

        self.assertLess(elapsed, 5.0, "self-deadlock class: close() must not re-acquire the lock")
        self.assertIsNone(session._proc)
        self.assertEqual(session.inflight, 0)


class PeekSessionTest(unittest.TestCase):
    """(d) peek_session() — module-level, read-only, never creates."""

    def tearDown(self):
        try:
            from core.mlx_whisper_session import reset_mlx_whisper_session

            reset_mlx_whisper_session()
        except Exception:
            pass

    def test_peek_session_returns_none_without_creating_when_absent(self):
        import core.mlx_whisper_session as mws

        mws.reset_mlx_whisper_session()
        self.assertIsNone(mws._session)

        result = mws.peek_session()

        self.assertIsNone(result)
        self.assertIsNone(mws._session)

    def test_peek_session_returns_existing_singleton_without_replacing_it(self):
        import core.mlx_whisper_session as mws

        created = mws.get_mlx_whisper_session()

        result = mws.peek_session()

        self.assertIs(result, created)
        self.assertIs(mws._session, created)


if __name__ == "__main__":
    unittest.main()
