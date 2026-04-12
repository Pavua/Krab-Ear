"""Тесты FuzzySearcher и HistoryService.handle_fuzzy_search."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.fuzzy_search import FuzzyMatch, FuzzySearcher
from backend.history_service import HistoryService
from backend.state_store import StateStore


class FuzzySearcherUnitTests(unittest.TestCase):
    """Юнит-тесты для FuzzySearcher."""

    def setUp(self) -> None:
        self.searcher = FuzzySearcher()

    # ------------------------------------------------------------------
    # 1. Точное совпадение
    # ------------------------------------------------------------------
    def test_exact_match_score_1(self) -> None:
        texts = ["hello world", "foo bar"]
        results = self.searcher.search("hello world", texts, threshold=0.9)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].index, 0)
        self.assertAlmostEqual(results[0].score, 1.0, places=2)

    # ------------------------------------------------------------------
    # 2. Частичное вхождение (query — подстрока текста)
    # ------------------------------------------------------------------
    def test_partial_match_substring(self) -> None:
        texts = ["The quick brown fox jumps over the lazy dog", "unrelated text"]
        results = self.searcher.search("quick brown fox", texts, threshold=0.6)
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0].index, 0)
        self.assertGreaterEqual(results[0].score, 0.6)

    # ------------------------------------------------------------------
    # 3. Допуск к опечатке (одна буква)
    # ------------------------------------------------------------------
    def test_typo_tolerance(self) -> None:
        texts = ["transcription", "other text"]
        results = self.searcher.search("transcriptin", texts, threshold=0.7)  # пропущена 'o'
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0].index, 0)

    # ------------------------------------------------------------------
    # 4. Фильтрация по threshold
    # ------------------------------------------------------------------
    def test_threshold_filtering(self) -> None:
        texts = ["apple", "orange", "banana"]
        # Высокий порог — только очень похожие
        results_high = self.searcher.search("apple", texts, threshold=0.9)
        results_low = self.searcher.search("apple", texts, threshold=0.1)
        self.assertLessEqual(len(results_high), len(results_low))

    # ------------------------------------------------------------------
    # 5. Русский текст
    # ------------------------------------------------------------------
    def test_russian_text(self) -> None:
        texts = ["Привет, как дела?", "Всё хорошо, спасибо", "Добрый день"]
        results = self.searcher.search("Привет", texts, threshold=0.4)
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0].index, 0)

    def test_russian_typo(self) -> None:
        texts = ["транскрипция голоса", "перевод текста"]
        results = self.searcher.search("транскрипция голос", texts, threshold=0.7)
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0].index, 0)

    # ------------------------------------------------------------------
    # 6. Пустой запрос — должен вернуть пустой список
    # ------------------------------------------------------------------
    def test_empty_query_returns_empty(self) -> None:
        texts = ["some text", "another text"]
        results = self.searcher.search("", texts, threshold=0.5)
        self.assertEqual(results, [])

    # ------------------------------------------------------------------
    # 7. Пустой список текстов
    # ------------------------------------------------------------------
    def test_empty_texts_list(self) -> None:
        results = self.searcher.search("query", [], threshold=0.5)
        self.assertEqual(results, [])

    # ------------------------------------------------------------------
    # 8. Сортировка по score (лучший результат первый)
    # ------------------------------------------------------------------
    def test_results_sorted_by_score_desc(self) -> None:
        texts = [
            "completely different text here",  # low score
            "hello world is great",             # medium
            "hello world",                      # high
        ]
        results = self.searcher.search("hello world", texts, threshold=0.3)
        if len(results) >= 2:
            for i in range(len(results) - 1):
                self.assertGreaterEqual(results[i].score, results[i + 1].score)

    # ------------------------------------------------------------------
    # 9. Оптимизация: слишком короткий текст пропускается
    # ------------------------------------------------------------------
    def test_skip_very_short_texts(self) -> None:
        # query длиной 12 символов → min_text_len = 4
        # Текст "ab" (2 символа) должен быть пропущен
        texts = ["ab", "transcription test"]
        results = self.searcher.search("transcription", texts, threshold=0.5)
        indices = [r.index for r in results]
        self.assertNotIn(0, indices)

    # ------------------------------------------------------------------
    # 10. FuzzyMatch dataclass
    # ------------------------------------------------------------------
    def test_fuzzy_match_fields(self) -> None:
        texts = ["hello world"]
        results = self.searcher.search("hello world", texts, threshold=0.9)
        self.assertEqual(len(results), 1)
        m = results[0]
        self.assertIsInstance(m, FuzzyMatch)
        self.assertEqual(m.index, 0)
        self.assertIsInstance(m.score, float)
        self.assertEqual(m.matched_text, "hello world")


class HistoryServiceFuzzySearchTests(unittest.TestCase):
    """Интеграционные тесты handle_fuzzy_search через HistoryService."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

        # Добавляем записи в историю
        self.svc.handle_add_history_item({"text": "Hello world recording", "paste_status": "ok"})
        self.svc.handle_add_history_item({"text": "Привет мир транскрипция", "paste_status": "ok"})
        self.svc.handle_add_history_item({"text": "completely different content", "paste_status": "ok"})

    # ------------------------------------------------------------------
    # 11. Fuzzy search возвращает совпадения
    # ------------------------------------------------------------------
    def test_fuzzy_search_finds_match(self) -> None:
        result = self.svc.handle_fuzzy_search({"query": "Hello world", "threshold": 0.6})
        self.assertIn("matches", result)
        matches = result["matches"]
        self.assertTrue(len(matches) >= 1)
        texts = [m["text"] for m in matches]
        self.assertTrue(any("Hello world" in t for t in texts))

    # ------------------------------------------------------------------
    # 12. Fuzzy search с русским запросом
    # ------------------------------------------------------------------
    def test_fuzzy_search_russian(self) -> None:
        result = self.svc.handle_fuzzy_search({"query": "транскрипция", "threshold": 0.5})
        matches = result["matches"]
        self.assertTrue(len(matches) >= 1)

    # ------------------------------------------------------------------
    # 13. Пустой запрос через IPC
    # ------------------------------------------------------------------
    def test_fuzzy_search_empty_query_ipc(self) -> None:
        result = self.svc.handle_fuzzy_search({"query": "", "threshold": 0.6})
        self.assertEqual(result["matches"], [])

    # ------------------------------------------------------------------
    # 14. Структура ответа
    # ------------------------------------------------------------------
    def test_fuzzy_search_response_structure(self) -> None:
        result = self.svc.handle_fuzzy_search({"query": "Hello", "threshold": 0.4})
        self.assertIn("matches", result)
        for match in result["matches"]:
            self.assertIn("id", match)
            self.assertIn("text", match)
            self.assertIn("score", match)
            self.assertIn("ts", match)
            self.assertIsInstance(match["score"], float)

    # ------------------------------------------------------------------
    # 15. threshold=1.0 — только точные совпадения
    # ------------------------------------------------------------------
    def test_fuzzy_search_threshold_1_no_typo(self) -> None:
        result = self.svc.handle_fuzzy_search({"query": "Helo world", "threshold": 1.0})
        # Опечатка, порог 1.0 — должно вернуть пустой список
        self.assertEqual(result["matches"], [])


if __name__ == "__main__":
    unittest.main()
