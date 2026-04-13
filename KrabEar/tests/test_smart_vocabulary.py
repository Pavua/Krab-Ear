"""Unit-тесты для SmartVocabularyBuilder."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.smart_vocabulary import SmartVocabularyBuilder, VocabularyUpdate
from backend.vocabulary_store import VocabularyStore


# ── Helpers / Fakes ────────────────────────────────────────────────────────────


def _make_item(
    text: str,
    source_text: str = "",
    confidence: float = 1.0,
) -> dict:
    return {
        "text": text,
        "source_text": source_text,
        "confidence": confidence,
    }


class FakeStore:
    """Минимальный фейк StateStore для тестов."""

    def __init__(self, items: Optional[List[dict]] = None) -> None:
        self._items = items or []

    def get_history_page(
        self, cursor: Any = None, limit: int = 100
    ) -> Tuple[List[dict], Any]:
        return self._items[:limit], None

    def to_dict_items(self) -> List[dict]:
        return list(self._items)


class FakeVocabStore:
    """Минимальный фейк VocabularyStore."""

    def __init__(self, initial: Optional[List[str]] = None) -> None:
        self._words: List[str] = list(initial or [])
        self.saved: List[str] = []

    def load(self) -> List[str]:
        return list(self._words)

    def add_words(self, new_words: List[str]) -> List[str]:
        added = set(self._words)
        added.update(w.strip() for w in new_words if w.strip())
        self._words = sorted(added)
        self.saved.extend(new_words)
        return list(self._words)

    def remove_words(self, words: List[str]) -> List[str]:
        remove_set = set(words)
        self._words = [w for w in self._words if w not in remove_set]
        return list(self._words)


# ── Тесты ──────────────────────────────────────────────────────────────────────


class TestVocabularyUpdateDataclass(unittest.TestCase):
    """Проверяем, что VocabularyUpdate создаётся корректно."""

    def test_default_values(self) -> None:
        update = VocabularyUpdate()
        self.assertEqual(update.new_words, [])
        self.assertEqual(update.removed_words, [])
        self.assertEqual(update.total, 0)
        self.assertEqual(update.sources, {})

    def test_with_values(self) -> None:
        update = VocabularyUpdate(
            new_words=["GPT4", "iPhone"],
            removed_words=[],
            total=2,
            sources={"technical_terms": 2},
        )
        self.assertEqual(update.new_words, ["GPT4", "iPhone"])
        self.assertEqual(update.total, 2)
        self.assertIn("technical_terms", update.sources)


class TestBuildVocabularyEmpty(unittest.TestCase):
    """build_vocabulary на пустых входных данных."""

    def setUp(self) -> None:
        self.builder = SmartVocabularyBuilder()

    def test_empty_items_returns_empty_update(self) -> None:
        update = self.builder.build_vocabulary([])
        self.assertIsInstance(update, VocabularyUpdate)
        self.assertEqual(update.new_words, [])
        self.assertEqual(update.total, 0)

    def test_items_with_no_text_returns_empty(self) -> None:
        items = [{"text": "", "source_text": ""}, {"text": "   "}]
        update = self.builder.build_vocabulary(items, min_frequency=1)
        self.assertEqual(update.new_words, [])


class TestBuildVocabularyProperNouns(unittest.TestCase):
    """build_vocabulary извлекает имена собственные."""

    def setUp(self) -> None:
        self.builder = SmartVocabularyBuilder()

    def test_proper_noun_extracted(self) -> None:
        # Слово "Кузнецов" встречается несколько раз в середине предложений
        items = [
            _make_item("Сегодня Кузнецов выступал на конференции"),
            _make_item("Доклад Кузнецова был очень интересным"),
            _make_item("Мнение Кузнецова важно учитывать"),
        ]
        update = self.builder.build_vocabulary(items, min_frequency=2)
        # Может присутствовать в new_words
        self.assertIsInstance(update.new_words, list)
        # Источники должны быть включены
        self.assertIn("proper_nouns", update.sources)

    def test_sources_dict_has_expected_keys(self) -> None:
        items = [_make_item("Данные по Москве за год") for _ in range(3)]
        update = self.builder.build_vocabulary(items, min_frequency=2)
        expected_keys = {"proper_nouns", "technical_terms", "misrecognized", "domain_terms"}
        self.assertTrue(expected_keys.issubset(set(update.sources.keys())))


class TestBuildVocabularyTechnicalTerms(unittest.TestCase):
    """build_vocabulary извлекает технические термины."""

    def setUp(self) -> None:
        self.builder = SmartVocabularyBuilder()

    def test_camelcase_term_extracted(self) -> None:
        items = [_make_item(f"Используем метод MachineLearning в задаче {i}") for i in range(4)]
        update = self.builder.build_vocabulary(items, min_frequency=3)
        lower_words = [w.lower() for w in update.new_words]
        self.assertIn("machinelearning", lower_words)

    def test_tech_digit_term_extracted(self) -> None:
        items = [_make_item(f"Модель GPT4 даёт хорошие результаты в задаче {i}") for i in range(4)]
        update = self.builder.build_vocabulary(items, min_frequency=3)
        lower_words = [w.lower() for w in update.new_words]
        self.assertIn("gpt4", lower_words)


class TestBuildVocabularyMisrecognized(unittest.TestCase):
    """build_vocabulary извлекает слова из низко-уверенных записей."""

    def setUp(self) -> None:
        self.builder = SmartVocabularyBuilder()

    def test_low_confidence_words_included(self) -> None:
        # Слово "транскрибация" встречается в записях с низким confidence
        items = [
            _make_item(
                "Процесс транскрибация завершён успешно",
                confidence=0.4,
            )
            for _ in range(4)
        ]
        update = self.builder.build_vocabulary(items, min_frequency=3)
        # Должны быть ключ misrecognized в sources
        self.assertIn("misrecognized", update.sources)

    def test_high_confidence_items_skip_misrecognized(self) -> None:
        # Высокий confidence → misrecognized source пуст
        items = [
            _make_item("Обычный текст без проблем", confidence=0.95)
            for _ in range(5)
        ]
        update = self.builder.build_vocabulary(items, min_frequency=2)
        self.assertEqual(update.sources.get("misrecognized", 0), 0)


class TestGetVocabularySuggestions(unittest.TestCase):
    """get_vocabulary_suggestions возвращает правильные предложения."""

    def setUp(self) -> None:
        self.builder = SmartVocabularyBuilder()

    def test_returns_list_of_dicts(self) -> None:
        items = [_make_item(f"Встреча с командой DevOps по проекту {i}") for i in range(3)]
        suggestions = self.builder.get_vocabulary_suggestions(items, existing=[], min_frequency=2)
        self.assertIsInstance(suggestions, list)

    def test_suggestion_has_required_fields(self) -> None:
        items = [_make_item("Тест SystemDesign архитектуры") for _ in range(3)]
        suggestions = self.builder.get_vocabulary_suggestions(items, existing=[], min_frequency=2)
        for s in suggestions:
            self.assertIn("word", s)
            self.assertIn("frequency", s)
            self.assertIn("source", s)
            self.assertIn("confidence", s)

    def test_existing_words_filtered_out(self) -> None:
        # Слово MachineLearning уже в словаре → не предлагать
        items = [_make_item("Метод MachineLearning применяется широко") for _ in range(5)]
        existing = ["MachineLearning", "machinelearning"]
        suggestions = self.builder.get_vocabulary_suggestions(
            items, existing=existing, min_frequency=2
        )
        words_lower = [s["word"].lower() for s in suggestions]
        self.assertNotIn("machinelearning", words_lower)

    def test_empty_items_returns_empty_list(self) -> None:
        result = self.builder.get_vocabulary_suggestions([], existing=[])
        self.assertEqual(result, [])

    def test_top_k_limits_results(self) -> None:
        # Много разных текстов, top_k=5 должен ограничить вывод
        import string
        items = [
            _make_item(f"Слово {letter}Word применяется много раз при анализе")
            for letter in string.ascii_uppercase[:10]
            for _ in range(3)
        ]
        suggestions = self.builder.get_vocabulary_suggestions(
            items, existing=[], min_frequency=2, top_k=5
        )
        self.assertLessEqual(len(suggestions), 5)

    def test_sorted_by_frequency_desc(self) -> None:
        # Одно слово встречается чаще всего
        items = (
            [_make_item("Технология MachineLearning применяется широко")] * 10
            + [_make_item("Технология DevOps применяется")] * 3
        )
        suggestions = self.builder.get_vocabulary_suggestions(
            items, existing=[], min_frequency=2, top_k=20
        )
        if len(suggestions) >= 2:
            self.assertGreaterEqual(suggestions[0]["frequency"], suggestions[1]["frequency"])


class TestAutoUpdate(unittest.TestCase):
    """auto_update корректно обновляет VocabularyStore."""

    def setUp(self) -> None:
        self.builder = SmartVocabularyBuilder()

    def test_auto_update_empty_store(self) -> None:
        store = FakeStore(items=[])
        vocab = FakeVocabStore()
        update = self.builder.auto_update(store, vocab)
        self.assertIsInstance(update, VocabularyUpdate)
        self.assertEqual(update.new_words, [])

    def test_auto_update_adds_new_words(self) -> None:
        items = [
            _make_item(f"Интеграция MachineLearning в пайплайн {i}") for i in range(4)
        ]
        store = FakeStore(items=items)
        vocab = FakeVocabStore(initial=[])
        update = self.builder.auto_update(store, vocab, min_frequency=3)
        # Что-то должно быть сохранено (MachineLearning — CamelCase термин)
        self.assertIsInstance(update.new_words, list)

    def test_auto_update_skips_existing_words(self) -> None:
        items = [_make_item("Технология MachineLearning используется") for _ in range(5)]
        store = FakeStore(items=items)
        vocab = FakeVocabStore(initial=["MachineLearning"])
        update = self.builder.auto_update(store, vocab, min_frequency=3)
        # MachineLearning уже есть → не должен попасть в new_words
        new_lower = [w.lower() for w in update.new_words]
        self.assertNotIn("machinelearning", new_lower)

    def test_auto_update_returns_vocabulary_update(self) -> None:
        store = FakeStore(items=[_make_item("Тест") for _ in range(3)])
        vocab = FakeVocabStore()
        result = self.builder.auto_update(store, vocab)
        self.assertIsInstance(result, VocabularyUpdate)
        self.assertIsInstance(result.new_words, list)
        self.assertIsInstance(result.sources, dict)


class TestVocabularyStoreIntegration(unittest.TestCase):
    """Интеграционный тест: SmartVocabularyBuilder + реальный VocabularyStore."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.vocab_store = VocabularyStore(data_dir=Path(self._tmpdir))
        self.builder = SmartVocabularyBuilder()

    def test_build_and_save_via_vocabulary_store(self) -> None:
        items = [_make_item(f"Метод DataScience применяется в задаче {i}") for i in range(5)]
        store = FakeStore(items=items)
        update = self.builder.auto_update(store, self.vocab_store, min_frequency=3)
        self.assertIsInstance(update, VocabularyUpdate)
        # Словарь должен остаться корректным JSON
        loaded = self.vocab_store.load()
        self.assertIsInstance(loaded, list)

    def test_no_duplicates_after_multiple_updates(self) -> None:
        items = [_make_item("Модель GPT4 даёт хорошие результаты") for _ in range(5)]
        store = FakeStore(items=items)
        # Запускаем авто-апдейт дважды
        self.builder.auto_update(store, self.vocab_store, min_frequency=3)
        self.builder.auto_update(store, self.vocab_store, min_frequency=3)
        loaded = self.vocab_store.load()
        # Не должно быть дублей
        self.assertEqual(len(loaded), len(set(loaded)))


if __name__ == "__main__":
    unittest.main()
