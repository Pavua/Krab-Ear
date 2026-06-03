"""Wave-24 LOW hardening — obsidian_sync.py containment + confidence coercion tests.

Covers:
- get_sync_status returns file_count=0 (and does NOT read the filesystem) when
  the persisted folder would escape the vault (path-traversal attempt).
- get_sync_status returns a correct file_count for a normal configured vault.
- _build_md_content coerces non-float confidence to 0.0 instead of raising
  ValueError and silently skipping the item.
"""

import sys
import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve()
_TESTS_DIR = _HERE.parent
_KRABEAR_DIR = _TESTS_DIR.parent
_PROJECT_ROOT = _KRABEAR_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
if str(_KRABEAR_DIR) not in sys.path:
    sys.path.insert(0, str(_KRABEAR_DIR))

from backend.obsidian_sync import ObsidianSyncManager  # noqa: E402


class TestGetSyncStatusContainment(unittest.TestCase):
    """get_sync_status must not read outside the vault when folder is unsafe."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.vault = Path(self.tmp) / "vault"
        self.vault.mkdir()
        self.data_dir = Path(self.tmp) / "data"
        self.data_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_configured_mgr(self, folder: str = "Notes") -> ObsidianSyncManager:
        """Create a manager already configured with vault + folder."""
        mgr = ObsidianSyncManager(data_dir=self.data_dir)
        # Directly inject state to bypass configure() validation
        mgr._vault_path = self.vault
        mgr._folder = folder
        mgr._last_sync_ts = None
        return mgr

    # -----------------------------------------------------------------------
    # Normal case: valid folder
    # -----------------------------------------------------------------------

    def test_file_count_valid_folder(self):
        """get_sync_status returns correct file_count for a safe folder."""
        folder = "Transcriptions"
        target = self.vault / folder
        target.mkdir(parents=True)
        (target / "t1.md").write_text("# test")
        (target / "t2.md").write_text("# test2")
        (target / "ignore.txt").write_text("not md")

        mgr = self._make_configured_mgr(folder)
        status = mgr.get_sync_status()

        self.assertTrue(status["configured"])
        self.assertEqual(status["file_count"], 2)

    # -----------------------------------------------------------------------
    # Traversal: unsafe folder
    # -----------------------------------------------------------------------

    def test_file_count_zero_for_traversal_folder(self):
        """get_sync_status returns file_count=0 when folder would escape vault."""
        mgr = self._make_configured_mgr("../../escape")
        status = mgr.get_sync_status()
        self.assertEqual(
            status["file_count"],
            0,
            "get_sync_status must return file_count=0 for traversal folder",
        )

    def test_file_count_zero_for_absolute_folder(self):
        """get_sync_status returns file_count=0 when folder is an absolute path."""
        mgr = self._make_configured_mgr("/etc")
        status = mgr.get_sync_status()
        self.assertEqual(status["file_count"], 0)

    def test_iterdir_not_called_for_unsafe_folder(self):
        """iterdir must not be called when folder is unsafe (no FS reads outside vault)."""
        mgr = self._make_configured_mgr("../../evil")
        with patch.object(Path, "iterdir", side_effect=AssertionError("iterdir called on unsafe path")):
            status = mgr.get_sync_status()
        # If we reach here without AssertionError, iterdir was not called
        self.assertEqual(status["file_count"], 0)

    def test_configured_true_even_with_unsafe_folder(self):
        """Configured flag is still True even if folder is unsafe (vault is set)."""
        mgr = self._make_configured_mgr("../../evil")
        status = mgr.get_sync_status()
        self.assertTrue(status["configured"])


class TestBuildMdContentConfidenceCoercion(unittest.TestCase):
    """_build_md_content must coerce non-numeric confidence to 0.0."""

    def setUp(self):
        self.mgr = ObsidianSyncManager()

    def _item(self, confidence):
        return {
            "id": "abc123",
            "ts": "2026-06-03T10:00:00",
            "text": "Hello world",
            "translated_text": "",
            "translation_mode": "off",
            "source_lang": "ru",
            "target_lang": "",
            "tags": [],
            "diarization": None,
            "confidence": confidence,
        }

    def test_float_confidence_renders_correctly(self):
        """Normal float confidence should render as-is."""
        content = self.mgr._build_md_content(self._item(0.95))
        self.assertIn("confidence: 0.950", content)

    def test_string_confidence_coerced_to_zero(self):
        """Non-numeric string confidence must be coerced to 0.0, item must not crash."""
        content = self.mgr._build_md_content(self._item("high"))
        self.assertIn("confidence: 0.000", content)
        # The rest of the item must still be rendered
        self.assertIn("Hello world", content)

    def test_none_confidence_not_rendered(self):
        """None confidence must not add a confidence line to frontmatter."""
        content = self.mgr._build_md_content(self._item(None))
        self.assertNotIn("confidence:", content)

    def test_dict_confidence_coerced_to_zero(self):
        """A dict value for confidence must be coerced to 0.0."""
        content = self.mgr._build_md_content(self._item({"value": 0.9}))
        self.assertIn("confidence: 0.000", content)

    def test_int_confidence_accepted(self):
        """Integer 1 should coerce to 1.000 without error."""
        content = self.mgr._build_md_content(self._item(1))
        self.assertIn("confidence: 1.000", content)

    def test_sync_does_not_skip_item_with_bad_confidence(self):
        """An item with non-numeric confidence must be synced (not silently skipped)."""
        tmp = tempfile.mkdtemp()
        try:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            mgr = ObsidianSyncManager(data_dir=data_dir)
            mgr.configure(str(vault), "Notes")

            item = {
                "id": "bad001",
                "ts": "2026-06-03T12:00:00",
                "text": "Should sync despite bad confidence",
                "translated_text": "",
                "translation_mode": "off",
                "source_lang": "ru",
                "target_lang": "",
                "tags": [],
                "diarization": None,
                "confidence": "bad_value",
            }
            result = mgr.sync([item], force=True)
            self.assertEqual(result.synced_count, 1)
            self.assertEqual(len(result.errors), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
