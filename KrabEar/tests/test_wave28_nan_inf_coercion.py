"""Wave-28 MED: NaN/Inf coercion in HistoryItem.from_dict + CostEstimator.

FIX A1 — models.py: from_dict accepts NaN/Inf for confidence and
          audio_duration_sec; these propagate downstream and poison IPC JSON.
FIX A2 — cost_estimator.py: NaN/Inf duration_sec bypasses negative guard
          and returns NaN cost values in IPC responses.
"""

from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class HistoryItemNanCoercionTestCase(unittest.TestCase):
    """HistoryItem.from_dict must sanitise non-finite floats."""

    def _item(self, **kw):
        from backend.models import HistoryItem
        base = {"id": "x", "ts": "2024-01-01T00:00:00+00:00", "text": "hi"}
        base.update(kw)
        return HistoryItem.from_dict(base)

    # --- confidence ---

    def test_nan_confidence_becomes_zero(self):
        item = self._item(confidence=float("nan"))
        self.assertEqual(item.confidence, 0.0)

    def test_inf_confidence_becomes_zero(self):
        item = self._item(confidence=float("inf"))
        self.assertEqual(item.confidence, 0.0)

    def test_neg_inf_confidence_becomes_zero(self):
        item = self._item(confidence=float("-inf"))
        self.assertEqual(item.confidence, 0.0)

    def test_confidence_above_one_clamped(self):
        item = self._item(confidence=1.5)
        self.assertEqual(item.confidence, 1.0)

    def test_confidence_below_zero_clamped(self):
        item = self._item(confidence=-0.3)
        self.assertEqual(item.confidence, 0.0)

    def test_valid_confidence_preserved(self):
        item = self._item(confidence=0.85)
        self.assertAlmostEqual(item.confidence, 0.85)

    def test_none_confidence_stays_none(self):
        item = self._item()  # no confidence key
        self.assertIsNone(item.confidence)

    # --- audio_duration_sec ---

    def test_nan_duration_becomes_zero(self):
        item = self._item(audio_duration_sec=float("nan"))
        self.assertEqual(item.audio_duration_sec, 0.0)

    def test_inf_duration_becomes_zero(self):
        item = self._item(audio_duration_sec=float("inf"))
        self.assertEqual(item.audio_duration_sec, 0.0)

    def test_neg_inf_duration_becomes_zero(self):
        item = self._item(audio_duration_sec=float("-inf"))
        self.assertEqual(item.audio_duration_sec, 0.0)

    def test_valid_duration_preserved(self):
        item = self._item(audio_duration_sec=42.5)
        self.assertAlmostEqual(item.audio_duration_sec, 42.5)

    def test_none_duration_stays_none(self):
        item = self._item()  # no audio_duration_sec key
        self.assertIsNone(item.audio_duration_sec)

    # --- JSON round-trip safety ---

    def test_nan_confidence_to_dict_json_safe(self):
        """json.dumps with allow_nan=False must not raise after from_dict coercion."""
        item = self._item(confidence=float("nan"), audio_duration_sec=float("inf"))
        d = item.to_dict()
        # This would raise ValueError if NaN/Inf survived
        serialized = json.dumps(d, allow_nan=False)
        self.assertIsInstance(serialized, str)

    def test_valid_item_json_round_trip(self):
        item = self._item(confidence=0.75, audio_duration_sec=60.0)
        d = item.to_dict()
        serialized = json.dumps(d, allow_nan=False)
        reloaded = json.loads(serialized)
        self.assertAlmostEqual(reloaded["confidence"], 0.75)
        self.assertAlmostEqual(reloaded["audio_duration_sec"], 60.0)


class CostEstimatorNanGuardTestCase(unittest.TestCase):
    """CostEstimator must return safe zero-cost for non-finite duration_sec."""

    def setUp(self):
        from backend.cost_estimator import CostEstimator
        self.est = CostEstimator()

    def test_nan_duration_returns_zero_cost(self):
        result = self.est.estimate_cost(duration_sec=float("nan"))
        self.assertEqual(result.compute_time_sec, 0.0)
        self.assertEqual(result.memory_mb, 0.0)
        self.assertEqual(result.disk_mb, 0.0)
        self.assertEqual(result.total_relative_cost, 0.0)

    def test_inf_duration_returns_zero_cost(self):
        result = self.est.estimate_cost(duration_sec=float("inf"))
        self.assertEqual(result.compute_time_sec, 0.0)
        self.assertEqual(result.total_relative_cost, 0.0)

    def test_neg_inf_duration_returns_zero_cost(self):
        result = self.est.estimate_cost(duration_sec=float("-inf"))
        self.assertEqual(result.compute_time_sec, 0.0)

    def test_nan_result_is_finite(self):
        result = self.est.estimate_cost(duration_sec=float("nan"))
        self.assertTrue(math.isfinite(result.compute_time_sec))
        self.assertTrue(math.isfinite(result.memory_mb))
        self.assertTrue(math.isfinite(result.disk_mb))
        self.assertTrue(math.isfinite(result.total_relative_cost))

    def test_nan_result_json_serialisable(self):
        result = self.est.estimate_cost(duration_sec=float("nan"))
        d = {
            "compute_time_sec": result.compute_time_sec,
            "memory_mb": result.memory_mb,
            "disk_mb": result.disk_mb,
            "total_relative_cost": result.total_relative_cost,
            "features_cost": result.features_cost,
        }
        serialized = json.dumps(d, allow_nan=False)
        self.assertIsInstance(serialized, str)

    def test_normal_duration_still_works(self):
        result = self.est.estimate_cost(duration_sec=60.0, quality="balanced")
        self.assertGreater(result.compute_time_sec, 0.0)
        self.assertGreater(result.memory_mb, 0.0)

    def test_negative_duration_raises(self):
        with self.assertRaises(ValueError):
            self.est.estimate_cost(duration_sec=-1.0)

    def test_batch_with_nan_duration_item(self):
        """estimate_batch_cost must handle NaN in individual items gracefully."""
        files = [
            {"duration_sec": float("nan")},
            {"duration_sec": 30.0},
        ]
        result = self.est.estimate_batch_cost(files)
        # Total compute should equal 30s item only (nan item returns 0)
        self.assertTrue(math.isfinite(result["total_compute_time_sec"]))
        self.assertGreater(result["total_compute_time_sec"], 0.0)
        serialized = json.dumps(result, allow_nan=False)
        self.assertIsInstance(serialized, str)


if __name__ == "__main__":
    unittest.main()
