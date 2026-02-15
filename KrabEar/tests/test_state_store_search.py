"""Тесты ускоренного поиска истории в StateStore."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore


class StateStoreSearchTestCase(unittest.TestCase):
    """Проверяет корректность fallback при ускоренном индексе поиска."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")

    def test_search_fallback_finds_old_record_outside_recent_index(self) -> None:
        self.store.add_history_item(text="needle-very-old", paste_status="failed")
        for idx in range(4105):
            self.store.add_history_item(text=f"row-{idx}", paste_status="failed")

        page, next_cursor = self.store.search_history(query="needle-very-old", cursor=None, limit=20)
        self.assertEqual(len(page), 1)
        self.assertIn("needle-very-old", page[0]["text"])
        self.assertIsNone(next_cursor)

    def test_search_index_invalidation_after_new_item(self) -> None:
        page_before, _ = self.store.search_history(query="fresh-hit", cursor=None, limit=20)
        self.assertEqual(page_before, [])

        self.store.add_history_item(text="fresh-hit", paste_status="failed")
        page_after, _ = self.store.search_history(query="fresh-hit", cursor=None, limit=20)
        self.assertEqual(len(page_after), 1)
        self.assertIn("fresh-hit", page_after[0]["text"])


if __name__ == "__main__":
    unittest.main()
