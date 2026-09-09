"""Privacy-гейты text/collection/speaker обязаны быть fail-CLOSED (2026-09).

Кандидаты аудита всё ещё ``except Exception: return False`` на чтении
``privacy_mode_enabled`` — OSError из ``cached_settings()`` (ENOSPC/EMFILE/
EACCES в ``StateStore._lock()``) открывает гейт.

Эталон: ``RecordingCoreService._privacy_mode_enabled`` +
``test_privacy_gate_fail_closed_2026_09_01.py``.

TextProcessing: тесты инжектят optional ``settings_svc`` (как TextScoring).
Runtime wiring в ``service.py`` — follow-up: #2005 уже в базе, но этот PR
его не трогает, чтобы не смешивать волны.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _raises_oserror(*_a, **_k):
    # Именно OSError: cached_settings() ловит только StateStoreLockTimeout.
    raise OSError(24, "Too many open files")


def _settings_svc(side_effect):
    svc = MagicMock()
    svc.cached_settings.side_effect = side_effect
    return svc


def _make_text_processing(settings_side_effect):
    from backend.text_processing_service import TextProcessingService

    return TextProcessingService(
        readability_scorer=MagicMock(),
        transcription_scorer=MagicMock(),
        emotion_detector=MagicMock(),
        text_comparator=MagicMock(),
        abbreviation_expander=MagicMock(),
        text_postprocessor=MagicMock(),
        store=MagicMock(),
        settings_svc=_settings_svc(settings_side_effect),
    )


def _make_collection(settings_fn):
    from backend.collection_manager import CollectionManager

    tmp = tempfile.mkdtemp()
    return CollectionManager(store=SimpleNamespace(data_dir=tmp), settings_fn=settings_fn)


def _make_speaker(settings_fn):
    from backend.speaker_manager import SpeakerManager

    return SpeakerManager(data_dir=None, settings_fn=settings_fn)


class TextProcessingPrivacyGateFailsClosedTests(unittest.TestCase):
    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        svc = _make_text_processing(_raises_oserror)
        self.assertTrue(
            svc._is_privacy_mode(),
            "сбой cached_settings() обязан читаться как privacy ON (fail-closed)",
        )

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        svc = _make_text_processing(lambda *a, **k: {})
        self.assertFalse(svc._is_privacy_mode())

    def test_summarize_text_blocked_when_settings_raise(self) -> None:
        svc = _make_text_processing(_raises_oserror)
        result = svc.handle_summarize_text({"text": "секретный транскрипт владельца"})
        self.assertEqual(result.get("summary"), "")
        self.assertEqual(result.get("reason"), "privacy_mode_active")


class CollectionPrivacyGateFailsClosedTests(unittest.TestCase):
    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        mgr = _make_collection(_raises_oserror)
        self.assertTrue(
            mgr._is_privacy_mode(),
            "сбой settings_fn обязан читаться как privacy ON (fail-closed)",
        )

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        mgr = _make_collection(lambda: {})
        self.assertFalse(mgr._is_privacy_mode())

    def test_list_collections_blocked_when_settings_raise(self) -> None:
        mgr = _make_collection(_raises_oserror)
        result = mgr.handle_list_collections({})
        self.assertEqual(result.get("collections"), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")


class SpeakerPrivacyGateFailsClosedTests(unittest.TestCase):
    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        mgr = _make_speaker(_raises_oserror)
        self.assertTrue(
            mgr._is_privacy_mode(),
            "сбой settings_fn обязан читаться как privacy ON (fail-closed)",
        )

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        mgr = _make_speaker(lambda: {})
        self.assertFalse(mgr._is_privacy_mode())

    def test_get_aliases_blocked_when_settings_raise(self) -> None:
        mgr = _make_speaker(_raises_oserror)
        result = mgr.handle_get_speaker_aliases({})
        self.assertEqual(result.get("aliases"), {})
        self.assertEqual(result.get("reason"), "privacy_mode_active")


if __name__ == "__main__":
    unittest.main()
