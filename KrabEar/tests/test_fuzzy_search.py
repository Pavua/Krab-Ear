"""Тесты FuzzySearcher и HistoryService.handle_fuzzy_search."""

from __future__ import annotations
from backend.state_store import StateStore
from backend.history_service import HistoryService
from core.fuzzy_search import FuzzyMatch, FuzzySearcher

import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.timing_budgets import REDOS_BUDGET_SEC  # noqa: E402


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


class FuzzySearcherAdditionalTests(unittest.TestCase):
    """Дополнительные тесты FuzzySearcher: граничные случаи и свойства."""

    def setUp(self) -> None:
        self.searcher = FuzzySearcher()

    # ------------------------------------------------------------------
    # 11. Нет совпадений выше порога → пустой список
    # ------------------------------------------------------------------
    def test_no_matches_returns_empty(self) -> None:
        texts = ["completely unrelated", "nothing here at all"]
        results = self.searcher.search("zxqwerty", texts, threshold=0.9)
        self.assertEqual(results, [])

    # ------------------------------------------------------------------
    # 12. Все score в диапазоне [0.0, 1.0]
    # ------------------------------------------------------------------
    def test_scores_in_valid_range(self) -> None:
        texts = ["hello world", "hello", "world", "test text", "foo bar baz"]
        results = self.searcher.search("hello world", texts, threshold=0.0)
        for m in results:
            self.assertGreaterEqual(m.score, 0.0,
                                    f"score {m.score} < 0 for {m.matched_text!r}")
            self.assertLessEqual(m.score, 1.0,
                                 f"score {m.score} > 1 for {m.matched_text!r}")

    # ------------------------------------------------------------------
    # 13. Пустые строки в списке пропускаются (не вызывают ошибку)
    # ------------------------------------------------------------------
    def test_empty_strings_in_texts_skipped(self) -> None:
        texts = ["", "", "hello world", ""]
        results = self.searcher.search("hello world", texts, threshold=0.9)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].matched_text, "hello world")

    # ------------------------------------------------------------------
    # 14. Испанский текст
    # ------------------------------------------------------------------
    def test_spanish_text_search(self) -> None:
        texts = [
            "El niño juega en el parque",
            "La casa es muy bonita hoy",
            "completamente diferente",
        ]
        results = self.searcher.search("niño juega", texts, threshold=0.5)
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0].index, 0)

    def test_spanish_typo_tolerance(self) -> None:
        texts = ["fantástico trabajo", "otro texto aquí"]
        results = self.searcher.search("fantastico trabajo", texts, threshold=0.7)
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0].index, 0)

    # ------------------------------------------------------------------
    # 15. index корректно соответствует позиции в исходном списке
    # ------------------------------------------------------------------
    def test_index_matches_original_position(self) -> None:
        texts = ["alpha beta", "gamma delta", "hello world", "omega psi"]
        results = self.searcher.search("hello world", texts, threshold=0.9)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].index, 2)

    # ------------------------------------------------------------------
    # 16. matched_text совпадает с оригиналом (без lower)
    # ------------------------------------------------------------------
    def test_matched_text_preserves_case(self) -> None:
        texts = ["Hello World"]
        results = self.searcher.search("hello world", texts, threshold=0.9)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].matched_text, "Hello World")

    # ------------------------------------------------------------------
    # 17. threshold=0.0 возвращает все непустые тексты
    # ------------------------------------------------------------------
    def test_threshold_zero_returns_all_nonempty(self) -> None:
        texts = ["one", "two three", "completely different zxqwerty"]
        # min_text_len = max(1, len("abc") // 3) = 1 → все тексты проходят
        results = self.searcher.search("abc", texts, threshold=0.0)
        # Все непустые тексты с len >= min_text_len должны вернуться
        self.assertGreater(len(results), 0)

    # ------------------------------------------------------------------
    # 18. Запрос длиннее текста — _partial_ratio не падает
    # ------------------------------------------------------------------
    def test_query_longer_than_text(self) -> None:
        texts = ["hi"]
        # query «hello world» длиннее «hi» — не должно упасть
        results = self.searcher.search("hello world", texts, threshold=0.5)
        # Результат может быть пустым, но без исключений
        self.assertIsInstance(results, list)

    # ------------------------------------------------------------------
    # 19. Один текст совпадает точно — остальные ниже порога
    # ------------------------------------------------------------------
    def test_only_one_exact_match_above_threshold(self) -> None:
        texts = [
            "The quick brown fox",
            "completely unrelated stuff",
            "another irrelevant entry",
        ]
        results = self.searcher.search(
            "The quick brown fox", texts, threshold=0.95
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].index, 0)
        self.assertAlmostEqual(results[0].score, 1.0, places=2)

    # ------------------------------------------------------------------
    # 20. Поиск по одному тексту совпадающему частично
    # ------------------------------------------------------------------
    def test_partial_score_gt_full_score_edge(self) -> None:
        # query «fox» (3 символа) — слишком короткое, min_text_len = max(1, 1) = 1
        texts = ["The quick brown fox jumps"]
        results = self.searcher.search("fox", texts, threshold=0.4)
        # Должен найти, т.к. "fox" — точная подстрока
        self.assertTrue(len(results) >= 1)


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

    # ------------------------------------------------------------------
    # W1007: privacy_mode guard — fuzzy_search returns empty in privacy mode
    # ------------------------------------------------------------------
    def test_fuzzy_search_empty_in_privacy_mode(self) -> None:
        """handle_fuzzy_search возвращает пустой results при privacy_mode_enabled=True (W1003 F3)."""
        # Включаем режим конфиденциальности через store settings
        self.store.save_settings({"privacy_mode_enabled": True})
        result = self.svc.handle_fuzzy_search({"query": "Hello world", "threshold": 0.5})
        # Должен вернуть пустой results и reason без передачи текстов в searcher
        self.assertIn("results", result)
        self.assertEqual(result["results"], [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        # ok=True — не ошибка, просто заблокировано privacy mode
        self.assertTrue(result.get("ok", True))


class FuzzySearcherWave111Tests(unittest.TestCase):
    """Wave 111 required tests по спецификации задачи."""

    def setUp(self) -> None:
        self.searcher = FuzzySearcher()

    # ------------------------------------------------------------------
    # test_exact_match_returns_1_0_score
    # ------------------------------------------------------------------
    def test_exact_match_returns_1_0_score(self) -> None:
        texts = ["transcription result"]
        results = self.searcher.search("transcription result", texts, threshold=0.0)
        self.assertTrue(len(results) >= 1)
        # Точное совпадение должно давать score == 1.0
        self.assertAlmostEqual(results[0].score, 1.0, places=5)

    # ------------------------------------------------------------------
    # test_close_match_high_score (1-2 typo distance)
    # ------------------------------------------------------------------
    def test_close_match_high_score(self) -> None:
        # «transcripion» — пропущена буква 't', расстояние редактирования 1
        texts = ["transcription"]
        results = self.searcher.search("transcripion", texts, threshold=0.7)
        self.assertTrue(len(results) >= 1,
                        "Ожидается высокий score при 1 опечатке")
        self.assertGreaterEqual(results[0].score, 0.7)

    # ------------------------------------------------------------------
    # test_dissimilar_strings_low_score
    # ------------------------------------------------------------------
    def test_dissimilar_strings_low_score(self) -> None:
        texts = ["completely unrelated gibberish xqzwv"]
        results = self.searcher.search("hello world", texts, threshold=0.9)
        # Нет совпадений выше 0.9 — пустой результат
        self.assertEqual(results, [])

    # ------------------------------------------------------------------
    # test_unicode_substrings
    # ------------------------------------------------------------------
    def test_unicode_substrings(self) -> None:
        # Запрос — кириллическая подстрока
        texts = [
            "Голосовая транскрипция в Krab Ear",
            "совершенно другой контент",
        ]
        results = self.searcher.search("транскрипция", texts, threshold=0.5)
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0].index, 0)

    # ------------------------------------------------------------------
    # test_empty_query_handling
    # ------------------------------------------------------------------
    def test_empty_query_handling(self) -> None:
        texts = ["some text here", "another entry"]
        # Пустая строка → сразу возврат []
        self.assertEqual(self.searcher.search("", texts), [])
        # Только пробелы — тоже пустой запрос по длине 0
        # (FuzzySearcher проверяет `if not query`)
        # Строка из пробелов — truthy, но будем проверять граничный случай
        results = self.searcher.search("   ", texts, threshold=0.9)
        # Результат может быть пуст (пробелы не совпадут с реальным текстом)
        self.assertIsInstance(results, list)

    # ------------------------------------------------------------------
    # test_case_insensitive_match
    # ------------------------------------------------------------------
    def test_case_insensitive_match(self) -> None:
        texts = ["Hello World Recording"]
        # Запрос в нижнем регистре — должен совпасть благодаря .lower()
        results = self.searcher.search("hello world recording", texts, threshold=0.9)
        self.assertTrue(len(results) >= 1,
                        "Case-insensitive match должен работать")
        self.assertAlmostEqual(results[0].score, 1.0, places=5)
        # Оригинальный текст сохраняется без изменений
        self.assertEqual(results[0].matched_text, "Hello World Recording")

    # ------------------------------------------------------------------
    # test_concurrent_search
    # ------------------------------------------------------------------
    def test_concurrent_search(self) -> None:
        texts = [f"текст {i} для параллельного поиска" for i in range(100)]
        errors = []

        def worker():
            try:
                for _ in range(30):
                    results = self.searcher.search("параллельного", texts, threshold=0.5)
                    assert isinstance(results, list)
                    assert all(isinstance(m, FuzzyMatch) for m in results)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent FuzzySearch raised: {errors}")


class FuzzySearcherSecurityTests(unittest.TestCase):
    """Regression tests for security hardening (memory-bomb + ReDoS guards)."""

    def setUp(self) -> None:
        self.searcher = FuzzySearcher()

    # ------------------------------------------------------------------
    # Memory-bomb guard: only last MAX_TEXTS (5000) texts are processed
    # ------------------------------------------------------------------
    def test_memory_bomb_guard_processes_at_most_5000_texts(self) -> None:
        """search() must not process more than 5000 texts even for larger inputs."""
        # Build 6000 texts; only the last 5000 should be processed.
        # The first 1000 entries are unique sentinel strings that should be skipped.
        texts = [f"sentinel_entry_{i}" for i in range(1000)]
        texts += [f"normal text entry {i}" for i in range(5000)]
        results = self.searcher.search("normal text entry", texts, threshold=0.7)
        # Sentinels (indices 0-999) must never appear — they are beyond the 5000 cap
        sentinel_indices = {r.index for r in results if r.index < 1000}
        self.assertEqual(
            sentinel_indices, set(),
            "Memory-bomb guard failed: sentinel entries before the 5000 cap were processed"
        )

    def test_memory_bomb_guard_total_results_bounded(self) -> None:
        """With 6000 identical texts, processed count <= 5000."""
        texts = ["hello world"] * 6000
        results = self.searcher.search("hello world", texts, threshold=0.9)
        # At most 5000 texts processed, so at most 5000 results
        self.assertLessEqual(len(results), 5000)

    # ------------------------------------------------------------------
    # ReDoS guard: query and text are clamped to 2000 chars before SequenceMatcher
    # ------------------------------------------------------------------
    def test_redos_guard_very_long_query_does_not_hang(self) -> None:
        """A query longer than 2000 chars must complete quickly (no O(N^2) hang)."""
        import time
        long_query = "a" * 10_000
        texts = ["a" * 10_000, "short text", "hello world"]
        t0 = time.monotonic()
        results = self.searcher.search(long_query, texts, threshold=0.0)
        elapsed = time.monotonic() - t0
        # Must complete in <2 s even on slow CI hardware
        self.assertLess(elapsed, REDOS_BUDGET_SEC, f"ReDoS guard failed: search took {elapsed:.3f}s")
        self.assertIsInstance(results, list)

    def test_redos_guard_very_long_text_does_not_hang(self) -> None:
        """Texts longer than 2000 chars are truncated internally; no hang."""
        import time
        texts = ["b" * 50_000]
        t0 = time.monotonic()
        results = self.searcher.search("b" * 50, texts, threshold=0.0)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, REDOS_BUDGET_SEC, f"ReDoS guard (long text) took {elapsed:.3f}s")
        self.assertIsInstance(results, list)


if __name__ == "__main__":
    unittest.main()
