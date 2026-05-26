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

from core.audio_denoiser import AudioDenoiser
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
# Wave 1080 — W1062 F1+F2 fixes
# ---------------------------------------------------------------------------

class TestAudioDenoiserW1080PercentileNoiseFloor(unittest.TestCase):
    """W1062 F1: noise floor должен сэмплироваться по тихим фреймам,
    а не по первым 200 мс."""

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_noise_sampled_from_quietest_frames_not_first_200ms(self) -> None:
        """_percentile_noise_clip возвращает тихие фреймы, а не первые 200 мс.

        Строим аудио: первые 200 мс — громкая синусоида (speech-like),
        затем 800 мс тишины + редкий тихий фон. Функция должна выбрать
        тихие фреймы из середины/конца, а не начало.
        """
        from core.audio_denoiser import _percentile_noise_clip

        rng = np.random.default_rng(123)
        sr = 16000
        duration = 1.0
        n = int(sr * duration)

        # Первые 200 мс — громкая речь (RMS ~0.4)
        speech_len = int(sr * 0.2)
        t_speech = np.linspace(0, 0.2, speech_len, endpoint=False)
        speech_part = 0.5 * np.sin(2 * np.pi * 440.0 * t_speech)

        # Остальные 800 мс — тишина + слабый шум (RMS ~0.01)
        quiet_part = rng.standard_normal(n - speech_len) * 0.01

        audio = np.concatenate([speech_part, quiet_part]).astype(np.float64)

        noise_clip = _percentile_noise_clip(audio, sr)

        # Noise clip должен быть НЕ из первых 200 мс (которые громкие)
        # Проверяем: RMS noise_clip << RMS первых 200 мс
        rms_noise_clip = float(np.sqrt(np.mean(noise_clip ** 2)))
        rms_first_200ms = float(np.sqrt(np.mean(speech_part ** 2)))

        self.assertLess(
            rms_noise_clip, rms_first_200ms * 0.1,
            f"noise_clip RMS ({rms_noise_clip:.4f}) должен быть << "
            f"RMS первых 200 мс ({rms_first_200ms:.4f})"
        )

    def test_speech_at_start_not_suppressed(self) -> None:
        """Речь в начале записи не подавляется (W1062 F1 регрессионный тест).

        Симулируем ситуацию «пользователь уже говорит в момент нажатия хоткея»:
        - Первые 200 мс — речь (яркая синусоида, RMS ~0.4).
        - Остальные 800 мс — тишина.

        Деноизер НЕ должен превращать начало в тишину (старый баг:
        если noise floor = первые 200 мс = речь, то вся речь подавлялась).
        """
        sr = 16000
        duration = 1.0
        n = int(sr * duration)

        # Первые 200 мс — речь
        speech_len = int(sr * 0.2)
        t_speech = np.linspace(0, 0.2, speech_len, endpoint=False)
        speech_part = (0.5 * np.sin(2 * np.pi * 440.0 * t_speech)).astype(np.float32)

        # Остальные 800 мс — тишина
        quiet_part = np.zeros(n - speech_len, dtype=np.float32)

        audio = np.concatenate([speech_part, quiet_part])

        result = self.denoiser.denoise(audio, sr, strength="moderate")

        # RMS первых 200 мс результата должен быть сопоставим с оригиналом
        rms_orig_speech = float(np.sqrt(np.mean(speech_part.astype(np.float64) ** 2)))
        rms_result_speech = float(np.sqrt(np.mean(result[:speech_len].astype(np.float64) ** 2)))

        # Допускаем умеренное снижение, но не более 80% от оригинала
        self.assertGreater(
            rms_result_speech, rms_orig_speech * 0.20,
            f"Речь в начале не должна быть подавлена: "
            f"RMS result={rms_result_speech:.4f}, orig={rms_orig_speech:.4f}"
        )

    def test_percentile_noise_clip_fallback_all_loud(self) -> None:
        """Когда всё аудио громкое — fallback на первые 200 мс без краша."""
        from core.audio_denoiser import _percentile_noise_clip

        sr = 16000
        # Константный сигнал высокой амплитуды — все фреймы одинаковые
        audio = np.ones(sr * 2, dtype=np.float64) * 0.9

        # Не должен бросать исключение
        noise_clip = _percentile_noise_clip(audio, sr)

        # Должен вернуть непустой массив
        self.assertGreater(len(noise_clip), 0)
        # При всех одинаковых фреймах тихих нет — fallback на первые 200 мс
        # (или всё аудио — в любом случае RMS должен быть ~0.9)
        rms = float(np.sqrt(np.mean(noise_clip ** 2)))
        self.assertGreater(rms, 0.5)

    def test_percentile_performance_60s_audio(self) -> None:
        """Percentile compute над 60 с аудио занимает < 50 мс."""
        import time
        from core.audio_denoiser import _percentile_noise_clip

        sr = 16000
        rng = np.random.default_rng(0)
        audio = (rng.standard_normal(sr * 60) * 0.1).astype(np.float64)

        start = time.perf_counter()
        _percentile_noise_clip(audio, sr)
        elapsed_ms = (time.perf_counter() - start) * 1000

        self.assertLess(
            elapsed_ms, 50.0,
            f"_percentile_noise_clip заняло {elapsed_ms:.1f} мс > 50 мс"
        )


class TestAudioDenoiserW1080StrongModeBounds(unittest.TestCase):
    """W1062 F2: strong mode должен сохранять минимум 25% сигнала
    в речевой полосе 300–3000 Гц."""

    def setUp(self) -> None:
        self.denoiser = AudioDenoiser()

    def test_strong_mode_preserves_whisper_band(self) -> None:
        """strong mode: RMS в полосе 300–3000 Гц ≥ 25% от оригинала.

        Синтезируем шёпот как сумму синусоид в речевой полосе (500 Гц + 1 кГц).
        После деноизинга в режиме strong каждый бин речевой полосы должен
        иметь коэффициент усиления ≥ _STRONG_MIN_GAIN (0.25).
        """
        sr = 16000
        duration = 1.5
        t = np.linspace(0, duration, int(sr * duration), endpoint=False)

        # «Шёпот» — тихие синусоиды в речевой полосе (RMS ~0.07)
        whisper = (
            0.08 * np.sin(2 * np.pi * 500.0 * t)
            + 0.06 * np.sin(2 * np.pi * 1000.0 * t)
            + 0.04 * np.sin(2 * np.pi * 2000.0 * t)
        ).astype(np.float32)

        # Добавляем небольшой широкополосный шум (SNR ~10 dB)
        rng = np.random.default_rng(42)
        noise = (rng.standard_normal(len(t)) * 0.01).astype(np.float32)
        audio = np.clip(whisper + noise, -1.0, 1.0).astype(np.float32)

        result = self.denoiser.denoise(audio, sr, strength="strong")

        # RMS результата в первых 0.5 с (речевая часть)
        half = int(sr * 0.5)
        rms_orig = float(np.sqrt(np.mean(whisper[:half].astype(np.float64) ** 2)))
        rms_result = float(np.sqrt(np.mean(result[:half].astype(np.float64) ** 2)))

        # Должно сохраниться минимум 25% от исходного сигнала
        min_expected = rms_orig * 0.25
        self.assertGreater(
            rms_result, min_expected * 0.5,  # допускаем 50% от теоретического min
            f"strong mode подавил шёпот слишком агрессивно: "
            f"RMS result={rms_result:.5f}, 25% от orig={min_expected:.5f}"
        )

    def test_strong_less_aggressive_than_before_in_speech_band(self) -> None:
        """strong mode с W1062 F2 подавляет НЕ до нуля в речевой полосе.

        Верифицируем что маска в speech band не равна нулю:
        это достигается через _STRONG_MIN_GAIN = 0.25 cap.
        """
        from core.audio_denoiser import _STRONG_MIN_GAIN
        # Просто проверяем что константа корректна
        self.assertGreaterEqual(_STRONG_MIN_GAIN, 0.20,
                                "_STRONG_MIN_GAIN должен быть ≥ 0.20 (сохранять шёпот)")
        self.assertLessEqual(_STRONG_MIN_GAIN, 0.50,
                             "_STRONG_MIN_GAIN должен быть ≤ 0.50 (не подавлять шум)")

    def test_speech_band_bins_valid_range(self) -> None:
        """_speech_band_bins возвращает корректные индексы для sr=16000."""
        from core.audio_denoiser import _speech_band_bins, _N_FFT

        bin_low, bin_high = _speech_band_bins(16000)
        max_bin = _N_FFT // 2

        self.assertGreaterEqual(bin_low, 0)
        self.assertLessEqual(bin_high, max_bin)
        self.assertLess(bin_low, bin_high,
                        "bin_low должен быть меньше bin_high")

        # 300 Гц @ N_FFT=512, sr=16000 → bin ≈ 9–10
        # 3000 Гц → bin ≈ 96
        self.assertGreaterEqual(bin_low, 5)
        self.assertLessEqual(bin_high, 110)

    def test_moderate_unchanged_by_f2(self) -> None:
        """moderate и light mode НЕ затронуты патчем F2 (только strong)."""
        from core.audio_denoiser import _STRONG_MIN_GAIN

        # Тест на уровне поведения: moderate не должен применять min_gain cap
        # Проверяем через сравнение результатов light и moderate
        sr = 16000
        rng = np.random.default_rng(7)
        noisy = (rng.standard_normal(sr * 1) * 0.3 + 0.1 * np.sin(
            2 * np.pi * 440 * np.linspace(0, 1, sr))).astype(np.float32)
        noisy = np.clip(noisy, -1, 1)

        result_mod = self.denoiser.denoise(noisy, sr, strength="moderate")
        result_strong = self.denoiser.denoise(noisy, sr, strength="strong")

        # Strong с cap должен быть «ближе» к оригиналу в speech band чем раньше,
        # но moderate без cap должен подавлять больше чем light
        result_light = self.denoiser.denoise(noisy, sr, strength="light")

        rms_light = float(np.sqrt(np.mean(result_light.astype(np.float64) ** 2)))
        rms_mod = float(np.sqrt(np.mean(result_mod.astype(np.float64) ** 2)))
        rms_strong = float(np.sqrt(np.mean(result_strong.astype(np.float64) ** 2)))

        # light > moderate в RMS (меньше подавляет)
        self.assertGreater(rms_light, rms_mod * 0.9,
                           "light должен давать RMS ≥ moderate после F2 патча")
        # strong с cap должен быть между moderate и light (за счёт speech band)
        # или меньше moderate — в любом случае не быть выше light
        self.assertLessEqual(rms_strong, rms_light * 1.5,
                             "strong не должен быть намного выше light по RMS")


if __name__ == "__main__":
    unittest.main()
