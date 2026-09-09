"""Privacy-гейты HealthCheckService: diagnostics fail-CLOSED (2026-09).

``HealthCheckService._is_privacy_mode()`` (путь ``get_diagnostics``) ловил любой
сбой чтения настроек и возвращал ``False`` — fail-OPEN: ``history.total_items``
мог утечь count активных записей при IO/lock ошибке.

``_is_privacy_mode_nowait()`` для ``handle_ping`` делегирует fail-closed
``SettingsService.cached_settings(nowait=True)``; внешний except тоже обязан
читать неизвестность как privacy ON, не ломая zero-wait ping.

Эталон: ``history_service._is_privacy_mode`` +
``test_privacy_gate_fail_closed_history_2026_09.py``.
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

_REAL_HISTORY_COUNT = 12_591


def _raises_oserror(*_a, **_k):
    raise OSError(24, "Too many open files")


def _make_health_svc(settings_side_effect, *, store_count: int = _REAL_HISTORY_COUNT):
    from backend.health_check_service import HealthCheckService

    store = MagicMock()
    store.data_dir = "/tmp/krab_test_data"
    store.count_active_items.return_value = store_count

    settings_svc = MagicMock()
    settings_svc._cache_ttl = 5
    settings_svc._cache = None
    settings_svc.cached_settings.side_effect = settings_side_effect

    return HealthCheckService(
        store=store,
        health_checker=MagicMock(),
        startup_diagnostics=MagicMock(),
        integrity_checker=MagicMock(),
        settings_svc=settings_svc,
        start_time=0.0,
        app_version="test-health-privacy",
        recorder=MagicMock(is_recording=False),
    ), store, settings_svc


class HealthDiagnosticsPrivacyGateFailsClosedTests(unittest.TestCase):
    """``get_diagnostics`` обязан маскировать history_count при сбое settings."""

    def test_privacy_helper_returns_true_when_settings_raise(self) -> None:
        svc, _, _ = _make_health_svc(_raises_oserror)
        self.assertTrue(
            svc._is_privacy_mode(),
            "сбой cached_settings() обязан читаться как privacy ON (fail-closed)",
        )

    def test_privacy_helper_returns_real_flag_on_normal_path(self) -> None:
        svc, _, _ = _make_health_svc(lambda *a, **k: {"privacy_mode_enabled": False})
        self.assertFalse(svc._is_privacy_mode())

        svc_on, _, _ = _make_health_svc(lambda *a, **k: {"privacy_mode_enabled": True})
        self.assertTrue(svc_on._is_privacy_mode())

    def test_missing_key_is_treated_as_privacy_off(self) -> None:
        svc, _, _ = _make_health_svc(lambda *a, **k: {})
        self.assertFalse(svc._is_privacy_mode())

    def test_get_diagnostics_masks_history_count_when_settings_raise(self) -> None:
        svc, store, settings_svc = _make_health_svc(_raises_oserror)
        result = svc.handle_get_diagnostics({})
        self.assertEqual(result["history"]["total_items"], 0)
        store.count_active_items.assert_not_called()
        settings_svc.cached_settings.assert_called()

    def test_get_diagnostics_shows_real_count_when_privacy_off(self) -> None:
        svc, store, _ = _make_health_svc(lambda *a, **k: {"privacy_mode_enabled": False})
        result = svc.handle_get_diagnostics({})
        self.assertEqual(result["history"]["total_items"], _REAL_HISTORY_COUNT)
        store.count_active_items.assert_called_once_with()


class HealthPingPrivacyNowaitFailsClosedTests(unittest.TestCase):
    """Ping остаётся nowait; неожиданный сбой privacy-чтения — fail-closed."""

    def test_nowait_helper_returns_true_when_settings_raise(self) -> None:
        svc, _, settings_svc = _make_health_svc(_raises_oserror)
        self.assertTrue(
            svc._is_privacy_mode_nowait(),
            "неожиданный сбой nowait-чтения обязан маскировать history_count",
        )
        settings_svc.cached_settings.assert_called_once_with(nowait=True)

    def test_ping_masks_history_count_on_settings_raise_without_store_call(self) -> None:
        svc, store, settings_svc = _make_health_svc(_raises_oserror)
        result = svc.handle_ping({})
        self.assertEqual(result["history_count"], 0)
        store.count_active_items.assert_not_called()
        settings_svc.cached_settings.assert_called_once_with(nowait=True)


class HealthPrivacyProductionWiringTests(unittest.TestCase):
    """BackendService передаёт settings_svc в HealthCheckService."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._svc = None

    def tearDown(self) -> None:
        if self._svc is not None:
            self._svc.close()

    def test_backend_wires_settings_svc_into_health_check(self) -> None:
        from backend.service import BackendService
        from backend.state_store import StateStore

        store = StateStore(data_dir=Path(self._tmpdir))
        self._svc = BackendService(store=store)

        self.assertIs(
            self._svc._health_check_svc._settings_svc,
            self._svc._settings_svc,
            "BackendService должен wire _settings_svc в _health_check_svc",
        )


if __name__ == "__main__":
    unittest.main()
