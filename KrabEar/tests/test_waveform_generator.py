"""Тесты WaveformGenerator — генерация waveform-данных для визуализации.

Покрывает:
- generate_waveform: базовая генерация, нормализация, пустой буфер
- WaveformData: структура и поля
- generate_from_file: несуществующий файл, корректный WAV
- IPC: метод get_waveform через BackendService
"""

from __future__ import annotations
from core.waveform_generator import WaveformData, WaveformGenerator

import math
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

# Настройка путей для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ── Вспомогательные функции ─────────────────────────────────────────────────


def _make_sine(sample_rate: int = 16000, duration_sec: float = 1.0, freq: float = 440.0) -> np.ndarray:
    """Генерирует синусоиду в диапазоне [-1, 1]."""
    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
    return np.sin(2 * np.pi * freq * t).astype(np.float32)


def _write_wav(path: Path, audio: np.ndarray, sample_rate: int = 16000) -> None:
    """Сохраняет float32 аудио в 16-bit PCM WAV."""
    pcm = (audio * 32767).astype(np.int16)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


# ── Тест 1: базовая генерация синусоиды ────────────────────────────────────


class TestGenerateWaveformBasic(unittest.TestCase):
    """Базовые свойства WaveformData после generate_waveform."""

    def setUp(self):
        self.gen = WaveformGenerator()
        self.sr = 16000
        self.audio = _make_sine(self.sr, duration_sec=1.0)

    def test_returns_waveform_data_instance(self):
        result = self.gen.generate_waveform(self.audio, self.sr)
        self.assertIsInstance(result, WaveformData)

    def test_points_count_matches_num_points(self):
        result = self.gen.generate_waveform(self.audio, self.sr, num_points=100)
        self.assertEqual(len(result.points), 100)

    def test_default_num_points_is_200(self):
        result = self.gen.generate_waveform(self.audio, self.sr)
        self.assertEqual(len(result.points), 200)

    def test_points_normalized_between_0_and_1(self):
        result = self.gen.generate_waveform(self.audio, self.sr, num_points=50)
        for p in result.points:
            self.assertGreaterEqual(p, 0.0, f"Точка {p} < 0")
            self.assertLessEqual(p, 1.0, f"Точка {p} > 1")

    def test_duration_sec_correct(self):
        result = self.gen.generate_waveform(self.audio, self.sr)
        self.assertAlmostEqual(result.duration_sec, 1.0, places=2)

    def test_sample_rate_preserved(self):
        result = self.gen.generate_waveform(self.audio, self.sr)
        self.assertEqual(result.sample_rate, self.sr)

    def test_peak_amplitude_positive(self):
        result = self.gen.generate_waveform(self.audio, self.sr)
        self.assertGreater(result.peak_amplitude, 0.0)

    def test_rms_amplitude_positive(self):
        result = self.gen.generate_waveform(self.audio, self.sr)
        self.assertGreater(result.rms_amplitude, 0.0)

    def test_peak_ge_rms(self):
        result = self.gen.generate_waveform(self.audio, self.sr)
        self.assertGreaterEqual(result.peak_amplitude, result.rms_amplitude)


# ── Тест 2: пустой аудиобуфер ─────────────────────────────────────────────


class TestGenerateWaveformEmpty(unittest.TestCase):
    """Поведение при пустом буфере."""

    def setUp(self):
        self.gen = WaveformGenerator()

    def test_empty_array_returns_zeros(self):
        result = self.gen.generate_waveform(np.array([], dtype=np.float32), 16000)
        self.assertEqual(len(result.points), 200)
        self.assertTrue(all(p == 0.0 for p in result.points))

    def test_empty_array_duration_zero(self):
        result = self.gen.generate_waveform(np.array([], dtype=np.float32), 16000)
        self.assertEqual(result.duration_sec, 0.0)

    def test_empty_array_peak_zero(self):
        result = self.gen.generate_waveform(np.array([], dtype=np.float32), 16000)
        self.assertEqual(result.peak_amplitude, 0.0)


# ── Тест 3: нормализация — максимальная точка всегда == 1.0 ───────────────


class TestNormalization(unittest.TestCase):
    """Пик waveform должен быть равен 1.0 для ненулевого сигнала."""

    def setUp(self):
        self.gen = WaveformGenerator()

    def test_max_point_equals_one_for_sine(self):
        audio = _make_sine(16000, 1.0)
        result = self.gen.generate_waveform(audio, 16000, num_points=100)
        self.assertAlmostEqual(max(result.points), 1.0, places=5)

    def test_max_point_equals_one_for_constant(self):
        audio = np.full(16000, 0.5, dtype=np.float32)
        result = self.gen.generate_waveform(audio, 16000, num_points=50)
        self.assertAlmostEqual(max(result.points), 1.0, places=5)

    def test_scaled_audio_same_shape(self):
        """Масштабирование сигнала не меняет форму нормализованного waveform."""
        audio = _make_sine(16000, 1.0)
        r1 = self.gen.generate_waveform(audio * 0.1, 16000, num_points=50)
        r2 = self.gen.generate_waveform(audio * 0.9, 16000, num_points=50)
        for p1, p2 in zip(r1.points, r2.points):
            self.assertAlmostEqual(p1, p2, places=4)


# ── Тест 4: многоканальное аудио ──────────────────────────────────────────


class TestMultichannelAudio(unittest.TestCase):
    """2D аудио (samples, channels) должно усредняться до моно."""

    def setUp(self):
        self.gen = WaveformGenerator()

    def test_stereo_audio_returns_correct_length(self):
        # (N, 2) — стерео
        stereo = np.random.uniform(-1, 1, (8000, 2)).astype(np.float32)
        result = self.gen.generate_waveform(stereo, 16000, num_points=80)
        self.assertEqual(len(result.points), 80)

    def test_stereo_points_in_range(self):
        stereo = np.random.uniform(-1, 1, (8000, 2)).astype(np.float32)
        result = self.gen.generate_waveform(stereo, 16000, num_points=80)
        for p in result.points:
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)


# ── Тест 5: параметр num_points ───────────────────────────────────────────


class TestNumPoints(unittest.TestCase):
    """num_points с разными значениями."""

    def setUp(self):
        self.gen = WaveformGenerator()
        self.audio = _make_sine(16000, 1.0)

    def test_num_points_1(self):
        result = self.gen.generate_waveform(self.audio, 16000, num_points=1)
        self.assertEqual(len(result.points), 1)
        self.assertAlmostEqual(result.points[0], 1.0, places=4)

    def test_num_points_500(self):
        result = self.gen.generate_waveform(self.audio, 16000, num_points=500)
        self.assertEqual(len(result.points), 500)

    def test_num_points_larger_than_samples(self):
        """Если точек больше чем семплов, должен работать корректно."""
        short_audio = _make_sine(16000, 0.01)  # ~160 семплов
        result = self.gen.generate_waveform(short_audio, 16000, num_points=200)
        self.assertEqual(len(result.points), 200)
        for p in result.points:
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)

    def test_invalid_num_points_raises(self):
        with self.assertRaises(ValueError):
            self.gen.generate_waveform(self.audio, 16000, num_points=0)

    def test_invalid_sample_rate_raises(self):
        with self.assertRaises(ValueError):
            self.gen.generate_waveform(self.audio, -1)


# ── Тест 6: generate_from_file ────────────────────────────────────────────


class TestAlreadySmallAudio(unittest.TestCase):
    """Fewer samples than num_points: output still has num_points values."""

    def setUp(self):
        self.gen = WaveformGenerator()

    def test_tiny_audio_output_length_matches_num_points(self):
        """10 samples, 200 points — output still has 200 points."""
        tiny = np.array([0.1, -0.2, 0.3, -0.4, 0.5, -0.5, 0.4, -0.3, 0.2, -0.1],
                        dtype=np.float32)
        result = self.gen.generate_waveform(tiny, 16000, num_points=200)
        self.assertEqual(len(result.points), 200)

    def test_tiny_audio_points_in_range(self):
        tiny = np.linspace(-0.8, 0.8, 5, dtype=np.float32)
        result = self.gen.generate_waveform(tiny, 16000, num_points=50)
        for p in result.points:
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)

    def test_single_sample_returns_num_points(self):
        single = np.array([0.7], dtype=np.float32)
        result = self.gen.generate_waveform(single, 16000, num_points=10)
        self.assertEqual(len(result.points), 10)
        # single sample: every point should be peak-normalised → 1.0
        self.assertTrue(all(abs(p - 1.0) < 1e-5 for p in result.points))


class TestEnvelopePreservation(unittest.TestCase):
    """Downsampled waveform preserves the amplitude envelope."""

    def setUp(self):
        self.gen = WaveformGenerator()

    def test_silent_region_reflected_in_low_points(self):
        """Second half silent → second half of waveform should be near zero."""
        sr = 16000
        half = sr // 2
        audio = np.concatenate([
            np.ones(half, dtype=np.float32),
            np.zeros(half, dtype=np.float32),
        ])
        result = self.gen.generate_waveform(audio, sr, num_points=100)
        first_half = result.points[:50]
        second_half = result.points[50:]
        # first half should average much higher than second half
        self.assertGreater(sum(first_half) / 50, sum(second_half) / 50 + 0.4)

    def test_increasing_amplitude_envelope(self):
        """Linearly increasing amplitude → later bins larger than earlier bins."""
        sr = 16000
        ramp = np.linspace(0.0, 1.0, sr, dtype=np.float32)
        result = self.gen.generate_waveform(ramp, sr, num_points=100)
        first_quarter_avg = sum(result.points[:25]) / 25
        last_quarter_avg = sum(result.points[75:]) / 25
        self.assertGreater(last_quarter_avg, first_quarter_avg)


class TestGenerateFromFile(unittest.TestCase):
    """Чтение waveform из файла."""

    def setUp(self):
        self.gen = WaveformGenerator()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            self.gen.generate_from_file("/nonexistent/path/audio.wav")

    def test_wav_file_returns_correct_num_points(self):
        audio = _make_sine(16000, 1.0)
        wav_path = self.tmp_dir / "test.wav"
        _write_wav(wav_path, audio)
        result = self.gen.generate_from_file(str(wav_path), num_points=100)
        self.assertEqual(len(result.points), 100)

    def test_wav_file_duration_correct(self):
        audio = _make_sine(16000, 2.0)
        wav_path = self.tmp_dir / "test2s.wav"
        _write_wav(wav_path, audio)
        result = self.gen.generate_from_file(str(wav_path))
        self.assertAlmostEqual(result.duration_sec, 2.0, delta=0.05)

    def test_wav_file_sample_rate_correct(self):
        audio = _make_sine(22050, 1.0, freq=440.0)
        wav_path = self.tmp_dir / "test22k.wav"
        _write_wav(wav_path, audio, sample_rate=22050)
        result = self.gen.generate_from_file(str(wav_path))
        self.assertEqual(result.sample_rate, 22050)

    def test_wav_file_points_in_range(self):
        audio = _make_sine(16000, 0.5)
        wav_path = self.tmp_dir / "test_range.wav"
        _write_wav(wav_path, audio)
        result = self.gen.generate_from_file(str(wav_path), num_points=50)
        for p in result.points:
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)

    def test_wav_file_peak_positive(self):
        audio = _make_sine(16000, 1.0)
        wav_path = self.tmp_dir / "test_peak.wav"
        _write_wav(wav_path, audio)
        result = self.gen.generate_from_file(str(wav_path))
        self.assertGreater(result.peak_amplitude, 0.0)


# ── Тест 7: IPC-метод get_waveform ────────────────────────────────────────


class TestGetWaveformIPC(unittest.TestCase):
    """Тест IPC-метода get_waveform через BackendService."""

    def setUp(self):
        from backend.state_store import StateStore
        from backend.service import BackendService

        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        self.store = StateStore(data_dir=tmp_path)

        # Создаём BackendService с минимальными зависимостями
        self.service = BackendService(store=self.store)

        # Тестовый WAV файл
        audio = _make_sine(16000, 1.0)
        self.wav_path = tmp_path / "ipc_test.wav"
        _write_wav(self.wav_path, audio)

    def tearDown(self):
        self._tmp.cleanup()

    def test_get_waveform_returns_ok(self):
        response = self.service.handle_request({
            "id": "t1",
            "method": "get_waveform",
            "params": {"file_path": str(self.wav_path)},
        })
        self.assertTrue(response.get("ok"), f"Ответ не ok: {response}")

    def test_get_waveform_result_has_points(self):
        response = self.service.handle_request({
            "id": "t2",
            "method": "get_waveform",
            "params": {"file_path": str(self.wav_path)},
        })
        result = response.get("result", {})
        self.assertIn("points", result)
        self.assertEqual(len(result["points"]), 200)

    def test_get_waveform_custom_num_points(self):
        response = self.service.handle_request({
            "id": "t3",
            "method": "get_waveform",
            "params": {"file_path": str(self.wav_path), "num_points": 50},
        })
        result = response.get("result", {})
        self.assertEqual(len(result["points"]), 50)

    def test_get_waveform_result_has_required_fields(self):
        response = self.service.handle_request({
            "id": "t4",
            "method": "get_waveform",
            "params": {"file_path": str(self.wav_path)},
        })
        result = response.get("result", {})
        for field in ("points", "duration_sec", "sample_rate", "peak_amplitude", "rms_amplitude"):
            self.assertIn(field, result, f"Поле '{field}' отсутствует в ответе")

    def test_get_waveform_missing_file_path_returns_error(self):
        response = self.service.handle_request({
            "id": "t5",
            "method": "get_waveform",
            "params": {},
        })
        self.assertFalse(response.get("ok", True))

    def test_get_waveform_nonexistent_file_returns_error(self):
        response = self.service.handle_request({
            "id": "t6",
            "method": "get_waveform",
            "params": {"file_path": "/no/such/file.wav"},
        })
        self.assertFalse(response.get("ok", True))


# ── Тест 8: NaN/Inf guard (W1539 F4) ─────────────────────────────────────


class TestNaNInfGuard(unittest.TestCase):
    """W1549: corrupt audio (NaN/Inf samples) must not propagate to WaveformData.

    RFC 8259 disallows NaN/Inf literals in JSON — Swift JSONDecoder rejects them
    and drops the entire IPC response, blanking the waveform UI.
    """

    def setUp(self):
        self.gen = WaveformGenerator()

    def test_waveform_nan_audio_input_returns_zero_rms(self):
        """All-NaN input: rms_amplitude must be 0.0, not NaN."""
        nan_audio = np.full(1600, float("nan"), dtype=np.float32)
        result = self.gen.generate_waveform(nan_audio, 16000, num_points=10)
        self.assertEqual(result.rms_amplitude, 0.0,
                         "rms_amplitude must be 0.0 for NaN audio")
        self.assertFalse(math.isnan(result.rms_amplitude),
                         "rms_amplitude must not be NaN")

    def test_waveform_inf_audio_input_returns_zero_peak(self):
        """All-Inf input: peak_amplitude must be 0.0, not Inf."""
        inf_audio = np.full(1600, float("inf"), dtype=np.float32)
        result = self.gen.generate_waveform(inf_audio, 16000, num_points=10)
        self.assertEqual(result.peak_amplitude, 0.0,
                         "peak_amplitude must be 0.0 for Inf audio")
        self.assertFalse(math.isinf(result.peak_amplitude),
                         "peak_amplitude must not be Inf")

    def test_waveform_normal_audio_unchanged(self):
        """Normal audio: rms and peak must be finite and positive."""
        audio = _make_sine(16000, duration_sec=1.0)
        result = self.gen.generate_waveform(audio, 16000, num_points=100)
        self.assertTrue(math.isfinite(result.rms_amplitude),
                        "rms_amplitude must be finite for normal audio")
        self.assertTrue(math.isfinite(result.peak_amplitude),
                        "peak_amplitude must be finite for normal audio")
        self.assertGreater(result.rms_amplitude, 0.0)
        self.assertGreater(result.peak_amplitude, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
