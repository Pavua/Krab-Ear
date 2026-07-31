"""S3/Задача 7b, п.11 — секция ``rest_watchdog`` в get_diagnostics.

Спека: docs/superpowers/specs/2026-07-31-s3-rest-flip-design.md §Р6.
Образец: test_diagnostics_rest_inprocess_M2.py (секция ``rest_in_process``
и симметричный ``wake_word_watchdog`` fallback).

Коллаборатор ``RestWatchdog`` опционален — конструируется только когда
рубильник REST_IN_PROCESS_ENABLED включён (см. service.py). get_diagnostics
не должен падать ни в одном из состояний: подключён-и-жив, не подключён,
подключён-но-state() упал.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_HERE = Path(__file__).resolve()
_KRAB_EAR_ROOT = _HERE.parent.parent
if str(_KRAB_EAR_ROOT) not in sys.path:
    sys.path.insert(0, str(_KRAB_EAR_ROOT))

from backend.health_check_service import HealthCheckService  # noqa: E402


def _make_service(rest_watchdog=None) -> HealthCheckService:
    """Minimal HealthCheckService с duck-typed коллабораторами (тот же
    приём, что test_diagnostics_rest_inprocess_M2.py — без BackendService,
    без daemon-тредов, tearDown-правило #1782 не применимо)."""
    store = SimpleNamespace(count_active_items=lambda: 0, data_dir="/tmp/nonexistent")
    settings_svc = SimpleNamespace(
        cached_settings=lambda: {},
        _cache_ttl=5.0,
        _cache=None,
    )
    return HealthCheckService(
        store=store,
        health_checker=SimpleNamespace(check_all=lambda: {}),
        startup_diagnostics=SimpleNamespace(),
        integrity_checker=SimpleNamespace(),
        settings_svc=settings_svc,
        rest_watchdog=rest_watchdog,
    )


class RestWatchdogDiagnosticsTest(unittest.TestCase):
    def test_wired_collaborator_reflects_state(self) -> None:
        fake_wd = SimpleNamespace(
            state=lambda: {
                "consecutive_failures": 0,
                "port_held_externally": False,
                "heal_attempts_in_window": 0,
                "last_probe_ts": None,
            }
        )
        svc = _make_service(rest_watchdog=fake_wd)
        diag = svc.handle_get_diagnostics({})
        self.assertIn("rest_watchdog", diag)
        section = diag["rest_watchdog"]
        self.assertEqual(
            {"consecutive_failures", "port_held_externally",
             "heal_attempts_in_window", "last_probe_ts"},
            set(section.keys()),
        )
        self.assertFalse(section["port_held_externally"])

    def test_missing_collaborator_is_not_an_error(self) -> None:
        # Дефолт волны S3 — рубильник REST_IN_PROCESS_ENABLED выключен,
        # поэтому None здесь означает "не подключён", не сбой диагностики.
        svc = _make_service(rest_watchdog=None)
        diag = svc.handle_get_diagnostics({})
        section = diag["rest_watchdog"]
        self.assertEqual({"enabled": False, "wired": False}, section)

    def test_constructor_wiring_stores_collaborator(self) -> None:
        fake_wd = MagicMock()
        fake_wd.state.return_value = {"consecutive_failures": 1}
        svc = HealthCheckService(
            store=MagicMock(),
            health_checker=MagicMock(),
            startup_diagnostics=MagicMock(),
            integrity_checker=MagicMock(),
            rest_watchdog=fake_wd,
        )
        self.assertIs(fake_wd, svc._rest_watchdog)
        diag = svc.handle_get_diagnostics({})
        self.assertEqual({"consecutive_failures": 1}, diag["rest_watchdog"])

    def test_diagnostics_still_includes_rest_in_process_section_unchanged(self) -> None:
        # Регрессия: новая секция не должна вытеснить или изменить соседнюю
        # rest_in_process (M2/S3), которая осталась без изменений.
        svc = _make_service(rest_watchdog=None)
        diag = svc.handle_get_diagnostics({})
        self.assertIn("rest_in_process", diag)
        self.assertIn("wake_word_watchdog", diag)


if __name__ == "__main__":
    unittest.main()
