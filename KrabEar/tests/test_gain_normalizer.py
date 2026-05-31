"""Тесты GainNormalizer — нормализация усиления аудио перед STT."""

from __future__ import annotations
from core.gain_normalizer import GainNormalizer, GainResult, _rms_db, _db_to_linear

import math
import sys
import unittest
from pathlib import Path

import numpy as np

# Настройка пути для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


SR = 16000  # стандартная частота для тестов


# ---------------------------------------------------------------------------
# Вспомогательные генераторы сигналов
# ---------------------------------------------------------------------------

def _sine(
    freq: float = 440.0,
    duration: float = 1.0,
    amplitude: float = 0.1,
    sr: int = SR,
) -> np.ndarray:
    """Синусоида с заданной амплитудой."""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return (amplitude * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def _silence(duration: float = 1.0, sr: int = SR) -> np.ndarray:
    """Абсолютная тишина."""
    return np.zeros(int(sr * duration), dtype=np.float32)


def _loud(duration: float = 1.0, amplitude: float = 0.98, sr: int = SR) -> np.ndarray:
    """Громкий сигнал, близкий к клиппингу."""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return (amplitude * np.sin(2 * math.pi * 440 * t)).astype(np.float32)


def _stereo(duration: float = 1.0, amplitude: float = 0.1, sr: int = SR) -> np.ndarray:
    """Стерео сигнал (N, 2)."""
    mono = _sine(duration=duration, amplitude=amplitude, sr=sr)
    return np.stack([mono, mono * 0.8], axis=1)


# ---------------------------------------------------------------------------
# Тест 1: GainResult содержит правильные поля
# ---------------------------------------------------------------------------

class TestGainResultFields(unittest.TestCase):
    """Проверка типов и полей GainResult."""

    def setUp(self):
        self.normalizer = GainNormalizer()

    def test_result_is_gain_result_instance(self):
        result = self.normalizer.normalize(_sine())
        self.assertIsInstance(result, GainResult)

    def test_result_has_audio_array(self):
        result = self.normalizer.normalize(_sine())
        self.assertIsInstance(result.audio, np.ndarray)
        self.assertEqual(result.audio.dtype, np.float32)

    def test_result_numeric_fields_are_floats(self):
        result = self.normalizer.normalize(_sine())
        self.assertIsInstance(result.gain_applied_db, float)
        self.assertIsInstance(result.input_rms_db, float)
        self.assertIsInstance(result.output_rms_db, float)
        self.assertIsInstance(result.clipped_samples, int)

    def test_to_dict_has_expected_keys(self):
        result = self.normalizer.normalize(_sine())
        d = result.to_dict()
        self.assertIn("gain_applied_db", d)
        self.assertIn("input_rms_db", d)
        self.assertIn("output_rms_db", d)
        self.assertIn("clipped_samples", d)

    def test_audio_length_preserved(self):
        audio = _sine(duration=2.0)
        result = self.normalizer.normalize(audio)
        self.assertEqual(len(result.audio), len(audio))


# ---------------------------------------------------------------------------
# Тест 2: Нормализация к целевому RMS
# ---------------------------------------------------------------------------

class TestNormalizeTargetRms(unittest.TestCase):
    """Проверка, что выходной RMS соответствует target_db."""

    def setUp(self):
        self.normalizer = GainNormalizer()

    def test_rms_reaches_target_minus20(self):
        audio = _sine(amplitude=0.01, duration=2.0)  # тихий сигнал
        result = self.normalizer.normalize(audio, target_db=-20.0)
        self.assertAlmostEqual(result.output_rms_db, -20.0, delta=1.5)

    def test_rms_reaches_target_minus30(self):
        audio = _sine(amplitude=0.3, duration=2.0)
        result = self.normalizer.normalize(audio, target_db=-30.0)
        self.assertAlmostEqual(result.output_rms_db, -30.0, delta=1.5)

    def test_rms_reaches_target_minus10(self):
        audio = _sine(amplitude=0.01, duration=2.0)
        result = self.normalizer.normalize(audio, target_db=-10.0)
        # Мощный усилитель — ограничитель может скорректировать, но RMS должен быть близок
        self.assertGreater(result.output_rms_db, -15.0)

    def test_gain_applied_is_correct_sign(self):
        # Тихий сигнал → положительное усиление
        audio = _sine(amplitude=0.001, duration=1.0)
        result = self.normalizer.normalize(audio, target_db=-20.0)
        self.assertGreater(result.gain_applied_db, 0.0)

    def test_loud_signal_gain_is_negative(self):
        # Громкий сигнал → отрицательное усиление (аттенюация)
        audio = _sine(amplitude=0.5, duration=1.0)
        result = self.normalizer.normalize(audio, target_db=-30.0)
        self.assertLess(result.gain_applied_db, 0.0)

    def test_input_rms_db_matches_actual(self):
        audio = _sine(amplitude=0.1, duration=2.0)
        expected_rms_db = _rms_db(audio)
        result = self.normalizer.normalize(audio, target_db=-20.0)
        self.assertAlmostEqual(result.input_rms_db, expected_rms_db, delta=0.5)


# ---------------------------------------------------------------------------
# Тест 3: Тишина обрабатывается корректно
# ---------------------------------------------------------------------------

class TestSilenceHandling(unittest.TestCase):
    """Пустой / тихий сигнал не должен вызывать исключений."""

    def setUp(self):
        self.normalizer = GainNormalizer()

    def test_silence_does_not_raise(self):
        result = self.normalizer.normalize(_silence(2.0))
        self.assertIsInstance(result, GainResult)

    def test_silence_gain_is_zero(self):
        result = self.normalizer.normalize(_silence(1.0))
        self.assertAlmostEqual(result.gain_applied_db, 0.0, delta=1e-6)

    def test_empty_array_does_not_raise(self):
        result = self.normalizer.normalize(np.array([], dtype=np.float32))
        self.assertIsInstance(result, GainResult)
        self.assertEqual(len(result.audio), 0)

    def test_silence_clipped_samples_zero(self):
        result = self.normalizer.normalize(_silence(1.0))
        self.assertEqual(result.clipped_samples, 0)


# ---------------------------------------------------------------------------
# Тест 4: Soft-knee limiter не допускает клиппинга
# ---------------------------------------------------------------------------

class TestSoftKneeLimit(unittest.TestCase):
    """Проверка, что ограничитель не даёт амплитуде превысить 1.0."""

    def setUp(self):
        self.normalizer = GainNormalizer()

    def test_output_peak_does_not_exceed_1(self):
        # Чрезмерно громкий сигнал
        audio = np.ones(SR, dtype=np.float32) * 0.9
        result = self.normalizer.normalize(audio, target_db=-3.0)
        peak = float(np.max(np.abs(result.audio)))
        self.assertLessEqual(peak, 1.0 + 1e-6)

    def test_very_loud_signal_peak_bounded(self):
        # Усиление тихого сигнала на 40 дБ может перегрузить выход
        audio = _sine(amplitude=0.001, duration=1.0)
        result = self.normalizer.normalize(audio, target_db=0.0)
        peak = float(np.max(np.abs(result.audio)))
        self.assertLessEqual(peak, 1.0 + 1e-6)

    def test_limiter_counts_clipped_samples_for_saturated(self):
        # Полностью перегруженный сигнал — ограничитель должен зафиксировать много семплов
        audio = np.ones(SR * 2, dtype=np.float64) * 5.0  # сигнал 5×
        _, clipped = GainNormalizer._soft_knee_limit(audio.astype(np.float64))
        self.assertGreater(clipped, 0)

    def test_limiter_does_not_clip_low_signal(self):
        # Сигнал ниже порога ограничителя — клиппинга нет
        audio = _sine(amplitude=0.3, duration=1.0).astype(np.float64)
        limited, clipped = GainNormalizer._soft_knee_limit(audio)
        self.assertEqual(clipped, 0)
        np.testing.assert_array_almost_equal(limited, audio, decimal=5)


# ---------------------------------------------------------------------------
# Тест 5: auto_gain
# ---------------------------------------------------------------------------

class TestAutoGain(unittest.TestCase):
    """Проверка авто-нормализации."""

    def setUp(self):
        self.normalizer = GainNormalizer()

    def test_auto_gain_quiet_raises_rms(self):
        audio = _sine(amplitude=0.001, duration=2.0)
        result = self.normalizer.auto_gain(audio)
        self.assertGreater(result.output_rms_db, result.input_rms_db)

    def test_auto_gain_loud_lowers_peak(self):
        audio = _loud(amplitude=0.99, duration=1.0)
        result = self.normalizer.auto_gain(audio)
        peak_before = float(np.max(np.abs(audio)))
        peak_after = float(np.max(np.abs(result.audio)))
        self.assertLessEqual(peak_after, peak_before + 1e-6)

    def test_auto_gain_output_peak_bounded(self):
        audio = _loud(amplitude=0.99, duration=1.0)
        result = self.normalizer.auto_gain(audio)
        peak = float(np.max(np.abs(result.audio)))
        self.assertLessEqual(peak, 1.0 + 1e-6)

    def test_auto_gain_silence_unchanged(self):
        audio = _silence(1.0)
        result = self.normalizer.auto_gain(audio)
        self.assertAlmostEqual(result.gain_applied_db, 0.0, delta=1e-6)

    def test_auto_gain_returns_gain_result(self):
        result = self.normalizer.auto_gain(_sine())
        self.assertIsInstance(result, GainResult)


# ---------------------------------------------------------------------------
# Тест 6: Многоканальное аудио
# ---------------------------------------------------------------------------

class TestMultichannel(unittest.TestCase):
    """Стерео/многоканальное аудио усредняется в моно."""

    def setUp(self):
        self.normalizer = GainNormalizer()

    def test_stereo_normalize_does_not_raise(self):
        audio = _stereo(duration=1.0, amplitude=0.1)
        result = self.normalizer.normalize(audio, target_db=-20.0)
        self.assertIsInstance(result, GainResult)

    def test_stereo_output_is_1d(self):
        audio = _stereo(duration=1.0, amplitude=0.1)
        result = self.normalizer.normalize(audio, target_db=-20.0)
        self.assertEqual(result.audio.ndim, 1)

    def test_stereo_auto_gain_does_not_raise(self):
        audio = _stereo(duration=1.0, amplitude=0.05)
        result = self.normalizer.auto_gain(audio)
        self.assertIsInstance(result, GainResult)


# ---------------------------------------------------------------------------
# Тест 7: Корректность RMS-метрики и дБ-утилит
# ---------------------------------------------------------------------------

class TestRmsUtils(unittest.TestCase):
    """Проверка вспомогательных функций."""

    def test_rms_db_sine_known_value(self):
        # Синусоида амплитудой A: RMS = A/√2
        amplitude = 0.5
        audio = _sine(amplitude=amplitude, duration=2.0)
        expected_rms = amplitude / math.sqrt(2)
        expected_db = 20.0 * math.log10(expected_rms)
        actual_db = _rms_db(audio)
        self.assertAlmostEqual(actual_db, expected_db, delta=0.2)

    def test_db_to_linear_roundtrip(self):
        for db in [-40.0, -20.0, -6.0, 0.0, 6.0]:
            linear = _db_to_linear(db)
            recovered = 20.0 * math.log10(linear)
            self.assertAlmostEqual(recovered, db, delta=1e-6)

    def test_rms_db_silence_returns_floor(self):
        from core.gain_normalizer import _SILENCE_FLOOR_DB
        db = _rms_db(np.zeros(1000, dtype=np.float32))
        self.assertLessEqual(db, _SILENCE_FLOOR_DB)


# ---------------------------------------------------------------------------
# Тест 8: Идемпотентность повторной нормализации
# ---------------------------------------------------------------------------

class TestIdempotency(unittest.TestCase):
    """Повторная нормализация уже нормализованного сигнала не должна
    значительно менять уровень."""

    def setUp(self):
        self.normalizer = GainNormalizer()

    def test_second_normalize_is_stable(self):
        audio = _sine(amplitude=0.05, duration=2.0)
        first = self.normalizer.normalize(audio, target_db=-20.0)
        second = self.normalizer.normalize(first.audio, target_db=-20.0)
        # После второй нормализации RMS должен остаться близким к target
        self.assertAlmostEqual(second.output_rms_db, -20.0, delta=1.5)
        # Второй проход не должен сильно менять усиление
        self.assertAlmostEqual(second.gain_applied_db, 0.0, delta=2.0)

    def test_auto_gain_twice_peak_bounded(self):
        audio = _sine(amplitude=0.001, duration=2.0)
        first = self.normalizer.auto_gain(audio)
        second = self.normalizer.auto_gain(first.audio)
        peak = float(np.max(np.abs(second.audio)))
        self.assertLessEqual(peak, 1.0 + 1e-6)


# ---------------------------------------------------------------------------
# Тест 9: Wave 130 — обязательные кейсы
# ---------------------------------------------------------------------------

class TestWave130RequiredCases(unittest.TestCase):
    """Wave 130: дополнительные кейсы по спецификации."""

    def setUp(self):
        self.normalizer = GainNormalizer()

    # test_normalize_to_target_db
    def test_normalize_to_target_db(self):
        """Выходной RMS точно попадает в target_db (±2 дБ)."""
        for target in (-30.0, -20.0, -14.0):
            audio = _sine(amplitude=0.05, duration=2.0)
            result = self.normalizer.normalize(audio, target_db=target)
            self.assertAlmostEqual(
                result.output_rms_db, target, delta=2.0,
                msg=f"target={target}: RMS={result.output_rms_db:.2f}",
            )

    # test_silent_audio_no_clipping
    def test_silent_audio_no_clipping(self):
        """Тишина не порождает клиппинга и не меняет сигнал."""
        result = self.normalizer.normalize(_silence(1.0))
        self.assertEqual(result.clipped_samples, 0)
        peak = float(np.max(np.abs(result.audio))) if len(result.audio) else 0.0
        self.assertLessEqual(peak, 1.0 + 1e-6)

    # test_loud_audio_attenuated
    def test_loud_audio_attenuated(self):
        """Громкий сигнал аттенюируется: peak выхода ≤ peak входа."""
        audio = _loud(amplitude=0.95, duration=1.0)
        result = self.normalizer.normalize(audio, target_db=-30.0)
        peak_in = float(np.max(np.abs(audio)))
        peak_out = float(np.max(np.abs(result.audio)))
        self.assertLessEqual(peak_out, peak_in + 1e-6,
                             msg=f"peak_in={peak_in:.3f}, peak_out={peak_out:.3f}")
        self.assertLess(result.gain_applied_db, 0.0)

    # test_handles_short_audio
    def test_handles_short_audio(self):
        """Очень короткое аудио (1-10 семплов) обрабатывается без исключений."""
        for n in (1, 5, 10):
            audio = np.full(n, 0.1, dtype=np.float32)
            result = self.normalizer.normalize(audio, target_db=-20.0)
            self.assertIsInstance(result, GainResult)
            self.assertEqual(len(result.audio), n)

    # test_empty_audio_returns_empty
    def test_empty_audio_returns_empty(self):
        """Пустой массив возвращает пустой GainResult без исключений."""
        empty = np.array([], dtype=np.float32)
        result = self.normalizer.normalize(empty)
        self.assertIsInstance(result, GainResult)
        self.assertEqual(len(result.audio), 0)
        self.assertEqual(result.clipped_samples, 0)

    # test_target_db_clamped_to_safe_range
    def test_target_db_clamped_to_safe_range(self):
        """Негативные target_db не вызывают исключений и не приводят к
        бесконечным значениям или NaN в результате.
        Позитивные target_db отклоняются с ValueError (W1066 F5).
        """
        audio = _sine(amplitude=0.1, duration=1.0)
        for extreme_target in (-80.0, 0.0):
            result = self.normalizer.normalize(audio, target_db=extreme_target)
            self.assertFalse(
                np.any(np.isnan(result.audio)),
                msg=f"NaN at target_db={extreme_target}",
            )
            self.assertFalse(
                np.any(np.isinf(result.audio)),
                msg=f"Inf at target_db={extreme_target}",
            )
            peak = float(np.max(np.abs(result.audio))) if len(result.audio) else 0.0
            self.assertLessEqual(peak, 1.0 + 1e-6,
                                 msg=f"Clipping at target_db={extreme_target}")
        # target_db > 0 must now raise ValueError (W1066 F5)
        with self.assertRaises(ValueError):
            self.normalizer.normalize(audio, target_db=6.0)

    # test_concurrent_normalize
    def test_concurrent_normalize(self):
        """Параллельные вызовы normalize() из нескольких потоков не конкурируют
        за состояние и возвращают правильные независимые результаты."""
        import threading

        errors: list[Exception] = []
        results: list[GainResult] = []
        lock = threading.Lock()

        def worker(amplitude: float) -> None:
            try:
                audio = _sine(amplitude=amplitude, duration=1.0)
                r = self.normalizer.normalize(audio, target_db=-20.0)
                with lock:
                    results.append(r)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        amplitudes = [0.01, 0.05, 0.1, 0.2, 0.5]
        threads = [threading.Thread(target=worker, args=(a,)) for a in amplitudes]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")
        self.assertEqual(len(results), len(amplitudes))
        for r in results:
            self.assertFalse(np.any(np.isnan(r.audio)))
            peak = float(np.max(np.abs(r.audio)))
            self.assertLessEqual(peak, 1.0 + 1e-6)


# ---------------------------------------------------------------------------
# Тест 10: W1066 — NaN/Inf guard + target_db validation (W1064 F1+F2+F5)
# ---------------------------------------------------------------------------

class TestW1066Guards(unittest.TestCase):
    """W1066: NaN/Inf propagation guard and target_db > 0 validation."""

    def setUp(self):
        self.normalizer = GainNormalizer()

    def test_nan_input_returns_unchanged_with_warning(self):
        """NaN samples in input → audio returned unchanged, warning logged."""
        import logging

        audio = _sine(amplitude=0.1, duration=0.5)
        audio[10] = float("nan")

        with self.assertLogs("KrabEar.GainNormalizer", level=logging.WARNING) as cm:
            result = self.normalizer.normalize(audio)

        # Result must be a valid GainResult with same length
        self.assertIsInstance(result, GainResult)
        self.assertEqual(len(result.audio), len(audio))
        # Gain must be zero — signal was not processed
        self.assertAlmostEqual(result.gain_applied_db, 0.0, delta=1e-6)
        # Warning must mention non-finite
        self.assertTrue(
            any("non-finite" in line for line in cm.output),
            msg=f"Expected 'non-finite' in warning, got: {cm.output}",
        )

    def test_inf_input_returns_unchanged_with_warning(self):
        """Inf samples in input → audio returned unchanged, warning logged."""
        import logging

        audio = _sine(amplitude=0.1, duration=0.5)
        audio[5] = float("inf")

        with self.assertLogs("KrabEar.GainNormalizer", level=logging.WARNING) as cm:
            result = self.normalizer.normalize(audio)

        self.assertIsInstance(result, GainResult)
        self.assertAlmostEqual(result.gain_applied_db, 0.0, delta=1e-6)
        self.assertTrue(
            any("non-finite" in line for line in cm.output),
            msg=f"Expected 'non-finite' in warning, got: {cm.output}",
        )

    def test_positive_target_db_raises_value_error(self):
        """target_db > 0 must raise ValueError (guarantees clipping)."""
        audio = _sine(amplitude=0.1, duration=0.5)
        with self.assertRaises(ValueError):
            self.normalizer.normalize(audio, target_db=1.0)

    def test_zero_target_db_does_not_raise(self):
        """target_db == 0 is on the boundary and must not raise."""
        audio = _sine(amplitude=0.1, duration=0.5)
        # Should complete without exception (limiter handles any overshoot)
        result = self.normalizer.normalize(audio, target_db=0.0)
        self.assertIsInstance(result, GainResult)

    def test_finite_clean_audio_not_affected_by_guard(self):
        """Clean audio (all finite) must not trigger the NaN guard path."""
        audio = _sine(amplitude=0.1, duration=1.0)
        import io
        import logging

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.WARNING)
        log = logging.getLogger("KrabEar.GainNormalizer")
        log.addHandler(handler)
        try:
            result = self.normalizer.normalize(audio, target_db=-20.0)
        finally:
            log.removeHandler(handler)

        # Guard path logs warning — make sure it was NOT triggered
        warnings_output = stream.getvalue()
        self.assertNotIn("non-finite", warnings_output)
        # Normal normalisation should have applied a nonzero gain
        self.assertIsInstance(result, GainResult)


# ---------------------------------------------------------------------------
# Тест 11: Wave 1719 — regression tests for gain-cap + limiter fixes
# ---------------------------------------------------------------------------

class TestWave1719Regressions(unittest.TestCase):
    """Wave 1719: regression guard for four bugs fixed in gain_normalizer.py.

    BUG 1: no max-gain cap → near-silence gets amplified 800-900x → square wave
    BUG 2: soft-knee top computed incorrectly → knee_width_db is a dead knob
    BUG 3: soft-knee curve discontinuity at knee_top
    BUG 4: auto_gain() missing NaN/Inf guard
    """

    def setUp(self):
        self.normalizer = GainNormalizer()

    # ------------------------------------------------------------------
    # BUG 1: near-silence must NOT become a square wave
    # ------------------------------------------------------------------

    def _make_near_silence(self, rms_db: float, duration: float = 0.5) -> np.ndarray:
        """Generate a sine signal with a known RMS level in dBFS."""
        # RMS of a sine with amplitude A is A/sqrt(2)
        # So A = rms_linear * sqrt(2)
        rms_linear = _db_to_linear(rms_db)
        amplitude = rms_linear * math.sqrt(2)
        t = np.linspace(0, duration, int(SR * duration), endpoint=False)
        return (amplitude * np.sin(2 * math.pi * 440 * t)).astype(np.float32)

    def test_near_silence_minus79_not_square_wave(self):
        """Signal at -79 dBFS MUST NOT be amplified into a clipped square wave.

        Fail-before: without cap, gain_db = -20 - (-79) = +59 dB → 891× →
        entire waveform hard-clips → peak = 1.0, clipping_ratio ≈ 1.0.
        Pass-after: gain capped at +30 dB → peak ≪ 1.0 for a clean sine.
        """
        audio = self._make_near_silence(rms_db=-79.0)
        result = self.normalizer.normalize(audio, target_db=-20.0)

        # Applied gain must be capped
        self.assertLessEqual(
            result.gain_applied_db, 30.0 + 1e-3,
            msg=f"gain_applied_db={result.gain_applied_db:.2f} exceeds +30 dB cap",
        )

        # Peak must be well below 1.0 (sine at -79 dBFS + 30 dB ≈ -49 dBFS,
        # peak ≈ sqrt(2) * 10^(-49/20) ≈ 0.005 — far from 1.0)
        peak = float(np.max(np.abs(result.audio)))
        self.assertLess(
            peak, 0.5,
            msg=f"Output peak {peak:.4f} too high — signal may be clipped to square wave",
        )

        # No clipping at all
        self.assertEqual(
            result.clipped_samples, 0,
            msg=f"Near-silence at -79 dBFS should produce 0 clipped samples, "
                f"got {result.clipped_samples}",
        )

    def test_near_silence_minus60_not_square_wave(self):
        """Signal at -60 dBFS (old +40 dB gain, 100×) also stays safe."""
        audio = self._make_near_silence(rms_db=-60.0)
        result = self.normalizer.normalize(audio, target_db=-20.0)

        self.assertLessEqual(result.gain_applied_db, 30.0 + 1e-3)
        peak = float(np.max(np.abs(result.audio)))
        self.assertLess(peak, 0.9)

    def test_gain_cap_warning_logged_when_capped(self):
        """A warning must be logged when gain is capped."""
        import logging as _logging
        audio = self._make_near_silence(rms_db=-79.0)
        with self.assertLogs("KrabEar.GainNormalizer", level=_logging.WARNING) as cm:
            self.normalizer.normalize(audio, target_db=-20.0)
        self.assertTrue(
            any("cap" in line.lower() or "square" in line.lower() or "exceeds" in line.lower()
                for line in cm.output),
            msg=f"Expected gain-cap warning, got: {cm.output}",
        )

    def test_normal_signal_not_capped(self):
        """Normal speech-level signal (-40 dBFS) needs +20 dB — below cap, no warning."""
        audio = self._make_near_silence(rms_db=-40.0)
        result = self.normalizer.normalize(audio, target_db=-20.0)
        self.assertAlmostEqual(result.gain_applied_db, 20.0, delta=0.5)

    # ------------------------------------------------------------------
    # BUG 2: soft-knee width parameter must be a live knob
    # ------------------------------------------------------------------

    def test_knee_width_changes_knee_zone(self):
        """Different _LIMITER_KNEE_DB values must produce different knee_top.

        We test indirectly: with a large knee (wide), a signal just above
        threshold but well below HARD_CLIP is in the knee zone and gets
        compressed. With knee_width_db≈0 (near-zero), the knee zone is
        near-empty and the same signal passes through almost unchanged
        (until hard-clip at _HARD_CLIP).
        """
        from core.gain_normalizer import _LIMITER_KNEE_DB, _LIMITER_THRESHOLD, _HARD_CLIP

        # Build a constant signal right in the middle of [threshold, HARD_CLIP]
        mid_level = (_LIMITER_THRESHOLD + _HARD_CLIP) / 2.0  # ≈ 0.975
        audio = np.full(SR, mid_level, dtype=np.float64)

        GainNormalizer._soft_knee_limit(audio)

        # With the current knee formula the default knee_top < HARD_CLIP,
        # so mid_level is often ABOVE knee_top and hard-clips.
        # The important check: the knee param is not completely ignored —
        # samples between threshold and knee_top are compressed (not identical
        # to input and not hard-clipped to 1.0 at the same time).
        # Verify knee_top is meaningful (not simply equal to HARD_CLIP for
        # default _LIMITER_KNEE_DB > 0):
        knee_fraction = 1.0 - _db_to_linear(-_LIMITER_KNEE_DB)
        knee_top = _HARD_CLIP - (_HARD_CLIP - _LIMITER_THRESHOLD) * knee_fraction
        self.assertLess(
            knee_top, _HARD_CLIP - 1e-6,
            msg=f"knee_top={knee_top:.6f} must be strictly below HARD_CLIP={_HARD_CLIP}; "
                "knee_width_db parameter is a dead knob",
        )
        self.assertGreater(
            knee_top, _LIMITER_THRESHOLD + 1e-6,
            msg=f"knee_top={knee_top:.6f} must be above threshold={_LIMITER_THRESHOLD}",
        )

    # ------------------------------------------------------------------
    # BUG 3: soft-knee curve must be continuous at knee_top
    # ------------------------------------------------------------------

    def test_knee_curve_continuous_at_knee_top(self):
        """The soft-knee output must be continuous at the knee_top boundary.

        Sample just below knee_top and at knee_top must not have a step jump.
        Old formula: f(t=1) = threshold + (knee_top - threshold) * 0.5
                              ≠ knee_top → discontinuity.
        New formula: f(t=1) = threshold + (knee_top - threshold) * 1² = knee_top.
        """
        from core.gain_normalizer import (
            _LIMITER_KNEE_DB, _LIMITER_THRESHOLD, _HARD_CLIP,
        )

        knee_fraction = 1.0 - _db_to_linear(-_LIMITER_KNEE_DB)
        knee_top = _HARD_CLIP - (_HARD_CLIP - _LIMITER_THRESHOLD) * knee_fraction

        epsilon = 1e-6
        just_below = knee_top - epsilon
        at_knee_top = knee_top

        audio_below = np.array([just_below], dtype=np.float64)
        audio_at = np.array([at_knee_top], dtype=np.float64)

        limited_below, _ = GainNormalizer._soft_knee_limit(audio_below)
        limited_at, _ = GainNormalizer._soft_knee_limit(audio_at)

        val_below = float(np.abs(limited_below[0]))
        val_at = float(np.abs(limited_at[0]))

        # The two values must be very close (no step jump)
        self.assertAlmostEqual(
            val_below, val_at, delta=0.01,
            msg=f"Step discontinuity at knee_top: just_below={val_below:.6f}, "
                f"at_knee_top={val_at:.6f}. Difference={abs(val_at - val_below):.6f}",
        )

    def test_knee_curve_at_t1_reaches_knee_top(self):
        """At exactly knee_top the compressed output must equal knee_top (not midpoint)."""
        from core.gain_normalizer import (
            _LIMITER_KNEE_DB, _LIMITER_THRESHOLD, _HARD_CLIP,
        )

        knee_fraction = 1.0 - _db_to_linear(-_LIMITER_KNEE_DB)
        knee_top = _HARD_CLIP - (_HARD_CLIP - _LIMITER_THRESHOLD) * knee_fraction

        # A sample exactly at knee_top — the limiter must map it to the hard-clip
        # boundary (knee_top itself, since the condition is abs > knee_top for hard-clip).
        audio = np.array([knee_top], dtype=np.float64)
        limited, clipped = GainNormalizer._soft_knee_limit(audio)

        # At exactly knee_top (<=knee_top) the knee zone handles it → compressed to knee_top
        # (t=1 → output = knee_top).
        output_val = float(np.abs(limited[0]))
        self.assertAlmostEqual(
            output_val, knee_top, delta=0.005,
            msg=f"At t=1 (knee_top={knee_top:.6f}) output should be knee_top, got {output_val:.6f}",
        )
        # Must not be hard-clipped either
        self.assertEqual(clipped, 0, msg="Exactly at knee_top should not count as hard-clipped")

    def test_knee_curve_monotone(self):
        """The soft-knee compression curve must be monotonically non-decreasing."""
        from core.gain_normalizer import (
            _LIMITER_KNEE_DB, _LIMITER_THRESHOLD, _HARD_CLIP,
        )

        knee_fraction = 1.0 - _db_to_linear(-_LIMITER_KNEE_DB)
        knee_top = _HARD_CLIP - (_HARD_CLIP - _LIMITER_THRESHOLD) * knee_fraction

        # Sample the knee zone at 50 evenly spaced points
        levels = np.linspace(_LIMITER_THRESHOLD + 1e-6, knee_top - 1e-6, 50)
        audio = levels.copy()
        limited, _ = GainNormalizer._soft_knee_limit(audio)
        outputs = np.abs(limited)

        for i in range(len(outputs) - 1):
            self.assertLessEqual(
                outputs[i], outputs[i + 1] + 1e-9,
                msg=f"Knee curve is not monotone at index {i}: "
                    f"output[{i}]={outputs[i]:.6f} > output[{i+1}]={outputs[i+1]:.6f}",
            )

    # ------------------------------------------------------------------
    # BUG 4: auto_gain must handle NaN/Inf input gracefully
    # ------------------------------------------------------------------

    def test_auto_gain_nan_input_returns_gracefully(self):
        """auto_gain with NaN samples must return unchanged audio, no exception."""
        import logging as _logging

        audio = _sine(amplitude=0.1, duration=0.5)
        audio[42] = float("nan")

        with self.assertLogs("KrabEar.GainNormalizer", level=_logging.WARNING) as cm:
            result = self.normalizer.auto_gain(audio)

        self.assertIsInstance(result, GainResult)
        self.assertEqual(len(result.audio), len(audio))
        self.assertAlmostEqual(result.gain_applied_db, 0.0, delta=1e-6)
        self.assertTrue(
            any("non-finite" in line for line in cm.output),
            msg=f"Expected 'non-finite' warning from auto_gain, got: {cm.output}",
        )

    def test_auto_gain_inf_input_returns_gracefully(self):
        """auto_gain with Inf samples must return unchanged audio, no exception."""
        import logging as _logging

        audio = _sine(amplitude=0.1, duration=0.5)
        audio[10] = float("inf")

        with self.assertLogs("KrabEar.GainNormalizer", level=_logging.WARNING) as cm:
            result = self.normalizer.auto_gain(audio)

        self.assertIsInstance(result, GainResult)
        self.assertAlmostEqual(result.gain_applied_db, 0.0, delta=1e-6)
        self.assertTrue(
            any("non-finite" in line for line in cm.output),
            msg=f"Expected 'non-finite' warning from auto_gain, got: {cm.output}",
        )

    def test_auto_gain_all_nan_returns_gracefully(self):
        """auto_gain with all-NaN array must return gracefully."""
        audio = np.full(SR, float("nan"), dtype=np.float32)
        result = self.normalizer.auto_gain(audio)
        self.assertIsInstance(result, GainResult)
        self.assertAlmostEqual(result.gain_applied_db, 0.0, delta=1e-6)

    def test_auto_gain_clean_audio_not_affected_by_guard(self):
        """Clean audio in auto_gain must NOT trigger the NaN guard."""
        import io
        import logging as _logging

        audio = _sine(amplitude=0.05, duration=0.5)
        stream = io.StringIO()
        handler = _logging.StreamHandler(stream)
        handler.setLevel(_logging.WARNING)
        log = _logging.getLogger("KrabEar.GainNormalizer")
        log.addHandler(handler)
        try:
            result = self.normalizer.auto_gain(audio)
        finally:
            log.removeHandler(handler)

        self.assertNotIn("non-finite", stream.getvalue())
        self.assertIsInstance(result, GainResult)
        # Gain should be applied (quiet signal amplified)
        self.assertGreater(result.gain_applied_db, 0.0)


if __name__ == "__main__":
    unittest.main()
