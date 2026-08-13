"""Unit tests для SettingsBackup и новых IPC-хендлеров backup/restore.

Проверяет:
- create_backup создаёт файл в backup_dir
- backup_id включает reason-метку
- list_backups возвращает правильные метаданные
- list_backups сортировка: новейший первый
- auto-prune: >10 бэкапов → обрезается до MAX_BACKUPS
- restore_backup round-trip: данные совпадают
- restore_backup с несуществующим id → FileNotFoundError
- чувствительные поля не пишутся в бэкап
- handle_set_settings автоматически создаёт бэкап
- handle_list_settings_backups возвращает список
- handle_restore_settings_backup сохраняет и инвалидирует кэш
- handle_create_manual_settings_backup с пользовательским reason
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

from backend.settings_backup import MAX_BACKUPS, SettingsBackup
from backend.settings_service import SettingsService


# ---------------------------------------------------------------------------
# Helpers
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
    "onboarding_completed": False,
    "translate_and_paste": False,
}


def _make_store(settings: dict | None = None) -> MagicMock:
    store = MagicMock()
    current: dict = dict(settings or _BASE_SETTINGS)

    def load_settings(lock_timeout_sec: float | None = None, nowait: bool = False):
        return dict(current)

    def save_settings(new_settings):
        current.clear()
        current.update(new_settings)
        return dict(current)

    store.load_settings.side_effect = load_settings
    store.save_settings.side_effect = save_settings
    return store


# ---------------------------------------------------------------------------
# SettingsBackup unit tests
# ---------------------------------------------------------------------------

class TestSettingsBackupCreate(unittest.TestCase):
    """create_backup создаёт файл с правильным именем и содержимым."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup = SettingsBackup(backup_dir=Path(self.tmp))

    def test_creates_file(self):
        backup_id = self.backup.create_backup({"key": "value"})
        path = Path(self.tmp) / f"{backup_id}.json"
        self.assertTrue(path.exists(), f"Backup file not found: {path}")

    def test_backup_id_contains_reason(self):
        backup_id = self.backup.create_backup({"x": 1}, reason="before_set")
        self.assertIn("before_set", backup_id)

    def test_backup_id_contains_timestamp(self):
        backup_id = self.backup.create_backup({})
        # Timestamp part: YYYYMMDDTHHMMSSz  (16 chars)
        ts_part = backup_id[:16]
        self.assertTrue(ts_part[0].isdigit(), "Should start with year digits")

    def test_file_content_round_trip(self):
        data = {"quality_profile": "max", "auto_paste": False}
        backup_id = self.backup.create_backup(data, reason="test")
        path = Path(self.tmp) / f"{backup_id}.json"
        with path.open() as f:
            loaded = json.load(f)
        self.assertEqual(loaded["quality_profile"], "max")
        self.assertFalse(loaded["auto_paste"])

    def test_sensitive_fields_excluded(self):
        data = {"quality_profile": "balanced", "voice_gateway_api_key": "secret"}
        backup_id = self.backup.create_backup(data)
        restored = self.backup.restore_backup(backup_id)
        self.assertNotIn("voice_gateway_api_key", restored)
        self.assertIn("quality_profile", restored)


class TestSettingsBackupList(unittest.TestCase):
    """list_backups возвращает правильные метаданные."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup = SettingsBackup(backup_dir=Path(self.tmp))

    def test_empty_dir_returns_empty_list(self):
        self.assertEqual(self.backup.list_backups(), [])

    def test_list_contains_metadata(self):
        self.backup.create_backup({"a": 1}, reason="manual")
        backups = self.backup.list_backups()
        self.assertEqual(len(backups), 1)
        item = backups[0]
        self.assertIn("backup_id", item)
        self.assertIn("ts", item)
        self.assertIn("reason", item)
        self.assertIn("file_size", item)
        self.assertIn("settings_count_keys", item)

    def test_list_sorted_newest_first(self):
        self.backup.create_backup({"x": 1}, reason="first")
        self.backup.create_backup({"x": 2}, reason="second")
        backups = self.backup.list_backups()
        ids = [b["backup_id"] for b in backups]
        # Newest should be first because filenames sort lexicographically (descending)
        self.assertGreaterEqual(ids[0], ids[-1])

    def test_list_settings_count_keys_accurate(self):
        self.backup.create_backup({"a": 1, "b": 2, "c": 3}, reason="count_test")
        backups = self.backup.list_backups()
        self.assertEqual(backups[0]["settings_count_keys"], 3)


class TestSettingsBackupPrune(unittest.TestCase):
    """Auto-prune: держит не более MAX_BACKUPS файлов."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup = SettingsBackup(backup_dir=Path(self.tmp))

    def test_prune_keeps_max_backups(self):
        import time
        for i in range(MAX_BACKUPS + 3):
            self.backup.create_backup({"i": i}, reason=f"step{i}")
            time.sleep(0.01)  # ensure unique timestamps

        files = list(Path(self.tmp).glob("*.json"))
        self.assertLessEqual(
            len(files),
            MAX_BACKUPS,
            f"Expected ≤{MAX_BACKUPS} files, got {len(files)}",
        )


class TestSettingsBackupRestore(unittest.TestCase):
    """restore_backup возвращает правильные данные."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup = SettingsBackup(backup_dir=Path(self.tmp))

    def test_restore_round_trip(self):
        original = {"quality_profile": "max", "auto_paste": False, "mode": "headless"}
        backup_id = self.backup.create_backup(original, reason="test_restore")
        restored = self.backup.restore_backup(backup_id)
        self.assertEqual(restored["quality_profile"], "max")
        self.assertFalse(restored["auto_paste"])

    def test_restore_missing_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            self.backup.restore_backup("nonexistent_backup_id")

    def test_restore_empty_id_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.backup.restore_backup("")


# ---------------------------------------------------------------------------
# SettingsService integration tests for backup IPC handlers
# ---------------------------------------------------------------------------

class TestSettingsServiceBackupIPC(unittest.TestCase):
    """IPC handlers handle_list_settings_backups, handle_restore_settings_backup,
    handle_create_manual_settings_backup."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup_mgr = SettingsBackup(backup_dir=Path(self.tmp))
        self.store = _make_store()
        self.svc = SettingsService(store=self.store, backup=self.backup_mgr)

    def test_set_settings_triggers_auto_backup(self):
        """handle_set_settings должен создать 1 бэкап перед записью."""
        self.svc.handle_set_settings({"auto_paste": False})
        backups = self.backup_mgr.list_backups()
        self.assertGreaterEqual(len(backups), 1, "Auto-backup expected after set_settings")

    def test_auto_backup_reason_is_before_set(self):
        self.svc.handle_set_settings({"auto_paste": False})
        backups = self.backup_mgr.list_backups()
        self.assertEqual(backups[0]["reason"], "before_set")

    def test_handle_list_settings_backups_returns_list(self):
        # Create a manual backup first
        self.backup_mgr.create_backup({"x": 1}, reason="manual")
        result = self.svc.handle_list_settings_backups({})
        self.assertIn("backups", result)
        self.assertIsInstance(result["backups"], list)
        self.assertGreaterEqual(len(result["backups"]), 1)

    def test_handle_list_settings_backups_limit(self):
        for i in range(5):
            self.backup_mgr.create_backup({"i": i})
        result = self.svc.handle_list_settings_backups({"limit": 2})
        self.assertLessEqual(len(result["backups"]), 2)

    def test_handle_restore_settings_backup_round_trip(self):
        """Restore сохраняет настройки из бэкапа и инвалидирует кэш."""
        backup_data = dict(_BASE_SETTINGS)
        backup_data["quality_profile"] = "max"
        backup_id = self.backup_mgr.create_backup(backup_data, reason="pre_restore")

        result = self.svc.handle_restore_settings_backup({"backup_id": backup_id})
        self.assertEqual(result["backup_id"], backup_id)
        self.assertIn("restored_settings", result)
        # Store.save_settings should have been called with restored data
        self.store.save_settings.assert_called()

    def test_handle_restore_settings_backup_missing_id_raises(self):
        with self.assertRaises(ValueError):
            self.svc.handle_restore_settings_backup({"backup_id": ""})

    def test_handle_restore_settings_backup_nonexistent_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.svc.handle_restore_settings_backup({"backup_id": "does_not_exist"})

    def test_handle_create_manual_settings_backup(self):
        result = self.svc.handle_create_manual_settings_backup({"reason": "pre_update"})
        self.assertIn("backup_id", result)
        self.assertIn("settings_count_keys", result)
        self.assertIn("pre_update", result["backup_id"])

    def test_handle_create_manual_settings_backup_default_reason(self):
        result = self.svc.handle_create_manual_settings_backup({})
        self.assertIn("manual", result["backup_id"])

    def test_handle_create_manual_settings_backup_creates_file(self):
        result = self.svc.handle_create_manual_settings_backup({"reason": "ui_click"})
        backup_id = result["backup_id"]
        path = Path(self.tmp) / f"{backup_id}.json"
        self.assertTrue(path.exists())


# ---------------------------------------------------------------------------
# Wave 104: additional coverage tests
# ---------------------------------------------------------------------------

class TestSettingsBackupAtomicWrite(unittest.TestCase):
    """test_atomic_write: backup writes to .tmp file then renames atomically."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup = SettingsBackup(backup_dir=Path(self.tmp))

    def test_atomic_write_no_partial_file_left(self):
        """No .tmp residue should remain after a successful backup."""
        self.backup.create_backup({"key": "value"}, reason="atomic_test")
        tmp_files = list(Path(self.tmp).glob("*.tmp"))
        self.assertEqual(tmp_files, [], "No .tmp residue should remain after backup")

    def test_atomic_write_final_json_parseable(self):
        """Written file must be valid JSON (not corrupted mid-write)."""
        backup_id = self.backup.create_backup({"a": 1, "b": 2}, reason="integrity")
        path = Path(self.tmp) / f"{backup_id}.json"
        with path.open() as f:
            data = json.load(f)
        self.assertIsInstance(data, dict)


class TestSettingsBackupUnicode(unittest.TestCase):
    """test_unicode_in_settings: cyrillic/emoji values survive round-trip."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup = SettingsBackup(backup_dir=Path(self.tmp))

    def test_unicode_round_trip(self):
        data = {
            "device_name": "Микрофон встроенный",
            "label": "Краб Ухо 🦀",
            "note": "日本語テスト",
        }
        backup_id = self.backup.create_backup(data, reason="unicode")
        restored = self.backup.restore_backup(backup_id)
        self.assertEqual(restored["device_name"], "Микрофон встроенный")
        self.assertEqual(restored["label"], "Краб Ухо 🦀")
        self.assertEqual(restored["note"], "日本語テスト")

    def test_unicode_reason_sanitized(self):
        """Unicode reason must not crash and must produce a valid backup_id."""
        backup_id = self.backup.create_backup({"x": 1}, reason="перед_обновлением")
        path = Path(self.tmp) / f"{backup_id}.json"
        self.assertTrue(path.exists())


class TestSettingsBackupConcurrent(unittest.TestCase):
    """test_concurrent_backup_safe: multiple threads can call create_backup simultaneously."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup = SettingsBackup(backup_dir=Path(self.tmp))

    def test_concurrent_backup_safe(self):
        import threading
        errors: list[Exception] = []

        def _create(i: int) -> None:
            try:
                self.backup.create_backup({"i": i}, reason=f"thread{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_create, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent backup raised: {errors}")
        # After pruning, should have at most MAX_BACKUPS files
        files = list(Path(self.tmp).glob("*.json"))
        self.assertLessEqual(len(files), MAX_BACKUPS)


class TestRestoreBackupPathTraversal(unittest.TestCase):
    """W929 F1 — restore_backup must reject path-traversal backup_id values."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup = SettingsBackup(backup_dir=Path(self.tmp))

    def test_restore_backup_rejects_path_traversal(self):
        """backup_id containing '..' escaping the backup dir must raise ValueError."""
        traversal_ids = [
            "../../etc/passwd",
            "../sibling/evil",
            "/absolute/path",
        ]
        for bad_id in traversal_ids:
            with self.subTest(backup_id=bad_id):
                with self.assertRaises(ValueError, msg=f"Expected ValueError for {bad_id!r}"):
                    self.backup.restore_backup(bad_id)

    def test_restore_backup_accepts_valid_id(self):
        """A normal backup_id (no traversal) must still work after the guard."""
        bid = self.backup.create_backup({"key": "value"}, reason="test")
        restored = self.backup.restore_backup(bid)
        self.assertEqual(restored, {"key": "value"})


if __name__ == "__main__":
    unittest.main()
