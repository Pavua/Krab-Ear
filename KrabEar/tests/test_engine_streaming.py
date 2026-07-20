"""Тесты streaming chunked transcription (transcribe_chunked + auto-routing)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import AudioEngine  # noqa: E402


def _make_audio(seconds: float, sr: int = 16000) -> np.ndarray:
    """Синусоидальный буфер заданной длительности (16 kHz, float32)."""
    t = np.linspace(0, seconds, int(seconds * sr), dtype=np.float32)
    return np.sin(2 * np.pi * 440 * t).astype(np.float32)


def _fake_fallback(text: str = "hello world"):
    """Возвращает фабрику fake _transcribe_with_fallback."""
    def _inner(audio_data, prompt, language=None):
        words = text.split()
        segments = [{"avg_logprob": -0.3, "text": w} for w in words]
        return {
            "text": text,
            "segments": segments,
            "engine": "mlx-whisper",
            "model_used": "test-model",
            "language": "ru",
        }
    return _inner


class TestTranscribeChunkedBasic(unittest.TestCase):
    """Базовые проверки transcribe_chunked."""

    def setUp(self):
        with patch("core.engine.mlx_whisper", None):
            self.engine = AudioEngine.__new__(AudioEngine)
            self.engine.current_model = "test-model"
            self.engine.quality_profile = "balanced"
            self.engine._unavailable_models = {}
            self.engine._last_llm_diff = None
            self.engine._llm_rewriter = None
            self.engine._confidence_calibrator = MagicMock()
            self.engine._confidence_calibrator.calibrate_detailed.return_value = MagicMock(
                calibrated=0.8, adjustments=[]
            )

    def test_single_pass_short_audio(self):
        """Короткое аудио (10 с) → streaming НЕ активируется (settings.STT_STREAMING_ENABLED=False)."""
        audio = _make_audio(10.0)
        with patch("core.config.settings.STT_STREAMING_ENABLED", False):
            with patch.object(
                self.engine, "_transcribe_with_fallback", side_effect=_fake_fallback("тест текст")
            ) as mock_fb:
                with patch.object(self.engine, "_maybe_run_diarization", return_value=None):
                    with patch.object(self.engine, "_llm_rewrite_allowed", return_value=False):
                        with patch("core.engine.settings") as mock_settings:
                            mock_settings.STT_STREAMING_ENABLED = False
                            mock_settings.TRANSCRIBE_LANGUAGE = "ru"
                            mock_settings.TRANSCRIBE_PROMPT = "prompt"
                            mock_settings.DIARIZATION_ENABLED = False
                            mock_settings.LLM_ENABLED = False
                            mock_settings.SENSEVOICE_EMOTION_TO_HISTORY = False
                            mock_settings.MAX_AUDIO_MB = 1000
                            mock_settings.SMART_SILENCE_SKIP_ENABLED = False
                            mock_settings.PIPELINE_V2 = False
                            mock_settings.PIPELINE_V2_ENABLED = False  # W1707: explicit False (MagicMock attr is truthy)
                            result = self.engine.transcribe(audio)
                # fallback должен вызываться ровно 1 раз (single-pass)
                self.assertEqual(mock_fb.call_count, 1)
                self.assertIn("text", result)

    def test_chunked_splits_long_audio(self):
        """60-секундное аудио → 4 чанка (chunk=15, overlap=2, step=13)."""
        audio = _make_audio(60.0)
        call_count = [0]

        def counting_fallback(audio_data, prompt, language=None):
            call_count[0] += 1
            return {
                "text": f"chunk text {call_count[0]}",
                "segments": [{"avg_logprob": -0.3}],
                "engine": "mlx-whisper",
                "model_used": "test-model",
                "language": "ru",
            }

        with patch.object(self.engine, "_transcribe_with_fallback", side_effect=counting_fallback):
            result = self.engine.transcribe_chunked(
                audio,
                sample_rate=16000,
                chunk_sec=15.0,
                overlap_sec=2.0,
            )

        self.assertIn("chunks", result)
        # 60s аудио / step 13s ≈ 5 чанков (включая последний хвост)
        # (start=0,13,26,39,52 → 5 чанков)
        self.assertGreaterEqual(len(result["chunks"]), 4)
        self.assertGreaterEqual(call_count[0], 4)
        self.assertIn("text", result)
        self.assertTrue(result["text"])

    def test_chunked_forwards_real_source_sample_rate(self):
        """Чанки 48 кГц передают effective_sr в GigaAM fallback-chain."""
        audio = _make_audio(2.0, sr=48_000)
        observed_rates: list[int | None] = []

        def capture_rate(audio_data, prompt, language=None, audio_sample_rate=None):
            observed_rates.append(audio_sample_rate)
            return {
                "text": "часть",
                "segments": [{"avg_logprob": -0.3}],
                "engine": "gigaam-rnnt",
                "model_used": "test-model",
                "language": "ru",
            }

        with patch.object(
            self.engine, "_transcribe_with_fallback", side_effect=capture_rate,
        ):
            self.engine.transcribe_chunked(
                audio,
                sample_rate=48_000,
                chunk_sec=1.0,
                overlap_sec=0.0,
            )

        self.assertTrue(observed_rates)
        self.assertEqual(observed_rates, [48_000] * len(observed_rates))

    def test_seam_stitching_removes_duplicates(self):
        """Дублирующиеся слова на шве удаляются через LCS."""
        # chunk_prev заканчивается на "hello world", chunk_next начинается с "world foo bar"
        result = AudioEngine._stitch_overlap("hello world", "world foo bar", overlap_words=3)
        # "world" — дубль на шве; ожидаем "hello world foo bar"
        self.assertIn("hello", result)
        self.assertIn("foo", result)
        self.assertIn("bar", result)
        # "world" должен присутствовать ровно один раз
        self.assertEqual(result.lower().count("world"), 1)

    def test_all_chunks_failed_graceful(self):
        """Если все чанки упали — возвращаем пустой text без исключения."""
        audio = _make_audio(40.0)

        def always_fail(audio_data, prompt, language=None):
            raise RuntimeError("STT unavailable")

        with patch.object(self.engine, "_transcribe_with_fallback", side_effect=always_fail):
            result = self.engine.transcribe_chunked(
                audio,
                sample_rate=16000,
                chunk_sec=15.0,
                overlap_sec=2.0,
            )

        self.assertEqual(result["text"], "")
        self.assertIn("chunks", result)
        for chunk in result["chunks"]:
            self.assertFalse(chunk["ok"])

    def test_partial_chunks_succeed(self):
        """Если часть чанков упала — результат содержит текст успешных чанков."""
        audio = _make_audio(40.0)
        call_count = [0]

        def partial_fail(audio_data, prompt, language=None):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("chunk 2 failed")
            return {
                "text": "успешный текст",
                "segments": [{"avg_logprob": -0.3}],
                "engine": "mlx-whisper",
                "model_used": "test-model",
                "language": "ru",
            }

        with patch.object(self.engine, "_transcribe_with_fallback", side_effect=partial_fail):
            result = self.engine.transcribe_chunked(
                audio,
                sample_rate=16000,
                chunk_sec=15.0,
                overlap_sec=2.0,
            )

        self.assertIn("успешный", result["text"])
        ok = sum(1 for c in result["chunks"] if c["ok"])
        fail = sum(1 for c in result["chunks"] if not c["ok"])
        self.assertGreater(ok, 0)
        self.assertGreater(fail, 0)

    def test_chunk_timestamps_present(self):
        """Каждый чанк содержит start_sec и end_sec."""
        audio = _make_audio(35.0)

        with patch.object(self.engine, "_transcribe_with_fallback", side_effect=_fake_fallback("текст")):
            result = self.engine.transcribe_chunked(
                audio,
                sample_rate=16000,
                chunk_sec=15.0,
                overlap_sec=2.0,
            )

        for chunk in result["chunks"]:
            self.assertIn("start_sec", chunk)
            self.assertIn("end_sec", chunk)
            self.assertGreaterEqual(chunk["end_sec"], chunk["start_sec"])

    def test_result_fields_present(self):
        """Результат содержит все обязательные поля."""
        audio = _make_audio(35.0)
        required_fields = [
            "text", "chunks", "confidence", "duration_ms",
            "engine", "model", "language", "segments",
        ]

        with patch.object(self.engine, "_transcribe_with_fallback", side_effect=_fake_fallback("текст")):
            result = self.engine.transcribe_chunked(audio, sample_rate=16000)

        for field in required_fields:
            self.assertIn(field, result, f"Поле '{field}' отсутствует в результате")

    def test_confidence_averaged_across_chunks(self):
        """Уверенность результата — среднее по успешным чанкам."""
        audio = _make_audio(35.0)

        def fallback_with_conf(audio_data, prompt, language=None):
            return {
                "text": "текст",
                "segments": [{"avg_logprob": -0.5}],  # exp(-0.5) ≈ 0.606
                "engine": "mlx-whisper",
                "model_used": "test-model",
                "language": "ru",
            }

        with patch.object(self.engine, "_transcribe_with_fallback", side_effect=fallback_with_conf):
            result = self.engine.transcribe_chunked(audio, sample_rate=16000)

        self.assertGreater(result["confidence"], 0.0)
        self.assertLessEqual(result["confidence"], 1.0)

    def test_lcs_empty_inputs(self):
        """LCS с пустыми входами возвращает 0."""
        self.assertEqual(AudioEngine._lcs_length([], []), 0)
        self.assertEqual(AudioEngine._lcs_length(["a"], []), 0)
        self.assertEqual(AudioEngine._lcs_length([], ["b"]), 0)

    def test_stitch_empty_prev(self):
        """stitch с пустым prev → возвращает next без изменений."""
        result = AudioEngine._stitch_overlap("", "foo bar", overlap_words=2)
        self.assertEqual(result, "foo bar")

    def test_stitch_empty_next(self):
        """stitch с пустым next → возвращает prev без изменений."""
        result = AudioEngine._stitch_overlap("foo bar", "", overlap_words=2)
        self.assertEqual(result, "foo bar")


if __name__ == "__main__":
    unittest.main()
