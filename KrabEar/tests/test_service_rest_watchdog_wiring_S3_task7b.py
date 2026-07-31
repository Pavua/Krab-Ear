"""S3/Задача 7b: проводка RestWatchdog внутри BackendService.

Спека: docs/superpowers/specs/2026-07-31-s3-rest-flip-design.md §Р6.
Образец фикстур — test_service_rest_inprocess_wiring_M2.py (тот же приём:
патчим create_app(), чтобы получить надгробие БЕЗ реального биндинга порта —
здесь это удобно вдвойне, т.к. RestWatchdog безопасно no-op'ит на надгробии
(п.5 задачи, уже в 7a) и не требует настоящей сети для проверки проводки).

Что проверяем:
1. Выключенный рубильник (дефолт) не создаёт RestWatchdog — симметрично
   self._rest_inprocess is None.
2. Включённый рубильник (даже с надгробием) создаёт RestWatchdog.
3. close() останавливает rest_watchdog ДО rest_inprocess (п.7 плана).
4. _shutdown_backend взводит терминальную защёлку сторожа САМЫМ ПЕРВЫМ шагом
   — раньше rest_inprocess.begin_shutdown() (см. отдельный тест в
   test_bounded_single_owner_shutdown_W1787.py — ordering-контракт самой
   функции; здесь проверяем именно факт проводки объекта в BackendService).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.rest_watchdog import RestWatchdog  # noqa: E402
from backend.service import BackendService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from core.config import settings  # noqa: E402
from tests.test_backend_service import (  # noqa: E402
    FakeRecorder,
    FakeTranscriber,
    FakeTranslator,
)


class RestWatchdogAbsentWhenSwitchOffTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._settings_patch = patch.object(settings, "REST_IN_PROCESS_ENABLED", False)
        self._settings_patch.start()
        self.addCleanup(self._settings_patch.stop)

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()

    def test_rest_watchdog_absent_when_switch_off(self) -> None:
        self.assertIsNone(self.service._rest_watchdog)


class RestWatchdogWiredWhenSwitchOnTestCase(unittest.TestCase):
    """Рубильник включён, сборка REST-приложения падает (надгробие) — сторож
    ВСЁ РАВНО конструируется и подключается к надгробию (п.5: "не лечить
    надгробие" — RestWatchdog умеет это сам, конструктор не обязан гадать)."""

    def setUp(self) -> None:
        self._settings_patch = patch.object(settings, "REST_IN_PROCESS_ENABLED", True)
        self._settings_patch.start()
        self.addCleanup(self._settings_patch.stop)

        self._create_app_patch = patch(
            "backend.rest_server.create_app",
            side_effect=RuntimeError("boom: flask assembly failed"),
        )
        self._create_app_patch.start()
        self.addCleanup(self._create_app_patch.stop)

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        # S3/Задача 9: runtime-ключ — единственное реальное включение с этой
        # волны (см. test_rest_inprocess_runtime_toggle_S3_task9.py); голое
        # pydantic-поле выше остаётся фоллбэком, недостижимым на практике.
        store.save_settings({"rest_in_process_enabled": True})
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()

    def test_rest_watchdog_constructed_and_wired_to_owner(self) -> None:
        self.assertIsNotNone(self.service._rest_watchdog)
        self.assertIsInstance(self.service._rest_watchdog, RestWatchdog)
        self.assertIs(self.service._rest_watchdog._owner, self.service._rest_inprocess)

    def test_rest_watchdog_wired_into_health_check_service(self) -> None:
        self.assertIs(
            self.service._rest_watchdog,
            self.service._health_check_svc._rest_watchdog,
        )
        diag = self.service._health_check_svc.handle_get_diagnostics({})
        self.assertIn("rest_watchdog", diag)

    def test_close_stops_rest_watchdog_before_rest_inprocess(self) -> None:
        events: list[str] = []
        watchdog = self.service._rest_watchdog
        rest_inprocess = self.service._rest_inprocess

        orig_watchdog_stop = watchdog.stop
        orig_rest_stop = rest_inprocess.stop

        def _spy_watchdog_stop():
            events.append("watchdog")
            return orig_watchdog_stop()

        def _spy_rest_stop(*a, **kw):
            events.append("rest_inprocess")
            return orig_rest_stop(*a, **kw)

        watchdog.stop = _spy_watchdog_stop
        rest_inprocess.stop = _spy_rest_stop

        self.service.close()

        self.assertEqual(events, ["watchdog", "rest_inprocess"])

    def test_close_does_not_raise_when_watchdog_stop_fails(self) -> None:
        self.service._rest_watchdog.stop = lambda: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        try:
            self.service.close()
        except Exception as exc:  # pragma: no cover - тест должен упасть, если это случится
            self.fail(f"close() не проглотил сбой RestWatchdog.stop(): {exc}")


if __name__ == "__main__":
    unittest.main()
