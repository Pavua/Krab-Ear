"""Тесты что AudioEngine.transcribe() передаёт пустой initial_prompt в preview path."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class EnginePreviewPromptTestCase(unittest.TestCase):
    """Preview path должен передавать пустой initial_prompt, non-preview — полный."""

    def _make_fake_whisper_result(self, text: str = "test"):
        return {
            "text": text,
            "segments": [{"avg_logprob": -0.2}],
            "engine": "fake-whisper",
            "model_used": "fake",
            "language": "ru",
        }

    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_preview_passes_empty_prompt(self, mock_fallback, mock_diar):
        """При is_preview=True, _transcribe_with_fallback получает prompt=''."""
        from core.engine import AudioEngine
        mock_fallback.return_value = self._make_fake_whisper_result()
        mock_diar.return_value = None
        engine = AudioEngine()
        engine.transcribe("fake.wav", is_preview=True)
        _, kwargs = mock_fallback.call_args
        self.assertEqual(kwargs.get("prompt", None), "")

    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_non_preview_passes_full_prompt(self, mock_fallback, mock_diar):
        """При is_preview=False, prompt содержит TRANSCRIBE_PROMPT и тематику."""
        from core.engine import AudioEngine
        from core.config import settings
        mock_fallback.return_value = self._make_fake_whisper_result()
        mock_diar.return_value = None
        engine = AudioEngine()
        engine.transcribe("fake.wav", is_preview=False, domain="casual")
        _, kwargs = mock_fallback.call_args
        prompt = kwargs.get("prompt", "")
        self.assertIn(settings.TRANSCRIBE_PROMPT, prompt)
        self.assertIn("Тематика:", prompt)

    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_non_preview_includes_extra_vocabulary(self, mock_fallback, mock_diar):
        """extra_vocabulary попадает в prompt только non-preview."""
        from core.engine import AudioEngine
        mock_fallback.return_value = self._make_fake_whisper_result()
        mock_diar.return_value = None
        engine = AudioEngine()
        engine.transcribe("fake.wav", is_preview=False, extra_vocabulary=["Mercadona"])
        _, kwargs = mock_fallback.call_args
        self.assertIn("Mercadona", kwargs.get("prompt", ""))


if __name__ == "__main__":
    unittest.main()
