"""Тесты для AudioNormalizationStage."""

from __future__ import annotations
from core.pipeline.stages.audio_normalization import AudioNormalizationStage
from core.pipeline.context import PipelineContext

import os
import sys
import tempfile
import unittest

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


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


class TestAudioNormEdgeCases(unittest.TestCase):
    """Edge cases: very short/long audio, extreme amplitudes."""

    def setUp(self):
        self.stage = AudioNormalizationStage()

    def test_very_short_audio_less_than_100_samples(self):
        # 50 samples at 16kHz ≈ 3ms
        data = np.array([0.1, 0.2, 0.15, 0.05, -0.1, -0.2] * 8 + [0.1], dtype=np.float32)
        self.assertEqual(len(data), 49)
        ctx = _make_ctx(data)
        result = self.stage.process(ctx)
        self.assertIsNotNone(result.normalized_audio)
        self.assertEqual(result.normalized_audio.shape[0], 49)

    def test_very_short_audio_single_sample(self):
        data = np.array([0.5], dtype=np.float32)
        ctx = _make_ctx(data)
        result = self.stage.process(ctx)
        # Should not crash, passthrough or normalize
        self.assertIsNotNone(result.normalized_audio)

    def test_very_long_audio_10_million_samples(self):
        # 10M samples at 16kHz ≈ 10 minutes
        data = np.ones(10_000_000, dtype=np.float32) * 0.3
        ctx = _make_ctx(data)
        result = self.stage.process(ctx)
        self.assertIsNotNone(result.normalized_audio)
        # Verify clipping
        self.assertLessEqual(float(result.normalized_audio.max()), 1.0)
        self.assertGreaterEqual(float(result.normalized_audio.min()), -1.0)

    def test_extreme_amplitude_positive(self):
        # Very large positive values
        data = np.full(1000, 100.0, dtype=np.float32)
        ctx = _make_ctx(data)
        result = self.stage.process(ctx)
        # Should be clipped/normalized
        self.assertLessEqual(float(result.normalized_audio.max()), 1.0)

    def test_extreme_amplitude_negative(self):
        # Very large negative values
        data = np.full(1000, -50.0, dtype=np.float32)
        ctx = _make_ctx(data)
        result = self.stage.process(ctx)
        self.assertGreaterEqual(float(result.normalized_audio.min()), -1.0)

    def test_mixed_extreme_values(self):
        # Mix of very large positive and negative
        data = np.concatenate([
            np.full(500, 1000.0, dtype=np.float32),
            np.full(500, -500.0, dtype=np.float32)
        ])
        ctx = _make_ctx(data)
        result = self.stage.process(ctx)
        self.assertLessEqual(float(result.normalized_audio.max()), 1.0)
        self.assertGreaterEqual(float(result.normalized_audio.min()), -1.0)

    def test_nan_values_handled_gracefully(self):
        data = np.array([0.1, 0.2, np.nan, 0.3, 0.4], dtype=np.float32)
        ctx = _make_ctx(data)
        result = self.stage.process(ctx)
        # Should either skip NaN or add error (implementation-dependent)
        self.assertIsNotNone(result.normalized_audio)

    def test_inf_values_handled_gracefully(self):
        data = np.array([0.1, np.inf, 0.3, -np.inf, 0.5], dtype=np.float32)
        ctx = _make_ctx(data)
        result = self.stage.process(ctx)
        # Should handle infinity gracefully
        self.assertIsNotNone(result.normalized_audio)


class TestNoneAudioInput(unittest.TestCase):
    """None audio_input — stage должна сделать soft-fail, не бросать исключений."""

    def setUp(self):
        self.stage = AudioNormalizationStage()

    def test_none_input_does_not_raise(self):
        ctx = _make_ctx(None)
        try:
            self.stage.process(ctx)
        except Exception as exc:
            self.fail(f"process(None) raised {exc!r}")

    def test_none_input_appends_error(self):
        ctx = _make_ctx(None)
        result = self.stage.process(ctx)
        self.assertTrue(len(result.errors) > 0)
        self.assertIn("audio_normalization", result.errors[0])

    def test_none_input_normalized_audio_passthrough(self):
        ctx = _make_ctx(None)
        result = self.stage.process(ctx)
        self.assertIsNone(result.normalized_audio)

    def test_should_run_returns_true_for_none(self):
        ctx = _make_ctx(None)
        self.assertTrue(self.stage.should_run(ctx))


class TestMetadataPreservation(unittest.TestCase):
    """Проверяем, что стадия не затирает поля контекста, не связанные с нормализацией."""

    def setUp(self):
        self.stage = AudioNormalizationStage()

    def test_raw_text_preserved(self):
        ctx = PipelineContext(audio_input=np.zeros(64, dtype=np.float32))
        ctx.raw_text = "previous text"
        result = self.stage.process(ctx)
        self.assertEqual(result.raw_text, "previous text")

    def test_session_id_preserved(self):
        ctx = PipelineContext(audio_input=np.zeros(64, dtype=np.float32))
        sid = ctx.session_id
        result = self.stage.process(ctx)
        self.assertEqual(result.session_id, sid)

    def test_lang_hint_preserved(self):
        ctx = PipelineContext(
            audio_input=np.zeros(64, dtype=np.float32),
            lang_hint="ru",
        )
        result = self.stage.process(ctx)
        self.assertEqual(result.lang_hint, "ru")

    def test_extra_vocabulary_preserved(self):
        ctx = PipelineContext(
            audio_input=np.zeros(64, dtype=np.float32),
            extra_vocabulary=["краб", "уши"],
        )
        result = self.stage.process(ctx)
        self.assertEqual(result.extra_vocabulary, ["краб", "уши"])

    def test_is_preview_preserved(self):
        ctx = PipelineContext(
            audio_input=np.zeros(64, dtype=np.float32),
            is_preview=True,
        )
        result = self.stage.process(ctx)
        self.assertTrue(result.is_preview)

    def test_cleanup_profile_preserved(self):
        ctx = PipelineContext(
            audio_input=np.zeros(64, dtype=np.float32),
            cleanup_profile="strict",
        )
        result = self.stage.process(ctx)
        self.assertEqual(result.cleanup_profile, "strict")

    def test_existing_errors_preserved(self):
        ctx = PipelineContext(audio_input=np.zeros(64, dtype=np.float32))
        ctx.errors.append("prior_error")
        result = self.stage.process(ctx)
        self.assertIn("prior_error", result.errors)

    def test_confidence_preserved(self):
        ctx = PipelineContext(audio_input=np.zeros(64, dtype=np.float32))
        ctx.confidence = 0.87
        result = self.stage.process(ctx)
        self.assertAlmostEqual(result.confidence, 0.87)

    def test_stage_returns_same_context_object(self):
        ctx = PipelineContext(audio_input=np.zeros(64, dtype=np.float32))
        result = self.stage.process(ctx)
        self.assertIs(result, ctx)


if __name__ == "__main__":
    unittest.main()
