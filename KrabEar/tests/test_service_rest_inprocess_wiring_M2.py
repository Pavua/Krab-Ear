"""M2 Task 6: проводка in-process REST внутри BackendService.

Спека: docs/superpowers/specs/2026-07-16-m-series-rest-merge-design.md §4.2.

Что проверяем (НЕ живой сокет — его проверит отдельный смок):
1. Выключенный рубильник (дефолт) не создаёт InProcessRestServer — прод
   на двух процессах не затронут.
2. close() не падает и не оставляет сервер работающим.
3. _push_rest_error() строит валидный KrabError и передаёт его в ErrorBus —
   главный тест задачи. Плановый набросок звал ErrorBus.push() именованными
   аргументами; push() принимает ОБЪЕКТ KrabError, поэтому без этой проверки
   первый же реальный конфликт порта тихо упал бы на Pydantic-валидации
   внутри чужого треда (InProcessRestServer._serve), а fail-open стал бы
   fail-silent.
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

from backend.error_bus import KrabError  # noqa: E402
from backend.service import BackendService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from core.config import settings  # noqa: E402
from tests.test_backend_service import (  # noqa: E402
    FakeRecorder,
    FakeTranscriber,
    FakeTranslator,
)


class RestInProcessDefaultOffTestCase(unittest.TestCase):
    """Дефолтный рубильник REST_IN_PROCESS_ENABLED=False не должен ничего поднимать."""

    def setUp(self) -> None:
        # chunk-изоляция: settings — модульный синглтон, гарантируем известное
        # состояние рубильника независимо от того, что могли выставить другие
        # тестовые файлы в этом же процессе (см. CLAUDE.md про chunk pollution).
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
        # Обязательный close() — иначе daemon-треды BackendService роняют весь
        # файл чанка при выходе интерпретатора (feedback_backendservice_teardown_ci.md).
        self.service.close()

    def test_rest_inprocess_absent_when_switch_off(self) -> None:
        self.assertIsNone(self.service._rest_inprocess)

    def test_close_does_not_raise_and_leaves_stopped(self) -> None:
        # close() уже вызывается в tearDown; здесь проверяем, что повторный
        # вызов (идемпотентность) тоже не бросает и сервер остаётся не поднят.
        self.service.close()
        self.assertIsNone(self.service._rest_inprocess)


class PushRestErrorTestCase(unittest.TestCase):
    """Главный тест: _push_rest_error() строит валидный KrabError."""

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
        # Подменяем реальную ErrorBus на мок ПОСЛЕ конструирования, чтобы не
        # мешать остальной проводке __init__ (rewriter/transcriber/recorder
        # уже получили ссылку на настоящую шину до этой точки).
        self.service._error_bus = MagicMock()

    def tearDown(self) -> None:
        self.service.close()

    def test_push_rest_error_calls_error_bus_with_krab_error(self) -> None:
        self.service._push_rest_error("rest.port_conflict", "127.0.0.1:5005 занят: OSError")

        self.service._error_bus.push.assert_called_once()
        (pushed,), _kwargs = self.service._error_bus.push.call_args
        self.assertIsInstance(pushed, KrabError)
        self.assertEqual(pushed.component, "rest")
        self.assertEqual(pushed.code, "rest.port_conflict")
        self.assertEqual(pushed.severity, "warn")  # из ERROR_REGISTRY["rest.port_conflict"]
        self.assertIn("занят", pushed.message_debug)
        self.assertEqual(pushed.context, {"detail": "127.0.0.1:5005 занят: OSError"})

    def test_push_rest_error_never_raises_on_unknown_code(self) -> None:
        # Код вне ERROR_REGISTRY всё ещё обязан дойти до push() с безопасными
        # дефолтами — колбэк зовётся из чужого треда, бросать нельзя.
        try:
            self.service._push_rest_error("rest.made_up_code", "деталь")
        except Exception as exc:  # pragma: no cover - тест должен упасть, если это случится
            self.fail(f"_push_rest_error бросил исключение: {exc}")
        self.service._error_bus.push.assert_called_once()
        (pushed,), _kwargs = self.service._error_bus.push.call_args
        self.assertIsInstance(pushed, KrabError)
        self.assertEqual(pushed.component, "rest")

    def test_push_rest_error_swallows_error_bus_failure(self) -> None:
        # Если сама шина ошибок упала (например, Sentry-клиент за ней бросил) —
        # колбэк обязан проглотить исключение, а не уронить rest-inprocess тред.
        self.service._error_bus.push.side_effect = RuntimeError("шина недоступна")
        try:
            self.service._push_rest_error("rest.port_conflict", "деталь")
        except Exception as exc:  # pragma: no cover
            self.fail(f"_push_rest_error не проглотил сбой ErrorBus: {exc}")


if __name__ == "__main__":
    unittest.main()
