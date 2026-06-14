"""Wave-23 tests: finite-guard for duration_sec NaN/Inf in MetadataEnricher.

MED: NaN duration_sec → bare `NaN` token in IPC JSON (not RFC 8259, Swift
     JSONDecoder rejects the whole response).
LOW: Inf duration_sec → OverflowError in TranscriptionScorer.

Both must produce a finite, JSON-serialisable result after the fix.
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

from backend.metadata_enricher import MetadataEnricher, _safe_float  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(duration_sec=5.0, confidence=0.85, text="Hello world test."):
    return {
        "text": text,
        "duration_sec": duration_sec,
        "confidence": confidence,
        "has_diarization": False,
        "has_llm_enhancement": False,
    }


# ---------------------------------------------------------------------------
# _safe_float unit tests
# ---------------------------------------------------------------------------

class SafeFloatTestCase(unittest.TestCase):
    """Unit tests for the _safe_float helper."""

    def test_normal_float_passthrough(self):
        self.assertAlmostEqual(_safe_float(3.14), 3.14)

    def test_zero_passthrough(self):
        self.assertEqual(_safe_float(0.0), 0.0)

    def test_nan_returns_default(self):
        result = _safe_float(float("nan"))
        self.assertEqual(result, 0.0)
        self.assertTrue(math.isfinite(result))

    def test_inf_returns_default(self):
        result = _safe_float(float("inf"))
        self.assertEqual(result, 0.0)
        self.assertTrue(math.isfinite(result))

    def test_neg_inf_returns_default(self):
        result = _safe_float(float("-inf"))
        self.assertEqual(result, 0.0)
        self.assertTrue(math.isfinite(result))

    def test_custom_default(self):
        self.assertEqual(_safe_float(float("nan"), default=1.0), 1.0)

    def test_non_numeric_returns_default(self):
        self.assertEqual(_safe_float("oops"), 0.0)  # type: ignore[arg-type]

    def test_integer_input_ok(self):
        self.assertAlmostEqual(_safe_float(42), 42.0)


# ---------------------------------------------------------------------------
# MetadataEnricher NaN/Inf guard — MED finding
# ---------------------------------------------------------------------------

class MetadataEnricherNaNTestCase(unittest.TestCase):
    """MED: NaN duration_sec must produce finite metadata + valid IPC JSON."""

    def setUp(self):
        self.enricher = MetadataEnricher()

    def test_nan_duration_enriches_without_error(self):
        item = _make_item(duration_sec=float("nan"))
        result = self.enricher.enrich(item)
        self.assertIn("metadata", result)

    def test_nan_duration_metadata_fields_are_finite(self):
        item = _make_item(duration_sec=float("nan"))
        result = self.enricher.enrich(item)
        meta = result["metadata"]
        for key in ("speech_pace_wpm",):
            val = meta[key]
            self.assertTrue(
                math.isfinite(val),
                f"metadata['{key}'] = {val!r} is not finite",
            )

    def test_nan_duration_round_trips_json(self):
        """json.dumps(allow_nan=False) must not raise — MED core requirement."""
        item = _make_item(duration_sec=float("nan"))
        result = self.enricher.enrich(item)
        # This must not raise ValueError: "Out of range float values are not JSON compliant"
        serialised = json.dumps(result, allow_nan=False)
        self.assertIsInstance(serialised, str)
        self.assertNotIn("NaN", serialised)
        self.assertNotIn("Infinity", serialised)

    def test_nan_duration_no_overflow_error(self):
        """TranscriptionScorer must not OverflowError on NaN (guarded before call)."""
        item = _make_item(duration_sec=float("nan"))
        # Must complete without exception
        result = self.enricher.enrich(item)
        self.assertIn("metadata", result)


# ---------------------------------------------------------------------------
# MetadataEnricher Inf guard — LOW finding
# ---------------------------------------------------------------------------

class MetadataEnricherInfTestCase(unittest.TestCase):
    """LOW: +Inf/-Inf duration_sec must not OverflowError in TranscriptionScorer."""

    def setUp(self):
        self.enricher = MetadataEnricher()

    def test_positive_inf_duration_no_overflow(self):
        item = _make_item(duration_sec=float("inf"))
        result = self.enricher.enrich(item)
        self.assertIn("metadata", result)

    def test_positive_inf_duration_metadata_finite(self):
        item = _make_item(duration_sec=float("inf"))
        result = self.enricher.enrich(item)
        meta = result["metadata"]
        for key in ("speech_pace_wpm",):
            self.assertTrue(
                math.isfinite(meta[key]),
                f"metadata['{key}'] = {meta[key]!r} is not finite after +Inf input",
            )

    def test_positive_inf_round_trips_json(self):
        item = _make_item(duration_sec=float("inf"))
        result = self.enricher.enrich(item)
        serialised = json.dumps(result, allow_nan=False)
        self.assertNotIn("Infinity", serialised)
        self.assertNotIn("NaN", serialised)

    def test_negative_inf_duration_no_overflow(self):
        item = _make_item(duration_sec=float("-inf"))
        result = self.enricher.enrich(item)
        self.assertIn("metadata", result)

    def test_negative_inf_round_trips_json(self):
        item = _make_item(duration_sec=float("-inf"))
        result = self.enricher.enrich(item)
        serialised = json.dumps(result, allow_nan=False)
        self.assertNotIn("Infinity", serialised)
        self.assertNotIn("NaN", serialised)


# ---------------------------------------------------------------------------
# Combined: NaN confidence guard
# ---------------------------------------------------------------------------

class MetadataEnricherNaNConfidenceTestCase(unittest.TestCase):
    """NaN confidence must also be finite-guarded (guard is symmetric)."""

    def setUp(self):
        self.enricher = MetadataEnricher()

    def test_nan_confidence_enriches_without_error(self):
        item = _make_item(confidence=float("nan"))
        result = self.enricher.enrich(item)
        self.assertIn("metadata", result)

    def test_nan_confidence_round_trips_json(self):
        item = _make_item(confidence=float("nan"))
        result = self.enricher.enrich(item)
        serialised = json.dumps(result, allow_nan=False)
        self.assertNotIn("NaN", serialised)


# ---------------------------------------------------------------------------
# Regression: normal path still works
# ---------------------------------------------------------------------------

class MetadataEnricherNormalPathTestCase(unittest.TestCase):
    """Ensure the guard doesn't break the happy path."""

    def setUp(self):
        self.enricher = MetadataEnricher()

    def test_normal_item_enriched(self):
        item = _make_item(duration_sec=10.0, confidence=0.9)
        result = self.enricher.enrich(item)
        meta = result["metadata"]
        self.assertGreaterEqual(meta["word_count"], 0)
        self.assertIsInstance(meta["speech_pace_wpm"], float)
        self.assertTrue(math.isfinite(meta["speech_pace_wpm"]))

    def test_normal_item_json_safe(self):
        item = _make_item(duration_sec=10.0, confidence=0.9)
        result = self.enricher.enrich(item)
        json.dumps(result, allow_nan=False)  # must not raise


# ---------------------------------------------------------------------------
# Wave-23 GAP: non-numeric duration_sec/confidence (str / list / dict)
# ---------------------------------------------------------------------------

class MetadataEnricherNonNumericTestCase(unittest.TestCase):
    """The Wave-23 ``_safe_float`` guard accepts ``Any`` and coerces non-numeric
    input to the default — but the live call-site wrapped it in a bare
    ``float(item.get(...) or 0.0)`` that runs FIRST and raises on a non-numeric
    string/list before ``_safe_float`` is ever reached. A malformed
    ``enrich_recording`` IPC payload (``{"duration_sec": "abc"}``) therefore
    crashed the handler instead of degrading gracefully like NaN/Inf do.
    """

    def setUp(self):
        self.enricher = MetadataEnricher()

    def test_non_numeric_str_duration_does_not_raise(self):
        item = _make_item(duration_sec="abc")
        result = self.enricher.enrich(item)  # pre-fix: ValueError from inner float()
        self.assertIn("metadata", result)

    def test_non_numeric_str_confidence_does_not_raise(self):
        item = _make_item(confidence="oops")
        result = self.enricher.enrich(item)
        self.assertIn("metadata", result)

    def test_list_duration_does_not_raise(self):
        item = _make_item(duration_sec=["x"])  # pre-fix: TypeError from inner float()
        result = self.enricher.enrich(item)
        self.assertIn("metadata", result)

    def test_non_numeric_result_is_json_safe(self):
        item = _make_item(duration_sec="abc", confidence=["bad"])
        result = self.enricher.enrich(item)
        json.dumps(result, allow_nan=False)  # must not raise

    def test_numeric_string_still_parsed(self):
        # behaviour preservation: the old inner float() parsed numeric strings,
        # so "12.5"/"0.9" must keep coercing to numbers, not silently default.
        item = _make_item(duration_sec="12.5", confidence="0.9")
        result = self.enricher.enrich(item)
        self.assertIn("metadata", result)
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
