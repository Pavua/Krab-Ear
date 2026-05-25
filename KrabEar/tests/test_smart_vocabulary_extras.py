"""test_smart_vocabulary_extras.py — глубокие edge-case тесты SmartVocabularyBuilder.

Wave 208 extras: min_word_length, punctuation, numeric strings, case normalisation,
large history, concurrency, unicode mix, corrupted entries.
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from typing import List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.smart_vocabulary import SmartVocabularyBuilder, VocabularyUpdate  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(text: str, source_text: str = "", confidence: float = 1.0) -> dict:
    return {"text": text, "source_text": source_text, "confidence": confidence}


def _repeat_item(text: str, n: int, confidence: float = 1.0) -> List[dict]:
    """Return n identical items to push a word above min_frequency."""
    return [_item(text, confidence=confidence) for _ in range(n)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMinWordLength5(unittest.TestCase):
    """min_word_length=5 должен отсеивать слова короче 5 символов."""

    def setUp(self):
        self.builder = SmartVocabularyBuilder(min_word_length=5)

    def test_short_words_excluded(self):
        # "cat" (3 chars) and "run" (3 chars) must not appear in results
        items = _repeat_item("cat run superlongword superlongword superlongword", n=5)
        result = self.builder.build_vocabulary(items, min_frequency=3)
        words_lower = [w.lower() for w in result.new_words]
        self.assertNotIn("cat", words_lower)
        self.assertNotIn("run", words_lower)

    def test_long_words_included_if_frequent(self):
        # "Kubernetes" (10 chars) appears 5 times — should be a candidate
        items = _repeat_item("Kubernetes cluster Kubernetes Kubernetes Kubernetes Kubernetes", n=5)
        result = self.builder.build_vocabulary(items, min_frequency=3)
        words_lower = [w.lower() for w in result.new_words]
        self.assertIn("kubernetes", words_lower)

    def test_boundary_exactly_5_chars(self):
        # "alpha" = 5 chars exactly; should not be excluded by length guard
        items = _repeat_item("alpha alpha alpha alpha alpha", n=1)
        result = self.builder.build_vocabulary(items, min_frequency=3)
        # alpha may or may not appear (stop-word check), but must not crash
        self.assertIsInstance(result, VocabularyUpdate)


class TestPunctuationAroundWords(unittest.TestCase):
    """Пунктуация вокруг слов не должна включаться в извлечённые термины."""

    def setUp(self):
        self.builder = SmartVocabularyBuilder(min_word_length=3)

    def test_comma_stripped(self):
        items = _repeat_item("телефон, телефон, телефон", n=5)
        result = self.builder.build_vocabulary(items, min_frequency=3)
        for w in result.new_words:
            self.assertNotIn(",", w, f"Запятая в слове: {w!r}")

    def test_period_stripped(self):
        items = _repeat_item("сервер. сервер. сервер.", n=5)
        result = self.builder.build_vocabulary(items, min_frequency=3)
        for w in result.new_words:
            self.assertNotIn(".", w, f"Точка в слове: {w!r}")

    def test_parentheses_stripped(self):
        items = _repeat_item("(микрофон) (микрофон) (микрофон)", n=5)
        result = self.builder.build_vocabulary(items, min_frequency=3)
        for w in result.new_words:
            self.assertNotIn("(", w)
            self.assertNotIn(")", w)


class TestExcludeNumericStrings(unittest.TestCase):
    """Чисто числовые строки не должны попадать в словарь."""

    def setUp(self):
        self.builder = SmartVocabularyBuilder(min_word_length=3)

    def test_pure_digits_excluded(self):
        items = _repeat_item("123 456 789 тест тест тест", n=5)
        result = self.builder.build_vocabulary(items, min_frequency=3)
        for w in result.new_words:
            self.assertFalse(w.isdigit(), f"Число попало в словарь: {w!r}")

    def test_float_string_excluded(self):
        items = _repeat_item("3.14 3.14 3.14 прокси прокси прокси", n=5)
        result = self.builder.build_vocabulary(items, min_frequency=3)
        for w in result.new_words:
            self.assertNotEqual(w, "3.14", "Плавающее число попало в словарь")

    def test_year_digits_excluded(self):
        items = _repeat_item("2024 2024 2024 дата дата дата", n=5)
        result = self.builder.build_vocabulary(items, min_frequency=3)
        for w in result.new_words:
            self.assertFalse(w.isdigit(), f"Год попал в словарь: {w!r}")


class TestCaseNormalisation(unittest.TestCase):
    """Одно слово в разных регистрах должно стать одной записью в словаре."""

    def setUp(self):
        self.builder = SmartVocabularyBuilder(min_word_length=3)

    def test_same_word_different_case_deduped(self):
        # "кластер" / "Кластер" / "КЛАСТЕР" — должна быть одна запись
        items = _repeat_item(
            "кластер Кластер КЛАСТЕР кластер Кластер", n=5
        )
        result = self.builder.build_vocabulary(items, min_frequency=3)
        matching = [w for w in result.new_words if w.lower() == "кластер"]
        self.assertLessEqual(len(matching), 1, "Дублирование по регистру")

    def test_output_words_no_duplicate_lower(self):
        items = _repeat_item("Server server SERVER server Server", n=5)
        result = self.builder.build_vocabulary(items, min_frequency=3)
        lowers = [w.lower() for w in result.new_words]
        self.assertEqual(len(lowers), len(set(lowers)), "Дубликаты в new_words")


class TestLargeHistory1000Items(unittest.TestCase):
    """Обработка 1000 записей не должна падать и давать разумный результат."""

    def setUp(self):
        self.builder = SmartVocabularyBuilder(min_word_length=3)

    def test_no_crash_1000_items(self):
        items = []
        for i in range(1000):
            items.append(_item(f"запись номер {i} тестовое слово кластер"))
        result = self.builder.build_vocabulary(items, min_frequency=3)
        self.assertIsInstance(result, VocabularyUpdate)
        self.assertGreaterEqual(result.total, 0)

    def test_frequent_word_appears_1000_items(self):
        # "кластер" appears in every item — must end up in vocabulary
        items = [_item("кластер сервер данные") for _ in range(1000)]
        result = self.builder.build_vocabulary(items, min_frequency=5)
        words_lower = [w.lower() for w in result.new_words]
        self.assertIn("кластер", words_lower)


class TestConcurrentThreadSafe(unittest.TestCase):
    """20 параллельных потоков одновременно вызывают build_vocabulary — нет гонок."""

    def setUp(self):
        self.builder = SmartVocabularyBuilder(min_word_length=3)

    def test_20_threads_no_exception(self):
        items = _repeat_item("кластер сервер данные тестирование", n=10)
        errors: List[Exception] = []
        results: List[VocabularyUpdate] = []

        def run():
            try:
                r = self.builder.build_vocabulary(items, min_frequency=3)
                results.append(r)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"Ошибки в потоках: {errors}")
        self.assertEqual(len(results), 20)

    def test_results_consistent_across_threads(self):
        """All threads should return the same set of new_words."""
        items = _repeat_item("телефон данные сервер тестирование", n=10)
        results: List[VocabularyUpdate] = []

        def run():
            r = self.builder.build_vocabulary(items, min_frequency=3)
            results.append(frozenset(w.lower() for w in r.new_words))

        threads = [threading.Thread(target=run) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # All results must be identical
        self.assertEqual(len(set(results)), 1, "Непоследовательные результаты между потоками")


class TestUnicodeRuEsEnMix(unittest.TestCase):
    """Смешанный RU/ES/EN текст должен корректно обрабатываться."""

    def setUp(self):
        self.builder = SmartVocabularyBuilder(min_word_length=3)

    def test_mixed_ru_en_items(self):
        items = (
            _repeat_item("Python кластер Python кластер Python кластер", n=5) +
            _repeat_item("машинное обучение машинное обучение машинное", n=5)
        )
        result = self.builder.build_vocabulary(items, min_frequency=3)
        self.assertIsInstance(result, VocabularyUpdate)
        # Should not crash on mixed scripts
        for w in result.new_words:
            self.assertIsInstance(w, str)

    def test_spanish_text_processed(self):
        items = _repeat_item(
            "Configuración servidor Configuración servidor Configuración servidor", n=5
        )
        result = self.builder.build_vocabulary(items, min_frequency=3)
        self.assertIsInstance(result, VocabularyUpdate)

    def test_no_non_printable_chars_in_output(self):
        items = _repeat_item("тест\x00данные\x1fсервер тест данные сервер", n=5)
        result = self.builder.build_vocabulary(items, min_frequency=3)
        for w in result.new_words:
            for ch in w:
                self.assertGreaterEqual(ord(ch), 32, f"Непечатный символ в слове: {w!r}")

    def test_cyrillic_words_preserved(self):
        items = _repeat_item("микрофон транскрипция диаризация микрофон транскрипция диаризация", n=5)
        result = self.builder.build_vocabulary(items, min_frequency=3)
        words_lower = [w.lower() for w in result.new_words]
        # At least one of the RU-specific terms should survive
        found = any(w in words_lower for w in ["микрофон", "транскрипция", "диаризация"])
        self.assertTrue(found, f"Ни одно кириллическое слово не выжило: {words_lower}")


class TestCorruptedHistoryEntryGraceful(unittest.TestCase):
    """Повреждённые/неожиданные записи истории не должны вызывать исключений."""

    def setUp(self):
        self.builder = SmartVocabularyBuilder(min_word_length=3)

    def test_none_text_field(self):
        items = [{"text": None, "source_text": None, "confidence": 0.9}]
        items += _repeat_item("нормальный текст нормальный", n=5)
        result = self.builder.build_vocabulary(items, min_frequency=3)
        self.assertIsInstance(result, VocabularyUpdate)

    def test_missing_text_field(self):
        items = [{"confidence": 0.9}]  # no 'text' or 'source_text'
        items += _repeat_item("тест тест тест тест", n=5)
        result = self.builder.build_vocabulary(items, min_frequency=3)
        self.assertIsInstance(result, VocabularyUpdate)

    def test_empty_string_text(self):
        items = [{"text": "", "source_text": "", "confidence": 0.5}] * 10
        items += _repeat_item("кластер кластер кластер", n=5)
        result = self.builder.build_vocabulary(items, min_frequency=3)
        self.assertIsInstance(result, VocabularyUpdate)

    def test_non_string_text_field(self):
        items = [{"text": 12345, "source_text": ["не", "строка"], "confidence": 0.9}]
        items += _repeat_item("данные данные данные", n=5)
        result = self.builder.build_vocabulary(items, min_frequency=3)
        self.assertIsInstance(result, VocabularyUpdate)

    def test_nan_confidence_handled(self):
        items = [{"text": "тест данные кластер", "confidence": float("nan")}] * 5
        result = self.builder.build_vocabulary(items, min_frequency=3)
        self.assertIsInstance(result, VocabularyUpdate)

    def test_very_long_text_no_crash(self):
        long_text = "кластер " * 10000
        items = [{"text": long_text, "confidence": 0.9}]
        result = self.builder.build_vocabulary(items, min_frequency=3)
        self.assertIsInstance(result, VocabularyUpdate)

    def test_all_corrupted_returns_empty(self):
        items = [
            {"text": None, "confidence": 0.9},
            {"text": "", "confidence": 0.5},
            {"confidence": 0.8},
        ]
        result = self.builder.build_vocabulary(items, min_frequency=3)
        self.assertIsInstance(result, VocabularyUpdate)
        self.assertEqual(result.new_words, [])


if __name__ == "__main__":
    unittest.main()
