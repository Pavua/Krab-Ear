"""Неопределённый REST stop не должен выглядеть завершённым."""
import http.client
from threading import Event, Lock, Thread, current_thread
from types import SimpleNamespace

from flask import Flask
import pytest

import backend.rest_inprocess as rest_module
from backend.rest_inprocess import InProcessRestServer


class FakeServer:
    def __init__(self, *, fail_shutdown=False, fail_close=False):
        self.fail_shutdown = fail_shutdown
        self.fail_close = fail_close
        self.shutdown_called = False
        self.close_called = False

    def shutdown(self):
        self.shutdown_called = True
        if self.fail_shutdown:
            raise RuntimeError("synthetic shutdown error")

    def server_close(self):
        self.close_called = True
        if self.fail_close:
            raise RuntimeError("synthetic close error")


class StuckThread:
    def is_alive(self):
        return True

    def join(self, timeout=None):
        pass


@pytest.mark.parametrize("failure", ["drain", "shutdown", "close", "join", "not_serving"])
def test_uncertain_stop_fails_closed(monkeypatch, failure):
    server = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), True)
    fake = FakeServer(fail_shutdown=failure == "shutdown", fail_close=failure == "close")
    server._server = fake
    server._thread = StuckThread() if failure == "join" else None
    if failure != "not_serving":
        server._serving.set()
    monkeypatch.setattr(rest_module, "SERVE_ENTER_TIMEOUT_SEC", 0)
    server._drain_active_requests = lambda _timeout: failure != "drain"

    with pytest.raises(RuntimeError):
        server.stop(timeout=0)
    assert server._stop_failed
    assert server._server is fake, "uncertain stop lost server ownership"
    assert server.start() is False
    assert server._shutting_down.is_set()
    if failure in ("drain", "not_serving"):
        assert not fake.shutdown_called and not fake.close_called


def test_restart_does_not_call_start_after_stop_error():
    server = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), True)
    calls = []

    def fail_stop():
        raise RuntimeError("synthetic stop error")

    server.stop = fail_stop
    server.start = lambda: calls.append("start") or True
    assert server.restart() is False
    assert calls == []


def test_existing_server_with_dead_thread_is_not_reported_running():
    server = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), True)
    fake = FakeServer()
    server._server = fake
    server._thread = None
    assert server.start() is False
    assert server._server is fake
    assert server._stop_failed and server._shutting_down.is_set()


def test_existing_server_start_refuses_if_stop_begins_after_initial_check():
    server = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), True)
    server._server = FakeServer()
    server._thread = StuckThread()
    server._start_whisper_idle_reaper = lambda: None
    start_waiting, allow_start, stop_entered, allow_stop = (Event() for _ in range(4))
    results, errors = [], []

    class ControlledLock:
        def __init__(self):
            self.lock = Lock()

        def __enter__(self):
            if current_thread().name == "controlled-start":
                start_waiting.set()
                if not allow_start.wait(5):
                    raise TimeoutError("start not released")
            self.lock.acquire()
            return self

        def __exit__(self, *_args):
            self.lock.release()

    def controlled_stop(_timeout):
        stop_entered.set()
        if not allow_stop.wait(5):
            raise TimeoutError("stop not released")
        return server._server, server._thread

    def stop():
        try:
            server.stop()
        except Exception as exc:
            errors.append(exc)

    server._lock = ControlledLock()
    server._stop_server = controlled_stop
    starter = Thread(target=lambda: results.append(server.start()), name="controlled-start")
    stopper = Thread(target=stop)
    starter.start()
    try:
        assert start_waiting.wait(3)
        stopper.start()
        assert stop_entered.wait(3)
        allow_start.set()
        starter.join(5)
        assert not starter.is_alive()
        assert results == [False]
    finally:
        allow_start.set()
        allow_stop.set()
        starter.join(5)
        if stopper.ident is not None:
            stopper.join(5)
    assert not stopper.is_alive() and not errors


def test_serve_crash_preserves_ownership_and_closes_admission():
    server = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), True)

    class CrashedServer(FakeServer):
        def serve_forever(self):
            raise RuntimeError("synthetic server crash")

    fake = CrashedServer()
    server._server = fake
    server._serve(fake)
    assert server._server is fake
    assert server._stop_failed and server._shutting_down.is_set()
    assert server.start() is False


@pytest.mark.parametrize("launch_first", [False, True])
def test_thread_start_failure_retains_candidate_and_closes_admission(monkeypatch, launch_first):
    entered, release, launched = Event(), Event(), Event()
    real_thread = Thread

    class Candidate(FakeServer):
        def serve_forever(self):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("worker not released")

    candidate = Candidate()
    monkeypatch.setattr("werkzeug.serving.make_server", lambda *_args, **_kwargs: candidate)

    def thread_factory(*args, **kwargs):
        target = kwargs["target"]

        def observed_target(*target_args):
            launched.set()
            target(*target_args)

        kwargs["target"] = observed_target
        worker = real_thread(*args, **kwargs)
        original_start = worker.start

        def fail_start():
            if launch_first:
                original_start()
                if not launched.wait(3):
                    raise TimeoutError("worker did not enter")
            raise RuntimeError("synthetic start failure")

        worker.start = fail_start
        return worker

    monkeypatch.setattr(rest_module.threading, "Thread", thread_factory)
    server = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), True)
    server._start_whisper_idle_reaper = lambda: None
    try:
        assert server.start() is False
        assert server._server is candidate and server._thread is not None
        assert server._stop_failed and server._shutting_down.is_set()
        assert server.start() is False
        assert not candidate.close_called
    finally:
        release.set()
        if server._thread is not None and server._thread.ident is not None:
            server._thread.join(5)
            assert not server._thread.is_alive()


def test_overlapping_stop_is_not_reported_as_success():
    server = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), True)
    fake = FakeServer()
    server._server = fake
    server._serving.set()
    entered, release = Event(), Event()
    errors = []

    def controlled_drain(_timeout):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("drain not released")
        return True

    def first_stop():
        try:
            server.stop()
        except Exception as exc:
            errors.append(exc)

    server._drain_active_requests = controlled_drain
    first = Thread(target=first_stop)
    first.start()
    try:
        assert entered.wait(3), "first stop did not reach drain"
        with pytest.raises(RuntimeError):
            server.stop()
        release.set()
        first.join(5)
        assert not first.is_alive()
        assert errors, "first stop ignored overlapping failure"
        assert server._stop_failed
        assert server._server is fake
        assert not fake.shutdown_called and not fake.close_called
    finally:
        release.set()
        first.join(5)


def test_poison_during_shutdown_does_not_close_or_forget_server():
    entered, release = Event(), Event()
    errors = []

    class DelayedShutdown(FakeServer):
        def shutdown(self):
            self.shutdown_called = True
            entered.set()
            if not release.wait(5):
                raise RuntimeError("shutdown not released")

    server = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), True)
    fake = DelayedShutdown()
    server._server = fake
    server._serving.set()
    server._drain_active_requests = lambda _timeout: True

    def stop():
        try:
            server.stop()
        except Exception as exc:
            errors.append(exc)

    first = Thread(target=stop)
    first.start()
    try:
        assert entered.wait(3), "shutdown was not entered"
        with pytest.raises(RuntimeError):
            server.stop()
        release.set()
        first.join(5)
        assert not first.is_alive()
        assert errors and server._stop_failed
        assert server._server is fake
        assert not fake.close_called
        assert server.start() is False
    finally:
        release.set()
        first.join(5)


def test_successful_stop_releases_server_and_allows_explicit_restart():
    server = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), True)
    server._start_whisper_idle_reaper = lambda: None
    fake = FakeServer()
    server._server = fake
    server._serving.set()
    server.stop()
    assert fake.shutdown_called and fake.close_called
    assert server._server is None and server._thread is None
    server.stop()
    try:
        assert server.start() is True
    finally:
        if server._server is not None:
            server.stop()


def test_real_http_handler_survives_drain_timeout(monkeypatch):
    app = Flask(__name__)
    entered, release = Event(), Event()
    responses = []
    errors = []

    @app.get("/work")
    def work():
        entered.set()
        if not release.wait(5):
            raise RuntimeError("handler not released")
        return "ok"

    monkeypatch.setattr(rest_module, "REST_DRAIN_BUDGET_SEC", 0.05)
    server = InProcessRestServer(app, SimpleNamespace(REST_SERVER_PORT=0), True)
    server._start_whisper_idle_reaper = lambda: None
    listener = listener_thread = request_thread = None

    def request_work():
        connection = http.client.HTTPConnection("127.0.0.1", listener.server_port, timeout=5)
        try:
            connection.request("GET", "/work")
            response = connection.getresponse()
            responses.append((response.status, response.read()))
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    try:
        started = server.start()
        listener, listener_thread = server._server, server._thread
        assert started
        request_thread = Thread(target=request_work)
        request_thread.start()
        assert entered.wait(3)
        with pytest.raises(RuntimeError, match="drain"):
            server.stop()
        assert server._server is listener and server._stop_failed
        assert server.start() is False
        release.set()
        request_thread.join(5)
        assert not request_thread.is_alive()
        assert not errors, errors
        assert responses == [(200, b"ok")]
    finally:
        release.set()
        if request_thread is not None and request_thread.ident is not None:
            request_thread.join(5)
        # Изолированный test listener принадлежит этому тесту; штатный stop
        # намеренно не снимает UNKNOWN, поэтому cleanup выполняется напрямую.
        listener = listener or server._server
        listener_thread = listener_thread or server._thread
        if listener is not None:
            if server._serving.is_set():
                listener.shutdown()
            listener.server_close()
        if listener_thread is not None and listener_thread.ident is not None:
            listener_thread.join(5)
    assert listener_thread is not None and not listener_thread.is_alive()
