"""Тесты W1292 — AutoGlossary.invalidate() вызывается после persist записи.

Покрывает:
  1. HistoryService.handle_add_history_item → invalidate() вызывается.
  2. RecordingCoreService._stop_recording_phase_e → invalidate() вызывается.
  3. Исключение в invalidate() не ломает persist (graceful degradation).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import HistoryService
from backend.state_store import StateStore
from tests.test_helpers import make_test_item  # noqa: E402


# ---------------------------------------------------------------------------
# Helper: minimal fake AutoGlossaryBuilder
# ---------------------------------------------------------------------------

class _FakeGlossary:
    """Minimal stub that records invalidate() calls."""

    def __init__(self) -> None:
        self.invalidate_count = 0

    def invalidate(self) -> None:
        self.invalidate_count += 1


class _RaisingGlossary:
    """Stub that raises on invalidate() to test error isolation."""

    def invalidate(self) -> None:
        raise RuntimeError("glossary boom")


# ---------------------------------------------------------------------------
# HistoryService tests
# ---------------------------------------------------------------------------

class TestHistoryAddInvalidatesAutoGlossary(unittest.TestCase):
    """handle_add_history_item должен вызвать invalidate() после успешного persist."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.glossary = _FakeGlossary()
        self.svc = HistoryService(
            store=self.store,
            auto_glossary_builder=self.glossary,
        )

    def test_history_add_invalidates_auto_glossary(self) -> None:
        """invalidate() вызывается ровно один раз после add_history_item."""
        result = self.svc.handle_add_history_item({"text": "Привет мир"})
        self.assertEqual(self.glossary.invalidate_count, 1)
        # Persist должен был вернуть dict с id
        self.assertIn("id", result)

    def test_no_invalidate_without_builder(self) -> None:
        """Если auto_glossary_builder=None — работает без ошибок."""
        svc = HistoryService(store=self.store)  # no auto_glossary_builder
        result = svc.handle_add_history_item({"text": "без глоссария"})
        self.assertIn("id", result)

    def test_invalidate_failure_does_not_break_persist(self) -> None:
        """Если invalidate() бросает — add_history_item всё равно возвращает dict."""
        svc = HistoryService(
            store=self.store,
            auto_glossary_builder=_RaisingGlossary(),
        )
        result = svc.handle_add_history_item({"text": "ошибка инвалидации"})
        # Persist должен был выполниться, несмотря на исключение в invalidate()
        self.assertIn("id", result)
        # Запись реально добавилась в store
        items, _ = self.store.get_history_page(cursor=None, limit=10)
        texts = [i.text if hasattr(i, "text") else i.get("text", "") for i in items]
        self.assertIn("ошибка инвалидации", texts)

    def test_multiple_adds_increment_invalidate_count(self) -> None:
        """Каждый add_history_item должен вызывать invalidate()."""
        self.svc.handle_add_history_item({"text": "первая запись"})
        self.svc.handle_add_history_item({"text": "вторая запись"})
        self.assertEqual(self.glossary.invalidate_count, 2)

    def test_late_injection_via_attribute(self) -> None:
        """Можно передать auto_glossary через атрибут после создания сервиса."""
        svc = HistoryService(store=self.store)  # no builder at init
        glossary = _FakeGlossary()
        svc._auto_glossary = glossary  # late injection
        svc.handle_add_history_item({"text": "позднее внедрение"})
        self.assertEqual(glossary.invalidate_count, 1)


# ---------------------------------------------------------------------------
# RecordingCoreService._stop_recording_phase_e tests
# ---------------------------------------------------------------------------

class TestRecordingPersistInvalidatesAutoGlossary(unittest.TestCase):
    """_stop_recording_phase_e должен вызвать invalidate() после persist."""

    def _make_service(self, glossary: object):
        """Build a RecordingCoreService with fake collaborators."""
        from backend.recording_core_service import RecordingCoreService

        fake_store = MagicMock()
        fake_store.data_dir = Path(self.tmp.name) / "data"
        fake_store.add_history_item.return_value = make_test_item(
            id="test-id-001", ts="2026-05-27T00:00:00"
        )

        svc = RecordingCoreService(
            recorder=MagicMock(),
            transcriber=MagicMock(),
            translator=MagicMock(),
            store=fake_store,
            vocabulary=MagicMock(),
            settings_svc=MagicMock(),
            llm_rewriter=MagicMock(),
            auto_glossary=glossary,
            semantic_searcher=MagicMock(is_enabled=False),
            context_memory=MagicMock(),
            clipboard_history=[],
            auto_backup=MagicMock(),
            session_tracker=MagicMock(),
            action_items_extractor=MagicMock(),
            transcription_counter_ref=[0],
            last_stt_engine_ref=[None],
        )
        return svc, fake_store

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _make_phase_d(self) -> dict:
        from unittest.mock import MagicMock as MM
        translation = MM()
        translation.mode = "off"
        translation.source_lang = "ru"
        translation.target_lang = ""
        translation.engine = ""
        return {
            "text": "тест",
            "display_text": "тест",
            "translated_text": "",
            "final_text": "тест",
            "translation": translation,
            "translation_status": "not_requested",
            "confidence": 0.95,
            "diarization_data": None,
            "tp": {},
        }

    def _make_sr(self) -> dict:
        return {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "translation_style": "natural",
            "translate_and_paste": False,
        }

    def test_recording_persist_invalidates_auto_glossary(self) -> None:
        """_stop_recording_phase_e вызывает invalidate() после add_history_item."""
        glossary = _FakeGlossary()
        svc, _ = self._make_service(glossary)

        # Patch event_bus.emit_typed to avoid contract imports
        with patch("backend.recording_core_service.event_bus") as mock_bus:
            mock_bus.emit_typed = MagicMock()
            svc._stop_recording_phase_e(
                phase_d=self._make_phase_d(),
                sr=self._make_sr(),
                duration_sec=5.0,
                stop_tail_trim_ms=200,
                silence_detected=False,
                silence_guard_enabled=False,
                background_guard_rejected=False,
                rt_session_id=None,
                settings={},
            )

        self.assertEqual(glossary.invalidate_count, 1)

    def test_invalidate_failure_does_not_break_persist_recording(self) -> None:
        """Если invalidate() бросает — _stop_recording_phase_e всё равно завершается."""
        svc, fake_store = self._make_service(_RaisingGlossary())

        with patch("backend.recording_core_service.event_bus") as mock_bus:
            mock_bus.emit_typed = MagicMock()
            result = svc._stop_recording_phase_e(
                phase_d=self._make_phase_d(),
                sr=self._make_sr(),
                duration_sec=3.0,
                stop_tail_trim_ms=0,
                silence_detected=False,
                silence_guard_enabled=False,
                background_guard_rejected=False,
                rt_session_id=None,
                settings={},
            )

        # add_history_item должен был вызваться
        fake_store.add_history_item.assert_called_once()
        # Результат должен содержать status
        self.assertEqual(result.get("status"), "ok")


if __name__ == "__main__":
    unittest.main()
