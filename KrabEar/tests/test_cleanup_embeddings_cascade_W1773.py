"""W1773: handle_cleanup_old_history must cascade semantic-search embedding
removal and playback-stats removal for age-deleted items.

Bug: The bulk age-delete path (IPC ``cleanup_old_history``) tombstoned items
and cascaded to transcript_versions / recording_chains / .md erase, but did
NOT call ``self._semantic_searcher.remove_item`` or
``self._playback_tracker.remove_stats`` for the deleted ids — leaving orphan
embedding rows in embeddings.npy and orphan playback stats for every
age-deleted transcript.  The single-delete path
(``handle_delete_history_item``) already performs these cascades.

Fix (history_service.py): mirrors the single-delete cascade set — iterates
``to_delete`` with soft try/except for each collaborator.

Tests:
  1. Semantic-searcher remove_item is called for each age-deleted item.
  2. Non-age-deleted items are NOT passed to remove_item.
  3. Exception from remove_item is soft-failed (no propagation).
  4. No semantic_searcher wired → cleanup still succeeds.
  5. playback_tracker remove_stats is called for each age-deleted item.
  6. Exception from remove_stats is soft-failed.
  7. No playback_tracker wired → cleanup still succeeds.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    def __init__(self, item_id: str, ts: str) -> None:
        self.id = item_id
        self.ts = ts
        self.text = "dummy"
        self.duration_sec = 0.0
        self.paste_status = "ok"


class FakeStore:
    """Minimal StateStore stub for handle_cleanup_old_history tests.

    data_dir must be a real Path so that _erase_transcript_md can construct
    the transcripts/ sub-path without raising TypeError.
    """

    def __init__(self, data_dir: Path) -> None:
        self._items: list[FakeHistoryItem] = []
        self._tombstones: list[dict] = []
        self._lock_obj = threading.Lock()
        self.data_dir = data_dir
        self.history_path = data_dir / "history.ndjson"
        self.tombstones_path = data_dir / "tombstones.ndjson"

    def add_item(self, item_id: str, ts: str) -> FakeHistoryItem:
        item = FakeHistoryItem(item_id, ts)
        self._items.append(item)
        return item

    def _lock(self):
        return self._lock_obj

    def _load_active_items_unlocked(self) -> list[FakeHistoryItem]:
        return list(self._items)

    def _append_ndjson(self, path, payload: dict) -> None:
        self._tombstones.append(payload)


class _FakeSemanticSearcher:
    def __init__(self) -> None:
        self.removed: list[str] = []
        self.raise_on_remove: Exception | None = None

    def remove_item(self, item_id: str) -> None:
        if self.raise_on_remove is not None:
            raise self.raise_on_remove
        self.removed.append(item_id)


class _FakePlaybackTracker:
    def __init__(self) -> None:
        self.removed: list[str] = []
        self.raise_on_remove: Exception | None = None

    def remove_stats(self, item_id: str) -> None:
        if self.raise_on_remove is not None:
            raise self.raise_on_remove
        self.removed.append(item_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OLD_TS = "2000-01-01T00:00:00+00:00"     # definitely older than any threshold
_RECENT_TS = "2099-12-31T23:59:59+00:00"  # definitely newer


_tmpdir_obj = tempfile.TemporaryDirectory()
_TMPDIR = Path(_tmpdir_obj.name)


def _make_service(old_ids=("old-a",), recent_ids=()):
    """Return (svc, store, searcher, tracker) with pre-populated items."""
    from backend.history_service import HistoryService

    store = FakeStore(data_dir=_TMPDIR)
    for iid in old_ids:
        store.add_item(iid, _OLD_TS)
    for iid in recent_ids:
        store.add_item(iid, _RECENT_TS)

    svc = HistoryService(store=store)
    searcher = _FakeSemanticSearcher()
    tracker = _FakePlaybackTracker()
    svc._semantic_searcher = searcher
    svc._playback_tracker = tracker
    return svc, store, searcher, tracker


# ---------------------------------------------------------------------------
# Tests: semantic_searcher cascade
# ---------------------------------------------------------------------------

class TestCleanupEmbeddingsCascadeW1773(unittest.TestCase):
    """handle_cleanup_old_history must call semantic_searcher.remove_item
    for every age-deleted item (W1773 fix)."""

    def test_remove_item_called_for_each_old_item(self):
        """All age-deleted ids must appear in searcher.removed."""
        svc, store, searcher, _ = _make_service(old_ids=["old-1", "old-2", "old-3"])
        result = svc.handle_cleanup_old_history({"older_than_days": 1})
        self.assertEqual(result["deleted_count"], 3)
        self.assertCountEqual(["old-1", "old-2", "old-3"], searcher.removed,
                              "remove_item must be called for each age-deleted id")

    def test_recent_items_not_passed_to_remove_item(self):
        """Items within the age threshold must NOT appear in searcher.removed."""
        svc, store, searcher, _ = _make_service(
            old_ids=["old-only"],
            recent_ids=["recent-a", "recent-b"],
        )
        result = svc.handle_cleanup_old_history({"older_than_days": 1})
        self.assertEqual(result["deleted_count"], 1)
        self.assertIn("old-only", searcher.removed)
        self.assertNotIn("recent-a", searcher.removed)
        self.assertNotIn("recent-b", searcher.removed)

    def test_remove_item_exception_is_soft_failed(self):
        """Exception from remove_item must NOT propagate — cleanup still returns ok."""
        svc, store, searcher, _ = _make_service(old_ids=["bad-item"])
        searcher.raise_on_remove = RuntimeError("index corrupted")
        try:
            result = svc.handle_cleanup_old_history({"older_than_days": 1})
        except Exception as exc:
            self.fail(
                f"handle_cleanup_old_history не должен пробрасывать ошибки из "
                f"semantic_searcher.remove_item, но получили: {exc}"
            )
        self.assertEqual(result["deleted_count"], 1)

    def test_no_semantic_searcher_still_cleans(self):
        """If _semantic_searcher is None, cleanup_old_history still succeeds."""
        from backend.history_service import HistoryService

        store = FakeStore(data_dir=_TMPDIR)
        store.add_item("old-x", _OLD_TS)
        svc = HistoryService(store=store)
        svc._semantic_searcher = None  # explicit None

        result = svc.handle_cleanup_old_history({"older_than_days": 1})
        self.assertEqual(result["deleted_count"], 1)

    def test_empty_delete_set_no_remove_item_calls(self):
        """If no items are deleted (all recent), remove_item must not be called."""
        svc, store, searcher, _ = _make_service(old_ids=[], recent_ids=["live-1"])
        result = svc.handle_cleanup_old_history({"older_than_days": 1})
        self.assertEqual(result["deleted_count"], 0)
        self.assertEqual(searcher.removed, [],
                         "remove_item must not be called when nothing is deleted")


# ---------------------------------------------------------------------------
# Tests: playback_tracker cascade
# ---------------------------------------------------------------------------

class TestCleanupPlaybackTrackerCascadeW1773(unittest.TestCase):
    """handle_cleanup_old_history must call playback_tracker.remove_stats
    for every age-deleted item (W1773 fix, mirrors W1343 single-delete F4)."""

    def test_remove_stats_called_for_each_old_item(self):
        """All age-deleted ids must appear in tracker.removed."""
        svc, store, _, tracker = _make_service(old_ids=["pt-old-1", "pt-old-2"])
        result = svc.handle_cleanup_old_history({"older_than_days": 1})
        self.assertEqual(result["deleted_count"], 2)
        self.assertCountEqual(["pt-old-1", "pt-old-2"], tracker.removed,
                              "remove_stats must be called for each age-deleted id")

    def test_remove_stats_exception_is_soft_failed(self):
        """Exception from remove_stats must NOT propagate."""
        svc, store, _, tracker = _make_service(old_ids=["pt-bad"])
        tracker.raise_on_remove = RuntimeError("tracker broken")
        try:
            result = svc.handle_cleanup_old_history({"older_than_days": 1})
        except Exception as exc:
            self.fail(
                f"handle_cleanup_old_history не должен пробрасывать ошибки из "
                f"playback_tracker.remove_stats, но получили: {exc}"
            )
        self.assertEqual(result["deleted_count"], 1)

    def test_no_playback_tracker_still_cleans(self):
        """If _playback_tracker is None, cleanup_old_history still succeeds."""
        from backend.history_service import HistoryService

        store = FakeStore(data_dir=_TMPDIR)
        store.add_item("old-pt-x", _OLD_TS)
        svc = HistoryService(store=store)
        svc._playback_tracker = None  # explicit None

        result = svc.handle_cleanup_old_history({"older_than_days": 1})
        self.assertEqual(result["deleted_count"], 1)


if __name__ == "__main__":
    unittest.main()
