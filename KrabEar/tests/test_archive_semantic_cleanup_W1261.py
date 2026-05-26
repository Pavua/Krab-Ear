"""Tests for W1255 F1+F3: ArchiveManager semantic-search cleanup (W1261).

Covers:
- archive_items calls semantic_searcher.remove_item for each archived item
- unarchive_items calls semantic_searcher.index_item after successful restore
- archive_items without semantic_searcher is a no-op (no AttributeError)
- semantic_searcher.remove_item failure does not abort the archive operation
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.archive_manager import ArchiveManager  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    def __init__(self, item_id: str, text: str, ts: str = "2026-01-01T10:00:00") -> None:
        self.id = item_id
        self.text = text
        self.ts = ts
        self.paste_status = "ok"
        self.source_text = ""
        self.translated_text = ""
        self.translation_mode = "off"
        self.source_lang = ""
        self.target_lang = ""
        self.translation_status = "not_requested"
        self.translation_engine = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "text": self.text,
            "paste_status": self.paste_status,
            "source_text": self.source_text,
            "translated_text": self.translated_text,
            "translation_mode": self.translation_mode,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "translation_status": self.translation_status,
            "translation_engine": self.translation_engine,
        }


class FakeStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = Path(data_dir)
        self._items: dict[str, FakeHistoryItem] = {}
        self._deleted: set[str] = set()
        self._added: list[dict[str, Any]] = []

    def add_fake_item(self, item_id: str, text: str) -> FakeHistoryItem:
        item = FakeHistoryItem(item_id, text)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str) -> FakeHistoryItem | None:
        if item_id in self._deleted:
            return None
        return self._items.get(item_id)

    def delete_history_item(self, item_id: str) -> bool:
        if item_id in self._items:
            self._deleted.add(item_id)
            return True
        return False

    def add_history_item(self, text: str, **kwargs: Any) -> FakeHistoryItem:
        item = FakeHistoryItem(item_id="restored-" + text[:8], text=text)
        self._items[item.id] = item
        self._added.append({"text": text, **kwargs})
        return item


class FakeSemanticSearcher:
    """Records calls to remove_item and index_item."""

    def __init__(self, remove_raises: bool = False) -> None:
        self.removed: list[str] = []
        self.indexed: list[tuple[str, str]] = []
        self._remove_raises = remove_raises

    def remove_item(self, item_id: str) -> bool:
        if self._remove_raises:
            raise RuntimeError("simulated remove failure")
        self.removed.append(item_id)
        return True

    def index_item(self, item_id: str, text: str) -> bool:
        self.indexed.append((item_id, text))
        return True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestArchiveSemanticRemove(unittest.TestCase):
    """archive_items calls semantic_searcher.remove_item for each archived item."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._searcher = FakeSemanticSearcher()
        self._mgr = ArchiveManager(store=self._store, semantic_searcher=self._searcher)

    def test_archive_items_calls_semantic_remove(self) -> None:
        self._store.add_fake_item("id-1", "first transcript")
        self._store.add_fake_item("id-2", "second transcript")

        result = self._mgr.archive_items(["id-1", "id-2"])

        self.assertEqual(result.archived_count, 2)
        self.assertIn("id-1", self._searcher.removed)
        self.assertIn("id-2", self._searcher.removed)
        self.assertEqual(len(self._searcher.removed), 2)

    def test_archive_items_remove_called_once_per_item(self) -> None:
        self._store.add_fake_item("id-3", "third transcript")

        self._mgr.archive_items(["id-3"])

        self.assertEqual(self._searcher.removed, ["id-3"])

    def test_archive_items_skips_missing_item_no_remove(self) -> None:
        """Items not found in store must not trigger remove_item."""
        result = self._mgr.archive_items(["nonexistent-id"])

        self.assertEqual(result.archived_count, 0)
        self.assertEqual(self._searcher.removed, [])


class TestUnarchiveSemanticIndex(unittest.TestCase):
    """unarchive_items calls semantic_searcher.index_item after restore."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._searcher = FakeSemanticSearcher()
        self._mgr = ArchiveManager(store=self._store, semantic_searcher=self._searcher)

    def _archive_item(self, item_id: str, text: str) -> None:
        self._store.add_fake_item(item_id, text)
        self._mgr.archive_items([item_id])
        # Reset remove tracking to isolate unarchive test
        self._searcher.removed.clear()

    def test_unarchive_items_calls_semantic_index(self) -> None:
        self._archive_item("id-A", "hello world transcript")

        result = self._mgr.unarchive_items(["id-A"])

        self.assertEqual(result["unarchived_count"], 1)
        self.assertEqual(len(self._searcher.indexed), 1)
        indexed_id, indexed_text = self._searcher.indexed[0]
        self.assertEqual(indexed_id, "id-A")
        self.assertEqual(indexed_text, "hello world transcript")

    def test_unarchive_items_not_found_no_index_call(self) -> None:
        result = self._mgr.unarchive_items(["ghost-id"])

        self.assertEqual(result["unarchived_count"], 0)
        self.assertEqual(self._searcher.indexed, [])

    def test_unarchive_items_empty_text_no_index_call(self) -> None:
        """Items with empty text should not trigger index_item."""
        # Manually write an archived item with empty text
        import json
        archive_file = Path(self._tmpdir) / "archive" / "archive.ndjson"
        record = {
            "id": "id-empty",
            "text": "",
            "paste_status": "ok",
            "archived_at": "2026-01-01T10:00:00+00:00",
        }
        with archive_file.open("a") as fh:
            fh.write(json.dumps(record) + "\n")

        result = self._mgr.unarchive_items(["id-empty"])

        self.assertEqual(result["unarchived_count"], 1)
        self.assertEqual(self._searcher.indexed, [])


class TestArchiveWithoutSemanticSearcher(unittest.TestCase):
    """archive_items/unarchive_items without semantic_searcher is a no-op."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = ArchiveManager(store=self._store)  # no semantic_searcher

    def test_archive_without_semantic_searcher_no_op(self) -> None:
        self._store.add_fake_item("id-X", "some text")
        # Must not raise even though no semantic_searcher is set
        result = self._mgr.archive_items(["id-X"])
        self.assertEqual(result.archived_count, 1)

    def test_unarchive_without_semantic_searcher_no_op(self) -> None:
        self._store.add_fake_item("id-Y", "some other text")
        self._mgr.archive_items(["id-Y"])

        result = self._mgr.unarchive_items(["id-Y"])
        self.assertEqual(result["unarchived_count"], 1)

    def test_semantic_searcher_none_attribute(self) -> None:
        self.assertIsNone(self._mgr._semantic_searcher)


class TestSemanticRemoveFailureDoesNotBreakArchive(unittest.TestCase):
    """semantic_searcher.remove_item failure must not prevent archiving."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._searcher = FakeSemanticSearcher(remove_raises=True)
        self._mgr = ArchiveManager(store=self._store, semantic_searcher=self._searcher)

    def test_semantic_remove_failure_does_not_break_archive(self) -> None:
        self._store.add_fake_item("id-fail", "important transcript")

        # Should complete successfully despite remove_item raising
        result = self._mgr.archive_items(["id-fail"])

        self.assertEqual(result.archived_count, 1)
        # Item must still be deleted from the store
        self.assertIn("id-fail", self._store._deleted)

    def test_multiple_items_continue_after_remove_failure(self) -> None:
        self._store.add_fake_item("id-f1", "transcript one")
        self._store.add_fake_item("id-f2", "transcript two")

        result = self._mgr.archive_items(["id-f1", "id-f2"])

        self.assertEqual(result.archived_count, 2)
        self.assertIn("id-f1", self._store._deleted)
        self.assertIn("id-f2", self._store._deleted)


class TestLateInjection(unittest.TestCase):
    """semantic_searcher can be injected after __init__ via attribute assignment."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = ArchiveManager(store=self._store)  # None by default

    def test_late_injection_works(self) -> None:
        searcher = FakeSemanticSearcher()
        self._mgr._semantic_searcher = searcher

        self._store.add_fake_item("id-late", "late text")
        result = self._mgr.archive_items(["id-late"])

        self.assertEqual(result.archived_count, 1)
        self.assertIn("id-late", searcher.removed)


if __name__ == "__main__":
    unittest.main()
