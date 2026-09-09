"""Privacy-гейты TextScoringService обязаны быть fail-CLOSED (2026-09).

Кандидат аудита: ``text_scoring_service.py`` читал ``privacy_mode_enabled`` через
generic ``_get_runtime_setting(..., False)`` — при сбое чтения settings путь
оставался открыт (fail-OPEN).

Эталон: ``recording_core_service._privacy_mode_enabled`` +
``test_privacy_gate_fail_closed_2026_09_01.py``.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _raises_oserror():
    raise OSError(24, "Too many open files")


def _make_service(settings_side_effect):
    from backend.text_scoring_service import TextScoringService

    settings_svc = MagicMock()
    settings_svc.cached_settings.side_effect = settings_side_effect

    term_extractor = MagicMock()
    term_extractor.extract_terms.return_value = [
        MagicMock(term="секрет", confidence=0.9, frequency=1, is_proper_noun=False),
    ]
    auto_title_generator = MagicMock()
    auto_title_generator.generate_title.return_value = "Секретный заголовок"

    return TextScoringService(
        llm_rewriter=None,
        term_extractor=term_extractor,
        auto_title_generator=auto_title_generator,
        get_runtime_setting=lambda key, default: default,
        settings_svc=settings_svc,
    )


class TextScoringPrivacyGateFailsClosedTests(unittest.TestCase):
    """Неизвестное состояние приватности обязано читаться как «privacy ON»."""

    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        svc = _make_service(_raises_oserror)
        self.assertTrue(
            svc._privacy_mode_enabled(),
            "сбой cached_settings() обязан читаться как privacy ON (fail-closed)",
        )

    def test_privacy_helper_returns_real_flag_on_normal_path(self) -> None:
        svc = _make_service(lambda *a, **k: {"privacy_mode_enabled": False})
        self.assertFalse(svc._privacy_mode_enabled())

        svc_on = _make_service(lambda *a, **k: {"privacy_mode_enabled": True})
        self.assertTrue(svc_on._privacy_mode_enabled())

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        svc = _make_service(lambda *a, **k: {})
        self.assertFalse(svc._privacy_mode_enabled())

    def test_extract_terms_blocked_when_settings_raise(self) -> None:
        svc = _make_service(_raises_oserror)
        result = svc.handle_extract_terms({"text": "секретный транскрипт владельца"})
        self.assertEqual(result.get("terms"), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        svc._term_extractor.extract_terms.assert_not_called()

    def test_generate_auto_title_blocked_when_settings_raise(self) -> None:
        svc = _make_service(_raises_oserror)
        result = svc.handle_generate_auto_title({"text": "секретный транскрипт владельца"})
        self.assertEqual(result.get("title"), "")
        self.assertEqual(result.get("titles"), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        svc._auto_title_generator.generate_title.assert_not_called()


class TextScoringPrivacyProductionWiringTests(unittest.TestCase):
    """BackendService.__init__ обязан передать settings_svc для fail-closed privacy."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._svc = None

    def tearDown(self) -> None:
        if self._svc is not None:
            self._svc.close()

    def test_backend_wires_settings_svc_into_text_scoring_svc(self) -> None:
        from backend.service import BackendService
        from backend.state_store import StateStore

        store = StateStore(data_dir=Path(self._tmpdir))
        self._svc = BackendService(store=store)

        self.assertIs(
            self._svc._text_scoring_svc._settings_svc,
            self._svc._settings_svc,
            "BackendService должен wire _settings_svc в _text_scoring_svc",
        )
        self.assertIsNotNone(self._svc._text_scoring_svc._settings_svc)


if __name__ == "__main__":
    unittest.main()
