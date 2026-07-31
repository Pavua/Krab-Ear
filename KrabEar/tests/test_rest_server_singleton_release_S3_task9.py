"""S3/Задача 9, п.3: `release_external_singletons()` и atexit-очистка.

Спека: docs/superpowers/specs/2026-07-31-s3-rest-flip-design.md §Р10.

``_rest_engine_cleanup`` (atexit, ``rest_server.py``) читало голое имя
``engine``. После ``adopt_external_singletons()`` (M2, in-process REST) это
уже объект ВЛАДЕЛЬЦА процесса (BackendService), а не собственный REST-движок
модуля — atexit закрывал бы GigaAM-адаптер backend'а посреди его собственного
жизненного цикла, а не свой. Фикс: ``release_external_singletons()`` — парная
операция к ``adopt_external_singletons()``, возвращающая module-level имена
обратно к собственному standalone-комплекту REST-модуля; atexit-очистка не
трогает усыновлённые объекты, пока флаг усыновления не снят.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_rest_server_singleton_release_S3_task9.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import backend.rest_server as rest_server  # noqa: E402


class _FakeAdapter:
    """Дублирует контракт настоящего GigaAM-адаптера в объёме, нужном тесту."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeRouter:
    def __init__(self, adapter: _FakeAdapter) -> None:
        self._adapter = adapter

    def get_gigaam_adapter(self) -> _FakeAdapter:
        return self._adapter


class _FakeEngineWithRouter:
    def __init__(self, adapter: _FakeAdapter) -> None:
        self._router = _FakeRouter(adapter)


class ReleaseExternalSingletonsTestCase(unittest.TestCase):
    """adopt/release пара восстанавливает module-level имена и флаг усыновления."""

    def setUp(self) -> None:
        # rest_server — process-wide синглтон-модуль (20 тест-файлов патчат
        # его напрямую), и часть из них (test_rest_adopt_singletons_M2.py)
        # восстанавливает глобалы вручную через setattr в обход
        # release_external_singletons() — флаг _singletons_adopted способен
        # застрять в True между тестами ОДНОГО чанка (живой пример: прогон
        # вместе со всеми test_rest_*.py). Форсируем известную чистую точку
        # старта здесь — release() идемпотентен, если усыновления не было —
        # и сверяем результат с АВТОРИТЕТНЫМ _OWN_SINGLETONS, а не со
        # снимком, который сам может быть чужим наследием пред. теста.
        rest_server.release_external_singletons()
        self._orig_engine = rest_server.engine
        self._orig_store = rest_server.store
        self._orig_transcriber = rest_server.transcriber
        self._orig_translator = rest_server.translator
        self._orig_tts_service = rest_server.tts_service

    def tearDown(self) -> None:
        rest_server.release_external_singletons()
        rest_server.engine = self._orig_engine
        rest_server.store = self._orig_store
        rest_server.transcriber = self._orig_transcriber
        rest_server.translator = self._orig_translator
        rest_server.tts_service = self._orig_tts_service

    def test_release_restores_original_globals_and_flag(self) -> None:
        fake_engine = MagicMock(name="owner_engine")
        fake_store = MagicMock(name="owner_store")
        fake_transcriber = MagicMock(name="owner_transcriber")
        fake_translator = MagicMock(name="owner_translator")
        fake_tts = MagicMock(name="owner_tts")

        rest_server.adopt_external_singletons(
            engine=fake_engine,
            store=fake_store,
            transcriber=fake_transcriber,
            translator=fake_translator,
            tts_service=fake_tts,
        )
        self.assertIs(rest_server.engine, fake_engine)
        self.assertTrue(rest_server._singletons_adopted)

        rest_server.release_external_singletons()

        self.assertIs(rest_server.engine, rest_server._OWN_SINGLETONS["engine"])
        self.assertIs(rest_server.store, rest_server._OWN_SINGLETONS["store"])
        self.assertIs(rest_server.transcriber, rest_server._OWN_SINGLETONS["transcriber"])
        self.assertIs(rest_server.translator, rest_server._OWN_SINGLETONS["translator"])
        self.assertIs(rest_server.tts_service, rest_server._OWN_SINGLETONS["tts_service"])
        self.assertFalse(rest_server._singletons_adopted)

    def test_release_without_prior_adopt_is_noop(self) -> None:
        rest_server._singletons_adopted = False
        # НЕ сравниваем с _OWN_SINGLETONS["engine"] — другие тест-файлы того
        # же чанка (test_rest_server.py:100-110) законно подменяют
        # rest_server.engine сырым присваиванием НАПРЯМУЮ, в обход
        # adopt/release-контракта, ради собственной изоляции при
        # module-level коллекции pytest. При выключенном флаге у release()
        # нет способа (и не должно быть) отличить такую стороннюю подмену от
        # штатного состояния — контракт no-op проверяем через
        # неизменность ТЕКУЩЕГО значения, каким бы оно ни было.
        current_engine = rest_server.engine
        try:
            rest_server.release_external_singletons()
        except Exception as exc:  # pragma: no cover - тест должен упасть, если это случится
            self.fail(f"release_external_singletons() бросил на no-op вызове: {exc}")
        self.assertIs(rest_server.engine, current_engine)
        self.assertFalse(rest_server._singletons_adopted)


class RestEngineCleanupSkipsAdoptedTestCase(unittest.TestCase):
    """_rest_engine_cleanup() (atexit) не закрывает усыновлённый (чужой) движок."""

    def setUp(self) -> None:
        self._orig_engine = rest_server.engine
        self._orig_adopted = rest_server._singletons_adopted

    def tearDown(self) -> None:
        rest_server.engine = self._orig_engine
        rest_server._singletons_adopted = self._orig_adopted

    def test_cleanup_skips_owner_adapter_when_adopted(self) -> None:
        owner_adapter = _FakeAdapter()
        rest_server.engine = _FakeEngineWithRouter(owner_adapter)
        rest_server._singletons_adopted = True

        rest_server._rest_engine_cleanup()

        self.assertFalse(
            owner_adapter.closed,
            "atexit не должен закрывать чужой (усыновлённый) адаптер",
        )

    def test_cleanup_closes_own_adapter_when_not_adopted(self) -> None:
        own_adapter = _FakeAdapter()
        rest_server.engine = _FakeEngineWithRouter(own_adapter)
        rest_server._singletons_adopted = False

        rest_server._rest_engine_cleanup()

        self.assertTrue(
            own_adapter.closed,
            "atexit обязан закрыть собственный адаптер REST-модуля, когда усыновления не было",
        )


if __name__ == "__main__":
    unittest.main()
