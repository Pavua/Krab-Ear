"""Source-only gate для новых start RecordingCoreService; не global fence."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from backend.recording_core_service import RecordingCoreService


def make_service():
    service = RecordingCoreService.__new__(RecordingCoreService)
    service.recorder = SimpleNamespace(is_recording=False)
    service._handle_start_recording_locked = Mock(return_value={"status": "recording"})
    return service


def test_disabled_by_default_and_closed_gate_is_sticky_unknown():
    service = make_service()
    assert service.handle_start_recording({}) == {"status": "recording"}
    assert service._begin_recording_maintenance() == {
        "new_starts": "blocked", "inflight": "unknown", "preservation": "unknown",
    }
    assert service.handle_start_recording({}) == {"status": "maintenance_active"}
    assert service._begin_recording_maintenance()["preservation"] == "unknown"
    assert service._handle_start_recording_locked.call_count == 1


def test_start_accepted_before_gate_can_finish_but_new_start_is_rejected():
    service = make_service()
    entered, release = threading.Event(), threading.Event()
    results = []

    def accepted_start(_params):
        entered.set()
        assert release.wait(4)
        return {"status": "recording"}

    service._handle_start_recording_locked.side_effect = accepted_start
    thread = threading.Thread(target=lambda: results.append(service.handle_start_recording({})))
    try:
        thread.start()
        assert entered.wait(2)
        assert service._begin_recording_maintenance()["inflight"] == "unknown"
        assert service.handle_start_recording({}) == {"status": "maintenance_active"}
    finally:
        release.set()
        if thread.ident is not None:
            thread.join(3)
    assert not thread.is_alive()
    assert results == [{"status": "recording"}]


def test_existing_strict_generation_stop_is_not_blocked_by_gate():
    service = make_service()
    service._active_generation = {"token": "generation", "owner": "dictation", "revision": 1}
    service._begin_recording_maintenance()
    params = {"generation_token": "generation", "source": "dictation", "expected_owner_revision": 1}
    assert service._stop_gate_decision(params) is None
    service._settings_svc = SimpleNamespace(cached_settings=lambda: {})
    service._stop_recording_phase_a = Mock(return_value={"early_return": {"status": "stopped"}})
    assert service.handle_stop_recording(params) == {"status": "stopped"}
    service._stop_recording_phase_a.assert_called_once()
    assert service._recording_maintenance_status()["preservation"] == "unknown"


def test_existing_strict_stop_rejects_wrong_owner_after_gate():
    service = make_service()
    service._active_generation = {"token": "generation", "owner": "dictation", "revision": 1}
    service._report_owner_mismatch = Mock()
    service._begin_recording_maintenance()
    params = {"generation_token": "generation", "source": "meeting", "expected_owner_revision": 1}
    assert service._stop_gate_decision(params)["status"] == "owner_mismatch"
    assert service._recording_maintenance_status()["preservation"] == "unknown"


def test_waiting_start_rechecks_gate_under_lifecycle_lock():
    service = make_service()
    waiting = threading.Event()
    lock = threading.Lock()

    class ObservedLock:
        def __enter__(self):
            waiting.set()
            lock.acquire()

        def __exit__(self, *_args):
            lock.release()

    service._recording_lifecycle_lock = ObservedLock()
    results = []
    thread = threading.Thread(target=lambda: results.append(service.handle_start_recording({})))
    lock.acquire()
    try:
        thread.start()
        assert waiting.wait(2)
        service._begin_recording_maintenance()
    finally:
        lock.release()
        if thread.ident is not None:
            thread.join(3)
    assert not thread.is_alive()
    assert results == [{"status": "maintenance_active"}]
    service._handle_start_recording_locked.assert_not_called()
