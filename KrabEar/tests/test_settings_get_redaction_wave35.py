"""wave-35 CRIT — тесты редактирования секретов в handle_get_settings и
handle_restore_settings_backup.

Проверяет:
- handle_get_settings возвращает 'REDACTED' для всех непустых полей SENSITIVE_FIELDS
- handle_get_settings НЕ трогает пустые значения чувствительных полей
- handle_get_settings возвращает обычные поля без изменений
- handle_restore_settings_backup['restored_settings'] не содержит открытых секретов
- cached_settings() НЕ мутируется редактированием (shallow copy)
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings_backup import SENSITIVE_FIELDS  # noqa: E402
from backend.settings_service import SettingsService  # noqa: E402


# ---------------------------------------------------------------------------
# Base settings dict with all credential fields set to real-looking values
# ---------------------------------------------------------------------------

_BASE_SETTINGS: dict = {
    "quality_profile": "balanced",
    "cleanup_profile": "soft",
    "translation_mode": "off",
    "auto_paste": True,
    "realtime_preview_enabled": True,
    "mode": "headless",
    "translation_style": "neutral",
    "clipboard_mode": "always_copy",
    "update_channel": "stable",
    "translation_glossary": {},
    "text_templates": {},
    "network_mode": "offline_default",
    "hotkey_profile": "default",
    "history_policy": "unlimited",
    "history_text_density": "normal",
    "capture_source_mode": "mic",
    "ui_last_tab": "history",
    "auto_start_enabled": False,
    "show_dock_icon": True,
    "play_start_sound": True,
    "audio_ducking_enabled": True,
    "silence_guard_enabled": True,
    "background_guard_enabled": True,
    "call_notify_default": True,
    "call_auto_summary": True,
    "history_focus_mode": True,
    "voice_gateway_url": "http://127.0.0.1:8090",
    "history_page_size": 50,
    "audio_ducking_percent": 50,
    "stop_tail_trim_ms": 180,
    "silence_guard_rms_threshold": 0.0020,
    "silence_guard_peak_threshold": 0.0120,
    "silence_guard_active_ratio_threshold": 0.015,
    "background_guard_min_peak": 0.025,
    "background_guard_min_rms": 0.0040,
    "background_guard_uniform_frame_threshold": 0.0060,
    "background_guard_max_uniform_active_ratio": 0.92,
    "overlay_opacity_percent": 45,
    "stt_hotwords": [],
    "stt_hotwords_enabled": True,
    "notifications_enabled": True,
    "notify_on_low_confidence": True,
    "notify_confidence_threshold": 0.5,
    "notify_on_llm_failure": True,
    "notify_on_import_complete": True,
    "notify_sound_enabled": True,
    # Credential fields — all non-empty
    "telnyx_api_key": "KEY_telnyx_secret_value",
    "twilio_auth_token": "tok_twilio_secret_value",
    "twilio_account_sid": "ACfake_twilio_sid",
    "voice_gateway_api_key": "vgw-real-secret",
    "smtp_password": "hunter2",
    "ipc_signing_secret": "hmac_key_very_secret",
    "sentry_dsn": "https://user@sentry.io/12345",
    "hf_token": "hf_fakeHuggingFaceToken",
    "lm_studio_api_key": "lmstudio-key",
    "rest_api_key": "rest-api-key",
    "stt_gigaam_hf_token": "hf_gigaam_tok",
    "llm_api_key": "llm-api-key",
    # Ordinary non-sensitive field
    "language": "ru",
}


def _make_store(settings: dict | None = None) -> MagicMock:
    s = dict(settings or _BASE_SETTINGS)
    store = MagicMock()
    store.load_settings.return_value = dict(s)
    store.save_settings.side_effect = lambda d: d
    return store


class TestGetSettingsRedactsSecrets(unittest.TestCase):
    """wave-35: handle_get_settings не отправляет секреты через IPC."""

    def setUp(self):
        self.store = _make_store()
        self.svc = SettingsService(store=self.store)

    def test_telnyx_api_key_is_redacted(self):
        result = self.svc.handle_get_settings({})
        self.assertEqual(result["telnyx_api_key"], "REDACTED",
                         "telnyx_api_key должен быть REDACTED в ответе get_settings")

    def test_twilio_auth_token_is_redacted(self):
        result = self.svc.handle_get_settings({})
        self.assertEqual(result["twilio_auth_token"], "REDACTED",
                         "twilio_auth_token должен быть REDACTED в ответе get_settings")

    def test_twilio_account_sid_is_redacted(self):
        result = self.svc.handle_get_settings({})
        self.assertEqual(result["twilio_account_sid"], "REDACTED",
                         "twilio_account_sid должен быть REDACTED в ответе get_settings")

    def test_voice_gateway_api_key_is_redacted(self):
        result = self.svc.handle_get_settings({})
        self.assertEqual(result["voice_gateway_api_key"], "REDACTED",
                         "voice_gateway_api_key должен быть REDACTED в ответе get_settings")

    def test_smtp_password_is_redacted(self):
        result = self.svc.handle_get_settings({})
        self.assertEqual(result["smtp_password"], "REDACTED",
                         "smtp_password должен быть REDACTED в ответе get_settings")

    def test_ipc_signing_secret_is_redacted(self):
        result = self.svc.handle_get_settings({})
        self.assertEqual(result["ipc_signing_secret"], "REDACTED",
                         "ipc_signing_secret должен быть REDACTED в ответе get_settings")

    def test_sentry_dsn_is_redacted(self):
        result = self.svc.handle_get_settings({})
        self.assertEqual(result["sentry_dsn"], "REDACTED",
                         "sentry_dsn должен быть REDACTED в ответе get_settings")

    def test_hf_token_is_redacted(self):
        result = self.svc.handle_get_settings({})
        self.assertEqual(result["hf_token"], "REDACTED",
                         "hf_token должен быть REDACTED в ответе get_settings")

    def test_all_sensitive_fields_are_redacted(self):
        """Все поля из SENSITIVE_FIELDS (которые непустые) должны стать REDACTED."""
        result = self.svc.handle_get_settings({})
        for field in SENSITIVE_FIELDS:
            if _BASE_SETTINGS.get(field):
                self.assertEqual(
                    result[field], "REDACTED",
                    f"Поле {field!r} должно быть REDACTED в ответе get_settings",
                )

    def test_non_sensitive_field_is_unchanged(self):
        """Обычные поля не должны изменяться."""
        result = self.svc.handle_get_settings({})
        self.assertEqual(result["language"], "ru")
        self.assertEqual(result["quality_profile"], "balanced")

    def test_empty_credential_stays_empty_not_redacted(self):
        """Пустое значение чувствительного поля НЕ должно заменяться на REDACTED —
        UI использует пустую строку как сигнал 'не сконфигурировано'."""
        settings = dict(_BASE_SETTINGS)
        settings["telnyx_api_key"] = ""
        store = _make_store(settings)
        svc = SettingsService(store=store)

        result = svc.handle_get_settings({})
        self.assertEqual(result["telnyx_api_key"], "",
                         "Пустой telnyx_api_key НЕ должен заменяться на REDACTED")

    def test_cached_settings_not_mutated_by_redaction(self):
        """Редактирование в handle_get_settings не должно мутировать внутренний кэш.

        Повторный вызов handle_get_settings должен по-прежнему возвращать REDACTED
        (не пустую строку и не сбитый кэш).
        """
        result1 = self.svc.handle_get_settings({})
        self.assertEqual(result1["telnyx_api_key"], "REDACTED")

        # Если кэш был мутирован, второй вызов вернёт "" (falsy) и не заредактирует
        result2 = self.svc.handle_get_settings({})
        self.assertEqual(result2["telnyx_api_key"], "REDACTED",
                         "Второй вызов должен также возвращать REDACTED — кэш не должен мутироваться")


class TestRestoreSettingsBackupRedactsResponse(unittest.TestCase):
    """wave-35: handle_restore_settings_backup не возвращает секреты в restored_settings."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.store = _make_store()
        self.svc = SettingsService(store=self.store)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_restore_response_does_not_leak_telnyx_key(self):
        """restored_settings в ответе restore не должен содержать telnyx_api_key в открытом виде."""
        backup_dir = Path(self.tmp_dir.name) / "backups"
        backup_dir.mkdir()

        from backend.settings_backup import SettingsBackup
        bk = SettingsBackup(backup_dir=backup_dir)
        # backup strips sensitive fields — but the live store has them; after restore
        # handle_restore_settings_backup preserves credentials from current store (W1337 F2)
        # and the response must still redact them.
        safe = {k: v for k, v in _BASE_SETTINGS.items() if k not in SENSITIVE_FIELDS}
        backup_id = bk.create_backup(safe, reason="test")

        svc = SettingsService(store=self.store, backup=bk)
        result = svc.handle_restore_settings_backup({"backup_id": backup_id})
        restored = result["restored_settings"]

        # The telnyx_api_key was preserved from current store (W1337 F2) and must be REDACTED
        # OR absent (if the current store's value is also empty/missing).
        telnyx = restored.get("telnyx_api_key", "")
        self.assertNotEqual(
            telnyx, "KEY_telnyx_secret_value",
            "telnyx_api_key не должен возвращаться в открытом виде из handle_restore_settings_backup",
        )

    def test_restore_response_redacts_all_non_empty_sensitive_fields(self):
        """Все непустые чувствительные поля в restored_settings должны быть REDACTED."""
        backup_dir = Path(self.tmp_dir.name) / "backups"
        backup_dir.mkdir()

        from backend.settings_backup import SettingsBackup
        bk = SettingsBackup(backup_dir=backup_dir)
        safe = {k: v for k, v in _BASE_SETTINGS.items() if k not in SENSITIVE_FIELDS}
        backup_id = bk.create_backup(safe, reason="test")

        svc = SettingsService(store=self.store, backup=bk)
        result = svc.handle_restore_settings_backup({"backup_id": backup_id})
        restored = result["restored_settings"]

        for field in SENSITIVE_FIELDS:
            if restored.get(field):
                self.assertEqual(
                    restored[field], "REDACTED",
                    f"Поле {field!r} должно быть REDACTED в restore response",
                )


if __name__ == "__main__":
    unittest.main()
