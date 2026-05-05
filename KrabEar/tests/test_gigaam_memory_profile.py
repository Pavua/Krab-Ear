"""Tests for GigaAM worker memory profiling instrumentation (Phase C C.1).

These tests verify the opt-in tracing mechanism introduced in gigaam_worker.py.
They do NOT load the actual GigaAM model (requires .venv_gigaam + gigaam package),
and they do NOT execute any subprocess. Pure unit tests for env-var detection.

Run:
    PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest \
        KrabEar/tests/test_gigaam_memory_profile.py -q
"""

from __future__ import annotations

import importlib
import os
import sys
import tracemalloc
import types
import unittest


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
        import gigaam_worker as mod  # type: ignore[import]
    except ImportError as exc:
        raise unittest.SkipTest(
            f"gigaam_worker import failed (expected in isolated venv): {exc}"
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

        # Patch sys.modules so `import torch` inside _free_mps_pool finds our stub
        sys.modules["torch"] = fake_torch  # type: ignore[assignment]
        try:
            mod._free_mps_pool()
        finally:
            sys.modules.pop("torch", None)

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
        """_free_mps_pool() must never raise even when torch is absent."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})

        # Remove torch from sys.modules to simulate missing torch
        saved_torch = sys.modules.pop("torch", None)
        try:
            # Must not raise
            mod._free_mps_pool()
        finally:
            if saved_torch is not None:
                sys.modules["torch"] = saved_torch

    def test_free_mps_pool_no_raise_when_empty_cache_missing(self) -> None:
        """_free_mps_pool() must not raise if torch.mps lacks empty_cache attribute."""
        mod = _reload_worker_module({"KRAB_EAR_TRACE_GIGAAM_MEM": None})

        # torch.mps without empty_cache
        fake_mps = types.SimpleNamespace()  # no empty_cache attribute
        fake_torch = types.SimpleNamespace(mps=fake_mps)

        sys.modules["torch"] = fake_torch  # type: ignore[assignment]
        try:
            mod._free_mps_pool()  # must not raise
        finally:
            sys.modules.pop("torch", None)


if __name__ == "__main__":
    unittest.main()
