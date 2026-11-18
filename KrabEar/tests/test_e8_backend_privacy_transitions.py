"""Переходы приватности на границе настроек, IPC и буфера PCM."""
import base64
from unittest.mock import Mock, patch

import pytest

from backend.live_subs_service import LiveSubsService
from backend.service import BackendService
from backend.settings_service import SettingsService
from backend.state_store import StateStore


def test_existing_profile_losing_settings_cannot_confirm_off(tmp_path):
    store = StateStore(tmp_path / "profile")
    store.initialize_startup_plaintext_policy(new_profile=True)
    svc = SettingsService(store=store)
    backend = object.__new__(BackendService)
    backend.store = store
    backend._settings_svc = svc
    with patch("backend.crypto_keystore.keychain_available", return_value=True):
        assert svc.handle_get_settings({})["privacy_mode_enabled"] is False
        assert backend._handle_get_encryption_status({})["enabled"] is False
        store.settings_path.unlink()
        assert svc.handle_get_settings({})["privacy_mode_enabled"] is True
        status = backend._handle_get_encryption_status({})
        assert status["ok"] is False
        assert status["enabled"] is None


def test_privacy_commit_discards_partial_audio_without_client_stop():
    live = LiveSubsService(Mock(), Mock())
    try:
        live.ingest(bytes(320), 16000, "off", False)
        assert live.buffer_duration_sec(16000) > 0
        live.on_settings_saved({"privacy_mode_enabled": False}, {"privacy_mode_enabled": True})
        assert live.buffer_duration_sec(16000) == 0
    finally:
        live.close()


def test_discard_during_decode_invalidates_chunk():
    live = LiveSubsService(Mock(), Mock())
    decode = live._decode_audio

    def decode_across_reset(*args):
        live.reset()
        return decode(*args)

    try:
        with patch.object(live, "_decode_audio", side_effect=decode_across_reset):
            live.ingest(bytes(320), 16000, "off", False)
        assert live.buffer_duration_sec(16000) == 0
    finally:
        live.close()


@pytest.fixture
def epoch_capture(tmp_path):
    store = StateStore(tmp_path / "profile")
    store.initialize_startup_plaintext_policy(new_profile=True)
    writer = StateStore(store.data_dir)

    def read_epoch():
        return store.read_plaintext_policy_snapshot().live_subs_policy_epoch

    transcriber = Mock()
    transcriber.transcribe.return_value = {"text": "fresh subtitle", "language": "en"}
    live = LiveSubsService(transcriber, Mock(), read_capture_epoch=read_epoch)
    try:
        yield live, writer, read_epoch, transcriber
    finally:
        live.close()


def _chunk(epoch, samples):
    return {
        "audio_chunk": base64.b64encode(bytes(2 * samples)).decode(),
        "live_subs_policy_epoch": epoch,
    }


def _cycle_privacy(writer):
    writer.save_settings({"privacy_mode_enabled": True})
    writer.save_settings({"privacy_mode_enabled": False})


@pytest.mark.parametrize("boundary", ["before_ingest", "during_decode"])
def test_delayed_chunk_preserves_new_capture_after_external_privacy_cycle(epoch_capture, boundary):
    live, writer, read_epoch, transcriber = epoch_capture
    old_epoch = read_epoch()
    name = "ingest" if boundary == "before_ingest" else "_decode_audio"
    original = getattr(live, name)

    def resume_after_new_capture(*args, **kwargs):
        _cycle_privacy(writer)
        fresh_epoch = read_epoch()
        assert fresh_epoch and fresh_epoch != old_epoch
        # Свежий чанк входит обычным IPC-путём, без задержки для старого.
        with patch.object(live, name, original):
            assert live.handle_ingest(_chunk(fresh_epoch, 320))["status"] == "accepted"
        return original(*args, **kwargs)

    with patch.object(live, name, side_effect=resume_after_new_capture):
        response = live.handle_ingest(_chunk(old_epoch, 160))
    assert live.buffer_duration_sec() == 320 / 16000
    assert response == {"ok": True, "skipped": True, "reason": "stale_capture_policy"}
    assert live.handle_stop({"live_subs_policy_epoch": read_epoch()})["flushed"]
    transcriber.transcribe.assert_called_once()


def test_old_snapshot_cannot_replace_queued_window_from_new_epoch(epoch_capture):
    live, writer, read_epoch, transcriber = epoch_capture
    old_epoch = read_epoch()
    submit = live._submit_window

    def submit_old_after_new(**kwargs):
        _cycle_privacy(writer)
        with patch.object(live, "_submit_window", submit):
            live.handle_ingest(_chunk(read_epoch(), 48000))
        return submit(**kwargs)

    with patch.object(live, "_ensure_worker_started"), patch.object(
        live, "_submit_window", side_effect=submit_old_after_new
    ):
        live.handle_ingest(_chunk(old_epoch, 48000))
    live._ensure_worker_started()
    assert live.wait_until_idle()
    transcriber.transcribe.assert_called_once()


def test_delayed_final_ingest_does_not_return_new_epoch_result(epoch_capture):
    live, writer, read_epoch, _ = epoch_capture
    old_epoch = read_epoch()
    await_completion = live._await_completion
    start_worker = live._ensure_worker_started

    def finish_new_epoch_first(seq, timeout):
        _cycle_privacy(writer)
        live.handle_ingest(_chunk(read_epoch(), 48000))
        start_worker()
        assert live.wait_until_idle()
        return await_completion(seq, timeout)

    with patch.object(live, "_ensure_worker_started"), patch.object(
        live, "_await_completion", side_effect=finish_new_epoch_first
    ):
        response = live.handle_ingest({**_chunk(old_epoch, 160), "is_final": True})
    assert response == {"ok": True, "skipped": True, "reason": "stale_capture_policy"}


def test_delayed_privacy_save_hook_keeps_current_epoch_buffer(epoch_capture):
    live, writer, read_epoch, _ = epoch_capture
    _cycle_privacy(writer)
    live.handle_ingest(_chunk(read_epoch(), 320))
    live.on_settings_saved({"privacy_mode_enabled": False}, {"privacy_mode_enabled": True})
    assert live.buffer_duration_sec() == 320 / 16000


def test_delayed_stop_timeout_does_not_cancel_new_epoch_window(epoch_capture):
    live, writer, read_epoch, transcriber = epoch_capture
    old_epoch = read_epoch()
    live.handle_ingest(_chunk(old_epoch, 160))

    def expire_old_stop(_seq, timeout):
        _cycle_privacy(writer)
        live.handle_ingest(_chunk(read_epoch(), 48000))
        return None

    with patch.object(live, "_ensure_worker_started"), patch.object(
        live, "_await_completion", side_effect=expire_old_stop
    ):
        response = live.handle_stop({"live_subs_policy_epoch": old_epoch})
    assert response["reason"] == "flush_timeout"
    live._ensure_worker_started()
    assert live.wait_until_idle()
    transcriber.transcribe.assert_called_once()
