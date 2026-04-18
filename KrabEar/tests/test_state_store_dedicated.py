"""Dedicated unit tests for StateStore (KrabEar/backend/state_store.py).

Covers:
- append + load history
- tombstone-based delete (file-level verification)
- compact removes tombstones physically
- concurrent appends via threading
- corrupt NDJSON lines are skipped
- settings round-trip
- settings merge semantics (DEFAULT_SETTINGS baseline)
- settings replace semantics (second save overwrites)
- get_history_stats
- count_active_items
- vocabulary round-trip
- idempotency check
- pagination (get_history_page)
- maybe_compact threshold
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore  # noqa: E402
from backend.models import DEFAULT_SETTINGS  # noqa: E402


def _make_store(tmp_dir: str, **kwargs) -> StateStore:
    return StateStore(Path(tmp_dir) / "data", **kwargs)


class AppendAndLoadTestCase(unittest.TestCase):
    """add_history_item + get_history_page basic behaviour."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(self.tmp.name)

    def test_append_three_items_and_load_all(self) -> None:
        self.store.add_history_item(text="alpha")
        self.store.add_history_item(text="beta")
        self.store.add_history_item(text="gamma")

        page, next_cursor = self.store.get_history_page(cursor=None, limit=10)
        self.assertEqual(len(page), 3)
        self.assertIsNone(next_cursor)

    def test_returned_items_contain_expected_text(self) -> None:
        self.store.add_history_item(text="hello-world")
        page, _ = self.store.get_history_page(cursor=None, limit=10)
        texts = [item["text"] for item in page]
        self.assertIn("hello-world", texts)

    def test_count_active_items(self) -> None:
        self.assertEqual(self.store.count_active_items(), 0)
        self.store.add_history_item(text="one")
        self.store.add_history_item(text="two")
        self.assertEqual(self.store.count_active_items(), 2)

    def test_items_returned_newest_first(self) -> None:
        self.store.add_history_item(text="first")
        self.store.add_history_item(text="last")
        page, _ = self.store.get_history_page(cursor=None, limit=10)
        # newest-first: "last" should appear before "first"
        texts = [item["text"] for item in page]
        self.assertEqual(texts[0], "last")
        self.assertEqual(texts[1], "first")


class TombstoneDeleteTestCase(unittest.TestCase):
    """delete_history_item writes tombstone; item disappears from active list."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(self.tmp.name)

    def test_deleted_item_absent_from_load(self) -> None:
        item = self.store.add_history_item(text="to-be-deleted")
        result = self.store.delete_history_item(item.id)
        self.assertTrue(result)

        page, _ = self.store.get_history_page(cursor=None, limit=10)
        ids = [r["id"] for r in page]
        self.assertNotIn(item.id, ids)

    def test_tombstone_entry_written_to_file(self) -> None:
        item = self.store.add_history_item(text="mark-me")
        self.store.delete_history_item(item.id)

        tombstones_path = self.store.tombstones_path
        raw = tombstones_path.read_text(encoding="utf-8")
        self.assertIn(item.id, raw)

        # Verify the tombstone is a valid JSON line with the correct id
        found = False
        for line in raw.splitlines():
            try:
                payload = json.loads(line)
                if payload.get("id") == item.id:
                    found = True
                    break
            except json.JSONDecodeError:
                pass
        self.assertTrue(found, "Tombstone JSON entry not found in file")

    def test_delete_empty_id_returns_false(self) -> None:
        self.assertFalse(self.store.delete_history_item(""))
        self.assertFalse(self.store.delete_history_item("   "))

    def test_other_items_unaffected_after_delete(self) -> None:
        a = self.store.add_history_item(text="keep-a")
        b = self.store.add_history_item(text="delete-b")
        self.store.delete_history_item(b.id)

        page, _ = self.store.get_history_page(cursor=None, limit=10)
        ids = [r["id"] for r in page]
        self.assertIn(a.id, ids)
        self.assertNotIn(b.id, ids)


class CompactTestCase(unittest.TestCase):
    """compact() removes tombstones physically from NDJSON files."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(self.tmp.name)

    def test_compact_removes_deleted_entries_from_history_file(self) -> None:
        items = [self.store.add_history_item(text=f"item-{i}") for i in range(5)]
        # Delete 3 of 5
        for item in items[:3]:
            self.store.delete_history_item(item.id)

        self.store.compact()

        # After compact: history file should only have 2 active lines
        raw = self.store.history_path.read_text(encoding="utf-8")
        lines = [l for l in raw.splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)

        # Deleted IDs must NOT appear in the file
        for item in items[:3]:
            self.assertNotIn(item.id, raw)

    def test_compact_clears_tombstones_file(self) -> None:
        item = self.store.add_history_item(text="bye")
        self.store.delete_history_item(item.id)
        self.assertGreater(self.store.tombstones_path.stat().st_size, 0)

        self.store.compact()
        self.assertEqual(self.store.tombstones_path.stat().st_size, 0)

    def test_compact_with_stats_returns_counts(self) -> None:
        for i in range(4):
            item = self.store.add_history_item(text=f"row-{i}")
            self.store.delete_history_item(item.id)

        stats = self.store.compact_with_stats()
        self.assertIn("before_active_count", stats)
        self.assertIn("after_active_count", stats)
        self.assertEqual(stats["after_active_count"], 0)

    def test_maybe_compact_triggers_above_threshold(self) -> None:
        # Use a tiny threshold so even one item triggers compaction
        store = StateStore(Path(self.tmp.name) / "tiny", compact_threshold_bytes=1)
        store.add_history_item(text="trigger-compact")
        triggered = store.maybe_compact()
        self.assertTrue(triggered)

    def test_maybe_compact_skips_below_threshold(self) -> None:
        store = StateStore(
            Path(self.tmp.name) / "big",
            compact_threshold_bytes=100 * 1024 * 1024,
        )
        store.add_history_item(text="no-compact")
        triggered = store.maybe_compact()
        self.assertFalse(triggered)


class ConcurrentAppendTestCase(unittest.TestCase):
    """File-lock prevents data loss during concurrent appends."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(self.tmp.name)

    def test_concurrent_appends_all_persisted(self) -> None:
        n_threads = 10
        errors: list[Exception] = []

        def append_items() -> None:
            try:
                for _ in range(3):
                    self.store.add_history_item(text="concurrent")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=append_items) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Threads raised: {errors}")
        count = self.store.count_active_items()
        self.assertEqual(count, n_threads * 3)


class CorruptLineTestCase(unittest.TestCase):
    """Malformed NDJSON lines are silently skipped."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(self.tmp.name)

    def test_corrupt_line_skipped_valid_items_returned(self) -> None:
        good = self.store.add_history_item(text="good-item")

        # Inject a corrupt line directly into the NDJSON file
        with self.store.history_path.open("a", encoding="utf-8") as fh:
            fh.write("not valid json\n")
            fh.write("{broken: true\n")

        # Add another good item after the corruption
        good2 = self.store.add_history_item(text="also-good")

        page, _ = self.store.get_history_page(cursor=None, limit=20)
        ids = [r["id"] for r in page]
        self.assertIn(good.id, ids)
        self.assertIn(good2.id, ids)
        # Total should be exactly 2 (corrupt lines ignored)
        self.assertEqual(len(page), 2)


class SettingsRoundTripTestCase(unittest.TestCase):
    """save_settings / load_settings round-trip and merge semantics."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(self.tmp.name)

    def test_save_and_load_returns_identical_custom_values(self) -> None:
        payload = {"language": "ru", "max_history": 500, "custom_key": "custom_val"}
        saved = self.store.save_settings(payload)
        loaded = self.store.load_settings()

        for key, value in payload.items():
            self.assertEqual(loaded[key], value, f"Key {key!r} mismatch")

        # Returned value from save_settings should match load_settings
        self.assertEqual(saved, loaded)

    def test_load_returns_defaults_when_no_file(self) -> None:
        # Fresh store: settings file was not written yet
        loaded = self.store.load_settings()
        for key, value in DEFAULT_SETTINGS.items():
            self.assertIn(key, loaded)

    def test_settings_merge_with_defaults(self) -> None:
        # save only one key; all DEFAULT_SETTINGS keys should still be present
        self.store.save_settings({"language": "es"})
        loaded = self.store.load_settings()
        for key in DEFAULT_SETTINGS:
            self.assertIn(key, loaded, f"Default key {key!r} missing after partial save")

    def test_sequential_saves_replace_previous(self) -> None:
        """Second save with overlapping keys overwrites first (replace semantics)."""
        self.store.save_settings({"language": "ru"})
        self.store.save_settings({"language": "en"})
        loaded = self.store.load_settings()
        self.assertEqual(loaded["language"], "en")

    def test_settings_file_corrupt_returns_defaults(self) -> None:
        # Write invalid JSON to settings file
        self.store.settings_path.write_text("not json at all", encoding="utf-8")
        loaded = self.store.load_settings()
        # Should silently fall back to defaults
        for key in DEFAULT_SETTINGS:
            self.assertIn(key, loaded)


class HistoryStatsTestCase(unittest.TestCase):
    """get_history_stats reflects actual file state."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(self.tmp.name)

    def test_stats_after_appends_and_deletes(self) -> None:
        items = [self.store.add_history_item(text=f"s{i}") for i in range(3)]
        self.store.delete_history_item(items[0].id)

        stats = self.store.get_history_stats()
        self.assertEqual(stats["active_count"], 2)
        self.assertEqual(stats["history_lines"], 3)
        self.assertEqual(stats["tombstones_lines"], 1)
        self.assertGreater(stats["history_bytes"], 0)
        self.assertGreater(stats["total_bytes"], 0)


class VocabularyRoundTripTestCase(unittest.TestCase):
    """save_vocabulary / load_vocabulary persistence."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(self.tmp.name)

    def test_save_and_load_vocabulary(self) -> None:
        words = ["hello", "world", "краб"]
        self.store.save_vocabulary(words)
        loaded = self.store.load_vocabulary()
        self.assertEqual(sorted(loaded), sorted(words))

    def test_vocabulary_deduplicated_on_save(self) -> None:
        self.store.save_vocabulary(["dup", "dup", "unique"])
        loaded = self.store.load_vocabulary()
        self.assertEqual(loaded.count("dup"), 1)

    def test_empty_vocabulary_returns_empty_list(self) -> None:
        self.store.save_vocabulary([])
        self.assertEqual(self.store.load_vocabulary(), [])


class PaginationTestCase(unittest.TestCase):
    """get_history_page cursor-based pagination."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(self.tmp.name)

    def test_pagination_returns_correct_page_sizes(self) -> None:
        for i in range(10):
            self.store.add_history_item(text=f"p{i}")

        page1, cursor = self.store.get_history_page(cursor=None, limit=4)
        self.assertEqual(len(page1), 4)
        self.assertIsNotNone(cursor)

        page2, cursor2 = self.store.get_history_page(cursor=cursor, limit=4)
        self.assertEqual(len(page2), 4)
        self.assertIsNotNone(cursor2)

        page3, cursor3 = self.store.get_history_page(cursor=cursor2, limit=4)
        self.assertEqual(len(page3), 2)
        self.assertIsNone(cursor3)

        # All 10 unique
        all_ids = [r["id"] for r in page1 + page2 + page3]
        self.assertEqual(len(set(all_ids)), 10)


class IdempotencyCheckTestCase(unittest.TestCase):
    """is_idempotent returns True for already-processed chat+message pairs."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(self.tmp.name)

    def test_is_idempotent_false_for_new_pair(self) -> None:
        self.assertFalse(self.store.is_idempotent("chat1", "msg1"))

    def test_is_idempotent_true_after_item_added(self) -> None:
        self.store.add_history_item(text="dup-check", chat_id="chat42", message_id="msg7")
        self.assertTrue(self.store.is_idempotent("chat42", "msg7"))

    def test_is_idempotent_false_for_none_inputs(self) -> None:
        self.assertFalse(self.store.is_idempotent(None, None))
        self.assertFalse(self.store.is_idempotent("chat1", None))
        self.assertFalse(self.store.is_idempotent(None, "msg1"))


if __name__ == "__main__":
    unittest.main()
