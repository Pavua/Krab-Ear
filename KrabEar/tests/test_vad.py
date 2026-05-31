"""Тесты для VoiceActivityDetector (core/vad.py)."""

from __future__ import annotations
from core.vad import VoiceActivityDetector, VADResult, SpeechSegment, _rms_to_db

import sys
import math
import unittest
from pathlib import Path

import numpy as np

# Настройка sys.path для корректного импорта модулей KrabEar
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


SR = 16000  # частота дискретизации для тестов


def _make_tone(freq_hz: float, duration_sec: float, amplitude: float = 0.5) -> np.ndarray:
    """Генерирует синусоиду заданной частоты и амплитуды."""
    t = np.linspace(0, duration_sec, int(SR * duration_sec), endpoint=False)
    return (amplitude * np.sin(2 * math.pi * freq_hz * t)).astype(np.float32)


def _make_silence(duration_sec: float, noise_level: float = 1e-4) -> np.ndarray:
    """Генерирует фрагмент тишины с небольшим фоновым шумом."""
    n = int(SR * duration_sec)
    rng = np.random.default_rng(42)
    return (rng.uniform(-noise_level, noise_level, n)).astype(np.float32)


def _concat(*parts: np.ndarray) -> np.ndarray:
    return np.concatenate(parts)


class TestVADBasicTypes(unittest.TestCase):
    """Проверка типов и структур данных VADResult / SpeechSegment."""

    def test_vad_result_defaults(self):
        result = VADResult()
        self.assertEqual(result.speech_segments, [])
        self.assertAlmostEqual(result.speech_ratio, 0.0)
        self.assertAlmostEqual(result.total_speech_sec, 0.0)
        self.assertAlmostEqual(result.total_silence_sec, 0.0)

    def test_vad_result_to_dict_keys(self):
        result = VADResult()
        d = result.to_dict()
        for key in ("speech_segments", "speech_segment_count", "speech_ratio",
                    "total_speech_sec", "total_silence_sec"):
            self.assertIn(key, d)

    def test_speech_segment_to_dict(self):
        seg = SpeechSegment(start_sec=0.5, end_sec=1.5, duration_sec=1.0, energy_db=-20.0)
        d = seg.to_dict()
        self.assertAlmostEqual(d["start_sec"], 0.5)
        self.assertAlmostEqual(d["end_sec"], 1.5)
        self.assertAlmostEqual(d["duration_sec"], 1.0)
        self.assertAlmostEqual(d["energy_db"], -20.0)

    def test_rms_to_db_floor(self):
        """_rms_to_db не должен падать при нулевом RMS."""
        db = _rms_to_db(0.0)
        self.assertLess(db, -100.0)

    def test_rms_to_db_typical(self):
        # RMS=1 → 0 дБ
        self.assertAlmostEqual(_rms_to_db(1.0), 0.0, places=3)
        # RMS=0.1 → -20 дБ
        self.assertAlmostEqual(_rms_to_db(0.1), -20.0, places=3)


class TestVADEdgeCases(unittest.TestCase):
    """Граничные и вырожденные случаи."""

    def setUp(self):
        self.vad = VoiceActivityDetector()

    def test_empty_audio_returns_empty_result(self):
        result = self.vad.detect(np.array([], dtype=np.float32), SR)
        self.assertEqual(result.speech_segments, [])
        self.assertAlmostEqual(result.speech_ratio, 0.0)

    def test_invalid_sample_rate_returns_empty(self):
        audio = _make_tone(440, 1.0)
        result = self.vad.detect(audio, sample_rate=0)
        self.assertEqual(result.speech_segments, [])

    def test_pure_silence_no_segments(self):
        silence = _make_silence(2.0)
        result = self.vad.detect(silence, SR)
        self.assertEqual(len(result.speech_segments), 0)
        self.assertAlmostEqual(result.speech_ratio, 0.0, places=1)

    def test_stereo_audio_handled(self):
        """Стерео аудио должно усредняться в моно без ошибок."""
        tone_l = _make_tone(440, 1.0)
        tone_r = _make_tone(880, 1.0)
        stereo = np.column_stack([tone_l, tone_r])
        result = self.vad.detect(stereo, SR)
        self.assertIsInstance(result, VADResult)

    def test_very_short_audio(self):
        """Аудио менее одного фрейма не должно падать."""
        tiny = np.array([0.1, -0.1, 0.05], dtype=np.float32)
        result = self.vad.detect(tiny, SR)
        self.assertIsInstance(result, VADResult)


class TestVADSpeechDetection(unittest.TestCase):
    """Тесты на корректное обнаружение речи."""

    def setUp(self):
        self.vad = VoiceActivityDetector(margin_db=8.0, onset_frames=2, offset_frames=3)

    def test_pure_speech_high_ratio(self):
        """Непрерывный тон → высокая доля речи."""
        speech = _make_tone(440, 3.0, amplitude=0.6)
        result = self.vad.detect(speech, SR)
        self.assertGreater(result.speech_ratio, 0.7)

    def test_speech_then_silence_one_segment(self):
        """Речь → тишина: должен быть ровно один сегмент."""
        speech = _make_tone(440, 1.0, amplitude=0.5)
        silence = _make_silence(2.0)
        audio = _concat(speech, silence)
        result = self.vad.detect(audio, SR)
        self.assertEqual(len(result.speech_segments), 1)

    def test_segment_timing_rough(self):
        """Сегмент речи должен начинаться примерно в начале тона."""
        silence_pre = _make_silence(0.5)
        speech = _make_tone(440, 1.0, amplitude=0.5)
        silence_post = _make_silence(0.5)
        audio = _concat(silence_pre, speech, silence_post)
        result = self.vad.detect(audio, SR)
        self.assertGreater(len(result.speech_segments), 0)
        seg = result.speech_segments[0]
        # Начало сегмента — в пределах 0.3 с от начала тона (0.5 с)
        self.assertLess(abs(seg.start_sec - 0.5), 0.3)

    def test_two_speech_regions(self):
        """Два тона, разделённых тишиной → два сегмента."""
        s1 = _make_tone(440, 0.5, amplitude=0.5)
        gap = _make_silence(0.6)
        s2 = _make_tone(880, 0.5, amplitude=0.5)
        audio = _concat(s1, gap, s2)
        result = self.vad.detect(audio, SR)
        self.assertGreaterEqual(len(result.speech_segments), 2)

    def test_speech_ratio_bounds(self):
        """speech_ratio всегда в [0, 1]."""
        for amp in [0.0, 0.01, 0.5, 1.0]:
            audio = np.full(SR * 2, amp, dtype=np.float32)
            result = self.vad.detect(audio, SR)
            self.assertGreaterEqual(result.speech_ratio, 0.0)
            self.assertLessEqual(result.speech_ratio, 1.0)

    def test_total_sec_consistency(self):
        """total_speech_sec + total_silence_sec ≈ длительности записи."""
        speech = _make_tone(440, 1.0, amplitude=0.5)
        silence = _make_silence(1.0)
        audio = _concat(speech, silence)
        result = self.vad.detect(audio, SR)
        total = result.total_speech_sec + result.total_silence_sec
        expected = len(audio) / SR
        self.assertAlmostEqual(total, expected, delta=0.15)

    def test_energy_db_negative(self):
        """energy_db для сегмента речи на уровне 0.5 амплитуды должен быть отрицательным."""
        speech = _make_tone(440, 1.0, amplitude=0.5)
        silence = _make_silence(0.5)
        audio = _concat(speech, silence)
        result = self.vad.detect(audio, SR)
        if result.speech_segments:
            seg = result.speech_segments[0]
            self.assertLess(seg.energy_db, 0.0)

    def test_segment_duration_positive(self):
        """Все возвращённые сегменты должны иметь положительную длительность."""
        speech = _make_tone(300, 2.0, amplitude=0.5)
        result = self.vad.detect(speech, SR)
        for seg in result.speech_segments:
            self.assertGreater(seg.duration_sec, 0.0)
            self.assertGreater(seg.end_sec, seg.start_sec)

    def test_segment_count_in_dict(self):
        """speech_segment_count в to_dict() совпадает с len(speech_segments)."""
        speech = _make_tone(440, 1.0, amplitude=0.5)
        result = self.vad.detect(speech, SR)
        d = result.to_dict()
        self.assertEqual(d["speech_segment_count"], len(result.speech_segments))


class TestVADParameters(unittest.TestCase):
    """Тесты влияния параметров на результат."""

    def test_high_margin_suppresses_quiet_speech(self):
        """Очень высокий margin_db → тихий сигнал не детектируется как речь."""
        quiet_speech = _make_tone(440, 2.0, amplitude=0.01)
        vad = VoiceActivityDetector(margin_db=40.0)
        result = vad.detect(quiet_speech, SR)
        self.assertEqual(len(result.speech_segments), 0)

    def test_min_speech_duration_filters_short(self):
        """min_speech_duration_sec отфильтровывает короткие фрагменты."""
        # Создаём очень короткий тон (20 мс)
        short_speech = _make_tone(440, 0.02, amplitude=0.8)
        silence = _make_silence(0.3)
        audio = _concat(short_speech, silence)
        vad = VoiceActivityDetector(
            margin_db=8.0,
            onset_frames=1,
            offset_frames=1,
            min_speech_duration_sec=0.1,
        )
        result = vad.detect(audio, SR)
        # Сегменты короче 100 мс должны быть отфильтрованы
        for seg in result.speech_segments:
            self.assertGreaterEqual(seg.duration_sec, 0.1)

    def test_different_frame_ms(self):
        """Разные frame_ms дают различающиеся, но разумные результаты."""
        audio = _concat(_make_tone(440, 1.0), _make_silence(1.0))
        vad = VoiceActivityDetector(margin_db=8.0, onset_frames=2, offset_frames=3)
        r10 = vad.detect(audio, SR, frame_ms=10)
        r30 = vad.detect(audio, SR, frame_ms=30)
        r50 = vad.detect(audio, SR, frame_ms=50)
        # Все варианты должны находить хотя бы один сегмент
        self.assertGreater(len(r10.speech_segments), 0)
        self.assertGreater(len(r30.speech_segments), 0)
        self.assertGreater(len(r50.speech_segments), 0)


class TestVADAllSilentAllLoud(unittest.TestCase):
    """All-silent → no speech; all-loud → speech detected."""

    def setUp(self):
        self.vad = VoiceActivityDetector(margin_db=8.0, onset_frames=2, offset_frames=3)

    def test_all_silent_frames_no_speech(self):
        """Полностью тихий сигнал → speech_ratio=0, нет сегментов."""
        silence = _make_silence(3.0, noise_level=1e-6)
        result = self.vad.detect(silence, SR)
        self.assertEqual(len(result.speech_segments), 0)
        self.assertAlmostEqual(result.speech_ratio, 0.0, places=1)

    def test_all_loud_continuous_speech_detected(self):
        """Непрерывный громкий тон → вся запись классифицируется как речь."""
        loud = _make_tone(440, 3.0, amplitude=0.9)
        result = self.vad.detect(loud, SR)
        self.assertGreater(result.speech_ratio, 0.7)
        self.assertGreater(len(result.speech_segments), 0)

    def test_voice_then_gap_then_voice(self):
        """Речь → долгая тишина → речь: два отдельных сегмента."""
        s1 = _make_tone(440, 1.0, amplitude=0.7)
        gap = _make_silence(1.0)
        s2 = _make_tone(880, 1.0, amplitude=0.7)
        audio = _concat(s1, gap, s2)
        result = self.vad.detect(audio, SR)
        # Должны быть сегменты и соотношение речи ~0.6
        self.assertGreater(len(result.speech_segments), 0)
        self.assertGreater(result.speech_ratio, 0.3)
        self.assertLess(result.speech_ratio, 1.0)

    def test_speech_ratio_is_zero_for_true_silence(self):
        """np.zeros → абсолютно тихо → speech_ratio=0."""
        audio = np.zeros(SR * 2, dtype=np.float32)
        result = self.vad.detect(audio, SR)
        self.assertAlmostEqual(result.speech_ratio, 0.0, places=2)

    def test_all_loud_speech_ratio_close_to_1(self):
        """Постоянная амплитуда 0.8 → speech_ratio близко к 1."""
        audio = np.full(SR * 2, 0.8, dtype=np.float32)
        result = self.vad.detect(audio, SR)
        self.assertGreater(result.speech_ratio, 0.7)


class TestVADRequiredCoverage(unittest.TestCase):
    """Явно-именованные тесты из спецификации Wave 109."""

    def setUp(self):
        self.vad = VoiceActivityDetector(margin_db=8.0, onset_frames=2, offset_frames=3)

    def test_speech_detected_in_clear_audio(self):
        """Громкий чистый тон → хотя бы один сегмент речи."""
        audio = _make_tone(440, 2.0, amplitude=0.6)
        result = self.vad.detect(audio, SR)
        self.assertGreater(len(result.speech_segments), 0)
        self.assertGreater(result.speech_ratio, 0.5)

    def test_no_speech_in_silence(self):
        """Абсолютная тишина → нет сегментов речи, ratio=0."""
        audio = np.zeros(SR * 2, dtype=np.float32)
        result = self.vad.detect(audio, SR)
        self.assertEqual(len(result.speech_segments), 0)
        self.assertAlmostEqual(result.speech_ratio, 0.0, places=2)

    def test_handles_short_audio(self):
        """Аудио менее одного фрейма (< 30 ms при SR=16000 → < 480 семплов) не падает."""
        for n in (1, 10, 100, 479):
            with self.subTest(n_samples=n):
                tiny = np.full(n, 0.3, dtype=np.float32)
                result = self.vad.detect(tiny, SR)
                self.assertIsInstance(result, VADResult)
                self.assertGreaterEqual(result.speech_ratio, 0.0)
                self.assertLessEqual(result.speech_ratio, 1.0)

    def test_threshold_adjustable(self):
        """margin_db влияет на чувствительность: высокий margin уменьшает speech_ratio."""
        # Сигнал с умеренной амплитудой (0.05) + фоновый шум (noise_level 1e-4)
        noise = _make_silence(2.0, noise_level=1e-4)
        signal = _make_tone(440, 2.0, amplitude=0.05)
        mixed = noise + signal  # сложение, чтобы не перегружать
        vad_sensitive = VoiceActivityDetector(margin_db=2.0, onset_frames=1, offset_frames=2)
        vad_strict = VoiceActivityDetector(margin_db=50.0, onset_frames=1, offset_frames=2)
        result_sensitive = vad_sensitive.detect(mixed, SR)
        result_strict = vad_strict.detect(mixed, SR)
        # Чувствительный VAD обнаруживает больше речи, строгий — меньше или нуль
        self.assertGreaterEqual(
            result_sensitive.speech_ratio,
            result_strict.speech_ratio,
        )

    def test_concurrent_detect(self):
        """Параллельные вызовы detect() из нескольких потоков не вызывают ошибок."""
        import threading

        errors: list[Exception] = []
        all_results: list[VADResult | None] = [None] * 12

        def run(idx: int):
            try:
                speech = _make_tone(440, 0.5, amplitude=0.5)
                silence = _make_silence(0.5)
                audio = np.concatenate([speech, silence])
                all_results[idx] = self.vad.detect(audio, SR)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Ошибки в потоках: {errors}")
        for i, res in enumerate(all_results):
            self.assertIsNotNone(res, f"Поток {i}: нет результата")
            self.assertGreaterEqual(res.speech_ratio, 0.0)  # type: ignore[union-attr]
            self.assertLessEqual(res.speech_ratio, 1.0)  # type: ignore[union-attr]


class TestVADWave1712Fixes(unittest.TestCase):
    """Wave 1712 regression tests: trailing-silence leak + NaN/Inf sanitization.

    Tests are designed to FAIL on the buggy code and PASS after fixes.
    """

    # ------------------------------------------------------------------
    # BUG 1 — trailing silence leak in _apply_hysteresis
    # ------------------------------------------------------------------

    def test_trailing_silence_not_marked_speech(self):
        """BUG1: [1,1,1,0,0,0] with offset=4 must NOT absorb trailing silence.

        Before fix: result was [T,T,T,T,T,T] — 3 silent tail frames absorbed.
        After fix:  result is  [T,T,T,F,F,F].
        """
        vad = VoiceActivityDetector(onset_frames=1, offset_frames=4)
        is_speech = np.array([True, True, True, False, False, False])
        result = vad._apply_hysteresis(is_speech)

        # Speech frames preserved
        self.assertTrue(result[0])
        self.assertTrue(result[1])
        self.assertTrue(result[2])

        # Trailing silence must stay False — was the bug
        self.assertFalse(result[3], "Trailing silence frame 3 must be False")
        self.assertFalse(result[4], "Trailing silence frame 4 must be False")
        self.assertFalse(result[5], "Trailing silence frame 5 must be False")

    def test_trailing_silence_total_speech_not_inflated(self):
        """BUG1: detect() total_speech_sec must not include trailing silence.

        Construct audio: 3 loud frames (~90 ms @16 kHz with frame_ms=30)
        followed by 3 silent frames. With offset=4 (must wait 4 silent frames
        to close segment), the trailing 3 silence frames would previously be
        absorbed into the speech segment, inflating total_speech_sec.
        """
        sr = 16000
        frame_ms = 30
        frame_size = int(sr * frame_ms / 1000)  # 480 samples

        # 3 speech frames of loud tone
        t = np.linspace(0, frame_ms * 3 / 1000, frame_size * 3, endpoint=False)
        speech_part = (0.6 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        # 3 silence frames
        silence_part = np.zeros(frame_size * 3, dtype=np.float32)
        audio = np.concatenate([speech_part, silence_part])

        vad = VoiceActivityDetector(
            margin_db=6.0,
            onset_frames=1,
            offset_frames=4,  # offset > available trailing silence
            min_speech_duration_sec=0.01,
        )
        result = vad.detect(audio, sr, frame_ms=frame_ms)

        # With trailing silence trimmed, speech_ratio must be ≤ 0.5
        # (speech ≤ half of total, not more)
        self.assertLessEqual(
            result.speech_ratio, 0.55,
            f"speech_ratio={result.speech_ratio:.3f} is too high — trailing silence leaked",
        )

    def test_bridging_gap_shorter_than_offset_still_bridges(self):
        """BUG1: speech-gap-speech where gap < offset must still bridge (no over-trim).

        [1,1,0,0,1,1] with offset=4 → all frames True (gap=2 < offset=4 → bridged).
        """
        vad = VoiceActivityDetector(onset_frames=1, offset_frames=4)
        is_speech = np.array([True, True, False, False, True, True])
        result = vad._apply_hysteresis(is_speech)

        # All 6 frames must be True — gap bridged
        self.assertTrue(
            result.all(),
            f"Bridging failed: {result.tolist()} — gap within offset must stay True",
        )

    def test_bridging_gap_at_least_offset_splits_correctly(self):
        """BUG1 sanity: gap >= offset should produce two separate segments.

        [1,1,0,0,0,0,1,1] with offset=3 → gap=4 >= offset → split, middle False.
        """
        vad = VoiceActivityDetector(onset_frames=1, offset_frames=3)
        is_speech = np.array([True, True, False, False, False, False, True, True])
        result = vad._apply_hysteresis(is_speech)

        # First two frames: speech
        self.assertTrue(result[0])
        self.assertTrue(result[1])
        # Gap frames must be False
        self.assertFalse(result[2])
        self.assertFalse(result[3])
        self.assertFalse(result[4])
        self.assertFalse(result[5])
        # Second speech burst
        self.assertTrue(result[6])
        self.assertTrue(result[7])

    # ------------------------------------------------------------------
    # BUG 2 — NaN/Inf audio silently zeroes out detection
    # ------------------------------------------------------------------

    def test_nan_audio_does_not_zero_speech_detection(self):
        """BUG2: audio with NaN frames must still detect real speech nearby.

        Before fix: any NaN propagated through np.mean → frame_rms=NaN →
        all comparisons False → speech_ratio=0, segments=[].
        After fix: NaN sanitized → real speech still detected.
        """
        sr = 16000
        import math

        # Real speech: 0.5 s loud tone
        t = np.linspace(0, 0.5, int(sr * 0.5), endpoint=False)
        speech = (0.5 * np.sin(2 * math.pi * 440 * t)).astype(np.float32)

        # NaN chunk in the middle (0.1 s)
        nan_chunk = np.full(int(sr * 0.1), float("nan"), dtype=np.float32)

        audio = np.concatenate([speech, nan_chunk, speech])

        vad = VoiceActivityDetector(margin_db=8.0, onset_frames=2, offset_frames=3)
        result = vad.detect(audio, sr)

        self.assertGreater(
            result.speech_ratio, 0.0,
            "NaN audio silently zeroed speech_ratio — BUG2 not fixed",
        )
        self.assertGreater(
            len(result.speech_segments), 0,
            "NaN audio produced zero segments — real speech was discarded",
        )

    def test_inf_audio_does_not_zero_speech_detection(self):
        """BUG2: audio with Inf frames must still detect real speech nearby."""
        sr = 16000
        import math

        t = np.linspace(0, 0.5, int(sr * 0.5), endpoint=False)
        speech = (0.5 * np.sin(2 * math.pi * 440 * t)).astype(np.float32)
        inf_chunk = np.full(int(sr * 0.05), float("inf"), dtype=np.float32)

        audio = np.concatenate([speech, inf_chunk, speech])

        vad = VoiceActivityDetector(margin_db=8.0, onset_frames=2, offset_frames=3)
        result = vad.detect(audio, sr)

        self.assertGreater(
            result.speech_ratio, 0.0,
            "Inf audio silently zeroed speech_ratio — BUG2 not fixed",
        )

    def test_all_nan_audio_returns_empty_gracefully(self):
        """BUG2: fully-NaN buffer must return empty VADResult, not crash."""
        sr = 16000
        audio = np.full(sr, float("nan"), dtype=np.float32)
        vad = VoiceActivityDetector()
        result = vad.detect(audio, sr)
        self.assertIsInstance(result, VADResult)
        self.assertAlmostEqual(result.speech_ratio, 0.0, places=2)
        self.assertEqual(len(result.speech_segments), 0)

    def test_speech_ratio_bounds_with_nan(self):
        """BUG2: speech_ratio must stay in [0, 1] even with NaN samples."""
        sr = 16000
        import math

        t = np.linspace(0, 1.0, sr, endpoint=False)
        speech = (0.5 * np.sin(2 * math.pi * 440 * t)).astype(np.float32)
        # Sprinkle NaN every 100 samples
        speech[::100] = float("nan")

        vad = VoiceActivityDetector()
        result = vad.detect(speech, sr)

        self.assertGreaterEqual(result.speech_ratio, 0.0)
        self.assertLessEqual(result.speech_ratio, 1.0)


if __name__ == "__main__":
    unittest.main()
