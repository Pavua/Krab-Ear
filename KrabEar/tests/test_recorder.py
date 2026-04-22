"""Unit-тесты базового поведения AudioRecorder.

sounddevice мокируется — реальный микрофон не нужен.
"""

from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

# Путь для прямого запуска через unittest
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.recorder import AudioRecorder  # noqa: E402


def _make_mock_stream(chunk_size: int = 1600) -> MagicMock:
    """Возвращает mock-объект, имитирующий sd.InputStream как context manager."""
    stream = MagicMock()
    # read() возвращает (data, overflowed=False)
    stream.read.return_value = (np.zeros((chunk_size, 1), dtype=np.float32), False)
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=stream)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


class AudioRecorderStateTest(unittest.TestCase):

    def test_initial_state_not_recording(self) -> None:
        """Новый рекордер не должен быть в состоянии записи."""
        rec = AudioRecorder()
        self.assertFalse(rec.is_recording)

    def test_start_sets_recording_state(self) -> None:
        """После start() рекордер должен сообщать is_recording=True."""
        with patch("sounddevice.InputStream", return_value=_make_mock_stream()):
            rec = AudioRecorder()
            result = rec.start()
            try:
                self.assertTrue(result, "start() должен вернуть True при первом вызове")
                self.assertTrue(rec.is_recording)
            finally:
                rec.stop()

    def test_stop_clears_recording_state(self) -> None:
        """После stop() рекордер должен сообщать is_recording=False."""
        with patch("sounddevice.InputStream", return_value=_make_mock_stream()):
            rec = AudioRecorder()
            rec.start()
            rec.stop()
            self.assertFalse(rec.is_recording)

    def test_double_start_is_safe(self) -> None:
        """Повторный вызов start() не должен падать; второй вызов возвращает False."""
        with patch("sounddevice.InputStream", return_value=_make_mock_stream()):
            rec = AudioRecorder()
            first = rec.start()
            try:
                second = rec.start()
                self.assertTrue(first)
                self.assertFalse(second, "Повторный start() должен вернуть False")
                self.assertTrue(rec.is_recording)
            finally:
                rec.stop()

    def test_double_stop_is_safe(self) -> None:
        """Повторный вызов stop() не должен падать; второй вызов возвращает None."""
        with patch("sounddevice.InputStream", return_value=_make_mock_stream()):
            rec = AudioRecorder()
            rec.start()
            first = rec.stop()
            second = rec.stop()
            self.assertIsNotNone(first, "Первый stop() должен вернуть (audio, duration)")
            self.assertIsNone(second, "Повторный stop() должен вернуть None")
            self.assertFalse(rec.is_recording)


class AudioRecorderGetAudioTest(unittest.TestCase):
    """stop() возвращает numpy-массив, имитируем chunks вручную."""

    def _make_recorder_with_chunks(
        self, n_samples: int = 1600, sample_rate: int = 16000
    ) -> AudioRecorder:
        import time

        rec = AudioRecorder(sample_rate=sample_rate, channels=1)
        rec._is_recording = True  # noqa: SLF001
        rec._started_at = time.monotonic() - 0.5  # noqa: SLF001
        rec._chunks = [  # noqa: SLF001
            np.ones((n_samples, 1), dtype=np.float32) * 0.5
        ]
        rec._thread = None  # noqa: SLF001
        return rec

    def test_stop_returns_tuple(self) -> None:
        """stop() должен вернуть (ndarray, float)."""
        rec = self._make_recorder_with_chunks()
        result = rec.stop()
        self.assertIsNotNone(result)
        audio, duration = result
        self.assertIsInstance(audio, np.ndarray)
        self.assertIsInstance(duration, float)

    def test_stop_audio_is_float32(self) -> None:
        rec = self._make_recorder_with_chunks()
        audio, _ = rec.stop()
        self.assertEqual(audio.dtype, np.float32)

    def test_stop_audio_is_1d(self) -> None:
        """stop() должен вернуть одномерный массив."""
        rec = self._make_recorder_with_chunks(n_samples=800)
        audio, _ = rec.stop()
        self.assertEqual(audio.ndim, 1)

    def test_stop_audio_values_correct(self) -> None:
        """Все значения в возвращённом массиве должны быть 0.5."""
        rec = self._make_recorder_with_chunks(n_samples=100)
        audio, _ = rec.stop()
        self.assertTrue(np.allclose(audio, 0.5))

    def test_stop_empty_chunks_returns_empty_array(self) -> None:
        """Если чанков нет — возвращается пустой массив."""
        import time

        rec = AudioRecorder()
        rec._is_recording = True  # noqa: SLF001
        rec._started_at = time.monotonic()  # noqa: SLF001
        rec._chunks = []  # noqa: SLF001
        rec._thread = None  # noqa: SLF001
        result = rec.stop()
        self.assertIsNotNone(result)
        audio, _ = result
        self.assertEqual(audio.size, 0)

    def test_stop_duration_is_positive(self) -> None:
        rec = self._make_recorder_with_chunks()
        _, duration = rec.stop()
        self.assertGreaterEqual(duration, 0.0)


class AudioRecorderDeviceParamsTest(unittest.TestCase):
    """Проверка device selection через конструктор (sample_rate, channels)."""

    def test_default_sample_rate(self) -> None:
        rec = AudioRecorder()
        self.assertEqual(rec.sample_rate, 16000)

    def test_default_channels(self) -> None:
        rec = AudioRecorder()
        self.assertEqual(rec.channels, 1)

    def test_custom_sample_rate(self) -> None:
        rec = AudioRecorder(sample_rate=44100)
        self.assertEqual(rec.sample_rate, 44100)

    def test_custom_channels(self) -> None:
        rec = AudioRecorder(channels=2)
        self.assertEqual(rec.channels, 2)

    def test_chunk_size_derived_from_sample_rate(self) -> None:
        """chunk_size должен быть 10% от sample_rate."""
        rec = AudioRecorder(sample_rate=8000)
        self.assertEqual(rec.chunk_size, 800)

    def test_custom_params_forwarded_to_sounddevice(self) -> None:
        """Убеждаемся, что InputStream получает правильный sample_rate."""
        mock_stream = _make_mock_stream()
        import sounddevice as sd  # type: ignore  # noqa: F401

        with patch("sounddevice.InputStream", return_value=mock_stream) as mock_sd:
            rec = AudioRecorder(sample_rate=22050, channels=1)
            rec.start()
            try:
                # Дождёмся, чтобы worker успел открыть поток
                import time
                time.sleep(0.05)
            finally:
                rec.stop()

            call_kwargs = mock_sd.call_args
            self.assertIsNotNone(call_kwargs)
            kwargs = call_kwargs[1] if call_kwargs[1] else {}
            args = call_kwargs[0] if call_kwargs[0] else ()
            # samplerate передаётся как kwarg
            self.assertEqual(kwargs.get("samplerate", args[0] if args else None), 22050)


class AudioRecorderSnapshotTest(unittest.TestCase):
    """Тест snapshot_audio и get_duration_sec."""

    def _make_recorder_recording(
        self, n_samples: int = 3200, sample_rate: int = 16000
    ) -> AudioRecorder:
        import time

        rec = AudioRecorder(sample_rate=sample_rate, channels=1)
        rec._is_recording = True  # noqa: SLF001
        rec._started_at = time.monotonic() - 1.0  # noqa: SLF001
        rec._chunks = [  # noqa: SLF001
            np.ones((n_samples, 1), dtype=np.float32) * 0.3
        ]
        rec._thread = None  # noqa: SLF001
        return rec

    def test_get_duration_sec_when_recording(self) -> None:
        rec = self._make_recorder_recording()
        dur = rec.get_duration_sec()
        self.assertGreater(dur, 0.0)

    def test_get_duration_sec_when_idle(self) -> None:
        rec = AudioRecorder()
        self.assertEqual(rec.get_duration_sec(), 0.0)

    def test_snapshot_returns_tuple(self) -> None:
        rec = self._make_recorder_recording()
        result = rec.snapshot_audio()
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_snapshot_audio_is_ndarray(self) -> None:
        rec = self._make_recorder_recording()
        audio, _ = rec.snapshot_audio()
        self.assertIsInstance(audio, np.ndarray)

    def test_snapshot_does_not_stop_recording(self) -> None:
        rec = self._make_recorder_recording()
        rec.snapshot_audio()
        self.assertTrue(rec.is_recording)

    def test_snapshot_respects_max_duration(self) -> None:
        """max_duration_sec=0.1 ограничивает длину снимка."""
        # 3200 samples @ 16kHz = 0.2s; max 0.1s = 1600 samples
        rec = self._make_recorder_recording(n_samples=3200, sample_rate=16000)
        audio, _ = rec.snapshot_audio(max_duration_sec=0.1)
        self.assertLessEqual(audio.size, 1600)

    def test_snapshot_empty_when_no_chunks(self) -> None:
        import time

        rec = AudioRecorder()
        rec._is_recording = True  # noqa: SLF001
        rec._started_at = time.monotonic()  # noqa: SLF001
        rec._chunks = []  # noqa: SLF001
        audio, _ = rec.snapshot_audio()
        self.assertEqual(audio.size, 0)

    def test_snapshot_duration_matches_elapsed(self) -> None:
        rec = self._make_recorder_recording()
        _, dur = rec.snapshot_audio()
        # _started_at = monotonic() - 1.0, so duration ≈ 1.0
        self.assertGreaterEqual(dur, 0.9)


if __name__ == "__main__":
    unittest.main()
