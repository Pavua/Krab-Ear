"""При закрытии REST запрос не может проскользнуть мимо учёта."""
from threading import Event, Thread, current_thread
from types import SimpleNamespace

import pytest
from flask import Flask

from backend.rest_inprocess import InProcessRestServer


@pytest.mark.parametrize("close_method", ["begin_shutdown", "stop"])
def test_close_serializes_with_request_admission(close_method):
    app = Flask(__name__)
    checked_open = Event()
    resume_gate = Event()
    entered_handler = Event()
    release_handler = Event()
    close_done = Event()
    close_observed = Event()
    drain_entered = Event()
    worker_errors = []
    server = InProcessRestServer(app, SimpleNamespace(REST_SERVER_PORT=0), True)
    original_is_set = server._shutting_down.is_set
    original_lock = server._active_lock
    original_drain = server._drain_active_requests

    def observed_drain(timeout):
        drain_entered.set()
        return original_drain(timeout)

    server._drain_active_requests = observed_drain

    class ObservedLock:
        def __enter__(self):
            if current_thread() is close_thread:
                close_observed.set()
            original_lock.acquire()
            return self

        def __exit__(self, *_args):
            original_lock.release()

    server._active_lock = ObservedLock()

    @app.get("/work")
    def work():
        entered_handler.set()
        release_handler.wait(5)
        return "ok"

    def pause_after_check():
        is_closed = original_is_set()
        checked_open.set()
        if not resume_gate.wait(5):
            raise TimeoutError("request gate was not released")
        return is_closed

    server._shutting_down.is_set = pause_after_check

    def request_work():
        try:
            with app.test_client() as client:
                client.get("/work")
        except Exception as exc:
            worker_errors.append(exc)

    def close():
        try:
            getattr(server, close_method)()
        except Exception as exc:
            worker_errors.append(exc)
        finally:
            close_done.set()
            close_observed.set()

    request_thread = Thread(target=request_work)
    close_thread = Thread(target=close)
    request_thread.start()
    try:
        assert checked_open.wait(3), "request did not reach admission gate"
        close_thread.start()
        assert close_observed.wait(3), "close did not reach its admission lock"
        closed_before_registration = close_done.is_set()
        resume_gate.set()
        assert not closed_before_registration, "close overtook admission registration"
        assert entered_handler.wait(3), "accepted request did not enter handler"
        if close_method == "stop":
            assert drain_entered.wait(3), "stop did not enter drain"
        else:
            assert close_done.wait(3), "begin_shutdown did not return"
        assert not original_drain(0), "live request missing from drain"
        release_handler.set()
        assert close_done.wait(3), "close did not return after request finished"
    finally:
        resume_gate.set()
        release_handler.set()
        request_thread.join(5)
        if close_thread.ident is not None:
            close_thread.join(5)
    assert not request_thread.is_alive()
    assert not close_thread.is_alive()
    assert not worker_errors, worker_errors
