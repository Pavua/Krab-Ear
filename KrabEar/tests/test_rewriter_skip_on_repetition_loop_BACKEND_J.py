"""Regression: LLM rewriter must NOT be called when STT output is a repetition loop.

Root cause of KRAB-EAR-BACKEND-J: Whisper produced text with a repeated bigram
(e.g. "числа всегда" × 14), is_likely_repetition_loop() fired correctly, but
_llm_rewrite_allowed() had no guard → LM Studio received garbage → HTTP 400 →
rewriter.timeout Sentry warning every session that hit a loop.

Fix (engine.py): `_is_loop = False` initialised before the detection block;
`if self._llm_rewrite_allowed() and not _is_loop` guards the rewrite call, and
similarly `if self._punctuation_pass_allowed() and not _is_loop` guards the
punctuation pass.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_loop_text() -> str:
    """Return text with a bigram repeated ≥5 times (triggers heuristic 1)."""
    return ("числа всегда " * 10).strip()  # bigram "числа всегда" × 10


def _make_normal_text() -> str:
    return "Сегодня мы обсудили план работы на следующую неделю."


def _make_whisper_result(text: str) -> dict:
    return {
        "text": text,
        "segments": [{"avg_logprob": -0.2}],
        "engine": "fake-whisper",
        "model_used": "fake",
        "language": "ru",
    }


def _make_mock_rewriter():
    mock = MagicMock()
    mock.rewrite.return_value = MagicMock(ok=False, text=None, fallback_reason="skipped", latency_ms=0)
    mock.fix_punctuation_only.return_value = None
    return mock


class RewriterSkipOnRepetitionLoopTest(unittest.TestCase):
    """LLM rewriter is skipped when is_likely_repetition_loop returns True."""

    def _settings_get_with_rewrite(self, key: str, default):
        if key == "llm_rewrite_enabled":
            return True
        if key == "stt_punctuation_llm_pass_enabled":
            return True
        return default

    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_rewriter_not_called_on_repetition_loop(self, mock_fallback, mock_diar):
        """When STT produces a bigram loop, rewrite() must NOT be called."""
        from core.engine import AudioEngine

        mock_fallback.return_value = _make_whisper_result(_make_loop_text())
        mock_diar.return_value = None

        mock_rewriter = _make_mock_rewriter()
        engine = AudioEngine(settings_get=self._settings_get_with_rewrite)
        engine._llm_rewriter = mock_rewriter

        engine.transcribe("fake.wav", is_preview=False)

        mock_rewriter.rewrite.assert_not_called()

    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_punct_pass_not_called_on_repetition_loop(self, mock_fallback, mock_diar):
        """When STT produces a bigram loop, fix_punctuation_only() must NOT be called."""
        from core.engine import AudioEngine

        mock_fallback.return_value = _make_whisper_result(_make_loop_text())
        mock_diar.return_value = None

        mock_rewriter = _make_mock_rewriter()
        engine = AudioEngine(settings_get=self._settings_get_with_rewrite)
        engine._llm_rewriter = mock_rewriter

        engine.transcribe("fake.wav", is_preview=False)

        mock_rewriter.fix_punctuation_only.assert_not_called()

    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_rewriter_called_on_normal_text(self, mock_fallback, mock_diar):
        """When STT output is normal, rewrite() must be called (rewriter enabled)."""
        from core.engine import AudioEngine

        mock_fallback.return_value = _make_whisper_result(_make_normal_text())
        mock_diar.return_value = None

        mock_rewriter = _make_mock_rewriter()
        engine = AudioEngine(settings_get=self._settings_get_with_rewrite)
        engine._llm_rewriter = mock_rewriter

        engine.transcribe("fake.wav", is_preview=False)

        mock_rewriter.rewrite.assert_called_once()


if __name__ == "__main__":
    unittest.main()
