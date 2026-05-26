"""Wave 984 — tests for F3 (atomic export write) and F4 (privacy mode guard).

F3: _do_export() must write via tmp + fsync + os.replace (no partial file on crash).
F4: check_and_export() must skip export when privacy_mode_enabled=True.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.export_scheduler import ExportScheduler  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(items=None):
    store = MagicMock()
    if items is None:
        items = [
            {"ts": "2026-01-01T12:00:00+00:00", "text": "Тест", "confidence": 0.9},
        ]
    store.get_history_page_filtered.return_value = (items, None)
    return store


# ---------------------------------------------------------------------------
# F3 — Atomic export write (tmp + fsync + os.replace)
# ---------------------------------------------------------------------------

class TestExportAtomicNoPartialFileOnCrash(unittest.TestCase):
    """F3: _do_export() must use atomic write pattern (tmp + fsync + os.replace).

    Verifies:
    1. No .tmp file left on disk after a successful export.
    2. If the write is interrupted mid-flush (simulated), the target file
       is either absent or fully intact — never a zero-length partial.
    3. os.replace is called (ensuring atomicity of final rename).
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.scheduler = ExportScheduler(data_dir=self.data_dir)
        self.store = _make_store()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_no_tmp_file_left_after_successful_export(self):
        """After a normal export, no .tmp file should remain on disk."""
        self.scheduler.configure(fmt="json", enabled=True)
        result = self.scheduler.check_and_export(self.store)
        self.assertIsNotNone(result)

        # Look for any .tmp file in the output dir
        output_dir = Path(result["path"]).parent
        tmp_files = list(output_dir.glob("*.tmp"))
        self.assertEqual(
            tmp_files,
            [],
            f"Temporary file(s) found after export: {tmp_files}",
        )

    def test_export_file_is_non_empty_after_write(self):
        """The export file must contain valid content — not be truncated."""
        self.scheduler.configure(fmt="json", enabled=True)
        result = self.scheduler.check_and_export(self.store)
        p = Path(result["path"])
        self.assertTrue(p.exists(), "Export file must exist")
        self.assertGreater(p.stat().st_size, 0, "Export file must not be empty")
        # Content must be valid JSON
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertIn("items", data)

    def test_tmp_file_cleaned_up_on_write_error(self):
        """If os.replace fails mid-write, the .tmp file must be cleaned up.

        We call _do_export directly so we don't have to deal with check_and_export
        state management — this directly tests the atomic write cleanup path.
        """
        output_dir = self.data_dir / "auto_exports"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Patch os.replace to raise so _do_export hits the except branch
        with patch("backend.export_scheduler.os.replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.scheduler._do_export(self.store, "json", output_dir)

        # After the error, no .tmp file should remain
        tmp_files = list(output_dir.glob("*.tmp"))
        self.assertEqual(
            tmp_files,
            [],
            f".tmp file not cleaned up after write error: {tmp_files}",
        )

    def test_os_replace_called_during_export(self):
        """os.replace must be called exactly once per export (atomic rename)."""
        self.scheduler.configure(fmt="json", enabled=True)

        replace_calls = []
        original_replace = os.replace

        def tracking_replace(src, dst):
            replace_calls.append((src, dst))
            return original_replace(src, dst)

        with patch("backend.export_scheduler.os.replace", side_effect=tracking_replace):
            result = self.scheduler.check_and_export(self.store)

        self.assertIsNotNone(result)
        # At least one call to os.replace must have happened for the export file
        export_replaces = [
            (s, d) for s, d in replace_calls
            if str(s).endswith(".tmp") and not str(d).endswith(".tmp")
        ]
        self.assertGreaterEqual(
            len(export_replaces),
            1,
            "os.replace not called for export file — atomic rename not happening",
        )


# ---------------------------------------------------------------------------
# F4 — Privacy mode guard
# ---------------------------------------------------------------------------

class TestExportSkipsInPrivacyMode(unittest.TestCase):
    """F4: check_and_export() must skip export when privacy_mode_enabled=True.

    Verifies:
    1. When settings_provider returns privacy_mode_enabled=True, no export happens.
    2. When settings_provider returns privacy_mode_enabled=False, export proceeds.
    3. Return value contains {"exported": False, "reason": "privacy_mode_active"}.
    4. No history data is loaded from store while privacy mode is active.
    5. If settings_provider raises, export proceeds (fail-open for data safety).
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.store = _make_store()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_scheduler(self, privacy_on: bool) -> ExportScheduler:
        settings = {"privacy_mode_enabled": privacy_on}
        sched = ExportScheduler(
            data_dir=self.data_dir,
            settings_provider=lambda: settings,
        )
        return sched

    def test_export_skipped_when_privacy_mode_active(self):
        """check_and_export returns privacy_mode_active result, not None."""
        sched = self._make_scheduler(privacy_on=True)
        sched.configure(fmt="json", enabled=True)

        result = sched.check_and_export(self.store)

        # Must return a dict indicating skip — not None and not a normal export entry
        self.assertIsNotNone(result, "Should return a dict, not None")
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("exported", True), "exported must be False")
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_no_export_file_created_in_privacy_mode(self):
        """No export file should be written to disk when privacy mode is active."""
        sched = self._make_scheduler(privacy_on=True)
        sched.configure(fmt="json", enabled=True)
        sched.check_and_export(self.store)

        output_dir = self.data_dir / "auto_exports"
        if output_dir.exists():
            files = list(output_dir.iterdir())
            self.assertEqual(files, [], f"No files should be written in privacy mode: {files}")

    def test_store_not_queried_in_privacy_mode(self):
        """get_history_page_filtered must NOT be called when privacy mode is active."""
        sched = self._make_scheduler(privacy_on=True)
        sched.configure(fmt="json", enabled=True)
        sched.check_and_export(self.store)

        self.store.get_history_page_filtered.assert_not_called()

    def test_export_proceeds_when_privacy_mode_disabled(self):
        """Normal export should happen when privacy_mode_enabled=False."""
        sched = self._make_scheduler(privacy_on=False)
        sched.configure(fmt="json", enabled=True)

        result = sched.check_and_export(self.store)

        # Should be a normal export dict with 'path'
        self.assertIsNotNone(result)
        self.assertIn("path", result, "Normal export should contain 'path'")
        self.assertTrue(Path(result["path"]).exists())

    def test_export_proceeds_without_settings_provider(self):
        """When no settings_provider is given, export proceeds normally (legacy compat)."""
        sched = ExportScheduler(data_dir=self.data_dir)
        sched.configure(fmt="json", enabled=True)

        result = sched.check_and_export(self.store)

        self.assertIsNotNone(result)
        self.assertIn("path", result)

    def test_export_proceeds_when_settings_provider_raises(self):
        """If settings_provider raises, export should proceed (fail-open)."""
        def bad_provider():
            raise RuntimeError("settings unavailable")

        sched = ExportScheduler(
            data_dir=self.data_dir,
            settings_provider=bad_provider,
        )
        sched.configure(fmt="json", enabled=True)

        # Should not raise; should proceed with export
        result = sched.check_and_export(self.store)
        self.assertIsNotNone(result)
        self.assertIn("path", result)

    def test_privacy_mode_checked_dynamically(self):
        """Privacy mode check uses the current value from settings_provider each call."""
        state = {"privacy_mode_enabled": True}

        sched = ExportScheduler(
            data_dir=self.data_dir,
            settings_provider=lambda: dict(state),
        )
        sched.configure(fmt="json", enabled=True)

        # First call: privacy on → skip
        result1 = sched.check_and_export(self.store)
        self.assertEqual(result1.get("reason"), "privacy_mode_active")

        # Toggle privacy off
        state["privacy_mode_enabled"] = False

        # Second call: privacy off → normal export
        result2 = sched.check_and_export(self.store)
        self.assertIsNotNone(result2)
        self.assertIn("path", result2)


if __name__ == "__main__":
    unittest.main()
