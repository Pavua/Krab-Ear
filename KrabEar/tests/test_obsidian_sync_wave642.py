"""Wave 642 — ObsidianSyncManager error-path tests.

Covers:
1. vault_dir missing → configure() raises ValueError (sync skipped)
2. Permission denied on state file → _load_state() logs warning, keeps defaults
3. Malformed state.json → state resets to defaults
4. .md write fail (disk full mock) → error recorded in SyncResult, no crash
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# --- path bootstrap ---
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.obsidian_sync import ObsidianSyncManager, _DEFAULT_FOLDER


class TestObsidianSyncMissingVault(unittest.TestCase):
    """vault_dir missing → configure() raises ValueError."""

    def test_configure_missing_vault_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = ObsidianSyncManager(data_dir=Path(tmp))
            non_existent = Path(tmp) / "no_such_vault"
            with self.assertRaises(ValueError) as ctx:
                mgr.configure(str(non_existent))
            self.assertIn("не существует", str(ctx.exception))

    def test_sync_without_configure_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = ObsidianSyncManager(data_dir=Path(tmp))
            # vault not configured → RuntimeError
            with self.assertRaises(RuntimeError) as ctx:
                mgr.sync([])
            self.assertIn("не настроен", str(ctx.exception))


class TestObsidianSyncPermDenied(unittest.TestCase):
    """Permission denied on state file → _load_state() falls back silently."""

    def test_load_state_perm_denied_keeps_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "obsidian_sync.json"
            state_path.write_text(
                json.dumps({"vault_path": None, "folder": "X", "last_sync_ts": "2026-01-01T00:00:00"}),
                encoding="utf-8",
            )
            # Simulate PermissionError during read_text
            with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
                mgr = ObsidianSyncManager(data_dir=Path(tmp))
            # Should fall back to defaults without crashing
            self.assertIsNone(mgr._vault_path)
            self.assertEqual(mgr._folder, _DEFAULT_FOLDER)
            self.assertIsNone(mgr._last_sync_ts)


class TestObsidianSyncMalformedState(unittest.TestCase):
    """Malformed state.json → state loads with defaults, no crash."""

    def test_malformed_json_resets_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "obsidian_sync.json"
            state_path.write_text("{this is not: valid json}", encoding="utf-8")
            mgr = ObsidianSyncManager(data_dir=Path(tmp))
            # Malformed JSON should not crash; defaults remain
            self.assertIsNone(mgr._vault_path)
            self.assertEqual(mgr._folder, _DEFAULT_FOLDER)
            self.assertIsNone(mgr._last_sync_ts)

    def test_state_with_nonexistent_vault_path_ignores_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "obsidian_sync.json"
            state_path.write_text(
                json.dumps({
                    "vault_path": "/nonexistent/vault/that/does/not/exist",
                    "folder": "MyNotes",
                    "last_sync_ts": "2026-01-01T00:00:00+00:00",
                }),
                encoding="utf-8",
            )
            mgr = ObsidianSyncManager(data_dir=Path(tmp))
            # vault_path that doesn't exist should be skipped
            self.assertIsNone(mgr._vault_path)
            # other fields still loaded
            self.assertEqual(mgr._folder, "MyNotes")
            self.assertEqual(mgr._last_sync_ts, "2026-01-01T00:00:00+00:00")


class TestObsidianSyncMdWriteFail(unittest.TestCase):
    """.md write fail (disk full mock) → error in SyncResult, no crash."""

    def _make_item(self, idx: int) -> dict:
        return {
            "id": f"item-{idx:04d}",
            "ts": f"2026-05-24T{idx:02d}:00:00+00:00",
            "text": f"Transcription text {idx}",
            "translated_text": "",
            "translation_mode": "off",
            "source_lang": "ru",
            "target_lang": "",
            "tags": [],
            "diarization": None,
            "confidence": 0.95,
        }

    def test_write_fail_disk_full_records_error(self):
        with tempfile.TemporaryDirectory() as tmp_data, tempfile.TemporaryDirectory() as tmp_vault:
            mgr = ObsidianSyncManager(data_dir=Path(tmp_data))
            mgr.configure(str(tmp_vault))

            items = [self._make_item(i) for i in range(3)]

            # Simulate OSError (disk full) on every .md write
            with patch.object(Path, "write_text", side_effect=OSError(28, "No space left on device")):
                result = mgr.sync(items, force=True)

            self.assertEqual(result.synced_count, 0)
            self.assertEqual(len(result.errors), 3)
            # Each error message references the item id
            for i, err in enumerate(result.errors):
                self.assertIn(f"item-{i:04d}", err)

    def test_partial_write_fail_continues_remaining_items(self):
        """If one item fails, the rest are still attempted."""
        with tempfile.TemporaryDirectory() as tmp_data, tempfile.TemporaryDirectory() as tmp_vault:
            mgr = ObsidianSyncManager(data_dir=Path(tmp_data))
            mgr.configure(str(tmp_vault))

            items = [self._make_item(i) for i in range(4)]

            call_count = 0
            original_write = Path.write_text

            def flaky_write(self_path, content, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise OSError(28, "No space left on device")
                return original_write(self_path, content, **kwargs)

            with patch.object(Path, "write_text", flaky_write):
                result = mgr.sync(items, force=True)

            # 3 succeed, 1 fails
            self.assertEqual(result.synced_count, 3)
            self.assertEqual(len(result.errors), 1)


if __name__ == "__main__":
    unittest.main()
