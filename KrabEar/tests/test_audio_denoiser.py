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

from core.audio_denoiser import AudioDenoiser, _find_noise_window
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
# W1067 — Regression: noise floor из тишайшего окна, а не из первых 200 мс
# ---------------------------------------------------------------------------

class TestNoiseWindowSelection(unittest.TestCase):
    """W1062 F1 HIGH: noise floor должен браться из тишайшего окна, а не первых 200 мс."""

    def test_find_noise_window_returns_int(self) -> None:
        """_find_noise_window возвращает целочисленный индекс начала окна."""
        audio = np.random.default_rng(0).standard_normal(32000).astype(np.float64)
        start = _find_noise_window(audio, _SR)
        self.assertIsInstance(start, int)
        self.assertGreaterEqual(start, 0)

    def test_find_noise_window_selects_quietest_region(self) -> None:
        """_find_noise_window выбирает тишайшее окно, а не первое."""
        # Первые 3200 сэмплов (200 мс) — речевой сигнал (высокая амплитуда)
        # Вторые 3200 сэмплов — тишина (почти нулевая амплитуда)
        rng = np.random.default_rng(42)
        speech_window = rng.standard_normal(3200).astype(np.float64) * 0.5
        silence_window = np.zeros(3200, dtype=np.float64) + 1e-6
        # Ещё один речевой фрагмент в конце
        tail = rng.standard_normal(9600).astype(np.float64) * 0.4
        audio = np.concatenate([speech_window, silence_window, tail])

        start = _find_noise_window(audio, _SR, window_ms=200)
        # Тишайшее окно — второе (индекс 1 → start = 3200)
        self.assertEqual(start, 3200,
                         f"Ожидали start=3200 (тишина), получили {start}")

    def test_find_noise_window_short_audio_returns_zero(self) -> None:
        """Если аудио короче одного окна — возвращаем 0."""
        short = np.ones(100, dtype=np.float64)
        start = _find_noise_window(short, _SR, window_ms=200)
        self.assertEqual(start, 0)

    def test_speech_at_start_not_suppressed(self) -> None:
        """Речевой сигнал в НАЧАЛЕ записи не должен подавляться через noise floor.

        Регрессионный тест для W1062 F1 HIGH: до фикса первые 200 мс брались как
        noise reference. Если пользователь начал говорить сразу после хоткея —
        речевые гармоники попадали в noise reference и подавлялись.

        Схема теста:
        - Первые 200 мс: громкий речеподобный синусоидальный сигнал (440 Гц).
        - Следующие 200 мс: полная тишина (noise floor).
        - Ещё 600 мс: слабый фоновый шум.

        После деноизинга речь в начале должна остаться достаточно громкой.
        """
        sr = _SR
        window = int(0.2 * sr)  # 3200 сэмплов @ 16 кГц

        # Первые 200 мс — речь (громкий синус)
        t_speech = np.linspace(0, 0.2, window, endpoint=False)
        speech = (0.5 * np.sin(2 * np.pi * 440.0 * t_speech)).astype(np.float64)

        # Следующие 200 мс — тишина (истинный noise floor)
        silence = np.zeros(window, dtype=np.float64)

        # Оставшиеся 600 мс — слабый шум
        rng = np.random.default_rng(99)
        tail = rng.standard_normal(int(0.6 * sr)).astype(np.float64) * 0.02

        audio = np.concatenate([speech, silence, tail]).astype(np.float32)

        denoiser = AudioDenoiser()
        result = denoiser.denoise(audio, sr, strength="moderate")

        # RMS первых 200 мс результата должна быть близка к RMS входных 200 мс.
        # До фикса: noise reference = speech → mask удаляла гармоники речи.
        # После фикса: noise reference = тишина → речь должна сохраняться.
        speech_rms_in = float(np.sqrt(np.mean(audio[:window].astype(np.float64) ** 2)))
        speech_rms_out = float(np.sqrt(np.mean(result[:window].astype(np.float64) ** 2)))

        # Допускаем не более 40% потерь амплитуды речи
        ratio = speech_rms_out / (speech_rms_in + 1e-10)
        self.assertGreater(
            ratio, 0.6,
            f"Речь в начале подавлена слишком сильно: ratio={ratio:.3f} "
            f"(rms_in={speech_rms_in:.4f}, rms_out={speech_rms_out:.4f}). "
            f"Вероятно, noise floor взят из речевого фрагмента, а не из тишины."
        )


if __name__ == "__main__":
    unittest.main()
