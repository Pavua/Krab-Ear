"""RU-звонок не делит слот файлового STT, но задерживает fail-fast выход REST до конца хода."""

import threading
import time
from unittest.mock import Mock

import pytest

from test_activity_diagnostics_rest_wiring import AUTH, post, rest, route, wav_upload  # noqa: F401  (fixtures)
from test_rest_timeout_admission_20261119 import timeout_route  # noqa: F401  (fixture)


def _ru_call(client):
    return client.post(
        "/v1/stt/transcribe",
        headers=AUTH,
        data={"file": (wav_upload(), "call.wav"), "request_profile": "voice_gateway_call", "lang_hint": "ru"},
    )


@pytest.fixture
def owner(timeout_route, monkeypatch):  # noqa: F811
    from backend import call_stt_client

    client, rs, _, _, running, exits = timeout_route
    client.application.config["REST_DEPS"].store.call_privacy_mode.return_value = False
    monkeypatch.setattr(rs, "_call_ipc_inflight", 0, raising=False)
    ipc = Mock(return_value={"status": "ok", "text": "синтетика"})
    monkeypatch.setattr(call_stt_client, "transcribe_ephemeral_call", ipc)
    return client, rs, running, exits, ipc


def test_ru_call_runs_while_file_stt_holds_the_slot(owner):
    client, rs, _, _, ipc = owner
    assert rs.try_acquire_stt_singleflight(0)
    try:
        with _ru_call(client) as call:
            assert call.status_code == 200
            assert call.get_json()["text"] == "синтетика"
    finally:
        rs.release_stt_singleflight()
    ipc.assert_called_once()
    assert rs._call_ipc_inflight == 0


def test_ru_call_runs_while_an_orphan_worker_holds_the_slot(owner):
    client, rs, _, _, ipc = owner
    rs._STT_ORPHAN_ACTIVE.set()
    with _ru_call(client) as call:
        assert call.status_code == 200
    ipc.assert_called_once()


@pytest.mark.parametrize("failure", ["rejected", "timeout", "protocol"])
def test_counter_is_released_on_every_outcome(owner, monkeypatch, failure):
    from backend import call_stt_client as client_module

    client, rs, _, _, ipc = owner
    error = {
        "rejected": client_module.CallSTTRejectedError,
        "timeout": client_module.CallSTTTimeoutError,
        "protocol": client_module.CallSTTProtocolError,
    }[failure]
    ipc.side_effect = error("synthetic")
    with _ru_call(client) as call:
        assert call.status_code in (403, 503, 504)
    assert rs._call_ipc_inflight == 0


def test_new_call_turn_is_refused_after_fail_fast_is_armed(owner):
    client, rs, running, _, ipc = owner
    with post(client) as old:
        assert old.status_code == 504
        running.set_result({})
        with _ru_call(client) as call:
            assert call.status_code == 503
            assert call.get_json() == {"error": "stt_busy", "text": ""}
    ipc.assert_not_called()
    assert rs._call_ipc_inflight == 0


def test_fail_fast_waits_for_a_call_turn_that_started_before_the_timeout(owner):
    client, rs, _, exits, _ = owner
    assert rs._call_ipc_enter()
    response = post(client)
    assert response.status_code == 504
    closer = threading.Thread(target=response.close)
    closer.start()
    try:
        time.sleep(0.2)
        exits.assert_not_called()
        assert closer.is_alive()
    finally:
        rs._call_ipc_exit()
    closer.join(timeout=3)
    assert not closer.is_alive()
    exits.assert_called_once()


def test_fail_fast_drain_is_bounded(owner, monkeypatch):
    client, rs, _, exits, _ = owner
    monkeypatch.setattr(rs, "_CALL_IPC_DRAIN_MAX_SEC", 0.2)
    assert rs._call_ipc_enter()
    try:
        response = post(client)
        assert response.status_code == 504
        started = time.monotonic()
        response.close()
        assert time.monotonic() - started < 3
        exits.assert_called_once()
    finally:
        rs._call_ipc_exit()


def test_embedded_rest_never_waits_or_exits(owner, monkeypatch):
    client, rs, running, exits, _ = owner
    rs._mark_backend_process()
    assert rs._call_ipc_enter()
    try:
        with post(client) as old:
            assert old.status_code == 504
            running.set_result({})
    finally:
        rs._call_ipc_exit()
    exits.assert_not_called()
