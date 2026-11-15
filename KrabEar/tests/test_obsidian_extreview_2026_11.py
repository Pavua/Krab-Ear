"""Внешнее ревью E3 (2026-11-15): Obsidian-синхронизация.

6а. purge удалял ВСЕ ``*.md`` в папке: чужие заметки владельца, если папка
    синхронизации совпала с рабочей. Теперь только ``transcript_*.md``.
6в. Запись шла ``write_text`` по пути, который мог оказаться symlink на чужой
    файл вне vault (проверялся каталог, не файл). Теперь запись с O_NOFOLLOW.
6г. ``_load_state`` принимал vault из state-файла без проверки ``$HOME``
    (проверка жила только в ``handle_configure``): подменённый файл состояния
    перенаправлял запись транскриптов в произвольный каталог.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from _plaintext_export_test_helpers import off_authorizer

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.obsidian_sync import ObsidianSyncManager  # noqa: E402


def _item() -> dict:
    return {
        "id": "abc12345-0000-0000-0000-000000000000",
        "ts": datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
        "text": "Секретный текст.",
        "translated_text": "",
        "translation_mode": "off",
        "source_lang": "ru",
        "target_lang": "",
        "tags": [],
        "diarization": None,
        "confidence": None,
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.mgr = ObsidianSyncManager(
            data_dir=str(self.data_dir), plaintext_export_authorizer=off_authorizer()
        )
        self.mgr.configure(str(self.vault), folder="Transcriptions")
        self.target = self.vault / "Transcriptions"
        self.target.mkdir(exist_ok=True)


class PurgeOnlyRemovesOwnTranscripts(_Base):
    def test_foreign_notes_in_sync_folder_survive_purge(self):
        mine = self.target / "transcript_2026-11-15_12-00-00_abc12345.md"
        mine.write_text("---\nsource: krab-ear\n---\n", encoding="utf-8")
        foreign = [
            self.target / "Daily 2026-11-15.md",
            self.target / "Идеи.md",
            self.target / "notes.md",
        ]
        for f in foreign:
            f.write_text("заметка владельца", encoding="utf-8")

        deleted = self.mgr.purge_all_synced_files()

        self.assertEqual(deleted, 1)
        self.assertFalse(mine.exists())
        for f in foreign:
            self.assertTrue(f.exists(), f"{f.name} — чужая заметка, purge её снёс")


class WriteDoesNotFollowSymlink(_Base):
    def test_symlinked_note_path_does_not_write_outside_vault(self):
        outside = self.root / "outside_victim.txt"
        outside.write_text("не трогать", encoding="utf-8")
        item = _item()
        link = self.target / self.mgr._make_filename(item)
        os.symlink(outside, link)

        result = self.mgr.sync([item], force=True)

        self.assertEqual(outside.read_text(encoding="utf-8"), "не трогать")
        self.assertTrue(result.errors, "запись через symlink обязана быть отказом")
        self.assertEqual(result.synced_count, 0)

    def test_regular_write_still_works(self):
        item = _item()

        result = self.mgr.sync([item], force=True)

        self.assertFalse(result.errors, result.errors)
        written = self.target / self.mgr._make_filename(item)
        self.assertIn("Секретный текст.", written.read_text(encoding="utf-8"))


class LoadStateRejectsVaultOutsideHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()

    def _load(self, vault: Path) -> ObsidianSyncManager:
        (self.data_dir / "obsidian_sync.json").write_text(
            json.dumps({"vault_path": str(vault), "folder": "Transcriptions", "last_sync_ts": None}),
            encoding="utf-8",
        )
        with mock.patch.object(Path, "home", return_value=self.home):
            return ObsidianSyncManager(
                data_dir=str(self.data_dir), plaintext_export_authorizer=off_authorizer()
            )

    def test_vault_outside_home_from_state_file_is_ignored(self):
        outside = self.root / "elsewhere"
        outside.mkdir()

        mgr = self._load(outside)

        self.assertIsNone(mgr._vault_path)

    def test_vault_inside_forbidden_home_subdir_is_ignored(self):
        ssh = self.home / ".ssh"
        ssh.mkdir()

        mgr = self._load(ssh)

        self.assertIsNone(mgr._vault_path)

    def test_vault_inside_home_is_loaded(self):
        vault = self.home / "Obsidian"
        vault.mkdir()

        mgr = self._load(vault)

        self.assertEqual(mgr._vault_path, vault)


if __name__ == "__main__":
    unittest.main()
