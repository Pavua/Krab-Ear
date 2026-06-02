"""Wave-19: NaN/Inf bypass fix — settings_validator + settings_service._coerce_bounded.

FINDING (MED): validate()'s range loop used `if parsed < min_v or parsed > max_v:` which
is False for NaN (all float comparisons with NaN return False), so NaN fell through to the
`else` branch and was stored unchanged.  NaN then serialises as non-RFC JSON that breaks
strict parsers and Swift JSONDecoder.  Same bypass existed in SettingsService._coerce_bounded
via max(min, min(NaN, max)) which propagates NaN unchanged.

FIX: explicit math.isfinite guard added in both code paths, resetting to `default`.
"""
import json
import math
import os
import sys
import unittest

_PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.settings_validator import SettingsValidator  # noqa: E402


class TestSettingsValidatorNaN(unittest.TestCase):
    """NaN / ±Inf must not survive validate()."""

    def setUp(self):
        self.v = SettingsValidator()

    # ------------------------------------------------------------------
    # NaN cases
    # ------------------------------------------------------------------

    def test_nan_confidence_threshold_replaced_by_default(self):
        result = self.v.validate({"notify_confidence_threshold": float("nan")})
        self.assertTrue(result.valid)
        val = result.fixed["notify_confidence_threshold"]
        self.assertTrue(math.isfinite(val), f"expected finite, got {val}")
        # Default in _RANGE_FIELDS is 0.5
        self.assertEqual(val, 0.5)

    def test_nan_call_budget_replaced_by_default(self):
        result = self.v.validate({"call_budget_usd": float("nan")})
        self.assertTrue(result.valid)
        val = result.fixed["call_budget_usd"]
        self.assertTrue(math.isfinite(val), f"expected finite, got {val}")
        # Default in _RANGE_FIELDS is 2.0
        self.assertEqual(val, 2.0)

    # ------------------------------------------------------------------
    # ±Inf cases
    # ------------------------------------------------------------------

    def test_pos_inf_replaced_by_default(self):
        result = self.v.validate({"notify_confidence_threshold": float("inf")})
        self.assertTrue(result.valid)
        val = result.fixed["notify_confidence_threshold"]
        self.assertTrue(math.isfinite(val), f"expected finite, got {val}")

    def test_neg_inf_replaced_by_default(self):
        result = self.v.validate({"call_budget_usd": float("-inf")})
        self.assertTrue(result.valid)
        val = result.fixed["call_budget_usd"]
        self.assertTrue(math.isfinite(val), f"expected finite, got {val}")

    # ------------------------------------------------------------------
    # Warning emitted
    # ------------------------------------------------------------------

    def test_nan_triggers_warning(self):
        result = self.v.validate({"notify_confidence_threshold": float("nan")})
        self.assertTrue(len(result.warnings) >= 1)
        self.assertTrue(
            any("notify_confidence_threshold" in w for w in result.warnings),
            f"no warning mentioning the field, got {result.warnings}",
        )

    # ------------------------------------------------------------------
    # Round-trip through json.dumps with allow_nan=False
    # ------------------------------------------------------------------

    def test_fixed_result_serialises_without_nan(self):
        """The fixed dict must be serialisable with strict JSON (allow_nan=False)."""
        payload = {
            "notify_confidence_threshold": float("nan"),
            "call_budget_usd": float("inf"),
        }
        result = self.v.validate(payload)
        # Must not raise — previously would if NaN slipped through
        try:
            serialised = json.dumps(result.fixed, allow_nan=False)
        except ValueError as exc:
            self.fail(f"json.dumps(allow_nan=False) raised: {exc}")
        # Sanity: round-trip back to Python
        roundtripped = json.loads(serialised)
        self.assertIn("notify_confidence_threshold", roundtripped)
        self.assertIn("call_budget_usd", roundtripped)

    # ------------------------------------------------------------------
    # Multiple non-finite fields in one call
    # ------------------------------------------------------------------

    def test_multiple_nonfinite_fields_all_fixed(self):
        payload = {
            "silence_guard_rms_threshold": float("nan"),
            "background_guard_min_peak": float("inf"),
            "notify_confidence_threshold": float("-inf"),
            "call_budget_usd": float("nan"),
        }
        result = self.v.validate(payload)
        self.assertTrue(result.valid)
        for key, val in result.fixed.items():
            if isinstance(val, float):
                self.assertTrue(
                    math.isfinite(val),
                    f"field '{key}' is still non-finite after validate(): {val}",
                )
        # Must serialise cleanly
        json.dumps(result.fixed, allow_nan=False)

    # ------------------------------------------------------------------
    # Normal in-range floats still pass through
    # ------------------------------------------------------------------

    def test_valid_float_unchanged(self):
        result = self.v.validate({"notify_confidence_threshold": 0.75})
        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["notify_confidence_threshold"], 0.75)
        # No warning about this field
        self.assertFalse(
            any("notify_confidence_threshold" in w for w in result.warnings),
            f"unexpected warning for valid value: {result.warnings}",
        )


class TestCoerceBoundedNaN(unittest.TestCase):
    """SettingsService._coerce_bounded must reject NaN/Inf."""

    def setUp(self):
        # Import SettingsService but we only need _coerce_bounded (static method)
        from backend.settings_service import SettingsService
        self._coerce_bounded = SettingsService._coerce_bounded

    def test_nan_returns_default_float(self):
        result = self._coerce_bounded(float("nan"), default=0.5, min_value=0.0, max_value=1.0)
        self.assertTrue(math.isfinite(result), f"expected finite, got {result}")
        self.assertEqual(result, 0.5)

    def test_pos_inf_returns_default_float(self):
        result = self._coerce_bounded(float("inf"), default=2.0, min_value=0.0, max_value=1000.0)
        self.assertTrue(math.isfinite(result), f"expected finite, got {result}")
        self.assertEqual(result, 2.0)

    def test_neg_inf_returns_default_float(self):
        result = self._coerce_bounded(float("-inf"), default=2.0, min_value=0.0, max_value=1000.0)
        self.assertTrue(math.isfinite(result), f"expected finite, got {result}")
        self.assertEqual(result, 2.0)

    def test_nan_with_int_default_still_int(self):
        # When default is int the coerce path is int(), float("nan") → int() raises ValueError
        # so the except branch fires — check it returns the int default unchanged.
        result = self._coerce_bounded(float("nan"), default=50, min_value=0, max_value=100)
        self.assertEqual(result, 50)
        self.assertIsInstance(result, int)

    def test_valid_float_clamped_normally(self):
        result = self._coerce_bounded(1.5, default=0.5, min_value=0.0, max_value=1.0)
        self.assertEqual(result, 1.0)

    def test_valid_float_in_range_unchanged(self):
        result = self._coerce_bounded(0.75, default=0.5, min_value=0.0, max_value=1.0)
        self.assertEqual(result, 0.75)

    def test_coerce_bounded_result_serialises_cleanly(self):
        """Result of _coerce_bounded must be JSON-safe even for NaN inputs."""
        for bad in (float("nan"), float("inf"), float("-inf")):
            result = self._coerce_bounded(bad, default=0.5, min_value=0.0, max_value=1.0)
            try:
                json.dumps({"v": result}, allow_nan=False)
            except ValueError as exc:
                self.fail(f"json.dumps raised for input {bad}: {exc}")


if __name__ == "__main__":
    unittest.main()
