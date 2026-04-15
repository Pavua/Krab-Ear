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


if __name__ == "__main__":
    unittest.main()
