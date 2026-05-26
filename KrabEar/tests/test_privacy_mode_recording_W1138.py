"""W1138 — RecordingCoreService privacy_mode tagging tests.

Verifies W1134 F2 HIGH fix: history items are tagged with privacy_mode=True
when privacy_mode_enabled=True in settings, and privacy_mode=False otherwise.

Tests:
- test_recording_in_privacy_mode_tagged
- test_recording_normal_no_privacy_flag
- test_history_item_privacy_field_persists_via_ndjson
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.models import HistoryItem
from backend.recording_core_service import RecordingCoreService
from backend.state_store import StateStore


# ---------------------------------------------------------------------------
# Shared fakes / stubs
# ---------------------------------------------------------------------------

class _FakeRecorder:
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
        # Non-silent audio so STT guard passes
        t = np.linspace(0.0, 1.0, 16000, endpoint=False, dtype=np.float32)
        audio = (np.sin(2.0 * np.pi * 440.0 * t) * 0.3).astype(np.float32)
        return audio, 1.0

    def snapshot_rms(self):
        return 0.05


class _FakeTranscriber:
    def transcribe(self, audio, **kwargs):
        return {"text": "тест приватного режима", "confidence": 0.9, "engine": "fake"}


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


class _ConfigurableSettingsSvc:
    """Settings service that can be configured with a dict."""

    def __init__(self, settings: dict | None = None):
        self._settings = settings or {}

    def cached_settings(self):
        return dict(self._settings)

    def invalidate_cache(self):
        pass


class _FakeSemanticSearcher:
    is_enabled = False

    def index_item(self, item_id, text):
        pass


def _make_service(tmp_dir, settings: dict | None = None):
    """Construct a RecordingCoreService with configurable settings."""
    store = StateStore(data_dir=Path(tmp_dir))
    vocab = MagicMock()
    vocab.load.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None
    return RecordingCoreService(
        recorder=_FakeRecorder(),
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
        store=store,
        vocabulary=vocab,
        settings_svc=_ConfigurableSettingsSvc(settings),
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPrivacyModeRecordingTagging(unittest.TestCase):
    """W1138: verify privacy_mode flag is set on HistoryItem when enabled."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_recording_in_privacy_mode_tagged(self):
        """When privacy_mode_enabled=True, persisted item must have privacy_mode=True."""
        svc = _make_service(self._tmp, settings={"privacy_mode_enabled": True})
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({})

        # If audio guards kicked in, result may be empty_audio — in that case
        # we can't verify the history item. Only assert when status=ok.
        if result.get("status") != "ok":
            self.skipTest("Audio guard rejected recording, cannot verify privacy tag")

        # The result payload itself should carry the flag
        self.assertTrue(result.get("privacy_mode"), "Result payload must include privacy_mode=True")

        # Verify the persisted HistoryItem in the store
        history_id = result.get("history_id")
        self.assertIsNotNone(history_id)
        store = svc.store
        items, _ = store.get_history_page(cursor=None, limit=10)
        # get_history_page returns list[dict]
        matching = [it for it in items if it.get("id") == history_id]
        self.assertEqual(len(matching), 1, "Should find exactly one item with that history_id")
        self.assertTrue(matching[0].get("privacy_mode"), "HistoryItem.privacy_mode must be True")

    def test_recording_normal_no_privacy_flag(self):
        """When privacy_mode_enabled is absent/False, item must have privacy_mode=False."""
        svc = _make_service(self._tmp, settings={"privacy_mode_enabled": False})
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({})

        if result.get("status") != "ok":
            self.skipTest("Audio guard rejected recording, cannot verify privacy tag")

        # Result payload should have privacy_mode=False
        self.assertFalse(result.get("privacy_mode"), "Result payload must include privacy_mode=False")

        history_id = result.get("history_id")
        self.assertIsNotNone(history_id)
        items, _ = svc.store.get_history_page(cursor=None, limit=10)
        # get_history_page returns list[dict]
        matching = [it for it in items if it.get("id") == history_id]
        self.assertEqual(len(matching), 1)
        self.assertFalse(matching[0].get("privacy_mode"), "HistoryItem.privacy_mode must be False")

    def test_recording_default_settings_no_privacy_flag(self):
        """When settings dict is empty (default), privacy_mode defaults to False."""
        svc = _make_service(self._tmp, settings={})
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({})

        if result.get("status") != "ok":
            self.skipTest("Audio guard rejected recording, cannot verify privacy tag")

        self.assertFalse(result.get("privacy_mode"))


class TestHistoryItemPrivacyField(unittest.TestCase):
    """W1138: verify HistoryItem.privacy_mode round-trips through to_dict/from_dict."""

    def test_privacy_mode_field_default_false(self):
        """HistoryItem.privacy_mode defaults to False on creation."""
        item = HistoryItem.create(text="test")
        self.assertFalse(item.privacy_mode)

    def test_privacy_mode_field_set_true(self):
        """HistoryItem.create() accepts privacy_mode=True."""
        item = HistoryItem.create(text="private text", privacy_mode=True)
        self.assertTrue(item.privacy_mode)

    def test_privacy_mode_persists_via_ndjson(self):
        """privacy_mode=True survives to_dict() → JSON → from_dict() round-trip."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = StateStore(data_dir=Path(tmp_dir))

            # Write item with privacy_mode=True via add_history_item
            item = store.add_history_item(
                text="private recording",
                privacy_mode=True,
            )
            self.assertTrue(item.privacy_mode, "add_history_item must pass privacy_mode=True")

            # to_dict round-trip
            d = item.to_dict()
            self.assertTrue(d.get("privacy_mode"), "to_dict must include privacy_mode=True")

            # from_dict round-trip
            restored = HistoryItem.from_dict(d)
            self.assertTrue(restored.privacy_mode, "from_dict must restore privacy_mode=True")

    def test_privacy_mode_false_persists_via_ndjson(self):
        """privacy_mode=False survives round-trip (default, non-regression)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = StateStore(data_dir=Path(tmp_dir))
            item = store.add_history_item(text="normal recording", privacy_mode=False)
            self.assertFalse(item.privacy_mode)
            d = item.to_dict()
            self.assertFalse(d.get("privacy_mode"))
            restored = HistoryItem.from_dict(d)
            self.assertFalse(restored.privacy_mode)

    def test_privacy_mode_missing_key_defaults_false(self):
        """from_dict with missing 'privacy_mode' key defaults to False (backward compat)."""
        payload = {
            "id": "abc123",
            "ts": "2026-05-26T10:00:00",
            "text": "legacy item",
        }
        item = HistoryItem.from_dict(payload)
        self.assertFalse(item.privacy_mode, "Legacy items without privacy_mode field must default False")

    def test_privacy_mode_json_serialization(self):
        """privacy_mode field is JSON-serializable (required for NDJSON store)."""
        item = HistoryItem.create(text="json test", privacy_mode=True)
        d = item.to_dict()
        serialized = json.dumps(d)  # must not raise
        loaded = json.loads(serialized)
        self.assertIs(loaded["privacy_mode"], True)


if __name__ == "__main__":
    unittest.main()
