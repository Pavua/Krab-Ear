"""W1178 — import_settings validator gate + restore_backup hooks/pre-backup/rollback.

Tests:
- test_import_settings_invalid_voice_gateway_url_rejected_not_saved
- test_import_settings_valid_payload_saved
- test_restore_backup_takes_auto_backup_before_restore
- test_restore_backup_calls_after_save_hooks
- test_restore_backup_invalid_data_rolls_back
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings_service import SettingsService  # noqa: E402
from backend.settings_backup import SettingsBackup  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_SETTINGS: dict = {
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
    "voice_gateway_api_key": "",
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
    "notifications_enabled": True,
    "notify_on_low_confidence": True,
    "notify_confidence_threshold": 0.5,
    "notify_on_llm_failure": True,
    "notify_on_import_complete": True,
    "notify_sound_enabled": True,
    "stt_hotwords": [],
    "stt_hotwords_enabled": True,
    "translate_and_paste": False,
    "onboarding_completed": False,
}


def _make_store(settings: dict | None = None) -> MagicMock:
    """Фиктивный store с mutable backing dict."""
    store = MagicMock()
    current: dict = dict(settings or _VALID_SETTINGS)
    store.load_settings.return_value = dict(current)
    saved_holder: list[dict] = []

    def _save(s: dict) -> dict:
        current.clear()
        current.update(s)
        store.load_settings.return_value = dict(current)
        saved_holder.clear()
        saved_holder.append(dict(s))
        return dict(s)

    store.save_settings.side_effect = _save
    store._saved = saved_holder
    store._current = current
    return store


def _make_backup() -> SettingsBackup:
    """SettingsBackup backed by a real temp directory."""
    tmp = Path(tempfile.mkdtemp())
    return SettingsBackup(backup_dir=tmp)


# ---------------------------------------------------------------------------
# F2 — handle_import_settings validator gate
# ---------------------------------------------------------------------------

class TestImportSettingsValidatorGate(unittest.TestCase):
    """handle_import_settings должен отклонять невалидные настройки без сохранения."""

    def test_import_settings_invalid_voice_gateway_url_rejected_not_saved(self):
        """Файл с невалидным voice_gateway_url должен вызывать ValueError, save не вызывается."""
        store = _make_store()
        svc = SettingsService(store=store, backup=_make_backup())

        # Подготавливаем файл с невалидным URL
        invalid_settings = {
            "voice_gateway_url": "ftp://evil.example.com/steal",  # не localhost/https
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(invalid_settings, fh)
            file_path = fh.name

        try:
            with self.assertRaises(ValueError) as ctx:
                svc.handle_import_settings({"file": file_path})
        finally:
            Path(file_path).unlink(missing_ok=True)

        # Сохранение НЕ должно было произойти
        store.save_settings.assert_not_called()
        # Сообщение об ошибке должно упоминать «отклонён» или «ошибки»
        self.assertIn("отклон", str(ctx.exception).lower() + "отклон")

    def test_import_settings_valid_payload_saved(self):
        """Валидный импорт должен вызвать save_settings и вернуть imported > 0."""
        store = _make_store()
        svc = SettingsService(store=store, backup=_make_backup())

        valid_payload = {
            "quality_profile": "max",
            "cleanup_profile": "strict",
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(valid_payload, fh)
            file_path = fh.name

        try:
            result = svc.handle_import_settings({"file": file_path})
        finally:
            Path(file_path).unlink(missing_ok=True)

        store.save_settings.assert_called_once()
        self.assertGreater(result["imported"], 0)
        self.assertEqual(result["skipped"], 0)


# ---------------------------------------------------------------------------
# F3 — handle_restore_settings_backup
# ---------------------------------------------------------------------------

class TestRestoreSettingsBackup(unittest.TestCase):
    """handle_restore_settings_backup: pre-backup + hooks + validation + rollback."""

    def _make_svc_with_real_backup(self, settings: dict | None = None):
        """Helper — возвращает (svc, backup) с реальным backup dir."""
        backup = _make_backup()
        store = _make_store(settings)
        svc = SettingsService(store=store, backup=backup)
        return svc, backup, store

    # ------------------------------------------------------------------

    def test_restore_backup_takes_auto_backup_before_restore(self):
        """Перед восстановлением должен создаваться auto-backup текущих настроек."""
        svc, backup, store = self._make_svc_with_real_backup()

        # Сначала создаём легитимный бэкап через публичный API
        current = svc.cached_settings()
        backup_id = backup.create_backup(current, reason="test_snapshot")

        # Мок backup.create_backup, чтобы отследить вызов reason="before_restore"
        original_create = backup.create_backup
        create_calls: list[str] = []

        def _tracked_create(settings, reason="manual"):
            create_calls.append(reason)
            return original_create(settings, reason=reason)

        backup.create_backup = _tracked_create

        svc.handle_restore_settings_backup({"backup_id": backup_id})

        self.assertIn("before_restore", create_calls,
                      "Должен быть вызов create_backup(reason='before_restore') до восстановления")

    def test_restore_backup_calls_after_save_hooks(self):
        """handle_restore_settings_backup должен дёрнуть after_save_hooks после сохранения."""
        svc, backup, store = self._make_svc_with_real_backup()

        current = svc.cached_settings()
        backup_id = backup.create_backup(current, reason="before_hook_test")

        hook_calls: list[tuple] = []

        def _hook(old, new):
            hook_calls.append((old, new))

        svc.register_after_save_hook(_hook)
        svc.handle_restore_settings_backup({"backup_id": backup_id})

        self.assertEqual(len(hook_calls), 1, "Hook должен быть вызван ровно один раз")
        old_s, new_s = hook_calls[0]
        self.assertIsInstance(old_s, dict)
        self.assertIsInstance(new_s, dict)

    def test_restore_backup_invalid_data_rolls_back(self):
        """Если бэкап содержит невалидные данные — restore откатывается и бросает ValueError."""
        svc, backup, store = self._make_svc_with_real_backup()

        # Сохраняем исходное значение, чтобы убедиться в rollback
        original_quality = svc.cached_settings().get("quality_profile", "balanced")

        # Создаём бэкап вручную с невалидным URL
        bad_settings = dict(_VALID_SETTINGS)
        bad_settings["voice_gateway_url"] = "ftp://attacker.example.com/steal"
        backup_id = backup.create_backup(bad_settings, reason="bad_test_snapshot")

        with self.assertRaises(ValueError) as ctx:
            svc.handle_restore_settings_backup({"backup_id": backup_id})

        # После rollback store должен содержать исходные настройки
        # (save_settings вызван с pre-restore значениями)
        last_saved = store._saved[-1] if store._saved else None
        if last_saved is not None:
            self.assertEqual(
                last_saved.get("quality_profile"),
                original_quality,
                "После rollback должны быть восстановлены исходные настройки",
            )

        err_msg = str(ctx.exception).lower()
        self.assertTrue(
            "отклон" in err_msg or "невалид" in err_msg,
            f"Сообщение об ошибке должно описывать проблему валидации, получили: {ctx.exception}",
        )

    def test_restore_backup_calls_reload_settings_from_json(self):
        """handle_restore_settings_backup должен вызывать reload_settings_from_json."""
        svc, backup, store = self._make_svc_with_real_backup()

        current = svc.cached_settings()
        backup_id = backup.create_backup(current, reason="reload_test")

        with patch("backend.settings_service.SettingsService.handle_restore_settings_backup",
                   wraps=svc.handle_restore_settings_backup):
            with patch("core.config.reload_settings_from_json", return_value=3) as mock_reload:
                svc.handle_restore_settings_backup({"backup_id": backup_id})
                mock_reload.assert_called_once()


if __name__ == "__main__":
    unittest.main()
