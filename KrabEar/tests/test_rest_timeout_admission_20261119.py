"""Граница допуска standalone REST после running timeout, без GPU и сокетов."""

import threading
from concurrent.futures import Future, TimeoutError
from unittest.mock import Mock

import pytest

import test_activity_diagnostics_rest_wiring as wiring
from test_activity_diagnostics_rest_wiring import AUTH, post, wav_upload

rest = wiring.rest
route = wiring.route


@pytest.fixture
def timeout_route(route, monkeypatch):
    client, rs, activity = route
    monkeypatch.setattr(rs, "_STT_SINGLEFLIGHT", threading.Lock())
    monkeypatch.setattr(rs, "_STT_ORPHAN_ACTIVE", threading.Event())
    monkeypatch.setattr(rs, "_STT_RECYCLE_PENDING", threading.Event())
    monkeypatch.setattr(rs, "_PROCESS_HOSTS_BACKEND", False)
    monkeypatch.setattr(rs.settings, "REST_IN_PROCESS_ENABLED", False)
    # Запись события на последней границе. Никаких сигналов либо os._exit.
    exits = Mock()
    monkeypatch.setattr(rs, "_exit_poisoned_rest_process", exits)
    executor = Mock()
    running = Future()
    assert running.set_running_or_notify_cancel()
    monkeypatch.setattr(running, "result", Mock(side_effect=TimeoutError))
    successful = Future()
    successful.set_result({"text": "", "language": "es"})
    executor.submit.side_effect = [running, successful]
    monkeypatch.setattr(rs.concurrent.futures, "ThreadPoolExecutor", Mock(return_value=executor))
    yield client, rs, activity, executor, running, exits
    if not running.done():
        running.set_result({})


def test_standalone_blocks_new_stt_when_old_future_ends_before_504_closes(timeout_route):
    client, rs, activity, executor, running, exits = timeout_route
    response = post(client)
    assert response.status_code == 504
    assert activity.snapshot()["reservations"] == 1
    try:
        assert post(client).status_code == 503
        running.set_result({})
        assert activity.snapshot()["reservations"] == 0
        assert rs._STT_SINGLEFLIGHT.acquire(blocking=False)
        rs._STT_SINGLEFLIGHT.release()

        retry = post(client)
        assert retry.status_code == 503
        assert retry.get_json() == {"error": "stt_busy"}
        assert executor.submit.call_count == 1
        exits.assert_not_called()
    finally:
        response.close()
    exits.assert_called_once_with(70)
    assert not rs.try_acquire_stt_singleflight(0)


def test_waiting_slot_caller_rechecks_after_running_timeout(timeout_route, monkeypatch):
    client, rs, _, executor, running, _ = timeout_route
    waiting = threading.Event()
    slot = rs._STT_SINGLEFLIGHT

    def acquire(timeout):
        if slot.locked():
            waiting.set()
        return slot.acquire(timeout=timeout)

    monkeypatch.setattr(rs, "_STT_SINGLEFLIGHT", Mock(acquire=acquire, release=slot.release))
    finished = Future()

    def retry():
        try:
            with client.application.test_client() as other:
                with post(other) as response:
                    finished.set_result(response.status_code)
        except Exception as error:
            finished.set_exception(error)

    worker = threading.Thread(target=retry)

    def timeout_after_second_request_waits(**_):
        worker.start()
        assert waiting.wait(3)
        raise TimeoutError

    monkeypatch.setattr(running, "result", timeout_after_second_request_waits)
    with post(client) as old:
        try:
            assert old.status_code == 504
            running.set_result({})
            assert finished.result(timeout=3) == 503
            assert executor.submit.call_count == 1
        finally:
            if not running.done():
                running.set_result({})
            if worker.ident is not None:
                worker.join(timeout=3)
                assert not worker.is_alive()


@pytest.mark.parametrize("embedded_by", ["marker", "settings"])
def test_embedded_rest_resumes_after_future_without_arming_exit(timeout_route, monkeypatch, embedded_by):
    client, rs, _, executor, running, exits = timeout_route
    if embedded_by == "marker":
        rs._mark_backend_process()
    else:
        monkeypatch.setattr(rs.settings, "REST_IN_PROCESS_ENABLED", True)
    with post(client) as old:
        assert old.status_code == 504
        assert post(client).status_code == 503
        running.set_result({})
        assert post(client).status_code == 200
        assert executor.submit.call_count == 2
        # Смена настройки между timeout и close не может разрешить старый exit.
        monkeypatch.setattr(rs.settings, "REST_IN_PROCESS_ENABLED", False)
    exits.assert_not_called()


def test_standalone_does_not_dispatch_new_ru_call_to_owner(timeout_route, monkeypatch):
    from backend import call_stt_client

    client, _, _, _, running, _ = timeout_route
    client.application.config["REST_DEPS"].store.call_privacy_mode.return_value = False
    owner = Mock(return_value={"status": "ok", "text": "синтетика"})
    monkeypatch.setattr(call_stt_client, "transcribe_ephemeral_call", owner)
    with post(client) as old:
        assert old.status_code == 504
        running.set_result({})
        with client.post("/v1/stt/transcribe", headers=AUTH, data={
            "file": (wav_upload(), "call.wav"),
            "request_profile": "voice_gateway_call",
            "lang_hint": "ru",
        }) as call:
            assert call.status_code == 503
            assert call.get_json() == {"error": "stt_busy", "text": ""}
    owner.assert_not_called()


@pytest.mark.parametrize("unreadable", [False, True])
def test_privacy_rechecked_on_ordinary_busy_reply(timeout_route, monkeypatch, unreadable):
    client, rs, _, executor, _, _ = timeout_route
    store = client.application.config["REST_DEPS"].store

    def deny_after_privacy_changes(_timeout):
        if unreadable:
            store.load_settings.side_effect = OSError("synthetic settings failure")
        else:
            store.load_settings.return_value = {"privacy_mode_enabled": True}
        return False

    monkeypatch.setattr(rs, "try_acquire_stt_singleflight", deny_after_privacy_changes)
    with post(client) as retry:
        assert retry.status_code == 403
        assert retry.get_json() == {"skipped": "privacy_mode", "text": ""}
    executor.submit.assert_not_called()
    store.load_settings.assert_called_with(nowait=True)
