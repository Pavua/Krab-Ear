"""C3a wave — privacy-mode gate regression test for ObsidianSyncManager.handle_sync.

Sibling-gate asymmetry (CLAUDE.md "Recurring bug classes"): handle_create_apple_note
(apple_integration_service.py) already gates on privacy_mode_enabled before writing
transcript text to Apple Notes. ObsidianSyncManager.handle_sync writes the same kind
of transcript text to .md files in a user's Obsidian vault but had NO such gate —
first live caller is the C3a quick-capture flow (main+QuickCapture.swift ->
run_obsidian_sync), so with privacy mode on, a quick-capture note's text would
still land on disk in the vault.

This test asserts:
  1. privacy_mode_enabled=True -> handle_sync() returns {"ok": False, ...} and
     does NOT write any .md file to the configured vault.
  2. privacy_mode_enabled=False (or no settings_get provided) -> handle_sync()
     proceeds normally (regression guard against a bad gate blocking real use).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.obsidian_sync import ObsidianSyncManager  # noqa: E402


def _make_item(text: str = "секретная быстрая заметка", item_id: str = "aaa00001") -> dict:
    return {
        "id": item_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "text": text,
        "translated_text": "",
        "translation_mode": "off",
        "source_lang": "ru",
        "target_lang": "",
        "tags": [],
        "diarization": None,
        "confidence": None,
    }


class ObsidianSyncPrivacyGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name) / "data"
        self._data_dir.mkdir()
        self._vault_dir = Path(self._tmp.name) / "vault"
        self._vault_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _configured_mgr(self, privacy_mode: bool) -> ObsidianSyncManager:
        mgr = ObsidianSyncManager(
            data_dir=self._data_dir,
            settings_get=lambda key, default: privacy_mode if key == "privacy_mode_enabled" else default,
        )
        mgr.configure(str(self._vault_dir))
        return mgr

    def _md_files(self) -> list[Path]:
        return list(self._vault_dir.rglob("*.md"))

    def test_privacy_mode_on_blocks_sync_and_returns_ok_false(self) -> None:
        mgr = self._configured_mgr(privacy_mode=True)
        result = mgr.handle_sync({"items": [_make_item()], "force": True})

        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("error"), "privacy_mode_active")
        self.assertIn("user_msg", result)

    def test_privacy_mode_on_writes_no_file_to_vault(self) -> None:
        mgr = self._configured_mgr(privacy_mode=True)
        mgr.handle_sync({"items": [_make_item()], "force": True})

        self.assertEqual(self._md_files(), [], "privacy mode ON must not write any .md file to the vault")

    def test_privacy_mode_off_still_syncs_normally(self) -> None:
        """Regression guard: the new gate must not block the normal (privacy-off) path."""
        mgr = self._configured_mgr(privacy_mode=False)
        result = mgr.handle_sync({"items": [_make_item()], "force": True})

        self.assertNotEqual(result.get("error"), "privacy_mode_active")
        self.assertEqual(result.get("synced_count"), 1)
        self.assertEqual(len(self._md_files()), 1)

    def test_no_settings_get_provided_defaults_to_privacy_off(self) -> None:
        """Same fallback contract as AppleIntegrationService: missing settings_get
        must not silently block sync (default=False, matches existing callers that
        don't wire a settings provider, e.g. ad-hoc scripts/tests)."""
        mgr = ObsidianSyncManager(data_dir=self._data_dir)
        mgr.configure(str(self._vault_dir))
        result = mgr.handle_sync({"items": [_make_item()], "force": True})

        self.assertNotEqual(result.get("error"), "privacy_mode_active")
        self.assertEqual(result.get("synced_count"), 1)


if __name__ == "__main__":
    unittest.main()
