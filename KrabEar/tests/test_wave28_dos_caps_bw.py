"""Tests for wave-28 MED DoS caps:
  B1 — bookmarks.ndjson: MAX_BOOKMARKS cap + tombstone compaction.
  B2 — webhook_manager: MAX_WEBHOOKS registration cap (SSRF guard preserved).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Path bootstrap (matches project convention)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.bookmarks import BookmarkManager
from backend.webhook_manager import WebhookManager


# ===========================================================================
# B1 — BookmarkManager caps
# ===========================================================================


class TestBookmarkManagerDosCap(unittest.TestCase):
    """MAX_BOOKMARKS enforced; compaction triggered on tombstone ratio."""

    def _make_mgr(self) -> tuple[BookmarkManager, Path]:
        td = tempfile.mkdtemp()
        mgr = BookmarkManager(Path(td))
        return mgr, Path(td)

    # --- cap enforcement ---------------------------------------------------

    def test_add_up_to_limit_succeeds(self):
        """All adds up to MAX_BOOKMARKS − 1 succeed."""
        mgr, _ = self._make_mgr()
        # Patch MAX_BOOKMARKS to a small number for speed
        with patch("backend.bookmarks.MAX_BOOKMARKS", 5):
            for i in range(5):
                result = mgr.add("sess1", float(i))
                self.assertIn("id", result, f"add #{i} should succeed")

    def test_add_at_limit_rejected(self):
        """The (MAX_BOOKMARKS + 1)-th add must be rejected."""
        mgr, _ = self._make_mgr()
        cap = 5
        with patch("backend.bookmarks.MAX_BOOKMARKS", cap):
            for i in range(cap):
                mgr.add("sess1", float(i))

            result = mgr.add("sess1", float(cap))

        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "limit_exceeded")

    def test_add_10001_bookmarks_error_after_10000(self):
        """Canonical test: 10 001 adds → error after 10 000."""
        mgr, _ = self._make_mgr()
        cap = 10_000

        # Use a smaller real cap for the inner logic; patch to cap.
        with patch("backend.bookmarks.MAX_BOOKMARKS", cap):
            # First `cap` additions must succeed
            for i in range(cap):
                r = mgr.add("sess", float(i))
                self.assertIn(
                    "id", r,
                    f"add #{i} should succeed but got: {r}",
                )

            # (cap + 1)-th must be rejected
            last = mgr.add("sess", float(cap))

        self.assertEqual(last.get("ok"), False, f"Expected limit_exceeded, got: {last}")
        self.assertEqual(last.get("reason"), "limit_exceeded")

    def test_cap_observes_write_from_second_manager(self):
        """Последовательная запись второго менеджера не обходит общий лимит файла."""
        mgr, data_dir = self._make_mgr()
        second_mgr = BookmarkManager(data_dir)

        with patch("backend.bookmarks.MAX_BOOKMARKS", 2):
            self.assertIn("id", mgr.add("first", 1.0))
            self.assertIn("id", second_mgr.add("second", 2.0))
            result = mgr.add("over-limit", 3.0)

        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "limit_exceeded")

    def test_add_survives_signature_refresh_error_after_append(self):
        """Сбой служебного stat после append не отменяет сохранённую закладку."""
        mgr, _ = self._make_mgr()
        signature = mgr._file_signature_unlocked()

        with patch.object(
            mgr,
            "_file_signature_unlocked",
            side_effect=[signature, signature, OSError("stat failed")],
        ):
            result = mgr.add("sess", 1.0)

        self.assertIn("id", result)
        self.assertEqual(len(mgr.list_all()), 1)

    def test_cap_survives_signature_refresh_error_during_reload(self):
        """Сбой stat после reload не подменяет загруженный cap нулевым кэшем."""
        mgr, data_dir = self._make_mgr()
        with patch("backend.bookmarks.MAX_BOOKMARKS", 1):
            self.assertIn("id", mgr.add("first", 1.0))
            reloaded_mgr = BookmarkManager(data_dir)
            signature = reloaded_mgr._file_signature_unlocked()

            with patch.object(
                reloaded_mgr,
                "_file_signature_unlocked",
                side_effect=[signature, OSError("stat failed"), signature],
            ):
                result = reloaded_mgr.add("over-limit", 2.0)

        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "limit_exceeded")
        self.assertEqual(len(reloaded_mgr.list_all()), 1)

    def test_after_delete_space_freed(self):
        """Deleting a bookmark frees a slot so the next add succeeds."""
        mgr, _ = self._make_mgr()
        cap = 3
        with patch("backend.bookmarks.MAX_BOOKMARKS", cap):
            ids = []
            for i in range(cap):
                r = mgr.add("sess", float(i))
                ids.append(r["id"])

            # At limit — next add fails
            over = mgr.add("sess", 99.0)
            self.assertEqual(over.get("reason"), "limit_exceeded")

            # Delete one, then next add succeeds
            mgr.delete(ids[0])
            ok = mgr.add("sess", 99.0)
            self.assertIn("id", ok)

    def test_handle_add_bookmark_limit_exceeded_response(self):
        """IPC handler returns {ok:False, reason:limit_exceeded} (not an exception)."""
        mgr, _ = self._make_mgr()
        cap = 2
        with patch("backend.bookmarks.MAX_BOOKMARKS", cap):
            for i in range(cap):
                mgr.handle_add_bookmark(
                    {"session_id": "s", "offset_sec": float(i)}
                )

            result = mgr.handle_add_bookmark(
                {"session_id": "s", "offset_sec": 99.0}
            )

        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "limit_exceeded")

    # --- compaction --------------------------------------------------------

    def test_compaction_triggered_when_tombstone_ratio_reached(self):
        """File is rewritten when tombstone fraction >= _COMPACT_TOMBSTONE_RATIO (50%)."""
        mgr, td = self._make_mgr()
        ndjson_path = td / "bookmarks.ndjson"

        # Add 4 bookmarks
        ids = []
        for i in range(4):
            r = mgr.add("sess", float(i))
            ids.append(r["id"])

        # Delete all 4: 4 tombstones / 8 total = 50% = threshold → compaction fires on
        # next _load_active call (list_all / list_for_item / add).
        for bid in ids:
            mgr.delete(bid)

        lines_before_load = ndjson_path.read_text().splitlines()
        # 4 original + 4 tombstones = 8 lines (compaction not triggered yet)
        self.assertEqual(len(lines_before_load), 8)

        # Trigger a load — compaction fires
        active = mgr.list_all()
        self.assertEqual(active, [])

        # After compaction, file is empty (0 active entries)
        content_after = ndjson_path.read_text().strip()
        self.assertEqual(content_after, "")

    def test_compaction_keeps_active_entries(self):
        """Compaction rewrites only active entries (tombstoned ones are dropped)."""
        mgr, td = self._make_mgr()
        ndjson_path = td / "bookmarks.ndjson"

        # Add 4 bookmarks; delete 2 → 2 tombstones / 6 lines = 33% (below 50%)
        # Then add 2 more → 8 lines, still 2 tombstones = 25%
        # Then delete those 2 originals too → 4 tombstones / 8 lines = 50% = threshold
        ids = []
        for i in range(4):
            r = mgr.add("sess", float(i))
            ids.append(r["id"])

        # Delete 2, leaving 2 active (2/4 = 50% but file has 6 lines: 4 originals + 2 tombstones)
        # 2 tombstones / 6 lines = 33% → no compact yet
        mgr.delete(ids[0])
        mgr.delete(ids[1])

        # Confirm no compact yet
        lines_mid = [ln for ln in ndjson_path.read_text().splitlines() if ln.strip()]
        self.assertEqual(len(lines_mid), 6)

        # Delete the remaining 2 original entries; file = 4 originals + 4 tombstones = 8,
        # tombstone ratio = 4/8 = 50% ≥ threshold → compact on next load
        mgr.delete(ids[2])
        mgr.delete(ids[3])

        active = mgr.list_all()  # triggers compaction
        self.assertEqual(len(active), 0)

        lines = [ln for ln in ndjson_path.read_text().splitlines() if ln.strip()]
        # Compacted file should be empty (0 active entries)
        self.assertEqual(len(lines), 0)

    def test_compaction_keeps_mixed_active_entries(self):
        """Compaction keeps only active entries when some are deleted and some remain."""
        mgr, td = self._make_mgr()
        ndjson_path = td / "bookmarks.ndjson"

        # Add 4, delete 2, add 2 more (so 4 active after all, 2 tombstones in file)
        ids1 = []
        for i in range(4):
            r = mgr.add("sess", float(i))
            ids1.append(r["id"])

        # Delete 2 → 2 tombstones / 6 lines = 33%, no compact
        mgr.delete(ids1[0])
        mgr.delete(ids1[1])

        # Add 2 more: now 6 original + 2 tombstones = 8 lines, still 2/8 = 25%
        ids2 = []
        for i in range(2):
            r = mgr.add("sess2", float(i + 10))
            ids2.append(r["id"])

        # Now delete 2 more of the originals → 4 tombstones / 10 lines = 40%
        mgr.delete(ids1[2])
        mgr.delete(ids1[3])

        # 4/10 = 40% < 50% → compact not triggered; force via patched threshold
        with patch("backend.bookmarks._COMPACT_TOMBSTONE_RATIO", 0.35):
            active = mgr.list_all()  # triggers compaction at 40% ≥ 35%

        # 2 entries from ids2 should survive
        self.assertEqual(len(active), 2)
        surviving_ids = {a["id"] for a in active}
        self.assertEqual(surviving_ids, set(ids2))

        # File should have exactly 2 lines after compact
        lines = [ln for ln in ndjson_path.read_text().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)

    def test_delete_all_clears_file(self):
        """delete_all() truncates bookmarks.ndjson to empty (purge gate)."""
        mgr, td = self._make_mgr()
        ndjson_path = td / "bookmarks.ndjson"

        for i in range(5):
            mgr.add("sess", float(i))

        count = mgr.delete_all()
        self.assertEqual(count, 5)
        self.assertEqual(ndjson_path.read_text().strip(), "")

    def test_delete_all_idempotent(self):
        """Second call to delete_all() on empty file returns 0 and doesn't raise."""
        mgr, _ = self._make_mgr()
        mgr.delete_all()
        count = mgr.delete_all()
        self.assertEqual(count, 0)


# ===========================================================================
# B2 — WebhookManager caps
# ===========================================================================


class _FakeHTTPResponse:
    """Minimal stub for urllib.request.urlopen return value."""
    status = 200

    def read(self, n=-1):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _make_webhook_mgr() -> tuple[WebhookManager, str]:
    td = tempfile.mkdtemp()
    mgr = WebhookManager(td)
    return mgr, td


def _register(mgr: WebhookManager, i: int) -> str:
    """Register a webhook using allow_local=True to bypass SSRF check in tests."""
    return mgr.register_webhook(
        url=f"http://example-{i}.test/hook",
        events=[],
        secret="",
        allow_local=True,
    )


class TestWebhookManagerDosCap(unittest.TestCase):
    """MAX_WEBHOOKS enforced; SSRF guard unchanged."""

    def test_register_up_to_limit_succeeds(self):
        """Registering MAX_WEBHOOKS webhooks all succeed."""
        mgr, _ = _make_webhook_mgr()
        cap = 5
        with patch("backend.webhook_manager.MAX_WEBHOOKS", cap):
            for i in range(cap):
                wid = _register(mgr, i)
                self.assertTrue(wid, f"register #{i} should return a webhook_id")

    def test_register_at_limit_raises(self):
        """The (MAX_WEBHOOKS + 1)-th registration raises ValueError."""
        mgr, _ = _make_webhook_mgr()
        cap = 5
        with patch("backend.webhook_manager.MAX_WEBHOOKS", cap):
            for i in range(cap):
                _register(mgr, i)

            with self.assertRaises(ValueError) as ctx:
                _register(mgr, cap)

        self.assertIn("webhook_limit_reached", str(ctx.exception))

    def test_register_101_webhooks_error_after_100(self):
        """Canonical test: 101 registrations → error after 100."""
        mgr, _ = _make_webhook_mgr()
        cap = 100

        with patch("backend.webhook_manager.MAX_WEBHOOKS", cap):
            # First `cap` must succeed
            for i in range(cap):
                wid = _register(mgr, i)
                self.assertTrue(wid, f"register #{i} should succeed")

            # 101st must fail
            with self.assertRaises(ValueError) as ctx:
                _register(mgr, cap)

        self.assertIn("webhook_limit_reached", str(ctx.exception))

    def test_after_unregister_space_freed(self):
        """Unregistering a webhook frees a slot."""
        mgr, _ = _make_webhook_mgr()
        cap = 3
        with patch("backend.webhook_manager.MAX_WEBHOOKS", cap):
            wids = [_register(mgr, i) for i in range(cap)]

            # At limit
            with self.assertRaises(ValueError):
                _register(mgr, cap)

            # Free one slot
            mgr.unregister_webhook(wids[0])

            # Now succeeds
            wid = _register(mgr, 99)
            self.assertTrue(wid)

    def test_handle_register_webhook_returns_structured_error(self):
        """IPC handler returns {ok:False, reason:webhook_limit_reached} on overflow."""
        mgr, _ = _make_webhook_mgr()
        cap = 2
        with patch("backend.webhook_manager.MAX_WEBHOOKS", cap):
            # Fill up via IPC handler (uses allow_local=True is NOT available via IPC,
            # but we can pre-fill via direct method)
            for i in range(cap):
                _register(mgr, i)

            result = mgr.handle_register_webhook(
                {"url": "https://example.com/hook", "events": []}
            )

        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "webhook_limit_reached")

    def test_ssrf_guard_still_blocks_localhost(self):
        """SSRF guard is intact — localhost URL is still rejected regardless of cap."""
        mgr, _ = _make_webhook_mgr()
        cap = 100
        with patch("backend.webhook_manager.MAX_WEBHOOKS", cap):
            with self.assertRaises(ValueError) as ctx:
                mgr.register_webhook(
                    url="http://localhost/steal",
                    events=[],
                    allow_local=False,
                )
        self.assertIn("SSRF", str(ctx.exception))

    def test_ssrf_guard_blocks_private_ip(self):
        """Private RFC1918 URLs are blocked (SSRF guard not weakened by DoS cap)."""
        mgr, _ = _make_webhook_mgr()
        with self.assertRaises(ValueError) as ctx:
            mgr.register_webhook(
                url="http://192.168.1.1/admin",
                events=[],
                allow_local=False,
            )
        self.assertIn("SSRF", str(ctx.exception))

    def test_cap_check_is_atomic_with_save(self):
        """Registration count in persisted file matches in-memory count after cap hit."""
        mgr, td = _make_webhook_mgr()
        cap = 3
        with patch("backend.webhook_manager.MAX_WEBHOOKS", cap):
            for i in range(cap):
                _register(mgr, i)

            with self.assertRaises(ValueError):
                _register(mgr, cap)

        # Reload from disk
        mgr2 = WebhookManager(td)
        self.assertEqual(len(mgr2.list_webhooks()), cap)


if __name__ == "__main__":
    unittest.main()
