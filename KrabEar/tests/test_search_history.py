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


class TestSearchHistoryWave146(unittest.TestCase):
    """Wave 146 — required named tests."""

    # ------------------------------------------------------------------
    # test_record_query
    # ------------------------------------------------------------------
    def test_record_query(self):
        """record_search() сохраняет запрос с корректными полями."""
        mgr = SearchHistoryManager()
        mgr.record_search("тест запрос", results_count=42)
        recent = mgr.get_recent_searches(limit=1)
        self.assertEqual(len(recent), 1)
        entry = recent[0]
        self.assertEqual(entry["query"], "тест запрос")
        self.assertEqual(entry["results_count"], 42)
        self.assertIn("ts", entry)

    # ------------------------------------------------------------------
    # test_list_recent_queries
    # ------------------------------------------------------------------
    def test_list_recent_queries(self):
        """get_recent_searches() возвращает записи от новых к старым."""
        mgr = SearchHistoryManager()
        for i in range(5):
            mgr.record_search(f"query {i}")
        recent = mgr.get_recent_searches(limit=10)
        self.assertEqual(len(recent), 5)
        self.assertEqual(recent[0]["query"], "query 4")
        self.assertEqual(recent[-1]["query"], "query 0")

    # ------------------------------------------------------------------
    # test_max_history_capped
    # ------------------------------------------------------------------
    def test_max_history_capped(self):
        """История не превышает _MAX_ENTRIES записей."""
        from backend.search_history import _MAX_ENTRIES
        mgr = SearchHistoryManager()
        # Напрямую набиваем entries сверх лимита
        for i in range(_MAX_ENTRIES + 50):
            mgr._entries.append({"query": f"q{i}", "results_count": 0, "ts": "2026-01-01T00:00:00+00:00"})
        # record_search срабатывает обрезка
        mgr.record_search("trigger trim")
        self.assertLessEqual(len(mgr._entries), _MAX_ENTRIES)

    # ------------------------------------------------------------------
    # test_unicode_query
    # ------------------------------------------------------------------
    def test_unicode_query(self):
        """Кириллица, арабский, эмодзи не ломают хранение и извлечение."""
        mgr = SearchHistoryManager()
        queries = ["Привет мир", "مرحبا بالعالم", "こんにちは", "🎤🔊"]
        for q in queries:
            mgr.record_search(q)
        recent = mgr.get_recent_searches(limit=10)
        stored_queries = [e["query"] for e in recent]
        for q in queries:
            self.assertIn(q, stored_queries)

    # ------------------------------------------------------------------
    # test_persist_reload
    # ------------------------------------------------------------------
    def test_persist_reload(self):
        """Запись сохраняется в файл и корректно загружается новым экземпляром."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr1 = SearchHistoryManager(data_dir=tmpdir)
            mgr1.record_search("persist me", results_count=99)

            mgr2 = SearchHistoryManager(data_dir=tmpdir)
            recent = mgr2.get_recent_searches()
            queries = [e["query"] for e in recent]
            self.assertIn("persist me", queries)
            match = next(e for e in recent if e["query"] == "persist me")
            self.assertEqual(match["results_count"], 99)

    # ------------------------------------------------------------------
    # test_concurrent_record
    # ------------------------------------------------------------------
    def test_concurrent_record(self):
        """Параллельные вызовы record_search не вызывают ошибок гонки."""
        import threading

        mgr = SearchHistoryManager()
        errors: list[Exception] = []

        def worker(idx: int):
            try:
                for j in range(15):
                    mgr.record_search(f"thread{idx}-q{j}", results_count=j)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Гонки в потоках: {errors}")
        # 6 потоков × 15 запросов = 90
        total = len(mgr.get_recent_searches(limit=200))
        self.assertEqual(total, 90)



class TestSearchHistoryGeminiWaveA(unittest.TestCase):
    """Wave-A Gemini fixes — unbounded growth, injection, atomicity, purge."""

    # ------------------------------------------------------------------
    # 1) query length cap at 1000 chars (injection / OOM guard)
    # ------------------------------------------------------------------
    def test_query_truncated_at_1000_chars(self):
        """record_search truncates queries longer than 1000 chars."""
        mgr = SearchHistoryManager()
        long_query = "x" * 2000
        mgr.record_search(long_query)
        entry = mgr.get_recent_searches(limit=1)[0]
        self.assertEqual(len(entry["query"]), 1000)
        self.assertEqual(entry["query"], "x" * 1000)

    def test_query_exactly_1000_chars_not_truncated(self):
        """A 1000-char query is stored as-is."""
        mgr = SearchHistoryManager()
        exact = "a" * 1000
        mgr.record_search(exact)
        entry = mgr.get_recent_searches(limit=1)[0]
        self.assertEqual(len(entry["query"]), 1000)

    def test_query_injection_chars_stored_safely(self):
        """Shell metacharacters and JSON-special chars survive round-trip."""
        mgr = SearchHistoryManager()
        injection = '"; DROP TABLE entries; -- <script>alert(1)</script>'
        mgr.record_search(injection)
        entry = mgr.get_recent_searches(limit=1)[0]
        self.assertEqual(entry["query"], injection)

    # ------------------------------------------------------------------
    # 2) atomic write via tmp file (power-loss safety)
    # ------------------------------------------------------------------
    def test_atomic_write_no_partial_file(self):
        """_save() writes via .json.tmp then renames; tmp file is not left behind."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SearchHistoryManager(data_dir=tmpdir)
            mgr.record_search("atomicity test")

            tmp_path = mgr._path.with_suffix(".json.tmp")
            self.assertFalse(
                tmp_path.exists(),
                "Temporary .json.tmp file must be renamed away after save",
            )
            self.assertTrue(mgr._path.exists())
            data = json.loads(mgr._path.read_text(encoding="utf-8"))
            self.assertIn("entries", data)

    # ------------------------------------------------------------------
    # 3) clear_search_history deletes the file (purge contract)
    # ------------------------------------------------------------------
    def test_clear_deletes_file_not_rewrites_empty(self):
        """clear_search_history() removes the JSON file entirely."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SearchHistoryManager(data_dir=tmpdir)
            mgr.record_search("будет удалён")
            self.assertTrue(mgr._path.exists())

            mgr.clear_search_history()
            self.assertFalse(
                mgr._path.exists(),
                "clear_search_history() must delete the file",
            )

    def test_clear_no_file_no_error(self):
        """clear_search_history() is safe when no file exists yet."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SearchHistoryManager(data_dir=tmpdir)
            self.assertFalse(mgr._path.exists())
            mgr.clear_search_history()
            self.assertEqual(mgr.get_recent_searches(), [])

    def test_clear_without_data_dir_no_error(self):
        """clear_search_history() with data_dir=None does not raise."""
        mgr = SearchHistoryManager()
        mgr.record_search("один")
        mgr.clear_search_history()
        self.assertEqual(mgr.get_recent_searches(), [])

    # ------------------------------------------------------------------
    # 4) _load() cap: loading an oversized file trims to _MAX_ENTRIES
    # ------------------------------------------------------------------
    def test_load_caps_entries_from_oversized_file(self):
        """_load() applies [-_MAX_ENTRIES:] so an oversized file is capped on read."""
        from backend.search_history import _MAX_ENTRIES
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "search_history.json"
            oversized = [
                {"query": f"q{i}", "results_count": 0, "ts": "2026-01-01T00:00:00+00:00"}
                for i in range(_MAX_ENTRIES + 100)
            ]
            path.write_text(
                json.dumps({"entries": oversized}, ensure_ascii=False),
                encoding="utf-8",
            )
            mgr = SearchHistoryManager(data_dir=tmpdir)
            self.assertEqual(len(mgr._entries), _MAX_ENTRIES)
            self.assertEqual(mgr._entries[-1]["query"], f"q{_MAX_ENTRIES + 99}")


if __name__ == "__main__":
    unittest.main()
