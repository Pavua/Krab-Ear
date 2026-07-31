"""S3/финальное ревью, Фикс 1: терминальная защёлка держит restart() в полёте.

Спека: docs/superpowers/specs/2026-07-31-s3-rest-flip-design.md §Р6.

Найденный баг (см. begin_shutdown()/start() в rest_inprocess.py ДО фикса):
``start()`` БЕЗУСЛОВНО делал ``self._shutting_down.clear()``, а комментарий
рядом утверждал, что терминальность обеспечена СНАРУЖИ ("после начала
останова процесса start() уже никто не позовёт"). Это неверно для
``restart()`` (rest_inprocess.py) — та живёт до ``RESTART_STOP_DEADLINE_SEC``
на ОТДЕЛЬНОМ треде сторожа и внутри безусловно зовёт ``self.start()`` уже
ПОСЛЕ того, как её собственный ``stop()`` дождался/сдался. Если SIGTERM (и
``RestWatchdog.stop()`` с ``join(timeout=2.0)``, который НЕ дожидается
идущего restart()) приходит в это окно, ``_shutdown_backend``/``close()``
успевают дойти до ``rest_inprocess.stop()``/``release_external_singletons()``,
а затем "зомби"-``restart()`` вызывает ``self.start()`` и поднимает Flask
заново — уже над полузакрытым/закрытым backend'ом.

Фикс: отдельный НЕОБРАТИМЫЙ ``threading.Event`` (``_terminal``), взводимый в
``begin_shutdown()``, проверяемый ПЕРВЫМ действием в ``start()`` — вместо
безусловного ``clear()``. ``_terminal`` — НЕ то же самое, что переиспользуемый
``_shutting_down`` (тот сбрасывается в start() после штатного лечения).
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from flask import Flask  # noqa: E402

from backend.rest_inprocess import InProcessRestServer  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _TinyApp:
    def __call__(self, environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]


class TerminalLatchBlocksStartTest(unittest.TestCase):
    def test_start_returns_false_after_begin_shutdown(self):
        port = _free_port()
        cfg = SimpleNamespace(REST_SERVER_PORT=port)
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=True)
        srv.begin_shutdown()

        self.assertFalse(srv.start())
        self.assertFalse(srv.status()["running"])

    def test_start_does_not_reopen_socket_after_stop_and_begin_shutdown(self):
        """Регрессия основного сценария: stop() (терминальный, из финального
        teardown) взводит begin_shutdown() внутри себя — последующий start()
        обязан оставаться no-op, порт не должен снова слушать."""
        port = _free_port()
        cfg = SimpleNamespace(REST_SERVER_PORT=port)
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=True)
        self.assertTrue(srv.start())
        try:
            srv.begin_shutdown()  # эмулирует _shutdown_backend
            srv.stop()  # финальный останов, идёт из close()

            self.assertFalse(srv.start(), "start() обязан отказаться после терминальной защёлки")

            with self.assertRaises((urllib.error.URLError, ConnectionRefusedError, OSError)):
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
        finally:
            srv.stop()

    def test_restart_in_flight_during_shutdown_does_not_revive_server(self):
        """Живой сценарий Фикса 1: restart() зовёт start() ПОСЛЕ того, как
        begin_shutdown() уже взведена конкурентно (гонка сторожа с teardown).
        Терминальная защёлка обязана победить независимо от порядка гонки —
        start() не должен снова открыть слушающий сокет."""
        port = _free_port()
        cfg = SimpleNamespace(REST_SERVER_PORT=port)
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=True)
        self.assertTrue(srv.start())
        try:
            # Симулируем "restart() в полёте": stop() уже отработал (как
            # первая половина restart()), но до вызова start() (вторая
            # половина) backend успел взвести терминальную защёлку.
            srv.stop()
            srv.begin_shutdown()

            revived = srv.start()

            self.assertFalse(revived, "start() внутри restart() обязан деградировать в no-op")
            with self.assertRaises((urllib.error.URLError, ConnectionRefusedError, OSError)):
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
        finally:
            srv.stop()

    def test_shutting_down_flag_and_terminal_flag_are_distinct(self):
        """_terminal != _shutting_down: до begin_shutdown() штатный
        stop()+start() (лечение сторожа) обязан по-прежнему сбрасывать
        shutting_down и поднимать сервер заново — терминальная защёлка не
        должна ложно сработать на переиспользуемом lifecycle."""
        port = _free_port()
        cfg = SimpleNamespace(REST_SERVER_PORT=port)
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg, enabled=True)
        self.assertTrue(srv.start())
        try:
            srv.stop()  # штатный stop() тоже взводит shutting_down (см. docstring), но НЕ terminal
            self.assertTrue(srv.start(), "без begin_shutdown() start() обязан снова поднять сервер")
            deadline = time.monotonic() + 2.0
            ok = False
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as resp:
                        ok = resp.status == 200
                        break
                except Exception:
                    time.sleep(0.05)
            self.assertTrue(ok, "сервер обязан снова слушать после штатного stop()+start()")
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
