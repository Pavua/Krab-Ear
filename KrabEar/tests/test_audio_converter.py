"""Тесты для AudioConverter (core/audio_converter.py).

Использует синтетические WAV-файлы, сгенерированные через numpy+soundfile.
ffmpeg-зависимые тесты пропускаются если бинарник не найден.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import soundfile as sf

from core.audio_converter import AudioConverter, AudioInfo, SUPPORTED_FORMATS


def _make_wav(path: str, duration: float = 1.0, sample_rate: int = 16000, channels: int = 1) -> str:
    """Создаёт синтетический WAV с синусоидой."""
    samples = int(duration * sample_rate)
    t = np.linspace(0, duration, samples, endpoint=False)
    audio = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    if channels == 2:
        audio = np.stack([audio, audio], axis=1)
    sf.write(path, audio, sample_rate)
    return path


class TestAudioInfo(unittest.TestCase):
    """Тесты AudioInfo dataclass и get_audio_info."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="krab_ear_test_")
        self.converter = AudioConverter()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_get_audio_info_basic(self):
        """get_audio_info возвращает корректные метаданные для WAV файла."""
        wav = os.path.join(self.tmp, "test.wav")
        _make_wav(wav, duration=2.0, sample_rate=16000)
        info = self.converter.get_audio_info(wav)
        self.assertIsInstance(info, AudioInfo)
        self.assertAlmostEqual(info.duration, 2.0, delta=0.05)
        self.assertEqual(info.sample_rate, 16000)
        self.assertEqual(info.channels, 1)
        self.assertEqual(info.format, "wav")
        self.assertGreater(info.size_mb, 0)

    def test_get_audio_info_stereo(self):
        """get_audio_info корректно определяет 2 канала."""
        wav = os.path.join(self.tmp, "stereo.wav")
        _make_wav(wav, duration=0.5, sample_rate=22050, channels=2)
        info = self.converter.get_audio_info(wav)
        self.assertEqual(info.channels, 2)
        self.assertEqual(info.sample_rate, 22050)

    def test_get_audio_info_file_not_found(self):
        """get_audio_info бросает FileNotFoundError для несуществующего файла."""
        with self.assertRaises(FileNotFoundError):
            self.converter.get_audio_info("/tmp/krab_ear_nonexistent_12345.wav")

    def test_get_audio_info_invalid_file(self):
        """get_audio_info бросает RuntimeError для невалидного файла."""
        bad = os.path.join(self.tmp, "bad.wav")
        with open(bad, "wb") as f:
            f.write(b"not an audio file at all")
        with self.assertRaises(RuntimeError):
            self.converter.get_audio_info(bad)


class TestSupportedFormats(unittest.TestCase):
    """Тесты is_supported_format."""

    def setUp(self):
        self.converter = AudioConverter()

    def test_supported_formats_positive(self):
        """Поддерживаемые форматы распознаются корректно."""
        for ext in [".wav", ".mp3", ".ogg", ".flac", ".m4a"]:
            with self.subTest(ext=ext):
                self.assertTrue(self.converter.is_supported_format(f"/some/file{ext}"))

    def test_supported_formats_case_insensitive(self):
        """Проверка регистронезависимости расширений."""
        self.assertTrue(self.converter.is_supported_format("/file.WAV"))
        self.assertTrue(self.converter.is_supported_format("/file.MP3"))

    def test_unsupported_formats(self):
        """Неподдерживаемые форматы возвращают False."""
        for ext in [".avi", ".mp4", ".txt", ".aiff", ""]:
            with self.subTest(ext=ext):
                self.assertFalse(self.converter.is_supported_format(f"/file{ext}"))

    def test_supported_formats_set(self):
        """SUPPORTED_FORMATS содержит ожидаемый набор расширений."""
        expected = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}
        self.assertEqual(SUPPORTED_FORMATS, expected)


class TestConvert(unittest.TestCase):
    """Тесты метода convert (требует ffmpeg)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="krab_ear_test_conv_")
        self.converter = AudioConverter()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _skip_if_no_ffmpeg(self):
        if not self.converter.is_ffmpeg_available():
            self.skipTest("ffmpeg не установлен — пропуск теста конвертации")

    def test_convert_wav_to_wav(self):
        """Конвертация WAV→WAV меняет sample_rate до 16000."""
        self._skip_if_no_ffmpeg()
        src = os.path.join(self.tmp, "input.wav")
        dst = os.path.join(self.tmp, "output.wav")
        _make_wav(src, duration=0.5, sample_rate=44100)
        result = self.converter.convert(src, output_format="wav", sample_rate=16000, output_path=dst)
        self.assertEqual(result, dst)
        info = self.converter.get_audio_info(dst)
        self.assertEqual(info.sample_rate, 16000)
        self.assertEqual(info.channels, 1)

    def test_convert_returns_temp_path_when_no_output(self):
        """Без output_path возвращается путь к временному файлу."""
        self._skip_if_no_ffmpeg()
        src = os.path.join(self.tmp, "input.wav")
        _make_wav(src, duration=0.3, sample_rate=22050)
        result = self.converter.convert(src)
        try:
            self.assertTrue(os.path.exists(result))
            self.assertTrue(result.endswith(".wav"))
        finally:
            os.unlink(result)

    def test_convert_file_not_found(self):
        """convert бросает FileNotFoundError для несуществующего файла."""
        with self.assertRaises(FileNotFoundError):
            self.converter.convert("/tmp/krab_ear_no_such_file.wav")

    def test_convert_unsupported_format(self):
        """convert бросает ValueError для неподдерживаемого формата."""
        bad = os.path.join(self.tmp, "file.avi")
        with open(bad, "wb") as f:
            f.write(b"dummy")
        with self.assertRaises(ValueError):
            self.converter.convert(bad)

    def test_convert_no_ffmpeg_raises(self):
        """convert бросает RuntimeError если ffmpeg недоступен."""
        converter = AudioConverter(ffmpeg_path="/nonexistent/ffmpeg")
        src = os.path.join(self.tmp, "input.wav")
        _make_wav(src, duration=0.2, sample_rate=16000)
        with self.assertRaises(RuntimeError):
            converter.convert(src)


class TestAudioConverterInit(unittest.TestCase):
    """Тесты инициализации и is_ffmpeg_available."""

    def test_explicit_nonexistent_ffmpeg(self):
        """AudioConverter с несуществующим ffmpeg_path: is_ffmpeg_available() == False."""
        c = AudioConverter(ffmpeg_path="/nonexistent/ffmpeg")
        self.assertFalse(c.is_ffmpeg_available())

    def test_auto_detect(self):
        """AudioConverter без аргументов пытается обнаружить ffmpeg."""
        c = AudioConverter()
        # Просто убеждаемся что метод возвращает bool без исключений
        self.assertIsInstance(c.is_ffmpeg_available(), bool)


if __name__ == "__main__":
    unittest.main()
