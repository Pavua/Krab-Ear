"""Тесты для AudioConverter (core/audio_converter.py).

Использует синтетические WAV-файлы, сгенерированные через numpy+soundfile.
ffmpeg-зависимые тесты пропускаются если бинарник не найден.
"""

from __future__ import annotations
from core.audio_converter import AudioConverter, AudioInfo, SUPPORTED_FORMATS
import soundfile as sf
import numpy as np

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


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


class TestConvertMocked(unittest.TestCase):
    """Тесты convert() через mock subprocess — не требует реального ffmpeg."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="krab_ear_mock_conv_")
        # Создаём stub ffmpeg path (должен существовать + быть executable для init)
        self.fake_ffmpeg = os.path.join(self.tmp, "ffmpeg")
        with open(self.fake_ffmpeg, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(self.fake_ffmpeg, 0o755)
        self.converter = AudioConverter(ffmpeg_path=self.fake_ffmpeg)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_src(self, name: str = "input.wav") -> str:
        path = os.path.join(self.tmp, name)
        _make_wav(path, duration=0.2, sample_rate=16000)
        return path

    def _mock_run_ok(self):
        """Возвращает mock subprocess.CompletedProcess с returncode=0."""
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    # ── Проверка аргументов subprocess ───────────────────────────────────────

    def test_convert_calls_ffmpeg_with_correct_args(self):
        """convert() вызывает ffmpeg с -y -i src -ac 1 -ar rate dst."""
        src = self._make_src()
        dst = os.path.join(self.tmp, "out.wav")

        with patch("subprocess.run", return_value=self._mock_run_ok()) as mock_run:
            result = self.converter.convert(src, output_format="wav", sample_rate=16000, output_path=dst)

        self.assertEqual(result, dst)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], self.fake_ffmpeg)
        self.assertIn("-y", cmd)
        self.assertIn("-i", cmd)
        self.assertIn(str(src), cmd)
        self.assertIn("-ac", cmd)
        self.assertIn("1", cmd)
        self.assertIn("-ar", cmd)
        self.assertIn("16000", cmd)
        self.assertIn(dst, cmd)

    def test_convert_custom_sample_rate_passed_to_ffmpeg(self):
        """Пользовательский sample_rate передаётся в аргументы ffmpeg."""
        src = self._make_src()
        dst = os.path.join(self.tmp, "out22.wav")

        with patch("subprocess.run", return_value=self._mock_run_ok()) as mock_run:
            self.converter.convert(src, output_format="wav", sample_rate=22050, output_path=dst)

        cmd = mock_run.call_args[0][0]
        ar_idx = cmd.index("-ar")
        self.assertEqual(cmd[ar_idx + 1], "22050")

    def test_convert_nonzero_returncode_raises_runtime_error(self):
        """convert() бросает RuntimeError если ffmpeg вернул ненулевой код."""
        src = self._make_src()
        dst = os.path.join(self.tmp, "out_fail.wav")

        fail_result = MagicMock()
        fail_result.returncode = 1
        fail_result.stderr = "some ffmpeg error"

        with patch("subprocess.run", return_value=fail_result):
            with self.assertRaises(RuntimeError) as ctx:
                self.converter.convert(src, output_format="wav", output_path=dst)
        self.assertIn("1", str(ctx.exception))

    def test_convert_returns_temp_path_without_output_path(self):
        """Без output_path convert() создаёт временный файл и возвращает его путь."""
        src = self._make_src()
        created_paths: list = []

        def fake_run(cmd, **kwargs):
            # Симулируем создание выходного файла ffmpeg
            dst = cmd[-1]
            with open(dst, "wb") as f:
                f.write(b"\x00" * 64)
            created_paths.append(dst)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        with patch("subprocess.run", side_effect=fake_run):
            result = self.converter.convert(src)

        self.assertTrue(len(created_paths) == 1)
        self.assertEqual(result, created_paths[0])
        self.assertTrue(result.endswith(".wav"))
        # Cleanup temp
        try:
            os.unlink(result)
        except OSError:
            pass

    def test_convert_subprocess_oserror_raises_runtime_error(self):
        """OSError от subprocess (ffmpeg не запускается) → RuntimeError."""
        src = self._make_src()
        dst = os.path.join(self.tmp, "out_os.wav")

        with patch("subprocess.run", side_effect=OSError("exec failed")):
            with self.assertRaises(RuntimeError):
                self.converter.convert(src, output_path=dst)

    def test_convert_file_not_found_before_subprocess(self):
        """FileNotFoundError бросается до запуска subprocess."""
        with patch("subprocess.run") as mock_run:
            with self.assertRaises(FileNotFoundError):
                self.converter.convert("/tmp/krab_ear_no_such_42.wav")
        mock_run.assert_not_called()

    def test_convert_unsupported_format_before_subprocess(self):
        """ValueError для неподдерживаемого формата бросается до subprocess."""
        bad = os.path.join(self.tmp, "file.avi")
        with open(bad, "wb") as fh:
            fh.write(b"dummy")
        with patch("subprocess.run") as mock_run:
            with self.assertRaises(ValueError):
                self.converter.convert(bad)
        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
