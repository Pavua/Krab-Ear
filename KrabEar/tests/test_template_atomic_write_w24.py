"""Wave-24 LOW hardening — template_manager.py atomic write tests.

Covers:
- Normal add/remove round-trip still works after the atomic write refactor.
- When os.replace raises (simulated mid-write crash), the original file is
  left intact (no data loss, no partial write).
- PasteFormatter._save uses the same atomic pattern (smoke test).
"""

import json
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

from backend.template_manager import TemplateManager  # noqa: E402
from core.paste_formatter import PasteFormatter  # noqa: E402


class TestTemplateManagerAtomicWrite(unittest.TestCase):
    """TemplateManager._save_user uses atomic write (tmp + os.replace)."""

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.mgr = TemplateManager(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # -----------------------------------------------------------------------
    # Happy-path
    # -----------------------------------------------------------------------

    def test_add_template_persisted(self):
        """add_template writes the template to disk correctly."""
        self.mgr.add_template("my_tmpl", "Hello {name}")
        data = json.loads((self.tmp_dir / "templates.json").read_text())
        names = [t["name"] for t in data]
        self.assertIn("my_tmpl", names)

    def test_remove_template_removes_from_disk(self):
        """remove_template removes the template from the persisted file."""
        self.mgr.add_template("del_me", "Delete this")
        self.mgr.remove_template("del_me")
        data = json.loads((self.tmp_dir / "templates.json").read_text())
        names = [t["name"] for t in data]
        self.assertNotIn("del_me", names)

    # -----------------------------------------------------------------------
    # Crash-safety
    # -----------------------------------------------------------------------

    def test_original_file_intact_when_replace_raises(self):
        """If os.replace raises mid-write, the original templates.json is untouched."""
        # Write a good initial template
        self.mgr.add_template("safe_template", "Keep me")
        original_content = (self.tmp_dir / "templates.json").read_text(encoding="utf-8")
        original_data = json.loads(original_content)

        # Simulate a crash/disk-full during os.replace
        with patch("os.replace", side_effect=OSError("disk full")):
            # The exception should propagate (or be swallowed by _save_user's
            # try/except in the calling method — either way the original file
            # must not be truncated or overwritten with partial data).
            try:
                self.mgr.add_template("new_template", "I might not make it")
            except Exception:
                pass  # OSError propagation is acceptable

        # The original file must still be readable and contain "safe_template"
        disk_content = (self.tmp_dir / "templates.json").read_text(encoding="utf-8")
        disk_data = json.loads(disk_content)
        # The original template must still be there
        self.assertEqual(
            original_data,
            disk_data,
            "templates.json was modified after a simulated os.replace failure",
        )

    def test_no_stale_tmp_file_on_success(self):
        """After a successful save no .tmp file should remain."""
        self.mgr.add_template("clean_up", "no tmp files please")
        tmp_file = self.tmp_dir / "templates.json.tmp"
        self.assertFalse(
            tmp_file.exists(),
            ".tmp file was not cleaned up after successful atomic write",
        )

    def test_no_stale_tmp_file_on_failure(self):
        """After a failed os.replace the .tmp file must be deleted."""
        self.mgr.add_template("seed", "seed")
        with patch("os.replace", side_effect=OSError("disk full")):
            try:
                self.mgr.add_template("crash", "crash")
            except Exception:
                pass

        tmp_file = self.tmp_dir / "templates.json.tmp"
        self.assertFalse(
            tmp_file.exists(),
            ".tmp stale file was left on disk after failed atomic write",
        )


class TestPasteFormatterAtomicWrite(unittest.TestCase):
    """PasteFormatter._save uses atomic write."""

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.fmt = PasteFormatter(data_dir=self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_add_custom_formatter_persisted(self):
        """add_custom_formatter writes to disk."""
        self.fmt.add_custom_formatter("myapp", {"capitalize": True})
        data = json.loads((self.tmp_dir / "paste_formatters.json").read_text())
        self.assertIn("myapp", data)

    def test_original_file_intact_when_replace_raises(self):
        """Original paste_formatters.json intact when os.replace fails."""
        self.fmt.add_custom_formatter("safe_app", {"capitalize": True})
        original = (self.tmp_dir / "paste_formatters.json").read_text(encoding="utf-8")

        with patch("os.replace", side_effect=OSError("disk full")):
            try:
                self.fmt.add_custom_formatter("crash_app", {"capitalize": False})
            except Exception:
                pass

        disk = (self.tmp_dir / "paste_formatters.json").read_text(encoding="utf-8")
        self.assertEqual(original, disk)

    def test_no_stale_tmp_on_success(self):
        """No .tmp file after successful save."""
        self.fmt.add_custom_formatter("cleanapp", {})
        self.assertFalse(
            (self.tmp_dir / "paste_formatters.json.tmp").exists()
        )


if __name__ == "__main__":
    unittest.main()
