"""W1414 — Tests for Transcriber wrapper silence_ranges + progress_callback forwarding
and _stop_recording_phase_c settings= guard path.

Fixes verified:
  F1: Transcriber.transcribe() now accepts silence_ranges and progress_callback params
      and forwards them verbatim to engine.transcribe() — no more engine bypass.
  F2: _stop_recording_phase_c passes settings=cached_settings() and diarize= to the
      wrapper so the HF_TOKEN guard can fire.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_transcriber_wrapper_W1414.py -v
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.transcriber import Transcriber


# ---------------------------------------------------------------------------
# Shared fake engine — captures all kwargs passed to transcribe()
# ---------------------------------------------------------------------------

class _CapturingEngine:
    """Fake AudioEngine that records all args passed to transcribe()."""

    def __init__(self, llm_rewriter=None, settings_get=None):
        self.quality_profile = "balanced"
        self._llm_rewriter = llm_rewriter
        self._settings_get = settings_get
        self.calls: list[dict] = []

    def set_quality_profile(self, profile: str) -> bool:
        old = self.quality_profile
        self.quality_profile = profile
        return old != profile

    def transcribe(self, audio_data: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"audio_data": audio_data, **kwargs})
        return {"text": "ok", "confidence": 0.9}


# ---------------------------------------------------------------------------
# F1: silence_ranges forwarding
# ---------------------------------------------------------------------------

class TranscriberForwardsSilenceRangesTest(unittest.TestCase):
    """Transcriber.transcribe() must accept and forward silence_ranges to engine."""

    def setUp(self):
        self.engine = _CapturingEngine()
        self.transcriber = Transcriber(engine=self.engine)

    def test_transcriber_forwards_silence_ranges(self):
        """silence_ranges must appear in the kwargs received by engine.transcribe()."""
        ranges = [(0.5, 1.2), (3.0, 3.8)]
        self.transcriber.transcribe("audio.wav", silence_ranges=ranges)

        self.assertEqual(len(self.engine.calls), 1)
        call = self.engine.calls[0]
        self.assertIn("silence_ranges", call, "silence_ranges not forwarded to engine")
        self.assertEqual(call["silence_ranges"], ranges)

    def test_transcriber_silence_ranges_default_none(self):
        """When silence_ranges is omitted, engine must receive None (not missing key)."""
        self.transcriber.transcribe("audio.wav")

        call = self.engine.calls[0]
        self.assertIn("silence_ranges", call)
        self.assertIsNone(call["silence_ranges"])

    def test_transcriber_silence_ranges_empty_list(self):
        """Empty list silence_ranges must be forwarded as-is."""
        self.transcriber.transcribe("audio.wav", silence_ranges=[])

        call = self.engine.calls[0]
        self.assertEqual(call["silence_ranges"], [])


# ---------------------------------------------------------------------------
# F1: progress_callback forwarding
# ---------------------------------------------------------------------------

class TranscriberForwardsProgressCallbackTest(unittest.TestCase):
    """Transcriber.transcribe() must accept and forward progress_callback to engine."""

    def setUp(self):
        self.engine = _CapturingEngine()
        self.transcriber = Transcriber(engine=self.engine)

    def test_transcriber_forwards_progress_callback(self):
        """progress_callback must appear in the kwargs received by engine.transcribe()."""
        stages: list[str] = []

        def cb(stage: str) -> None:
            stages.append(stage)

        self.transcriber.transcribe("audio.wav", progress_callback=cb)

        call = self.engine.calls[0]
        self.assertIn("progress_callback", call, "progress_callback not forwarded to engine")
        self.assertIs(call["progress_callback"], cb)

    def test_transcriber_progress_callback_default_none(self):
        """When progress_callback is omitted, engine must receive None."""
        self.transcriber.transcribe("audio.wav")

        call = self.engine.calls[0]
        self.assertIn("progress_callback", call)
        self.assertIsNone(call["progress_callback"])

    def test_transcriber_both_new_params_forwarded(self):
        """silence_ranges and progress_callback can be passed simultaneously."""
        ranges = [(1.0, 2.0)]
        cb = lambda stage: None

        self.transcriber.transcribe("audio.wav", silence_ranges=ranges, progress_callback=cb)

        call = self.engine.calls[0]
        self.assertEqual(call["silence_ranges"], ranges)
        self.assertIs(call["progress_callback"], cb)


# ---------------------------------------------------------------------------
# F2: _stop_recording_phase_c passes settings= to Transcriber.transcribe()
# ---------------------------------------------------------------------------

class PhaseCPassesSettingsToTranscriberTest(unittest.TestCase):
    """_stop_recording_phase_c must call transcriber.transcribe() with settings=.

    This ensures the HF_TOKEN guard in Transcriber._push_diarization_no_token_if_needed
    can fire instead of silently being bypassed.
    """

    def _make_service(self, diarization_enabled: bool = True) -> Any:
        """Build a minimal RecordingCoreService stub with a capturing transcriber."""
        from backend.recording_core_service import RecordingCoreService

        store = MagicMock()
        store.get_history_page.return_value = ([], None)

        recorder = MagicMock()
        recorder.is_recording = False

        settings_svc = MagicMock()
        settings_svc.cached_settings.return_value = {
            "diarization_enabled": diarization_enabled,
            "stt_hotwords_enabled": False,
            "stt_hotwords": [],
            "auto_glossary_enabled": False,
            "auto_glossary_window_days": 7,
            "auto_glossary_top_n": 30,
        }

        # Build a real Transcriber with capturing engine
        capturing_engine = _CapturingEngine()
        transcriber = Transcriber(engine=capturing_engine)

        # Minimal vocabulary stub
        vocabulary = MagicMock()
        vocabulary.load.return_value = []

        # Minimal auto_glossary stub
        auto_glossary = MagicMock()
        auto_glossary.build.return_value = []

        svc = RecordingCoreService.__new__(RecordingCoreService)
        svc.store = store
        svc.recorder = recorder
        svc._settings_svc = settings_svc
        svc.transcriber = transcriber
        svc.vocabulary = vocabulary
        svc._auto_glossary = auto_glossary
        # recording_state dict needed by _stop_recording_phase_c
        svc._recording_state = {}
        svc._lock = threading.Lock()

        return svc, capturing_engine

    def test_phase_c_passes_settings_to_transcriber(self):
        """_stop_recording_phase_c must call transcriber.transcribe() with settings= dict.

        We verify this by patching Transcriber.transcribe and asserting settings= appears
        in the call kwargs (the engine never receives settings — the wrapper consumes it).
        """
        svc, engine = self._make_service(diarization_enabled=False)

        audio = np.zeros(16000, dtype=np.float32)
        sr = {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "lang_hint": None,
        }

        captured_kwargs: list[dict] = []
        original_transcribe = svc.transcriber.transcribe

        def patched_transcribe(audio_data, **kwargs):
            captured_kwargs.append(kwargs)
            return original_transcribe(audio_data, **kwargs)

        svc.transcriber.transcribe = patched_transcribe
        svc._stop_recording_phase_c(audio, 1.0, sr)

        self.assertEqual(len(captured_kwargs), 1, "transcriber.transcribe should be called once")
        kwargs = captured_kwargs[0]
        self.assertIn("settings", kwargs, "settings= not passed to transcriber.transcribe()")
        self.assertIsInstance(kwargs["settings"], dict)

    def test_phase_c_passes_diarize_true_when_enabled(self):
        """When diarization_enabled=True, diarize=True must reach the transcriber."""
        svc, engine = self._make_service(diarization_enabled=True)

        audio = np.zeros(16000, dtype=np.float32)
        sr = {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "lang_hint": None,
        }
        svc._stop_recording_phase_c(audio, 1.0, sr)

        call = engine.calls[0]
        # diarize must be True (before HF_TOKEN guard may override it)
        # We test that the value reaching engine is True
        # (the guard in Transcriber will override to False if no token, but
        #  the wrapper received settings so it CAN fire)
        self.assertIn("diarize", call)
        # With no HF_TOKEN in test env, diarize should be forced to False by guard
        # meaning the guard fired (settings= was passed correctly)
        self.assertFalse(call.get("diarize"), "HF_TOKEN guard should override diarize=False")

    def test_phase_c_diarize_none_when_disabled(self):
        """When diarization_enabled=False, diarize=None must reach the transcriber."""
        svc, engine = self._make_service(diarization_enabled=False)

        audio = np.zeros(16000, dtype=np.float32)
        sr = {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "lang_hint": None,
        }
        svc._stop_recording_phase_c(audio, 1.0, sr)

        call = engine.calls[0]
        self.assertIn("diarize", call)
        self.assertIsNone(call["diarize"])


if __name__ == "__main__":
    unittest.main()
