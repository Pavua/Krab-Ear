"""Тесты backup/restore функциональности HistoryService."""
from __future__ import annotations
from backend.history_service import HistoryService
from backend.state_store import StateStore

import json
import sys
import unittest
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

# Добавляем корень проекта в sys.path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_service(data_dir: Path) -> HistoryService:
    store = StateStore(data_dir)
    return HistoryService(store=store)


class BackupHistoryTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.svc = _make_service(self.data_dir)

    def tearDown(self):
        self._tmpdir.cleanup()

    # ------------------------------------------------------------------
    # handle_backup_history
    # ------------------------------------------------------------------

    def test_backup_creates_directory(self):
        result = self.svc.handle_backup_history({})
        backup_path = Path(result["backup_path"])
        self.assertTrue(backup_path.exists())
        self.assertTrue(backup_path.is_dir())

    def test_backup_copies_history_file(self):
        self.svc.store.add_history_item(text="hello backup")
        result = self.svc.handle_backup_history({})
        backup_dir = Path(result["backup_path"])
        self.assertTrue((backup_dir / "history.ndjson").exists())

    def test_backup_creates_meta_file(self):
        result = self.svc.handle_backup_history({})
        backup_dir = Path(result["backup_path"])
        meta_file = backup_dir / "backup_meta.json"
        self.assertTrue(meta_file.exists())
        meta = json.loads(meta_file.read_text())
        self.assertIn("backup_ts", meta)
        self.assertIn("entries", meta)

    def test_backup_returns_entries_count(self):
        self.svc.store.add_history_item(text="entry one")
        self.svc.store.add_history_item(text="entry two")
        result = self.svc.handle_backup_history({})
        self.assertEqual(result["entries"], 2)

    def test_backup_returns_size_mb(self):
        self.svc.store.add_history_item(text="some text")
        result = self.svc.handle_backup_history({})
        self.assertIn("size_mb", result)
        self.assertIsInstance(result["size_mb"], float)

    def test_backup_inside_backups_subdir(self):
        result = self.svc.handle_backup_history({})
        backup_dir = Path(result["backup_path"])
        self.assertEqual(backup_dir.parent.name, "backups")

    def test_empty_store_backup(self):
        result = self.svc.handle_backup_history({})
        self.assertEqual(result["entries"], 0)
        self.assertIsNotNone(result["backup_path"])

    # ------------------------------------------------------------------
    # handle_list_backups
    # ------------------------------------------------------------------

    def test_list_backups_empty(self):
        result = self.svc.handle_list_backups({})
        self.assertEqual(result["backups"], [])

    def test_list_backups_after_backup(self):
        self.svc.handle_backup_history({})
        result = self.svc.handle_list_backups({})
        self.assertEqual(len(result["backups"]), 1)

    def test_list_backups_returns_metadata(self):
        self.svc.store.add_history_item(text="test entry")
        self.svc.handle_backup_history({})
        result = self.svc.handle_list_backups({})
        entry = result["backups"][0]
        self.assertIn("path", entry)
        self.assertIn("backup_date", entry)
        self.assertIn("entries", entry)
        self.assertIn("size_mb", entry)

    def test_list_backups_multiple(self):
        # Inject distinct timestamps so backup dir names don't collide — no sleep needed.
        ts1 = datetime(2026, 1, 1, 10, 0, 0)
        ts2 = datetime(2026, 1, 1, 10, 0, 2)
        with patch("backend.history_service.datetime") as mock_dt:
            mock_dt.now.side_effect = [ts1, ts2]
            self.svc.handle_backup_history({})
            self.svc.handle_backup_history({})
        result = self.svc.handle_list_backups({})
        self.assertEqual(len(result["backups"]), 2)

    # ------------------------------------------------------------------
    # handle_restore_history
    # ------------------------------------------------------------------

    def test_restore_requires_backup_path(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_restore_history({})

    def test_restore_invalid_path_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_restore_history({"backup_path": "/nonexistent/path"})

    def test_restore_from_backup(self):
        # Добавляем записи и создаём backup
        self.svc.store.add_history_item(text="record one")
        self.svc.store.add_history_item(text="record two")
        backup_result = self.svc.handle_backup_history({})

        # Сбрасываем историю (через compact с пустым файлом)
        self.svc.store.history_path.write_text("", encoding="utf-8")

        # Восстанавливаем
        restore_result = self.svc.handle_restore_history(
            {"backup_path": backup_result["backup_path"]}
        )
        self.assertEqual(restore_result["restored_entries"], 2)

    def test_restore_returns_backup_date(self):
        self.svc.store.add_history_item(text="hello")
        backup_result = self.svc.handle_backup_history({})

        self.svc.store.history_path.write_text("", encoding="utf-8")
        restore_result = self.svc.handle_restore_history(
            {"backup_path": backup_result["backup_path"]}
        )
        self.assertIn("backup_date", restore_result)
        self.assertNotEqual(restore_result["backup_date"], "unknown")

    def test_restore_invalid_dir_no_history_ndjson(self):
        # Папка существует, но не является валидным backup-ом
        empty_dir = self.data_dir / "fake_backup"
        empty_dir.mkdir()
        with self.assertRaises(RuntimeError):
            self.svc.handle_restore_history({"backup_path": str(empty_dir)})

    def test_restore_settings_flag(self):
        # Создаём backup со settings
        self.svc.store.save_settings({"transcription_language": "es"})
        backup_result = self.svc.handle_backup_history({})
        backup_dir = Path(backup_result["backup_path"])
        self.assertTrue((backup_dir / "settings.json").exists())

        # Меняем settings
        self.svc.store.save_settings({"transcription_language": "ru"})

        # Восстанавливаем с флагом restore_settings=True
        self.svc.handle_restore_history({
            "backup_path": str(backup_dir),
            "restore_settings": True,
        })
        restored = self.svc.store.load_settings()
        self.assertEqual(restored.get("transcription_language"), "es")

    def test_restore_without_settings_flag_preserves_settings(self):
        # Изначальные settings
        self.svc.store.save_settings({"transcription_language": "es"})
        backup_result = self.svc.handle_backup_history({})

        # Меняем settings
        self.svc.store.save_settings({"transcription_language": "ru"})

        # Восстанавливаем без флага — settings не трогаем
        self.svc.handle_restore_history({"backup_path": backup_result["backup_path"]})
        restored = self.svc.store.load_settings()
        self.assertEqual(restored.get("transcription_language"), "ru")


if __name__ == "__main__":
    unittest.main()
