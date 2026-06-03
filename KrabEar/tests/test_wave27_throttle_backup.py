"""Wave-27 MED fixes — ipc_throttle + settings_backup.

FIX D1: transcribe_paths / transcribe_paths_async must be in HEAVY_METHODS
         (≤5/min) — they trigger full MLX STT + file I/O per path, same
         resource cost as bulk_reprocess_start which was already added in
         wave-25.

FIX D2: restore_backup / _parse_file_info call json.load without a size guard.
         Crafted large backup files could exhaust memory. Files > 10 MB are
         rejected with ValueError before json.load is called.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.ipc_throttle import (  # noqa: E402
    HEAVY_METHODS,
    MEDIUM_METHODS,
    EXCLUDED_METHODS,
    IPCThrottle,
    _classify_method,
)
from backend.settings_backup import SettingsBackup  # noqa: E402


# ---------------------------------------------------------------------------
# D1: transcribe_paths in HEAVY_METHODS
# ---------------------------------------------------------------------------

class TestTranscribePathsInHeavyBucket(unittest.TestCase):
    """transcribe_paths and transcribe_paths_async must be heavy-throttled."""

    def test_transcribe_paths_in_heavy_methods(self):
        self.assertIn(
            "transcribe_paths",
            HEAVY_METHODS,
            "transcribe_paths (full MLX STT + file I/O) must be in HEAVY_METHODS",
        )

    def test_transcribe_paths_async_in_heavy_methods(self):
        self.assertIn(
            "transcribe_paths_async",
            HEAVY_METHODS,
            "transcribe_paths_async must be in HEAVY_METHODS",
        )

    def test_transcribe_paths_classified_as_heavy(self):
        self.assertEqual(_classify_method("transcribe_paths"), "heavy")

    def test_transcribe_paths_async_classified_as_heavy(self):
        self.assertEqual(_classify_method("transcribe_paths_async"), "heavy")

    def test_transcribe_paths_not_in_medium_methods(self):
        self.assertNotIn("transcribe_paths", MEDIUM_METHODS)

    def test_transcribe_paths_async_not_in_medium_methods(self):
        self.assertNotIn("transcribe_paths_async", MEDIUM_METHODS)

    def test_transcribe_paths_not_in_excluded_methods(self):
        self.assertNotIn("transcribe_paths", EXCLUDED_METHODS)

    def test_transcribe_paths_async_not_in_excluded_methods(self):
        self.assertNotIn("transcribe_paths_async", EXCLUDED_METHODS)

    def test_heavy_medium_sets_still_disjoint_after_addition(self):
        overlap = HEAVY_METHODS & MEDIUM_METHODS
        self.assertEqual(overlap, set(), f"HEAVY ∩ MEDIUM must be empty; got {overlap}")

    def test_transcribe_paths_rate_limited_to_heavy_cap(self):
        """transcribe_paths must be rejected after 5 calls (default heavy cap)."""
        throttle = IPCThrottle()
        allowed = sum(1 for _ in range(6) if throttle.check_rate("transcribe_paths"))
        self.assertEqual(allowed, 5, "Default heavy cap is 5/min")

    def test_transcribe_paths_async_rate_limited_to_heavy_cap(self):
        throttle = IPCThrottle()
        allowed = sum(1 for _ in range(6) if throttle.check_rate("transcribe_paths_async"))
        self.assertEqual(allowed, 5, "Default heavy cap is 5/min")

    def test_transcribe_paths_sixth_call_returns_false(self):
        throttle = IPCThrottle()
        for _ in range(5):
            throttle.check_rate("transcribe_paths")
        self.assertFalse(
            throttle.check_rate("transcribe_paths"),
            "6th call must be rejected (heavy bucket exhausted)",
        )

    def test_transcribe_paths_has_positive_wait_time_when_exhausted(self):
        throttle = IPCThrottle(limits={"heavy": 1, "medium": 30, "light": 120})
        throttle.check_rate("transcribe_paths")
        wait = throttle.get_wait_time("transcribe_paths")
        self.assertGreater(wait, 0.0, "Wait time must be positive when bucket is empty")
        self.assertLessEqual(wait, 60.0)

    def test_transcribe_paths_stats_category_is_heavy(self):
        throttle = IPCThrottle()
        throttle.check_rate("transcribe_paths")
        stats = throttle.get_throttle_stats()
        info = stats["methods"].get("transcribe_paths", {})
        self.assertEqual(info.get("category"), "heavy")
        self.assertEqual(info.get("limit_per_minute"), 5)

    def test_bulk_reprocess_start_still_in_heavy(self):
        """Wave-25 addition must not have been accidentally removed."""
        self.assertIn("bulk_reprocess_start", HEAVY_METHODS)


# ---------------------------------------------------------------------------
# D2: settings_backup size guard
# ---------------------------------------------------------------------------

class TestSettingsBackupSizeGuard(unittest.TestCase):
    """restore_backup must reject files > 10 MB with ValueError."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup = SettingsBackup(backup_dir=Path(self.tmp))

    def _write_raw_backup(self, backup_id: str, content: bytes) -> Path:
        """Write arbitrary bytes to the backup directory under backup_id.json."""
        path = Path(self.tmp) / f"{backup_id}.json"
        path.write_bytes(content)
        return path

    def test_restore_normal_backup_still_works(self):
        """Normal (small) backup must continue to round-trip correctly."""
        bid = self.backup.create_backup({"quality_profile": "balanced"}, reason="sz_ok")
        result = self.backup.restore_backup(bid)
        self.assertEqual(result["quality_profile"], "balanced")

    def test_restore_oversized_file_raises_value_error(self):
        """Backup file > 10 MB must raise ValueError before json.load."""
        oversized_id = "20260101T000000_000000Z_evil"
        # Write 10 MB + 1 byte of garbage — enough to cross the threshold.
        _MAX = 10 * 1024 * 1024
        self._write_raw_backup(
            oversized_id,
            b"x" * (_MAX + 1),
        )
        with self.assertRaises(ValueError, msg="Oversized file must raise ValueError"):
            self.backup.restore_backup(oversized_id)

    def test_restore_exactly_at_limit_is_accepted(self):
        """A file exactly at the 10 MB limit must not be rejected by the size guard.

        We write a valid JSON dict padded to exactly 10 MB with a long string value;
        restore_backup must return without raising ValueError (JSON may still be
        invalid depending on content, but the size guard itself must pass).
        """
        _MAX = 10 * 1024 * 1024
        # Create a dict whose JSON serialization is exactly _MAX bytes or just under.
        # Use a padding string: '{"k": "' + 'a'*N + '"}' = N + 9 chars.
        padding_len = _MAX - 9  # {"k": "..."}
        payload = json.dumps({"k": "a" * padding_len}).encode("utf-8")
        # Ensure we're at or below the limit
        self.assertLessEqual(len(payload), _MAX)
        exact_id = "20260101T000001_000000Z_exact"
        self._write_raw_backup(exact_id, payload)
        # Must not raise ValueError from size guard
        result = self.backup.restore_backup(exact_id)
        self.assertIn("k", result)

    def test_restore_file_one_byte_over_limit_raises(self):
        """File at exactly limit+1 bytes must raise ValueError."""
        _MAX = 10 * 1024 * 1024
        over_id = "20260101T000002_000000Z_over"
        self._write_raw_backup(over_id, b"A" * (_MAX + 1))
        with self.assertRaises(ValueError):
            self.backup.restore_backup(over_id)

    def test_parse_file_info_oversized_returns_zero_keys(self):
        """_parse_file_info must skip json.load for oversized files (returns 0 keys)."""
        _MAX = 10 * 1024 * 1024
        oversized_id = "20260101T000003_000000Z_biglist"
        path = self._write_raw_backup(
            oversized_id,
            b"x" * (_MAX + 1),
        )
        info = self.backup._parse_file_info(path)
        self.assertIsNotNone(info)
        # Key count must be 0 (skipped due to size) rather than raising
        self.assertEqual(
            info["settings_count_keys"],
            0,
            "_parse_file_info must return settings_count_keys=0 for oversized file",
        )

    def test_list_backups_with_oversized_file_does_not_raise(self):
        """list_backups must not raise even if one backup file is oversized."""
        _MAX = 10 * 1024 * 1024
        # Create one normal backup
        self.backup.create_backup({"normal": True}, reason="ok")
        # Plant one oversized file
        oversized_id = "20260101T000004_000000Z_bloated"
        self._write_raw_backup(oversized_id, b"X" * (_MAX + 100))
        # Must not raise
        try:
            backups = self.backup.list_backups()
        except Exception as exc:
            self.fail(f"list_backups raised unexpectedly: {exc}")
        # The normal backup must still be present
        ids = [b["backup_id"] for b in backups]
        self.assertTrue(
            any("ok" in bid for bid in ids),
            "Normal backup must appear in list despite oversized sibling",
        )

    def test_size_error_message_contains_file_size(self):
        """ValueError for oversized file must mention the actual size."""
        _MAX = 10 * 1024 * 1024
        big_id = "20260101T000005_000000Z_verbose"
        size = _MAX + 999
        self._write_raw_backup(big_id, b"Z" * size)
        with self.assertRaises(ValueError) as ctx:
            self.backup.restore_backup(big_id)
        self.assertIn(str(size), str(ctx.exception))


# ---------------------------------------------------------------------------
# D2: SENSITIVE_FIELDS completeness — wave-20 fields must be covered
# ---------------------------------------------------------------------------

class TestSensitiveFieldsWave20Coverage(unittest.TestCase):
    """llm_api_key / smtp_password / ipc_signing_secret must be in SENSITIVE_FIELDS."""

    def _import_sensitive_fields(self):
        from backend.settings_backup import SENSITIVE_FIELDS
        return SENSITIVE_FIELDS

    def test_llm_api_key_in_sensitive_fields(self):
        sf = self._import_sensitive_fields()
        self.assertIn("llm_api_key", sf)

    def test_smtp_password_in_sensitive_fields(self):
        sf = self._import_sensitive_fields()
        self.assertIn("smtp_password", sf)

    def test_ipc_signing_secret_in_sensitive_fields(self):
        sf = self._import_sensitive_fields()
        self.assertIn("ipc_signing_secret", sf)

    def test_wave20_fields_redacted_from_backup_file(self):
        """All three wave-20 credentials must be absent from written backup bytes."""
        tmp = tempfile.mkdtemp()
        backup = SettingsBackup(backup_dir=Path(tmp))
        bid = backup.create_backup({
            "quality_profile": "balanced",
            "llm_api_key": "lm-studio-secret",
            "smtp_password": "hunter2",
            "ipc_signing_secret": "hmac-abc",
        })
        path = Path(tmp) / f"{bid}.json"
        raw = path.read_bytes()
        self.assertNotIn(b"lm-studio-secret", raw)
        self.assertNotIn(b"hunter2", raw)
        self.assertNotIn(b"hmac-abc", raw)


if __name__ == "__main__":
    unittest.main()
