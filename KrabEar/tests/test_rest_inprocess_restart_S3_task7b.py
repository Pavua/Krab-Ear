"""S3/Задача 7b: контракт ``ever_served`` и неблокирующий ``restart()``.

Спека: docs/superpowers/specs/2026-07-31-s3-rest-flip-design.md §Р6.
План: docs/superpowers/plans/2026-07-31-s3-inprocess-rest-enable.md,
задачи 7a/7b, п.6.

Стиль — как у соседних test_rest_inprocess_*_M2/S3_task5.py: реальный сокет
на свободном порту, минимальное WSGI-приложение вместо полного create_app().
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from flask import Flask  # noqa: E402

from backend.rest_inprocess import (  # noqa: E402
    RESTART_STOP_DEADLINE_SEC,
    InProcessRestServer,
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _TinyApp:
    """Минимальное WSGI-приложение — тест бьёт в транспорт, не в REST-контракт."""

    def __call__(self, environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]


class EverServedFlagTest(unittest.TestCase):
    """Контракт RestWatchdog: status()["ever_served"] — "был живой serve"."""

    def test_false_before_start(self):
        cfg = SimpleNamespace(REST_SERVER_PORT=_free_port())
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=True)
        self.assertFalse(srv.status()["ever_served"])

    def test_true_after_successful_start(self):
        port = _free_port()
        cfg = SimpleNamespace(REST_SERVER_PORT=port)
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=True)
        self.assertTrue(srv.start())
        try:
            deadline = time.monotonic() + 2.0
            while not srv.status()["ever_served"] and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(srv.status()["ever_served"])
        finally:
            srv.stop()

    def test_stays_true_after_stop(self):
        # "хотя бы раз за жизнь процесса" — не "сейчас": сторож обязан лечить
        # и REST, который в данный момент не running, но когда-то поднимался.
        port = _free_port()
        cfg = SimpleNamespace(REST_SERVER_PORT=port)
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=True)
        self.assertTrue(srv.start())
        deadline = time.monotonic() + 2.0
        while not srv.status()["ever_served"] and time.monotonic() < deadline:
            time.sleep(0.01)
        srv.stop()
        self.assertTrue(srv.status()["ever_served"])

    def test_false_when_port_conflict_prevents_start(self):
        # Краевой случай, который сторож сознательно НЕ чинит (см. докстринг
        # rest_watchdog.py): конфликт порта на старте — ever_served остаётся
        # False навсегда для этого экземпляра.
        port = _free_port()
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        try:
            cfg = SimpleNamespace(REST_SERVER_PORT=port)
            srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=True)
            self.assertFalse(srv.start())
            self.assertFalse(srv.status()["ever_served"])
        finally:
            blocker.close()


class RestartHealsAndServesAgainTest(unittest.TestCase):
    def test_restart_returns_true_and_server_serves_again(self):
        port = _free_port()
        cfg = SimpleNamespace(REST_SERVER_PORT=port)
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=True)
        self.assertTrue(srv.start())
        try:
            self.assertTrue(srv.restart())
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
        finally:
            srv.stop()

    def test_after_successful_restart_probe_gets_2xx_not_zombied_503(self):
        """🔴 Обязательный тест 7b (план, раздел «Задачи 7a и 7b»).

        Без сброса shutting_down в start() REST навсегда отвечал бы 503 на
        ВСЁ, включая /health, после первого же лечения — а сторож по
        контракту принимает любой HTTP-ответ (в т.ч. 503) за доказательство
        жизни и больше никогда не лечит. Проверяется через ПОЛНЫЙ restart()
        (не голый stop()+start(), как в
        test_rest_inprocess_drain_S3_task5.py::ShuttingDownFlagOwnershipTest)
        — то есть той же дорогой, которой реально ходит RestWatchdog.
        """
        port = _free_port()
        app = Flask(__name__)

        @app.route("/health")
        def _health():
            return "ok"

        cfg = SimpleNamespace(REST_SERVER_PORT=port)
        srv = InProcessRestServer(app=app, settings=cfg, enabled=True)
        self.assertTrue(srv.start())
        try:
            self.assertTrue(srv.restart())
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=5
            ) as resp:
                self.assertEqual(resp.status, 200)
        finally:
            srv.stop()


class RestartDoesNotHangTest(unittest.TestCase):
    """п.6 плана: stop() ВНУТРИ restart() на отдельном треде с общим deadline."""

    def test_restart_does_not_hang_when_stop_is_wedged(self):
        port = _free_port()
        cfg = SimpleNamespace(REST_SERVER_PORT=port)
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=True)
        self.assertTrue(srv.start())
        # Симулируем зависший server.shutdown() — тот самый отказ, который
        # restart() обязан лечить, а не унаследовать. Без деталей: реальный
        # serve_forever() застрял бы аналогично на своём собственном отказе.
        srv._server.shutdown = lambda: threading.Event().wait()

        t0 = time.monotonic()
        ok = srv.restart()
        elapsed = time.monotonic() - t0

        self.assertFalse(ok)
        self.assertLess(
            elapsed, RESTART_STOP_DEADLINE_SEC + 2.0,
            f"restart() завис на {elapsed:.1f}с вместо возврата по deadline",
        )

    def test_restart_on_never_started_server_starts_it(self):
        # restart() = stop() + start(); stop() на никогда не запущенном
        # сервере — идемпотентный no-op (см. test_rest_inprocess_server_M2.py
        # ::test_stop_is_idempotent), start() поднимает его штатно.
        port = _free_port()
        cfg = SimpleNamespace(REST_SERVER_PORT=port)
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=True)
        try:
            self.assertTrue(srv.restart())
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
        finally:
            srv.stop()

    def test_restart_on_disabled_switch_returns_false(self):
        cfg = SimpleNamespace(REST_SERVER_PORT=_free_port())
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=False)
        self.assertFalse(srv.restart())


if __name__ == "__main__":
    unittest.main()
