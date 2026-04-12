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


if __name__ == "__main__":
    unittest.main()
