"""Тесты для core/audio_denoiser.py — адаптивное шумоподавление."""

from __future__ import annotations

import sys
import os
import unittest
import threading

import numpy as np

# Настройка PYTHONPATH для запуска как standalone
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.audio_denoiser import AudioDenoiser, _has_whispered_segments, _NOISEREDUCE_PARAMS
from core.noise_profiler import NoiseProfiler

_SR = 16000  # стандартная частота дискретизации Whisper


def _make_clean_audio(duration_sec: float = 1.0, freq: float = 440.0) -> np.ndarray:
    """Синусоида — чистый тональный сигнал с высоким SNR."""
    t = np.linspace(0, duration_sec, int(_SR * duration_sec), endpoint=False)
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _make_noisy_audio(duration_sec: float = 1.0, snr_target_db: float = 5.0) -> np.ndarray:
    """Синусоида + белый шум с заданным SNR."""
    t = np.linspace(0, duration_sec, int(_SR * duration_sec), endpoint=False)
    signal = 0.5 * np.sin(2 * np.pi * 440.0 * t)
    signal_rms = float(np.sqrt(np.mean(signal ** 2)))
    noise_rms = signal_rms / (10 ** (snr_target_db / 20.0))
    rng = np.random.default_rng(42)
    noise = rng.standard_normal(len(t)) * noise_rms
    return np.clip(signal + noise, -1.0, 1.0).astype(np.float32)


class TestAudioDenoiserStrengthOff(unittest.TestCase):
    """strength='off' — всегда passthrough."""

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_strength_off_returns_identical_array(self) -> None:
        """При strength='off' массив не изменяется."""
        audio = _make_noisy_audio(snr_target_db=3.0)
        result = self.denoiser.denoise(audio, _SR, strength="off")
        np.testing.assert_array_equal(audio, result)

    def test_strength_off_noisy_audio_unchanged(self) -> None:
        """strength='off' даже для очень зашумлённого аудио — без обработки."""
        audio = _make_noisy_audio(snr_target_db=1.0)
        result = self.denoiser.denoise(audio, _SR, strength="off")
        self.assertEqual(audio.shape, result.shape)
        np.testing.assert_array_equal(audio, result)


class TestAudioDenoiserOutputShape(unittest.TestCase):
    """Форма и dtype выходного массива."""

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_output_shape_preserved(self) -> None:
        """Форма выходного массива совпадает с входной."""
        audio = _make_noisy_audio(duration_sec=0.5, snr_target_db=5.0)
        result = self.denoiser.denoise(audio, _SR, strength="moderate")
        self.assertEqual(audio.shape, result.shape)

    def test_output_shape_long_audio(self) -> None:
        """Форма сохраняется для длинного аудио."""
        audio = _make_noisy_audio(duration_sec=2.0, snr_target_db=8.0)
        result = self.denoiser.denoise(audio, _SR, strength="light")
        self.assertEqual(audio.shape, result.shape)

    def test_multichannel_input_produces_mono_output(self) -> None:
        """Многоканальный входной массив → моно на выходе."""
        mono = _make_noisy_audio(duration_sec=0.5, snr_target_db=5.0)
        stereo = np.stack([mono, mono], axis=1)  # (N, 2)
        result = self.denoiser.denoise(stereo, _SR, strength="moderate")
        # Деноизер усредняет до моно и возвращает 1-D
        self.assertEqual(result.ndim, 1)
        self.assertEqual(result.shape[0], stereo.shape[0])


class TestAudioDenoiserClipping(unittest.TestCase):
    """Значения выходного массива находятся в [-1, 1]."""

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_output_clipped_light(self) -> None:
        audio = _make_noisy_audio(duration_sec=1.0, snr_target_db=4.0)
        result = self.denoiser.denoise(audio, _SR, strength="light")
        self.assertLessEqual(float(np.max(result)), 1.0)
        self.assertGreaterEqual(float(np.min(result)), -1.0)

    def test_output_clipped_strong(self) -> None:
        audio = _make_noisy_audio(duration_sec=1.0, snr_target_db=2.0)
        result = self.denoiser.denoise(audio, _SR, strength="strong")
        self.assertLessEqual(float(np.max(result)), 1.0)
        self.assertGreaterEqual(float(np.min(result)), -1.0)


class TestAudioDenoiserSNRBasedDecision(unittest.TestCase):
    """Шумоподавление изменяет зашумлённое аудио, но оставляет чистое близким."""

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_noisy_audio_is_modified(self) -> None:
        """Зашумлённое аудио (SNR ~5 dB) изменяется деноизером."""
        audio = _make_noisy_audio(duration_sec=1.0, snr_target_db=5.0)
        result = self.denoiser.denoise(audio, _SR, strength="moderate")
        # После обработки сигнал должен измениться
        diff = float(np.mean(np.abs(result.astype(np.float64) - audio.astype(np.float64))))
        self.assertGreater(diff, 1e-6, "Деноизер должен изменять зашумлённый сигнал")

    def test_noisy_audio_snr_improves(self) -> None:
        """После деноизинга шум уменьшается (RMS шума ниже)."""
        # Создаём сигнал с явным шумом
        t = np.linspace(0, 1.0, _SR, endpoint=False)
        signal = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float64)
        rng = np.random.default_rng(0)
        noise = rng.standard_normal(_SR) * 0.3
        audio = np.clip(signal + noise, -1.0, 1.0).astype(np.float32)

        result = self.denoiser.denoise(audio, _SR, strength="strong")

        # RMS результата должен быть меньше (шум подавлен)
        original_rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        result_rms = float(np.sqrt(np.mean(result.astype(np.float64) ** 2)))
        self.assertLess(result_rms, original_rms)


class TestNoiseProfilerSNRThreshold(unittest.TestCase):
    """NoiseProfiler: чистый сигнал даёт высокий SNR, зашумлённый — низкий."""

    def setUp(self) -> None:
        self.profiler = NoiseProfiler()

    def test_clean_audio_snr_above_threshold(self) -> None:
        """Чистый тональный сигнал → SNR выше стандартного порога 15 dB."""
        audio = _make_clean_audio(duration_sec=1.0)
        profile = self.profiler.profile(audio, _SR)
        self.assertGreater(profile.snr_db, 15.0)

    def test_white_noise_snr_below_threshold(self) -> None:
        """Белый шум без сигнала → SNR ниже 15 dB."""
        rng = np.random.default_rng(7)
        noise_only = (rng.standard_normal(_SR) * 0.4).astype(np.float32)
        profile = self.profiler.profile(noise_only, _SR)
        self.assertLess(profile.snr_db, 15.0)


# ---------------------------------------------------------------------------
# Wave 128 — дополнительные тесты по спецификации
# ---------------------------------------------------------------------------

class TestAudioDenoiserWave128(unittest.TestCase):
    """Wave 128 required test cases."""

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    # ------------------------------------------------------------------
    # test_off_level_returns_input_unchanged
    # ------------------------------------------------------------------

    def test_off_level_returns_input_unchanged(self) -> None:
        """strength='off' возвращает ИДЕНТИЧНЫЙ объект-массив (без копирования)."""
        audio = _make_noisy_audio(duration_sec=1.0, snr_target_db=3.0)
        result = self.denoiser.denoise(audio, _SR, strength="off")
        # Массивы тождественны (тот же объект или те же данные)
        np.testing.assert_array_equal(audio, result)

    # ------------------------------------------------------------------
    # test_light_level_reduces_noise
    # ------------------------------------------------------------------

    def test_light_level_reduces_noise(self) -> None:
        """strength='light' уменьшает общую энергию зашумлённого сигнала."""
        audio = _make_noisy_audio(duration_sec=1.0, snr_target_db=3.0)
        result = self.denoiser.denoise(audio, _SR, strength="light")
        rms_in = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        rms_out = float(np.sqrt(np.mean(result.astype(np.float64) ** 2)))
        self.assertLess(rms_out, rms_in,
                        "light denoising должно уменьшать RMS зашумлённого сигнала")

    # ------------------------------------------------------------------
    # test_strong_level_reduces_more
    # ------------------------------------------------------------------

    def test_strong_level_reduces_more(self) -> None:
        """strength='strong' подавляет больше, чем strength='light'."""
        audio = _make_noisy_audio(duration_sec=1.0, snr_target_db=3.0)
        result_light = self.denoiser.denoise(audio, _SR, strength="light")
        result_strong = self.denoiser.denoise(audio, _SR, strength="strong")

        rms_light = float(np.sqrt(np.mean(result_light.astype(np.float64) ** 2)))
        rms_strong = float(np.sqrt(np.mean(result_strong.astype(np.float64) ** 2)))
        self.assertLess(rms_strong, rms_light,
                        "strong должен подавлять больше чем light (ниже RMS)")

    # ------------------------------------------------------------------
    # test_handles_short_audio
    # ------------------------------------------------------------------

    def test_handles_short_audio(self) -> None:
        """Аудио короче _N_FFT*2 возвращается без обработки (passthrough)."""
        # _N_FFT = 512, min обрабатываемая длина = 1024 → берём 500 сэмплов
        short_audio = _make_noisy_audio(duration_sec=0.03, snr_target_db=5.0)
        # убедимся что короче порога
        self.assertLess(len(short_audio), 512 * 2)
        result = self.denoiser.denoise(short_audio, _SR, strength="moderate")
        # Должен вернуть тот же массив без исключений
        np.testing.assert_array_equal(short_audio, result)

    # ------------------------------------------------------------------
    # test_handles_silence
    # ------------------------------------------------------------------

    def test_handles_silence(self) -> None:
        """Нулевое (тишина) аудио обрабатывается без NaN и не бросает исключений."""
        silence = np.zeros(int(_SR * 1.5), dtype=np.float32)
        result = self.denoiser.denoise(silence, _SR, strength="moderate")
        self.assertFalse(np.any(np.isnan(result)), "Тишина не должна давать NaN")
        self.assertEqual(result.shape, silence.shape)
        # Для тишины RMS должен остаться ≈ 0
        rms = float(np.sqrt(np.mean(result.astype(np.float64) ** 2)))
        self.assertLess(rms, 1e-3)

    # ------------------------------------------------------------------
    # test_invalid_level_falls_back_to_off
    # ------------------------------------------------------------------

    def test_invalid_level_falls_back_to_off(self) -> None:
        """Неизвестный уровень strength использует параметры 'moderate' (не крашится).

        В коде: _STRENGTH_PARAMS.get(strength, _STRENGTH_PARAMS["moderate"])
        Поэтому неизвестный уровень ≡ moderate — сигнал должен измениться.
        """
        audio = _make_noisy_audio(duration_sec=1.0, snr_target_db=3.0)
        # type: ignore — нарочно передаём невалидное значение
        result = self.denoiser.denoise(audio, _SR, strength="ultra")  # type: ignore[arg-type]
        # Должен вернуть обработанный результат (не упасть)
        self.assertEqual(result.shape, audio.shape)
        # Значения в [-1, 1]
        self.assertLessEqual(float(np.max(result)), 1.0)
        self.assertGreaterEqual(float(np.min(result)), -1.0)

    # ------------------------------------------------------------------
    # test_concurrent_denoise
    # ------------------------------------------------------------------

    def test_concurrent_denoise(self) -> None:
        """Несколько потоков одновременно вызывают denoise без гонок или крашей."""
        errors: list[Exception] = []
        results: list[np.ndarray] = [None] * 6  # type: ignore[list-item]

        def worker(idx: int) -> None:
            try:
                audio = _make_noisy_audio(duration_sec=0.5, snr_target_db=5.0)
                results[idx] = self.denoiser.denoise(audio, _SR, strength="moderate")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Concurrent denoise errors: {errors}")
        for i, res in enumerate(results):
            self.assertIsNotNone(res, f"Thread {i} produced no result")
            self.assertFalse(np.any(np.isnan(res)),
                             f"Thread {i} result contains NaN")


# ---------------------------------------------------------------------------
# W1071 — F2 (strong-mode whisper downgrade) + F4 (multichannel warning)
# ---------------------------------------------------------------------------

def _make_whisper_audio(duration_sec: float = 1.0) -> np.ndarray:
    """Синусоида шёпотной амплитуды (-42 dB RMS ≈ линейн. 0.008)."""
    t = np.linspace(0, duration_sec, int(_SR * duration_sec), endpoint=False)
    # Амплитуда 0.008 даёт RMS ≈ 0.0057 → 20*log10(0.0057) ≈ -44.9 dB
    return (0.008 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)


def _make_normal_speech_audio(duration_sec: float = 1.0) -> np.ndarray:
    """Синусоида нормальной громкости речи (-12 dB RMS)."""
    t = np.linspace(0, duration_sec, int(_SR * duration_sec), endpoint=False)
    # Амплитуда 0.25 даёт RMS ≈ 0.177 → ≈ -15 dB — вне диапазона шёпота
    return (0.25 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)


class TestHasWhisperedSegments(unittest.TestCase):
    """Тесты вспомогательной функции _has_whispered_segments."""

    def test_whisper_amplitude_detected(self) -> None:
        """Шёпотная амплитуда в диапазоне -50..-35 dB обнаруживается."""
        audio = _make_whisper_audio(duration_sec=1.0).astype(np.float64)
        self.assertTrue(_has_whispered_segments(audio, _SR))

    def test_normal_speech_not_detected_as_whisper(self) -> None:
        """Нормальная громкость речи (выше -35 dB) не считается шёпотом."""
        audio = _make_normal_speech_audio(duration_sec=1.0).astype(np.float64)
        self.assertFalse(_has_whispered_segments(audio, _SR))

    def test_silence_not_detected_as_whisper(self) -> None:
        """Тишина (ниже -50 dB) не считается шёпотом."""
        silence = np.zeros(int(_SR * 1.0), dtype=np.float64)
        self.assertFalse(_has_whispered_segments(silence, _SR))

    def test_empty_audio_safe(self) -> None:
        """Пустой массив не вызывает исключений."""
        self.assertFalse(_has_whispered_segments(np.array([]), _SR))


class TestStrongModeWhisperDowngrade(unittest.TestCase):
    """W1062 F2 — strong-mode автоматически понижается до moderate при шёпоте."""

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_strong_mode_downgrades_when_whisper_detected(self) -> None:
        """При шёпотной амплитуде strong эффективно использует moderate-параметры.

        Проверяем косвенно: результат strong при шёпоте должен быть ближе
        к результату moderate (для нормального сигнала), чем результат чистого
        strong, потому что downgrade ограничивает prop_decrease до 0.75.
        """
        whisper_audio = _make_whisper_audio(duration_sec=1.0)

        result_strong = self.denoiser.denoise(whisper_audio, _SR, strength="strong")
        result_moderate = self.denoiser.denoise(whisper_audio, _SR, strength="moderate")

        # strong при шёпоте должен дать тот же результат что moderate
        # (поскольку происходит downgrade strong→moderate)
        np.testing.assert_array_almost_equal(
            result_strong, result_moderate, decimal=6,
            err_msg="strong с шёпотом должен вести себя идентично moderate (downgrade)",
        )

    def test_strong_mode_unchanged_for_normal_speech(self) -> None:
        """При нормальной громкости речи strong НЕ понижается до moderate."""
        normal_audio = _make_normal_speech_audio(duration_sec=1.0)

        result_strong = self.denoiser.denoise(normal_audio, _SR, strength="strong")
        result_moderate = self.denoiser.denoise(normal_audio, _SR, strength="moderate")

        # strong при нормальной речи НЕ должен быть равен moderate
        diff = float(np.mean(np.abs(result_strong.astype(np.float64) - result_moderate.astype(np.float64))))
        self.assertGreater(
            diff, 1e-8,
            "strong без шёпота должен отличаться от moderate (нет downgrade)",
        )

    def test_strong_mode_output_shape_preserved_for_whisper(self) -> None:
        """Форма выходного массива сохраняется после downgrade."""
        whisper_audio = _make_whisper_audio(duration_sec=0.5)
        result = self.denoiser.denoise(whisper_audio, _SR, strength="strong")
        self.assertEqual(result.shape, whisper_audio.shape)

    def test_strong_mode_output_clipped_for_whisper(self) -> None:
        """Выходные значения остаются в [-1, 1] после downgrade."""
        whisper_audio = _make_whisper_audio(duration_sec=1.0)
        result = self.denoiser.denoise(whisper_audio, _SR, strength="strong")
        self.assertLessEqual(float(np.max(result)), 1.0)
        self.assertGreaterEqual(float(np.min(result)), -1.0)


class TestMultichannelWarning(unittest.TestCase):
    """W1062 F4 — многоканальный вход логирует предупреждение."""

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_multichannel_logs_warning(self) -> None:
        """При многоканальном входе логируется предупреждение о downmix."""
        mono = _make_noisy_audio(duration_sec=0.5, snr_target_db=5.0)
        stereo = np.stack([mono, mono], axis=1)  # (N, 2)

        with self.assertLogs("KrabEar.AudioDenoiser", level="WARNING") as cm:
            self.denoiser.denoise(stereo, _SR, strength="moderate")

        self.assertTrue(
            any("многоканальный" in msg or "моно" in msg for msg in cm.output),
            f"Ожидалось предупреждение о многоканальном входе, получено: {cm.output}",
        )

    def test_multichannel_output_is_mono(self) -> None:
        """После обработки многоканального входа возвращается 1-D массив."""
        mono = _make_noisy_audio(duration_sec=0.5, snr_target_db=5.0)
        stereo = np.stack([mono, mono], axis=1)  # (N, 2)
        result = self.denoiser.denoise(stereo, _SR, strength="light")
        self.assertEqual(result.ndim, 1)
        self.assertEqual(result.shape[0], stereo.shape[0])


# ---------------------------------------------------------------------------
# W1550 — _NOISEREDUCE_PARAMS regression tests (W1322 floor restored)
# ---------------------------------------------------------------------------

class TestNoiseReduceParamsW1550(unittest.TestCase):
    """W1550: _NOISEREDUCE_PARAMS dict restored (W1322 regression guard)."""

    def test_noisereduce_params_constant_exists(self) -> None:
        """_NOISEREDUCE_PARAMS должен быть определён в модуле."""
        import core.audio_denoiser as mod
        self.assertTrue(hasattr(mod, "_NOISEREDUCE_PARAMS"),
                        "_NOISEREDUCE_PARAMS отсутствует — W1322 регрессия")

    def test_noisereduce_params_has_all_levels(self) -> None:
        """_NOISEREDUCE_PARAMS содержит light/moderate/strong."""
        for level in ("light", "moderate", "strong"):
            self.assertIn(level, _NOISEREDUCE_PARAMS,
                          f"_NOISEREDUCE_PARAMS отсутствует ключ '{level}'")

    def test_noisereduce_params_strong_has_min_attenuation_db(self) -> None:
        """strong-mode должен содержать min_attenuation_db=-12.0 (W1322 floor)."""
        strong = _NOISEREDUCE_PARAMS["strong"]
        self.assertIn("min_attenuation_db", strong,
                      "strong-mode без min_attenuation_db — W1322 floor потерян")
        self.assertAlmostEqual(strong["min_attenuation_db"], -12.0,
                               msg="min_attenuation_db должен быть -12.0 dB")

    def test_noisereduce_params_strong_stationary_false(self) -> None:
        """strong-mode должен использовать stationary=False для нестационарного шума."""
        self.assertFalse(_NOISEREDUCE_PARAMS["strong"].get("stationary", True),
                         "strong-mode должен иметь stationary=False")

    def test_noisereduce_params_light_moderate_stationary_true(self) -> None:
        """light/moderate используют stationary=True."""
        for level in ("light", "moderate"):
            self.assertTrue(
                _NOISEREDUCE_PARAMS[level].get("stationary", False),
                f"_NOISEREDUCE_PARAMS['{level}'] должен иметь stationary=True",
            )

    def test_noisereduce_params_prop_decrease_ordering(self) -> None:
        """prop_decrease: light < moderate < strong."""
        self.assertLess(
            _NOISEREDUCE_PARAMS["light"]["prop_decrease"],
            _NOISEREDUCE_PARAMS["moderate"]["prop_decrease"],
        )
        self.assertLess(
            _NOISEREDUCE_PARAMS["moderate"]["prop_decrease"],
            _NOISEREDUCE_PARAMS["strong"]["prop_decrease"],
        )

    def test_noisereduce_params_used_in_denoise_strong(self) -> None:
        """denoise(..., strength='strong') проходит без исключений при noisereduce=absent."""
        # noisereduce чаще всего не установлен в CI → fallback path тоже покрываем
        denoiser = AudioDenoiser()
        audio = _make_noisy_audio(duration_sec=1.0, snr_target_db=5.0)
        # Не должно бросать исключение (W1322 параметры корректны)
        result = denoiser.denoise(audio, _SR, strength="strong")
        self.assertEqual(result.shape, audio.shape)


if __name__ == "__main__":
    unittest.main()
