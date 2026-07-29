"""M2: InProcessRestServer — старт/останов, выключенный рубильник, EADDRINUSE."""
import socket
import sys
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.rest_inprocess import InProcessRestServer  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _TinyApp:
    """Минимальное WSGI-приложение вместо настоящего Flask-app.

    Тест проверяет ТРАНСПОРТ (тред, порт, shutdown), а не REST-контракт —
    поднимать полный create_app() здесь значит тащить AudioEngine в юнит-тест.
    """

    def __call__(self, environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]


class InProcessRestServerTest(unittest.TestCase):
    def test_disabled_switch_does_not_start(self):
        cfg = SimpleNamespace(REST_IN_PROCESS_ENABLED=False, REST_SERVER_PORT=_free_port())
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg)
        self.assertFalse(srv.start())
        self.assertFalse(srv.status()["running"])
        self.assertIs(srv.status()["enabled"], False)
        srv.stop()

    def test_starts_and_serves_then_stops(self):
        port = _free_port()
        cfg = SimpleNamespace(REST_IN_PROCESS_ENABLED=True, REST_SERVER_PORT=port)
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg)
        self.assertTrue(srv.start())
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
            self.assertTrue(srv.status()["running"])
        finally:
            srv.stop()
        self.assertFalse(srv.status()["running"])

    def test_port_conflict_fails_open_and_pushes_error(self):
        port = _free_port()
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        pushed = []
        try:
            cfg = SimpleNamespace(REST_IN_PROCESS_ENABLED=True, REST_SERVER_PORT=port)
            srv = InProcessRestServer(
                app=_TinyApp(), settings=cfg,
                error_push=lambda code, detail: pushed.append((code, detail)),
            )
            self.assertFalse(srv.start())          # fail-open, НЕ исключение
            self.assertFalse(srv.status()["running"])
            self.assertIsNotNone(srv.status()["error"])
            self.assertEqual([c for c, _ in pushed], ["rest.port_conflict"])
            srv.stop()
        finally:
            blocker.close()

    def test_stop_is_idempotent(self):
        cfg = SimpleNamespace(REST_IN_PROCESS_ENABLED=True, REST_SERVER_PORT=_free_port())
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg)
        srv.start()
        srv.stop()
        srv.stop()   # второй раз не должен бросать


if __name__ == "__main__":
    unittest.main()
