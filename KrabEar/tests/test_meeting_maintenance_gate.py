"""Проверки локального допуска meeting до preflight, без живого аудио."""
from unittest.mock import Mock

import pytest

from backend.recording_core_service import RecordingCoreService
from backend.meeting_session_service import _MeetingSession
from tests.test_meeting_session_service_W_C2a import _make_svc


def test_closed_gate_refuses_meeting_before_reservation_or_worker():
    service, _, _ = _make_svc()
    core = RecordingCoreService.__new__(RecordingCoreService)
    core._begin_recording_maintenance()
    core.handle_start_recording = Mock(return_value={"status": "maintenance_active"})
    service._recording_core = core
    service._start_worker = Mock()
    service._stop_worker = Mock(return_value=True)

    result = service.handle_meeting_start({})

    assert result == {"ok": False, "error": "maintenance_active"}
    assert service._session is None
    assert service._setup_done_event.is_set()
    service._start_worker.assert_not_called()
    service._stop_worker.assert_not_called()
    core.handle_start_recording.assert_not_called()


def test_unknown_gate_rolls_back_reservation_after_claim():
    service, _, _ = _make_svc()

    def unknown_after_claim(claim):
        claim()
        return None

    service._recording_core._try_admit_recording_root = unknown_after_claim
    service._start_worker = Mock()
    result = service.handle_meeting_start({})

    assert result == {"ok": False, "error": "maintenance_unknown"}
    assert service._session is None
    assert service._setup_done_event.is_set()
    service._start_worker.assert_not_called()


def test_missing_gate_refuses_before_preflight():
    service, _, _ = _make_svc()
    service._recording_core = object()
    service._start_worker = Mock()

    result = service.handle_meeting_start({})

    assert result == {"ok": False, "error": "maintenance_unknown"}
    assert service._session is None
    assert service._setup_done_event.is_set()
    service._start_worker.assert_not_called()


def test_unknown_gate_restores_previous_privacy_stopped_session():
    service, _, _ = _make_svc()
    previous = _MeetingSession()
    previous.privacy_stopped = True
    service._session = previous
    service._setup_done_event.clear()

    def unknown_after_claim(claim):
        claim()
        return None

    service._recording_core._try_admit_recording_root = unknown_after_claim
    assert service.handle_meeting_start({}) == {"ok": False, "error": "maintenance_unknown"}
    assert service._session is previous
    assert not service._setup_done_event.is_set()


def test_interrupted_gate_restores_state_and_reraises():
    service, _, _ = _make_svc()

    def interrupted_after_claim(claim):
        claim()
        raise KeyboardInterrupt

    service._recording_core._try_admit_recording_root = interrupted_after_claim
    service._start_worker = Mock()
    with pytest.raises(KeyboardInterrupt):
        service.handle_meeting_start({})
    assert service._session is None
    assert service._setup_done_event.is_set()
    service._start_worker.assert_not_called()


def test_existing_meeting_replay_does_not_require_new_admission():
    service, _, _ = _make_svc()
    service._session = _MeetingSession()
    service._recording_core._try_admit_recording_root = Mock(return_value=False)
    result = service.handle_meeting_start({})
    assert result["ok"] is True and result["already_active"] is True
    service._recording_core._try_admit_recording_root.assert_not_called()


def test_stop_of_existing_meeting_remains_available_when_gate_closed():
    service, _, _ = _make_svc()
    try:
        started = service.handle_meeting_start({})
        assert started["ok"] is True
        service._recording_core._try_admit_recording_root = Mock(return_value=False)
        result = service.handle_meeting_stop({"generation_token": started["generation_token"]})
        assert result["ok"] is True
        assert len(service._recording_core.stopped) == 1
    finally:
        service.close()


def test_gate_closed_after_reservation_cleans_up_preflight():
    service, _, _ = _make_svc()
    core = RecordingCoreService.__new__(RecordingCoreService)
    core.handle_start_recording = Mock(wraps=core.handle_start_recording)
    service._recording_core = core
    service._start_worker = Mock(side_effect=core._begin_recording_maintenance)
    service._stop_worker = Mock(return_value=True)

    with pytest.raises(RuntimeError, match="recorder start rejected"):
        service.handle_meeting_start({})
    service._start_worker.assert_called_once()
    service._stop_worker.assert_called_once()
    assert service._session is None
    assert service._setup_done_event.is_set()
