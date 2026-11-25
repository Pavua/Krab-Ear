"""REST должен подтвердить останов до освобождения ресурсов backend."""
from types import SimpleNamespace
from unittest.mock import MagicMock
import sys
import os
import http.client
from threading import Event, Lock, Thread

import backend
import backend.service as service_module
import backend.rest_inprocess as rest_module
from flask import Flask
import pytest

from backend.service import BackendService, _shutdown_backend


@pytest.fixture(autouse=True)
def isolate_rest_module(monkeypatch):
    module = SimpleNamespace(release_external_singletons=MagicMock())
    monkeypatch.setitem(sys.modules, "backend.rest_server", module)
    monkeypatch.setattr(backend, "rest_server", module, raising=False)
    return module


def make_service():
    service = object.__new__(BackendService)
    for name in ("_rest_watchdog", "_rest_inprocess", "_memory_conductor", "_meeting_svc",
                 "_plaintext_export_authorizer", "transcriber", "_detach_event_bus_wiring"):
        setattr(service, name, MagicMock())
    service._close_guard = Lock()
    service._close_state = "OPEN"
    service._rest_gate_unknown = False
    return service


@pytest.mark.parametrize("failure", ["begin", "stop_exception", "stop_false", "watchdog_exception"])
def test_unknown_rest_barrier_prevents_resource_teardown(failure, isolate_rest_module):
    service = make_service()
    if failure == "begin":
        service._rest_inprocess.begin_shutdown.side_effect = RuntimeError("synthetic begin")
    elif failure == "stop_exception":
        service._rest_inprocess.stop.side_effect = RuntimeError("synthetic stop")
    elif failure == "stop_false":
        service._rest_inprocess.stop.return_value = False
    else:
        service._rest_watchdog.stop.side_effect = RuntimeError("synthetic watchdog")
    assert BackendService.close(service) is False
    service._plaintext_export_authorizer.close.assert_not_called()
    service._memory_conductor.stop.assert_not_called()
    service._meeting_svc.close.assert_not_called()
    service.transcriber.close.assert_not_called()
    service._detach_event_bus_wiring.assert_not_called()
    isolate_rest_module.release_external_singletons.assert_not_called()


def test_rest_barrier_precedes_backend_resource_teardown():
    service = make_service()
    events = []
    service._rest_watchdog.begin_shutdown.side_effect = lambda: events.append("watchdog_begin")
    service._rest_watchdog.stop.side_effect = lambda: events.append("watchdog_stop")
    service._rest_inprocess.begin_shutdown.side_effect = lambda: events.append("rest_begin")
    service._rest_inprocess.stop.side_effect = lambda: events.append("rest_stop")
    service._plaintext_export_authorizer.close.side_effect = lambda: events.append("authorizer_close")
    assert BackendService.close(service) is True
    assert events.index("watchdog_begin") < events.index("rest_begin") < events.index("rest_stop")
    assert events.index("rest_begin") < events.index("watchdog_stop") < events.index("rest_stop")
    assert events.index("rest_stop") < events.index("authorizer_close")


def test_unknown_close_is_sticky_across_repeated_close():
    service = make_service()
    service._rest_inprocess.stop.side_effect = [RuntimeError("unknown"), None]
    assert BackendService.close(service) is False
    assert BackendService.close(service) is False
    service._memory_conductor.stop.assert_not_called()
    service.transcriber.close.assert_not_called()


def test_concurrent_close_refuses_without_cleanup_and_preserves_unknown():
    service = make_service()
    entered, release = Event(), Event()
    results = []

    def pending_stop():
        entered.set()
        if not release.wait(5):
            raise TimeoutError("stop not released")
        raise RuntimeError("synthetic unknown")

    service._rest_inprocess.stop.side_effect = pending_stop
    worker = Thread(target=lambda: results.append(service.close()))
    worker.start()
    try:
        assert entered.wait(3)
        assert service.close() is False
        service._memory_conductor.stop.assert_not_called()
        release.set()
        worker.join(5)
        assert not worker.is_alive() and results == [False]
        assert service.close() is False
        service._memory_conductor.stop.assert_not_called()
    finally:
        release.set()
        worker.join(5)


def test_missing_close_guard_fails_without_cleanup():
    service = make_service()
    del service._close_guard
    assert service.close() is False
    service._memory_conductor.stop.assert_not_called()


def test_missing_close_state_fails_without_cleanup():
    service = make_service()
    del service._close_state
    assert service.close() is False
    service._memory_conductor.stop.assert_not_called()


def test_successful_close_is_idempotent():
    service = make_service()
    assert service.close() is True
    assert service.close() is True
    service._rest_inprocess.stop.assert_called_once_with()


def test_gate_failure_cannot_race_close_into_resource_teardown():
    service = make_service()
    gate_entered, release = Event(), Event()
    server, handler = MagicMock(), MagicMock()
    outcomes, exits = [], []
    service._rest_watchdog.begin_shutdown.side_effect = [RuntimeError("first gate fails"), None]
    calls = 0

    def rest_gate():
        nonlocal calls
        calls += 1
        if calls == 1:
            gate_entered.set()
            if not release.wait(5):
                raise TimeoutError("gate not released")

    service._rest_inprocess.begin_shutdown.side_effect = rest_gate
    worker = Thread(target=lambda: outcomes.append(_shutdown_backend(
        service, server, handler, flush_fn=lambda: None, exit_fn=exits.append,
    )))
    worker.start()
    try:
        assert gate_entered.wait(3)
        assert service.close() is False
        service._memory_conductor.stop.assert_not_called()
        release.set()
        worker.join(5)
        assert not worker.is_alive() and outcomes == [False]
        assert exits == [os.EX_SOFTWARE]
        assert service.close() is False
        service._memory_conductor.stop.assert_not_called()
    finally:
        release.set()
        worker.join(5)


def test_system_exception_in_coordinator_gate_is_sticky():
    service = make_service()
    service._rest_watchdog.begin_shutdown.side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        _shutdown_backend(service, MagicMock(), MagicMock(), flush_fn=lambda: None, exit_fn=lambda _: None)
    assert service.close() is False
    service._memory_conductor.stop.assert_not_called()


def test_real_http_drain_failure_does_not_close_backend_resources(monkeypatch):
    entered, release = Event(), Event()
    responses, errors = [], []
    app = Flask(__name__)

    @app.get("/work")
    def work():
        entered.set()
        if not release.wait(5):
            raise TimeoutError("handler not released")
        return "ok"

    monkeypatch.setattr(rest_module, "REST_DRAIN_BUDGET_SEC", 0.05)
    owner = rest_module.InProcessRestServer(app, SimpleNamespace(REST_SERVER_PORT=0), True)
    owner._start_whisper_idle_reaper = lambda: None
    service = make_service()
    service._rest_inprocess = owner
    listener = server_thread = client_thread = None

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
        started = owner.start()
        listener, server_thread = owner._server, owner._thread
        assert started
        client_thread = Thread(target=request_work)
        client_thread.start()
        assert entered.wait(3)
        assert service.close() is False
        service._memory_conductor.stop.assert_not_called()
        service.transcriber.close.assert_not_called()
        assert owner._server is listener and owner._stop_failed
        release.set()
        client_thread.join(5)
        assert not client_thread.is_alive() and not errors
        assert responses == [(200, b"ok")]
    finally:
        release.set()
        if client_thread is not None and client_thread.ident is not None:
            client_thread.join(5)
        listener = listener or owner._server
        server_thread = server_thread or owner._thread
        if listener is not None:
            if owner._serving.is_set():
                listener.shutdown()
            listener.server_close()
        if server_thread is not None and server_thread.ident is not None:
            server_thread.join(5)
            assert not server_thread.is_alive()


def test_interrupted_gate_error_logging_keeps_unknown(monkeypatch):
    service = make_service()
    service._rest_watchdog.begin_shutdown.side_effect = RuntimeError("synthetic gate")
    with monkeypatch.context() as context:
        context.setattr(service_module.logger, "exception", MagicMock(side_effect=KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            _shutdown_backend(service, MagicMock(), MagicMock(), flush_fn=lambda: None, exit_fn=lambda _: None)
    service._rest_watchdog.begin_shutdown.side_effect = None
    assert service.close() is False
    service._memory_conductor.stop.assert_not_called()


@pytest.mark.parametrize("failed_gate", ["_rest_watchdog", "_rest_inprocess"])
def test_shutdown_gate_failure_attempts_both_gates_then_refuses_teardown(failed_gate):
    service = make_service()
    service.close = MagicMock(return_value=True)
    getattr(service, failed_gate).begin_shutdown.side_effect = RuntimeError("synthetic gate")
    server, handler = MagicMock(), MagicMock()
    exits = []
    result = _shutdown_backend(service, server, handler, flush_fn=lambda: None, exit_fn=exits.append)
    assert result is False and exits == [os.EX_SOFTWARE]
    service._rest_watchdog.begin_shutdown.assert_called_once_with()
    service._rest_inprocess.begin_shutdown.assert_called_once_with()
    server.stop.assert_not_called()
    handler.shutdown.assert_not_called()
