"""W1173 — тесты унификации SENSITIVE_FIELDS между settings_service и settings_backup.

Проверяет:
- settings_service._SENSITIVE_FIELDS содержит все 5 полей, добавленных в W897
  (telnyx_api_key, twilio_account_sid, twilio_auth_token, sentry_dsn, stt_gigaam_hf_token)
- handle_export_settings не включает эти поля в экспортируемый файл
- settings_service._SENSITIVE_FIELDS идентичен settings_backup.SENSITIVE_FIELDS (единая точка истины)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings_backup import SENSITIVE_FIELDS as BACKUP_SENSITIVE_FIELDS
from backend.settings_service import SettingsService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store_with_secrets() -> MagicMock:
    """Создаёт mock store с настройками, включая все 5 новых чувствительных полей."""
    store = MagicMock()
    settings = {
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
        # Базовые чувствительные поля (старые)
        "voice_gateway_api_key": "vgw-secret",
        "hf_token": "hf-secret",
        "rest_api_key": "rest-secret",
        "lm_studio_api_key": "lm-secret",
        # 5 новых полей, добавленных W897 в settings_backup но отсутствовавших
        # в старом settings_service._SENSITIVE_FIELDS
        "telnyx_api_key": "KEY_telnyx_plaintext_leak",
        "twilio_account_sid": "ACtwilio_plaintext_leak",
        "twilio_auth_token": "tok_twilio_plaintext_leak",
        "sentry_dsn": "https://secret@sentry.io/123",
        "stt_gigaam_hf_token": "hf_gigaam_plaintext_leak",
        # Обычное поле (должно остаться в экспорте)
        "language": "ru",
    }
    store.load_settings.return_value = dict(settings)
    store.save_settings.side_effect = lambda s: s
    return store


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

class TestSensitiveFieldsUnification(unittest.TestCase):
    """W1173: единая точка истины для SENSITIVE_FIELDS."""

    def test_settings_export_constants_match_settings_backup(self):
        """SettingsService._SENSITIVE_FIELDS должен быть идентичен settings_backup.SENSITIVE_FIELDS."""
        self.assertEqual(
            SettingsService._SENSITIVE_FIELDS,
            BACKUP_SENSITIVE_FIELDS,
            "settings_service._SENSITIVE_FIELDS расходится с settings_backup.SENSITIVE_FIELDS — "
            "нарушает единую точку истины (W1173)",
        )

    def test_settings_service_has_telnyx_api_key_in_sensitive(self):
        """_SENSITIVE_FIELDS должен содержать telnyx_api_key."""
        self.assertIn("telnyx_api_key", SettingsService._SENSITIVE_FIELDS)

    def test_settings_service_has_twilio_credentials_in_sensitive(self):
        """_SENSITIVE_FIELDS должен содержать twilio_account_sid и twilio_auth_token."""
        self.assertIn("twilio_account_sid", SettingsService._SENSITIVE_FIELDS)
        self.assertIn("twilio_auth_token", SettingsService._SENSITIVE_FIELDS)

    def test_settings_service_has_sentry_dsn_in_sensitive(self):
        """_SENSITIVE_FIELDS должен содержать sentry_dsn."""
        self.assertIn("sentry_dsn", SettingsService._SENSITIVE_FIELDS)

    def test_settings_service_has_gigaam_token_in_sensitive(self):
        """_SENSITIVE_FIELDS должен содержать stt_gigaam_hf_token."""
        self.assertIn("stt_gigaam_hf_token", SettingsService._SENSITIVE_FIELDS)


class TestExportSettingsRedactsNewFields(unittest.TestCase):
    """W1173: handle_export_settings не должен экспортировать 5 новых чувствительных полей."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.store = _make_store_with_secrets()
        self.svc = SettingsService(store=self.store)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _export_and_load(self) -> dict:
        out_path = Path(self.tmp_dir.name) / "export_test.json"
        self.svc.handle_export_settings({"file": str(out_path)})
        with out_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def test_settings_export_redacts_telnyx_api_key(self):
        """handle_export_settings не должен содержать telnyx_api_key."""
        exported = self._export_and_load()
        self.assertNotIn(
            "telnyx_api_key", exported,
            "telnyx_api_key утекает в plaintext через handle_export_settings (W1168 F1 CRIT)",
        )

    def test_settings_export_redacts_twilio_credentials(self):
        """handle_export_settings не должен содержать twilio_account_sid / twilio_auth_token."""
        exported = self._export_and_load()
        self.assertNotIn(
            "twilio_account_sid", exported,
            "twilio_account_sid утекает в plaintext через handle_export_settings (W1168 F1 CRIT)",
        )
        self.assertNotIn(
            "twilio_auth_token", exported,
            "twilio_auth_token утекает в plaintext через handle_export_settings (W1168 F1 CRIT)",
        )

    def test_settings_export_redacts_sentry_dsn(self):
        """handle_export_settings не должен содержать sentry_dsn."""
        exported = self._export_and_load()
        self.assertNotIn(
            "sentry_dsn", exported,
            "sentry_dsn утекает в plaintext через handle_export_settings (W1168 F1 CRIT)",
        )

    def test_settings_export_redacts_gigaam_token(self):
        """handle_export_settings не должен содержать stt_gigaam_hf_token."""
        exported = self._export_and_load()
        self.assertNotIn(
            "stt_gigaam_hf_token", exported,
            "stt_gigaam_hf_token утекает в plaintext через handle_export_settings (W1168 F1 CRIT)",
        )

    def test_settings_export_still_includes_non_sensitive_field(self):
        """handle_export_settings должен включать обычные (не чувствительные) поля."""
        exported = self._export_and_load()
        self.assertIn(
            "language", exported,
            "Обычные поля должны оставаться в экспорте",
        )
        self.assertEqual(exported["language"], "ru")

    def test_settings_export_also_redacts_legacy_sensitive_fields(self):
        """handle_export_settings должен также редактировать старые чувствительные поля."""
        exported = self._export_and_load()
        for field in ("voice_gateway_api_key", "hf_token", "rest_api_key", "lm_studio_api_key"):
            self.assertNotIn(field, exported, f"Поле {field} не должно быть в экспорте")


class TestImportSettingsIgnoresNewSensitiveFields(unittest.TestCase):
    """W1173: handle_import_settings должен тихо пропускать все 5 новых полей."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.store = _make_store_with_secrets()
        self.svc = SettingsService(store=self.store)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _write_import_file(self, data: dict) -> str:
        p = Path(self.tmp_dir.name) / "import_test.json"
        with p.open("w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return str(p)

    def test_import_skips_telnyx_api_key(self):
        """handle_import_settings должен пропустить telnyx_api_key из входящего файла."""
        path = self._write_import_file({"telnyx_api_key": "INJECTED", "language": "es"})
        result = self.svc.handle_import_settings({"file": path})
        self.assertGreater(result["skipped"], 0)

    def test_import_skips_twilio_credentials(self):
        """handle_import_settings должен пропустить twilio_account_sid + twilio_auth_token."""
        path = self._write_import_file({
            "twilio_account_sid": "INJECTED_SID",
            "twilio_auth_token": "INJECTED_TOK",
            "language": "es",
        })
        result = self.svc.handle_import_settings({"file": path})
        self.assertGreaterEqual(result["skipped"], 2)

    def test_import_skips_sentry_dsn(self):
        """handle_import_settings должен пропустить sentry_dsn из входящего файла."""
        path = self._write_import_file({"sentry_dsn": "https://evil@evil.io/1", "language": "es"})
        result = self.svc.handle_import_settings({"file": path})
        self.assertGreater(result["skipped"], 0)

    def test_import_skips_gigaam_token(self):
        """handle_import_settings должен пропустить stt_gigaam_hf_token из входящего файла."""
        path = self._write_import_file({"stt_gigaam_hf_token": "INJECTED_TOK", "language": "es"})
        result = self.svc.handle_import_settings({"file": path})
        self.assertGreater(result["skipped"], 0)


if __name__ == "__main__":
    unittest.main()
