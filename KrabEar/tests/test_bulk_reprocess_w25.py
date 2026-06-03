"""wave-25 tests: bulk_reprocess / auto_dedup / usage_tracker hardening.

Covers the five wave-25 fixes:

  D1 (HIGH) — BulkReprocessor recording-guard is actually wired:
      * BulkReprocessor.reprocess() raises while a recording is active (anti-SIGSEGV).
      * service.py constructs BulkReprocessor with is_recording_fn= (source assertion).
      * _handle_bulk_reprocess_start translates that RuntimeError into a structured
        {"ok": False, "reason": "recording_active"} IPC response.

  D2 (MED) — bulk_reprocess_start is in the HEAVY throttle bucket (≤5/min).

  D3 (MED) — _handle_bulk_reprocess_start rejects NaN/Inf/out-of-range threshold
      with {"ok": False, "reason": "invalid_threshold"} BEFORE calling reprocess().

  D4 (MED) — recording_core_service._sanitize_dedup_threshold clamps a negative /
      NaN / out-of-range auto_dedup_threshold back to the safe default (so a
      negative threshold no longer flags EVERY recording as a duplicate).
      Also: _handle_run_deduplication rejects a NaN/negative threshold.

  D5 (MED) — get_usage_stats is wired: UsageTracker.get_usage_stats() returns a
      non-None dict, and _handle_get_usage_stats delegates to it.

These tests bind the REAL BackendService handler methods onto a lightweight
stand-in (no full-service bootstrap, no MLX execution) so they validate the
live handler code rather than a copy.  Importing backend.service is ubuntu-safe:
core/engine.py guards `import mlx_whisper` with try/except (-> None when absent),
so no test here depends on mlx_whisper being importable.
"""
from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.bulk_reprocess import BulkReprocessor
from backend.usage_tracker import UsageTracker
from backend.recording_core_service import (
    _sanitize_dedup_threshold,
    _DEFAULT_DEDUP_THRESHOLD,
)
from backend.ipc_throttle import HEAVY_METHODS, IPCThrottle
from backend.service import BackendService


# ---------------------------------------------------------------------------
# Lightweight stand-in that borrows the REAL BackendService handler methods.
# ---------------------------------------------------------------------------

class _HandlerHarness:
    """Minimal object onto which real BackendService handlers are bound.

    Only the attributes the handlers under test actually read are provided.
    Binding via `BackendService._handle_X.__get__(self)` runs the real handler
    body — no copy, no drift, no full-service bootstrap.
    """

    def __init__(self, privacy_mode: bool = False):
        self.store = MagicMock()
        self._semantic_searcher = MagicMock()
        self._bulk_reprocessor = MagicMock()
        self._auto_deduplicator = MagicMock()
        self._usage_tracker = MagicMock()
        self._settings_data: dict = {"privacy_mode_enabled": privacy_mode}

    def _cached_settings(self) -> dict:
        return self._settings_data

    def _get_runtime_setting(self, key: str, default):
        """Mirror BackendService._get_runtime_setting for tests that bind real handlers."""
        try:
            return self._cached_settings().get(key, default)
        except Exception:
            return default

    # Bind real handlers as methods.
    def handle_bulk_reprocess_start(self, params):
        return BackendService._handle_bulk_reprocess_start.__get__(self)(params)

    def handle_run_deduplication(self, params):
        return BackendService._handle_run_deduplication.__get__(self)(params)

    def handle_get_usage_stats(self, params):
        return BackendService._handle_get_usage_stats.__get__(self)(params)


def _make_recording_bulk_reprocessor(is_recording: bool) -> BulkReprocessor:
    """Real BulkReprocessor wired with a controllable is_recording_fn."""
    store = MagicMock()
    store._load_active_items_unlocked = MagicMock(return_value=[])
    store._lock = MagicMock(return_value=contextlib.nullcontext())
    return BulkReprocessor(
        store=store,
        transcriber=MagicMock(),
        version_manager=MagicMock(),
        is_recording_fn=lambda: is_recording,
    )


# ===========================================================================
# D1 — recording guard
# ===========================================================================

class TestD1RecordingGuard(unittest.TestCase):
    """HIGH: bulk reprocess must refuse while a recording is active."""

    def test_reprocess_raises_while_recording(self):
        """Real BulkReprocessor.reprocess() raises RuntimeError when recording."""
        br = _make_recording_bulk_reprocessor(is_recording=True)
        with self.assertRaises(RuntimeError) as ctx:
            br.reprocess()
        self.assertIn("active recording", str(ctx.exception))

    def test_reprocess_proceeds_when_not_recording(self):
        """No false positive: empty store + not recording -> total=0, no raise."""
        br = _make_recording_bulk_reprocessor(is_recording=False)
        result = br.reprocess()
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["cancelled"])

    def test_handler_returns_recording_active(self):
        """_handle_bulk_reprocess_start maps the RuntimeError to a structured error."""
        harness = _HandlerHarness()
        harness._bulk_reprocessor.reprocess.side_effect = RuntimeError(
            "bulk_reprocess refused: active recording in progress"
        )
        result = harness.handle_bulk_reprocess_start({})
        self.assertEqual(result, {"ok": False, "reason": "recording_active"})

    def test_handler_reraises_unrelated_runtime_error(self):
        """A RuntimeError unrelated to recording is NOT swallowed."""
        harness = _HandlerHarness()
        harness._bulk_reprocessor.reprocess.side_effect = RuntimeError("disk on fire")
        with self.assertRaises(RuntimeError):
            harness.handle_bulk_reprocess_start({})

    def test_service_source_wires_is_recording_fn(self):
        """service.py constructs BulkReprocessor with is_recording_fn= (guard is live)."""
        service_path = PROJECT_ROOT / "backend" / "service.py"
        src = service_path.read_text(encoding="utf-8")
        # The constructor block must pass is_recording_fn — otherwise the guard is dead.
        self.assertIn("self._bulk_reprocessor = BulkReprocessor(", src)
        ctor_start = src.index("self._bulk_reprocessor = BulkReprocessor(")
        ctor_block = src[ctor_start:ctor_start + 600]
        self.assertIn("is_recording_fn=", ctor_block,
                      "BulkReprocessor must be constructed with is_recording_fn= (dead guard otherwise)")
        self.assertIn("is_recording", ctor_block)


# ===========================================================================
# D2 — rate limit
# ===========================================================================

class TestD2RateLimit(unittest.TestCase):
    """MED: bulk_reprocess_start must be a HEAVY-bucket method."""

    def test_in_heavy_methods(self):
        self.assertIn("bulk_reprocess_start", HEAVY_METHODS)

    def test_throttle_classifies_as_heavy(self):
        """Heavy bucket caps at 5/min: the 6th immediate call is throttled."""
        throttle = IPCThrottle()
        allowed = [throttle.check_rate("bulk_reprocess_start") for _ in range(6)]
        # First 5 allowed, 6th rejected (capacity 5 with empty refill window).
        self.assertEqual(allowed[:5], [True, True, True, True, True])
        self.assertFalse(allowed[5])


# ===========================================================================
# D3 — NaN / out-of-range threshold rejection in bulk_reprocess_start
# ===========================================================================

class TestD3ThresholdValidation(unittest.TestCase):
    """MED: NaN/Inf/out-of-range threshold must be rejected before reprocess()."""

    def setUp(self):
        self.harness = _HandlerHarness()
        self.harness._bulk_reprocessor.reprocess.return_value = {
            "total": 0, "reprocessed": 0, "skipped": 0, "errors": [], "cancelled": False,
        }

    def test_nan_threshold_rejected(self):
        result = self.harness.handle_bulk_reprocess_start({"threshold": float("nan")})
        self.assertEqual(result, {"ok": False, "reason": "invalid_threshold"})
        self.harness._bulk_reprocessor.reprocess.assert_not_called()

    def test_inf_threshold_rejected(self):
        result = self.harness.handle_bulk_reprocess_start({"threshold": float("inf")})
        self.assertEqual(result, {"ok": False, "reason": "invalid_threshold"})
        self.harness._bulk_reprocessor.reprocess.assert_not_called()

    def test_negative_threshold_rejected(self):
        result = self.harness.handle_bulk_reprocess_start({"threshold": -1.0})
        self.assertEqual(result, {"ok": False, "reason": "invalid_threshold"})
        self.harness._bulk_reprocessor.reprocess.assert_not_called()

    def test_above_one_threshold_rejected(self):
        result = self.harness.handle_bulk_reprocess_start({"threshold": 1.5})
        self.assertEqual(result, {"ok": False, "reason": "invalid_threshold"})
        self.harness._bulk_reprocessor.reprocess.assert_not_called()

    def test_non_numeric_threshold_rejected(self):
        result = self.harness.handle_bulk_reprocess_start({"threshold": "abc"})
        self.assertEqual(result, {"ok": False, "reason": "invalid_threshold"})
        self.harness._bulk_reprocessor.reprocess.assert_not_called()

    def test_valid_threshold_passes_through(self):
        result = self.harness.handle_bulk_reprocess_start({"threshold": 0.6})
        self.harness._bulk_reprocessor.reprocess.assert_called_once()
        self.assertEqual(self.harness._bulk_reprocessor.reprocess.call_args.kwargs["threshold"], 0.6)
        self.assertNotIn("reason", result)

    def test_default_threshold_passes_through(self):
        self.harness.handle_bulk_reprocess_start({})
        self.harness._bulk_reprocessor.reprocess.assert_called_once()
        self.assertEqual(self.harness._bulk_reprocessor.reprocess.call_args.kwargs["threshold"], 0.7)


# ===========================================================================
# D4 — auto_dedup negative / NaN threshold sanitisation
# ===========================================================================

class TestD4AutoDedupThreshold(unittest.TestCase):
    """MED: negative/NaN auto_dedup_threshold must not flag every recording dup."""

    def test_negative_clamped_to_default(self):
        self.assertEqual(_sanitize_dedup_threshold(-1.0), _DEFAULT_DEDUP_THRESHOLD)

    def test_negative_is_not_passed_through(self):
        """The dangerous value (-1.0 -> sim >= -1.0 always True) is never honoured."""
        self.assertNotEqual(_sanitize_dedup_threshold(-1.0), -1.0)
        # And the result is a sane probability.
        self.assertGreaterEqual(_sanitize_dedup_threshold(-1.0), 0.0)
        self.assertLessEqual(_sanitize_dedup_threshold(-1.0), 1.0)

    def test_above_one_clamped_to_default(self):
        self.assertEqual(_sanitize_dedup_threshold(2.0), _DEFAULT_DEDUP_THRESHOLD)

    def test_nan_clamped_to_default(self):
        self.assertEqual(_sanitize_dedup_threshold(float("nan")), _DEFAULT_DEDUP_THRESHOLD)

    def test_inf_clamped_to_default(self):
        self.assertEqual(_sanitize_dedup_threshold(float("inf")), _DEFAULT_DEDUP_THRESHOLD)

    def test_non_numeric_clamped_to_default(self):
        self.assertEqual(_sanitize_dedup_threshold("not-a-number"), _DEFAULT_DEDUP_THRESHOLD)
        self.assertEqual(_sanitize_dedup_threshold(None), _DEFAULT_DEDUP_THRESHOLD)

    def test_valid_value_preserved(self):
        self.assertEqual(_sanitize_dedup_threshold(0.99), 0.99)
        self.assertEqual(_sanitize_dedup_threshold(0.0), 0.0)
        self.assertEqual(_sanitize_dedup_threshold(1.0), 1.0)

    def test_run_deduplication_handler_rejects_nan(self):
        """_handle_run_deduplication rejects a NaN threshold before delegating."""
        harness = _HandlerHarness()
        result = harness.handle_run_deduplication({"threshold": float("nan")})
        self.assertEqual(result, {"ok": False, "reason": "invalid_threshold"})
        harness._auto_deduplicator.handle_run_deduplication.assert_not_called()

    def test_run_deduplication_handler_rejects_negative(self):
        harness = _HandlerHarness()
        result = harness.handle_run_deduplication({"threshold": -0.5})
        self.assertEqual(result, {"ok": False, "reason": "invalid_threshold"})
        harness._auto_deduplicator.handle_run_deduplication.assert_not_called()

    def test_run_deduplication_handler_passes_valid(self):
        harness = _HandlerHarness()
        harness._auto_deduplicator.handle_run_deduplication.return_value = {"total_scanned": 0}
        result = harness.handle_run_deduplication({"threshold": 0.85})
        harness._auto_deduplicator.handle_run_deduplication.assert_called_once()
        forwarded = harness._auto_deduplicator.handle_run_deduplication.call_args.args[0]
        self.assertEqual(forwarded["threshold"], 0.85)
        self.assertEqual(result, {"total_scanned": 0})

    def test_run_deduplication_handler_no_threshold_ok(self):
        """Omitting threshold uses the downstream default (no rejection)."""
        harness = _HandlerHarness()
        harness._auto_deduplicator.handle_run_deduplication.return_value = {"total_scanned": 0}
        result = harness.handle_run_deduplication({})
        harness._auto_deduplicator.handle_run_deduplication.assert_called_once()
        self.assertEqual(result, {"total_scanned": 0})


# ===========================================================================
# D5 — get_usage_stats wiring
# ===========================================================================

class TestD5GetUsageStats(unittest.TestCase):
    """MED: get_usage_stats must return a real, non-None dict."""

    def test_usage_tracker_returns_non_none_dict(self):
        """Real UsageTracker.get_usage_stats() returns a structured dict."""
        with tempfile.TemporaryDirectory() as td:
            tracker = UsageTracker(data_dir=td)
            stats = tracker.get_usage_stats()
        self.assertIsNotNone(stats)
        self.assertIsInstance(stats, dict)
        for key in ("today", "this_week", "this_month", "all_time"):
            self.assertIn(key, stats)

    def test_usage_tracker_reflects_recorded_usage(self):
        with tempfile.TemporaryDirectory() as td:
            tracker = UsageTracker(data_dir=td)
            tracker.record_usage(duration_sec=12.5, word_count=20)
            stats = tracker.get_usage_stats()
        self.assertEqual(stats["all_time"]["recordings"], 1)
        self.assertEqual(stats["all_time"]["total_words"], 20)

    def test_handler_delegates_to_usage_tracker(self):
        """_handle_get_usage_stats returns exactly what UsageTracker provides (non-None)."""
        harness = _HandlerHarness()
        sentinel = {"today": {}, "all_time": {"recordings": 7}}
        harness._usage_tracker.get_usage_stats.return_value = sentinel
        result = harness.handle_get_usage_stats({})
        harness._usage_tracker.get_usage_stats.assert_called_once()
        self.assertIs(result, sentinel)
        self.assertIsNotNone(result)

    def test_handler_with_real_tracker_non_none(self):
        """End-to-end: real tracker through the real handler yields a non-None dict."""
        harness = _HandlerHarness()
        with tempfile.TemporaryDirectory() as td:
            harness._usage_tracker = UsageTracker(data_dir=td)
            result = harness.handle_get_usage_stats({})
        self.assertIsNotNone(result)
        self.assertIn("all_time", result)


if __name__ == "__main__":
    unittest.main()
