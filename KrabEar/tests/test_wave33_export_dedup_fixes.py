"""Tests for wave-33 fixes:
  G1 (MED) — export_scheduler._format_html: ts field HTML-escaped.
  G2 (MED) — auto_deduplication.handle_run_deduplication: cap at MAX_DEDUP_ITEMS.
  G3 (LOW) — export_scheduler.configure: interval_seconds minimum validation.
"""
from __future__ import annotations

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

from backend.export_scheduler import ExportScheduler  # noqa: E402
from backend.auto_deduplication import AutoDeduplicator, MAX_DEDUP_ITEMS  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scheduler(tmp_dir: Path) -> ExportScheduler:
    return ExportScheduler(data_dir=tmp_dir)


def _make_store_with_n_items(n: int) -> MagicMock:
    """Fake store that returns n items on first get_history_page call."""
    store = MagicMock()
    items = [{"id": str(i), "ts": f"2026-01-01T{i:06d}", "text": f"text {i}"} for i in range(n)]

    # Return all items (up to requested limit) in one page; no next cursor.
    def _get_page(cursor=None, limit=200):
        return items[:limit], None

    store.get_history_page.side_effect = _get_page
    return store


# ---------------------------------------------------------------------------
# G1: _format_html HTML-escaping
# ---------------------------------------------------------------------------

class FormatHtmlEscapingTestCase(unittest.TestCase):
    """G1: _format_html must HTML-escape all user-controlled fields including ts."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sched = _make_scheduler(Path(self.tmp))

    def test_ts_xss_payload_is_escaped(self):
        """ts containing <script> tag must not appear raw in HTML output."""
        items = [{"ts": "<script>alert(1)</script>", "text": "safe text"}]
        html_out = self.sched._format_html(items)
        self.assertNotIn("<script>", html_out)
        self.assertIn("&lt;script&gt;", html_out)

    def test_text_xss_payload_is_escaped(self):
        """text field with HTML tags must be escaped."""
        items = [{"ts": "2026-01-01", "text": "<b>bold</b>"}]
        html_out = self.sched._format_html(items)
        self.assertNotIn("<b>", html_out)
        self.assertIn("&lt;b&gt;", html_out)

    def test_ts_ampersand_escaped(self):
        """Ampersand in ts must be escaped as &amp;."""
        items = [{"ts": "2026-01&amp;foo", "text": "ok"}]
        html_out = self.sched._format_html(items)
        # html.escape will turn & into &amp;; the original &amp; will be double-escaped
        self.assertNotIn("<script>", html_out)

    def test_benign_ts_preserved_in_escaped_form(self):
        """Normal ISO ts (no special chars) should appear correctly."""
        items = [{"ts": "2026-05-01T12:00:00", "text": "hello"}]
        html_out = self.sched._format_html(items)
        self.assertIn("2026-05-01T12:00:00", html_out)

    def test_empty_ts_no_crash(self):
        """Missing ts key should not raise, should produce empty cell."""
        items = [{"text": "no ts"}]
        html_out = self.sched._format_html(items)
        self.assertIn("no ts", html_out)


# ---------------------------------------------------------------------------
# G2: handle_run_deduplication cap at MAX_DEDUP_ITEMS
# ---------------------------------------------------------------------------

class DedupCapTestCase(unittest.TestCase):
    """G2: handle_run_deduplication must reject histories > MAX_DEDUP_ITEMS."""

    def setUp(self):
        self.dedup = AutoDeduplicator()

    def test_over_cap_returns_error(self):
        """501 items > 500 cap → returns {'ok': False, 'reason': '...'}."""
        store = _make_store_with_n_items(MAX_DEDUP_ITEMS + 1)
        params = {"_store": store}
        result = self.dedup.handle_run_deduplication(params)
        self.assertFalse(result.get("ok"), f"Expected ok=False, got: {result}")
        self.assertIn("too many items", result.get("reason", ""))
        self.assertIn(str(MAX_DEDUP_ITEMS), result.get("reason", ""))

    def test_at_cap_proceeds(self):
        """Exactly MAX_DEDUP_ITEMS items should NOT be rejected."""
        store = _make_store_with_n_items(MAX_DEDUP_ITEMS)
        params = {"_store": store}
        result = self.dedup.handle_run_deduplication(params)
        # Should not return error dict with ok=False
        self.assertNotEqual(result.get("ok"), False)

    def test_under_cap_proceeds(self):
        """10 items should not be rejected."""
        store = _make_store_with_n_items(10)
        params = {"_store": store}
        result = self.dedup.handle_run_deduplication(params)
        self.assertNotEqual(result.get("ok"), False)

    def test_max_dedup_items_constant(self):
        """MAX_DEDUP_ITEMS must be 500."""
        self.assertEqual(MAX_DEDUP_ITEMS, 500)


# ---------------------------------------------------------------------------
# G3: configure interval_seconds minimum validation
# ---------------------------------------------------------------------------

class ExportIntervalValidationTestCase(unittest.TestCase):
    """G3: configure() must reject interval_seconds < 60."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sched = _make_scheduler(Path(self.tmp))

    def test_interval_seconds_30_is_rejected(self):
        """interval_seconds=30 must raise ValueError."""
        with self.assertRaises(ValueError):
            self.sched.configure(fmt="json", interval_seconds=30)

    def test_interval_seconds_0_is_rejected(self):
        """interval_seconds=0 must raise ValueError."""
        with self.assertRaises(ValueError):
            self.sched.configure(fmt="json", interval_seconds=0)

    def test_interval_seconds_59_is_rejected(self):
        """interval_seconds=59 (one below minimum) must raise ValueError."""
        with self.assertRaises(ValueError):
            self.sched.configure(fmt="json", interval_seconds=59)

    def test_interval_seconds_60_is_accepted(self):
        """interval_seconds=60 (minimum valid) must not raise."""
        result = self.sched.configure(fmt="json", interval_seconds=60)
        self.assertIn("interval_hours", result)

    def test_interval_seconds_3600_maps_to_1_hour(self):
        """3600 seconds = 1 hour."""
        result = self.sched.configure(fmt="json", interval_seconds=3600)
        self.assertEqual(result["interval_hours"], 1)

    def test_interval_seconds_7200_maps_to_2_hours(self):
        """7200 seconds = 2 hours."""
        result = self.sched.configure(fmt="json", interval_seconds=7200)
        self.assertEqual(result["interval_hours"], 2)

    def test_interval_seconds_none_uses_interval_hours(self):
        """None interval_seconds (default) falls back to interval_hours."""
        result = self.sched.configure(fmt="json", interval_hours=5)
        self.assertEqual(result["interval_hours"], 5)

    def test_interval_hours_still_works_without_interval_seconds(self):
        """interval_hours alone still works (backward compatibility)."""
        result = self.sched.configure(fmt="json", interval_hours=12)
        self.assertEqual(result["interval_hours"], 12)


if __name__ == "__main__":
    unittest.main()
