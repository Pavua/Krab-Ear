"""Privacy-гейты HistoryService обязаны быть fail-CLOSED (2026-09).

ЧТО НАЙДЕНО
-----------
``HistoryService._is_privacy_mode()`` ловит любой сбой чтения настроек и
возвращает ``False`` — fail-OPEN. Конструктор в ``service.py`` не передаёт
``cached_settings=``; late-inject ``self._history._settings_svc`` уже есть
(~1311) для purge, но гейт его не читает и падает в ``store.load_settings()``.

Путь достижим: ``SettingsService.cached_settings()`` ловит ТОЛЬКО
``StateStoreLockTimeout``; ``OSError`` (ENOSPC/EMFILE/EACCES в фазе flock)
пролетает мимо → get_history / search / export отдают транскрипты.

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

_SECRET = "секретный транскрипт владельца"


def _raises_oserror(*_a, **_k):
    # Именно OSError, а не StateStoreLockTimeout: cached_settings() ловит
    # только второй, а первый документирован как реалистичный в _lock().
    raise OSError(24, "Too many open files")


def _make_service(settings_side_effect, *, with_settings_svc: bool = True):
    from backend.history_service import HistoryService

    secret_item = {"id": "h1", "text": _SECRET}
    store = MagicMock()
    store.data_dir = "."
    # Fail-open сюда и утёк бы корпус: store говорит privacy OFF.
    store.load_settings.return_value = {"privacy_mode_enabled": False}
    store.get_history_page_filtered.return_value = ([secret_item], None)
    store.search_history.return_value = ([secret_item], None)

    # Как в проде: конструктор без cached_settings=; settings_svc — late-inject.
    svc = HistoryService(store=store)
    if with_settings_svc:
        settings_svc = MagicMock()
        settings_svc.cached_settings.side_effect = settings_side_effect
        svc._settings_svc = settings_svc
    else:
        store.load_settings.side_effect = settings_side_effect
    return svc


class HistoryPrivacyGateFailsClosedTests(unittest.TestCase):
    """Неизвестное состояние приватности обязано читаться как «privacy ON»."""

    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        svc = _make_service(_raises_oserror)
        self.assertTrue(
            svc._is_privacy_mode(),
            "сбой cached_settings() обязан читаться как privacy ON (fail-closed)",
        )

    def test_privacy_helper_returns_true_when_store_load_raises(self) -> None:
        """Без settings_svc IO-сбой store.load_settings() тоже fail-closed."""
        svc = _make_service(_raises_oserror, with_settings_svc=False)
        self.assertTrue(svc._is_privacy_mode())

    def test_privacy_helper_returns_real_flag_on_normal_path(self) -> None:
        svc = _make_service(lambda *a, **k: {"privacy_mode_enabled": False})
        self.assertFalse(svc._is_privacy_mode())

        svc_on = _make_service(lambda *a, **k: {"privacy_mode_enabled": True})
        self.assertTrue(svc_on._is_privacy_mode())

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        """Отсутствие ключа ≠ сбой: настройки прочитаны, режим просто выключен."""
        svc = _make_service(lambda *a, **k: {})
        self.assertFalse(svc._is_privacy_mode())

    def test_get_history_page_hides_transcripts_when_settings_raise(self) -> None:
        svc = _make_service(_raises_oserror)
        res = svc.handle_get_history_page({"limit": 50})
        self.assertEqual(res.get("items"), [])
        self.assertIsNone(res.get("next_cursor"))
        self.assertEqual(res.get("reason"), "privacy_mode_active")
        self.assertNotIn(_SECRET, str(res))
        svc.store.get_history_page_filtered.assert_not_called()

    def test_search_history_hides_transcripts_when_settings_raise(self) -> None:
        svc = _make_service(_raises_oserror)
        res = svc.handle_search_history({"query": "секрет"})
        self.assertEqual(res.get("items"), [])
        self.assertEqual(res.get("total"), 0)
        self.assertEqual(res.get("reason"), "privacy_mode_active")
        self.assertNotIn(_SECRET, str(res))
        svc.store.search_history.assert_not_called()

    def test_export_history_hides_transcripts_when_settings_raise(self) -> None:
        svc = _make_service(_raises_oserror)
        res = svc.handle_export_history({})
        self.assertEqual(res.get("content"), "")
        self.assertEqual(res.get("total_items"), 0)
        self.assertEqual(res.get("reason"), "privacy_mode_active")
        self.assertNotIn(_SECRET, str(res))
        svc.store.get_history_page_filtered.assert_not_called()


class HistoryPrivacyProductionWiringTests(unittest.TestCase):
    """BackendService late-inject ``_settings_svc`` в HistoryService (purge + privacy)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._svc = None

    def tearDown(self) -> None:
        if self._svc is not None:
            self._svc.close()

    def test_backend_late_injects_settings_svc_into_history(self) -> None:
        from backend.service import BackendService
        from backend.state_store import StateStore

        store = StateStore(data_dir=Path(self._tmpdir))
        self._svc = BackendService(store=store)

        self.assertIs(
            self._svc._history._settings_svc,
            self._svc._settings_svc,
            "BackendService должен late-inject _settings_svc в _history",
        )
        self.assertIsNone(
            self._svc._history._cached_settings,
            "конструктор HistoryService в проде не передаёт cached_settings=",
        )


if __name__ == "__main__":
    unittest.main()
