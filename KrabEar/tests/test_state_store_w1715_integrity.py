"""Wave 1715 — StateStore data-integrity regression tests.

Three bugs fixed in state_store.py by W1715:

  BUG 1 — annotations & calendar_links journals never truncated on compaction:
    _compact_unlocked() truncated 6 delta journals (tombstones, status, tags,
    favorites, text_updates, action_items) but silently omitted
    annotations_path and calendar_links_path.  Both are writable delta
    journals; after a delete + compact cycle, deleted items' annotation /
    calendar-link entries stayed in the journals forever (unbounded growth).
    Additionally, calendar_links_path was omitted from get_storage_breakdown()
    so the dashboard under-reported disk usage.

    Fix: selective rewrite of both journals during compaction — keep only
    entries whose "id" is still in the active-ids set (same last-write-wins
    semantics as the other journals).  Also add both paths to
    get_storage_breakdown() accounting.

  BUG 2 — delta writes without fsync (action_items and text_updates):
    update_history_item_action_items() and update_history_item_text() used
    bare ``open("a") + fh.write()`` instead of the _append_ndjson() helper,
    which flushes and fsyncs on every call.  A crash between the buffer fill
    and the physical write loses the last entry silently.

    Fix: route both methods through _append_ndjson() so every delta write is
    durably committed to disk.

  BUG 3 — tmp_history not cleaned on exception:
    _compact_unlocked() opened history_path.with_suffix(".ndjson.tmp") for
    writing with no try/finally to unlink it on error.  If json.dumps() raised
    or fsync failed (disk full), a stale .ndjson.tmp was left behind.

    Fix: wrap the compaction write in try/finally; unlink tmp_history if the
    atomic replace() didn't complete (_history_replaced flag).

Tests:
  BUG 1:
    1. test_compact_removes_dead_annotation_entries
       (MUST fail before fix — proves the regression)
    2. test_compact_removes_dead_calendar_link_entries
       (MUST fail before fix — proves the regression)
    3. test_compact_keeps_surviving_annotations
       After compaction, annotations for still-active items survive.
    4. test_storage_breakdown_includes_calendar_links
       get_storage_breakdown() ndjson_mb must account for calendar_links.

  BUG 2:
    5. test_update_action_items_fsynced
       update_history_item_action_items() calls os.fsync via _append_ndjson.
    6. test_update_text_fsynced
       update_history_item_text() calls os.fsync via _append_ndjson.
    7. test_update_action_items_data_survives_reload
       Data written by update_history_item_action_items() survives a fresh
       StateStore instance reload (end-to-end durability assertion).
    8. test_update_text_data_survives_reload
       Same for update_history_item_text().

  BUG 3:
    9. test_compact_tmp_cleaned_on_exception
       If json.dumps raises during compaction, no .ndjson.tmp is left behind.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_dir: str | Path, **kwargs) -> StateStore:
    return StateStore(Path(tmp_dir) / "data", **kwargs)


def _add(store: StateStore, text: str = "hello") -> str:
    item = store.add_history_item(text)
    return item.id


# ---------------------------------------------------------------------------
# BUG 1 — annotations & calendar_links purged on compact
# ---------------------------------------------------------------------------

class TestCompactPurgesDeadAnnotations(unittest.TestCase):
    """After delete + compact, the deleted item's annotation entry must be
    gone from annotations_path.  Before the W1715 fix this test fails because
    the journal is never rewritten during compaction."""

    def test_compact_removes_dead_annotation_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            live_id = _add(store, "live item")
            dead_id = _add(store, "dead item")

            # Add annotations to both items.
            store.set_annotation(live_id, "keep this note")
            store.set_annotation(dead_id, "should be removed after compact")

            # Verify both annotations are present before delete+compact.
            ann_before = list(StateStore._read_ndjson_unlocked(store.annotations_path))
            ann_ids_before = {r["id"] for r in ann_before}
            self.assertIn(dead_id, ann_ids_before, "dead item annotation must be present before compact")
            self.assertIn(live_id, ann_ids_before, "live item annotation must be present before compact")

            # Delete the dead item and compact.
            store.delete_history_item(dead_id)
            store.compact()

            # After compaction the dead item's annotation must be gone.
            ann_after = list(StateStore._read_ndjson_unlocked(store.annotations_path))
            ann_ids_after = {r["id"] for r in ann_after}
            self.assertNotIn(
                dead_id,
                ann_ids_after,
                "deleted item's annotation entry must be removed during compaction",
            )
            # Live item's annotation must survive.
            self.assertIn(
                live_id,
                ann_ids_after,
                "live item's annotation entry must survive compaction",
            )

    def test_compact_keeps_surviving_annotations(self):
        """Items that are NOT deleted must retain their annotations after compact."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            ids = [_add(store, f"item {i}") for i in range(4)]
            for i, item_id in enumerate(ids):
                store.set_annotation(item_id, f"note {i}")

            # Delete none; compact to exercise the rewrite path.
            store.compact()

            ann_after = list(StateStore._read_ndjson_unlocked(store.annotations_path))
            ann_ids_after = {r["id"] for r in ann_after}
            for item_id in ids:
                self.assertIn(
                    item_id,
                    ann_ids_after,
                    f"annotation for surviving item {item_id} must be kept",
                )
            self.assertEqual(len(ann_after), len(ids))


class TestCompactPurgesDeadCalendarLinks(unittest.TestCase):
    """After delete + compact, the deleted item's calendar link entry must be
    gone from calendar_links_path.  Before the W1715 fix this test fails."""

    def test_compact_removes_dead_calendar_link_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            live_id = _add(store, "live item")
            dead_id = _add(store, "dead item")

            cal_event = {"title": "Test Meeting", "start": "2026-06-01T10:00:00Z"}
            store.update_history_item_calendar(live_id, cal_event)
            store.update_history_item_calendar(dead_id, {"title": "Dead Meeting", "start": "2026-06-01T09:00:00Z"})

            # Confirm both entries exist.
            links_before = list(StateStore._read_ndjson_unlocked(store.calendar_links_path))
            link_ids_before = {r["id"] for r in links_before}
            self.assertIn(dead_id, link_ids_before)
            self.assertIn(live_id, link_ids_before)

            store.delete_history_item(dead_id)
            store.compact()

            links_after = list(StateStore._read_ndjson_unlocked(store.calendar_links_path))
            link_ids_after = {r["id"] for r in links_after}
            self.assertNotIn(
                dead_id,
                link_ids_after,
                "deleted item's calendar link must be removed during compaction",
            )
            self.assertIn(
                live_id,
                link_ids_after,
                "live item's calendar link must survive compaction",
            )


class TestStorageBreakdownIncludesCalendarLinks(unittest.TestCase):
    """get_storage_breakdown() must include calendar_links_path in ndjson_mb
    so the dashboard does not under-report disk usage."""

    def test_storage_breakdown_includes_calendar_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            item_id = _add(store, "item with cal link")
            store.update_history_item_calendar(
                item_id, {"title": "Meeting", "start": "2026-06-01T10:00:00Z"}
            )

            # The calendar_links_path should be non-empty now.
            self.assertGreater(store.calendar_links_path.stat().st_size, 0)

            # Verify calendar_links_path is summed by measuring the path list
            # used inside get_storage_breakdown().  We do this by tracking
            # calls to _safe_size_mb via a wrapping patch.
            measured_paths: list[Path] = []
            _orig_safe_size_mb = StateStore._safe_size_mb

            def _tracking(p: Path) -> float:
                measured_paths.append(p)
                return _orig_safe_size_mb(p)

            with patch.object(StateStore, "_safe_size_mb", staticmethod(_tracking)):
                breakdown = store.get_storage_breakdown()

            measured_names = {p.name for p in measured_paths}
            self.assertIn(
                "history_calendar_links.ndjson",
                measured_names,
                "calendar_links_path must be included in get_storage_breakdown() measurement",
            )
            # ndjson_mb must be > 0 (we wrote data)
            self.assertGreater(breakdown["ndjson_mb"], 0)


# ---------------------------------------------------------------------------
# BUG 2 — delta writes fsynced via _append_ndjson
# ---------------------------------------------------------------------------

class TestUpdateActionItemsFsynced(unittest.TestCase):
    """update_history_item_action_items() must call os.fsync (via
    _append_ndjson).  Before the W1715 fix, it used bare open("a")+write()
    with no fsync."""

    def test_update_action_items_fsynced(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item_id = _add(store, "item")

            with patch("backend.state_store.os.fsync") as mock_fsync:
                result = store.update_history_item_action_items(
                    item_id,
                    ["task A"],
                    ["decision B"],
                    ["question C"],
                )

            self.assertTrue(result, "update_history_item_action_items should return True")
            self.assertGreaterEqual(
                mock_fsync.call_count, 1,
                "update_history_item_action_items must call os.fsync at least once",
            )

    def test_update_action_items_data_survives_reload(self):
        """Data written by update_history_item_action_items() is visible to a
        fresh StateStore instance (proves the write actually made it to disk)."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"

            store1 = StateStore(data_dir)
            item_id = _add(store1, "item for action-items reload test")
            store1.update_history_item_action_items(
                item_id,
                ["task X"],
                ["decision Y"],
                ["question Z"],
            )

            # Open a second independent instance to simulate a reload.
            store2 = StateStore(data_dir)
            overrides = store2._load_action_items_overrides_unlocked()

            self.assertIn(item_id, overrides, "action_items override not found after reload")
            self.assertEqual(overrides[item_id]["action_items"], ["task X"])
            self.assertEqual(overrides[item_id]["decisions"], ["decision Y"])
            self.assertEqual(overrides[item_id]["questions"], ["question Z"])


class TestUpdateTextFsynced(unittest.TestCase):
    """update_history_item_text() must call os.fsync (via _append_ndjson).
    Before the W1715 fix, it used bare open("a")+write() with no fsync."""

    def test_update_text_fsynced(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item_id = _add(store, "original text")

            with patch("backend.state_store.os.fsync") as mock_fsync:
                result = store.update_history_item_text(item_id, "updated text", confidence=0.9)

            self.assertTrue(result, "update_history_item_text should return True")
            self.assertGreaterEqual(
                mock_fsync.call_count, 1,
                "update_history_item_text must call os.fsync at least once",
            )

    def test_update_text_data_survives_reload(self):
        """Data written by update_history_item_text() is visible to a fresh
        StateStore instance (proves the write actually made it to disk)."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"

            store1 = StateStore(data_dir)
            item_id = _add(store1, "original text for reload test")
            store1.update_history_item_text(item_id, "rewritten text", confidence=0.85)

            store2 = StateStore(data_dir)
            overrides = store2._load_text_overrides_unlocked()

            self.assertIn(item_id, overrides, "text override not found after reload")
            self.assertEqual(overrides[item_id]["text"], "rewritten text")
            self.assertAlmostEqual(overrides[item_id]["confidence"], 0.85, places=4)


# ---------------------------------------------------------------------------
# BUG 3 — tmp file cleaned on exception during compaction
# ---------------------------------------------------------------------------

class TestCompactTmpCleanedOnException(unittest.TestCase):
    """If an exception is raised during the compaction write (simulated by
    making json.dumps raise), no stale .ndjson.tmp file must remain in the
    data directory.  Before the W1715 fix there was no try/finally around
    the tmp-file write."""

    def test_compact_tmp_cleaned_on_json_dumps_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            for i in range(3):
                _add(store, f"item {i}")

            # Patch json.dumps to raise an error during compaction.
            # We need it to raise only for the dict-style call inside
            # _compact_unlocked (item.to_dict()), not for the journal rewrites.
            # We count calls: the first call to dumps inside _compact_unlocked
            # is for the first history item — raise there.
            call_count = [0]
            original_dumps = json.dumps

            def _boom(obj, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise OSError("simulated disk error during compact write")
                return original_dumps(obj, **kwargs)

            with patch("backend.state_store.json.dumps", side_effect=_boom):
                try:
                    store.compact()
                except OSError:
                    pass  # expected — the exception should propagate up

            tmp_files = list(store.data_dir.glob("*.ndjson.tmp"))
            self.assertEqual(
                tmp_files,
                [],
                f"Stale .ndjson.tmp files found after failed compact: {tmp_files}",
            )

    def test_compact_successful_leaves_no_tmp_files(self):
        """Successful compaction must also leave no .ndjson.tmp files."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            ids = [_add(store, f"item {i}") for i in range(3)]
            store.set_annotation(ids[0], "a note")
            store.update_history_item_calendar(
                ids[1], {"title": "Meeting", "start": "2026-06-01T10:00:00Z"}
            )
            store.delete_history_item(ids[2])

            store.compact()

            tmp_files = list(store.data_dir.glob("*.ndjson.tmp"))
            self.assertEqual(
                tmp_files,
                [],
                f"Stale .ndjson.tmp files found after successful compact: {tmp_files}",
            )

    def test_compact_successful_also_cleans_journal_tmp_files(self):
        """Compaction must not leave any .tmp sibling files at all."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            for i in range(4):
                _add(store, f"item {i}")

            store.compact()

            # No .tmp files of any kind should remain.
            tmp_files = list(store.data_dir.glob("*.tmp"))
            self.assertEqual(
                tmp_files,
                [],
                f"Stale .tmp files found after compact: {tmp_files}",
            )


# ---------------------------------------------------------------------------
# Combined: annotations + calendar links correctly handled across multi-cycle
# ---------------------------------------------------------------------------

class TestMultiCycleAnnotationAndCalendarLinkIntegrity(unittest.TestCase):
    """Run two delete+compact cycles; after each cycle only surviving items'
    annotation/calendar entries must remain."""

    def test_two_delete_compact_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            ids = [_add(store, f"item {i}") for i in range(6)]

            # Annotate and calendar-link all items.
            for i, item_id in enumerate(ids):
                store.set_annotation(item_id, f"note {i}")
                store.update_history_item_calendar(
                    item_id,
                    {"title": f"Event {i}", "start": "2026-06-01T10:00:00Z"},
                )

            # Cycle 1: delete first two, compact.
            store.delete_history_item(ids[0])
            store.delete_history_item(ids[1])
            store.compact()

            # ids[0] and ids[1] must be gone from both journals.
            ann = {r["id"] for r in StateStore._read_ndjson_unlocked(store.annotations_path)}
            cal = {r["id"] for r in StateStore._read_ndjson_unlocked(store.calendar_links_path)}
            for dead_id in ids[:2]:
                self.assertNotIn(dead_id, ann, f"cycle 1: annotation for {dead_id} must be gone")
                self.assertNotIn(dead_id, cal, f"cycle 1: calendar link for {dead_id} must be gone")
            for live_id in ids[2:]:
                self.assertIn(live_id, ann, f"cycle 1: annotation for {live_id} must survive")
                self.assertIn(live_id, cal, f"cycle 1: calendar link for {live_id} must survive")

            # Cycle 2: delete two more, compact.
            store.delete_history_item(ids[2])
            store.delete_history_item(ids[3])
            store.compact()

            ann2 = {r["id"] for r in StateStore._read_ndjson_unlocked(store.annotations_path)}
            cal2 = {r["id"] for r in StateStore._read_ndjson_unlocked(store.calendar_links_path)}
            for dead_id in ids[:4]:
                self.assertNotIn(dead_id, ann2, f"cycle 2: annotation for {dead_id} must be gone")
                self.assertNotIn(dead_id, cal2, f"cycle 2: calendar link for {dead_id} must be gone")
            for live_id in ids[4:]:
                self.assertIn(live_id, ann2, f"cycle 2: annotation for {live_id} must survive")
                self.assertIn(live_id, cal2, f"cycle 2: calendar link for {live_id} must survive")


if __name__ == "__main__":
    unittest.main()
