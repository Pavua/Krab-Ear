"""Transcriber diarization guard + error_bus tests (Wave 78 coverage).

Covers:
- _push_diarization_no_token_if_needed logic
- diarize param override when HF_TOKEN missing
- settings dict diarization_enabled flag interaction
- error_bus.push called when token absent + error_bus wired
- error_bus absent → graceful skip (no AttributeError)
- diarize=False explicit → no token check
- diarize=True explicit + token present → allowed through
- settings param None → no token check
- transcribe() passes diarize kwarg to engine
- history_context and stt_hotwords forwarded to engine
- skip_vad_prefilter forwarded to engine
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.transcriber import Transcriber


# ---------------------------------------------------------------------------
# Shared fake engine (records all kwargs)
# ---------------------------------------------------------------------------

class FakeEngine:
    """Minimal AudioEngine mock that records all transcribe() kwargs."""

    def __init__(self, llm_rewriter=None, settings_get=None):
        self.quality_profile = "balanced"
        self._llm_rewriter = llm_rewriter
        self._settings_get = settings_get
        self.calls: list[dict[str, Any]] = []

    def set_quality_profile(self, profile: str) -> bool:
        changed = self.quality_profile != profile.lower()
        self.quality_profile = profile.lower()
        return changed

    def transcribe(self, audio_data: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"audio_data": audio_data, **kwargs})
        return {"text": "ok", "confidence": 0.9, "language": "ru"}


# ---------------------------------------------------------------------------
# 1. _push_diarization_no_token_if_needed unit tests
# ---------------------------------------------------------------------------

class PushDiarizationNoTokenTests(unittest.TestCase):
    """Direct tests of the helper method."""

    def _make_transcriber(self) -> Transcriber:
        return Transcriber(engine=FakeEngine())

    def test_returns_true_when_diarization_disabled(self):
        """Returns True (safe) when diarization_enabled=False."""
        t = self._make_transcriber()
        result = t._push_diarization_no_token_if_needed({"diarization_enabled": False})
        self.assertTrue(result)

    def test_returns_true_when_key_missing(self):
        """Returns True (safe) when diarization_enabled not in settings."""
        t = self._make_transcriber()
        result = t._push_diarization_no_token_if_needed({})
        self.assertTrue(result)

    def test_returns_false_when_diarization_enabled_no_token(self):
        """Returns False when diarization is enabled but HF_TOKEN absent."""
        t = self._make_transcriber()
        env_patch = {"HF_TOKEN": "", "KRAB_EAR_HF_TOKEN": ""}
        with patch.dict(os.environ, env_patch, clear=False):
            # Ensure neither key is set
            os.environ.pop("HF_TOKEN", None)
            os.environ.pop("KRAB_EAR_HF_TOKEN", None)
            result = t._push_diarization_no_token_if_needed({"diarization_enabled": True})
        self.assertFalse(result)

    def test_returns_true_when_hf_token_env_present(self):
        """Returns True when HF_TOKEN env var is set."""
        t = self._make_transcriber()
        with patch.dict(os.environ, {"HF_TOKEN": "hf_abc123"}, clear=False):
            result = t._push_diarization_no_token_if_needed({"diarization_enabled": True})
        self.assertTrue(result)

    def test_returns_true_when_krab_ear_hf_token_env_present(self):
        """Returns True when KRAB_EAR_HF_TOKEN env var is set (no HF_TOKEN)."""
        t = self._make_transcriber()
        patched = {"KRAB_EAR_HF_TOKEN": "hf_xyz789"}
        with patch.dict(os.environ, patched, clear=False):
            os.environ.pop("HF_TOKEN", None)
            result = t._push_diarization_no_token_if_needed({"diarization_enabled": True})
        self.assertTrue(result)

    def test_no_error_bus_no_attribute_error(self):
        """When _error_bus not set, missing token causes no AttributeError."""
        t = self._make_transcriber()
        self.assertFalse(hasattr(t, "_error_bus"))
        # Should not raise
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HF_TOKEN", None)
            os.environ.pop("KRAB_EAR_HF_TOKEN", None)
            result = t._push_diarization_no_token_if_needed({"diarization_enabled": True})
        self.assertFalse(result)

    def test_with_error_bus_calls_push(self):
        """When _error_bus is set and token missing, error_bus.push() is called."""
        t = self._make_transcriber()
        mock_bus = MagicMock()
        t._error_bus = mock_bus

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HF_TOKEN", None)
            os.environ.pop("KRAB_EAR_HF_TOKEN", None)
            # Mock the imports inside the method
            with patch("backend.error_bus.KrabError") as mock_krab_error, \
                 patch("backend.error_codes.ERROR_REGISTRY", {
                     "diarization.no_token": {
                         "severity": "warn",
                         "user_msg_ru": "HF token не задан",
                         "actionable": True,
                         "action_id": "open_settings",
                     }
                 }):
                mock_krab_error.return_value = MagicMock()
                result = t._push_diarization_no_token_if_needed({"diarization_enabled": True})

        self.assertFalse(result)
        mock_bus.push.assert_called_once()

    def test_error_bus_push_exception_swallowed(self):
        """If error_bus.push raises, the method still returns False without crashing."""
        t = self._make_transcriber()
        mock_bus = MagicMock()
        mock_bus.push.side_effect = Exception("bus exploded")
        t._error_bus = mock_bus

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HF_TOKEN", None)
            os.environ.pop("KRAB_EAR_HF_TOKEN", None)
            with patch("backend.error_bus.KrabError", MagicMock()), \
                 patch("backend.error_codes.ERROR_REGISTRY", {
                     "diarization.no_token": {
                         "severity": "warn",
                         "user_msg_ru": "HF token не задан",
                         "actionable": True,
                         "action_id": None,
                     }
                 }):
                result = t._push_diarization_no_token_if_needed({"diarization_enabled": True})

        # Must return False; no exception raised to caller
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# 2. transcribe() diarize override tests
# ---------------------------------------------------------------------------

class DiarizeOverrideTests(unittest.TestCase):
    """Test that transcribe() overrides diarize=False when token missing."""

    def setUp(self):
        self.engine = FakeEngine()
        self.transcriber = Transcriber(engine=self.engine)

    def _clear_hf_tokens(self):
        os.environ.pop("HF_TOKEN", None)
        os.environ.pop("KRAB_EAR_HF_TOKEN", None)

    def test_diarize_forced_false_when_settings_enabled_no_token(self):
        """diarize=None + settings.diarization_enabled=True + no token → engine gets diarize=False."""
        self._clear_hf_tokens()
        self.transcriber.transcribe(
            b"audio",
            settings={"diarization_enabled": True},
            diarize=None,
        )
        call = self.engine.calls[0]
        self.assertFalse(call.get("diarize"))

    def test_diarize_explicit_true_no_token_also_overridden(self):
        """diarize=True explicit + settings.diarization_enabled=True + no token → overridden to False."""
        self._clear_hf_tokens()
        self.transcriber.transcribe(
            b"audio",
            settings={"diarization_enabled": True},
            diarize=True,
        )
        call = self.engine.calls[0]
        self.assertFalse(call.get("diarize"))

    def test_diarize_not_overridden_when_token_present(self):
        """diarize=True + token present → engine receives diarize=True."""
        with patch.dict(os.environ, {"HF_TOKEN": "hf_abc"}, clear=False):
            self.transcriber.transcribe(
                b"audio",
                settings={"diarization_enabled": True},
                diarize=True,
            )
        call = self.engine.calls[0]
        self.assertTrue(call.get("diarize"))

    def test_no_settings_no_token_check(self):
        """settings=None → diarize guard skipped entirely, diarize=True reaches engine."""
        self._clear_hf_tokens()
        self.transcriber.transcribe(b"audio", settings=None, diarize=True)
        call = self.engine.calls[0]
        self.assertTrue(call.get("diarize"))

    def test_settings_diarization_disabled_no_override(self):
        """settings.diarization_enabled=False → no token check, diarize passed through."""
        self._clear_hf_tokens()
        self.transcriber.transcribe(
            b"audio",
            settings={"diarization_enabled": False},
            diarize=True,
        )
        call = self.engine.calls[0]
        self.assertTrue(call.get("diarize"))

    def test_diarize_false_explicit_no_token_check(self):
        """diarize=False explicit → settings.diarization_enabled ignored (intent=False)."""
        self._clear_hf_tokens()
        self.transcriber.transcribe(
            b"audio",
            settings={"diarization_enabled": True},
            diarize=False,
        )
        call = self.engine.calls[0]
        self.assertFalse(call.get("diarize"))


# ---------------------------------------------------------------------------
# 3. Extra kwarg forwarding tests
# ---------------------------------------------------------------------------

class ExtraKwargForwardingTests(unittest.TestCase):
    """Test that history_context, stt_hotwords, skip_vad_prefilter reach engine."""

    def setUp(self):
        self.engine = FakeEngine()
        self.transcriber = Transcriber(engine=self.engine)

    def test_history_context_forwarded(self):
        """history_context is forwarded to engine.transcribe()."""
        ctx = [{"id": "abc", "text": "previous"}]
        self.transcriber.transcribe(b"audio", history_context=ctx)
        call = self.engine.calls[0]
        self.assertEqual(call.get("history_context"), ctx)

    def test_history_context_none_forwarded(self):
        """history_context=None is forwarded as None."""
        self.transcriber.transcribe(b"audio", history_context=None)
        call = self.engine.calls[0]
        self.assertIsNone(call.get("history_context"))

    def test_stt_hotwords_forwarded(self):
        """stt_hotwords list is forwarded to engine.transcribe()."""
        hotwords = ["краб", "ухо", "OpenAI"]
        self.transcriber.transcribe(b"audio", stt_hotwords=hotwords)
        call = self.engine.calls[0]
        self.assertEqual(call.get("stt_hotwords"), hotwords)

    def test_skip_vad_prefilter_forwarded_true(self):
        """skip_vad_prefilter=True is forwarded to engine."""
        self.transcriber.transcribe(b"audio", skip_vad_prefilter=True)
        call = self.engine.calls[0]
        self.assertTrue(call.get("skip_vad_prefilter"))

    def test_skip_vad_prefilter_forwarded_false(self):
        """skip_vad_prefilter=False (default) is forwarded to engine."""
        self.transcriber.transcribe(b"audio")
        call = self.engine.calls[0]
        self.assertFalse(call.get("skip_vad_prefilter"))

    def test_is_preview_always_false_in_transcribe(self):
        """transcribe() always sets is_preview=False in engine call."""
        self.transcriber.transcribe(b"audio")
        call = self.engine.calls[0]
        self.assertFalse(call.get("is_preview"))

    def test_is_preview_always_true_in_transcribe_preview(self):
        """transcribe_preview() always sets is_preview=True in engine call."""
        self.transcriber.transcribe_preview(b"audio")
        call = self.engine.calls[0]
        self.assertTrue(call.get("is_preview"))


# ---------------------------------------------------------------------------
# 4. _error_bus attribute wiring tests
# ---------------------------------------------------------------------------

class ErrorBusWiringTests(unittest.TestCase):
    """Test _error_bus attribute management."""

    def test_error_bus_not_set_by_default(self):
        """By default, _error_bus is not set on a new Transcriber."""
        t = Transcriber(engine=FakeEngine())
        self.assertFalse(hasattr(t, "_error_bus"))

    def test_error_bus_can_be_assigned(self):
        """_error_bus can be injected post-init (late-injection pattern)."""
        t = Transcriber(engine=FakeEngine())
        mock_bus = MagicMock()
        t._error_bus = mock_bus
        self.assertIs(t._error_bus, mock_bus)

    def test_transcribe_works_without_error_bus(self):
        """Full transcribe() workflow works with no _error_bus attribute."""
        engine = FakeEngine()
        t = Transcriber(engine=engine)
        # No _error_bus → should not raise
        result = t.transcribe(b"audio")
        self.assertIn("text", result)


if __name__ == "__main__":
    unittest.main()
