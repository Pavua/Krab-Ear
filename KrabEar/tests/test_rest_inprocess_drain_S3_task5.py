"""S3/Задача 5: дренаж запросов и владение флагом shutting_down.

Часть тестов бьёт напрямую в Flask-хуки через test_client() (белый ящик,
без сокетов — быстро и детерминированно). Часть поднимает реальный сервер
через InProcessRestServer.start()/stop() с настоящими HTTP-соединениями в
отдельном треде — только там и проверяется, что stop() действительно ждёт
активный запрос, но не ждёт долгоживущие стримы.
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

from backend.event_bus import EventBus  # noqa: E402
from backend.rest_inprocess import (  # noqa: E402
    REST_DRAIN_BUDGET_SEC,
    InProcessRestServer,
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class DrainHooksWhiteBoxTest(unittest.TestCase):
    """Проверяет 503-гейт и реестр напрямую через Flask test_client()."""

    def test_before_request_returns_503_when_shutting_down(self):
        app = Flask(__name__)

        @app.route("/x")
        def _x():
            return "ok"

        cfg = SimpleNamespace(REST_SERVER_PORT=_free_port())
        srv = InProcessRestServer(app=app, settings=cfg, enabled=True)
        srv.begin_shutdown()

        resp = app.test_client().get("/x")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json(), {"error": "shutting_down"})

    def test_streaming_paths_are_not_counted_in_active_registry(self):
        """Реестр НЕ считает долгоживущие стримы (их три, не два)."""
        app = Flask(__name__)
        counts: dict[str, int] = {}

        @app.route("/v1/events")
        def _sse():
            counts["events"] = srv._active_requests
            return "stream"

        @app.route("/v1/stream")
        def _ws_stream():
            counts["stream"] = srv._active_requests
            return "stream"

        @app.route("/ws/events")
        def _ws_events():
            counts["ws_events"] = srv._active_requests
            return "stream"

        @app.route("/x")
        def _normal():
            counts["x"] = srv._active_requests
            return "ok"

        cfg = SimpleNamespace(REST_SERVER_PORT=_free_port())
        srv = InProcessRestServer(app=app, settings=cfg, enabled=True)
        client = app.test_client()

        client.get("/v1/events")
        client.get("/v1/stream")
        client.get("/ws/events")
        client.get("/x")

        self.assertEqual(counts["events"], 0, "SSE /v1/events не должен считаться")
        self.assertEqual(counts["stream"], 0, "WS /v1/stream не должен считаться")
        self.assertEqual(counts["ws_events"], 0, "WS /ws/events не должен считаться")
        self.assertEqual(counts["x"], 1, "обычный путь обязан считаться во время обработки")
        self.assertEqual(srv._active_requests, 0, "реестр обязан опустеть после всех teardown")


class ShuttingDownFlagOwnershipTest(unittest.TestCase):
    """S3/Задача 5, п.1: владение флагом между stop() и start()."""

    def test_flag_is_cleared_after_start(self):
        """Регрессия на «зомби, невидимый для собственного сторожа».

        Симулирует restart() = stop() + start() (лечение сторожа, задача 7):
        без сброса флага в start() REST навсегда отвечал бы 503 после первого
        же лечения, включая /health.
        """
        port = _free_port()
        app = Flask(__name__)

        @app.route("/x")
        def _x():
            return "ok"

        cfg = SimpleNamespace(REST_SERVER_PORT=port)
        srv = InProcessRestServer(app=app, settings=cfg, enabled=True)
        self.assertTrue(srv.start())
        srv.stop()  # взводит shutting_down

        self.assertTrue(srv.start())  # restart(): stop() + start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/x", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
        finally:
            srv.stop()


class DrainOverSocketTest(unittest.TestCase):
    """Реальные HTTP-соединения через настоящий сервер (не test_client())."""

    def test_stop_drains_active_request_before_closing(self):
        """Дренаж дожидается активного (не-стримингового) запроса."""
        port = _free_port()
        app = Flask(__name__)
        entered = threading.Event()

        @app.route("/slow")
        def _slow():
            entered.set()
            time.sleep(0.3)
            return "ok"

        cfg = SimpleNamespace(REST_SERVER_PORT=port)
        srv = InProcessRestServer(app=app, settings=cfg, enabled=True)
        self.assertTrue(srv.start())

        result: dict = {}

        def _hit():
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/slow", timeout=5
                ) as resp:
                    result["status"] = resp.status
            except Exception as exc:  # pragma: no cover - защитная диагностика
                result["error"] = exc

        t = threading.Thread(target=_hit)
        t.start()
        self.assertTrue(entered.wait(timeout=5), "запрос не успел войти в обработчик")

        t0 = time.monotonic()
        srv.stop()
        elapsed = time.monotonic() - t0
        t.join(timeout=5)

        self.assertNotIn("error", result, result.get("error"))
        self.assertEqual(result.get("status"), 200)
        self.assertGreaterEqual(
            elapsed, 0.2,
            "stop() вернулся быстрее длительности запроса — похоже, дренаж не ждал",
        )

    def test_stop_does_not_wait_for_streaming_path(self):
        """Дренаж НЕ ждёт долгоживущий стрим — иначе выгорал бы весь бюджет."""
        port = _free_port()
        app = Flask(__name__)
        entered = threading.Event()
        release = threading.Event()

        @app.route("/v1/events")
        def _sse():
            entered.set()
            release.wait(timeout=10)
            return "done"

        cfg = SimpleNamespace(REST_SERVER_PORT=port)
        srv = InProcessRestServer(app=app, settings=cfg, enabled=True)
        self.assertTrue(srv.start())

        t = threading.Thread(
            target=lambda: urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/events", timeout=15
            ).read()
        )
        t.start()
        self.assertTrue(entered.wait(timeout=5), "стрим не успел войти в обработчик")

        t0 = time.monotonic()
        srv.stop()
        elapsed = time.monotonic() - t0

        release.set()
        t.join(timeout=10)

        self.assertLess(
            elapsed, REST_DRAIN_BUDGET_SEC + 2.0,
            "stop() завис на подключённом SSE-клиенте вместо немедленного возврата",
        )


class SentinelDoubleBroadcastTest(unittest.TestCase):
    """S3/Задача 5, п.7: shutdown_handler разошлёт сентинел ПОВТОРНО."""

    def test_broadcast_shutdown_sentinel_is_safe_when_called_twice(self):
        bus = EventBus()
        q = bus.subscribe()

        sent_first = bus.broadcast_shutdown_sentinel()
        self.assertEqual(sent_first, 1)
        self.assertIsNone(q.get_nowait())
        bus.unsubscribe(q)

        # Тот же вызов, что shutdown_handler._broadcast_event_bus_sentinel
        # сделает СЛЕДУЮЩИМ шагом после нашего раннего сентинела из stop().
        sent_second = bus.broadcast_shutdown_sentinel()
        self.assertEqual(sent_second, 0)

    def test_inprocess_server_sentinel_push_survives_repeat_stop(self):
        """stop() не бросает, даже если sentinel_push уже вызывался ранее."""
        calls: list[int] = []
        bus = EventBus()

        def _push():
            calls.append(1)
            return bus.broadcast_shutdown_sentinel()

        cfg = SimpleNamespace(REST_SERVER_PORT=_free_port())
        srv = InProcessRestServer(
            app=Flask(__name__), settings=cfg, enabled=True, sentinel_push=_push,
        )
        srv.start()
        srv.stop()
        srv.stop()  # идемпотентность: второй stop() не имеет сервера — sentinel_push не зовётся снова

        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
