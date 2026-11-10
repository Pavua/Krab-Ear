"""Зависший shutdown не должен бесконечно блокировать вызывающего."""
from threading import Event, Thread
from types import SimpleNamespace
from unittest import mock

from flask import Flask

from backend.rest_inprocess import InProcessRestServer


def test_hung_shutdown_returns_without_releasing_ownership():
    entered, release, finished = Event(), Event(), Event()
    errors = []

    class Server:
        closed = False

        def shutdown(self):
            entered.set()
            release.wait(5)

        def server_close(self):
            self.closed = True

    owner = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), True)
    server = Server()
    owner._server = server
    owner._serving.set()
    owner._drain_active_requests = lambda _timeout: True

    def stop():
        try:
            owner.stop(timeout=0.05)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = Thread(target=stop, daemon=True)
    try:
        worker.start()
        assert entered.wait(2)
        assert finished.wait(0.5), "stop заблокирован в server.shutdown()"
        assert errors and owner._stop_failed
        assert owner._server is server and not server.closed
        assert owner.start() is False
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(3)
        shutdown_worker = owner._shutdown_worker
        if shutdown_worker is not None and shutdown_worker.ident is not None:
            shutdown_worker.join(3)
    assert shutdown_worker is not None
    assert not shutdown_worker.is_alive()
    assert owner._server is server and not server.closed
    assert owner._stop_failed and owner.start() is False


def test_interrupted_shutdown_join_keeps_ownership():
    owner = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), True)
    server = mock.Mock()
    owner._server = server
    owner._serving.set()
    owner._drain_active_requests = lambda _timeout: True
    worker = mock.Mock()
    worker.join.side_effect = KeyboardInterrupt
    with mock.patch("backend.rest_inprocess.threading.Thread", return_value=worker):
        try:
            owner.stop(timeout=0.05)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("interruption must propagate")
    assert owner._shutdown_worker is worker
    assert owner._stop_failed and owner._server is server
    server.server_close.assert_not_called()
    assert owner.start() is False


def test_ambiguous_shutdown_thread_start_keeps_ownership():
    owner = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), True)
    server = mock.Mock()
    owner._server = server
    owner._serving.set()
    owner._drain_active_requests = lambda _timeout: True
    worker = mock.Mock()
    worker.start.side_effect = RuntimeError("ambiguous start")
    with mock.patch("backend.rest_inprocess.threading.Thread", return_value=worker):
        try:
            owner.stop(timeout=0.05)
        except RuntimeError:
            pass
        else:
            raise AssertionError("start failure must propagate")
    assert owner._shutdown_worker is worker
    assert owner._stop_failed and owner._server is server
    server.server_close.assert_not_called()
    assert owner.start() is False


def test_shutdown_and_server_join_share_one_budget():
    owner = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), True)
    server = mock.Mock()
    server_thread = mock.Mock()
    server_thread.is_alive.side_effect = [True, False]
    worker = mock.Mock()
    worker.is_alive.return_value = False
    owner._server = server
    owner._thread = server_thread
    owner._serving.set()
    owner._drain_active_requests = lambda _timeout: True
    with mock.patch("backend.rest_inprocess.threading.Thread", return_value=worker), \
         mock.patch("backend.rest_inprocess.time.monotonic", side_effect=[100.0, 104.0]):
        owner.stop(timeout=5)
    worker.join.assert_called_once_with(timeout=5)
    server_thread.join.assert_called_once_with(timeout=1)
