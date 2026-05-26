"""W1132: Проверка, что NoiseProfiler использует SILENCE_THRESHOLD_AMP из silence_detector.

Tests:
1. _SILENCE_RMS_THRESHOLD в noise_profiler совпадает с SILENCE_THRESHOLD_AMP из silence_detector.
2. Нет внутреннего дрейфа: -40 dB == 0.01 в amplitude.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.noise_profiler as _np_mod
from core.silence_detector import SILENCE_THRESHOLD_AMP


class TestSilenceThresholdUnification(unittest.TestCase):
    """Silence threshold SSOT: noise_profiler должен использовать silence_detector константу."""

    def test_noise_profiler_threshold_matches_silence_detector(self):
        """_SILENCE_RMS_THRESHOLD в noise_profiler равен SILENCE_THRESHOLD_AMP из silence_detector."""
        self.assertAlmostEqual(
            _np_mod._SILENCE_RMS_THRESHOLD,
            SILENCE_THRESHOLD_AMP,
            places=10,
            msg=(
                f"noise_profiler._SILENCE_RMS_THRESHOLD={_np_mod._SILENCE_RMS_THRESHOLD} "
                f"!= silence_detector.SILENCE_THRESHOLD_AMP={SILENCE_THRESHOLD_AMP}. "
                "Нет дрейфа между модулями — оба должны использовать -40 dB (0.01)."
            ),
        )

    def test_silence_threshold_amp_is_minus40_db(self):
        """SILENCE_THRESHOLD_AMP == 10^(-40/20) == 0.01 (нет внутреннего дрейфа)."""
        expected = 10.0 ** (-40.0 / 20.0)  # -40 dB в амплитуде
        self.assertAlmostEqual(
            SILENCE_THRESHOLD_AMP,
            expected,
            places=10,
            msg=f"SILENCE_THRESHOLD_AMP должен быть 0.01 (-40 dB), получили {SILENCE_THRESHOLD_AMP}",
        )

    def test_noise_profiler_threshold_not_legacy_value(self):
        """_SILENCE_RMS_THRESHOLD не равен старому значению 0.001 (-60 dB)."""
        legacy_value = 0.001
        self.assertNotAlmostEqual(
            _np_mod._SILENCE_RMS_THRESHOLD,
            legacy_value,
            places=5,
            msg=(
                f"noise_profiler._SILENCE_RMS_THRESHOLD всё ещё равен legacy 0.001 (-60 dB). "
                "Ожидалось 0.01 (-40 dB) — единая константа из silence_detector."
            ),
        )


if __name__ == "__main__":
    unittest.main()
