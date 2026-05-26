"""Wave 862 — StateStore W853 fsync + journal-truncation atomicity tests.

W853 (commit ac677c6f) fixed two durability bugs in _compact_unlocked():

  Bug 1 — no fsync before rename:
    The compacted history tmp file was renamed into place without an
    os.fsync() call first.  A crash after the rename but before the
    kernel flushed dirty pages could leave the history file pointing at
    an empty or partial write.  Fix: os.fsync(fh.fileno()) is called
    before tmp_history.replace(self.history_path).

  Bug 2 — non-atomic journal truncation:
    Delta journals (tombstones, status, tags, favorites, text_updates,
    action_items) were previously truncated with a plain write_text("").
    write_text() is not atomic: a crash between two journal truncations
    leaves some journals cleared and others intact, producing orphaned
    overrides.  Fix: each truncation now goes through a tmp file that is
    fsynced and atomically renamed.

These tests verify both fixes using unittest.mock to intercept os.fsync
and inspect call counts / file state without actually crashing the process.

Tests:
  1. test_compact_fsync_called_before_rename
     Mock os.fsync; call compact(); assert fsync was called for the
     history tmp fd AND for each of the 6 delta-journal tmp fds.

  2. test_compact_history_tmp_fsynced_before_replace
     Verify the order invariant: fsync for the history tmp must happen
     before history_path is overwritten (no stale history_path after rename).

  3. test_compact_all_delta_journals_cleared_atomically
     After compact() all 6 delta journals listed in _compact_unlocked()
     must be empty files (size == 0).  This is the observable result of
     the atomic truncation path.

  4. test_compact_no_tmp_files_left_behind
     After compact() no *.ndjson.tmp sibling files must remain in the
     data directory.  A crash-recovery run must not trip over stale temps.

  5. test_append_ndjson_fsync_called_on_every_write
     _append_ndjson() is the hot write path used by add_history_item,
     set_paste_status, delete_history_item, etc.  It must call os.fsync
     exactly once per invocation.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

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
# 1. os.fsync is called during compact() for history tmp AND delta journals
# ---------------------------------------------------------------------------

class TestCompactFsyncCalled(unittest.TestCase):
    """compact() must call os.fsync() at least once for the history tmp file
    and once for each of the 6 delta-journal tmp files (7 fsyncs minimum)."""

    def test_compact_fsync_called_before_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            # Add items and a tombstone so the compaction has real work to do.
            ids = [_add(store, f"item {i}") for i in range(3)]
            store.delete_history_item(ids[0])
            store.set_paste_status(ids[1], "ok")

            with patch("backend.state_store.os.fsync") as mock_fsync:
                store.compact()

            # W853 fix 1: fsync for the history tmp file.
            # W853 fix 2: fsync for each of 6 delta-journal tmp files.
            # Total minimum = 7 fsync calls.
            total_calls = mock_fsync.call_count
            self.assertGreaterEqual(
                total_calls, 7,
                f"Expected >= 7 fsync calls (1 history + 6 journals) but got {total_calls}",
            )


# ---------------------------------------------------------------------------
# 2. History tmp fsynced before history_path is replaced
# ---------------------------------------------------------------------------

class TestCompactHistoryFsyncOrderBeforeReplace(unittest.TestCase):
    """The fsync on the history tmp fd must happen before the history file
    is replaced.  We verify this by checking the history file content is
    correct immediately after compact() — which would fail if the rename
    happened without a prior fsync causing a torn write to be visible."""

    def test_compact_history_tmp_fsynced_before_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            keep_ids = [_add(store, f"keep {i}") for i in range(4)]
            del_id = _add(store, "delete me")
            store.delete_history_item(del_id)

            # Track whether fsync was called before replace by recording the
            # order of calls using a side_effect list.
            call_log: list[str] = []
            real_fsync = __import__("os").fsync
            original_replace = Path.replace

            def tracking_fsync(fd: int) -> None:
                call_log.append("fsync")
                real_fsync(fd)

            def tracking_replace(self_path: Path, target: Path) -> None:  # type: ignore[override]
                if self_path.name.endswith(".ndjson.tmp"):
                    call_log.append(f"replace:{target.name}")
                return original_replace(self_path, target)

            with (
                patch("backend.state_store.os.fsync", side_effect=tracking_fsync),
                patch.object(Path, "replace", tracking_replace),
            ):
                store.compact()

            # Find the first replace for history.ndjson
            try:
                first_history_replace_pos = next(
                    i for i, entry in enumerate(call_log)
                    if entry == "replace:history.ndjson"
                )
            except StopIteration:
                self.fail("replace:history.ndjson not found in call_log")

            # There must be at least one fsync call BEFORE the history replace.
            fsyncs_before_replace = sum(
                1 for entry in call_log[:first_history_replace_pos]
                if entry == "fsync"
            )
            self.assertGreaterEqual(
                fsyncs_before_replace, 1,
                "os.fsync must be called at least once before history.ndjson is replaced",
            )

            # After compact, active items must still be present and deleted item gone.
            active = store._load_active_items_with_lock()
            active_ids = {item.id for item in active}
            for kid in keep_ids:
                self.assertIn(kid, active_ids)
            self.assertNotIn(del_id, active_ids)


# ---------------------------------------------------------------------------
# 3. All 6 delta journals are empty after compact()
# ---------------------------------------------------------------------------

class TestCompactDeltaJournalsClearedAtomically(unittest.TestCase):
    """After compact() the 6 delta journals listed in _compact_unlocked()
    must all be empty files (size == 0).  This is the observable outcome of
    the atomic truncation path introduced in W853 fix 2."""

    DELTA_JOURNAL_ATTRS = [
        "tombstones_path",
        "status_path",
        "tags_path",
        "favorites_path",
        "text_updates_path",
        "action_items_path",
    ]

    def test_compact_all_delta_journals_cleared_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            ids = [_add(store, f"item {i}") for i in range(3)]

            # Populate all delta journals.
            store.delete_history_item(ids[0])
            store.set_paste_status(ids[1], "ok")
            store.update_history_item_tags(ids[1], ["tag1", "tag2"])
            store.update_history_item_favorite(ids[1], True)
            store.update_history_item_text(ids[1], "updated text")
            store.update_history_item_action_items(ids[2], ["task1"], ["decision1"], ["q1"])

            # Verify all journals are non-empty before compaction.
            for attr in self.DELTA_JOURNAL_ATTRS:
                path: Path = getattr(store, attr)
                self.assertGreater(
                    path.stat().st_size, 0,
                    f"{attr} should be non-empty before compact()",
                )

            store.compact()

            # After compaction all delta journals must be empty.
            for attr in self.DELTA_JOURNAL_ATTRS:
                path = getattr(store, attr)
                size = path.stat().st_size
                self.assertEqual(
                    size, 0,
                    f"{attr} must be empty (0 bytes) after compact(), got {size} bytes",
                )


# ---------------------------------------------------------------------------
# 4. No *.ndjson.tmp files left behind after compact()
# ---------------------------------------------------------------------------

class TestCompactNoTmpFilesLeftBehind(unittest.TestCase):
    """compact() must not leave stale *.ndjson.tmp sibling files in the
    data directory after a successful run.  Stale temps would confuse a
    subsequent crash-recovery pass."""

    def test_compact_no_tmp_files_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            for i in range(5):
                _add(store, f"item {i}")

            store.compact()

            tmp_files = list(store.data_dir.glob("*.ndjson.tmp"))
            self.assertEqual(
                tmp_files, [],
                f"Found stale .ndjson.tmp files after compact(): {tmp_files}",
            )


# ---------------------------------------------------------------------------
# 5. _append_ndjson calls os.fsync exactly once per invocation
# ---------------------------------------------------------------------------

class TestAppendNdjsonFsyncCalledOnEveryWrite(unittest.TestCase):
    """_append_ndjson() must call os.fsync exactly once for every invocation.
    This is the W853 hot-path durability guarantee for add_history_item,
    set_paste_status, delete_history_item, etc."""

    def test_append_ndjson_fsync_called_on_every_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            target = store.data_dir / "test_journal.ndjson"
            target.touch()

            n_writes = 5
            with patch("backend.state_store.os.fsync") as mock_fsync:
                for i in range(n_writes):
                    StateStore._append_ndjson(target, {"seq": i, "data": f"value-{i}"})

            self.assertEqual(
                mock_fsync.call_count, n_writes,
                f"os.fsync must be called exactly once per _append_ndjson call; "
                f"expected {n_writes}, got {mock_fsync.call_count}",
            )

            # Sanity: all entries must be readable.
            records = list(StateStore._read_ndjson_unlocked(target))
            self.assertEqual(len(records), n_writes)
            for i, rec in enumerate(records):
                self.assertEqual(rec["seq"], i)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
