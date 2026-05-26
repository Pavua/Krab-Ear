"""Tests for audio_analytics_service check_audio_duplicate migration to equals().

W1125 F2 HIGH: verify that handle_check_audio_duplicate uses equals() instead
of deprecated compare() and that the IPC response shape is correct.

Two cases:
  1. Exact match — is_duplicate=True, similarity=1.0
  2. Different audio — is_duplicate=False, similarity=0.0
"""

from __future__ import annotations

import ast
import os
import sys
import unittest

# Path setup for standalone and PYTHONPATH-based runs
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import numpy as np

from backend.audio_analytics_service import AudioAnalyticsService
from core.audio_fingerprint import AudioFingerprinter


# ---------------------------------------------------------------------------
# Minimal stubs (no heavy dependencies)
# ---------------------------------------------------------------------------

class _FakeStore:
    """Minimal store stub — AudioAnalyticsService constructor needs it."""
    def __init__(self):
        self.items = []

    def get_all(self):
        return self.items


class _FakeConverter:
    pass


class _FakeQualityTrends:
    pass


class _FakeWordTimingAnalyzer:
    pass


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestCheckAudioDuplicateUsesEquals(unittest.TestCase):
    """Verify check_audio_duplicate uses equals() not compare()."""

    def setUp(self):
        self._store = _FakeStore()
        self._service = AudioAnalyticsService(
            audio_converter=_FakeConverter(),
            quality_trends=_FakeQualityTrends(),
            audio_fingerprinter=AudioFingerprinter(),
            word_timing_analyzer=_FakeWordTimingAnalyzer(),
            store=self._store,
        )

    def _make_tone(self, freq_hz: float, duration_s: float = 0.1, sr: int = 16000) -> list:
        """Generate a simple sine tone as list[float]."""
        t = np.linspace(0, duration_s, int(sr * duration_s), endpoint=False)
        return (np.sin(2 * np.pi * freq_hz * t) * 0.5).tolist()

    def test_exact_duplicate_returns_true(self):
        """Same audio twice => is_duplicate=True, similarity=1.0."""
        audio = self._make_tone(440.0)
        result = self._service.handle_check_audio_duplicate({
            "audio1": audio,
            "audio2": audio,
            "sample_rate": 16000,
        })

        self.assertIn("is_duplicate", result)
        self.assertIn("similarity", result)
        self.assertIn("fingerprint1", result)
        self.assertIn("fingerprint2", result)

        self.assertTrue(result["is_duplicate"],
                        "Same audio should be detected as duplicate")
        self.assertEqual(result["similarity"], 1.0,
                         "Backwards-compat similarity should be 1.0 for exact match")
        self.assertEqual(result["fingerprint1"], result["fingerprint2"])

    def test_different_audio_returns_false(self):
        """Different tones => is_duplicate=False, similarity=0.0."""
        audio1 = self._make_tone(440.0)   # A4
        audio2 = self._make_tone(880.0)   # A5 — very different features

        result = self._service.handle_check_audio_duplicate({
            "audio1": audio1,
            "audio2": audio2,
            "sample_rate": 16000,
        })

        self.assertFalse(result["is_duplicate"],
                         "Different audio should not be detected as duplicate")
        self.assertEqual(result["similarity"], 0.0,
                         "Backwards-compat similarity should be 0.0 for non-duplicate")
        self.assertNotEqual(result["fingerprint1"], result["fingerprint2"])

    def test_similarity_field_is_deprecated_binary(self):
        """similarity field must be exactly 0.0 or 1.0 — no intermediate floats."""
        audio = self._make_tone(330.0)
        result = self._service.handle_check_audio_duplicate({
            "audio1": audio,
            "audio2": audio,
        })
        self.assertIn(result["similarity"], (0.0, 1.0),
                      "similarity must be binary (deprecated backwards-compat field)")


# ---------------------------------------------------------------------------
# AST check: ensure compare() is not called in audio_analytics_service.py
# ---------------------------------------------------------------------------

class TestNoCompareCallInHandler(unittest.TestCase):
    """Static AST check that handle_check_audio_duplicate no longer calls compare()."""

    def test_handler_does_not_call_compare(self):
        service_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "backend", "audio_analytics_service.py",
        )
        with open(service_path, "r", encoding="utf-8") as fh:
            source = fh.read()

        tree = ast.parse(source, filename=service_path)

        handler_body_linenos: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "handle_check_audio_duplicate":
                for child in ast.walk(node):
                    if hasattr(child, "lineno"):
                        handler_body_linenos.add(child.lineno)

        self.assertTrue(handler_body_linenos,
                        "Could not find handle_check_audio_duplicate function")

        compare_calls_in_handler: list[int] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "compare"
                and hasattr(node, "lineno")
                and node.lineno in handler_body_linenos
            ):
                compare_calls_in_handler.append(node.lineno)

        self.assertEqual(
            compare_calls_in_handler,
            [],
            f"handle_check_audio_duplicate still calls compare() at lines "
            f"{compare_calls_in_handler} — should use equals() (W1125 F2)",
        )

    def test_handler_calls_equals(self):
        """AST check that handle_check_audio_duplicate calls equals()."""
        service_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "backend", "audio_analytics_service.py",
        )
        with open(service_path, "r", encoding="utf-8") as fh:
            source = fh.read()

        tree = ast.parse(source, filename=service_path)

        handler_body_linenos: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "handle_check_audio_duplicate":
                for child in ast.walk(node):
                    if hasattr(child, "lineno"):
                        handler_body_linenos.add(child.lineno)

        equals_calls_in_handler: list[int] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "equals"
                and hasattr(node, "lineno")
                and node.lineno in handler_body_linenos
            ):
                equals_calls_in_handler.append(node.lineno)

        self.assertTrue(
            len(equals_calls_in_handler) >= 1,
            "handle_check_audio_duplicate must call equals() (W1125 F2)",
        )


# ---------------------------------------------------------------------------
# AudioFingerprinter.equals() unit tests
# ---------------------------------------------------------------------------

class TestAudioFingerprinterEquals(unittest.TestCase):
    """Direct unit tests for the new equals() method."""

    def setUp(self):
        self._fp = AudioFingerprinter()
        sr = 16000
        t = np.linspace(0, 0.1, int(sr * 0.1), endpoint=False)
        self._audio_a = (np.sin(2 * np.pi * 440 * t) * 0.5).astype(np.float32)
        self._audio_b = (np.sin(2 * np.pi * 880 * t) * 0.5).astype(np.float32)

    def test_same_fingerprint_equals_true(self):
        fp = self._fp.fingerprint(self._audio_a, 16000)
        self.assertTrue(self._fp.equals(fp, fp))

    def test_different_fingerprint_equals_false(self):
        fp_a = self._fp.fingerprint(self._audio_a, 16000)
        fp_b = self._fp.fingerprint(self._audio_b, 16000)
        self.assertFalse(self._fp.equals(fp_a, fp_b))

    def test_compare_shim_returns_binary_only(self):
        """compare() must return only 1.0 or 0.0 — no intermediate values."""
        fp_a = self._fp.fingerprint(self._audio_a, 16000)
        fp_b = self._fp.fingerprint(self._audio_b, 16000)
        result_diff = self._fp.compare(fp_a, fp_b)
        result_same = self._fp.compare(fp_a, fp_a)
        self.assertIn(result_diff, (0.0, 1.0))
        self.assertIn(result_same, (0.0, 1.0))
        self.assertEqual(result_same, 1.0)
        self.assertEqual(result_diff, 0.0)


if __name__ == "__main__":
    unittest.main()
