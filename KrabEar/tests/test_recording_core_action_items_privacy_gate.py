"""Regression test: action_items_auto_extract must respect privacy_mode_enabled.

Bug (recording_core_service.py, _stop_recording_phase_e, ~line 1644):
  The action_items_auto_extract block sent the full transcript (display_text) to
  self._action_items_extractor.extract() — an LLM call — WITHOUT checking
  _privacy_mode at all, unlike the neighbouring STT_FINAL emit (line ~1602) and
  the semantic auto-index guard (line ~1561) in the exact same function, both of
  which already gate on `not _privacy_mode`.

  Per CLAUDE.md "Privacy gate completeness": any code path that reads/sends
  transcript-derived content MUST gate on privacy_mode_enabled. A user with
  action_items_auto_extract=True and privacy_mode_enabled=True would silently
  leak their transcript to the LLM rewriter path despite believing privacy mode
  protected them.

Fix: wrap the whole action_items_auto_extract block in `if not _privacy_mode:`,
mirroring the existing pattern used elsewhere in _stop_recording_phase_e.

This test FAILS before the fix (extractor.extract() is called even in privacy
mode) and PASSES after.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.action_items_extractor import ActionItem, ActionItemsResult
from backend.recording_core_service import RecordingCoreService
from backend.state_store import StateStore


class _FakeSettingsSvc:
    def cached_settings(self):
        return {}

    def invalidate_cache(self):
        pass


class _FakeSemanticSearcher:
    is_enabled = False

    def index_item(self, item_id, text):
        pass


class ActionItemsAutoExtractPrivacyGateTest(unittest.TestCase):
    """recording_core_service.py:~1644 — action_items_auto_extract privacy gate."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def _make_service(self, action_items_extractor):
        store = StateStore(data_dir=Path(self._tmp))
        vocab = MagicMock()
        vocab.get_words.return_value = []
        session_tracker = MagicMock()
        session_tracker._active_session = None
        return RecordingCoreService(
            recorder=MagicMock(),
            transcriber=MagicMock(),
            translator=MagicMock(),
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
            action_items_extractor=action_items_extractor,
            transcription_counter_ref=[0],
            last_stt_engine_ref=[None],
        )

    def _make_phase_d(self, text: str = "Нужно сделать X к пятнице.") -> dict:
        from backend.translator import TranslationResult
        tr = TranslationResult(
            text=text,
            status="skipped",
            source_lang="auto",
            target_lang="ru",
            mode="auto",
            engine="fake",
        )
        return {
            "text": text,
            "display_text": text,
            "translated_text": "",
            "final_text": text,
            "translation": tr,
            "translation_status": "skipped",
            "confidence": 0.9,
            "diarization_data": None,
            "tp": {"language": "ru"},
        }

    def _make_sr(self) -> dict:
        return {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "translation_style": "neutral",
            "translate_and_paste": False,
        }

    def _call_phase_e(self, svc, settings: dict, duration_sec: float = 120.0) -> dict:
        return svc._stop_recording_phase_e(
            phase_d=self._make_phase_d(),
            sr=self._make_sr(),
            duration_sec=duration_sec,
            stop_tail_trim_ms=0,
            silence_detected=False,
            silence_guard_enabled=False,
            background_guard_rejected=False,
            rt_session_id=None,
            settings=settings,
        )

    def test_action_items_extract_skipped_when_privacy_mode_on(self):
        """privacy_mode_enabled=True must block the LLM call entirely, even when
        action_items_auto_extract=True and duration exceeds the threshold."""
        extractor = MagicMock()
        # Real dataclass so a typo'd attribute access would fail loudly, not be
        # silently swallowed by a MagicMock stand-in.
        extractor.extract.return_value = ActionItemsResult(
            action_items=[ActionItem(text="Сделать X", priority="high")],
            decisions=["Решили Y"],
            questions=["Что насчёт Z?"],
            ok=True,
        )
        svc = self._make_service(extractor)

        result = self._call_phase_e(
            svc,
            settings={
                "action_items_auto_extract": True,
                "action_items_min_duration_sec": 60.0,
                "privacy_mode_enabled": True,
            },
            duration_sec=120.0,
        )

        extractor.extract.assert_not_called()
        self.assertNotIn(
            "action_items_extracted", result,
            "action_items_extracted must not appear in the result when privacy_mode is on",
        )
        self.assertNotIn(
            "action_items_count", result,
            "action_items_count must not appear in the result when privacy_mode is on",
        )

    def test_action_items_extract_called_when_privacy_mode_off(self):
        """Control: normal path must be unaffected by the fix — extractor is still
        invoked and its result still surfaces in result_payload when privacy is off."""
        extractor = MagicMock()
        extractor.extract.return_value = ActionItemsResult(
            action_items=[ActionItem(text="Сделать X", priority="high")],
            decisions=["Решили Y"],
            questions=["Что насчёт Z?"],
            ok=True,
        )
        svc = self._make_service(extractor)

        result = self._call_phase_e(
            svc,
            settings={
                "action_items_auto_extract": True,
                "action_items_min_duration_sec": 60.0,
                "privacy_mode_enabled": False,
            },
            duration_sec=120.0,
        )

        extractor.extract.assert_called_once()
        self.assertTrue(result.get("action_items_extracted"))
        self.assertEqual(result.get("action_items_count"), 1)


if __name__ == "__main__":
    unittest.main()
