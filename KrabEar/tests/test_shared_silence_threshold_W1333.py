"""Tests for W1333: shared -40 dB silence threshold constant.

Проверяет что:
1. Все 6 файлов ссылаются на константы из core.silence_constants, а не
   задают -40.0 вручную.
2. SILENCE_THRESHOLD_DB == -40.0.
3. SILENCE_THRESHOLD_AMP соответствует 10^(DB/20).
"""

from __future__ import annotations

import ast
import math
import sys
import os
import unittest
from pathlib import Path

# --- path bootstrap ---------------------------------------------------------
_HERE = Path(__file__).resolve()
_PROJECT_ROOT = _HERE.parent.parent.parent  # repo root (contains KrabEar/)
_KRAB_EAR = _PROJECT_ROOT / "KrabEar"
if str(_KRAB_EAR) not in sys.path:
    sys.path.insert(0, str(_KRAB_EAR))
# ---------------------------------------------------------------------------

from core.silence_constants import SILENCE_THRESHOLD_AMP, SILENCE_THRESHOLD_DB


class TestSilenceThresholdValues(unittest.TestCase):
    """Константы имеют правильные числовые значения."""

    def test_silence_threshold_db_value_is_minus_40(self) -> None:
        self.assertEqual(SILENCE_THRESHOLD_DB, -40.0)

    def test_silence_threshold_amp_matches_db(self) -> None:
        expected = 10.0 ** (SILENCE_THRESHOLD_DB / 20.0)
        self.assertAlmostEqual(SILENCE_THRESHOLD_AMP, expected, places=10)

    def test_silence_threshold_amp_is_0_01(self) -> None:
        # Convenience cross-check: 10^(-40/20) == 0.01
        self.assertAlmostEqual(SILENCE_THRESHOLD_AMP, 0.01, places=10)


class TestAllModulesReferenceSharedConstant(unittest.TestCase):
    """AST-анализ: ни один из 6 файлов не содержит литерал -40.0."""

    _TARGET_FILES = [
        _KRAB_EAR / "core" / "silence_detector.py",
        _KRAB_EAR / "core" / "smart_silence_skipper.py",
        _KRAB_EAR / "backend" / "realtime_silence_filter.py",
        _KRAB_EAR / "core" / "audio_chunker.py",
        _KRAB_EAR / "backend" / "call_silence_probe.py",
        _KRAB_EAR / "backend" / "audio_analytics_service.py",
    ]

    def _collect_float_literals(self, path: Path) -> list[float]:
        """Возвращает все float-литералы в исходнике через AST."""
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        literals: list[float] = []
        for node in ast.walk(tree):
            # Python ≤3.7: ast.Num; 3.8+: ast.Constant
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                literals.append(node.value)
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
                if isinstance(node.operand, ast.Constant) and isinstance(
                    node.operand.value, (int, float)
                ):
                    literals.append(-float(node.operand.value))
        return literals

    def test_no_minus_40_literal_in_silence_detector(self) -> None:
        literals = self._collect_float_literals(self._TARGET_FILES[0])
        self.assertNotIn(-40.0, literals,
                         "silence_detector.py still has bare -40.0 literal")

    def test_no_minus_40_literal_in_smart_silence_skipper(self) -> None:
        literals = self._collect_float_literals(self._TARGET_FILES[1])
        self.assertNotIn(-40.0, literals,
                         "smart_silence_skipper.py still has bare -40.0 literal")

    def test_no_minus_40_literal_in_realtime_silence_filter(self) -> None:
        literals = self._collect_float_literals(self._TARGET_FILES[2])
        self.assertNotIn(-40.0, literals,
                         "realtime_silence_filter.py still has bare -40.0 literal")

    def test_no_minus_40_literal_in_audio_chunker(self) -> None:
        literals = self._collect_float_literals(self._TARGET_FILES[3])
        self.assertNotIn(-40.0, literals,
                         "audio_chunker.py still has bare -40.0 literal")

    def test_no_minus_40_literal_in_call_silence_probe(self) -> None:
        literals = self._collect_float_literals(self._TARGET_FILES[4])
        self.assertNotIn(-40.0, literals,
                         "call_silence_probe.py still has bare -40.0 literal")

    def test_no_minus_40_literal_in_audio_analytics_service(self) -> None:
        literals = self._collect_float_literals(self._TARGET_FILES[5])
        self.assertNotIn(-40.0, literals,
                         "audio_analytics_service.py still has bare -40.0 literal")

    def test_all_modules_reference_shared_constant(self) -> None:
        """Интеграционный тест: все 6 файлов импортируют из silence_constants."""
        missing: list[str] = []
        for path in self._TARGET_FILES:
            source = path.read_text(encoding="utf-8")
            if "silence_constants" not in source:
                missing.append(path.name)
        self.assertFalse(
            missing,
            f"Следующие файлы не импортируют core.silence_constants: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
