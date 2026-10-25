"""Настоящий worker не выдаёт raw text при неизвестном ACK или policy отказе."""
from unittest.mock import Mock, patch

import numpy as np
import pytest
from backend.live_subs_service import LiveSubsService


@pytest.mark.parametrize("status", ["privacy", "stale", "unknown", "rejected", "busy"])
def test_policy_or_uncertain_commit_suppresses_ws_and_sse_text(status):
    sink = Mock()
    sink.begin_window.return_value = {"status": "ready", "ticket": "a" * 32}
    sink.commit_window.return_value = {"status": status}
    transcriber = Mock()
    transcriber.transcribe.return_value = {"text": "private phrase", "language": "ru"}
    svc = LiveSubsService(transcriber, Mock(), replay_sink=sink)
    try:
        with patch("backend.live_subs_service.event_bus") as bus:
            result = svc.ingest(np.zeros(16000, dtype=np.int16).tobytes(), 16000, "off", True)
        assert result["text"] == "" and result.get("translation") is None
        assert result["replay_status"] == status
        bus.emit_typed.assert_not_called()
        assert "private phrase" not in repr(bus.emit_envelope.call_args_list)
    finally:
        svc.stop()


def test_journal_accepted_emits_hint_without_recording_again():
    sink = Mock()
    ticket = "a" * 32
    sink.begin_window.return_value = {"status": "ready", "ticket": ticket}
    sink.commit_window.return_value = {"status": "accepted", "seq": 1}
    transcriber = Mock()
    transcriber.transcribe.return_value = {"text": "phrase", "language": "ru"}
    svc = LiveSubsService(transcriber, Mock(), replay_sink=sink)
    try:
        with patch("backend.live_subs_service.event_bus") as bus:
            result = svc.ingest(np.zeros(16000, dtype=np.int16).tobytes(), 16000, "off", True)
        assert result["event_id"] == ticket and result["replay_status"] == "accepted"
        bus.emit_typed.assert_not_called()
        envelope = bus.emit_envelope.call_args.args[0]
        assert envelope["origin"] == "ipc" and envelope["data"]["text"] == "phrase"
    finally:
        svc.stop()


def test_unavailable_legacy_rechecks_privacy_after_stt_before_any_delivery():
    state = {}
    sink = Mock()
    sink.begin_window.return_value = {"status": "unavailable"}
    transcriber = Mock()
    def stt(*args, **kwargs):
        state["privacy_mode_enabled"] = True
        return {"text": "private phrase", "language": "ru"}
    transcriber.transcribe.side_effect = stt
    svc = LiveSubsService(transcriber, Mock(), settings_get=lambda k, d: state.get(k, d), replay_sink=sink)
    try:
        with patch("backend.live_subs_service.event_bus") as bus:
            result = svc.ingest(np.zeros(16000, dtype=np.int16).tobytes(), 16000, "off", True)
        assert result["text"] == ""
        bus.emit_typed.assert_not_called()
        bus.emit_envelope.assert_not_called()
    finally:
        svc.stop()


def test_denied_begin_never_transcribes_old_audio():
    sink = Mock()
    sink.begin_window.return_value = {"status": "stale", "reconnect_required": True}
    transcriber = Mock()
    svc = LiveSubsService(transcriber, Mock(), replay_sink=sink)
    try:
        result = svc.ingest(np.zeros(16000, dtype=np.int16).tobytes(), 16000, "off", True)
        assert result["text"] == "" and result["reconnect_required"]
        transcriber.transcribe.assert_not_called()
        sink.commit_window.assert_not_called()
    finally:
        svc.stop()


def test_privacy_changed_during_commit_suppresses_all_delivery():
    state = {}
    sink = Mock()
    sink.begin_window.return_value = {"status": "ready", "ticket": "a" * 32}
    def commit(*args):
        state["privacy_mode_enabled"] = True
        return {"status": "accepted", "seq": 1}
    sink.commit_window.side_effect = commit
    transcriber = Mock()
    transcriber.transcribe.return_value = {"text": "private phrase", "language": "ru"}
    svc = LiveSubsService(transcriber, Mock(), settings_get=lambda k, d: state.get(k, d), replay_sink=sink)
    try:
        with patch("backend.live_subs_service.event_bus") as bus:
            result = svc.ingest(np.zeros(16000, dtype=np.int16).tobytes(), 16000, "off", True)
        assert result["text"] == "" and result["replay_status"] == "privacy"
        bus.emit_typed.assert_not_called()
        bus.emit_envelope.assert_not_called()
    finally:
        svc.stop()


def test_replay_hints_keep_existing_ws_event_timestamp_contract():
    sink = Mock()
    sink.begin_window.return_value = {"status": "unavailable"}
    transcriber = Mock()
    transcriber.transcribe.return_value = {"text": "phrase", "language": "ru"}
    svc = LiveSubsService(transcriber, Mock(), replay_sink=sink)
    try:
        with patch("backend.live_subs_service.event_bus") as bus:
            svc.ingest(np.zeros(16000, dtype=np.int16).tobytes(), 16000, "off", True)
        envelope = bus.emit_envelope.call_args.args[0]
        assert isinstance(envelope.get("ts"), str) and envelope["ts"]
    finally:
        svc.stop()


@pytest.mark.parametrize("status", ["stale", "busy", "rejected", "invalid", "conflict", "error"])
def test_suppressed_worker_result_publishes_metadata_gap(status):
    with patch("backend.live_subs_service.event_bus") as bus:
        result = LiveSubsService._replay_suppressed({"status": status})
    bus.emit_envelope.assert_called_once()
    envelope = bus.emit_envelope.call_args.args[0]
    assert envelope["data"]["replay_status"] == status
    assert envelope["data"].get("reconnect_required") == result.get("reconnect_required")
    assert "text" not in envelope["data"]
    assert "translation" not in envelope["data"]
