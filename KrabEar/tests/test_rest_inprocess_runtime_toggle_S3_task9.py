"""S3/Задача 9, п.1: приоритет включения in-process REST.

Спека: docs/superpowers/specs/2026-07-31-s3-rest-flip-design.md §Р10.

До первого фикса ``service.py`` читал ТОЛЬКО статическое pydantic-поле
``REST_IN_PROCESS_ENABLED`` — владелец включал ключ через
``set_settings({"rest_in_process_enabled": True})``, IPC рапортовал успех,
но ни один участок прод-кода этот runtime-ключ не читал (тот же класс бага,
что паттерн волны 58, раздел "Runtime vs static settings reads" в CLAUDE.md).

Первый фикс поменял приоритет на runtime-ключ первым — но ``DEFAULT_SETTINGS``
ВСЕГДА содержит ``rest_in_process_enabled``, поэтому переменная окружения
``KRAB_EAR_REST_IN_PROCESS_ENABLED`` (документированный механизм проекта,
CLAUDE.md "Config override") стала недостижимой для этого конкретного ключа —
живая проверка на проде это подтвердила.

Итоговый приоритет (см. комментарий в ``service.py`` рядом с ``_rest_enabled``):
явная переменная окружения → runtime-ключ из settings.json → pydantic-дефолт.
Переменная окружения — аварийный рычаг уровня launchd-plist, обязана
перебивать пользовательскую настройку (тот же принцип, что
``settings_service._ENV_PINNED_SETTINGS`` для ``ipc_signing_secret``).

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_rest_inprocess_runtime_toggle_S3_task9.py -v
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.service import BackendService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from core.config import settings  # noqa: E402
from tests.test_backend_service import (  # noqa: E402
    FakeRecorder,
    FakeTranscriber,
    FakeTranslator,
)

_ENV_VAR = "KRAB_EAR_REST_IN_PROCESS_ENABLED"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _TinyApp:
    """Минимальное WSGI-приложение вместо настоящего create_app() (образец M2).

    Тест проверяет ПРОВОДКУ рубильника (что REST реально стартует и слушает
    сокет), а не REST-контракт — поднимать полный create_app() значит тащить
    AudioEngine в юнит-тест.
    """

    def __call__(self, environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]


class RuntimeSettingAloneStartsRestTestCase(unittest.TestCase):
    """rest_in_process_enabled=True в settings.json поднимает живой REST,
    даже когда статическое pydantic-поле REST_IN_PROCESS_ENABLED=False —
    ПРИ ОТСУТСТВУЮЩЕЙ переменной окружения (см. EnvVarPinsOverRuntimeKey
    ниже для случая, когда переменная задана)."""

    def setUp(self) -> None:
        self._port = _free_port()
        # Гарантируем отсутствие пина независимо от окружения, в котором
        # запущен тест — иначе тест из этого класса стал бы недетерминирован
        # на машине/CI с уже выставленной переменной.
        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        os.environ.pop(_ENV_VAR, None)

        self._settings_patch = patch.object(settings, "REST_IN_PROCESS_ENABLED", False)
        self._settings_patch.start()
        self.addCleanup(self._settings_patch.stop)
        self._port_patch = patch.object(settings, "REST_SERVER_PORT", self._port)
        self._port_patch.start()
        self.addCleanup(self._port_patch.stop)

        self._create_app_patch = patch(
            "backend.rest_server.create_app", return_value=_TinyApp()
        )
        self._create_app_patch.start()
        self.addCleanup(self._create_app_patch.stop)

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        # Единственное включение — runtime-ключ владельца, ДО конструктора.
        store.save_settings({"rest_in_process_enabled": True})

        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()

    def test_runtime_setting_alone_starts_real_server(self) -> None:
        self.assertIsNotNone(self.service._rest_inprocess)
        status = self.service._rest_inprocess.status()
        self.assertTrue(status["running"])
        # Живая проверка — реальный сокет отвечает, а не просто флаг True.
        with urllib.request.urlopen(f"http://127.0.0.1:{self._port}/", timeout=2) as resp:
            self.assertEqual(resp.status, 200)


class StaticFieldAloneDoesNotEnableWhenEnvUnsetTestCase(unittest.TestCase):
    """Голое pydantic-поле True БЕЗ runtime-ключа И БЕЗ переменной окружения
    не включает REST — DEFAULT_SETTINGS всегда даёт rest_in_process_enabled=
    False, и это побеждает статический дефолт pydantic-поля, ПОКА переменная
    окружения не задана (иначе см. EnvVarPinsOverRuntimeKey ниже — так этот
    тест был сформулирован НЕЧЕСТНО до ревью: он не проверял направление
    "нет переменной", а звучал как будто pydantic-поле никогда не решает."""

    def setUp(self) -> None:
        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        os.environ.pop(_ENV_VAR, None)

        self._settings_patch = patch.object(settings, "REST_IN_PROCESS_ENABLED", True)
        self._settings_patch.start()
        self.addCleanup(self._settings_patch.stop)

        # create_app() не должен быть вызван вовсе — если тест это увидит,
        # значит рубильник всё ещё читает голое pydantic-поле напрямую.
        self._create_app_patch = patch(
            "backend.rest_server.create_app",
            side_effect=AssertionError(
                "create_app() не должен вызываться без runtime-ключа rest_in_process_enabled"
            ),
        )
        self._create_app_patch.start()
        self.addCleanup(self._create_app_patch.stop)

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        # Runtime-ключ намеренно НЕ выставлен — DEFAULT_SETTINGS даёт False.

        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()

    def test_static_field_alone_is_not_enough_when_env_unset(self) -> None:
        self.assertIsNone(self.service._rest_inprocess)


class EnvVarPinsOverRuntimeKeyTestCase(unittest.TestCase):
    """KRAB_EAR_REST_IN_PROCESS_ENABLED — аварийный рычаг уровня launchd-
    plist. Когда переменная задана, она перебивает runtime-ключ settings.json
    в ОБЕ стороны: включает вопреки runtime=False и выключает вопреки
    runtime=True. Тот же принцип пиннинга, что
    settings_service._ENV_PINNED_SETTINGS для ipc_signing_secret (там —
    запрет перезаписи через set_settings; здесь — приоритет чтения на
    старте BackendService)."""

    def test_env_true_overrides_runtime_key_false(self) -> None:
        port = _free_port()
        env_patch = patch.dict(os.environ, {_ENV_VAR: "1"})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        # settings.REST_IN_PROCESS_ENABLED уже отражал бы переменную окружения
        # в реальном процессе (pydantic env_prefix="KRAB_EAR_") — здесь
        # выставляем это явно, т.к. синглтон settings сконструирован ДО
        # запуска теста и не перечитывает окружение постфактум.
        settings_patch = patch.object(settings, "REST_IN_PROCESS_ENABLED", True)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        port_patch = patch.object(settings, "REST_SERVER_PORT", port)
        port_patch.start()
        self.addCleanup(port_patch.stop)
        create_app_patch = patch(
            "backend.rest_server.create_app", return_value=_TinyApp()
        )
        create_app_patch.start()
        self.addCleanup(create_app_patch.stop)

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        # Runtime-ключ явно противоречит переменной окружения — она обязана
        # победить.
        store.save_settings({"rest_in_process_enabled": False})

        service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )
        self.addCleanup(service.close)

        self.assertIsNotNone(service._rest_inprocess)
        self.assertTrue(service._rest_inprocess.status()["running"])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as resp:
            self.assertEqual(resp.status, 200)

    def test_env_false_overrides_runtime_key_true(self) -> None:
        env_patch = patch.dict(os.environ, {_ENV_VAR: "0"})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        settings_patch = patch.object(settings, "REST_IN_PROCESS_ENABLED", False)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        # create_app() не должен быть вызван вовсе — переменная окружения
        # запрещает REST, даже когда runtime-ключ говорит "включить".
        create_app_patch = patch(
            "backend.rest_server.create_app",
            side_effect=AssertionError(
                "create_app() не должен вызываться — переменная окружения "
                "запрещает REST вопреки runtime-ключу"
            ),
        )
        create_app_patch.start()
        self.addCleanup(create_app_patch.stop)

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        # Runtime-ключ явно противоречит переменной окружения — она обязана
        # победить.
        store.save_settings({"rest_in_process_enabled": True})

        service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )
        self.addCleanup(service.close)

        self.assertIsNone(service._rest_inprocess)


if __name__ == "__main__":
    unittest.main()
