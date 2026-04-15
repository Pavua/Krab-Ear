"""Тесты SearchHistoryManager — история поисковых запросов Krab Ear."""

from __future__ import annotations
from backend.search_history import SearchHistoryManager

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


class TestSearchHistoryBasic(unittest.TestCase):
    """Базовые операции без персистентности (data_dir=None)."""

    def setUp(self):
        self.mgr = SearchHistoryManager()  # in-memory

    def test_record_and_get_recent(self):
        self.mgr.record_search("привет мир", results_count=5)
        recent = self.mgr.get_recent_searches(limit=10)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["query"], "привет мир")
        self.assertEqual(recent[0]["results_count"], 5)
        self.assertIn("ts", recent[0])

    def test_get_recent_empty(self):
        recent = self.mgr.get_recent_searches()
        self.assertEqual(recent, [])

    def test_get_recent_order_newest_first(self):
        self.mgr.record_search("first")
        self.mgr.record_search("second")
        self.mgr.record_search("third")
        recent = self.mgr.get_recent_searches(limit=10)
        self.assertEqual(recent[0]["query"], "third")
        self.assertEqual(recent[1]["query"], "second")
        self.assertEqual(recent[2]["query"], "first")

    def test_get_recent_limit_respected(self):
        for i in range(10):
            self.mgr.record_search(f"query {i}")
        recent = self.mgr.get_recent_searches(limit=3)
        self.assertEqual(len(recent), 3)

    def test_empty_query_ignored(self):
        self.mgr.record_search("")
        self.mgr.record_search("   ")
        self.assertEqual(self.mgr.get_recent_searches(), [])

    def test_query_stripped(self):
        self.mgr.record_search("  hello  ")
        recent = self.mgr.get_recent_searches()
        self.assertEqual(recent[0]["query"], "hello")

    def test_get_popular_searches(self):
        for _ in range(3):
            self.mgr.record_search("популярный")
        for _ in range(1):
            self.mgr.record_search("редкий")
        popular = self.mgr.get_popular_searches(limit=10)
        self.assertEqual(popular[0]["query"], "популярный")
        self.assertEqual(popular[0]["count"], 3)
        self.assertEqual(popular[1]["query"], "редкий")
        self.assertEqual(popular[1]["count"], 1)

    def test_get_popular_empty(self):
        popular = self.mgr.get_popular_searches()
        self.assertEqual(popular, [])

    def test_get_popular_limit_respected(self):
        for q in ["a", "b", "c", "d", "e"]:
            self.mgr.record_search(q)
        popular = self.mgr.get_popular_searches(limit=2)
        self.assertEqual(len(popular), 2)

    def test_clear_search_history(self):
        self.mgr.record_search("один")
        self.mgr.record_search("два")
        self.mgr.clear_search_history()
        self.assertEqual(self.mgr.get_recent_searches(), [])
        self.assertEqual(self.mgr.get_popular_searches(), [])

    def test_results_count_default_zero(self):
        self.mgr.record_search("без счётчика")
        entry = self.mgr.get_recent_searches()[0]
        self.assertEqual(entry["results_count"], 0)


class TestSearchHistoryPersistence(unittest.TestCase):
    """Тесты персистентности в файл."""

    def test_persists_to_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SearchHistoryManager(data_dir=tmpdir)
            mgr.record_search("сохранить", results_count=7)

            path = Path(tmpdir) / "search_history.json"
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("entries", data)
            self.assertEqual(len(data["entries"]), 1)
            self.assertEqual(data["entries"][0]["query"], "сохранить")
            self.assertEqual(data["entries"][0]["results_count"], 7)

    def test_loads_from_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr1 = SearchHistoryManager(data_dir=tmpdir)
            mgr1.record_search("запрос 1")
            mgr1.record_search("запрос 2")

            # Создаём новый экземпляр — данные загружаются из файла
            mgr2 = SearchHistoryManager(data_dir=tmpdir)
            recent = mgr2.get_recent_searches()
            queries = [e["query"] for e in recent]
            self.assertIn("запрос 1", queries)
            self.assertIn("запрос 2", queries)

    def test_clear_removes_from_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SearchHistoryManager(data_dir=tmpdir)
            mgr.record_search("временный")
            mgr.clear_search_history()

            # Перезагружаем — файл должен быть пустым
            mgr2 = SearchHistoryManager(data_dir=tmpdir)
            self.assertEqual(mgr2.get_recent_searches(), [])

    def test_max_entries_trimming(self):
        """Проверяет обрезку до 500 записей."""
        from backend.search_history import _MAX_ENTRIES
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SearchHistoryManager(data_dir=tmpdir)
            # Добавляем больше записей, чем максимум
            for i in range(_MAX_ENTRIES + 10):
                mgr._entries.append({
                    "query": f"query {i}",
                    "results_count": 0,
                    "ts": "2026-01-01T00:00:00+00:00",
                })
            # Запись одного нового — должна срабатывать обрезка
            mgr.record_search("новый после лимита")
            self.assertLessEqual(len(mgr._entries), _MAX_ENTRIES)

    def test_graceful_on_corrupted_file(self):
        """Повреждённый файл не должен вызывать исключение."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "search_history.json"
            path.write_text("INVALID JSON{{", encoding="utf-8")
            # Не должен бросать исключение
            mgr = SearchHistoryManager(data_dir=tmpdir)
            self.assertEqual(mgr.get_recent_searches(), [])


class TestSearchHistoryIPC(unittest.TestCase):
    """Тесты IPC-обработчиков."""

    def setUp(self):
        self.mgr = SearchHistoryManager()

    def test_handle_get_recent_searches(self):
        self.mgr.record_search("ipc test", results_count=3)
        result = self.mgr.handle_get_recent_searches({"limit": 10})
        self.assertIn("searches", result)
        self.assertEqual(result["searches"][0]["query"], "ipc test")

    def test_handle_get_popular_searches(self):
        for _ in range(4):
            self.mgr.record_search("popular ipc")
        result = self.mgr.handle_get_popular_searches({"limit": 5})
        self.assertIn("searches", result)
        self.assertEqual(result["searches"][0]["query"], "popular ipc")
        self.assertEqual(result["searches"][0]["count"], 4)

    def test_handle_clear_search_history(self):
        self.mgr.record_search("to be cleared")
        result = self.mgr.handle_clear_search_history({})
        self.assertTrue(result.get("ok"))
        self.assertEqual(self.mgr.get_recent_searches(), [])

    def test_handle_get_recent_default_limit(self):
        """Без параметра limit используется дефолтное значение 20."""
        for i in range(25):
            self.mgr.record_search(f"q{i}")
        result = self.mgr.handle_get_recent_searches({})
        self.assertLessEqual(len(result["searches"]), 20)

    def test_handle_get_popular_default_limit(self):
        """Без параметра limit используется дефолтное значение 10."""
        for q in [f"term{i}" for i in range(15)]:
            self.mgr.record_search(q)
        result = self.mgr.handle_get_popular_searches({})
        self.assertLessEqual(len(result["searches"]), 10)


class TestSearchHistoryThreadSafety(unittest.TestCase):
    """Проверка потокобезопасности."""

    def test_concurrent_record_search(self):
        mgr = SearchHistoryManager()
        errors: list[Exception] = []

        def worker(idx: int) -> None:
            try:
                for j in range(10):
                    mgr.record_search(f"thread{idx} query{j}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Ошибки в потоках: {errors}")
        # 5 потоков × 10 запросов = 50
        self.assertEqual(len(mgr.get_recent_searches(limit=100)), 50)


if __name__ == "__main__":
    unittest.main()
