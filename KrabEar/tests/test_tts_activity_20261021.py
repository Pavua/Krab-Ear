"""TTS activity: настоящие handler/Flask, только synthesis leaf синтетический."""
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

import backend.tts_service as tts
import test_tts_admission_20261016 as admission
import test_stt_residency_rest as residency
from backend.activity_diagnostics import ActivityReservations

enabled = admission.enabled
rest = residency.rest


def snapshot(service):
    assert callable(getattr(service, 'activity_snapshot', None)), 'TTS activity API missing'
    return service.activity_snapshot()


def test_shared_handler_stays_busy_until_synthesis_and_cleanup_finish(enabled, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def synth(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return admission._wav()

    monkeypatch.setattr(tts, '_say_to_wav', synth)
    first, second = tts.TTSService(), tts.TTSService()
    before = snapshot(first)
    assert before['state'] == 'idle'
    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(first.handle_synthesize_speech, {'text': 'PRIVATE SENTINEL'})
        try:
            assert started.wait(1)
            busy = snapshot(second)
            assert busy['known'] is True and busy['state'] == 'busy'
            assert busy['inflight'] == 1
            rejected = second.handle_synthesize_speech({'text': 'another'})
            assert rejected['error_code'] == 'tts_busy'
            assert snapshot(second)['reservations'] == 1
            assert 'PRIVATE' not in str(busy)
            assert busy['generation_scope'] == 'tts_handler'
            assert busy['completion_scope'] == 'synthesis_handler'
        finally:
            release.set()
        assert active.result(timeout=2)['byte_count'] > 0
    after = snapshot(first)
    assert after['state'] == 'idle' and after['reservations'] == 0
    assert after['generation'] > before['generation']


@pytest.mark.parametrize('kind', ['empty', 'raises', 'disabled', 'invalid'])
def test_every_terminal_handler_path_completes_observation(enabled, monkeypatch, kind):
    backend = Mock(return_value=b'')
    if kind == 'raises':
        backend.side_effect = OSError('PRIVATE')
    if kind == 'disabled':
        monkeypatch.setattr(tts.settings, 'TTS_FALLBACK_SAY', False)
    monkeypatch.setattr(tts, '_say_to_wav', backend)
    service = tts.TTSService()
    before = snapshot(service)
    result = service.handle_synthesize_speech({'text': '' if kind == 'invalid' else 'private'})
    assert isinstance(result, dict)
    state = snapshot(service)
    assert state['known'] is True and state['state'] == 'idle'
    assert state['reservations'] == 0
    assert state['generation'] > before['generation']


def test_actual_authenticated_readiness_tracks_real_tts_while_stt_idle(rest, enabled, monkeypatch):
    client, _ = rest
    import backend.rest_server as rs
    service = tts.TTSService()
    client.application.config['REST_DEPS'].tts_service = service
    monkeypatch.setattr(rs, '_REST_ACTIVITY', ActivityReservations())
    monkeypatch.setattr('backend.brain_lease.observe_brain_lease', lambda: {})
    started, release = threading.Event(), threading.Event()

    def synth(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return admission._wav()

    monkeypatch.setattr(tts, '_say_to_wav', synth)
    def send():
        with client.application.test_client() as separate:
            return separate.post('/v1/tts/synthesize', json={'text': 'private'},
                                 headers={'Authorization': 'Bearer test-residency-key'})

    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(send)
        try:
            assert started.wait(1)
            response = client.get('/v1/readiness', headers={'Authorization': 'Bearer test-residency-key'})
            assert response.status_code == 200
            activity = response.json['activity']
            assert 'tts' in activity['components'], 'actual readiness omits TTS work'
            assert activity['components']['native_stt']['state'] == 'idle'
            assert activity['components']['tts']['state'] == 'busy'
            assert activity['state'] == 'busy'
            assert activity['coverage']['complete'] is False
            assert 'tts_handler' in activity['coverage']['tracked']
            assert 'tts_direct_synthesis' in activity['coverage']['untracked']
        finally:
            release.set()
        assert work.result(timeout=2).status_code == 200


def test_readiness_auth_and_unavailable_tts_provider(rest, monkeypatch):
    client, _ = rest
    service = tts.TTSService()
    client.application.config['REST_DEPS'].tts_service = service
    read = Mock(side_effect=RuntimeError('PRIVATE'))
    monkeypatch.setattr(service, 'activity_snapshot', read, raising=False)
    assert client.get('/v1/readiness').status_code == 401
    read.assert_not_called()
    response = client.get('/v1/readiness', headers={'Authorization': 'Bearer test-residency-key'})
    assert response.status_code == 200
    assert 'tts' in response.json['activity']['components'], 'TTS failure must be visible as unknown'
    assert response.json['activity']['components']['tts']['known'] is False
    assert 'PRIVATE' not in str(response.json)


def test_snapshot_contention_is_unknown_without_waiting_or_entering_admission(monkeypatch):
    service = tts.TTSService()
    snapshot(service)
    gate = Mock()
    gate.acquire.side_effect = AssertionError('snapshot must not touch admission')
    monkeypatch.setattr(tts, '_SYNTHESIS_GATE', gate)
    observer = tts._TTS_ACTIVITY
    with observer._lock:
        state = snapshot(service)
        assert state['known'] is False and state['state'] == 'unknown'
        assert state['busy_reasons'] == ['lock_contended']
    assert snapshot(service)['state'] == 'idle'
    gate.acquire.assert_not_called()


def test_post_synthesis_exception_does_not_leak_observer_or_admission(enabled, monkeypatch):
    service = tts.TTSService()
    snapshot(service)
    monkeypatch.setattr(tts, '_say_to_wav', lambda *args, **kwargs: admission._wav())
    with monkeypatch.context() as patcher:
        patcher.setattr(tts.base64, 'b64encode', Mock(side_effect=RuntimeError('synthetic encoding')))
        with pytest.raises(RuntimeError, match='synthetic encoding'):
            service.handle_synthesize_speech({'text': 'private'})
    assert snapshot(service)['state'] == 'idle'
    assert service.handle_synthesize_speech({'text': 'again'})['byte_count'] > 0


def test_readiness_missing_dependencies_are_unknown_without_500(rest, monkeypatch):
    client, _ = rest
    import backend.rest_server as rs

    class MissingDeps:
        def __getattr__(self, name):
            raise RuntimeError('PRIVATE unavailable dependency')

    monkeypatch.setattr(rs, '_deps', lambda: MissingDeps())
    response = client.get('/v1/readiness', headers={'Authorization': 'Bearer test-residency-key'})
    assert response.status_code == 200
    assert response.json['activity']['components']['tts']['state'] == 'unknown'
