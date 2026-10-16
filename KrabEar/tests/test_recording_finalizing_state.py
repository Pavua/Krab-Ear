"""Getter должен видеть тяжёлый stop-tail, включая параллельную новую запись."""
import threading

from test_recording_stop_gate import _make_service


def test_actual_stop_exposes_finalization_until_tail_completes(tmp_path):
    service = _make_service(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    errors = []
    result = {}
    original = service._stop_recording_phase_c

    def blocked_phase_c(*args, **kwargs):
        entered.set()
        if not release.wait(15):
            raise TimeoutError('phase C barrier not released')
        return original(*args, **kwargs)

    service._stop_recording_phase_c = blocked_phase_c
    started = service.handle_start_recording({'source': 'dictation'})

    def stop():
        try:
            result.update(service.handle_stop_recording({
                'generation_token': started['generation_token'],
            }))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=stop)
    worker.start()
    try:
        assert entered.wait(10)
        assert started['generation_token'] in service._finalizing_generations
        state = service.handle_get_recording_state({})
        assert state['is_recording'] is False
        assert state['owner'] is None and state['generation_token'] is None
        assert state.get('finalizing_count') == 1
        assert state.get('is_finalizing') is True

        second = service.handle_start_recording({'source': 'meeting'})
        state = service.handle_get_recording_state({})
        assert state['is_recording'] is True
        assert state['generation_token'] == second['generation_token']
        assert state['finalizing_count'] == 1
        release.set()
        worker.join(10)
        assert not worker.is_alive() and not errors
        assert result['status'] == 'ok'
        state = service.handle_get_recording_state({})
        assert state['finalizing_count'] == 0 and state['is_finalizing'] is False
        assert state['generation_token'] == second['generation_token']
        service.handle_stop_recording({'generation_token': second['generation_token']})
    finally:
        release.set()
        worker.join(10)
        service.close_background_workers()
    assert not worker.is_alive()


def test_fresh_idle_exposes_explicit_zero_finalization(tmp_path):
    service = _make_service(tmp_path)
    try:
        state = service.handle_get_recording_state({})
        assert type(state.get('finalizing_count')) is int
        assert state['finalizing_count'] == 0
        assert state.get('is_finalizing') is False
    finally:
        service.close_background_workers()
