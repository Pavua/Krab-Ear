"""
Regression tests for W1441 finding #4 CRIT:
Duplicate _safe_float definitions — 1-arg shadow removed (W1442).
"""

import ast
import math
import sys
import os
import unittest

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.audio_quality import _safe_float, AudioQualityAnalyzer  # noqa: E402


class TestSafeFloatAccepts2Args(unittest.TestCase):
    """_safe_float must accept 2-arg signature without TypeError."""

    def test_safe_float_accepts_2_args(self):
        """_safe_float(value, default) must not raise TypeError."""
        result = _safe_float(float("nan"), 1.0)
        self.assertEqual(result, 1.0)

    def test_safe_float_returns_finite_as_is(self):
        result = _safe_float(3.14, 0.0)
        self.assertAlmostEqual(result, 3.14)

    def test_safe_float_replaces_inf(self):
        result = _safe_float(float("inf"), 99.0)
        self.assertEqual(result, 99.0)

    def test_safe_float_replaces_neg_inf(self):
        result = _safe_float(float("-inf"), -1.0)
        self.assertEqual(result, -1.0)

    def test_safe_float_default_is_zero(self):
        """1-arg form (default=0.0) must still work for the 1-arg callers."""
        result = _safe_float(float("nan"))
        self.assertEqual(result, 0.0)


class TestAudioQualityAnalyzeWithSilenceNoCrash(unittest.TestCase):
    """Integration smoke — _safe_float(silence_ratio, 1.0) call at line 159."""

    def _make_analyzer(self):
        return AudioQualityAnalyzer()

    def test_audio_quality_analyze_with_silence_no_crash(self):
        """analyze() on a fully-silent signal must complete without TypeError."""
        analyzer = self._make_analyzer()
        silent_audio = np.zeros(16000, dtype=np.float32)
        report = analyzer.analyze(silent_audio, 16000)
        # silence_ratio should be 1.0 (all silent frames)
        self.assertAlmostEqual(report.silence_ratio, 1.0, places=2)
        self.assertIsInstance(report.quality_score, str)

    def test_audio_quality_analyze_mixed_signal_no_crash(self):
        """analyze() on a real signal also mustn't crash."""
        analyzer = self._make_analyzer()
        t = np.linspace(0, 1, 16000, dtype=np.float32)
        audio = (np.sin(2 * math.pi * 440 * t) * 0.5).astype(np.float32)
        report = analyzer.analyze(audio, 16000)
        self.assertIn(report.quality_score, {"excellent", "good", "fair", "poor"})


class TestNoDuplicateSafeFloatDefinitions(unittest.TestCase):
    """AST scan — exactly ONE def _safe_float must exist in audio_quality.py."""

    def _source_path(self):
        import core.audio_quality as m
        return m.__file__.replace(".pyc", ".py")

    def test_no_duplicate_safe_float_definitions(self):
        src = open(self._source_path()).read()
        tree = ast.parse(src)
        defs = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_safe_float"
        ]
        self.assertEqual(
            len(defs), 1,
            f"Expected exactly 1 _safe_float definition, found {len(defs)}: "
            f"lines {[d.lineno for d in defs]}"
        )

    def test_surviving_definition_has_default_param(self):
        """The surviving definition must accept a 'default' parameter."""
        import core.audio_quality as m
        src = open(self._source_path()).read()
        tree = ast.parse(src)
        defs = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_safe_float"
        ]
        self.assertEqual(len(defs), 1)
        arg_names = [a.arg for a in defs[0].args.args]
        self.assertIn("default", arg_names, "Surviving _safe_float must have 'default' param")


if __name__ == "__main__":
    unittest.main()
