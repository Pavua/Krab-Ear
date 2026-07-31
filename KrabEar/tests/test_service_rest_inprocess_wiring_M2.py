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
4. S3/Задача 4: сбой СБОРКИ REST-приложения (импорт/adopt_external_singletons/
   create_app()/конструктор) оставляет надгробие, а не ``None`` — до фикса
   диагностика на ``None`` отдавала словарь, байт в байт совпадающий с
   «рубильник выключен», и канарейка две недели видела бы штатную картину
   при мёртвом REST.
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

    def test_push_rest_error_startup_failed_has_error_severity(self) -> None:
        # S3/Задача 4: rest.startup_failed обязан быть в ERROR_REGISTRY с
        # severity "error" (в отличие от "warn" у rest.port_conflict) — там
        # REST хотя бы может подняться после освобождения порта, здесь чинить
        # нечего (см. _RestInProcessTombstone).
        self.service._push_rest_error("rest.startup_failed", "сборка упала")
        self.service._error_bus.push.assert_called_once()
        (pushed,), _kwargs = self.service._error_bus.push.call_args
        self.assertEqual(pushed.code, "rest.startup_failed")
        self.assertEqual(pushed.severity, "error")


class RestInProcessBuildFailureTestCase(unittest.TestCase):
    """S3/Задача 4: сбой сборки REST-приложения оставляет надгробие, не None.

    ``start()`` документирован как "НИКОГДА не бросает" и сам обрабатывает
    EADDRINUSE (fail-open внутри себя) — значит внешний ``except`` в
    ``service.py`` ловит только сбой СБОРКИ: импорт, adopt_external_singletons,
    create_app(), сам конструктор InProcessRestServer. Здесь имитируем именно
    это, а не конфликт порта (тот уже покрыт test_rest_inprocess_server_M2.py).
    """

    def setUp(self) -> None:
        self._settings_patch = patch.object(settings, "REST_IN_PROCESS_ENABLED", True)
        self._settings_patch.start()
        self.addCleanup(self._settings_patch.stop)

        # create_app() падает так, будто сборка Flask-приложения сломалась —
        # заведомо ДО попытки биндинга порта, то есть start() тут даже не
        # позовётся.
        self._create_app_patch = patch(
            "backend.rest_server.create_app",
            side_effect=RuntimeError("boom: flask assembly failed"),
        )
        self._create_app_patch.start()
        self.addCleanup(self._create_app_patch.stop)

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        # S3/Задача 9: runtime-ключ — ЕДИНСТВЕННОЕ реальное включение с этой
        # волны (см. test_rest_inprocess_runtime_toggle_S3_task9.py); голое
        # pydantic-поле выше остаётся как фоллбэк, который сюда не доходит,
        # т.к. DEFAULT_SETTINGS всегда содержит "rest_in_process_enabled".
        store.save_settings({"rest_in_process_enabled": True})
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()

    def test_build_failure_leaves_tombstone_not_none(self) -> None:
        self.assertIsNotNone(self.service._rest_inprocess)
        status = self.service._rest_inprocess.status()
        self.assertIs(status["tombstone"], True)
        self.assertIn("boom", status["error"])

    def test_build_failure_status_distinguishable_from_disabled(self) -> None:
        # Схема "выключенного" состояния (см. HealthCheckService._get_rest_inprocess_summary
        # fallback на None): {"enabled": False, "running": False, "port": None,
        # "error": None} — БЕЗ ключа "tombstone" вовсе. Надгробие обязано
        # отличаться и по ключу "tombstone", и по "enabled" (рубильник тут
        # включён владельцем, просто сборка упала).
        status = self.service._rest_inprocess.status()
        self.assertIn("tombstone", status)
        self.assertIs(status["enabled"], True)
        self.assertIs(status["running"], False)

    def test_build_failure_pushes_startup_failed_error_code(self) -> None:
        # _push_rest_error зовётся из except-ветки — проверяем через
        # публичный API ErrorBus.list_recent(), что она реально получила
        # rest.startup_failed (не молчание).
        recent = self.service._error_bus.list_recent()
        codes = [err.code for err in recent]
        self.assertIn("rest.startup_failed", codes)


class RestInProcessBuildFailureCallsPushErrorTestCase(unittest.TestCase):
    """Отдельный класс: патчит _push_rest_error ДО конструктора, чтобы
    детерминированно проверить сам факт вызова с правильным кодом — без
    зависимости от внутреннего устройства ErrorBus."""

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

        self._push_error_patch = patch.object(BackendService, "_push_rest_error")
        self.mock_push_error = self._push_error_patch.start()
        self.addCleanup(self._push_error_patch.stop)

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        # S3/Задача 9: runtime-ключ — единственное реальное включение.
        store.save_settings({"rest_in_process_enabled": True})
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()

    def test_build_failure_calls_push_rest_error_with_startup_failed(self) -> None:
        self.mock_push_error.assert_called_once()
        (code, detail), _kwargs = self.mock_push_error.call_args
        self.assertEqual(code, "rest.startup_failed")
        self.assertIn("boom", detail)


if __name__ == "__main__":
    unittest.main()
