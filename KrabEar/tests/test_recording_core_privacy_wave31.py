"""Tests for wave-31 privacy fixes in RecordingCoreService.

FIX B1 (HIGH): handle_get_recording_state preview_text leak
  Before: the IPC poll method returned self._preview_text even when
  privacy_mode_enabled=True, leaking accumulated partial transcript.
  After:  preview_text="" is returned when privacy_mode is on.

FIX B2 (MED): semantic auto-index ignores privacy_mode
  Before: after transcription completes, _semantic_searcher.index_item was
  called unconditionally, embedding privacy-mode transcript text into the
  persistent search index.
  After:  index_item is NOT called when privacy_mode_enabled=True.
"""

from __future__ import annotations

import sys
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_KRAB_EAR = os.path.join(_PROJECT_ROOT, "KrabEar")
if _KRAB_EAR not in sys.path:
    sys.path.insert(0, _KRAB_EAR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings_svc(privacy: bool = False) -> MagicMock:
    svc = MagicMock()
    svc.cached_settings.return_value = {"privacy_mode_enabled": privacy}
    return svc


def _make_service(privacy: bool = False):
    from backend.recording_core_service import RecordingCoreService

    recorder = MagicMock()
    recorder.is_recording = True
    recorder.snapshot_rms = MagicMock(return_value=0.0)
    recorder.get_duration_sec = MagicMock(return_value=2.5)

    session_tracker = MagicMock()
    session_tracker._active_session = {"session_id": "sess-test"}

    svc = RecordingCoreService(
        recorder=recorder,
        transcriber=MagicMock(),
        translator=MagicMock(),
        store=MagicMock(),
        vocabulary=MagicMock(),
        settings_svc=_make_settings_svc(privacy=privacy),
        llm_rewriter=MagicMock(),
        auto_glossary=MagicMock(),
        semantic_searcher=MagicMock(),
        context_memory=MagicMock(),
        clipboard_history=[],
        auto_backup=MagicMock(),
        session_tracker=session_tracker,
        action_items_extractor=MagicMock(),
        transcription_counter_ref=[0],
        last_stt_engine_ref=[None],
    )
    return svc


# ---------------------------------------------------------------------------
# B1: handle_get_recording_state preview_text gate
# ---------------------------------------------------------------------------

class TestGetRecordingStatePrivacyGate(unittest.TestCase):
    """wave-31 HIGH: preview_text must be empty when privacy_mode_enabled=True."""

    def _inject_preview_text(self, svc, text: str) -> None:
        """Directly set the internal _preview_text to simulate live partial transcript."""
        with svc._preview_lock:
            svc._preview_text = text
            svc._preview_duration_sec = 3.0

    def test_preview_text_visible_when_privacy_off(self):
        """Normal path: preview_text is returned when privacy_mode is off."""
        svc = _make_service(privacy=False)
        self._inject_preview_text(svc, "тест расшифровки")

        result = svc.handle_get_recording_state({})

        self.assertEqual(result["preview_text"], "тест расшифровки",
                         "preview_text должен быть виден, когда privacy_mode=False")

    def test_preview_text_empty_when_privacy_on(self):
        """Privacy gate: preview_text must be empty when privacy_mode_enabled=True."""
        svc = _make_service(privacy=True)
        self._inject_preview_text(svc, "секретная переговорная")

        result = svc.handle_get_recording_state({})

        self.assertEqual(result["preview_text"], "",
                         "preview_text должен быть пустым при privacy_mode=True")

    def test_other_fields_still_returned_in_privacy_mode(self):
        """Non-PII fields (is_recording, duration_sec, audio_rms, elapsed_sec, session_id)
        must still be present and accurate even when privacy_mode=True."""
        svc = _make_service(privacy=True)
        self._inject_preview_text(svc, "не должно утечь")

        result = svc.handle_get_recording_state({})

        # Fields must exist
        for key in ("is_recording", "duration_sec", "audio_rms", "elapsed_sec", "session_id"):
            self.assertIn(key, result, f"Поле {key!r} должно присутствовать в ответе")

        # preview_text specifically emptied
        self.assertEqual(result["preview_text"], "")

        # Non-PII fields have meaningful values
        self.assertTrue(result["is_recording"])
        self.assertEqual(result["session_id"], "sess-test")

    def test_privacy_toggle_from_off_to_on(self):
        """Toggling privacy_mode mid-session: subsequent polls must hide preview_text."""
        settings_data = {"privacy_mode_enabled": False}
        settings_svc = MagicMock()
        settings_svc.cached_settings.return_value = settings_data

        from backend.recording_core_service import RecordingCoreService
        recorder = MagicMock()
        recorder.is_recording = True
        recorder.snapshot_rms = MagicMock(return_value=0.0)
        recorder.get_duration_sec = MagicMock(return_value=1.0)
        session_tracker = MagicMock()
        session_tracker._active_session = None

        svc = RecordingCoreService(
            recorder=recorder,
            transcriber=MagicMock(),
            translator=MagicMock(),
            store=MagicMock(),
            vocabulary=MagicMock(),
            settings_svc=settings_svc,
            llm_rewriter=MagicMock(),
            auto_glossary=MagicMock(),
            semantic_searcher=MagicMock(),
            context_memory=MagicMock(),
            clipboard_history=[],
            auto_backup=MagicMock(),
            session_tracker=session_tracker,
            action_items_extractor=MagicMock(),
            transcription_counter_ref=[0],
            last_stt_engine_ref=[None],
        )

        with svc._preview_lock:
            svc._preview_text = "частичный текст"

        # Phase 1: privacy off — text visible
        result_off = svc.handle_get_recording_state({})
        self.assertEqual(result_off["preview_text"], "частичный текст")

        # Toggle privacy on
        settings_data["privacy_mode_enabled"] = True

        # Phase 2: privacy on — text hidden
        result_on = svc.handle_get_recording_state({})
        self.assertEqual(result_on["preview_text"], "")


# ---------------------------------------------------------------------------
# B2: semantic auto-index privacy gate
# ---------------------------------------------------------------------------

class TestSemanticAutoIndexPrivacyGate(unittest.TestCase):
    """wave-31 MED: semantic index_item must NOT be called when privacy_mode=True.

    Вызывается НАСТОЯЩИЙ RecordingCoreService._stop_recording_phase_e. Прежние тесты исполняли
    копию условия (и «страховку» поиском строки, не ограничивавшую расстояние слева), поэтому
    удаление проверки privacy из рабочего кода их не краснило.
    """

    def _run_real_phase_e(self, *, privacy: bool, searcher_enabled: bool = True) -> MagicMock:
        import tempfile
        import threading as _real_threading
        from backend.recording_core_service import RecordingCoreService
        from backend.state_store import StateStore
        import backend.recording_core_service as _rcs_mod
        from backend.translator import TranslationResult

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        searcher = MagicMock()
        searcher.is_enabled = searcher_enabled
        session_tracker = MagicMock()
        session_tracker._active_session = None
        vocab = MagicMock()
        vocab.get_words.return_value = []
        svc = RecordingCoreService(
            recorder=MagicMock(), transcriber=MagicMock(), translator=MagicMock(),
            store=StateStore(data_dir=Path(tmp.name)), vocabulary=vocab,
            settings_svc=_make_settings_svc(privacy=privacy), llm_rewriter=None, auto_glossary=None,
            semantic_searcher=searcher, context_memory=None, clipboard_history=[],
            auto_backup=MagicMock(), session_tracker=session_tracker, action_items_extractor=None,
            transcription_counter_ref=[0], last_stt_engine_ref=[None],
        )
        text = "Тестовая запись для индексации."
        tr = TranslationResult(text=text, status="skipped", source_lang="auto", target_lang="ru",
                               mode="auto", engine="fake")
        phase_d = {
            "text": text, "display_text": text, "translated_text": "", "final_text": text,
            "translation": tr, "translation_status": "skipped", "confidence": 0.9,
            "diarization_data": None, "tp": {"language": "ru"},
        }
        sr = {"quality_profile": "balanced", "cleanup_profile": "soft",
              "translation_style": "neutral", "translate_and_paste": False}

        real_thread = _real_threading.Thread

        class _SyncSemanticThread:
            """semantic-index поток исполняется синхронно; прочие потоки — настоящие."""

            def __new__(cls, *args, **kwargs):
                if kwargs.get("name") == "semantic-index":
                    inst = object.__new__(cls)
                    inst._target, inst._args = kwargs["target"], kwargs.get("args", ())
                    return inst
                return real_thread(*args, **kwargs)

            def start(self):
                self._target(*self._args)

        with patch.object(_rcs_mod._cfg_settings, "SEMANTIC_SEARCH_AUTO_INDEX", True), \
                patch.object(_rcs_mod.threading, "Thread", _SyncSemanticThread):
            svc._stop_recording_phase_e(
                phase_d=phase_d, sr=sr, duration_sec=5.0, stop_tail_trim_ms=0,
                silence_detected=False, silence_guard_enabled=False,
                background_guard_rejected=False, rt_session_id=None,
                settings={"privacy_mode_enabled": privacy},
            )
        return searcher

    def test_semantic_index_called_when_privacy_off(self):
        """Normal path: index_item вызывается один раз с текстом записи при privacy OFF."""
        searcher = self._run_real_phase_e(privacy=False)
        searcher.index_item.assert_called_once()
        self.assertIn("Тестовая запись", searcher.index_item.call_args.args[1])

    def test_semantic_index_skipped_when_privacy_on(self):
        """Privacy gate: index_item НЕ вызывается при privacy_mode=True."""
        searcher = self._run_real_phase_e(privacy=True)
        searcher.index_item.assert_not_called()

    def test_semantic_index_not_started_when_disabled(self):
        """Baseline: index_item не вызывается при is_enabled=False независимо от privacy."""
        searcher = self._run_real_phase_e(privacy=False, searcher_enabled=False)
        searcher.index_item.assert_not_called()


if __name__ == "__main__":
    unittest.main()
