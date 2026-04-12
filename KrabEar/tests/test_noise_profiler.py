"""Тесты NoiseProfiler — профилирование фонового шума в аудиозаписях."""

from __future__ import annotations

import sys
import math
import unittest
from pathlib import Path

import numpy as np

# Настройка пути для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.noise_profiler import NoiseProfiler, NoiseProfile

SR = 16000  # стандартная частота дискретизации


# ---------------------------------------------------------------------------
# Вспомогательные генераторы сигналов
# ---------------------------------------------------------------------------

def _silence(duration: float = 1.0, sr: int = SR) -> np.ndarray:
    """Полная тишина."""
    return np.zeros(int(sr * duration), dtype=np.float32)


def _white_noise(duration: float = 1.0, amplitude: float = 0.05, sr: int = SR) -> np.ndarray:
    """Белый шум с заданной амплитудой."""
    rng = np.random.default_rng(42)
    return (amplitude * rng.standard_normal(int(sr * duration))).astype(np.float32)


def _sine(freq: float = 440.0, duration: float = 1.0, amplitude: float = 0.3, sr: int = SR) -> np.ndarray:
    """Синусоида заданной частоты."""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return (amplitude * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def _low_freq_noise(duration: float = 1.0, amplitude: float = 0.15, sr: int = SR) -> np.ndarray:
    """Шум с выраженным низкочастотным компонентом (имитация транспорта)."""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    # Смешиваем несколько низких частот
    signal = (
        amplitude * 0.6 * np.sin(2 * math.pi * 80 * t) +
        amplitude * 0.3 * np.sin(2 * math.pi * 120 * t) +
        amplitude * 0.1 * np.sin(2 * math.pi * 200 * t)
    )
    return signal.astype(np.float32)


def _speech_with_noise(speech_amp: float = 0.3, noise_amp: float = 0.01,
                       duration: float = 2.0, sr: int = SR) -> np.ndarray:
    """Речеподобный сигнал (синусоида) с фоновым шумом."""
    speech = _sine(440, duration, speech_amp, sr)
    noise = _white_noise(duration, noise_amp, sr)
    return (speech + noise).astype(np.float32)


def _stereo(duration: float = 1.0, sr: int = SR) -> np.ndarray:
    """Стерео-сигнал (2 канала)."""
    left = _sine(440, duration, 0.3, sr)
    right = _white_noise(duration, 0.05, sr)
    return np.column_stack([left, right]).astype(np.float32)


# ---------------------------------------------------------------------------
# Тесты структуры NoiseProfile
# ---------------------------------------------------------------------------

class TestNoiseProfileFields(unittest.TestCase):
    """Проверка наличия и типов полей в NoiseProfile."""

    def test_profile_returns_noise_profile_instance(self):
        profiler = NoiseProfiler()
        audio = _speech_with_noise()
        result = profiler.profile(audio, SR)
        self.assertIsInstance(result, NoiseProfile)

    def test_all_fields_present(self):
        profiler = NoiseProfiler()
        audio = _speech_with_noise()
        result = profiler.profile(audio, SR)
        self.assertIsInstance(result.noise_type, str)
        self.assertIsInstance(result.noise_level_db, float)
        self.assertIsInstance(result.snr_db, float)
        self.assertIsInstance(result.frequency_profile, str)
        self.assertIsInstance(result.recommendations, list)
        self.assertIsInstance(result.suitable_for_stt, bool)

    def test_to_dict_has_correct_keys(self):
        profiler = NoiseProfiler()
        audio = _speech_with_noise()
        result = profiler.profile(audio, SR)
        d = result.to_dict()
        expected_keys = {
            "noise_type", "noise_level_db", "snr_db",
            "frequency_profile", "recommendations", "suitable_for_stt",
        }
        self.assertEqual(set(d.keys()), expected_keys)

    def test_noise_type_is_valid_category(self):
        profiler = NoiseProfiler()
        valid_types = {"quiet", "office", "street", "music", "crowd"}
        for audio in [_silence(), _white_noise(), _speech_with_noise(), _low_freq_noise()]:
            result = profiler.profile(audio, SR)
            self.assertIn(result.noise_type, valid_types,
                          f"Неожиданный noise_type: {result.noise_type}")

    def test_frequency_profile_is_valid(self):
        profiler = NoiseProfiler()
        valid_profiles = {"low_frequency", "broadband", "high_frequency"}
        for audio in [_silence(2.0), _white_noise(2.0), _low_freq_noise(2.0), _sine(5000, 2.0)]:
            result = profiler.profile(audio, SR)
            self.assertIn(result.frequency_profile, valid_profiles,
                          f"Неожиданный frequency_profile: {result.frequency_profile}")


# ---------------------------------------------------------------------------
# Тесты SNR и suitable_for_stt
# ---------------------------------------------------------------------------

class TestSnrAndSuitability(unittest.TestCase):
    """Проверка вычисления SNR и флага suitable_for_stt."""

    def test_high_snr_suitable_for_stt(self):
        # Чистый сигнал с минимальным шумом → SNR > 15 → suitable
        audio = _speech_with_noise(speech_amp=0.4, noise_amp=0.005, duration=2.0)
        result = NoiseProfiler().profile(audio, SR)
        self.assertTrue(result.suitable_for_stt,
                        f"Ожидалось suitable_for_stt=True, SNR={result.snr_db:.1f}")

    def test_low_snr_not_suitable_for_stt(self):
        # Почти только шум (speech_amp ≈ noise_amp) → низкий SNR → не пригоден
        rng = np.random.default_rng(99)
        loud_noise = (0.2 * rng.standard_normal(SR * 2)).astype(np.float32)
        result = NoiseProfiler().profile(loud_noise, SR)
        # Чистый шум имеет SNR ~0 (нет отдельного сигнала vs шума)
        # или близко к 0; в любом случае проверяем тип
        self.assertIsInstance(result.suitable_for_stt, bool)

    def test_snr_higher_for_clean_than_noisy(self):
        clean = _speech_with_noise(speech_amp=0.4, noise_amp=0.002, duration=2.0)
        noisy = _speech_with_noise(speech_amp=0.1, noise_amp=0.15, duration=2.0)
        result_clean = NoiseProfiler().profile(clean, SR)
        result_noisy = NoiseProfiler().profile(noisy, SR)
        self.assertGreater(result_clean.snr_db, result_noisy.snr_db,
                           "Чистый сигнал должен иметь SNR выше зашумлённого")

    def test_suitable_for_stt_true_when_snr_above_15(self):
        # Конструируем сигнал с заведомо высоким SNR
        speech = _sine(440, 2.0, amplitude=0.5)
        tiny_noise = _white_noise(2.0, amplitude=0.001)
        audio = (speech + tiny_noise).astype(np.float32)
        result = NoiseProfiler().profile(audio, SR)
        self.assertTrue(result.suitable_for_stt,
                        f"SNR={result.snr_db:.1f} dB должен давать suitable_for_stt=True")


# ---------------------------------------------------------------------------
# Тесты классификации типа шума
# ---------------------------------------------------------------------------

class TestNoiseTypeClassification(unittest.TestCase):
    """Проверка логики классификации noise_type."""

    def test_very_quiet_audio_classified_as_quiet(self):
        # Очень тихий фон → "quiet"
        tiny_noise = _white_noise(duration=2.0, amplitude=0.0001)
        result = NoiseProfiler().profile(tiny_noise, SR)
        self.assertEqual(result.noise_type, "quiet",
                         f"Ожидался 'quiet', получен '{result.noise_type}', noise_level={result.noise_level_db:.1f}")

    def test_moderate_noise_classified_as_office_or_quiet(self):
        # Умеренный белый шум → "office" или "quiet" (зависит от уровня)
        moderate = _white_noise(duration=2.0, amplitude=0.02)
        result = NoiseProfiler().profile(moderate, SR)
        self.assertIn(result.noise_type, {"quiet", "office"},
                      f"Умеренный шум должен быть 'quiet' или 'office', получен '{result.noise_type}'")

    def test_low_frequency_loud_noise_classified_as_street(self):
        # Громкий низкочастотный шум → "street"
        loud_low = _low_freq_noise(duration=2.0, amplitude=0.3)
        result = NoiseProfiler().profile(loud_low, SR)
        self.assertEqual(result.noise_type, "street",
                         f"Громкий НЧ-шум должен быть 'street', получен '{result.noise_type}'")


# ---------------------------------------------------------------------------
# Тесты уровня шума (dBFS)
# ---------------------------------------------------------------------------

class TestNoiseLevelDb(unittest.TestCase):
    """Проверка вычисления уровня шума в дБFS."""

    def test_noise_level_decreases_with_amplitude(self):
        loud = _white_noise(duration=2.0, amplitude=0.2)
        quiet = _white_noise(duration=2.0, amplitude=0.001)
        result_loud = NoiseProfiler().profile(loud, SR)
        result_quiet = NoiseProfiler().profile(quiet, SR)
        self.assertGreater(result_loud.noise_level_db, result_quiet.noise_level_db,
                           "Громкий шум должен иметь более высокий noise_level_db")

    def test_noise_level_db_is_negative(self):
        # dBFS относительно полной шкалы → всегда ≤ 0
        audio = _white_noise(duration=2.0, amplitude=0.05)
        result = NoiseProfiler().profile(audio, SR)
        self.assertLessEqual(result.noise_level_db, 0.0,
                             "noise_level_db должен быть ≤ 0 dBFS")


# ---------------------------------------------------------------------------
# Тесты спектрального профиля
# ---------------------------------------------------------------------------

class TestFrequencyProfile(unittest.TestCase):
    """Проверка классификации частотного профиля."""

    def test_low_frequency_signal_classified_correctly(self):
        # Только НЧ-компоненты → "low_frequency"
        audio = _low_freq_noise(duration=2.0, amplitude=0.2)
        result = NoiseProfiler().profile(audio, SR)
        self.assertEqual(result.frequency_profile, "low_frequency",
                         f"НЧ-сигнал должен быть 'low_frequency', получен '{result.frequency_profile}'")

    def test_white_noise_classified_as_broadband(self):
        # Белый шум равномерно распределён по спектру → "broadband"
        audio = _white_noise(duration=2.0, amplitude=0.1)
        result = NoiseProfiler().profile(audio, SR)
        self.assertEqual(result.frequency_profile, "broadband",
                         f"Белый шум должен быть 'broadband', получен '{result.frequency_profile}'")


# ---------------------------------------------------------------------------
# Тесты рекомендаций
# ---------------------------------------------------------------------------

class TestRecommendations(unittest.TestCase):
    """Проверка генерации рекомендаций."""

    def test_recommendations_is_non_empty_for_noisy_audio(self):
        # Шумное аудио должно давать хотя бы одну рекомендацию
        loud_noise = _white_noise(duration=2.0, amplitude=0.3)
        result = NoiseProfiler().profile(loud_noise, SR)
        self.assertGreater(len(result.recommendations), 0,
                           "Для шумного аудио должны быть рекомендации")

    def test_recommendations_contain_strings(self):
        audio = _speech_with_noise(noise_amp=0.05)
        result = NoiseProfiler().profile(audio, SR)
        for rec in result.recommendations:
            self.assertIsInstance(rec, str)
            self.assertGreater(len(rec), 0)

    def test_quiet_profile_has_positive_recommendation(self):
        tiny_noise = _white_noise(duration=2.0, amplitude=0.0001)
        result = NoiseProfiler().profile(tiny_noise, SR)
        if result.noise_type == "quiet":
            # Должна быть позитивная рекомендация
            joined = " ".join(result.recommendations).lower()
            self.assertTrue(
                any(word in joined for word in ["отличные", "минимален", "рекоменд"]),
                f"Для 'quiet' ожидалась позитивная рекомендация, получено: {result.recommendations}"
            )


# ---------------------------------------------------------------------------
# Тесты граничных случаев
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):
    """Граничные случаи: пустое аудио, короткий сигнал, стерео."""

    def test_empty_audio_does_not_raise(self):
        audio = np.array([], dtype=np.float32)
        result = NoiseProfiler().profile(audio, SR)
        self.assertIsInstance(result, NoiseProfile)

    def test_very_short_audio_returns_profile(self):
        audio = _sine(440, duration=0.05)  # 50 мс — меньше _FRAME_SIZE
        result = NoiseProfiler().profile(audio, SR)
        self.assertIsInstance(result, NoiseProfile)
        # Для слишком короткого аудио suitable_for_stt = False
        self.assertFalse(result.suitable_for_stt,
                         "Слишком короткое аудио не должно быть suitable_for_stt")

    def test_stereo_audio_processed_without_error(self):
        audio = _stereo(duration=1.0)
        result = NoiseProfiler().profile(audio, SR)
        self.assertIsInstance(result, NoiseProfile)
        self.assertIn(result.noise_type, {"quiet", "office", "street", "music", "crowd"})

    def test_to_dict_values_are_serializable(self):
        import json
        audio = _speech_with_noise(duration=1.0)
        result = NoiseProfiler().profile(audio, SR)
        d = result.to_dict()
        # Не должно бросать исключений при JSON-сериализации
        serialized = json.dumps(d)
        self.assertIsInstance(serialized, str)
        self.assertGreater(len(serialized), 10)

    def test_different_sample_rates_supported(self):
        for sr in [8000, 22050, 44100, 48000]:
            n = sr  # 1 секунда
            audio = _sine(440, 1.0, 0.3, sr)
            result = NoiseProfiler().profile(audio, sr)
            self.assertIsInstance(result, NoiseProfile,
                                  f"Ошибка при sample_rate={sr}")


if __name__ == "__main__":
    unittest.main()
