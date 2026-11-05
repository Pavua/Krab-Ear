"""Локальный тест допуска Call Assist; не доказывает общий drain."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.call_assist_service import CallAssistService
from backend.recording_core_service import RecordingCoreService


@pytest.mark.parametrize("already_recording", [False, True])
def test_closed_gate_refuses_call_before_state_or_external_effects(already_recording):
    core = RecordingCoreService.__new__(RecordingCoreService)
    core._begin_recording_maintenance()
    recorder = SimpleNamespace(is_recording=already_recording, start=Mock())
    store = SimpleNamespace(load_settings=Mock(return_value={"voice_gateway_url": ""}))
    gateway = SimpleNamespace(start_session=Mock())
    assist = CallAssistService(store, recorder, Mock(), gateway=gateway, recording_core=core)
    state_before = dict(assist._state)

    result = assist.handle_start({})

    assert result == {"ok": False, "error": "maintenance_active"}
    assert assist._state == state_before
    recorder.start.assert_not_called()
    store.load_settings.assert_not_called()
    gateway.start_session.assert_not_called()


@pytest.mark.parametrize("outcome", [None, RuntimeError("synthetic")])
def test_gate_failure_rolls_back_claim_and_prevents_effects(outcome):
    def admit(claim):
        claim()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    recorder = SimpleNamespace(is_recording=False, start=Mock())
    store = SimpleNamespace(load_settings=Mock())
    gateway = SimpleNamespace(start_session=Mock())
    assist = CallAssistService(store, recorder, Mock(), gateway=gateway,
                               recording_core=SimpleNamespace(_try_admit_recording_root=admit))
    before = dict(assist._state)
    assert assist.handle_start({}) == {"ok": False, "error": "maintenance_unknown"}
    assert assist._state == before
    recorder.start.assert_not_called()
    store.load_settings.assert_not_called()
    gateway.start_session.assert_not_called()


def test_interrupted_gate_rolls_back_claim_and_reraises():
    def admit(claim):
        claim()
        raise KeyboardInterrupt

    recorder = SimpleNamespace(is_recording=False, start=Mock())
    store = SimpleNamespace(load_settings=Mock())
    assist = CallAssistService(store, recorder, Mock(),
                               recording_core=SimpleNamespace(_try_admit_recording_root=admit))
    before = dict(assist._state)
    with pytest.raises(KeyboardInterrupt):
        assist.handle_start({})
    assert assist._state == before
    recorder.start.assert_not_called()
    store.load_settings.assert_not_called()


def test_accepted_call_does_not_hold_gate_during_recorder_start():
    core = RecordingCoreService.__new__(RecordingCoreService)
    entered, release, gate_done = threading.Event(), threading.Event(), threading.Event()
    results, gates = [], []

    def start():
        entered.set()
        assert release.wait(5)
        return True

    recorder = SimpleNamespace(is_recording=False, start=start)
    store = SimpleNamespace(load_settings=Mock(return_value={"voice_gateway_url": ""}))
    assist = CallAssistService(store, recorder, Mock(), recording_core=core)
    caller = threading.Thread(target=lambda: results.append(assist.handle_start({})))

    def close_gate():
        gates.append(core._begin_recording_maintenance())
        gate_done.set()

    closer = threading.Thread(target=close_gate)
    try:
        caller.start()
        assert entered.wait(2)
        closer.start()
        assert gate_done.wait(2)
        assert gates[0]["inflight"] == "unknown"
    finally:
        release.set()
        for thread in (caller, closer):
            if thread.ident is not None:
                thread.join(3)
    assert not caller.is_alive() and not closer.is_alive()
    assert results[0]["active"] is True


def test_waiting_call_rechecks_gate_after_state_lock():
    core = RecordingCoreService.__new__(RecordingCoreService)
    recorder = SimpleNamespace(is_recording=False, start=Mock())
    store = SimpleNamespace(load_settings=Mock())
    assist = CallAssistService(store, recorder, Mock(), recording_core=core)
    waiting = threading.Event()
    real_lock = threading.Lock()

    class ObservedLock:
        def __enter__(self):
            waiting.set()
            real_lock.acquire()

        def __exit__(self, *_args):
            real_lock.release()

    assist._lock = ObservedLock()
    results = []
    caller = threading.Thread(target=lambda: results.append(assist.handle_start({})))
    real_lock.acquire()
    try:
        caller.start()
        assert waiting.wait(2)
        core._begin_recording_maintenance()
    finally:
        real_lock.release()
        if caller.ident is not None:
            caller.join(3)
    assert not caller.is_alive()
    assert results == [{"ok": False, "error": "maintenance_active"}]
    assert assist._state["active"] is False
    recorder.start.assert_not_called()
    store.load_settings.assert_not_called()


def test_gate_waits_until_admitted_root_is_claimed():
    core = RecordingCoreService.__new__(RecordingCoreService)
    core._ensure_recording_lifecycle_state()
    claimed, release, close_attempted, gate_done = (threading.Event() for _ in range(4))
    admitted, observed_busy = [], []

    class ObservedLock:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            if threading.current_thread().name == "gate-closer":
                acquired = self.lock.acquire(blocking=False)
                observed_busy.append(not acquired)
                close_attempted.set()
                if acquired:
                    return
            self.lock.acquire()

        def __exit__(self, *_args):
            self.lock.release()

    core._recording_maintenance_lock = ObservedLock()

    def claim():
        claimed.set()
        assert release.wait(5)

    caller = threading.Thread(target=lambda: admitted.append(core._try_admit_recording_root(claim)))
    closer = threading.Thread(
        name="gate-closer",
        target=lambda: (core._begin_recording_maintenance(), gate_done.set()),
    )
    try:
        caller.start()
        assert claimed.wait(2)
        closer.start()
        assert close_attempted.wait(2)
        assert observed_busy == [True]
    finally:
        release.set()
        for thread in (caller, closer):
            if thread.ident is not None:
                thread.join(3)
    assert not caller.is_alive() and not closer.is_alive()
    assert admitted == [True]
    assert gate_done.is_set()
