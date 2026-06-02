"""Wave-21: at-rest permission tests for AutoBackupManager.

Verifies that backup dirs are created with mode 0o700 and every
backup file written by _do_backup / _save_meta is chmod-ed to 0o600.
"""

from __future__ import annotations

import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.auto_backup import AutoBackupManager  # noqa: E402


def _make_store(data_dir: Path) -> MagicMock:
    store = MagicMock()
    store.data_dir = str(data_dir)
    store.history_path = data_dir / "history.ndjson"
    store.tombstones_path = data_dir / "tombstones.ndjson"
    store.status_path = data_dir / "status.json"
    store.settings_path = data_dir / "settings.json"
    store.count_active_items.return_value = 3
    # Create non-empty source files so shutil.copy2 has something to copy.
    for attr in ("history_path", "tombstones_path", "status_path", "settings_path"):
        p = getattr(store, attr)
        p.write_text('{"test": 1}', encoding="utf-8")
    return store


def _mode_bits(path: Path) -> int:
    """Return permission bits (e.g. 0o600) for *path*."""
    return stat.S_IMODE(path.stat().st_mode)


class TestAutoBackupPermissions(unittest.TestCase):
    """Backup dir must be 0o700; every file inside must be 0o600."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.store = _make_store(self.data_dir)
        # Use interval_hours=0 so check_and_backup() always runs a backup.
        self.mgr = AutoBackupManager(
            store=self.store,
            interval_hours=0,
            max_copies=7,
            enabled=True,
        )

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_backup_dir_mode_is_0o700(self) -> None:
        result = self.mgr.check_and_backup()
        self.assertTrue(result["backed_up"], "Expected backup to run")
        backup_dir = Path(result["backup_path"])
        self.assertTrue(backup_dir.is_dir())
        self.assertEqual(
            _mode_bits(backup_dir),
            0o700,
            f"backup dir {backup_dir} has mode {oct(_mode_bits(backup_dir))}, want 0o700",
        )

    def test_all_backup_files_mode_is_0o600(self) -> None:
        result = self.mgr.check_and_backup()
        self.assertTrue(result["backed_up"])
        backup_dir = Path(result["backup_path"])
        files = list(backup_dir.iterdir())
        self.assertGreater(len(files), 0, "No files found in backup dir")
        for f in files:
            if f.is_file():
                bits = _mode_bits(f)
                self.assertEqual(
                    bits,
                    0o600,
                    f"{f.name} has mode {oct(bits)}, want 0o600",
                )

    def test_meta_file_mode_is_0o600(self) -> None:
        """The auto_backup_meta.json written by _save_meta must be 0o600."""
        self.mgr.check_and_backup()
        meta_path = self.mgr._meta_path
        self.assertTrue(meta_path.exists())
        self.assertEqual(
            _mode_bits(meta_path),
            0o600,
            f"meta file has mode {oct(_mode_bits(meta_path))}, want 0o600",
        )

    def test_backups_dir_mode_is_0o700(self) -> None:
        self.mgr.check_and_backup()
        backups_dir = self.mgr.backups_dir
        self.assertTrue(backups_dir.is_dir())
        self.assertEqual(
            _mode_bits(backups_dir),
            0o700,
            f"backups_dir has mode {oct(_mode_bits(backups_dir))}, want 0o700",
        )

    def test_settings_backup_redacted_and_0o600(self) -> None:
        """Redacted settings.json copy inside the backup must be 0o600."""
        result = self.mgr.check_and_backup()
        backup_dir = Path(result["backup_path"])
        settings_copy = backup_dir / "settings.json"
        self.assertTrue(settings_copy.exists(), "settings.json not found in backup")
        self.assertEqual(
            _mode_bits(settings_copy),
            0o600,
            f"settings.json copy has mode {oct(_mode_bits(settings_copy))}, want 0o600",
        )

    def test_backup_meta_json_is_0o600(self) -> None:
        """backup_meta.json written inside each backup subdir must be 0o600."""
        result = self.mgr.check_and_backup()
        backup_dir = Path(result["backup_path"])
        bm = backup_dir / "backup_meta.json"
        self.assertTrue(bm.exists(), "backup_meta.json not found inside backup dir")
        self.assertEqual(
            _mode_bits(bm),
            0o600,
            f"backup_meta.json has mode {oct(_mode_bits(bm))}, want 0o600",
        )


if __name__ == "__main__":
    unittest.main()
