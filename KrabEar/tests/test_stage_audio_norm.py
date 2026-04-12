"""Тесты для AudioNormalizationStage."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.pipeline.context import PipelineContext
from core.pipeline.stages.audio_normalization import AudioNormalizationStage


def _make_ctx(audio_input) -> PipelineContext:
    return PipelineContext(audio_input=audio_input)


class TestAudioNormalizationStageProtocol(unittest.TestCase):
    """Проверяем соответствие PipelineStage-протоколу."""

    def setUp(self):
        self.stage = AudioNormalizationStage()

    def test_name_property(self):
        self.assertEqual(self.stage.name, "audio_normalization")

    def test_should_run_always_true_for_ndarray(self):
        ctx = _make_ctx(np.zeros(100, dtype=np.float32))
        self.assertTrue(self.stage.should_run(ctx))

    def test_should_run_always_true_for_path(self):
        ctx = _make_ctx("/some/file.wav")
        self.assertTrue(self.stage.should_run(ctx))

    def test_should_run_always_true_for_none(self):
        ctx = _make_ctx(None)
        self.assertTrue(self.stage.should_run(ctx))

    def test_implements_pipeline_stage_protocol(self):
        from core.pipeline.base import PipelineStage
        self.assertIsInstance(self.stage, PipelineStage)


class TestArrayNormalization(unittest.TestCase):
    """Нормализация numpy-буфера (live mic)."""

    def setUp(self):
        self.stage = AudioNormalizationStage()

    def test_mono_array_is_normalized(self):
        # Сигнал с RMS = 0.5, должен стать ~0.1
        data = np.ones(1000, dtype=np.float32) * 0.5
        ctx = _make_ctx(data)
        result = self.stage.process(ctx)
        out = result.normalized_audio
        rms = float(np.sqrt(np.mean(out ** 2)))
        self.assertAlmostEqual(rms, 0.1, places=4)

    def test_stereo_array_converted_to_mono(self):
        data = np.ones((500, 2), dtype=np.float32) * 0.3
        ctx = _make_ctx(data)
        result = self.stage.process(ctx)
        self.assertEqual(result.normalized_audio.ndim, 1)

    def test_silent_array_returned_unchanged_shape(self):
        data = np.zeros(256, dtype=np.float32)
        ctx = _make_ctx(data)
        result = self.stage.process(ctx)
        self.assertEqual(result.normalized_audio.shape, data.shape)

    def test_output_clipped_to_minus_one_plus_one(self):
        # Большая амплитуда — после нормализации всё в [-1, 1]
        data = np.full(100, 10.0, dtype=np.float32)
        ctx = _make_ctx(data)
        result = self.stage.process(ctx)
        self.assertLessEqual(float(result.normalized_audio.max()), 1.0)
        self.assertGreaterEqual(float(result.normalized_audio.min()), -1.0)

    def test_process_returns_context(self):
        ctx = _make_ctx(np.ones(64, dtype=np.float32) * 0.2)
        result = self.stage.process(ctx)
        self.assertIsInstance(result, PipelineContext)

    def test_normalized_audio_is_float32(self):
        data = np.ones(200, dtype=np.float64) * 0.4
        ctx = _make_ctx(data)
        result = self.stage.process(ctx)
        self.assertEqual(result.normalized_audio.dtype, np.float32)


class TestFileNormalization(unittest.TestCase):
    """Нормализация аудиофайла."""

    def setUp(self):
        self.stage = AudioNormalizationStage()
        try:
            import soundfile as sf
            self._sf = sf
            self._has_sf = True
        except ImportError:
            self._has_sf = False

    def _write_wav(self, data: np.ndarray, sr: int = 16000) -> str:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        self._sf.write(tmp.name, data, sr)
        return tmp.name

    def test_file_path_produces_normalized_wav(self):
        if not self._has_sf:
            self.skipTest("soundfile не установлен")
        data = np.full(1600, 0.5, dtype=np.float32)
        path = self._write_wav(data)
        try:
            ctx = _make_ctx(path)
            result = self.stage.process(ctx)
            out_path = result.normalized_audio
            self.assertTrue(os.path.exists(out_path))
            out_data, _ = self._sf.read(out_path)
            rms = float(np.sqrt(np.mean(np.array(out_data, dtype=np.float32) ** 2)))
            self.assertAlmostEqual(rms, 0.1, places=2)
        finally:
            os.unlink(path)

    def test_missing_file_returns_original_path(self):
        ctx = _make_ctx("/nonexistent/file_xyz.wav")
        result = self.stage.process(ctx)
        self.assertEqual(result.normalized_audio, "/nonexistent/file_xyz.wav")

    def test_stereo_file_becomes_mono(self):
        if not self._has_sf:
            self.skipTest("soundfile не установлен")
        data = np.full((1600, 2), 0.3, dtype=np.float32)
        path = self._write_wav(data)
        try:
            ctx = _make_ctx(path)
            result = self.stage.process(ctx)
            out_path = result.normalized_audio
            out_data, _ = self._sf.read(out_path)
            self.assertEqual(np.array(out_data).ndim, 1)
        finally:
            os.unlink(path)

    def test_path_object_accepted(self):
        if not self._has_sf:
            self.skipTest("soundfile не установлен")
        from pathlib import Path
        data = np.full(800, 0.2, dtype=np.float32)
        path = self._write_wav(data)
        try:
            ctx = _make_ctx(Path(path))
            result = self.stage.process(ctx)
            self.assertIsNotNone(result.normalized_audio)
        finally:
            os.unlink(path)


class TestUnknownInput(unittest.TestCase):
    """Неизвестный тип audio_input — soft fail."""

    def test_unknown_type_appends_error(self):
        stage = AudioNormalizationStage()
        ctx = _make_ctx(42)  # int — не массив, не путь
        result = stage.process(ctx)
        self.assertTrue(len(result.errors) > 0)
        self.assertIn("audio_normalization", result.errors[0])

    def test_unknown_type_passthrough(self):
        stage = AudioNormalizationStage()
        ctx = _make_ctx({"some": "dict"})
        result = stage.process(ctx)
        self.assertEqual(result.normalized_audio, {"some": "dict"})


if __name__ == "__main__":
    unittest.main()
