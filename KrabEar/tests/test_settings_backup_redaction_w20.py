"""Wave-20 security tests for SettingsBackup.

Covers three hardening changes:
  MED — llm_api_key / smtp_password / ipc_signing_secret must be redacted.
  LOW — backup files must be created with mode 0o600 (not world-readable).
  LOW — two backups created within the same second must produce distinct filenames.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings_backup import SENSITIVE_FIELDS, SettingsBackup


# ---------------------------------------------------------------------------
# MED: credential redaction
# ---------------------------------------------------------------------------

class TestNewSensitiveFieldsRedacted(unittest.TestCase):
    """llm_api_key / smtp_password / ipc_signing_secret must NOT appear in backup."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup = SettingsBackup(backup_dir=Path(self.tmp))

    def _create_and_read(self, settings: dict) -> dict:
        backup_id = self.backup.create_backup(settings, reason="test")
        path = Path(self.tmp) / f"{backup_id}.json"
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    # --- llm_api_key ---

    def test_llm_api_key_not_in_written_file(self):
        """llm_api_key (Bearer token) must not appear in the backup file."""
        data = self._create_and_read({
            "quality_profile": "balanced",
            "llm_api_key": "sk-supersecret-bearer-token-12345",
        })
        self.assertNotIn("llm_api_key", data,
                         "llm_api_key must be redacted from backup")

    def test_llm_api_key_not_in_restore(self):
        """restore_backup round-trip must not return llm_api_key."""
        bid = self.backup.create_backup({
            "mode": "headless",
            "llm_api_key": "exposed-key",
        })
        restored = self.backup.restore_backup(bid)
        self.assertNotIn("llm_api_key", restored)
        self.assertIn("mode", restored)

    def test_llm_api_key_in_sensitive_fields_frozenset(self):
        self.assertIn("llm_api_key", SENSITIVE_FIELDS)

    # --- smtp_password ---

    def test_smtp_password_not_in_written_file(self):
        """smtp_password must not appear in the backup file."""
        data = self._create_and_read({
            "smtp_user": "user@example.com",
            "smtp_password": "hunter2",
        })
        self.assertNotIn("smtp_password", data,
                         "smtp_password must be redacted from backup")
        # Non-secret sibling field must survive.
        self.assertIn("smtp_user", data)

    def test_smtp_password_in_sensitive_fields_frozenset(self):
        self.assertIn("smtp_password", SENSITIVE_FIELDS)

    # --- ipc_signing_secret ---

    def test_ipc_signing_secret_not_in_written_file(self):
        """ipc_signing_secret (HMAC key) must not appear in the backup file."""
        data = self._create_and_read({
            "quality_profile": "max",
            "ipc_signing_secret": "deadbeefdeadbeef" * 4,
        })
        self.assertNotIn("ipc_signing_secret", data,
                         "ipc_signing_secret must be redacted from backup")

    def test_ipc_signing_secret_in_sensitive_fields_frozenset(self):
        self.assertIn("ipc_signing_secret", SENSITIVE_FIELDS)

    # --- all three simultaneously ---

    def test_all_three_fields_redacted_simultaneously(self):
        """All three new sensitive fields are redacted in a single backup."""
        settings = {
            "quality_profile": "balanced",
            "llm_api_key": "lm-studio-api-key-xyz",
            "smtp_password": "correct-horse-battery-staple",
            "ipc_signing_secret": "hmac-shared-secret-abc",
            "auto_paste": True,
        }
        data = self._create_and_read(settings)
        self.assertNotIn("llm_api_key", data)
        self.assertNotIn("smtp_password", data)
        self.assertNotIn("ipc_signing_secret", data)
        # Non-secret fields must survive.
        self.assertEqual(data.get("quality_profile"), "balanced")
        self.assertTrue(data.get("auto_paste"))

    def test_secret_value_not_present_as_raw_string_in_file(self):
        """The raw secret value must not appear anywhere in the file bytes."""
        secret_val = "my-very-unique-llm-api-key-9999"
        bid = self.backup.create_backup({
            "quality_profile": "balanced",
            "llm_api_key": secret_val,
        })
        path = Path(self.tmp) / f"{bid}.json"
        raw = path.read_bytes()
        self.assertNotIn(secret_val.encode(), raw,
                         "Secret value must not appear anywhere in the backup file")


# ---------------------------------------------------------------------------
# LOW: file permissions (0o600)
# ---------------------------------------------------------------------------

class TestBackupFilePermissions(unittest.TestCase):
    """Backup files must be created with mode 0o600."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup = SettingsBackup(backup_dir=Path(self.tmp))

    def test_backup_file_mode_is_0o600(self):
        """Newly created backup file must have permissions 0o600."""
        bid = self.backup.create_backup(
            {"llm_api_key": "sk-secret", "quality_profile": "balanced"},
            reason="perm_test",
        )
        path = Path(self.tmp) / f"{bid}.json"
        file_mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(
            file_mode, 0o600,
            f"Expected file mode 0o600, got 0o{file_mode:o} for {path}",
        )

    def test_backup_file_not_world_readable(self):
        """Backup file must not have world-read (o+r) bit set."""
        bid = self.backup.create_backup(
            {"smtp_password": "hunter2"},
            reason="world_read_test",
        )
        path = Path(self.tmp) / f"{bid}.json"
        file_mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(
            file_mode & 0o004, 0,
            f"World-read bit must not be set; got mode 0o{file_mode:o}",
        )

    def test_backup_file_not_group_readable(self):
        """Backup file must not have group-read (g+r) bit set."""
        bid = self.backup.create_backup(
            {"ipc_signing_secret": "hmac-key"},
            reason="group_read_test",
        )
        path = Path(self.tmp) / f"{bid}.json"
        file_mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(
            file_mode & 0o040, 0,
            f"Group-read bit must not be set; got mode 0o{file_mode:o}",
        )

    def test_backup_file_mode_preserved_after_rename(self):
        """Atomic rename (.tmp → .json) must not reset perms to umask defaults."""
        bid = self.backup.create_backup(
            {"llm_api_key": "secret", "quality_profile": "max"},
            reason="rename_perm",
        )
        path = Path(self.tmp) / f"{bid}.json"
        file_mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(file_mode, 0o600,
                         "Perms must be 0o600 even after atomic rename")


# ---------------------------------------------------------------------------
# LOW: collision-proof filenames (microsecond timestamps)
# ---------------------------------------------------------------------------

class TestCollisionProofFilenames(unittest.TestCase):
    """Two same-second backups with the same reason must produce distinct filenames."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup = SettingsBackup(backup_dir=Path(self.tmp))

    def test_rapid_same_reason_backups_distinct_ids(self):
        """Two rapid backups with the same reason must have different backup_ids."""
        id1 = self.backup.create_backup({"x": 1}, reason="rapid")
        id2 = self.backup.create_backup({"x": 2}, reason="rapid")
        self.assertNotEqual(id1, id2,
                            "Rapid same-reason backups must have distinct IDs")

    def test_rapid_same_reason_backups_distinct_files(self):
        """Both rapid backups must produce distinct on-disk files."""
        id1 = self.backup.create_backup({"a": 1}, reason="burst")
        id2 = self.backup.create_backup({"a": 2}, reason="burst")
        path1 = Path(self.tmp) / f"{id1}.json"
        path2 = Path(self.tmp) / f"{id2}.json"
        self.assertTrue(path1.exists(), f"File for id1 missing: {path1}")
        self.assertTrue(path2.exists(), f"File for id2 missing: {path2}")
        self.assertNotEqual(path1, path2, "Both backup paths must be distinct")

    def test_microsecond_suffix_in_backup_id(self):
        """backup_id must contain microsecond-precision timestamp (underscore after HHMMSSz)."""
        bid = self.backup.create_backup({}, reason="ts_check")
        # New format: YYYYMMDDTHHMMSSµµµµµµZ_reason
        # e.g.        20260602T090930_729900Z_ts_check
        # The underscore between seconds and microseconds must be present.
        # Validate by checking the 15th char (0-indexed) is '_'.
        self.assertEqual(
            bid[15], "_",
            f"Expected '_' at position 15 (microsecond separator), got {bid!r}",
        )
        # The char at position 22 (after 6 microsecond digits) must be 'Z'.
        self.assertEqual(
            bid[22], "Z",
            f"Expected 'Z' at position 22 (UTC marker), got {bid!r}",
        )

    def test_many_rapid_backups_all_distinct(self):
        """10 rapid same-reason backups must all have distinct IDs."""
        ids = [
            self.backup.create_backup({"i": i}, reason="mass")
            for i in range(10)
        ]
        self.assertEqual(
            len(ids), len(set(ids)),
            f"Expected all IDs distinct; duplicates found: {ids}",
        )

    def test_old_format_backup_id_still_parseable_in_list(self):
        """list_backups must gracefully handle legacy (16-char ts) filenames."""
        # Simulate a legacy backup file written before Wave 20.
        legacy_id = "20240425T123456Z_auto"
        legacy_path = Path(self.tmp) / f"{legacy_id}.json"
        legacy_path.write_text('{"quality_profile": "balanced"}', encoding="utf-8")

        backups = self.backup.list_backups()
        ids = [b["backup_id"] for b in backups]
        self.assertIn(legacy_id, ids,
                      "Legacy backup file must still appear in list_backups()")
        # Reason must parse correctly.
        legacy_entry = next(b for b in backups if b["backup_id"] == legacy_id)
        self.assertEqual(legacy_entry["reason"], "auto")


if __name__ == "__main__":
    unittest.main()
