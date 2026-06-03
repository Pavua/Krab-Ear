"""Тесты PlaybackTracker — отслеживание воспроизведения записей Krab Ear."""

from __future__ import annotations
from backend.playback_tracker import PlaybackTracker

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
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


# ===========================================================================
# 7. Агрегированная статистика и расширенные сценарии
# ===========================================================================

class TestAggregatedStats(unittest.TestCase):
    """Проверяет комплексные сценарии с агрегированной статистикой."""

    def test_total_time_across_all_items(self):
        tracker = PlaybackTracker()
        tracker.record_playback("a", duration_listened_sec=10.0)
        tracker.record_playback("b", duration_listened_sec=20.0)
        tracker.record_playback("c", duration_listened_sec=30.0)
        tracker.record_playback("a", duration_listened_sec=5.0)

        stats_a = tracker.get_playback_stats("a")
        stats_b = tracker.get_playback_stats("b")
        stats_c = tracker.get_playback_stats("c")

        total = (
            stats_a["total_listened_sec"]
            + stats_b["total_listened_sec"]
            + stats_c["total_listened_sec"]
        )
        self.assertAlmostEqual(total, 65.0, places=2)

    def test_most_replayed_ties_handled_correctly(self):
        tracker = PlaybackTracker()
        # Три элемента с одинаковым play_count но разными total_listened_sec
        tracker.record_playback("a", duration_listened_sec=10.0)
        tracker.record_playback("a", duration_listened_sec=10.0)
        tracker.record_playback("b", duration_listened_sec=5.0)
        tracker.record_playback("b", duration_listened_sec=5.0)
        tracker.record_playback("c", duration_listened_sec=20.0)
        tracker.record_playback("c", duration_listened_sec=20.0)

        result = tracker.get_most_replayed(limit=3)
        # c должен быть первым (больше total_listened_sec)
        self.assertEqual(result[0]["item_id"], "c")
        self.assertAlmostEqual(result[0]["total_listened_sec"], 40.0)

    def test_persistence_with_multiple_updates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker1 = PlaybackTracker(data_dir=tmpdir)
            tracker1.record_playback("item1", duration_listened_sec=5.0)
            tracker1.record_playback("item2", duration_listened_sec=10.0)

            # Создаём новый трекер — должен загрузить данные
            tracker2 = PlaybackTracker(data_dir=tmpdir)
            stats1 = tracker2.get_playback_stats("item1")
            stats2 = tracker2.get_playback_stats("item2")

            self.assertEqual(stats1["play_count"], 1)
            self.assertAlmostEqual(stats1["total_listened_sec"], 5.0)
            self.assertEqual(stats2["play_count"], 1)
            self.assertAlmostEqual(stats2["total_listened_sec"], 10.0)

    def test_item_id_normalization(self):
        """Проверяет, что item_id нормализуется (trimmed)."""
        tracker = PlaybackTracker()
        tracker.record_playback("  item_with_spaces  ", duration_listened_sec=5.0)
        stats = tracker.get_playback_stats("item_with_spaces")
        self.assertEqual(stats["play_count"], 1)
        # Проверяем что они рассматриваются как один и тот же ID
        stats2 = tracker.get_playback_stats("  item_with_spaces  ")
        self.assertEqual(stats2["play_count"], 1)

    def test_large_number_of_items(self):
        tracker = PlaybackTracker()
        n_items = 100
        for i in range(n_items):
            tracker.record_playback(f"item_{i:03d}", duration_listened_sec=float(i))

        # Проверяем что все записались
        result = tracker.get_most_replayed(limit=n_items)
        self.assertGreaterEqual(len(result), 50)

    def test_empty_store_pagination(self):
        store = FakeStore([])
        tracker = PlaybackTracker()
        result = tracker.get_never_played(store, limit=20)
        self.assertEqual(result, [])

    def test_partially_played_history(self):
        store = FakeStore([f"id_{i}" for i in range(10)])
        tracker = PlaybackTracker()
        # Отметим только половину как воспроизведённые
        for i in range(5):
            tracker.record_playback(f"id_{i}")

        never_played = tracker.get_never_played(store, limit=20)
        never_played_ids = [item["id"] for item in never_played]

        self.assertEqual(len(never_played), 5)
        for i in range(5, 10):
            self.assertIn(f"id_{i}", never_played_ids)


# ===========================================================================
# 8. Граничные случаи и ошибки
# ===========================================================================

class TestEdgeCases(unittest.TestCase):
    """Проверяет граничные случаи и обработку ошибок."""

    def test_very_large_duration(self):
        # wave-34 F1: durations >86400 s (24h) are now rejected to prevent poisoning.
        # 1e6 s (~11.5 days) exceeds the cap → record_playback returns an error dict
        # and play_count stays at 0.
        tracker = PlaybackTracker()
        huge_duration = 1e6  # 1 миллион секунд — превышает лимит 24h
        result = tracker.record_playback("huge", duration_listened_sec=huge_duration)
        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "invalid_duration")
        stats = tracker.get_playback_stats("huge")
        self.assertEqual(stats["play_count"], 0)
        self.assertEqual(stats["total_listened_sec"], 0.0)

    def test_fractional_durations(self):
        tracker = PlaybackTracker()
        tracker.record_playback("frac1", duration_listened_sec=0.001)
        tracker.record_playback("frac1", duration_listened_sec=0.002)
        stats = tracker.get_playback_stats("frac1")
        self.assertAlmostEqual(stats["total_listened_sec"], 0.003, places=5)

    def test_item_id_with_special_chars(self):
        tracker = PlaybackTracker()
        special_id = "item-_123@test"
        tracker.record_playback(special_id, duration_listened_sec=5.0)
        stats = tracker.get_playback_stats(special_id)
        self.assertEqual(stats["item_id"], special_id)
        self.assertEqual(stats["play_count"], 1)

    def test_numeric_item_id_as_string(self):
        tracker = PlaybackTracker()
        tracker.record_playback("12345", duration_listened_sec=3.0)
        stats = tracker.get_playback_stats("12345")
        self.assertEqual(stats["play_count"], 1)

    def test_unicode_item_id(self):
        tracker = PlaybackTracker()
        tracker.record_playback("предмет_тест", duration_listened_sec=2.0)
        stats = tracker.get_playback_stats("предмет_тест")
        self.assertEqual(stats["play_count"], 1)

    def test_persistence_file_overwrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker1 = PlaybackTracker(data_dir=tmpdir)
            tracker1.record_playback("x", duration_listened_sec=10.0)

            # Создаём новый с той же папкой — должен перезагрузить
            tracker2 = PlaybackTracker(data_dir=tmpdir)
            stats = tracker2.get_playback_stats("x")
            self.assertEqual(stats["play_count"], 1)
            self.assertAlmostEqual(stats["total_listened_sec"], 10.0)

            # Добавляем ещё запись
            tracker2.record_playback("x", duration_listened_sec=5.0)
            tracker2.record_playback("y", duration_listened_sec=15.0)

            # Проверяем что файл правильно обновлён
            tracker3 = PlaybackTracker(data_dir=tmpdir)
            stats_x = tracker3.get_playback_stats("x")
            stats_y = tracker3.get_playback_stats("y")

            self.assertEqual(stats_x["play_count"], 2)
            self.assertAlmostEqual(stats_x["total_listened_sec"], 15.0)
            self.assertEqual(stats_y["play_count"], 1)
            self.assertAlmostEqual(stats_y["total_listened_sec"], 15.0)


# ===========================================================================
# 9. Последовательные операции (roundtrip)
# ===========================================================================

class TestRoundtripOperations(unittest.TestCase):
    """Проверяет полный цикл сохранения/загрузки/обновления."""

    def test_full_lifecycle(self):
        """Полный цикл: создание → сохранение → загрузка → обновление."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Этап 1: создание и сохранение
            t1 = PlaybackTracker(data_dir=tmpdir)
            t1.record_playback("item_a", duration_listened_sec=10.0)
            t1.record_playback("item_b", duration_listened_sec=20.0)

            # Этап 2: загрузка в новом экземпляре
            t2 = PlaybackTracker(data_dir=tmpdir)
            self.assertEqual(t2.get_playback_stats("item_a")["play_count"], 1)
            self.assertEqual(t2.get_playback_stats("item_b")["play_count"], 1)

            # Этап 3: обновление
            t2.record_playback("item_a", duration_listened_sec=5.0)

            # Этап 4: повторная загрузка
            t3 = PlaybackTracker(data_dir=tmpdir)
            stats = t3.get_playback_stats("item_a")
            self.assertEqual(stats["play_count"], 2)
            self.assertAlmostEqual(stats["total_listened_sec"], 15.0)

    def test_ipc_roundtrip(self):
        """Проверяет IPC-операции в последовательности."""
        tracker = PlaybackTracker()

        # Запись через IPC
        result1 = tracker.handle_record_playback({
            "item_id": "ipc_test",
            "duration_listened_sec": 10.0
        })
        self.assertEqual(result1["play_count"], 1)

        # Получение статистики через IPC
        result2 = tracker.handle_get_playback_stats({"item_id": "ipc_test"})
        self.assertEqual(result2["play_count"], 1)
        self.assertAlmostEqual(result2["total_listened_sec"], 10.0)

        # Запись ещё раз
        result3 = tracker.handle_record_playback({
            "item_id": "ipc_test",
            "duration_listened_sec": 5.0
        })
        self.assertEqual(result3["play_count"], 2)

        # Проверяем топ-воспроизводимые
        result4 = tracker.handle_get_most_replayed({"limit": 10})
        self.assertGreater(result4["count"], 0)
        self.assertEqual(result4["items"][0]["item_id"], "ipc_test")


# ===========================================================================
# 10. API-naming and get_stats coverage
# ===========================================================================

class TestAPIContract(unittest.TestCase):
    """Verify public method names and return-value contract match the spec."""

    def setUp(self):
        self.tracker = PlaybackTracker()

    def test_record_playback_method_exists(self):
        """Public API method record_playback must exist."""
        self.assertTrue(callable(getattr(self.tracker, "record_playback", None)))

    def test_get_playback_stats_returns_required_keys(self):
        """get_playback_stats must return play_count, total_listened_sec, last_played."""
        self.tracker.record_playback("key_test", duration_listened_sec=5.0)
        stats = self.tracker.get_playback_stats("key_test")
        for key in ("play_count", "total_listened_sec", "last_played", "item_id"):
            self.assertIn(key, stats, f"Missing key: {key}")

    def test_play_count_type_is_int(self):
        self.tracker.record_playback("type_test", duration_listened_sec=1.0)
        stats = self.tracker.get_playback_stats("type_test")
        self.assertIsInstance(stats["play_count"], int)

    def test_total_listened_sec_type_is_float(self):
        self.tracker.record_playback("type_test2", duration_listened_sec=1.0)
        stats = self.tracker.get_playback_stats("type_test2")
        self.assertIsInstance(stats["total_listened_sec"], float)

    def test_last_played_is_iso8601(self):
        self.tracker.record_playback("ts_test", duration_listened_sec=1.0)
        last = self.tracker.get_playback_stats("ts_test")["last_played"]
        self.assertIsNotNone(last)
        # Must be parseable as ISO8601
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        self.assertIsNotNone(dt)


class TestMultipleItemsIndependence(unittest.TestCase):
    """Multiple items must be tracked completely independently."""

    def test_ten_items_all_tracked_separately(self):
        tracker = PlaybackTracker()
        items = [f"item_{i}" for i in range(10)]
        for i, iid in enumerate(items):
            for _ in range(i + 1):
                tracker.record_playback(iid, duration_listened_sec=float(i))
        for i, iid in enumerate(items):
            stats = tracker.get_playback_stats(iid)
            self.assertEqual(stats["play_count"], i + 1)

    def test_recording_one_does_not_affect_another(self):
        tracker = PlaybackTracker()
        tracker.record_playback("alpha", duration_listened_sec=10.0)
        tracker.record_playback("alpha", duration_listened_sec=10.0)
        tracker.record_playback("beta", duration_listened_sec=5.0)

        alpha = tracker.get_playback_stats("alpha")
        beta = tracker.get_playback_stats("beta")
        gamma = tracker.get_playback_stats("gamma")

        self.assertEqual(alpha["play_count"], 2)
        self.assertEqual(beta["play_count"], 1)
        self.assertEqual(gamma["play_count"], 0)

    def test_disk_persistence_multiple_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            t1 = PlaybackTracker(data_dir=tmpdir)
            for i in range(5):
                t1.record_playback(f"item_{i}", duration_listened_sec=float(i * 2))

            t2 = PlaybackTracker(data_dir=tmpdir)
            for i in range(5):
                stats = t2.get_playback_stats(f"item_{i}")
                self.assertEqual(stats["play_count"], 1)
                self.assertAlmostEqual(stats["total_listened_sec"], float(i * 2))


# ===========================================================================
# Wave 133 explicitly-named tests matching task spec
# ===========================================================================

class TestPlaybackTrackerWave133(unittest.TestCase):
    """Named tests matching wave133 task spec."""

    def test_record_play_event(self):
        """record_playback() must register exactly one play event."""
        tracker = PlaybackTracker()
        tracker.record_playback("w133_item", duration_listened_sec=5.0)
        stats = tracker.get_playback_stats("w133_item")
        self.assertEqual(stats["play_count"], 1)
        self.assertIsNotNone(stats["last_played"])

    def test_aggregate_count_per_item(self):
        """Multiple play events for the same item accumulate play_count."""
        tracker = PlaybackTracker()
        for _ in range(7):
            tracker.record_playback("w133_multi", duration_listened_sec=1.0)
        stats = tracker.get_playback_stats("w133_multi")
        self.assertEqual(stats["play_count"], 7)

    def test_total_listened_seconds(self):
        """total_listened_sec must be the sum of all duration_listened_sec values."""
        tracker = PlaybackTracker()
        durations = [3.5, 7.0, 12.25]
        for d in durations:
            tracker.record_playback("w133_dur", duration_listened_sec=d)
        stats = tracker.get_playback_stats("w133_dur")
        self.assertAlmostEqual(stats["total_listened_sec"], sum(durations), places=4)

    def test_persist_reload(self):
        """Stats written to disk must survive a tracker reload."""
        with tempfile.TemporaryDirectory() as tmpdir:
            t1 = PlaybackTracker(data_dir=tmpdir)
            t1.record_playback("persist_me", duration_listened_sec=9.9)
            t1.record_playback("persist_me", duration_listened_sec=0.1)

            t2 = PlaybackTracker(data_dir=tmpdir)
            stats = t2.get_playback_stats("persist_me")
            self.assertEqual(stats["play_count"], 2)
            self.assertAlmostEqual(stats["total_listened_sec"], 10.0, places=4)

    def test_unicode_item_id(self):
        """Unicode item IDs (Cyrillic etc.) must be stored and retrieved correctly."""
        tracker = PlaybackTracker()
        uid = "запись_тест_юникод"
        tracker.record_playback(uid, duration_listened_sec=4.0)
        stats = tracker.get_playback_stats(uid)
        self.assertEqual(stats["play_count"], 1)
        self.assertEqual(stats["item_id"], uid)

    def test_concurrent_record(self):
        """Concurrent record_playback() calls must not lose counts."""
        tracker = PlaybackTracker()
        n_threads = 20
        n_per_thread = 10

        def worker():
            for _ in range(n_per_thread):
                tracker.record_playback("concurrent_w133", duration_listened_sec=0.5)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = tracker.get_playback_stats("concurrent_w133")
        expected = n_threads * n_per_thread
        self.assertEqual(stats["play_count"], expected)
        self.assertAlmostEqual(
            stats["total_listened_sec"], expected * 0.5, places=2
        )

    def test_handles_corrupted_storage(self):
        """Corrupted playback_stats.json must not crash init or subsequent ops."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "playback_stats.json"
            path.write_text("[ NOT VALID { JSON }}", encoding="utf-8")
            tracker = PlaybackTracker(data_dir=tmpdir)
            # Should start with empty stats
            self.assertEqual(tracker.get_playback_stats("any")["play_count"], 0)
            # Should be able to write new entries after corruption recovery
            tracker.record_playback("recovery_item", duration_listened_sec=3.0)
            stats = tracker.get_playback_stats("recovery_item")
            self.assertEqual(stats["play_count"], 1)


if __name__ == "__main__":
    unittest.main()
