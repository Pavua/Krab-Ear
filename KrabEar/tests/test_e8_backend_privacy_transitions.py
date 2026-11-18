"""Переходы приватности на границе настроек, IPC и буфера PCM."""
from unittest.mock import Mock, patch

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
