"""W1220 regression tests — WhisperX `aligned` unbound variable (W1214 CRITICAL).

Prior to the fix, if `whisperx.load_align_model()` raised an exception inside
the try-block, the except clause swallowed the error and `aligned` was never
assigned.  The diarization block that follows then referenced `aligned` and
raised `NameError: name 'aligned' is not defined`.

Fix: initialize `aligned = None` BEFORE the try-block so the diarization
guard `if word_timestamps is not None and aligned is not None` always sees a
bound name.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import AudioEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine() -> AudioEngine:
    engine = AudioEngine.__new__(AudioEngine)
    engine.quality_profile = "balanced"
    engine.current_model = "mlx-community/whisper-large-v3-turbo"
    engine._unavailable_models = set()
    engine._router = None
    engine._sensevoice_model = None
    engine._sensevoice_load_error = None
    engine._whisperx_model = None
    engine._whisperx_load_error = None
    return engine


def _mock_settings(mock: Any, *, diarization: bool = False) -> None:
    mock.WHISPERX_ENABLED = True
    mock.WHISPERX_MODEL = "large-v3"
    mock.WHISPERX_DEVICE = "cpu"
    mock.WHISPERX_DIARIZATION = diarization
    mock.WHISPERX_WORD_TIMESTAMPS = True
    mock.SENSEVOICE_ENABLED = False
    mock.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
    mock.SENSEVOICE_EMOTION_TO_HISTORY = False
    mock.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
    mock.MODEL_MAX_CANDIDATES = "mlx-community/whisper-large-v3-turbo"
    mock.TRANSCRIBE_TIMEOUT_SEC = 30
    mock.NETWORK_MODE = "offline_strict"
    mock.model_max_list = ["mlx-community/whisper-large-v3-turbo"]
    mock.HF_TOKEN = "fake-hf-token"


_FAKE_SEGMENTS = [{"text": "hello", "start": 0.0, "end": 1.0}]

_FAKE_TRANSCRIBE_RESULT = {
    "language": "en",
    "segments": _FAKE_SEGMENTS,
}


# ---------------------------------------------------------------------------
# 1. align_model load failure does NOT raise NameError (core regression)
# ---------------------------------------------------------------------------

class TestWhisperXAlignLoadFailureDoesNotRaiseNameError(unittest.TestCase):
    """W1214 CRITICAL: NameError must not surface when load_align_model fails."""

    @patch("core.engine.settings")
    @patch("core.engine._whisperx")
    @patch("core.engine._profiler")
    def test_align_load_failure_does_not_raise_name_error(
        self, mock_profiler: Any, mock_wx: Any, mock_settings: Any
    ) -> None:
        """load_align_model failure is swallowed; no NameError on `aligned`."""
        _mock_settings(mock_settings, diarization=False)
        mock_profiler.start_span.return_value.__enter__ = lambda s: s
        mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)

        # Model loads fine
        mock_wx.load_model.return_value = MagicMock()
        mock_wx.load_model.return_value.transcribe.return_value = _FAKE_TRANSCRIBE_RESULT

        # load_align_model RAISES — this is the failure path
        mock_wx.load_align_model.side_effect = RuntimeError("alignment model download failed")

        engine = _make_engine()
        engine._whisperx_model = mock_wx.load_model.return_value

        import numpy as np
        audio = np.zeros(16000, dtype=np.float32)

        # Must NOT raise NameError or any other exception — the except clause
        # in the align block should swallow and continue gracefully.
        try:
            result = engine._transcribe_whisperx(audio, language="en")
        except NameError as exc:
            self.fail(f"NameError raised for `aligned`: {exc}")
        except Exception:
            # Any other exception (e.g. from mocked parts) is acceptable as long
            # as it's not NameError — but actually the function should succeed.
            raise

        # word_timestamps should be None since alignment failed
        self.assertIsNone(result.get("word_timestamps"))
        # Engine returns a text result from the transcript
        self.assertIsInstance(result.get("text"), str)

    @patch("core.engine.settings")
    @patch("core.engine._whisperx")
    @patch("core.engine._profiler")
    def test_align_load_failure_with_diarization_enabled(
        self, mock_profiler: Any, mock_wx: Any, mock_settings: Any
    ) -> None:
        """W1214: diarization path must not hit NameError even when align failed."""
        _mock_settings(mock_settings, diarization=True)
        mock_profiler.start_span.return_value.__enter__ = lambda s: s
        mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)

        mock_model = MagicMock()
        mock_model.transcribe.return_value = _FAKE_TRANSCRIBE_RESULT
        mock_wx.load_model.return_value = mock_model

        # load_align_model RAISES — aligned is never assigned in the try-block
        mock_wx.load_align_model.side_effect = OSError("cannot download phoneme model")

        # Diarization pipeline itself also raises so we don't need real pyannote
        mock_wx.DiarizationPipeline.side_effect = RuntimeError("no hf token")

        engine = _make_engine()
        engine._whisperx_model = mock_model

        import numpy as np
        audio = np.zeros(16000, dtype=np.float32)

        # The critical assertion: no NameError
        try:
            result = engine._transcribe_whisperx(audio, language="en")
        except NameError as exc:
            self.fail(f"NameError raised for `aligned` in diarization path: {exc}")
        except Exception:
            raise

        # Both word_timestamps and speaker_turns should be None
        self.assertIsNone(result.get("word_timestamps"))
        self.assertIsNone(result.get("speaker_turns"))


# ---------------------------------------------------------------------------
# 2. align_success_uses_aligned — happy path still works
# ---------------------------------------------------------------------------

class TestWhisperXAlignSuccessUsesAligned(unittest.TestCase):
    """Happy path: when align succeeds, aligned is used and word_timestamps populated."""

    @patch("core.engine.settings")
    @patch("core.engine._whisperx")
    @patch("core.engine._profiler")
    def test_align_success_uses_aligned(
        self, mock_profiler: Any, mock_wx: Any, mock_settings: Any
    ) -> None:
        """When load_align_model succeeds, word_timestamps are populated."""
        _mock_settings(mock_settings, diarization=False)
        mock_profiler.start_span.return_value.__enter__ = lambda s: s
        mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)

        mock_model = MagicMock()
        mock_model.transcribe.return_value = _FAKE_TRANSCRIBE_RESULT
        mock_wx.load_model.return_value = mock_model

        # Successful alignment returns aligned segments with word timestamps
        mock_align_model = MagicMock()
        mock_align_meta = MagicMock()
        mock_wx.load_align_model.return_value = (mock_align_model, mock_align_meta)

        fake_aligned = {
            "segments": [
                {
                    "words": [
                        {"word": "hello", "start": 0.0, "end": 0.4, "score": 0.95},
                        {"word": "world", "start": 0.5, "end": 0.9, "score": 0.92},
                    ]
                }
            ]
        }
        mock_wx.align.return_value = fake_aligned

        engine = _make_engine()
        engine._whisperx_model = mock_model

        import numpy as np
        audio = np.zeros(16000, dtype=np.float32)

        result = engine._transcribe_whisperx(audio, language="en")

        self.assertIsNotNone(result.get("word_timestamps"))
        self.assertEqual(len(result["word_timestamps"]), 2)
        self.assertEqual(result["word_timestamps"][0]["word"], "hello")
        self.assertAlmostEqual(result["word_timestamps"][0]["confidence"], 0.95)

    @patch("core.engine.settings")
    @patch("core.engine._whisperx")
    @patch("core.engine._profiler")
    def test_align_success_then_diarization_uses_aligned(
        self, mock_profiler: Any, mock_wx: Any, mock_settings: Any
    ) -> None:
        """When align succeeds and diarization is enabled, aligned feeds assign_word_speakers."""
        _mock_settings(mock_settings, diarization=True)
        mock_profiler.start_span.return_value.__enter__ = lambda s: s
        mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)

        mock_model = MagicMock()
        mock_model.transcribe.return_value = _FAKE_TRANSCRIBE_RESULT
        mock_wx.load_model.return_value = mock_model

        mock_align_model = MagicMock()
        mock_align_meta = MagicMock()
        mock_wx.load_align_model.return_value = (mock_align_model, mock_align_meta)

        fake_aligned = {
            "segments": [
                {"words": [{"word": "test", "start": 0.0, "end": 0.3, "score": 0.9}]}
            ]
        }
        mock_wx.align.return_value = fake_aligned

        # Diarization pipeline returns segments with speakers
        mock_diar = MagicMock()
        mock_diar.return_value = MagicMock()
        mock_diar.return_value.itertracks.return_value = []
        mock_wx.DiarizationPipeline.return_value = mock_diar.return_value

        # assign_word_speakers gets called with the aligned result
        mock_wx.assign_word_speakers.return_value = {
            "segments": [
                {
                    "words": [
                        {"word": "test", "start": 0.0, "end": 0.3, "score": 0.9, "speaker": "SPEAKER_00"}
                    ]
                }
            ]
        }

        engine = _make_engine()
        engine._whisperx_model = mock_model

        # Patch _push_error so it doesn't blow up
        engine._push_error = MagicMock()  # type: ignore[method-assign]

        import numpy as np
        audio = np.zeros(16000, dtype=np.float32)

        result = engine._transcribe_whisperx(audio, language="en")

        # assign_word_speakers should have been called with aligned
        mock_wx.assign_word_speakers.assert_called_once()
        call_args = mock_wx.assign_word_speakers.call_args
        # Second argument should be the aligned dict
        self.assertEqual(call_args[0][1], fake_aligned)


# ---------------------------------------------------------------------------
# 3. word_timestamps disabled — aligned is irrelevant, no error
# ---------------------------------------------------------------------------

class TestWhisperXWordTimestampsDisabledSkipsAlign(unittest.TestCase):
    """When WHISPERX_WORD_TIMESTAMPS=False, the align block is skipped entirely."""

    @patch("core.engine.settings")
    @patch("core.engine._whisperx")
    @patch("core.engine._profiler")
    def test_word_timestamps_disabled_skips_align(
        self, mock_profiler: Any, mock_wx: Any, mock_settings: Any
    ) -> None:
        """load_align_model is NOT called when WHISPERX_WORD_TIMESTAMPS=False."""
        _mock_settings(mock_settings, diarization=False)
        mock_settings.WHISPERX_WORD_TIMESTAMPS = False  # override

        mock_profiler.start_span.return_value.__enter__ = lambda s: s
        mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)

        mock_model = MagicMock()
        mock_model.transcribe.return_value = _FAKE_TRANSCRIBE_RESULT
        mock_wx.load_model.return_value = mock_model

        engine = _make_engine()
        engine._whisperx_model = mock_model

        import numpy as np
        audio = np.zeros(16000, dtype=np.float32)

        result = engine._transcribe_whisperx(audio, language="en")

        mock_wx.load_align_model.assert_not_called()
        self.assertIsNone(result.get("word_timestamps"))

    @patch("core.engine.settings")
    @patch("core.engine._whisperx")
    @patch("core.engine._profiler")
    def test_word_timestamps_disabled_with_diarization_still_works(
        self, mock_profiler: Any, mock_wx: Any, mock_settings: Any
    ) -> None:
        """Diarization without word timestamps: no aligned needed, no NameError."""
        _mock_settings(mock_settings, diarization=True)
        mock_settings.WHISPERX_WORD_TIMESTAMPS = False  # override
        mock_settings.HF_TOKEN = "tok"

        mock_profiler.start_span.return_value.__enter__ = lambda s: s
        mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)

        mock_model = MagicMock()
        mock_model.transcribe.return_value = _FAKE_TRANSCRIBE_RESULT
        mock_wx.load_model.return_value = mock_model

        # Diarization raises so we don't need real pyannote
        mock_wx.DiarizationPipeline.side_effect = RuntimeError("pyannote not installed")

        engine = _make_engine()
        engine._whisperx_model = mock_model
        engine._push_error = MagicMock()  # type: ignore[method-assign]

        import numpy as np
        audio = np.zeros(16000, dtype=np.float32)

        # Must not raise NameError
        try:
            result = engine._transcribe_whisperx(audio, language="en")
        except NameError as exc:
            self.fail(f"NameError raised: {exc}")
        except Exception:
            raise

        self.assertIsNone(result.get("word_timestamps"))
        self.assertIsNone(result.get("speaker_turns"))


if __name__ == "__main__":
    unittest.main()
