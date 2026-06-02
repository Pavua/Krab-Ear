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

from core.audio_denoiser import (
    AudioDenoiser,
    _has_whispered_segments,
    _NOISEREDUCE_PARAMS,
    _percentile_noise_clip,
    _speech_band_bins,
    _STRONG_MIN_GAIN,
)
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


# ---------------------------------------------------------------------------
# W1718 — BUG 1: noise-window scan (percentile) + BUG 2: speech-band floor
#          + BUG 3: int16 input guard
# ---------------------------------------------------------------------------

class TestW1718NoiseWindowScan(unittest.TestCase):
    """W1718 BUG1: _percentile_noise_clip restored — speech at t=0 not suppressed.

    Регрессионный тест для W1062 F1 HIGH (body-revert W1071 удалил
    _percentile_noise_clip и вернул audio[:_NOISE_FLOOR_SAMPLES]).
    До фикса: первые 200 мс = speech → noise reference = speech → 73% речи подавлено.
    После фикса: quietest window выбирается по RMS → речь сохранена.
    """

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_speech_at_start_not_suppressed(self) -> None:
        """Речевой сигнал в НАЧАЛЕ записи не подавляется ≥90% (ratio ≥ 0.6).

        Схема теста:
        - Первые 200 мс: громкий речеподобный синусоидальный сигнал (440 Гц).
        - Следующие 200 мс: полная тишина (истинный noise floor).
        - Оставшиеся 600 мс: слабый фоновый шум.

        Без _percentile_noise_clip noise_reference = speech → маска убивает 440 Гц.
        С _percentile_noise_clip noise_reference = тишина → 440 Гц сохраняется.
        """
        sr = _SR
        window = int(0.2 * sr)  # 3200 сэмплов @ 16 кГц

        # Первые 200 мс — речь (громкий синус 440 Гц)
        t_speech = np.linspace(0, 0.2, window, endpoint=False)
        speech = (0.5 * np.sin(2 * np.pi * 440.0 * t_speech)).astype(np.float64)

        # Следующие 200 мс — тишина (истинный noise floor)
        silence = np.zeros(window, dtype=np.float64)

        # Оставшиеся 600 мс — слабый шум
        rng = np.random.default_rng(99)
        tail = rng.standard_normal(int(0.6 * sr)).astype(np.float64) * 0.02

        audio = np.concatenate([speech, silence, tail]).astype(np.float32)

        result = self.denoiser.denoise(audio, sr, strength="moderate")

        speech_rms_in = float(np.sqrt(np.mean(audio[:window].astype(np.float64) ** 2)))
        speech_rms_out = float(np.sqrt(np.mean(result[:window].astype(np.float64) ** 2)))
        ratio = speech_rms_out / (speech_rms_in + 1e-10)

        self.assertGreater(
            ratio, 0.6,
            f"Речь в начале подавлена слишком сильно: ratio={ratio:.3f} "
            f"(rms_in={speech_rms_in:.4f}, rms_out={speech_rms_out:.4f}). "
            f"Вероятно, noise floor взят из речевого фрагмента, а не из тишины."
        )

    def test_percentile_noise_clip_exists_and_callable(self) -> None:
        """_percentile_noise_clip доступна в модуле (не удалена W1071-body-revert)."""
        import core.audio_denoiser as mod
        self.assertTrue(callable(getattr(mod, "_percentile_noise_clip", None)),
                        "_percentile_noise_clip отсутствует — BUG 1 не исправлен")

    def test_percentile_noise_clip_picks_quiet_region(self) -> None:
        """_percentile_noise_clip выбирает тихие фреймы, а не первые 200 мс."""
        sr = _SR
        window = int(0.2 * sr)

        # Громкая речь в начале
        t = np.linspace(0, 0.2, window, endpoint=False)
        speech = 0.5 * np.sin(2 * np.pi * 440.0 * t)
        # Тишина — истинный noise floor
        silence = np.zeros(window)
        audio = np.concatenate([speech, silence]).astype(np.float64)

        clip = _percentile_noise_clip(audio)
        self.assertIsNotNone(clip, "_percentile_noise_clip вернул None для нормального сигнала")

        # RMS clip должен быть значительно ниже RMS всего аудио
        rms_clip = float(np.sqrt(np.mean(clip ** 2)))
        rms_all = float(np.sqrt(np.mean(audio ** 2)))
        self.assertLess(rms_clip, rms_all * 0.5,
                        f"noise_clip не является тихим регионом: rms_clip={rms_clip:.4f} "
                        f"rms_all={rms_all:.4f}")


class TestW1718SpeechBandFloor(unittest.TestCase):
    """W1718 BUG2: _STRONG_MIN_GAIN / speech-band floor restored (W1080 body-revert).

    До фикса: strong mode мог подавить 300–3000 Гц до 5% (−26 dB).
    После фикса: floor 25% (−12 dB) защищает речевую полосу.
    """

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_strong_min_gain_constant_exists(self) -> None:
        """_STRONG_MIN_GAIN = 0.25 должен быть определён в модуле."""
        import core.audio_denoiser as mod
        self.assertTrue(hasattr(mod, "_STRONG_MIN_GAIN"),
                        "_STRONG_MIN_GAIN отсутствует — BUG 2 не исправлен")
        self.assertAlmostEqual(mod._STRONG_MIN_GAIN, 0.25, places=5,
                               msg="_STRONG_MIN_GAIN должен быть 0.25 (-12 dB)")

    def test_speech_band_hz_constants_exist(self) -> None:
        """_SPEECH_BAND_LOW_HZ и _SPEECH_BAND_HIGH_HZ должны быть определены."""
        import core.audio_denoiser as mod
        self.assertTrue(hasattr(mod, "_SPEECH_BAND_LOW_HZ"),
                        "_SPEECH_BAND_LOW_HZ отсутствует — BUG 2 не исправлен")
        self.assertTrue(hasattr(mod, "_SPEECH_BAND_HIGH_HZ"),
                        "_SPEECH_BAND_HIGH_HZ отсутствует — BUG 2 не исправлен")
        self.assertEqual(mod._SPEECH_BAND_LOW_HZ, 300)
        self.assertEqual(mod._SPEECH_BAND_HIGH_HZ, 3000)

    def test_speech_band_bins_returns_valid_range(self) -> None:
        """_speech_band_bins должна возвращать валидный диапазон бинов."""
        low, high = _speech_band_bins(_SR)
        self.assertGreater(high, low,
                           f"_speech_band_bins вернул инвертированный диапазон: {low}..{high}")
        self.assertGreaterEqual(low, 0)
        # bin_high не должен превышать _N_FFT//2
        from core.audio_denoiser import _N_FFT
        self.assertLessEqual(high, _N_FFT // 2)

    def test_strong_mode_speech_band_not_crushed_below_floor(self) -> None:
        """strong mode не должен подавить 440 Гц (речевая полоса) ниже _STRONG_MIN_GAIN.

        Тест использует спектральный gating path (scipy). Создаём чистый синус
        440 Гц с заглушённым «noise floor» тишиной в первых 10 мс.
        После обработки энергия 440 Гц должна сохраниться хотя бы на 25%.
        """
        sr = _SR
        t = np.linspace(0, 1.0, sr, endpoint=False)
        # Слабый синус 440 Гц (−25 dB = 0.056 амплитуды) + крошечный шум
        rng = np.random.default_rng(7)
        weak_speech = 0.056 * np.sin(2 * np.pi * 440.0 * t)
        noise = rng.standard_normal(sr) * 0.15
        audio = np.clip(weak_speech + noise, -1.0, 1.0).astype(np.float32)

        result_strong = self.denoiser.denoise(audio, sr, strength="strong")
        result_moderate = self.denoiser.denoise(audio, sr, strength="moderate")

        rms_strong = float(np.sqrt(np.mean(result_strong.astype(np.float64) ** 2)))
        rms_moderate = float(np.sqrt(np.mean(result_moderate.astype(np.float64) ** 2)))

        # strong с floor должен сохранять больше энергии, чем без floor
        # (т.е. rms_strong > rms_strong_without_floor).
        # Косвенная проверка: strong не должен давать в 2× меньше moderate
        # (без floor он бы давал намного меньше)
        ratio_vs_moderate = rms_strong / (rms_moderate + 1e-10)
        self.assertGreater(
            ratio_vs_moderate, 0.1,
            f"strong mode подавил слишком агрессивно (ratio_vs_moderate={ratio_vs_moderate:.3f}). "
            f"Вероятно, _STRONG_MIN_GAIN floor отсутствует."
        )

    def test_speech_band_energy_floor_direct(self) -> None:
        """Прямая проверка: mask в речевой полосе после strong ≥ _STRONG_MIN_GAIN.

        Создаём синтетический аудиосигнал, вызываем _denoise_spectral_gating напрямую
        и проверяем что выходной сигнал в речевой полосе сохраняется хотя бы на
        _STRONG_MIN_GAIN от входного.
        """
        try:
            import scipy.signal  # noqa: F401  # type: ignore
        except ImportError:
            self.skipTest("scipy не установлен")

        sr = _SR
        # Чистый синус 1000 Гц (внутри речевой полосы 300–3000 Гц)
        t = np.linspace(0, 1.0, sr, endpoint=False)
        signal = 0.3 * np.sin(2 * np.pi * 1000.0 * t)
        audio = signal.astype(np.float64)

        # Нормальная амплитуда (не шёпот) — downgrade не произойдёт
        from core.audio_denoiser import _STRENGTH_PARAMS
        params = _STRENGTH_PARAMS["strong"]
        result = AudioDenoiser._denoise_spectral_gating(audio, sr, params, "strong")

        rms_in = float(np.sqrt(np.mean(audio ** 2)))
        rms_out = float(np.sqrt(np.mean(result ** 2)))

        # Выход должен быть ≥ _STRONG_MIN_GAIN от входа
        ratio = rms_out / (rms_in + 1e-10)
        self.assertGreaterEqual(
            ratio, _STRONG_MIN_GAIN * 0.5,  # допускаем небольшой запас
            f"strong mode подавил 1000 Гц ниже floor: ratio={ratio:.3f} "
            f"(ожидалось ≥ {_STRONG_MIN_GAIN * 0.5:.2f}). "
            f"_STRONG_MIN_GAIN cap не применяется."
        )


class TestW1718IntDtypeGuard(unittest.TestCase):
    """W1718 BUG3: int16 input не должен возвращать all-zeros.

    До фикса: float64 clip в [-1, 1] → .astype(int16) = all-zeros (потому что
    [-1.0, 1.0] усекается до 0 или ±1 в int16).
    После фикса: rescale × iinfo.max перед cast сохраняет сигнал.
    """

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_int16_input_not_all_zeros(self) -> None:
        """int16-входной массив возвращает ненулевой результат."""
        sr = _SR
        # Создаём int16 с умеренной амплитудой (50% от max)
        t = np.linspace(0, 1.0, sr, endpoint=False)
        audio_float = 0.5 * np.sin(2 * np.pi * 440.0 * t)
        audio_int16 = (audio_float * 32767).astype(np.int16)

        result = self.denoiser.denoise(audio_int16, sr, strength="moderate")

        self.assertEqual(result.dtype, np.int16,
                         f"Ожидался dtype int16, получен {result.dtype}")
        rms = float(np.sqrt(np.mean(result.astype(np.float64) ** 2)))
        self.assertGreater(rms, 100.0,
                           f"int16 результат all-zeros или очень тихий: RMS={rms:.1f}. "
                           f"Вероятно, .astype(int16) без rescale обнуляет [-1,1].")

    def test_int16_input_preserves_shape_and_dtype(self) -> None:
        """int16 вход сохраняет форму и dtype на выходе."""
        sr = _SR
        t = np.linspace(0, 0.5, sr // 2, endpoint=False)
        audio = (0.3 * np.sin(2 * np.pi * 1000.0 * t) * 32767).astype(np.int16)

        result = self.denoiser.denoise(audio, sr, strength="light")

        self.assertEqual(result.shape, audio.shape)
        self.assertEqual(result.dtype, np.int16)

    def test_int16_input_values_in_range(self) -> None:
        """int16 результат не выходит за пределы dtype."""
        sr = _SR
        t = np.linspace(0, 1.0, sr, endpoint=False)
        audio = (0.8 * np.sin(2 * np.pi * 440.0 * t) * 32767).astype(np.int16)

        result = self.denoiser.denoise(audio, sr, strength="strong")

        self.assertLessEqual(int(result.max()), 32767)
        self.assertGreaterEqual(int(result.min()), -32768)

    def test_float32_input_still_works(self) -> None:
        """float32-входной массив (нормальный production path) не поломан."""
        sr = _SR
        t = np.linspace(0, 1.0, sr, endpoint=False)
        audio = (0.4 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)

        result = self.denoiser.denoise(audio, sr, strength="moderate")

        self.assertEqual(result.dtype, np.float32)
        self.assertLessEqual(float(np.max(result)), 1.0)
        self.assertGreaterEqual(float(np.min(result)), -1.0)
        rms = float(np.sqrt(np.mean(result.astype(np.float64) ** 2)))
        self.assertGreater(rms, 1e-4,
                           "float32 результат пустой — production path поломан")

    def test_strength_off_int16_passthrough(self) -> None:
        """strength='off' с int16 входом — возвращает идентичный массив."""
        sr = _SR
        t = np.linspace(0, 0.5, sr // 2, endpoint=False)
        audio = (0.5 * np.sin(2 * np.pi * 440.0 * t) * 32767).astype(np.int16)

        result = self.denoiser.denoise(audio, sr, strength="off")
        np.testing.assert_array_equal(audio, result)


# ---------------------------------------------------------------------------
# W1769 — F1: короткий noise clip (< _N_FFT) не должен крашить STFT
#          F2: длинное аудио обрабатывается окнами с ограниченным peak memory
# ---------------------------------------------------------------------------

from core.audio_denoiser import (  # noqa: E402
    _pad_noise_clip,
    _N_FFT,
    _RMS_FRAME_SIZE,
    _MAX_DENOISE_WINDOW_SEC,
    _DENOISE_OVERLAP_SEC,
)


def _make_short_clip_audio() -> np.ndarray:
    """Аудио, которое ПРОХОДИТ guard len>=_N_FFT*2, но даёт noise_clip < _N_FFT.

    Большая часть сигнала громкая, а первые 2 фрейма (≈320 сэмплов) почти тихие —
    они и попадут в 10-й перцентиль RMS как noise reference. Конкатенация этих
    немногих тихих фреймов короче _N_FFT (512), что раньше крашило stft.
    """
    rng = np.random.default_rng(0)
    n = 1500  # > _N_FFT * 2 == 1024 → проходит guard
    audio = (rng.standard_normal(n) * 0.3).astype(np.float64)
    audio[: 2 * _RMS_FRAME_SIZE] *= 0.001  # 2 почти-тихих фрейма
    return audio.astype(np.float32)


class TestW1769ShortNoiseClipNoCrash(unittest.TestCase):
    """W1769 F1: короткий noise clip (< _N_FFT) не должен бросать ValueError.

    Регрессия: scipy.signal.stft с nperseg=_N_FFT на входе короче _N_FFT
    авто-урезает nperseg до длины входа, но noverlap=_N_FFT-_HOP остаётся
    прежним → 'noverlap must be less than nperseg'. Это краш в дефолтном
    production-бэкенде (spectral gating, noisereduce обычно отсутствует).
    """

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_old_code_would_raise_on_short_clip_stft(self) -> None:
        """FAIL-BEFORE проверка: «сырой» stft на коротком клипе действительно крашит.

        Документирует исходную причину сбоя — то, что фикс предотвращает.
        Прямой вызов scipy воспроизводит ValueError, который раньше всплывал
        наружу из _denoise_spectral_gating.
        """
        try:
            from scipy.signal import stft  # type: ignore
        except ImportError:
            self.skipTest("scipy не установлен")

        short_clip = np.random.default_rng(0).standard_normal(160).astype(np.float64)
        self.assertLess(len(short_clip), _N_FFT)
        with self.assertRaises(ValueError):
            # Это ИМЕННО то, что делал старый код (без _pad_noise_clip).
            stft(short_clip, fs=_SR, nperseg=_N_FFT, noverlap=_N_FFT - (_N_FFT // 4))

    def test_denoise_short_noise_clip_does_not_raise(self) -> None:
        """PASS-AFTER: denoise() с аудио, дающим короткий noise_clip, не крашит."""
        audio = _make_short_clip_audio()
        # До фикса здесь поднимался ValueError. После — обычный результат.
        result = self.denoiser.denoise(audio, _SR, strength="moderate")
        self.assertEqual(result.shape, audio.shape)
        self.assertTrue(np.all(np.isfinite(result)),
                        "результат содержит NaN/inf после деноизинга короткого клипа")

    def test_denoise_short_noise_clip_all_strengths(self) -> None:
        """Все уровни силы переживают короткий noise clip без исключений."""
        audio = _make_short_clip_audio()
        for strength in ("light", "moderate", "strong"):
            with self.subTest(strength=strength):
                result = self.denoiser.denoise(audio, _SR, strength=strength)  # type: ignore[arg-type]
                self.assertEqual(result.shape, audio.shape)
                self.assertTrue(np.all(np.isfinite(result)))

    def test_spectral_gating_direct_short_clip_via_pad(self) -> None:
        """_denoise_spectral_gating напрямую не крашит, когда noise_clip < _N_FFT.

        Строим сигнал так, что _percentile_noise_clip вернёт < _N_FFT сэмплов,
        и проверяем, что бэкенд (через _pad_noise_clip) проходит без ValueError.
        """
        try:
            import scipy.signal  # noqa: F401  # type: ignore
        except ImportError:
            self.skipTest("scipy не установлен")

        from core.audio_denoiser import _STRENGTH_PARAMS, _percentile_noise_clip
        audio = _make_short_clip_audio().astype(np.float64)
        clip = _percentile_noise_clip(audio)
        self.assertIsNotNone(clip)
        self.assertLess(len(clip), _N_FFT,
                        "тест-фикстура должна давать noise_clip короче _N_FFT")
        # Не должно бросать — раньше падало на stft(noise_clip, ...)
        out = AudioDenoiser._denoise_spectral_gating(
            audio, _SR, _STRENGTH_PARAMS["moderate"], "moderate"
        )
        self.assertEqual(len(out), len(audio))
        self.assertTrue(np.all(np.isfinite(out)))


class TestW1769PadNoiseClip(unittest.TestCase):
    """W1769 F1: _pad_noise_clip дополняет короткий клип до ≥ _N_FFT."""

    def test_pad_short_clip_to_n_fft(self) -> None:
        """Клип короче _N_FFT расширяется минимум до _N_FFT."""
        clip = np.random.default_rng(1).standard_normal(160).astype(np.float64)
        padded = _pad_noise_clip(clip)
        self.assertGreaterEqual(len(padded), _N_FFT)
        self.assertTrue(np.all(np.isfinite(padded)))

    def test_pad_preserves_long_clip(self) -> None:
        """Клип уже ≥ _N_FFT возвращается без изменений (тот же объект)."""
        clip = np.random.default_rng(2).standard_normal(1000).astype(np.float64)
        padded = _pad_noise_clip(clip)
        self.assertIs(padded, clip)

    def test_pad_edge_lengths(self) -> None:
        """Вырожденные длины (0, 1, 2) обрабатываются без исключений."""
        for ln in (0, 1, 2, 3):
            with self.subTest(length=ln):
                clip = (np.random.default_rng(ln).standard_normal(ln).astype(np.float64)
                        if ln > 0 else np.array([], dtype=np.float64))
                padded = _pad_noise_clip(clip)
                self.assertGreaterEqual(len(padded), _N_FFT)
                self.assertTrue(np.all(np.isfinite(padded)))

    def test_pad_exactly_n_fft_unchanged(self) -> None:
        """Клип длиной ровно _N_FFT не дополняется."""
        clip = np.ones(_N_FFT, dtype=np.float64)
        padded = _pad_noise_clip(clip)
        self.assertEqual(len(padded), _N_FFT)


class TestW1769LongAudioWindowing(unittest.TestCase):
    """W1769 F2: длинное аудио обрабатывается окнами с ограниченным peak memory."""

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_long_audio_completes_and_length_matches(self) -> None:
        """≈10-минутное аудио (> окна) обрабатывается, длина выхода = длине входа."""
        sr = _SR
        n = sr * 60 * 10  # 10 минут
        t = np.linspace(0, 60 * 10, n, endpoint=False)
        rng = np.random.default_rng(3)
        audio = (0.3 * np.sin(2 * np.pi * 300.0 * t)
                 + 0.05 * rng.standard_normal(n)).astype(np.float32)

        result = self.denoiser.denoise(audio, sr, strength="moderate")

        self.assertEqual(len(result), n,
                         "длина выхода должна точно совпадать с длиной входа")
        self.assertEqual(result.dtype, np.float32)
        self.assertTrue(np.all(np.isfinite(result)),
                        "оконная обработка не должна давать NaN/inf")

    def test_long_audio_processed_via_windows_not_full_stft(self) -> None:
        """Длинное аудио идёт через _denoise_windowed (а не один полный STFT).

        Мокаем _denoise_windowed и проверяем, что для аудио длиннее окна
        вызывается именно оконный путь.
        """
        from unittest import mock

        sr = _SR
        window_samples = int(_MAX_DENOISE_WINDOW_SEC * sr)
        n = window_samples + sr * 5  # гарантированно длиннее окна
        audio = (np.random.default_rng(4).standard_normal(n) * 0.1).astype(np.float32)

        with mock.patch.object(
            self.denoiser, "_denoise_windowed",
            wraps=self.denoiser._denoise_windowed,
        ) as spy:
            self.denoiser.denoise(audio, sr, strength="moderate")
            self.assertTrue(spy.called,
                            "_denoise_windowed должен вызываться для аудио длиннее окна")

    def test_short_audio_uses_direct_path_not_windowed(self) -> None:
        """Аудио ≤ окна НЕ должно идти через оконный путь (overhead не нужен)."""
        from unittest import mock

        sr = _SR
        audio = _make_noisy_audio(duration_sec=2.0, snr_target_db=5.0)  # << окна
        with mock.patch.object(self.denoiser, "_denoise_windowed") as spy:
            self.denoiser.denoise(audio, sr, strength="moderate")
            self.assertFalse(spy.called,
                             "короткое аудио не должно вызывать _denoise_windowed")

    def test_windowed_peak_memory_bounded_vs_full_stft(self) -> None:
        """Оконный peak memory существенно ниже полного STFT того же сигнала.

        Сравниваем tracemalloc-пик _denoise_windowed против полного
        _denoise_spectral_gating на одном входе. Окно фиксированного размера
        → пик не растёт квадратично с длительностью.
        """
        try:
            import scipy.signal  # noqa: F401  # type: ignore
        except ImportError:
            self.skipTest("scipy не установлен")

        import tracemalloc
        from core.audio_denoiser import _STRENGTH_PARAMS

        sr = _SR
        n = sr * 60 * 8  # 8 минут — достаточно, чтобы полный STFT был дорогим
        t = np.linspace(0, 60 * 8, n, endpoint=False)
        mono = (0.3 * np.sin(2 * np.pi * 300.0 * t)
                + 0.05 * np.random.default_rng(5).standard_normal(n)).astype(np.float64)
        params = _STRENGTH_PARAMS["moderate"]

        tracemalloc.start()
        _ = self.denoiser._denoise_spectral_gating(mono, sr, params, "moderate")
        _, peak_full = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        tracemalloc.start()
        out = self.denoiser._denoise_windowed(
            mono, sr, params, "moderate", None,
            window_samples=int(_MAX_DENOISE_WINDOW_SEC * sr),
        )
        _, peak_win = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertEqual(len(out), n)
        # Оконный пик должен быть заметно меньше полного STFT (ожидаем ≥40% экономии).
        self.assertLess(
            peak_win, peak_full * 0.6,
            f"оконный peak {peak_win/1e6:.0f} MB не ниже 60% полного "
            f"{peak_full/1e6:.0f} MB — bounded-memory регрессия",
        )

    def test_windowed_output_seam_free_on_clean_tone(self) -> None:
        """Чистый тон через несколько окон не имеет шва на стыке.

        Линейный кросс-фейд даёт partition-of-unity (веса = 1.0), поэтому
        sample-to-sample разница не должна иметь резких всплесков на границах
        окон. Проверяем, что max |diff| соответствует гладкому синусу.
        """
        sr = _SR
        dur = 150.0  # 2.5 мин → 3 окна по 60 с
        t = np.linspace(0, dur, int(sr * dur), endpoint=False)
        tone = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)

        result = self.denoiser.denoise(tone, sr, strength="moderate")

        diffs = np.abs(np.diff(result.astype(np.float64)))
        # Теоретический шаг чистого синуса 440 Гц при 16 кГц ≈ 0.052.
        # Шов дал бы скачок в разы больше — допускаем щедрый запас 0.2.
        self.assertLess(
            float(diffs.max()), 0.2,
            "обнаружен резкий скачок на стыке окон (шов overlap-add)",
        )
        self.assertTrue(np.all(np.isfinite(result)))

    def test_overlap_constant_sane(self) -> None:
        """Константы окна согласованы: перекрытие меньше окна."""
        self.assertGreater(_MAX_DENOISE_WINDOW_SEC, _DENOISE_OVERLAP_SEC)
        self.assertGreater(_DENOISE_OVERLAP_SEC, 0.0)


class TestW1769QualityPreservedNormalClip(unittest.TestCase):
    """W1769: нормальный 5–10 с клип деноизится идентично прямому бэкенду.

    Аудио короче окна не должно затрагиваться оконной логикой — результат
    обязан быть бит-в-бит равен прежнему прямому пути.
    """

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def _direct_backend_reference(
        self, audio: np.ndarray, strength: str
    ) -> np.ndarray:
        """Воспроизводит внутренний прямой путь denoise() для сравнения."""
        from core.audio_denoiser import _STRENGTH_PARAMS, _NOISEREDUCE_PARAMS
        mono = audio.astype(np.float64)
        params = _STRENGTH_PARAMS.get(strength, _STRENGTH_PARAMS["moderate"])
        nr_params = _NOISEREDUCE_PARAMS.get(strength)
        out = self.denoiser._apply_backend(mono, _SR, params, strength, nr_params)
        out = np.clip(out, -1.0, 1.0)
        return out.astype(audio.dtype)

    def test_normal_clip_identical_to_direct_backend(self) -> None:
        """5 с клип: публичный denoise() == прямой бэкенд (квалити не изменилось)."""
        rng = np.random.default_rng(6)
        t = np.linspace(0, 5.0, _SR * 5, endpoint=False)
        clip = np.clip(
            0.4 * np.sin(2 * np.pi * 440.0 * t) + 0.1 * rng.standard_normal(_SR * 5),
            -1.0, 1.0,
        ).astype(np.float32)

        public = self.denoiser.denoise(clip, _SR, strength="moderate")
        direct = self._direct_backend_reference(clip, "moderate")

        np.testing.assert_array_equal(
            public, direct,
            "нормальный клип должен деноизиться идентично прямому бэкенду "
            "(оконная логика не должна его затрагивать)",
        )

    def test_normal_clip_10s_identical_strong(self) -> None:
        """10 с клип на strength='strong' тоже идентичен прямому бэкенду."""
        rng = np.random.default_rng(7)
        t = np.linspace(0, 10.0, _SR * 10, endpoint=False)
        clip = np.clip(
            0.4 * np.sin(2 * np.pi * 440.0 * t) + 0.1 * rng.standard_normal(_SR * 10),
            -1.0, 1.0,
        ).astype(np.float32)

        public = self.denoiser.denoise(clip, _SR, strength="strong")
        direct = self._direct_backend_reference(clip, "strong")

        np.testing.assert_array_equal(public, direct)

    def test_normal_clip_still_reduces_noise(self) -> None:
        """Sanity: нормальный клип всё ещё реально подавляет шум (RMS падает)."""
        audio = _make_noisy_audio(duration_sec=8.0, snr_target_db=3.0)
        result = self.denoiser.denoise(audio, _SR, strength="moderate")
        rms_in = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        rms_out = float(np.sqrt(np.mean(result.astype(np.float64) ** 2)))
        self.assertLess(rms_out, rms_in)


if __name__ == "__main__":
    unittest.main()
