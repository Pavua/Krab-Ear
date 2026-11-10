"""Завершение backend не должно пропустить запаздывающий старт REST."""
from threading import Event, Thread
from types import SimpleNamespace

from flask import Flask
import pytest
import werkzeug.serving

from backend.rest_inprocess import InProcessRestServer


@pytest.mark.parametrize("close_method", ["begin_shutdown", "stop"])
def test_shutdown_during_bind_does_not_start_server(monkeypatch, close_method):
    binding, finish_bind, served, closed = (Event() for _ in range(4))
    result = []
    errors = []

    class FakeServer:
        def serve_forever(self):
            served.set()

        def server_close(self):
            closed.set()

        def shutdown(self):
            pass

    def delayed_bind(*_args, **_kwargs):
        binding.set()
        if not finish_bind.wait(5):
            raise TimeoutError("bind not released")
        return FakeServer()

    monkeypatch.setattr(werkzeug.serving, "make_server", delayed_bind)
    server = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), True)
    server._start_whisper_idle_reaper = lambda: None

    def start():
        try:
            result.append(server.start())
        except Exception as exc:
            errors.append(exc)

    def close():
        try:
            getattr(server, close_method)()
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=start)
    close_thread = Thread(target=close)
    thread.start()
    try:
        assert binding.wait(3), "start did not reach server creation"
        close_thread.start()
        assert server._shutting_down.wait(3), "close did not close admission"
        finish_bind.set()
        thread.join(5)
        close_thread.join(5)
        assert not thread.is_alive()
        assert not close_thread.is_alive()
        assert not errors, errors
        assert result == [False], "start succeeded after shutdown began"
        assert closed.is_set(), "unused socket was not closed"
        assert not served.is_set(), "server began serving after shutdown"
    finally:
        finish_bind.set()
        thread.join(5)
        if close_thread.ident is not None:
            close_thread.join(5)
        server.stop()


def test_cancelled_bind_close_failure_is_unknown_to_concurrent_stop(monkeypatch):
    binding, finish_bind = Event(), Event()
    starts, stop_errors = [], []

    class UnclosedServer:
        def server_close(self):
            raise OSError("synthetic close failure")

    candidate = UnclosedServer()

    def delayed_bind(*_args, **_kwargs):
        binding.set()
        if not finish_bind.wait(5):
            raise TimeoutError("bind not released")
        return candidate

    monkeypatch.setattr(werkzeug.serving, "make_server", delayed_bind)
    server = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), True)

    def stop():
        try:
            server.stop()
        except Exception as exc:
            stop_errors.append(exc)

    starter = Thread(target=lambda: starts.append(server.start()))
    stopper = Thread(target=stop)
    starter.start()
    try:
        assert binding.wait(3)
        stopper.start()
        assert server._shutting_down.wait(3)
        finish_bind.set()
        starter.join(5)
        stopper.join(5)
        assert not starter.is_alive() and not stopper.is_alive()
        assert starts == [False]
        assert len(stop_errors) == 1 and isinstance(stop_errors[0], RuntimeError)
        assert server._server is candidate and server._thread is None
        assert server._stop_failed and server._shutting_down.is_set()
        assert server.start() is False
    finally:
        finish_bind.set()
        starter.join(5)
        if stopper.ident is not None:
            stopper.join(5)


def test_second_start_cannot_hide_stop_from_first_bind(monkeypatch):
    binding, finish_bind, second_observed, bound, closed = (Event() for _ in range(5))
    results = []
    second_results = []
    errors = []

    class FakeServer:
        def serve_forever(self):
            pass

        def server_close(self):
            closed.set()

        def shutdown(self):
            pass

    def delayed_bind(*_args, **_kwargs):
        binding.set()
        if not finish_bind.wait(5):
            raise TimeoutError("bind not released")
        bound.set()
        return FakeServer()

    monkeypatch.setattr(werkzeug.serving, "make_server", delayed_bind)
    server = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), True)
    server._start_whisper_idle_reaper = lambda: None
    original_clear = server._shutting_down.clear
    clear_count = 0

    def observed_clear():
        nonlocal clear_count
        clear_count += 1
        original_clear()
        if clear_count == 2:
            second_observed.set()

    server._shutting_down.clear = observed_clear

    def start_first():
        try:
            results.append(server.start())
        except Exception as exc:
            errors.append(exc)

    def start_second():
        try:
            second_results.append(server.start())
        except Exception as exc:
            errors.append(exc)
        finally:
            second_observed.set()

    def close():
        try:
            server.stop()
        except Exception as exc:
            errors.append(exc)

    first = Thread(target=start_first)
    closer = Thread(target=close)
    second = Thread(target=start_second)
    first.start()
    try:
        assert binding.wait(3)
        closer.start()
        assert server._shutting_down.wait(3)
        second.start()
        assert second_observed.wait(3), "second start did not reach its decision"
        finish_bind.set()
        first.join(5)
        closer.join(5)
        second.join(5)
        assert not any(t.is_alive() for t in (first, closer, second))
        assert not errors, errors
        assert bound.is_set(), "delayed bind did not return a candidate"
        assert closed.is_set(), "cancelled candidate was not closed"
        assert results == [False], "second start hid stop from first bind"
        assert second_results == [False], "second start was not rejected during stop"
    finally:
        finish_bind.set()
        for thread in (first, closer, second):
            if thread.ident is not None:
                thread.join(5)
        server.stop()


def test_parallel_stops_are_reported_as_unknown():
    server = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), False)
    entered = Event()
    release = Event()
    errors = []

    def controlled_stop(_timeout):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("stop not released")
        return None, None

    def stop():
        try:
            server.stop()
        except Exception as exc:
            errors.append(exc)

    server._stop_server = controlled_stop
    first, second = Thread(target=stop), Thread(target=stop)
    first.start()
    try:
        assert entered.wait(3), "first stop did not enter"
        second.start()
        second.join(5)
        assert not second.is_alive()
        assert len(errors) == 1
        assert server._stop_owner is not None
        assert server.start() is False
        assert server._shutting_down.is_set(), "overlap reopened admission"
        release.set()
        first.join(5)
        assert not first.is_alive()
        assert server._stop_owner is None
        assert len(errors) == 2 and all(isinstance(exc, RuntimeError) for exc in errors)
        assert server._stop_failed
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)


def test_failed_stop_keeps_start_closed():
    server = InProcessRestServer(Flask(__name__), SimpleNamespace(REST_SERVER_PORT=0), False)

    def failed_stop(_timeout):
        raise RuntimeError("synthetic stop failure")

    server._stop_server = failed_stop
    with pytest.raises(RuntimeError, match="synthetic stop failure"):
        server.stop()
    assert server._stop_owner is None
    assert server._stop_failed
    assert server.start() is False
    assert server._shutting_down.is_set(), "failed stop must not reopen admission"
    server._stop_server = lambda _timeout: None
    with pytest.raises(RuntimeError):
        server.stop()
    assert server.start() is False
    assert server._shutting_down.is_set(), "later stop must not erase the failure"
