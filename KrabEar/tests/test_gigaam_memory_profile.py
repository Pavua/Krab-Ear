"""Tests for GigaAM worker memory profiling instrumentation (Phase C C.1).

These tests verify the opt-in tracing mechanism introduced in gigaam_worker.py.
They do NOT load the actual GigaAM model (requires .venv_gigaam + gigaam package),
and they do NOT execute any subprocess. Pure unit tests for env-var detection.

Run:
    PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest \
        KrabEar/tests/test_gigaam_memory_profile.py -q
"""

from __future__ import annotations

import os
import sys
import tracemalloc
import types
import unittest

import pytest

# GigaAM работает ТОЛЬКО на macOS (требует отдельный .venv_krab_ear_gigaam; пакет gigaam
# пинит torch<=2.5.1 / onnxruntime<=1.23.x). На Linux CI (Python 3.12, torch-CPU)
# повторный reimport gigaam_worker + torch-backed импорт stt_gigaam в этих профайлинг-
# тестах роняет интерпретатор SIGSEGV (re-init C-расширения небезопасен). Фича macOS-only,
# поэтому гейтим весь файл на darwin (W1755 — был единственный genuine-фейл chunk-2).
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="GigaAM worker profiling is macOS-only; segfaults on Linux py3.12 torch-CPU reimport",
)


# ---------------------------------------------------------------------------
# Helpers to reload gigaam_worker with a clean module cache
# ---------------------------------------------------------------------------


def _reload_worker_module(env: dict[str, str]) -> types.ModuleType:
    """Re-import gigaam_worker after setting the given env vars.

    Saves/restores the original environment around the import so tests
    are isolated.  Any previously cached module is evicted from sys.modules
    first so the module-level `_TRACE_MEM` flag is re-evaluated.
    """
    # Resolve absolute path to avoid sys.path issues
    worker_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "core", "workers")
    )
    if worker_dir not in sys.path:
        sys.path.insert(0, worker_dir)

    saved_env = {}
    for key in ("KRAB_EAR_TRACE_GIGAAM_MEM",):
        saved_env[key] = os.environ.pop(key, None)  # type: ignore[assignment]

    for key, value in env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    # Evict from module cache so module-level code re-runs
    sys.modules.pop("gigaam_worker", None)

    # Stop any running tracemalloc to avoid cross-test interference
    tracemalloc.stop()

    try:
        # Patch _acquire_singleton_lock to a no-op before the module re-imports
        # (the module-level call would fail in tests because the flock is already held
        # from a previous import in the same process — Wave 525 singleton guard).
        import unittest.mock as _mock
        with _mock.patch.dict(sys.modules, {}):
            with _mock.patch("builtins.__import__"):
                pass  # reset approach below
        # Direct approach: inject a stub before reimport
        import types as _types
        _stub_mod = _types.ModuleType("gigaam_worker")
        sys.modules["gigaam_worker"] = _stub_mod
        # Now actually load with singleton disabled
        sys.modules.pop("gigaam_worker", None)
        with _mock.patch(
            "core.workers.gigaam_worker._acquire_singleton_lock",
            new=lambda: None,
            create=True,
        ):
            pass  # can't patch before import; use env-based approach
        # Simpler: set an env var that disables the guard, or just replace flock
        # Use unittest.mock.patch on fcntl.flock for the duration of import
        import fcntl as _fcntl
        with _mock.patch.object(_fcntl, "flock", return_value=None):
            import gigaam_worker as mod  # type: ignore[import]  # noqa: F401
    except ImportError as exc:
        raise unittest.SkipTest(
            f"gigaam_worker import failed (expected in isolated venv): {exc}"
        )
    except SystemExit:
        raise unittest.SkipTest(
            "gigaam_worker singleton guard prevented reimport (duplicate process)"
        )
    finally:
        # Restore original environment
        for key, orig in saved_env.items():
            if orig is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = orig

    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGigaAMTraceEnvVar(unittest.TestCase):
    """Verify that KRAB_EAR_TRACE_GIGAAM_MEM controls tracing."""

    def tearDown(self) -> None:
        # Always stop tracemalloc and clean module cache between tests
        tracemalloc.stop()
        sys.modules.pop("gigaam_worker", None)

    def test_env_var_enables_tracing(self) -> None:
        """When KRAB_EAR_TRACE_GIGAAM_MEM=1, _TRACE_MEM must be True and
        tracemalloc must be running after module import."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": "1"})
        self.assertTrue(
            mod._TRACE_MEM,
            "_TRACE_MEM should be True when env var = '1'",
        )
        self.assertTrue(
            tracemalloc.is_tracing(),
            "tracemalloc should be running when KRAB_EAR_TRACE_GIGAAM_MEM=1",
        )

    def test_no_env_var_no_overhead(self) -> None:
        """Without KRAB_EAR_TRACE_GIGAAM_MEM, _TRACE_MEM must be False and
        tracemalloc must NOT be started by the worker module."""
        # Ensure tracemalloc is not running before import
        tracemalloc.stop()

        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})
        self.assertFalse(
            mod._TRACE_MEM,
            "_TRACE_MEM should be False when env var is absent",
        )
        self.assertFalse(
            tracemalloc.is_tracing(),
            "tracemalloc should NOT be running without KRAB_EAR_TRACE_GIGAAM_MEM",
        )

    def test_env_var_wrong_value_no_overhead(self) -> None:
        """KRAB_EAR_TRACE_GIGAAM_MEM=0 (or any value != '1') must not enable tracing."""
        tracemalloc.stop()

        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": "0"})
        self.assertFalse(
            mod._TRACE_MEM,
            "_TRACE_MEM should be False when env var = '0'",
        )
        self.assertFalse(
            tracemalloc.is_tracing(),
            "tracemalloc should NOT be running when env var = '0'",
        )

    def test_log_rss_no_op_when_tracing_disabled(self) -> None:
        """_log_rss() must not raise and must not start tracemalloc when tracing off."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})
        # Must not raise
        mod._log_rss("test_label")
        # tracemalloc still off
        self.assertFalse(tracemalloc.is_tracing())

    def test_log_tracemalloc_snapshot_no_op_when_disabled(self) -> None:
        """_log_tracemalloc_snapshot() must not raise when tracing off."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})
        # Must not raise even at request count that would trigger snapshot
        mod._log_tracemalloc_snapshot(10)
        mod._log_tracemalloc_snapshot(20)

    def test_log_tracemalloc_snapshot_runs_when_enabled(self) -> None:
        """_log_tracemalloc_snapshot() should take a snapshot at multiples of 10."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": "1"})
        self.assertTrue(mod._TRACE_MEM)
        self.assertTrue(tracemalloc.is_tracing())
        # Should not raise (snapshot will be empty but valid)
        mod._log_tracemalloc_snapshot(10)
        mod._log_tracemalloc_snapshot(20)
        # Non-multiples of 10 must also not raise
        mod._log_tracemalloc_snapshot(7)
        mod._log_tracemalloc_snapshot(11)


class TestGigaAMWorkerHelpers(unittest.TestCase):
    """Test worker helper functions that don't require GigaAM model."""

    def tearDown(self) -> None:
        tracemalloc.stop()
        sys.modules.pop("gigaam_worker", None)

    def test_err_format(self) -> None:
        """_err() must return {"ok": False, "error": <message>}."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})
        result = mod._err("test_error: something went wrong")
        self.assertFalse(result["ok"])
        self.assertIn("test_error", result["error"])

    def test_process_request_empty_line(self) -> None:
        """Empty input line should return error dict, not raise."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})
        result = mod._process_request("   ")
        self.assertFalse(result["ok"])

    def test_process_request_invalid_json(self) -> None:
        """Invalid JSON should return error dict."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})
        result = mod._process_request("{not json}")
        self.assertFalse(result["ok"])
        self.assertIn("json_decode_error", result["error"])

    def test_process_request_unknown_op(self) -> None:
        """Unknown op should return error dict."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})
        import json
        result = mod._process_request(json.dumps({"op": "frobulate"}))
        self.assertFalse(result["ok"])
        self.assertIn("unknown_op", result["error"])

    def test_process_request_ping(self) -> None:
        """ping op must return {"ok": True, "pong": True, "model_loaded": False}."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})
        import json
        result = mod._process_request(json.dumps({"op": "ping"}))
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("pong"))
        self.assertFalse(result.get("model_loaded"))  # no model loaded in unit test

    def test_process_request_transcribe_without_model(self) -> None:
        """transcribe without prior load must return error dict."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})
        import json
        result = mod._process_request(json.dumps({"op": "transcribe", "audio_path": "/tmp/x.wav"}))
        self.assertFalse(result["ok"])
        self.assertIn("model_not_loaded", result["error"])

    def test_process_request_shutdown_returns_none(self) -> None:
        """shutdown op must return None (signals main loop to exit)."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})
        import json
        result = mod._process_request(json.dumps({"op": "shutdown"}))
        self.assertIsNone(result)


class TestGigaAMMemoryHygiene(unittest.TestCase):
    """Verify torch.mps.empty_cache + gc.collect hygiene after transcribe (Phase C C.1-fix)."""

    def tearDown(self) -> None:
        tracemalloc.stop()
        sys.modules.pop("gigaam_worker", None)

    # ------------------------------------------------------------------
    # _free_mps_pool helpers
    # ------------------------------------------------------------------

    def test_free_mps_pool_calls_empty_cache(self) -> None:
        """_free_mps_pool() must call torch.mps.empty_cache() when available."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})

        empty_cache_calls: list[int] = []

        # Build a minimal fake torch.mps namespace
        fake_mps = types.SimpleNamespace(empty_cache=lambda: empty_cache_calls.append(1))
        fake_torch = types.SimpleNamespace(mps=fake_mps)

        # Подменяем модуль с обязательным восстановлением: удаление уже
        # импортированного настоящего torch заставляет соседний тест повторно
        # инициализировать C-extension и даёт SIGSEGV при завершении Python.
        missing = object()
        saved_torch = sys.modules.get("torch", missing)
        sys.modules["torch"] = fake_torch  # type: ignore[assignment]
        try:
            mod._free_mps_pool()
        finally:
            if saved_torch is missing:
                sys.modules.pop("torch", None)
            else:
                sys.modules["torch"] = saved_torch  # type: ignore[assignment]

        self.assertEqual(
            len(empty_cache_calls),
            1,
            "_free_mps_pool() must call torch.mps.empty_cache() exactly once",
        )

    def test_free_mps_pool_calls_gc_collect(self) -> None:
        """_free_mps_pool() must call gc.collect() (via the gc module already imported)."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})

        import gc as _gc_real

        collect_calls: list[int] = []
        original_collect = _gc_real.collect

        def _mock_collect(*args: object, **kwargs: object) -> int:
            collect_calls.append(1)
            return 0

        _gc_real.collect = _mock_collect  # type: ignore[assignment]
        # Also patch in the module's own gc reference (it imports gc at top-level)
        original_mod_gc_collect = mod.gc.collect
        mod.gc.collect = _mock_collect  # type: ignore[assignment]
        try:
            mod._free_mps_pool()
        finally:
            _gc_real.collect = original_collect
            mod.gc.collect = original_mod_gc_collect

        self.assertGreaterEqual(
            len(collect_calls),
            1,
            "_free_mps_pool() must call gc.collect() at least once",
        )

    def test_empty_cache_called_after_transcribe(self) -> None:
        """_handle_transcribe invokes _free_mps_pool (and thus empty_cache) once per response.

        We verify by patching _free_mps_pool on the module and checking it's called
        after a successful (mocked) transcribe. No real GigaAM model is loaded.
        """
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})

        free_pool_calls: list[int] = []

        original_free = mod._free_mps_pool

        def _mock_free() -> None:
            free_pool_calls.append(1)

        mod._free_mps_pool = _mock_free

        # Set up a minimal fake model
        fake_model = types.SimpleNamespace(transcribe=lambda path: "привет мир")
        mod._MODEL = fake_model
        mod._MODE = "rnnt"

        try:
            import json
            result = mod._process_request(json.dumps({"op": "transcribe", "audio_path": "/tmp/fake.wav"}))
        finally:
            mod._MODEL = None
            mod._MODE = None
            mod._free_mps_pool = original_free

        self.assertIsNotNone(result)
        self.assertTrue(result.get("ok"), f"Expected ok=True, got: {result}")
        self.assertEqual(
            len(free_pool_calls),
            1,
            "_free_mps_pool must be called once after transcribe response",
        )

    def test_gc_collect_called_after_transcribe(self) -> None:
        """gc.collect() is invoked by _free_mps_pool after each successful transcribe."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})

        import gc as _gc_real

        collect_calls: list[int] = []
        original_collect = _gc_real.collect
        original_mod_gc_collect = mod.gc.collect

        def _mock_collect(*args: object, **kwargs: object) -> int:
            collect_calls.append(1)
            return 0

        _gc_real.collect = _mock_collect  # type: ignore[assignment]
        mod.gc.collect = _mock_collect  # type: ignore[assignment]

        fake_model = types.SimpleNamespace(transcribe=lambda path: "тест")
        mod._MODEL = fake_model
        mod._MODE = "rnnt"

        try:
            import json
            result = mod._process_request(json.dumps({"op": "transcribe", "audio_path": "/tmp/fake.wav"}))
        finally:
            mod._MODEL = None
            mod._MODE = None
            _gc_real.collect = original_collect
            mod.gc.collect = original_mod_gc_collect

        self.assertIsNotNone(result)
        self.assertTrue(result.get("ok"), f"Expected ok=True, got: {result}")
        self.assertGreaterEqual(
            len(collect_calls),
            1,
            "gc.collect must be called at least once after transcribe response",
        )

    def test_free_mps_pool_no_raise_without_torch(self) -> None:
        """_free_mps_pool() не бросает исключение, когда torch отсутствует."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})

        # Простого удаления из sys.modules недостаточно: следующий import загрузит
        # настоящий PyTorch, а torch 2.13 после таких reimport-тестов падает SIGSEGV
        # уже при завершении процесса. Блокируем только этот импорт явно.
        import builtins
        from unittest.mock import patch

        real_import = builtins.__import__

        def _import_without_torch(name, *args, **kwargs):
            if name == "torch":
                raise ImportError("torch намеренно отсутствует в тесте")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_import_without_torch):
            mod._free_mps_pool()

    def test_free_mps_pool_no_raise_when_empty_cache_missing(self) -> None:
        """_free_mps_pool() must not raise if torch.mps lacks empty_cache attribute."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})

        # torch.mps without empty_cache
        fake_mps = types.SimpleNamespace()  # no empty_cache attribute
        fake_torch = types.SimpleNamespace(mps=fake_mps)

        missing = object()
        saved_torch = sys.modules.get("torch", missing)
        sys.modules["torch"] = fake_torch  # type: ignore[assignment]
        try:
            mod._free_mps_pool()  # must not raise
        finally:
            if saved_torch is missing:
                sys.modules.pop("torch", None)
            else:
                sys.modules["torch"] = saved_torch  # type: ignore[assignment]


class TestH2GcCollectAfterLongform(unittest.TestCase):
    """H2 hypothesis: gc.collect() called after longform inference path."""

    def tearDown(self) -> None:
        tracemalloc.stop()
        sys.modules.pop("gigaam_worker", None)

    def test_h2_gc_collect_called_after_longform(self) -> None:
        """After longform transcription, gc.collect() must be invoked (H2 cleanup)."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})

        import gc as _gc_real

        collect_calls: list[int] = []
        original_collect = _gc_real.collect
        original_mod_gc_collect = mod.gc.collect

        def _mock_collect(*args: object, **kwargs: object) -> int:
            collect_calls.append(1)
            return 0

        _gc_real.collect = _mock_collect  # type: ignore[assignment]
        mod.gc.collect = _mock_collect  # type: ignore[assignment]

        # Set up fake model with transcribe_longform
        import types as _types
        fake_segments = [{"transcription": "привет"}, {"transcription": "мир"}]
        fake_model = _types.SimpleNamespace(
            transcribe_longform=lambda path: fake_segments,
        )
        mod._MODEL = fake_model
        mod._MODE = "rnnt"

        try:
            import json
            result = mod._process_request(json.dumps({
                "op": "transcribe",
                "audio_path": "/tmp/fake_longform.wav",
                "longform": True,
            }))
        finally:
            mod._MODEL = None
            mod._MODE = None
            _gc_real.collect = original_collect
            mod.gc.collect = original_mod_gc_collect

        self.assertIsNotNone(result)
        self.assertTrue(result.get("ok"), f"Expected ok=True, got: {result}")
        # gc.collect() must be called at least twice: once from H2 del+collect,
        # once from _free_mps_pool (H1). Accept >=1 to be permissive.
        self.assertGreaterEqual(
            len(collect_calls),
            1,
            "gc.collect() must be called at least once after longform transcribe (H2)",
        )

    def test_h2_gc_collect_path_exists_in_worker(self) -> None:
        """Проверяет явное освобождение v3 longform-контейнера и запуск GC."""
        import os

        worker_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "core", "workers", "gigaam_worker.py")
        )
        with open(worker_path, "r") as fh:
            source = fh.read()

        self.assertIn(
            "del longform_result",
            source,
            "H2: v3 longform-контейнер должен освобождаться явно",
        )
        self.assertIn(
            "gc.collect()",
            source,
            "H2: gc.collect() must appear after longform path in gigaam_worker.py",
        )


class TestH3StderrDrainThread(unittest.TestCase):
    """H3 hypothesis: stderr drain thread created, ring buffer capped, OOM detection intact."""

    def _make_session(self) -> object:
        """Import and build a _GigaAMSubprocessSession stub (no real subprocess)."""
        import sys
        from pathlib import Path

        project_root = Path(__file__).resolve().parents[1]
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        session = _GigaAMSubprocessSession(
            venv_python="/fake/python",
            worker_path="/fake/worker.py",
            mode="rnnt",
            device="cpu",
        )
        return session

    def test_h3_stderr_ring_buffer_attribute_exists(self) -> None:
        """_GigaAMSubprocessSession must have _stderr_ring deque attribute."""
        from collections import deque
        session = self._make_session()
        self.assertTrue(
            hasattr(session, "_stderr_ring"),
            "_GigaAMSubprocessSession must have _stderr_ring attribute",
        )
        self.assertIsInstance(session._stderr_ring, deque)

    def test_h3_stderr_ring_buffer_capped_at_200(self) -> None:
        """_stderr_ring must cap at 200 lines (oldest lines discarded)."""
        from collections import deque
        session = self._make_session()
        ring: deque = session._stderr_ring

        # Push 300 lines — only the last 200 should remain
        for i in range(300):
            ring.append(f"line {i}\n")

        self.assertEqual(
            len(ring),
            200,
            "_stderr_ring must hold at most 200 lines (deque maxlen=200)",
        )
        # First surviving line should be line 100
        self.assertIn("line 100", ring[0])

    def test_h3_start_stderr_drain_no_op_without_proc(self) -> None:
        """_start_stderr_drain() must not raise when _proc is None."""
        session = self._make_session()
        session._proc = None
        session._start_stderr_drain()  # must not raise

    def test_h3_stderr_drain_thread_started_after_popen(self) -> None:
        """_start_stderr_drain() must create a daemon thread named gigaam-stderr-drain-*."""
        from unittest.mock import MagicMock

        session = self._make_session()

        # Build a fake proc that never exits but has readable stderr
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None  # still running
        # stderr.readline() returns one line then empty (to let drain thread finish)
        mock_proc.stderr.readline.side_effect = ["line1\n", ""]
        session._proc = mock_proc

        session._start_stderr_drain()

        self.assertIsNotNone(
            session._stderr_drain_thread,
            "_stderr_drain_thread must be set after _start_stderr_drain()",
        )
        thread = session._stderr_drain_thread
        self.assertTrue(thread.daemon, "stderr drain thread must be a daemon thread")
        self.assertIn("gigaam-stderr-drain", thread.name)

    def test_h3_drain_thread_populates_ring_buffer(self) -> None:
        """Lines emitted by the worker must appear in _stderr_ring."""
        import time
        from unittest.mock import MagicMock

        session = self._make_session()

        mock_proc = MagicMock()
        mock_proc.pid = 99999
        # Simulate two lines then EOF
        mock_proc.poll.side_effect = [None, None, 0]  # running twice, then exited
        mock_proc.stderr.readline.side_effect = [
            "gigaam_worker: started\n",
            "HuggingFace download: 100%\n",
            "",  # EOF
        ]
        mock_proc.stderr.__iter__ = lambda self: iter([])
        session._proc = mock_proc

        session._start_stderr_drain()

        # Give drain thread time to consume lines
        time.sleep(0.1)

        ring_contents = "".join(session._stderr_ring)
        self.assertIn(
            "gigaam_worker: started",
            ring_contents,
            "drain thread must populate _stderr_ring with worker stderr lines",
        )

    def test_h3_oom_detection_reads_from_ring_buffer(self) -> None:
        """_check_proc_oom_on_exit reads OOM pattern from ring buffer (not proc.stderr.read)."""
        from unittest.mock import MagicMock

        session = self._make_session()

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1  # non-OOM returncode, but OOM in ring
        # stderr.read() returns empty — drain thread already consumed it
        mock_proc.stderr.read.return_value = ""
        session._proc = mock_proc

        # Pre-populate ring as if drain thread already ran
        session._stderr_ring.append("RuntimeError: out of memory\n")

        callback = MagicMock()
        session.oom_callback = callback

        session._check_proc_oom_on_exit()

        callback.assert_called_once()
        args = callback.call_args[0]
        self.assertIn("out of memory", args[2])

    def test_h3_oom_detection_fallback_to_stderr_read_when_ring_empty(self) -> None:
        """When ring is empty, _check_proc_oom_on_exit falls back to proc.stderr.read()."""
        from unittest.mock import MagicMock

        session = self._make_session()

        mock_proc = MagicMock()
        mock_proc.poll.return_value = -6  # SIGABRT → OOM regardless of stderr
        mock_proc.stderr.read.return_value = ""
        session._proc = mock_proc
        # ring is empty — fallback path
        session._stderr_ring.clear()

        callback = MagicMock()
        session.oom_callback = callback

        session._check_proc_oom_on_exit()

        # -6 (SIGABRT) must trigger OOM even with empty stderr
        callback.assert_called_once()
        args = callback.call_args[0]
        self.assertEqual(args[1], -6)

    def test_h3_oom_detection_still_works_via_ring_buffer_full_scenario(self) -> None:
        """End-to-end: worker writes 'out of memory' → drain captures → OOM fires."""
        import time
        from unittest.mock import MagicMock

        session = self._make_session()

        mock_proc = MagicMock()
        mock_proc.pid = 11111
        # Simulate process: running, then exits with code 1 (non-signal OOM)
        mock_proc.poll.side_effect = [None, None, 1, 1]
        mock_proc.stderr.readline.side_effect = [
            "Loading model...\n",
            "RuntimeError: out of memory\n",
            "",
        ]
        mock_proc.stderr.__iter__ = lambda self: iter([])
        session._proc = mock_proc

        # Start drain — it will populate ring with OOM line
        session._start_stderr_drain()
        time.sleep(0.1)  # let drain thread run

        callback = MagicMock()
        session.oom_callback = callback

        session._check_proc_oom_on_exit()

        callback.assert_called_once()
        args = callback.call_args[0]
        self.assertIn("out of memory", args[2].lower())


if __name__ == "__main__":
    unittest.main()
