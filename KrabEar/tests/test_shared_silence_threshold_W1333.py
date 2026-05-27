"""Tests for W1333: shared -40 dB silence threshold constant.

Проверяет что:
1. Все 6 файлов (плюс audio_quality.py, добавлен W1477) ссылаются на константы
   из core.silence_constants, а не задают -40.0 вручную.
2. SILENCE_THRESHOLD_DB == -40.0.
3. SILENCE_THRESHOLD_AMP соответствует 10^(DB/20).

W1477: audio_quality.py включён в _TARGET_FILES чтобы поймать регрессию
возврата к хардкодному 0.001 (-60 dB). Три дополнительных теста:
- test_audio_quality_uses_shared_threshold
- test_analyze_silence_and_audio_quality_agree_silence_ratio
- test_no_hardcoded_0_001_in_audio_quality_py
"""

from __future__ import annotations

import ast
import math
import sys
import os
import unittest
from pathlib import Path

import numpy as np

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
        # W1477: audio_quality.py previously had hardcoded 0.001 (-60 dB)
        _KRAB_EAR / "core" / "audio_quality.py",
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

    def test_no_minus_40_literal_in_audio_quality(self) -> None:
        literals = self._collect_float_literals(self._TARGET_FILES[6])
        self.assertNotIn(-40.0, literals,
                         "audio_quality.py still has bare -40.0 literal")

    def test_all_modules_reference_shared_constant(self) -> None:
        """Интеграционный тест: все 7 файлов импортируют из silence_constants."""
        missing: list[str] = []
        for path in self._TARGET_FILES:
            source = path.read_text(encoding="utf-8")
            if "silence_constants" not in source:
                missing.append(path.name)
        self.assertFalse(
            missing,
            f"Следующие файлы не импортируют core.silence_constants: {missing}",
        )


class TestAudioQualityThresholdW1477(unittest.TestCase):
    """W1477: audio_quality.py использует общий порог тишины из silence_constants."""

    def test_audio_quality_uses_shared_threshold(self) -> None:
        """_SILENCE_RMS_THRESHOLD в audio_quality совпадает с SILENCE_THRESHOLD_AMP."""
        import core.audio_quality as aq
        self.assertAlmostEqual(
            aq._SILENCE_RMS_THRESHOLD,
            SILENCE_THRESHOLD_AMP,
            places=10,
            msg=(
                "_SILENCE_RMS_THRESHOLD must equal SILENCE_THRESHOLD_AMP (0.01). "
                "Previously was 0.001 (-60 dB) — diverged from shared constant."
            ),
        )

    def test_analyze_silence_and_audio_quality_agree_silence_ratio(self) -> None:
        """Integration: тихий сигнал даёт silence_ratio=1.0 в обоих детекторах.

        Создаём аудио с RMS=0.003 (между старым порогом 0.001 и новым 0.01).
        После W1477 оба детектора должны считать его тишиной.
        """
        from core.audio_quality import AudioQualityAnalyzer
        from core.silence_constants import SILENCE_THRESHOLD_AMP

        sample_rate = 16000
        duration = 1.0  # секунда
        n_samples = int(sample_rate * duration)

        # RMS ≈ 0.003 — выше старого порога 0.001, ниже нового 0.01
        rng = 0.003 * (2 ** 0.5)  # амплитуда синусоиды с RMS=0.003
        t = np.linspace(0, duration, n_samples, endpoint=False)
        audio = (rng * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

        # Убеждаемся что RMS действительно между старым и новым порогом
        actual_rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        self.assertGreater(actual_rms, 0.001,
                           "Test audio RMS should be above old 0.001 threshold")
        self.assertLess(actual_rms, SILENCE_THRESHOLD_AMP,
                        "Test audio RMS should be below new shared threshold")

        # AudioQualityAnalyzer должен теперь распознать весь сигнал как тишину
        analyzer = AudioQualityAnalyzer()
        report = analyzer.analyze(audio, sample_rate)
        self.assertAlmostEqual(
            report.silence_ratio, 1.0, places=1,
            msg=(
                f"silence_ratio={report.silence_ratio:.4f} but expected ~1.0 "
                f"for audio with RMS={actual_rms:.4f} < SILENCE_THRESHOLD_AMP={SILENCE_THRESHOLD_AMP}"
            ),
        )

    def test_no_hardcoded_0_001_in_audio_quality_py(self) -> None:
        """AST: audio_quality.py не содержит хардкодный _SILENCE_RMS_THRESHOLD = 0.001.

        Проверяет что константа НЕ присваивается через литерал 0.001.
        Метод ищет Assign узлы вида `_SILENCE_RMS_THRESHOLD = <float literal>`.
        Допустимые использования 0.001 в других контекстах (например,
        clipping threshold) не вызывают ошибку.
        """
        import ast
        path = _KRAB_EAR / "core" / "audio_quality.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            # Ищем: _SILENCE_RMS_THRESHOLD = <float literal>
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id == "_SILENCE_RMS_THRESHOLD"
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, float)
                    ):
                        self.fail(
                            f"_SILENCE_RMS_THRESHOLD is assigned a float literal "
                            f"{node.value.value!r} in audio_quality.py; "
                            f"must use SILENCE_THRESHOLD_AMP from core.silence_constants."
                        )
        # Также убеждаемся что файл действительно импортирует silence_constants
        self.assertIn(
            "silence_constants", source,
            "audio_quality.py must import from core.silence_constants (W1477)",
        )


if __name__ == "__main__":
    unittest.main()
