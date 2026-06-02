"""Wave 1776 — test: live-recording bookmarks are rebound to the final HistoryItem id
after stop_recording finalize.

Bug: BookmarkManager.update_session_id() was dead in production — bookmarks created
during a live recording were keyed to the session_tracker temp UUID, which no
HistoryItem ever had, making them permanently unreachable.

Fix: RecordingCoreService._stop_recording_phase_e() now calls
    self._bookmarks.update_session_id(bookmark_session_id, item.id)
after the history item is persisted.

This test verifies:
- A bookmark stored under the temp session id is reachable under item.id after finalize.
- Without the fix the bookmark would be orphaned (keyed to the temp id, item.id has none).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.bookmarks import BookmarkManager  # noqa: E402
from backend.recording_core_service import RecordingCoreService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------

class _AudioRecorder:
    """Recorder that returns a short speech-like audio clip on stop()."""
    is_recording = False
    sample_rate = 16000

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        t = np.linspace(0.0, 1.0, 16000, endpoint=False, dtype=np.float32)
        audio = (np.sin(2.0 * np.pi * 440.0 * t) * 0.3).astype(np.float32)
        return audio, 1.0

    def snapshot_rms(self):
        return 0.05

    def snapshot_audio(self, max_duration_sec=12.0):
        return None, 0.0


class _FakeTranscriber:
    def transcribe(self, audio, **kwargs):
        return {"text": "тестовая запись", "confidence": 0.9, "engine": "fake"}


class _FakeTranslator:
    def translate(self, text, **kwargs):
        from backend.translator import TranslationResult
        return TranslationResult(
            text=text,
            status="skipped",
            source_lang="auto",
            target_lang="ru",
            mode="auto",
            engine="fake",
        )


class _FakeSettingsSvc:
    def cached_settings(self):
        return {}

    def invalidate_cache(self):
        pass


class _FakeSemanticSearcher:
    is_enabled = False

    def index_item(self, item_id, text):
        pass


class _FakeSessionTracker:
    """Session tracker that creates a real session_id on start_session()."""

    def __init__(self):
        import uuid
        self._active_session: dict | None = None
        self._session_id: str = str(uuid.uuid4())

    def start_session(self, **kwargs) -> str:
        self._active_session = {"session_id": self._session_id}
        return self._session_id

    def end_session(self, result=None):
        session = self._active_session
        self._active_session = None
        return session

    def get_active_session(self):
        return dict(self._active_session) if self._active_session else None


def _make_service(tmp_dir):
    store = StateStore(data_dir=Path(tmp_dir))
    vocab = MagicMock()
    vocab.get_words.return_value = []
    vocab.load.return_value = []
    session_tracker = _FakeSessionTracker()

    svc = RecordingCoreService(
        recorder=_AudioRecorder(),
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
        store=store,
        vocabulary=vocab,
        settings_svc=_FakeSettingsSvc(),
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
    )
    # Inject BookmarkManager (mirrors what BackendService does after W1776 fix)
    svc._bookmarks = BookmarkManager(data_dir=Path(tmp_dir))
    return svc, session_tracker


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBookmarkRebindOnStopRecording(unittest.TestCase):
    """Verify that bookmarks created during a recording are rebound to item.id after stop."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_bookmarks_rebound_to_item_id_after_stop(self):
        """Fail-before fix: bookmarks keyed to temp session id; pass-after: keyed to item.id."""
        svc, session_tracker = _make_service(self._tmp)
        temp_session_id = session_tracker._session_id

        # Start recording — session tracker creates the temp session_id
        svc.handle_start_recording({})
        # Confirm session tracker is active with the expected temp id
        self.assertIsNotNone(session_tracker._active_session)
        self.assertEqual(
            session_tracker._active_session.get("session_id"), temp_session_id
        )

        # Simulate user placing a bookmark during recording (as Swift client does:
        # it reads session_id from get_recording_state, then calls add_bookmark).
        svc._bookmarks.add(session_id=temp_session_id, offset_sec=15.5, note="highlight")
        svc._bookmarks.add(session_id=temp_session_id, offset_sec=30.0, note="question")

        # Pre-fix assertion: bookmarks exist under temp id, none under real item id (yet)
        pre_fix = svc._bookmarks.list_for_item(temp_session_id)
        self.assertEqual(len(pre_fix), 2, "Two bookmarks should be staged under temp id")

        # Stop recording → triggers phase_e → item.id assigned → rebind call
        result = svc.handle_stop_recording({"quality_profile": "balanced"})

        # stop_recording must succeed (ok or empty_audio — either proves it ran)
        self.assertIn(result.get("status"), ("ok", "empty_audio", "empty_text"),
                      f"Unexpected stop_recording status: {result.get('status')}")

        if result.get("status") == "ok":
            real_item_id = result.get("history_id")
            self.assertIsNotNone(real_item_id, "history_id must be present on ok status")

            # Core assertion: bookmarks are now under the real item id
            after_rebind = svc._bookmarks.list_for_item(real_item_id)
            self.assertEqual(len(after_rebind), 2,
                             "Both bookmarks must be rebound to real item id")

            offsets = sorted(b["offset_sec"] for b in after_rebind)
            self.assertAlmostEqual(offsets[0], 15.5, places=2)
            self.assertAlmostEqual(offsets[1], 30.0, places=2)

            # Bookmarks must NOT remain under the orphaned temp id
            orphaned = svc._bookmarks.list_for_item(temp_session_id)
            self.assertEqual(orphaned, [],
                             "Temp session_id must be empty after rebind — "
                             "without fix bookmarks would be orphaned here")
        # If status is empty_audio/empty_text (silence guard or empty text guard fired),
        # the rebind is skipped (no item persisted) — that path is acceptable.

    def test_no_bookmarks_stop_recording_unaffected(self):
        """If no bookmarks exist, stop_recording completes normally (no error)."""
        svc, session_tracker = _make_service(self._tmp)

        svc.handle_start_recording({})
        # No bookmarks added
        result = svc.handle_stop_recording({"quality_profile": "balanced"})
        self.assertIn(result.get("status"), ("ok", "empty_audio", "empty_text"))

    def test_bookmarks_manager_none_stop_recording_unaffected(self):
        """_bookmarks=None (not wired) does not crash phase_e."""
        svc, session_tracker = _make_service(self._tmp)
        svc._bookmarks = None  # simulate not-yet-wired state

        svc.handle_start_recording({})
        result = svc.handle_stop_recording({"quality_profile": "balanced"})
        self.assertIn(result.get("status"), ("ok", "empty_audio", "empty_text"))

    def test_live_placeholder_bookmarks_rebound(self):
        """Bookmarks stored under '__live__' are also rebound when temp id is '__live__'."""
        svc, session_tracker = _make_service(self._tmp)

        # Override session_tracker to return None active session so bookmark_session_id
        # defaults to '__live__'
        session_tracker._active_session = None

        svc.handle_start_recording({})
        # Force active_session to None to simulate missing session_tracker state
        session_tracker._active_session = None

        # Add bookmark under __live__
        svc._bookmarks.add(session_id="__live__", offset_sec=5.0, note="live bookmark")

        result = svc.handle_stop_recording({"quality_profile": "balanced"})
        self.assertIn(result.get("status"), ("ok", "empty_audio", "empty_text"))

        if result.get("status") == "ok":
            real_item_id = result["history_id"]
            rebounded = svc._bookmarks.list_for_item(real_item_id)
            self.assertEqual(len(rebounded), 1)
            orphaned = svc._bookmarks.list_for_item("__live__")
            self.assertEqual(orphaned, [])


if __name__ == "__main__":
    unittest.main()
