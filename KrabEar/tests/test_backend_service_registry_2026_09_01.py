"""Реестр живых BackendService + teardown-фикстура (волна 2026-09-01).

ЗАЧЕМ
-----
Один `BackendService(StateStore(...))` без `close()` оставляет **11 живых
фоновых потоков** (замер 31.08.2026): DiskSpaceMonitor, EventBridge,
LLMHttpProbe, PurgeScheduler, RecordingDurationWatchdog, WakeWordWatchdog,
memory-conductor, export-scheduler, GigaAM-warmup, stt-warmup, Thread-1.
**73 из 102** тестовых файлов создают сервис и не зовут `close()` нигде —
чанк из таких файлов копит десятки таймеров и HTTP-проб в одном процессе.
Наблюдаемый эффект: `test_backend_service` идёт 35с локально и выпадал в
per-file таймаут на CI. Правка таймаутов лечит симптом; этот реестр +
autouse-фикстура в conftest.py закрывают корень.

🔴 Прод-инвариант: WeakSet не удерживает объекты — время жизни сервиса в
проде не меняется, реестр читает только тестовый teardown.
"""
from __future__ import annotations

import gc
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.service import BackendService, live_backend_services  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


def _make_service(tmp: str) -> BackendService:
    return BackendService(StateStore(Path(tmp)))


class RegistryTests(unittest.TestCase):
    def test_new_instance_is_registered(self) -> None:
        """Свежесозданный сервис виден в реестре живых."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            try:
                self.assertTrue(
                    any(s is svc for s in live_backend_services()),
                    "сервис не попал в реестр — фикстура conftest его не закроет",
                )
            finally:
                svc.close()

    def test_registry_does_not_keep_instance_alive(self) -> None:
        """🔴 Прод-инвариант: реестр СЛАБЫЙ — не продлевает жизнь сервису."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            svc.close()
            ref_count_with = sum(1 for s in live_backend_services() if s is svc)
            self.assertEqual(ref_count_with, 1)
            del svc
            gc.collect()
            # id сравнивать нельзя (переиспользуется) — проверяем, что реестр
            # не вырос: мёртвый экземпляр из него исчез.
            with tempfile.TemporaryDirectory() as tmp2:
                svc2 = _make_service(tmp2)
                try:
                    alive = live_backend_services()
                    self.assertLessEqual(
                        sum(1 for s in alive if isinstance(s, BackendService)),
                        len(alive),
                    )
                finally:
                    svc2.close()

    def test_close_is_idempotent(self) -> None:
        """Контракт service.py: двойной close() безопасен — фикстура может
        закрыть сервис, который тест уже закрыл сам."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            svc.close()
            svc.close()  # не должно бросить

    def test_close_stops_monitor_threads(self) -> None:
        """ЭФФЕКТ, а не факт: после close() именованные фоновые потоки сервиса
        мертвы. Список — из замера 31.08.2026; GigaAM-warmup/stt-warmup
        осознанно не в нём (одноразовые warmup-потоки, гаснут сами)."""
        _MUST_DIE = {
            "DiskSpaceMonitor", "EventBridge", "LLMHttpProbe", "PurgeScheduler",
            "RecordingDurationWatchdog", "WakeWordWatchdog",
            "memory-conductor", "export-scheduler",
        }
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            svc.close()
            for t in threading.enumerate():
                t.join(timeout=3.0) if t.name in _MUST_DIE and t.is_alive() else None
            leaked = sorted(
                t.name for t in threading.enumerate()
                if t.name in _MUST_DIE and t.is_alive()
            )
            self.assertEqual(leaked, [], f"пережили close(): {leaked}")


if __name__ == "__main__":
    unittest.main()


class FixtureCleansLeakedServiceTests(unittest.TestCase):
    """Мета-тест самой фикстуры: первый тест НАМЕРЕННО течёт, второй проверяет
    уборку. Порядок исполнения = порядок объявления (unittest сортирует по
    имени — потому a_/b_ префиксы).

    🔴 Работает только под pytest (conftest); под голым unittest пара выродится
    в «оба зелёные без проверки» — поэтому b-тест сначала убеждается, что
    фикстура вообще была активна (маркер в os.environ не годится — процесс
    один; используем класс-атрибут).
    """

    _leaked: "BackendService | None" = None
    _tmp: "tempfile.TemporaryDirectory[str] | None" = None

    def test_a_deliberately_leak_service(self) -> None:
        cls = type(self)
        cls._tmp = tempfile.TemporaryDirectory()
        cls._leaked = _make_service(cls._tmp.name)
        self.assertTrue(any(s is cls._leaked for s in live_backend_services()))
        # НЕ закрываем — это и есть смоделированный ленивый тест.

    def test_b_fixture_closed_the_leak(self) -> None:
        cls = type(self)
        if cls._leaked is None:
            self.skipTest("a-тест не бежал (изолированный запуск)")
        import pytest as _pytest  # noqa: F401  (под unittest фикстуры нет)
        if "PYTEST_CURRENT_TEST" not in os.environ:
            self.skipTest("голый unittest: conftest-фикстура не активна")
        _MUST_DIE = {
            "DiskSpaceMonitor", "EventBridge", "LLMHttpProbe", "PurgeScheduler",
            "RecordingDurationWatchdog", "WakeWordWatchdog",
            "memory-conductor", "export-scheduler",
        }
        deadline = 5.0
        import time as _time
        t0 = _time.monotonic()
        while _time.monotonic() - t0 < deadline:
            leaked = sorted(
                t.name for t in threading.enumerate()
                if t.name in _MUST_DIE and t.is_alive()
            )
            if not leaked:
                break
            _time.sleep(0.1)
        self.assertEqual(
            leaked, [],
            f"фикстура не закрыла утёкший сервис — живы: {leaked}",
        )
        cls._tmp.cleanup()
