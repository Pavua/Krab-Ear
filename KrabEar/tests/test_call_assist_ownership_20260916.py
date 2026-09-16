"""W2: call/stop не останавливает чужую диктовку (ownership bypass)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_assist_service import CallAssistService, VoiceGatewayClient  # noqa: E402
from backend.recording_core_service import RecordingCoreService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


class SharedRecorder:
    """Один рекордер на Core и CallAssist (как продовый shared)."""

    def __init__(self) -> None:
        self.is_recording = False
        self.stop_calls = 0

    def start(self, spill=None) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        self.stop_calls += 1
        if not self.is_recording:
            return None
        self.is_recording = False
        return None

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        import numpy as np
        return np.zeros(32000, dtype=np.float32), 2.0


class FakeStore:
    def __init__(self) -> None:
        self._settings = {
            "voice_gateway_url": "http://127.0.0.1:8090",
            "voice_gateway_api_key": "test-key-42",
            "call_auto_summary": False,
            "call_notify_default": True,
        }

    def load_settings(self, lock_timeout_sec=None, nowait=False):
        return dict(self._settings)


class FakeTranscriber:
    def transcribe(self, audio, **kwargs):
        return {"text": "x", "confidence": 0.9, "engine": "fake"}

    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        return {"text": "x"}


class _GwOk(VoiceGatewayClient):
    def start_session(self, voice_gateway_url, api_key, payload):
        return {"ok": True, "session_id": "gw-w2-001"}

    def stop_session(self, voice_gateway_url, api_key, session_id):
        return {"ok": True}

    def get(self, voice_gateway_url, api_key, path):
        return {"ok": True, "payload": {}}

    def post(self, voice_gateway_url, api_key, path, payload):
        return {"ok": True}

    def delete(self, voice_gateway_url, api_key, path):
        return {"ok": True}


class _FakeSettingsService:
    def __init__(self):
        self._settings = {
            "silence_guard_enabled": False,
            "background_guard_enabled": False,
            "realtime_preview_enabled": False,
            "realtime_partial_enabled": False,
            "realtime_silence_filter_enabled": False,
            "llm_brain_unload_on_recording": False,
            "llm_brain_lease_enabled": False,
        }

    def cached_settings(self):
        return dict(self._settings)

    def invalidate_cache(self):
        pass


class _FakeSemanticSearcher:
    is_enabled = False

    def index_item(self, item_id, text):
        pass


def _make_core(tmp_dir, recorder):
    vocabulary = MagicMock()
    vocabulary.get_words.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None
    return RecordingCoreService(
        recorder=recorder,
        transcriber=FakeTranscriber(),
        translator=MagicMock(),
        store=StateStore(data_dir=tmp_dir),
        vocabulary=vocabulary,
        settings_svc=_FakeSettingsService(),
        llm_rewriter=None,
        auto_glossary=None,
        semantic_searcher=_FakeSemanticSearcher(),
        context_memory=None,
        clipboard_history=[],
        auto_backup=MagicMock(),
        session_tracker=session_tracker,
        action_items_extractor=None,
        transcription_counter_ref=[0],
        last_stt_engine_ref=[None],
        rescue_dir=tmp_dir / "rescue",
    )


def _make_call(store, recorder, core):
    return CallAssistService(
        store=store,
        recorder=recorder,
        transcriber=FakeTranscriber(),
        gateway=_GwOk(),
        recording_core=core,
    )


class CallAssistOwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_ctx.cleanup)
        self.tmp = Path(self._tmp_ctx.name)
        self.recorder = SharedRecorder()
        self.core = _make_core(self.tmp / "data", self.recorder)
        self.addCleanup(self.core.close_background_workers)
        self.store = FakeStore()
        self.call = _make_call(self.store, self.recorder, self.core)

    def test_stop_does_not_kill_foreign_dictation(self) -> None:
        start = self.core.handle_start_recording({})
        self.assertEqual(start["status"], "recording")
        self.assertEqual(self.core.current_recording_owner(), "dictation")
        self.call.handle_start({})
        self.assertTrue(self.call.state["active"])
        stopped = self.call.handle_stop({"auto_summary": False})
        self.assertEqual(stopped["status"], "stopped")
        self.assertTrue(
            self.recorder.is_recording,
            "call/stop остановил чужую диктовку (ownership bypass)",
        )

    def test_stop_stops_call_owned_capture(self) -> None:
        self.call.handle_start({})
        self.assertTrue(self.call.state["active"])
        self.assertTrue(self.recorder.is_recording)
        stopped = self.call.handle_stop({"auto_summary": False})
        self.assertEqual(stopped["status"], "stopped")
        self.assertFalse(self.recorder.is_recording)


if __name__ == "__main__":
    unittest.main()
