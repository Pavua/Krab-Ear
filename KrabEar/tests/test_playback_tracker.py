"""Тесты PlaybackTracker — отслеживание воспроизведения записей Krab Ear."""

from __future__ import annotations
from backend.playback_tracker import PlaybackTracker

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

# Настройка путей для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Вспомогательный фейковый store
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    """Минимальная заглушка HistoryItem."""

    def __init__(self, item_id: str):
        self._id = item_id

    def to_dict(self) -> dict:
        return {"id": self._id, "text": f"текст {self._id}"}


class FakeStore:
    """Заглушка StateStore с пагинацией."""

    def __init__(self, items: list[str]):
        self._items = [FakeHistoryItem(iid) for iid in items]

    def get_history_page_filtered(self, cursor=None, limit=50, **_):
        start = int(cursor) if cursor is not None else 0
        end = start + limit
        page = self._items[start:end]
        next_cursor = str(end) if end < len(self._items) else None
        return page, next_cursor


# ===========================================================================
# 1. Базовые операции record_playback / get_playback_stats
# ===========================================================================

class TestRecordPlayback(unittest.TestCase):
    """Проверяет базовый учёт воспроизведений."""

    def setUp(self):
        self.tracker = PlaybackTracker()  # data_dir=None → in-memory

    def test_initial_stats_zero(self):
        stats = self.tracker.get_playback_stats("item_abc")
        self.assertEqual(stats["play_count"], 0)
        self.assertEqual(stats["total_listened_sec"], 0.0)
        self.assertIsNone(stats["last_played"])
        self.assertEqual(stats["item_id"], "item_abc")

    def test_single_playback_increments_count(self):
        self.tracker.record_playback("item_1", duration_listened_sec=10.0)
        stats = self.tracker.get_playback_stats("item_1")
        self.assertEqual(stats["play_count"], 1)

    def test_single_playback_records_duration(self):
        self.tracker.record_playback("item_1", duration_listened_sec=30.5)
        stats = self.tracker.get_playback_stats("item_1")
        self.assertAlmostEqual(stats["total_listened_sec"], 30.5, places=3)

    def test_multiple_playbacks_accumulate(self):
        self.tracker.record_playback("item_x", duration_listened_sec=10.0)
        self.tracker.record_playback("item_x", duration_listened_sec=20.0)
        self.tracker.record_playback("item_x", duration_listened_sec=5.0)
        stats = self.tracker.get_playback_stats("item_x")
        self.assertEqual(stats["play_count"], 3)
        self.assertAlmostEqual(stats["total_listened_sec"], 35.0, places=3)

    def test_last_played_is_set(self):
        self.tracker.record_playback("item_2")
        stats = self.tracker.get_playback_stats("item_2")
        self.assertIsNotNone(stats["last_played"])
        # ISO8601 строка
        self.assertIn("T", stats["last_played"])

    def test_zero_duration_is_valid(self):
        self.tracker.record_playback("item_3", duration_listened_sec=0.0)
        stats = self.tracker.get_playback_stats("item_3")
        self.assertEqual(stats["play_count"], 1)
        self.assertEqual(stats["total_listened_sec"], 0.0)

    def test_negative_duration_clamped_to_zero(self):
        self.tracker.record_playback("item_4", duration_listened_sec=-5.0)
        stats = self.tracker.get_playback_stats("item_4")
        self.assertEqual(stats["total_listened_sec"], 0.0)

    def test_empty_item_id_raises(self):
        with self.assertRaises(ValueError):
            self.tracker.record_playback("", duration_listened_sec=1.0)

    def test_whitespace_item_id_raises(self):
        with self.assertRaises(ValueError):
            self.tracker.record_playback("   ")

    def test_different_items_tracked_independently(self):
        self.tracker.record_playback("a", duration_listened_sec=5.0)
        self.tracker.record_playback("b", duration_listened_sec=10.0)
        self.tracker.record_playback("a", duration_listened_sec=3.0)
        stats_a = self.tracker.get_playback_stats("a")
        stats_b = self.tracker.get_playback_stats("b")
        self.assertEqual(stats_a["play_count"], 2)
        self.assertEqual(stats_b["play_count"], 1)
        self.assertAlmostEqual(stats_a["total_listened_sec"], 8.0)
        self.assertAlmostEqual(stats_b["total_listened_sec"], 10.0)


# ===========================================================================
# 2. get_most_replayed
# ===========================================================================

class TestGetMostReplayed(unittest.TestCase):

    def setUp(self):
        self.tracker = PlaybackTracker()

    def test_empty_returns_empty_list(self):
        result = self.tracker.get_most_replayed(limit=10)
        self.assertEqual(result, [])

    def test_sorted_by_play_count_descending(self):
        self.tracker.record_playback("low", duration_listened_sec=1.0)
        for _ in range(5):
            self.tracker.record_playback("high", duration_listened_sec=2.0)
        for _ in range(3):
            self.tracker.record_playback("mid", duration_listened_sec=1.5)

        result = self.tracker.get_most_replayed(limit=10)
        counts = [r["play_count"] for r in result]
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertEqual(result[0]["item_id"], "high")

    def test_limit_respected(self):
        for i in range(20):
            self.tracker.record_playback(f"item_{i}", duration_listened_sec=float(i))
        result = self.tracker.get_most_replayed(limit=5)
        self.assertEqual(len(result), 5)

    def test_limit_one_returns_single_item(self):
        self.tracker.record_playback("a")
        self.tracker.record_playback("b")
        result = self.tracker.get_most_replayed(limit=1)
        self.assertEqual(len(result), 1)

    def test_result_contains_required_keys(self):
        self.tracker.record_playback("item_z", duration_listened_sec=7.0)
        result = self.tracker.get_most_replayed(limit=5)
        self.assertEqual(len(result), 1)
        item = result[0]
        self.assertIn("item_id", item)
        self.assertIn("play_count", item)
        self.assertIn("total_listened_sec", item)
        self.assertIn("last_played", item)

    def test_tiebreak_by_total_listened_sec(self):
        # Оба воспроизведены по 2 раза, но разное суммарное время.
        self.tracker.record_playback("short1", duration_listened_sec=1.0)
        self.tracker.record_playback("short1", duration_listened_sec=1.0)
        self.tracker.record_playback("long1", duration_listened_sec=50.0)
        self.tracker.record_playback("long1", duration_listened_sec=50.0)
        result = self.tracker.get_most_replayed(limit=5)
        self.assertEqual(result[0]["item_id"], "long1")


# ===========================================================================
# 3. get_never_played
# ===========================================================================

class TestGetNeverPlayed(unittest.TestCase):

    def test_all_items_never_played(self):
        store = FakeStore(["id1", "id2", "id3"])
        tracker = PlaybackTracker()
        result = tracker.get_never_played(store, limit=50)
        ids = [r["id"] for r in result]
        self.assertIn("id1", ids)
        self.assertIn("id2", ids)
        self.assertIn("id3", ids)

    def test_played_items_excluded(self):
        store = FakeStore(["id1", "id2", "id3"])
        tracker = PlaybackTracker()
        tracker.record_playback("id2")
        result = tracker.get_never_played(store, limit=50)
        ids = [r["id"] for r in result]
        self.assertNotIn("id2", ids)
        self.assertIn("id1", ids)
        self.assertIn("id3", ids)

    def test_limit_respected(self):
        store = FakeStore([f"x{i}" for i in range(30)])
        tracker = PlaybackTracker()
        result = tracker.get_never_played(store, limit=5)
        self.assertLessEqual(len(result), 5)

    def test_empty_store_returns_empty(self):
        store = FakeStore([])
        tracker = PlaybackTracker()
        result = tracker.get_never_played(store, limit=10)
        self.assertEqual(result, [])

    def test_all_played_returns_empty(self):
        store = FakeStore(["a", "b", "c"])
        tracker = PlaybackTracker()
        for iid in ["a", "b", "c"]:
            tracker.record_playback(iid)
        result = tracker.get_never_played(store, limit=50)
        self.assertEqual(result, [])


# ===========================================================================
# 4. Персистентность
# ===========================================================================

class TestPersistence(unittest.TestCase):

    def test_stats_saved_to_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = PlaybackTracker(data_dir=tmpdir)
            tracker.record_playback("item_p1", duration_listened_sec=15.0)
            path = Path(tmpdir) / "playback_stats.json"
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("item_p1", data)
            self.assertEqual(data["item_p1"]["play_count"], 1)
            self.assertAlmostEqual(data["item_p1"]["total_listened_sec"], 15.0)

    def test_stats_reloaded_on_new_instance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            t1 = PlaybackTracker(data_dir=tmpdir)
            t1.record_playback("item_q1", duration_listened_sec=42.0)
            t1.record_playback("item_q1", duration_listened_sec=8.0)

            t2 = PlaybackTracker(data_dir=tmpdir)
            stats = t2.get_playback_stats("item_q1")
            self.assertEqual(stats["play_count"], 2)
            self.assertAlmostEqual(stats["total_listened_sec"], 50.0)

    def test_no_data_dir_does_not_crash(self):
        tracker = PlaybackTracker(data_dir=None)
        tracker.record_playback("item_nodisk", duration_listened_sec=5.0)
        stats = tracker.get_playback_stats("item_nodisk")
        self.assertEqual(stats["play_count"], 1)

    def test_corrupt_file_handled_gracefully(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "playback_stats.json"
            path.write_text("{ NOT VALID JSON !!!}", encoding="utf-8")
            # Не должно бросать исключений
            tracker = PlaybackTracker(data_dir=tmpdir)
            self.assertEqual(tracker.get_playback_stats("x")["play_count"], 0)


# ===========================================================================
# 5. IPC-обработчики
# ===========================================================================

class TestIPCHandlers(unittest.TestCase):

    def setUp(self):
        self.tracker = PlaybackTracker()

    def test_handle_record_playback_returns_stats(self):
        result = self.tracker.handle_record_playback(
            {"item_id": "ipc_item1", "duration_listened_sec": 20.0}
        )
        self.assertEqual(result["item_id"], "ipc_item1")
        self.assertEqual(result["play_count"], 1)
        self.assertAlmostEqual(result["total_listened_sec"], 20.0)

    def test_handle_record_playback_missing_item_id_raises(self):
        with self.assertRaises(ValueError):
            self.tracker.handle_record_playback({"duration_listened_sec": 5.0})

    def test_handle_get_playback_stats(self):
        self.tracker.record_playback("ipc_item2", duration_listened_sec=7.0)
        result = self.tracker.handle_get_playback_stats({"item_id": "ipc_item2"})
        self.assertEqual(result["play_count"], 1)
        self.assertAlmostEqual(result["total_listened_sec"], 7.0)

    def test_handle_get_playback_stats_missing_id_raises(self):
        with self.assertRaises(ValueError):
            self.tracker.handle_get_playback_stats({})

    def test_handle_get_most_replayed_returns_dict(self):
        self.tracker.record_playback("a")
        self.tracker.record_playback("a")
        self.tracker.record_playback("b")
        result = self.tracker.handle_get_most_replayed({"limit": 5})
        self.assertIn("items", result)
        self.assertIn("count", result)
        self.assertGreaterEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["item_id"], "a")

    def test_handle_get_most_replayed_default_limit(self):
        for i in range(15):
            self.tracker.record_playback(f"item_{i}")
        result = self.tracker.handle_get_most_replayed({})
        self.assertLessEqual(result["count"], 10)

    def test_handle_record_playback_zero_duration(self):
        result = self.tracker.handle_record_playback({"item_id": "ipc_zero"})
        self.assertEqual(result["play_count"], 1)
        self.assertEqual(result["total_listened_sec"], 0.0)


# ===========================================================================
# 6. Потокобезопасность
# ===========================================================================

class TestThreadSafety(unittest.TestCase):

    def test_concurrent_record_playback(self):
        tracker = PlaybackTracker()
        n_threads = 20
        replays_per_thread = 10

        def worker():
            for _ in range(replays_per_thread):
                tracker.record_playback("shared_item", duration_listened_sec=1.0)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = tracker.get_playback_stats("shared_item")
        expected = n_threads * replays_per_thread
        self.assertEqual(stats["play_count"], expected)
        self.assertAlmostEqual(stats["total_listened_sec"], float(expected), places=1)


if __name__ == "__main__":
    unittest.main()
