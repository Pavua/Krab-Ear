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
        cfg = SimpleNamespace(REST_SERVER_PORT=_free_port())
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=False)
        self.assertFalse(srv.start())
        self.assertFalse(srv.status()["running"])
        self.assertIs(srv.status()["enabled"], False)
        srv.stop()

    def test_starts_and_serves_then_stops(self):
        port = _free_port()
        cfg = SimpleNamespace(REST_SERVER_PORT=port)
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=True)
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
            cfg = SimpleNamespace(REST_SERVER_PORT=port)
            srv = InProcessRestServer(
                app=_TinyApp(), settings=cfg, enabled=True,
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
        cfg = SimpleNamespace(REST_SERVER_PORT=_free_port())
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=True)
        srv.start()
        srv.stop()
        srv.stop()   # второй раз не должен бросать


    def test_stop_immediately_after_start_does_not_hang(self):
        """Регрессия на гонку stop/_serve (находка финального ревью волны).

        _serve раньше читал self._server из поля, которое stop() обнуляет под
        локом: при неудачном порядке тред выходил НЕ входя в serve_forever(),
        а stop() звал server.shutdown(), который ждёт выставляемое только из
        finally внутри serve_forever() событие — и ждёт БЕЗ таймаута. То есть
        stop() висел навсегда, унося с собой close() и штатное завершение
        backend (launchd добивал SIGKILL по ExitTimeOut).

        Тест бьёт именно в это окно: stop() сразу после start(), без паузы.
        Порог 20с — с большим запасом над барьером 2с + join 5с; при
        регрессии тест не уложится ни в какой порог, он висит вечно.
        """
        import time as _time

        for _ in range(15):
            cfg = SimpleNamespace(REST_SERVER_PORT=_free_port())
            srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=True)
            self.assertTrue(srv.start())
            t0 = _time.monotonic()
            srv.stop()
            elapsed = _time.monotonic() - t0
            self.assertLess(
                elapsed, 20.0,
                f"stop() занял {elapsed:.1f}с — похоже на зависание shutdown()",
            )
            self.assertFalse(srv.status()["running"])


    def test_enabled_param_is_source_of_truth_not_settings(self):
        """S3/Задача 4: конструктор больше не читает settings.REST_IN_PROCESS_ENABLED.

        cfg тут — обычный SimpleNamespace без этого атрибута вовсе (settings
        отвечает только за порт). Если бы конструктор всё ещё заглядывал в
        settings, здесь было бы AttributeError либо (при getattr с дефолтом)
        тихий возврат к False независимо от переданного enabled=True —
        именно двухголовый рубильник, который чинит задача.
        """
        cfg = SimpleNamespace(REST_SERVER_PORT=_free_port())
        self.assertFalse(hasattr(cfg, "REST_IN_PROCESS_ENABLED"))
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=True)
        self.assertIs(srv.status()["enabled"], True)
        srv.stop()


if __name__ == "__main__":
    unittest.main()
