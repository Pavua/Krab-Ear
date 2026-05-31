"""Тесты для W1311 F3 MED: noisereduce backend должен уважать speech-band floor.

Проверяем, что _denoise_noisereduce передаёт правильное значение prop_decrease
в nr.reduce_noise() для каждого уровня strength:
  - strong   → prop_decrease=0.75
  - moderate → prop_decrease=0.85
  - light    → prop_decrease=0.50

W1322: fix noisereduce backend respects strength via prop_decrease.
"""

from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import patch, MagicMock, call
from typing import Any

import numpy as np

# Настройка PYTHONPATH для запуска как standalone
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.audio_denoiser import AudioDenoiser, _NOISEREDUCE_PARAMS

_SR = 16000


def _make_audio(duration_sec: float = 1.0) -> np.ndarray:
    """Создаём синусоиду достаточной длины для обработки."""
    t = np.linspace(0, duration_sec, int(_SR * duration_sec), endpoint=False)
    return (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)


class TestNoisereduceStrengthFloor(unittest.TestCase):
    """W1322: noisereduce backend должен передавать правильный prop_decrease.

    Согласно W1311 F3: 'strong' режим в noisereduce без ограничения применяет
    95% подавление (prop_decrease=0.95 по умолчанию). Fix: используем отдельную
    таблицу _NOISEREDUCE_PARAMS со значениями speech-band floor.
    """

    def _make_denoiser_with_noisereduce(self) -> AudioDenoiser:
        """Создаём AudioDenoiser, принудительно включая noisereduce путь."""
        denoiser = AudioDenoiser()
        denoiser._has_noisereduce = True
        return denoiser

    def _call_denoise_with_mock_nr(
        self,
        strength: str,
        audio: np.ndarray,
    ) -> dict[str, Any]:
        """Запускает denoise с замоканным noisereduce, возвращает kwargs из вызова."""
        captured: dict[str, Any] = {}

        def fake_reduce_noise(**kwargs: Any) -> np.ndarray:
            captured.update(kwargs)
            # Возвращаем нули той же длины — не важен результат, важны kwargs
            return np.zeros_like(kwargs["y"])

        denoiser = self._make_denoiser_with_noisereduce()

        fake_nr = MagicMock()
        fake_nr.reduce_noise = fake_reduce_noise

        with patch.dict("sys.modules", {"noisereduce": fake_nr}):
            denoiser.denoise(audio, _SR, strength=strength)  # type: ignore[arg-type]

        return captured

    # ------------------------------------------------------------------
    # test_noisereduce_strong_uses_prop_decrease_0_75
    # ------------------------------------------------------------------

    def test_noisereduce_strong_uses_prop_decrease_0_75(self) -> None:
        """W1550: strong передаёт prop_decrease=0.95 с min_attenuation_db=-12.0 floor.

        W1322 originally used 0.75; W1550 restored to 0.95 with explicit floor parameter
        (min_attenuation_db=-12.0) instead of reduced prop_decrease.
        """
        audio = _make_audio(duration_sec=1.0)
        kwargs = self._call_denoise_with_mock_nr("strong", audio)

        self.assertIn("prop_decrease", kwargs,
                      "nr.reduce_noise должен получить prop_decrease")
        self.assertAlmostEqual(
            kwargs["prop_decrease"], 0.95, places=6,
            msg="strong mode: prop_decrease должен быть 0.95 (W1550: floor via min_attenuation_db)"
        )

    # ------------------------------------------------------------------
    # test_noisereduce_moderate_uses_prop_decrease_0_85
    # ------------------------------------------------------------------

    def test_noisereduce_moderate_uses_prop_decrease_0_85(self) -> None:
        """W1550: moderate передаёт prop_decrease=0.75.

        W1322 originally used 0.85; W1550 restored to 0.75.
        """
        audio = _make_audio(duration_sec=1.0)
        kwargs = self._call_denoise_with_mock_nr("moderate", audio)

        self.assertIn("prop_decrease", kwargs,
                      "nr.reduce_noise должен получить prop_decrease")
        self.assertAlmostEqual(
            kwargs["prop_decrease"], 0.75, places=6,
            msg="moderate mode: prop_decrease должен быть 0.75 (W1550)"
        )

    # ------------------------------------------------------------------
    # test_noisereduce_light_uses_prop_decrease_0_5
    # ------------------------------------------------------------------

    def test_noisereduce_light_uses_prop_decrease_0_5(self) -> None:
        """strength='light' передаёт prop_decrease=0.5 в nr.reduce_noise().

        light оставляет минимум 50% оригинального речевого сигнала.
        """
        audio = _make_audio(duration_sec=1.0)
        kwargs = self._call_denoise_with_mock_nr("light", audio)

        self.assertIn("prop_decrease", kwargs,
                      "nr.reduce_noise должен получить prop_decrease")
        self.assertAlmostEqual(
            kwargs["prop_decrease"], 0.50, places=6,
            msg="light mode: prop_decrease должен быть 0.50 (speech-band floor W1311 F3)"
        )

    # ------------------------------------------------------------------
    # test_noisereduce_strong_not_0_95 (регрессионный)
    # ------------------------------------------------------------------

    def test_noisereduce_strong_no_longer_uses_0_95(self) -> None:
        """W1550: strong mode USES prop_decrease=0.95 WITH min_attenuation_db=-12.0 floor.

        W1322 changed from 0.95 to 0.75 (no floor). W1550 restored 0.95 with explicit
        floor parameter (min_attenuation_db=-12.0).
        """
        audio = _make_audio(duration_sec=1.0)
        kwargs = self._call_denoise_with_mock_nr("strong", audio)

        prop_decrease = kwargs.get("prop_decrease", None)
        self.assertIsNotNone(prop_decrease)
        # W1550: 0.95 is now CORRECT (floor enforced via min_attenuation_db=-12.0)
        self.assertAlmostEqual(
            prop_decrease, 0.95, places=6,
            msg="strong mode uses 0.95 with min_attenuation_db=-12.0 floor (W1550)"
        )
        # Verify the floor parameter is present
        self.assertIn("min_attenuation_db", kwargs,
                      "strong mode must pass min_attenuation_db=-12.0 floor (W1550)")

    # ------------------------------------------------------------------
    # test_noisereduce_params_table_values
    # ------------------------------------------------------------------

    def test_noisereduce_params_table_has_correct_values(self) -> None:
        """W1550: _NOISEREDUCE_PARAMS values restored from W1538 regression.

        W1322 originally set strong=0.75, moderate=0.85.
        W1550 restored: strong=0.95 (with min_attenuation_db=-12.0), moderate=0.75, light=0.50.
        """
        self.assertAlmostEqual(_NOISEREDUCE_PARAMS["strong"]["prop_decrease"], 0.95)
        self.assertAlmostEqual(_NOISEREDUCE_PARAMS["moderate"]["prop_decrease"], 0.75)
        self.assertAlmostEqual(_NOISEREDUCE_PARAMS["light"]["prop_decrease"], 0.50)

    # ------------------------------------------------------------------
    # test_spectral_gating_still_uses_original_params
    # ------------------------------------------------------------------

    def test_spectral_gating_uses_original_strong_params(self) -> None:
        """spectral gating (fallback) по-прежнему использует _STRENGTH_PARAMS.

        Это важно: разделение таблиц не должно ломать spectral gating path.
        spectral gating применяет prop_decrease к маске (per-bin), а не глобально,
        поэтому 0.95 корректен для этого backend.
        """
        from core.audio_denoiser import _STRENGTH_PARAMS

        # spectral gating strong всё ещё 0.95
        self.assertAlmostEqual(_STRENGTH_PARAMS["strong"]["prop_decrease"], 0.95)
        # spectral gating moderate всё ещё 0.75
        self.assertAlmostEqual(_STRENGTH_PARAMS["moderate"]["prop_decrease"], 0.75)

    # ------------------------------------------------------------------
    # test_noisereduce_passes_stationary_true
    # ------------------------------------------------------------------

    def test_noisereduce_passes_stationary_true_for_all_strengths(self) -> None:
        """W1550: stationary=True for light/moderate, stationary=False for strong."""
        audio = _make_audio(duration_sec=1.0)
        for strength in ("light", "moderate"):
            with self.subTest(strength=strength):
                kwargs = self._call_denoise_with_mock_nr(strength, audio)
                self.assertTrue(
                    kwargs.get("stationary"),
                    f"stationary=True для {strength!r} (W1550)"
                )
        # strong uses stationary=False (non-stationary noise)
        with self.subTest(strength="strong"):
            kwargs = self._call_denoise_with_mock_nr("strong", audio)
            self.assertFalse(
                kwargs.get("stationary", True),
                "stationary=False для strong (W1550: non-stationary noise)"
            )


class TestNoisereduceStrengthFloorDirect(unittest.TestCase):
    """Прямое тестирование _denoise_noisereduce через patch sys.modules."""

    def setUp(self) -> None:
        self.audio = np.zeros(int(_SR * 1.0), dtype=np.float64)
        self.audio[:3200] = 0.1  # имитация noise floor

    def _call_static(self, strength: str) -> dict[str, Any]:
        """Вызывает _denoise_noisereduce напрямую через patch.

        W1550: passes _NOISEREDUCE_PARAMS as nr_params (not params) to use the
        W1550 branch (params dict path uses n_std_thresh_stationary key which
        W1550 params don't have).
        """
        from core.audio_denoiser import _NOISEREDUCE_PARAMS, _STRENGTH_PARAMS

        nr_params = _NOISEREDUCE_PARAMS.get(strength, _NOISEREDUCE_PARAMS["moderate"])
        # spectral gating params (not used in noisereduce path, but required as positional arg)
        sg_params = _STRENGTH_PARAMS.get(strength, _STRENGTH_PARAMS["moderate"])
        captured: dict[str, Any] = {}

        def fake_reduce_noise(**kwargs: Any) -> np.ndarray:
            captured.update(kwargs)
            return np.zeros_like(kwargs["y"])

        fake_nr = MagicMock()
        fake_nr.reduce_noise = fake_reduce_noise

        with patch.dict("sys.modules", {"noisereduce": fake_nr}):
            AudioDenoiser._denoise_noisereduce(self.audio, _SR, sg_params, nr_params=nr_params)

        return captured

    def test_direct_strong_prop_decrease(self) -> None:
        """W1550: strong → prop_decrease=0.95 (floor via min_attenuation_db)."""
        kwargs = self._call_static("strong")
        self.assertAlmostEqual(kwargs["prop_decrease"], 0.95, places=6)

    def test_direct_moderate_prop_decrease(self) -> None:
        """W1550: moderate → prop_decrease=0.75."""
        kwargs = self._call_static("moderate")
        self.assertAlmostEqual(kwargs["prop_decrease"], 0.75, places=6)

    def test_direct_light_prop_decrease(self) -> None:
        """_denoise_noisereduce прямой вызов — light → prop_decrease=0.50."""
        kwargs = self._call_static("light")
        self.assertAlmostEqual(kwargs["prop_decrease"], 0.50, places=6)


if __name__ == "__main__":
    unittest.main()
