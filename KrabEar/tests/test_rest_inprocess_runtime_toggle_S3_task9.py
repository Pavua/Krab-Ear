"""S3/Задача 9, п.1: `rest_in_process_enabled` из settings.json реально включает REST.

Спека: docs/superpowers/specs/2026-07-31-s3-rest-flip-design.md §Р10.

До фикса ``service.py`` читал ТОЛЬКО статическое pydantic-поле
``REST_IN_PROCESS_ENABLED`` — владелец включал ключ через
``set_settings({"rest_in_process_enabled": True})``, IPC рапортовал успех,
но ни один участок прод-кода этот runtime-ключ не читал (тот же класс бага,
что паттерн волны 58, раздел "Runtime vs static settings reads" в CLAUDE.md).

Для этой волны ключ ``rest_in_process_enabled`` — ОСНОВНОЙ способ включения
(дефолт не меняется). Тесты доказывают обратное направление тоже: голое
pydantic-поле без runtime-ключа больше НЕ включает REST — иначе рубильник
остался бы двухголовым, только с перевёрнутым приоритетом.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_rest_inprocess_runtime_toggle_S3_task9.py -v
"""

from __future__ import annotations

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
    даже когда статическое pydantic-поле REST_IN_PROCESS_ENABLED=False."""

    def setUp(self) -> None:
        self._port = _free_port()
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


class StaticFieldAloneDoesNotEnableTestCase(unittest.TestCase):
    """Обратное направление: pydantic-поле True БЕЗ runtime-ключа больше не
    включает REST — DEFAULT_SETTINGS всегда даёт rest_in_process_enabled=False,
    и это должно побеждать статический дефолт pydantic-поля."""

    def setUp(self) -> None:
        self._settings_patch = patch.object(settings, "REST_IN_PROCESS_ENABLED", True)
        self._settings_patch.start()
        self.addCleanup(self._settings_patch.stop)

        # create_app() не должен быть вызван вовсе — если тест это увидит,
        # значит рубильник всё ещё читает голое pydantic-поле.
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

    def test_static_field_alone_is_not_enough(self) -> None:
        self.assertIsNone(self.service._rest_inprocess)


if __name__ == "__main__":
    unittest.main()
