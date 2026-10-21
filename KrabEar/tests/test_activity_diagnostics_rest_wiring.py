"""Actual Flask/WS wiring with controlled native leaves, no model inference."""
from concurrent.futures import Future, ThreadPoolExecutor
import io
import json
import threading
import time
import wave
from unittest.mock import Mock

import pytest

import test_stt_residency_rest as residency_tests
from backend.activity_diagnostics import ActivityReservations

rest = residency_tests.rest  # Existing isolated REST fixture, no duplicate constructors.


AUTH = {'Authorization': 'Bearer test-residency-key'}


@pytest.fixture
def route(rest, monkeypatch):
    import backend.rest_server as rs
    tracker = ActivityReservations()
    monkeypatch.setattr(rs, '_REST_ACTIVITY', tracker, raising=False)
    monkeypatch.setattr('backend.brain_lease.observe_brain_lease', lambda: {'known': False, 'state': 'contention'})
    return rest[0], rs, tracker


def wav_upload():
    data = io.BytesIO()
    with wave.open(data, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b'\0\0' * 1600)
    return io.BytesIO(data.getvalue())


def post(client):
    return client.post('/v1/stt/transcribe', headers=AUTH,
                       data={'file': (wav_upload(), 'sample.wav'), 'persist_history': 'false'},
                       content_type='multipart/form-data')


def test_authenticated_readiness_adds_activity_without_claiming_idle(route):
    client, _, tracker = route
    tracker.reserve('post_stt')
    response = client.get('/v1/readiness', headers=AUTH)
    assert response.status_code == 200
    result = response.get_json()
    assert result['activity']['owner'] == 'rest'
    assert result['activity']['state'] == 'busy'
    assert result['activity']['components']['native_stt']['reservations'] == 1
    assert result['activity']['generation'] is None
    assert result['activity']['coverage']['complete'] is False
    assert result['lease_observation']['state'] == 'contention'


def test_auth_is_checked_before_new_metadata(route, monkeypatch):
    client, _, tracker = route
    snapshot = Mock(side_effect=AssertionError('metadata before auth'))
    monkeypatch.setattr(tracker, 'snapshot', snapshot)
    assert client.get('/v1/readiness').status_code == 401
    snapshot.assert_not_called()


def test_readiness_survives_broken_activity_provider(route, monkeypatch):
    client, _, tracker = route
    monkeypatch.setattr(tracker, 'snapshot', Mock(side_effect=RuntimeError))
    response = client.get('/v1/readiness', headers=AUTH)
    assert response.status_code == 200
    assert response.get_json()['activity']['state'] == 'unknown'


def test_post_timeout_retains_actual_server_work_until_completion(route, monkeypatch):
    client, rs, tracker = route
    started, finish = threading.Event(), threading.Event()

    def native(*args, **kwargs):
        started.set()
        finish.wait(3)
        return {'text': '', 'language': 'es'}

    monkeypatch.setattr(client.application.config['REST_DEPS'].transcriber, 'transcribe', native)
    monkeypatch.setattr(rs, '_TRANSCRIBE_TIMEOUT_SEC', 0.02)
    monkeypatch.setattr(rs, '_arm_timeout_exit', Mock())
    try:
        response = post(client)
        assert response.status_code == 504
        assert started.is_set()
        assert tracker.snapshot()['inflight'] == 1
        response.close()
        assert tracker.snapshot()['reservations'] == 1
    finally:
        finish.set()
    deadline = time.monotonic() + 2
    while tracker.snapshot().get('reservations') and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert tracker.snapshot()['reservations'] == 0


def test_pending_cancel_and_submit_failure_do_not_leak_reservation(route, monkeypatch):
    client, rs, tracker = route
    pending = Future()
    executor = Mock()
    executor.submit.return_value = pending
    monkeypatch.setattr(rs.concurrent.futures, 'ThreadPoolExecutor', Mock(return_value=executor))
    monkeypatch.setattr(rs, '_TRANSCRIBE_TIMEOUT_SEC', 0.001)
    response = post(client)
    assert response.status_code == 504
    assert pending.cancelled()
    assert tracker.snapshot()['reservations'] == 0
    executor.submit.side_effect = RuntimeError('submit failed')
    assert post(client).status_code == 500
    assert tracker.snapshot()['reservations'] == 0


def test_actual_native_ws_handler_passes_tracked_slot_callbacks(route, monkeypatch):
    _, rs, tracker = route
    constructor = Mock(return_value=Mock())
    monkeypatch.setattr(rs, 'LiveSubsService', constructor)
    monkeypatch.setattr(rs, '_privacy_mode_enabled', lambda: False)
    monkeypatch.setattr(rs, '_ws_check_auth', lambda _: True)
    ws = Mock()
    ws.receive.side_effect = [json.dumps({'type': 'config', 'backend': 'native', 'source_lang': 'es'}), None]
    with rs.app.test_request_context('/v1/stream'):
        rs._ws_stream_handler(ws)
    callbacks = constructor.call_args.kwargs
    assert callbacks['stt_acquire'](0)
    try:
        assert tracker.snapshot()['inflight'] == 1
    finally:
        callbacks['stt_release']()
    assert tracker.snapshot()['reservations'] == 0


def test_submit_error_after_queueing_retains_activity_until_executor_drains(route, monkeypatch):
    client, rs, tracker = route
    started, finish, draining = threading.Event(), threading.Event(), threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)

    def native(*args, **kwargs):
        started.set()
        finish.wait(3)
        return {'text': ''}

    class PartialSubmit:
        def submit(self, fn, *args, **kwargs):
            pool.submit(fn, *args, **kwargs)
            assert started.wait(1)
            raise RuntimeError('submit failed after queueing')

        def shutdown(self, wait=True, **kwargs):
            draining.set()
            pool.shutdown(wait=wait)

    monkeypatch.setattr(client.application.config['REST_DEPS'].transcriber, 'transcribe', native)
    monkeypatch.setattr(rs.concurrent.futures, 'ThreadPoolExecutor', Mock(return_value=PartialSubmit()))
    statuses = []

    def request():
        with client.application.test_client() as separate:
            statuses.append(post(separate).status_code)

    thread = threading.Thread(target=request)
    thread.start()
    try:
        assert draining.wait(1)
        assert tracker.snapshot()['inflight'] == 1
    finally:
        finish.set()
        thread.join(3)
        pool.shutdown(wait=True)
    assert not thread.is_alive()
    assert statuses == [500]
    assert tracker.snapshot()['reservations'] == 0
