"""M2 Task 7 — секция ``rest_in_process`` в get_diagnostics.

Спека: docs/superpowers/specs/2026-07-16-m-series-rest-merge-design.md §4.2
(рубильник REST_IN_PROCESS_ENABLED, дефолт выключен).

Коллаборатор ``InProcessRestServer`` опционален — HealthCheckService должен
честно отражать три состояния: подключён-и-жив, не подключён (дефолт волны),
подключён-но-упал при опросе. Ни одно из них не должно ронять
handle_get_diagnostics (тот же паттерн, что _get_event_bridge_summary).

Coverage:
  1. коллаборатор передан -> секция отражает его status() дословно
  2. коллаборатор None -> enabled/running=False, port/error=None (не ошибка)
  3. status() бросает исключение -> error="status_failed", полный набор ключей
  4. проводка конструктора: self._rest_inprocess сохраняется из параметра
  5. S3/Задача 4: настоящее надгробие (_RestInProcessTombstone, не фейк) даёт
     ТРЕТЬЕ отличимое состояние — "включён, но сборка упала" — а не тот же
     словарь, что при коллабораторе None ("рубильник выключен"). Без этого
     ключа канарейка не отличила бы дефолтное выключенное состояние от
     мёртвого REST.
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
from backend.service import _RestInProcessTombstone  # noqa: E402

_SCHEMA_KEYS = {"enabled", "running", "port", "error"}


def _make_service(rest_inprocess=None) -> HealthCheckService:
    """Minimal HealthCheckService с duck-typed коллабораторами (тот же
    приём, что test_brain_lease_status_B3.py — без BackendService, без
    daemon-тредов, tearDown-правило #1782 не применимо)."""
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
        rest_inprocess=rest_inprocess,
    )


class RestInProcessDiagnosticsTest(unittest.TestCase):
    # 1 ------------------------------------------------------------------
    def test_wired_collaborator_reflects_status(self) -> None:
        fake_rest = SimpleNamespace(
            status=lambda: {"enabled": True, "running": True, "port": 5005, "error": None}
        )
        svc = _make_service(rest_inprocess=fake_rest)
        diag = svc.handle_get_diagnostics({})
        self.assertIn("rest_in_process", diag)
        section = diag["rest_in_process"]
        self.assertEqual(_SCHEMA_KEYS, set(section.keys()))
        self.assertTrue(section["enabled"])
        self.assertTrue(section["running"])
        self.assertEqual(5005, section["port"])
        self.assertIsNone(section["error"])

    # 2 ------------------------------------------------------------------
    def test_missing_collaborator_is_not_an_error(self) -> None:
        # Дефолт волны M2 — рубильник REST_IN_PROCESS_ENABLED выключен, поэтому
        # None здесь означает "выключено", а не сбой диагностики.
        svc = _make_service(rest_inprocess=None)
        diag = svc.handle_get_diagnostics({})
        section = diag["rest_in_process"]
        self.assertEqual(_SCHEMA_KEYS, set(section.keys()))
        self.assertFalse(section["enabled"])
        self.assertFalse(section["running"])
        self.assertIsNone(section["port"])
        self.assertIsNone(section["error"])

    # 3 ------------------------------------------------------------------
    def test_status_exception_is_swallowed(self) -> None:
        fake_rest = SimpleNamespace(status=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        svc = _make_service(rest_inprocess=fake_rest)
        # Не должно бросать наружу.
        diag = svc.handle_get_diagnostics({})
        section = diag["rest_in_process"]
        self.assertEqual(_SCHEMA_KEYS, set(section.keys()))
        self.assertFalse(section["enabled"])
        self.assertFalse(section["running"])
        self.assertIsNone(section["port"])
        self.assertEqual("status_failed", section["error"])

    # 4 ------------------------------------------------------------------
    def test_constructor_wiring_stores_collaborator(self) -> None:
        # Обязательные четыре аргумента — через MagicMock (образец из
        # инструкции задачи), проверяем именно проводку параметра в self.
        fake_rest = MagicMock()
        fake_rest.status.return_value = {
            "enabled": True, "running": False, "port": 5005, "error": "bind_failed",
        }
        svc = HealthCheckService(
            store=MagicMock(),
            health_checker=MagicMock(),
            startup_diagnostics=MagicMock(),
            integrity_checker=MagicMock(),
            rest_inprocess=fake_rest,
        )
        self.assertIs(fake_rest, svc._rest_inprocess)
        self.assertEqual(
            {"enabled": True, "running": False, "port": 5005, "error": "bind_failed"},
            svc._get_rest_inprocess_summary(),
        )

    # 5 ------------------------------------------------------------------
    def test_tombstone_is_distinguishable_from_disabled_and_alive(self) -> None:
        tombstone = _RestInProcessTombstone(
            enabled=True, port=5005, error="RuntimeError: boom",
        )
        svc = _make_service(rest_inprocess=tombstone)
        diag = svc.handle_get_diagnostics({})
        section = diag["rest_in_process"]

        # Схема дословно совпадает с остальными состояниями по общим ключам
        # (HealthCheckService не трогали — это её "прозрачный passthrough" на
        # status()), но словарь надгробия несёт ДОПОЛНИТЕЛЬНЫЙ ключ tombstone,
        # которого нет ни у "выключено" (коллаборатор None), ни у "жив".
        self.assertTrue(_SCHEMA_KEYS.issubset(section.keys()))
        self.assertIn("tombstone", section)
        self.assertIs(section["tombstone"], True)
        self.assertTrue(section["enabled"])
        self.assertFalse(section["running"])
        self.assertEqual(5005, section["port"])
        self.assertIn("boom", section["error"])

        disabled_section = _make_service(rest_inprocess=None).handle_get_diagnostics({})[
            "rest_in_process"
        ]
        self.assertNotIn("tombstone", disabled_section)
        self.assertNotEqual(disabled_section, section)


if __name__ == "__main__":
    unittest.main()
