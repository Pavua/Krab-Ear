"""Tests for W1216 F1 (auto-restart after worker crash) and F2 (spawn lock).

W1216 F1: _get_subprocess_session() must clear a dead session and re-spawn
          rather than returning the stale dead session on the next transcribe call.

W1216 F2: concurrent transcribe() calls with _subprocess is None must not
          spawn duplicate workers; _spawn_lock serialises the spawn path.

All tests mock subprocess; no real GigaAM worker is spawned.

Run:
    PYTHONPATH=KrabEar python3 -m unittest KrabEar/tests/test_gigaam_restart_lock_W1222.py -v
"""
from __future__ import annotations

import collections
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

# Настройка PYTHONPATH для standalone-запуска
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _make_adapter(transport: str = "subprocess"):
    """Return a GigaAMAdapter configured for subprocess transport."""
    from core.pipeline.stt_gigaam import GigaAMAdapter
    return GigaAMAdapter(device="cpu", mode="rnnt", transport=transport)


def _make_mock_session(loaded: bool = True) -> MagicMock:
    """Return a MagicMock that mimics _GigaAMSubprocessSession."""
    session = MagicMock()
    session.is_loaded.return_value = loaded
    session.transcribe.return_value = {"ok": True, "text": "тест", "engine": "gigaam-rnnt"}
    session.oom_callback = None
    return session


def _make_stub_session_class(spawn_counter: dict | None = None):
    """Return a stub _GigaAMSubprocessSession-like class that does not spawn a process."""
    counter = spawn_counter if spawn_counter is not None else {"n": 0}

    class StubSession:
        def __init__(self, *, venv_python: str, worker_path: str, mode: str, device: str):
            counter["n"] += 1
            self.oom_callback = None
            self._error_bus = None
            self._lock = threading.Lock()
            self._loaded = False
            self._proc = None
            self._stderr_ring = collections.deque(maxlen=200)
            self._stderr_drain_thread = None
            self._venv_python = venv_python
            self._worker_path = worker_path
            self._mode = mode
            self._device = device

        def start(self):
            self._loaded = True

        def is_loaded(self) -> bool:
            return self._loaded and self._proc is None

        def close(self):
            self._loaded = False

        def transcribe(self, audio_path: str, **_kwargs) -> dict:
            return {"ok": True, "text": "привет", "engine": "gigaam-rnnt"}

    # is_loaded should always return True after start() — patch it appropriately
    # For tests needing a live session: loaded=True simulated via _loaded after start().
    return StubSession


# ---------------------------------------------------------------------------
# F1: dead worker auto-restart
# ---------------------------------------------------------------------------

class TestDeadWorkerAutoRestarts(unittest.TestCase):
    """F1: after _timeout_kill fires the session becomes dead; next transcribe
    must clear the dead session and re-spawn successfully (W1216 F1)."""

    def test_dead_worker_auto_restarts_on_next_transcribe(self):
        """_get_subprocess_session() clears dead session and spawns a fresh one."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        adapter = _make_adapter()
        spawn_counter = {"n": 0}
        StubSession = _make_stub_session_class(spawn_counter)

        # Install a dead session (is_loaded() → False, simulates post-timeout state).
        dead_session = _make_mock_session(loaded=False)
        adapter._subprocess = dead_session

        with patch("os.path.exists", return_value=True), \
             patch("core.pipeline.stt_gigaam._GigaAMSubprocessSession", StubSession):
            new_session = adapter._get_subprocess_session()

        # Dead session should have been diagnosed (OOM check) then closed — 2026-08-13:
        # plain close() would silently discard the exit code/stderr ring of a session
        # that died while idle (no _send() in flight to trigger _check_proc_oom_on_exit).
        dead_session.diagnose_and_close.assert_called_once()
        # A new session was spawned.
        self.assertEqual(spawn_counter["n"], 1, "Expected exactly one new session spawned")
        # adapter._subprocess is now the new session, not the dead one.
        self.assertIsNot(adapter._subprocess, dead_session)
        # New session is live.
        self.assertTrue(new_session.is_loaded())

    def test_live_worker_is_not_restarted(self):
        """A live session must be returned as-is without close() being called."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        adapter = _make_adapter()
        live_session = _make_mock_session(loaded=True)
        adapter._subprocess = live_session

        result = adapter._get_subprocess_session()

        self.assertIs(result, live_session)
        live_session.close.assert_not_called()

    def test_none_subprocess_spawns_new_session(self):
        """With _subprocess is None, a new session is spawned."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        adapter = _make_adapter()
        self.assertIsNone(adapter._subprocess)

        spawn_counter = {"n": 0}
        StubSession = _make_stub_session_class(spawn_counter)

        with patch("os.path.exists", return_value=True), \
             patch("core.pipeline.stt_gigaam._GigaAMSubprocessSession", StubSession):
            session = adapter._get_subprocess_session()

        self.assertEqual(spawn_counter["n"], 1, "Expected exactly one spawn")
        self.assertIsNotNone(adapter._subprocess)
        self.assertTrue(session.is_loaded())


# ---------------------------------------------------------------------------
# F2: spawn lock serialises concurrent spawn calls
# ---------------------------------------------------------------------------

class TestConcurrentSpawnSerializedByLock(unittest.TestCase):
    """F2: two concurrent calls with _subprocess is None must spawn only one
    worker; the second thread re-uses the session set by the first (W1216 F2)."""

    def test_concurrent_spawn_serialized_by_lock(self):
        """Only one worker session is spawned when two threads race to spawn."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        adapter = _make_adapter()
        spawn_counter = {"n": 0}
        StubSession = _make_stub_session_class(spawn_counter)

        sessions_seen = []
        errors = []
        barrier = threading.Barrier(2)

        def worker():
            try:
                barrier.wait()
                session = adapter._get_subprocess_session()
                sessions_seen.append(session)
            except Exception as exc:
                errors.append(exc)

        with patch("os.path.exists", return_value=True), \
             patch("core.pipeline.stt_gigaam._GigaAMSubprocessSession", StubSession):
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        self.assertFalse(errors, f"Unexpected exceptions: {errors}")
        # Exactly one spawn should have occurred.
        self.assertEqual(spawn_counter["n"], 1, "Expected exactly one worker spawn")
        # Both threads should have received the same session object.
        self.assertEqual(len(sessions_seen), 2)
        self.assertIs(sessions_seen[0], sessions_seen[1],
                      "Both threads must share the same session instance")

    def test_spawn_lock_attr_exists_on_adapter(self):
        """GigaAMAdapter must expose _spawn_lock as a threading.Lock (W1216 F2)."""
        from core.pipeline.stt_gigaam import GigaAMAdapter
        adapter = GigaAMAdapter(device="cpu", mode="rnnt", transport="subprocess")
        self.assertTrue(hasattr(adapter, "_spawn_lock"),
                        "_spawn_lock attribute must exist on GigaAMAdapter")
        # threading.Lock() returns a _thread.lock; use acquire/release to verify behaviour.
        acquired = adapter._spawn_lock.acquire(blocking=False)
        self.assertTrue(acquired, "_spawn_lock must be acquirable (not already held)")
        adapter._spawn_lock.release()


# ---------------------------------------------------------------------------
# F2: spawn lock releases on exception
# ---------------------------------------------------------------------------

class TestSpawnLockReleasesOnException(unittest.TestCase):
    """F2: if spawn raises (e.g. missing venv), _spawn_lock must be released
    so subsequent calls are not permanently deadlocked (W1216 F2)."""

    def test_spawn_lock_releases_on_exception(self):
        """Lock is released even when _get_subprocess_session() raises RuntimeError."""
        from core.pipeline.stt_gigaam import GigaAMAdapter

        adapter = _make_adapter()

        # Make os.path.exists return False → RuntimeError (missing venv).
        with patch("os.path.exists", return_value=False):
            with self.assertRaises(RuntimeError):
                adapter._get_subprocess_session()

        # The lock must be released — trying to acquire it with blocking=False must succeed.
        acquired = adapter._spawn_lock.acquire(blocking=False)
        try:
            self.assertTrue(acquired,
                            "_spawn_lock must be released after an exception during spawn")
        finally:
            if acquired:
                adapter._spawn_lock.release()


if __name__ == "__main__":
    unittest.main()
