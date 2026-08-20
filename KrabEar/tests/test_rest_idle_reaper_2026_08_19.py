"""Memory Conductor T6 (2026-08-19): REST idle-reaper for mlx_whisper_worker.

Spec: docs/superpowers/specs/2026-08-19-memory-conductor-design.md
§3 C-EXECUTOR-LOCALITY, §4 (ledger), §6 (components).

C-EXECUTOR-LOCALITY: this REST process OWNS the mlx_whisper worker (the
session singleton lives in core.mlx_whisper_session, imported by this
process), so eviction runs HERE via a lightweight idle-reaper thread — never
from the IPC-side conductor.

Coverage:
  (a) the reaper thread is NEVER started at module import — rest_server.py is
      imported by chunked tests (#1782 daemon-thread-at-import class); a
      fresh subprocess proves no "whisper-idle-reaper" thread exists right
      after `import backend.rest_server`.
  (b) InProcessRestServer.start() (M2 run path) launches the reaper.
  (c) a tick with an idle session (past the threshold) calls
      session.close_if_idle() — but ONLY when memory_conductor_enabled AND
      an enforce flag are both true (shadow default otherwise, Global
      Constraint: memory_conductor_enforce=False by default).
  (d) peek-only: when no session exists yet, the tick never creates one.
  (e) each tick publishes krab_ear_rest/mlx_whisper_worker with
      state=active|idle derived from session.inflight/last_used_ts.
  (f) start_whisper_idle_reaper() is idempotent via a module-level
      threading.Event — RestWatchdog calls InProcessRestServer.start()
      repeatedly (restart = stop()+start()); a double call must yield
      exactly ONE "whisper-idle-reaper" thread.
  (g) guarded by mlx_whisper_worker_enabled() — when the worker path is
      disabled, no reaper thread is spawned at all.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import backend.rest_server as rest_server  # noqa: E402
from backend.rest_inprocess import InProcessRestServer  # noqa: E402


class _FakeStore:
    """Minimal store stand-in for `_load_settings_field()` (reads via _deps()).

    ``**kwargs`` on load_settings(): the fake-signature-drift guard
    (scripts/audit_fake_store_signatures.py) requires a fake to accept every
    kwarg the REAL StateStore is called with anywhere in production
    (e.g. ``nowait=``/``lock_timeout_sec=`` elsewhere in service.py) — not
    just the args this specific test's call site happens to use.
    """

    def __init__(self, values: dict | None = None) -> None:
        self._values = dict(values or {})

    def load_settings(self, **kwargs) -> dict:
        return dict(self._values)


class _TinyApp:
    """Minimal WSGI app — the reaper hook test cares about the transport
    (InProcessRestServer.start() calling the hook), not the REST contract."""

    def __call__(self, environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _reset_reaper_singletons() -> None:
    """Module-level singletons in rest_server.py must be reset between tests
    — start_whisper_idle_reaper()'s idempotency Event and the DI ledger-client
    seam are process-wide state, not per-test fixtures."""
    rest_server._whisper_reaper_started = threading.Event()
    rest_server._whisper_ledger_client = None


class ReaperNeverStartsAtImportTest(unittest.TestCase):
    """(a) a fresh subprocess importing backend.rest_server must show NO
    "whisper-idle-reaper" thread — the #1782 daemon-thread-at-import class."""

    def test_no_reaper_thread_after_bare_import(self):
        code = (
            "import threading\n"
            "import backend.rest_server\n"
            "names = [t.name for t in threading.enumerate()]\n"
            "print('THREADS:' + ','.join(names))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr[-4000:])
        thread_line = [ln for ln in result.stdout.splitlines() if ln.startswith("THREADS:")]
        self.assertTrue(thread_line, msg=f"no THREADS line in stdout: {result.stdout!r}")
        self.assertNotIn("whisper-idle-reaper", thread_line[-1])


class StartHookLaunchesReaperTest(unittest.TestCase):
    """(b) the M2 run path (InProcessRestServer.start()) launches the reaper."""

    def setUp(self):
        _reset_reaper_singletons()

    def tearDown(self):
        _reset_reaper_singletons()

    def test_inprocess_start_calls_reaper_hook(self):
        cfg = SimpleNamespace(REST_SERVER_PORT=_free_port())
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=True)
        with patch("backend.rest_server.start_whisper_idle_reaper") as mock_start:
            try:
                self.assertTrue(srv.start())
                mock_start.assert_called_once()
            finally:
                srv.stop()

    def test_disabled_switch_does_not_launch_reaper(self):
        cfg = SimpleNamespace(REST_SERVER_PORT=_free_port())
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=False)
        with patch("backend.rest_server.start_whisper_idle_reaper") as mock_start:
            self.assertFalse(srv.start())
            mock_start.assert_not_called()
        srv.stop()


class WhisperReaperEnabledGateTest(unittest.TestCase):
    """(g) guarded by mlx_whisper_worker_enabled().

    Thread construction is mocked away (not just spied-on-then-let-run): the
    real target is an infinite ``while True: tick(); sleep()`` loop with no
    stop mechanism (by design — it lives for the process lifetime), so a real
    spawn here would leak a daemon thread into every LATER test in this same
    pytest process, corrupting any threading.enumerate()-based assertion
    downstream (observed: this caused exactly that flake before the fix).
    """

    def setUp(self):
        _reset_reaper_singletons()

    def tearDown(self):
        _reset_reaper_singletons()

    def test_disabled_worker_path_spawns_no_thread(self):
        with patch("core.mlx_whisper_session.mlx_whisper_worker_enabled", return_value=False), \
                patch.object(rest_server.threading, "Thread") as mock_thread_cls:
            rest_server.start_whisper_idle_reaper(interval_sec=0.01)
        mock_thread_cls.assert_not_called()

    def test_enabled_worker_path_spawns_thread(self):
        with patch("core.mlx_whisper_session.mlx_whisper_worker_enabled", return_value=True), \
                patch.object(rest_server.threading, "Thread") as mock_thread_cls:
            rest_server.start_whisper_idle_reaper(interval_sec=0.01)
        mock_thread_cls.assert_called_once()
        _args, kwargs = mock_thread_cls.call_args
        self.assertEqual(kwargs.get("name"), "whisper-idle-reaper")
        self.assertTrue(kwargs.get("daemon"))
        mock_thread_cls.return_value.start.assert_called_once()


class WhisperReaperIdempotentStartTest(unittest.TestCase):
    """(f) double start_whisper_idle_reaper() → exactly ONE Thread spawned."""

    def setUp(self):
        _reset_reaper_singletons()

    def tearDown(self):
        _reset_reaper_singletons()

    def test_double_start_yields_one_thread(self):
        with patch("core.mlx_whisper_session.mlx_whisper_worker_enabled", return_value=True), \
                patch.object(rest_server.threading, "Thread") as mock_thread_cls:
            rest_server.start_whisper_idle_reaper(interval_sec=0.01)
            rest_server.start_whisper_idle_reaper(interval_sec=0.01)
        mock_thread_cls.assert_called_once()
        mock_thread_cls.return_value.start.assert_called_once()


class WhisperReaperTickTest(unittest.TestCase):
    """(c) idle+enforced → close_if_idle called; shadow default → not called.
    (d) peek-only: no session created when none exists.
    (e) ledger publish reflects inflight/last_used_ts state.

    🔴 EVERY test in this class must have ``rest_server._whisper_ledger_client``
    pre-populated with a fake BEFORE calling ``_whisper_idle_reaper_tick()`` —
    the lazy singleton in ``_whisper_ledger()`` otherwise constructs a REAL
    ``LedgerClient`` pointed at ``~/.openclaw/memory_ledger.json`` (CLAUDE.md:
    "в тестах ВСЕГДА подменяй path= на TemporaryDirectory"). setUp() below
    installs a default MagicMock for every test; tests that inspect the
    publish payload install their OWN MagicMock instead (still never real).
    """

    def setUp(self):
        _reset_reaper_singletons()
        rest_server._whisper_ledger_client = MagicMock()

    def tearDown(self):
        _reset_reaper_singletons()

    def _fake_session(self, inflight: int, last_used_ts: float):
        sess = MagicMock()
        sess.inflight = inflight
        sess.last_used_ts = last_used_ts
        sess.close_if_idle.return_value = True
        return sess

    def test_peek_only_no_session_created(self):
        creator = MagicMock(side_effect=AssertionError("must not create a session"))
        with patch("core.mlx_whisper_session.peek_session", return_value=None), \
                patch("core.mlx_whisper_session.get_mlx_whisper_session", creator):
            rest_server._whisper_idle_reaper_tick()
        creator.assert_not_called()

    def test_idle_past_threshold_enforced_calls_close_if_idle(self):
        old_ts = time.monotonic() - 999.0
        sess = self._fake_session(inflight=0, last_used_ts=old_ts)
        fake_store = _FakeStore({
            "whisper_idle_unload_sec": 5.0,
            "memory_conductor_enabled": True,
            "memory_conductor_enforce_whisper": True,
        })
        with patch("core.mlx_whisper_session.peek_session", return_value=sess), \
                patch.object(rest_server, "store", fake_store):
            rest_server._whisper_idle_reaper_tick()
        sess.close_if_idle.assert_called_once_with(5.0)

    def test_idle_past_threshold_shadow_default_does_not_evict(self):
        """Global Constraint: memory_conductor_enforce=False by default —
        reaper must SHADOW-log, never call close_if_idle."""
        old_ts = time.monotonic() - 999.0
        sess = self._fake_session(inflight=0, last_used_ts=old_ts)
        fake_store = _FakeStore({"whisper_idle_unload_sec": 5.0})
        with patch("core.mlx_whisper_session.peek_session", return_value=sess), \
                patch.object(rest_server, "store", fake_store):
            rest_server._whisper_idle_reaper_tick()
        sess.close_if_idle.assert_not_called()

    def test_conductor_enabled_without_resident_enforce_stays_shadow(self):
        old_ts = time.monotonic() - 999.0
        sess = self._fake_session(inflight=0, last_used_ts=old_ts)
        fake_store = _FakeStore({
            "whisper_idle_unload_sec": 5.0,
            "memory_conductor_enabled": True,
            # neither memory_conductor_enforce nor _enforce_whisper set
        })
        with patch("core.mlx_whisper_session.peek_session", return_value=sess), \
                patch.object(rest_server, "store", fake_store):
            rest_server._whisper_idle_reaper_tick()
        sess.close_if_idle.assert_not_called()

    def test_not_yet_idle_enough_does_not_evict(self):
        recent_ts = time.monotonic() - 1.0
        sess = self._fake_session(inflight=0, last_used_ts=recent_ts)
        fake_store = _FakeStore({
            "whisper_idle_unload_sec": 900.0,
            "memory_conductor_enabled": True,
            "memory_conductor_enforce_whisper": True,
        })
        with patch("core.mlx_whisper_session.peek_session", return_value=sess), \
                patch.object(rest_server, "store", fake_store):
            rest_server._whisper_idle_reaper_tick()
        sess.close_if_idle.assert_not_called()

    def test_publishes_active_state_when_inflight(self):
        sess = self._fake_session(inflight=1, last_used_ts=time.monotonic())
        ledger = MagicMock()
        rest_server._whisper_ledger_client = ledger
        with patch("core.mlx_whisper_session.peek_session", return_value=sess), \
                patch.object(rest_server, "store", _FakeStore()):
            rest_server._whisper_idle_reaper_tick()
        ledger.publish_own.assert_called_once()
        (payload,), _kwargs = ledger.publish_own.call_args
        entry = payload["mlx_whisper_worker"]
        self.assertEqual(entry["state"], "active")
        self.assertIsNone(entry["idle_since_ts"])
        self.assertEqual(entry["reload_cost"], "cheap")

    def test_publishes_idle_state_with_idle_since_ts(self):
        last_used = time.monotonic() - 42.0
        sess = self._fake_session(inflight=0, last_used_ts=last_used)
        ledger = MagicMock()
        rest_server._whisper_ledger_client = ledger
        with patch("core.mlx_whisper_session.peek_session", return_value=sess), \
                patch.object(rest_server, "store", _FakeStore({"whisper_idle_unload_sec": 900.0})):
            rest_server._whisper_idle_reaper_tick()
        (payload,), _kwargs = ledger.publish_own.call_args
        entry = payload["mlx_whisper_worker"]
        self.assertEqual(entry["state"], "idle")
        self.assertEqual(entry["idle_since_ts"], last_used)

    def test_no_session_publishes_nothing(self):
        ledger = MagicMock()
        rest_server._whisper_ledger_client = ledger
        with patch("core.mlx_whisper_session.peek_session", return_value=None):
            rest_server._whisper_idle_reaper_tick()
        ledger.publish_own.assert_not_called()


if __name__ == "__main__":
    unittest.main()
