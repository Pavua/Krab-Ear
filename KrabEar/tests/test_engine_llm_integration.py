"""Integration tests для AudioEngine LLM rewrite hook."""

import unittest
from unittest.mock import MagicMock, patch

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class AudioEngineLLMHookTestCase(unittest.TestCase):
    """Тесты что engine.transcribe() правильно вызывает llm_rewriter при runtime toggle=true."""

    def _make_fake_whisper_result(self, text: str):
        return {
            "text": text,
            "segments": [{"avg_logprob": -0.2}],
            "engine": "fake-whisper",
            "model_used": "fake",
            "language": "ru",
        }

    def _make_engine_with_rewriter(self, rewriter, settings_get):
        from core.engine import AudioEngine
        engine = AudioEngine()
        engine._llm_rewriter = rewriter
        engine._settings_get = settings_get
        return engine

    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    def test_transcribe_without_rewriter_returns_cleaned_text(self, mock_diar, mock_fallback):
        """llm_rewriter=None → text == cleanup output, llm_applied=False."""
        from core.engine import AudioEngine
        mock_fallback.return_value = self._make_fake_whisper_result("привет мир")
        mock_diar.return_value = None
        engine = AudioEngine()
        result = engine.transcribe(audio_data="fake.wav")
        self.assertEqual(result["raw_text"], "привет мир")
        self.assertIn("cleaned_text", result)
        self.assertFalse(result["llm_applied"])
        self.assertIsNone(result["llm_latency_ms"])

    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    def test_transcribe_with_rewriter_uses_llm_output(self, mock_diar, mock_fallback):
        """Мокнутый rewriter ok=True → engine.transcribe() text = rewriter.text."""
        from backend.llm_rewriter import LLMRewriteResult
        mock_fallback.return_value = self._make_fake_whisper_result("привет мир")
        mock_diar.return_value = None

        fake_rewriter = MagicMock()
        fake_rewriter.rewrite.return_value = LLMRewriteResult(
            ok=True, text="Привет, мир.", fallback_reason=None, latency_ms=1500
        )
        engine = self._make_engine_with_rewriter(
            fake_rewriter,
            lambda k, d: True if k == "llm_rewrite_enabled" else d,
        )

        result = engine.transcribe(audio_data="fake.wav")
        self.assertEqual(result["text"], "Привет, мир.")
        self.assertTrue(result["llm_applied"])
        self.assertEqual(result["llm_latency_ms"], 1500)
        self.assertIsNone(result["llm_fallback_reason"])
        fake_rewriter.rewrite.assert_called_once()

    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    def test_runtime_toggle_false_skips_rewriter(self, mock_diar, mock_fallback):
        """settings_get('llm_rewrite_enabled')=False → rewriter НЕ вызван."""
        mock_fallback.return_value = self._make_fake_whisper_result("тест")
        mock_diar.return_value = None

        fake_rewriter = MagicMock()
        engine = self._make_engine_with_rewriter(
            fake_rewriter,
            lambda k, d: False if k == "llm_rewrite_enabled" else d,
        )

        result = engine.transcribe(audio_data="fake.wav")
        fake_rewriter.rewrite.assert_not_called()
        self.assertFalse(result["llm_applied"])

    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    def test_llm_failure_falls_back_to_cleaned_text(self, mock_diar, mock_fallback):
        """rewriter ok=False → text = cleaned_text, llm_applied=False, fallback_reason set."""
        from backend.llm_rewriter import LLMRewriteResult
        mock_fallback.return_value = self._make_fake_whisper_result("привет мир")
        mock_diar.return_value = None

        fake_rewriter = MagicMock()
        fake_rewriter.rewrite.return_value = LLMRewriteResult(
            ok=False, text=None, fallback_reason="timeout", latency_ms=None
        )
        engine = self._make_engine_with_rewriter(
            fake_rewriter,
            lambda k, d: True if k == "llm_rewrite_enabled" else d,
        )

        result = engine.transcribe(audio_data="fake.wav")
        self.assertEqual(result["text"], result["cleaned_text"])
        self.assertFalse(result["llm_applied"])
        self.assertEqual(result["llm_fallback_reason"], "timeout")

    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    def test_transcribe_returns_all_three_text_versions(self, mock_diar, mock_fallback):
        """Dict содержит raw_text, cleaned_text, text — все три версии."""
        from backend.llm_rewriter import LLMRewriteResult
        mock_fallback.return_value = self._make_fake_whisper_result("raw text here")
        mock_diar.return_value = None

        fake_rewriter = MagicMock()
        fake_rewriter.rewrite.return_value = LLMRewriteResult(
            ok=True, text="FINAL", fallback_reason=None, latency_ms=100
        )
        engine = self._make_engine_with_rewriter(
            fake_rewriter,
            lambda k, d: True if isinstance(d, bool) else d,
        )

        result = engine.transcribe(audio_data="fake.wav")
        self.assertIn("raw_text", result)
        self.assertIn("cleaned_text", result)
        self.assertIn("text", result)
        self.assertEqual(result["raw_text"], "raw text here")
        self.assertEqual(result["text"], "FINAL")


if __name__ == "__main__":
    unittest.main()
