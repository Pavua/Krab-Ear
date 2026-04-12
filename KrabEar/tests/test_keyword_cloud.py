"""Тесты для KeywordCloudGenerator — генератор данных облака ключевых слов.

Покрывает:
1. generate_cloud — базовый сценарий (CloudWord dataclass)
2. generate_cloud — фильтрация стоп-слов
3. generate_cloud — нормализация регистра
4. generate_cloud — фильтрация по языку
5. generate_cloud — пустой список items
6. generate_cloud — ограничение max_words
7. CloudWord.weight — нормализация (0-1)
8. CloudWord.font_size — масштабирование (12-72)
9. generate_cloud_svg — валидный SVG
10. generate_cloud_svg — пустой SVG при отсутствии данных
11. generate_cloud — объекты с атрибутами (не словари)
12. _merge_similar — слияние вариантов написания
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from dataclasses import dataclass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.keyword_cloud import KeywordCloudGenerator, CloudWord


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _dict_item(text: str, source_lang: str = "") -> dict:
    return {"text": text, "source_lang": source_lang}


@dataclass
class _FakeHistoryItem:
    """Имитирует объект истории (не словарь)."""
    text: str
    source_lang: str = ""


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

class TestCloudWordDataclass(unittest.TestCase):
    """Тесты CloudWord dataclass."""

    def test_to_dict_contains_required_keys(self) -> None:
        cw = CloudWord(word="тест", count=5, weight=0.5, font_size=42)
        d = cw.to_dict()
        self.assertIn("word", d)
        self.assertIn("count", d)
        self.assertIn("weight", d)
        self.assertIn("font_size", d)

    def test_to_dict_values_match(self) -> None:
        cw = CloudWord(word="hello", count=10, weight=1.0, font_size=72)
        d = cw.to_dict()
        self.assertEqual(d["word"], "hello")
        self.assertEqual(d["count"], 10)
        self.assertAlmostEqual(d["weight"], 1.0)
        self.assertEqual(d["font_size"], 72)


class TestGenerateCloudBasic(unittest.TestCase):
    """Базовые тесты generate_cloud."""

    def setUp(self) -> None:
        self.gen = KeywordCloudGenerator()

    def test_returns_list_of_cloud_words(self) -> None:
        items = [_dict_item("кошка собака кошка птица")]
        result = self.gen.generate_cloud(items)
        self.assertIsInstance(result, list)
        self.assertTrue(all(isinstance(w, CloudWord) for w in result))

    def test_most_frequent_word_first(self) -> None:
        items = [_dict_item("кошка собака кошка кошка птица собака")]
        result = self.gen.generate_cloud(items)
        self.assertGreater(len(result), 0)
        # «кошка» встречается 3 раза — должна быть первой
        self.assertEqual(result[0].word, "кошка")
        self.assertEqual(result[0].count, 3)

    def test_empty_items_returns_empty_list(self) -> None:
        result = self.gen.generate_cloud([])
        self.assertEqual(result, [])

    def test_items_with_only_stop_words_returns_empty(self) -> None:
        items = [_dict_item("и а но да то или не ни")]
        result = self.gen.generate_cloud(items)
        self.assertEqual(result, [])

    def test_case_normalization(self) -> None:
        items = [_dict_item("Кошка кошка КОШКА")]
        result = self.gen.generate_cloud(items)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].word, "кошка")
        self.assertEqual(result[0].count, 3)

    def test_max_words_limits_output(self) -> None:
        # Создаём текст с 20 уникальными словами
        words = [f"слово{i}" for i in range(20)]
        items = [_dict_item(" ".join(words))]
        result = self.gen.generate_cloud(items, max_words=5)
        self.assertLessEqual(len(result), 5)


class TestGenerateCloudWeightAndFontSize(unittest.TestCase):
    """Тесты нормализации веса и масштабирования шрифта."""

    def setUp(self) -> None:
        self.gen = KeywordCloudGenerator()

    def test_top_word_has_weight_one(self) -> None:
        items = [_dict_item("кошка кошка кошка собака")]
        result = self.gen.generate_cloud(items)
        self.assertAlmostEqual(result[0].weight, 1.0)

    def test_weight_in_range_0_1(self) -> None:
        items = [_dict_item("кошка кошка кошка собака птица птица")]
        result = self.gen.generate_cloud(items)
        for cw in result:
            self.assertGreaterEqual(cw.weight, 0.0)
            self.assertLessEqual(cw.weight, 1.0)

    def test_font_size_in_range(self) -> None:
        items = [_dict_item("кошка кошка кошка собака птица птица рыба")]
        result = self.gen.generate_cloud(items)
        for cw in result:
            self.assertGreaterEqual(cw.font_size, 12)
            self.assertLessEqual(cw.font_size, 72)

    def test_top_word_has_max_font_size(self) -> None:
        items = [_dict_item("кошка кошка кошка собака")]
        result = self.gen.generate_cloud(items)
        self.assertEqual(result[0].font_size, 72)

    def test_custom_font_size_range(self) -> None:
        gen = KeywordCloudGenerator(font_size_min=20, font_size_max=50)
        items = [_dict_item("кошка кошка собака")]
        result = gen.generate_cloud(items)
        for cw in result:
            self.assertGreaterEqual(cw.font_size, 20)
            self.assertLessEqual(cw.font_size, 50)


class TestGenerateCloudLanguageFilter(unittest.TestCase):
    """Тесты фильтрации по языку."""

    def setUp(self) -> None:
        self.gen = KeywordCloudGenerator()

    def test_language_filter_ru(self) -> None:
        items = [
            _dict_item("кошка собака", source_lang="ru"),
            _dict_item("gato perro", source_lang="es"),
        ]
        result = self.gen.generate_cloud(items, language="ru")
        words = {cw.word for cw in result}
        self.assertIn("кошка", words)
        self.assertNotIn("gato", words)

    def test_language_filter_es(self) -> None:
        items = [
            _dict_item("кошка собака", source_lang="ru"),
            _dict_item("gato perro gato", source_lang="es"),
        ]
        result = self.gen.generate_cloud(items, language="es")
        words = {cw.word for cw in result}
        self.assertIn("gato", words)
        self.assertNotIn("кошка", words)

    def test_no_language_filter_includes_all(self) -> None:
        items = [
            _dict_item("кошка", source_lang="ru"),
            _dict_item("cat", source_lang="en"),
        ]
        result = self.gen.generate_cloud(items)
        words = {cw.word for cw in result}
        self.assertIn("кошка", words)
        self.assertIn("cat", words)

    def test_language_filter_empty_result_when_no_match(self) -> None:
        items = [_dict_item("кошка собака", source_lang="ru")]
        result = self.gen.generate_cloud(items, language="es")
        self.assertEqual(result, [])


class TestGenerateCloudObjectItems(unittest.TestCase):
    """Тесты с объектами вместо словарей."""

    def setUp(self) -> None:
        self.gen = KeywordCloudGenerator()

    def test_accepts_objects_with_text_attribute(self) -> None:
        items = [_FakeHistoryItem(text="кошка собака кошка")]
        result = self.gen.generate_cloud(items)
        self.assertGreater(len(result), 0)
        self.assertEqual(result[0].word, "кошка")

    def test_object_language_filter(self) -> None:
        items = [
            _FakeHistoryItem(text="кошка", source_lang="ru"),
            _FakeHistoryItem(text="gato gato", source_lang="es"),
        ]
        result = self.gen.generate_cloud(items, language="es")
        words = {cw.word for cw in result}
        self.assertIn("gato", words)
        self.assertNotIn("кошка", words)


class TestMergeSimilar(unittest.TestCase):
    """Тесты слияния похожих слов (е/ё варианты)."""

    def setUp(self) -> None:
        self.gen = KeywordCloudGenerator()

    def test_eshche_merged(self) -> None:
        # «еще» должен быть смержен в «ещё» — но оба являются стоп-словами,
        # поэтому используем кастомный генератор без стоп-слов для теста слияния.
        gen = KeywordCloudGenerator(stop_words=frozenset())
        items = [_dict_item("еще ещё еще")]
        result = gen.generate_cloud(items)
        self.assertEqual(len(result), 1)
        # canonical форма — «ещё»
        self.assertEqual(result[0].word, "ещё")
        self.assertEqual(result[0].count, 3)


class TestGenerateCloudSVG(unittest.TestCase):
    """Тесты генерации SVG."""

    def setUp(self) -> None:
        self.gen = KeywordCloudGenerator(seed=42)

    def test_svg_starts_with_svg_tag(self) -> None:
        items = [_dict_item("кошка собака кошка птица")]
        svg = self.gen.generate_cloud_svg(items)
        self.assertTrue(svg.strip().startswith("<svg"))

    def test_svg_ends_with_closing_tag(self) -> None:
        items = [_dict_item("кошка собака кошка птица")]
        svg = self.gen.generate_cloud_svg(items)
        self.assertIn("</svg>", svg)

    def test_svg_contains_word(self) -> None:
        items = [_dict_item("кошка кошка кошка")]
        svg = self.gen.generate_cloud_svg(items)
        self.assertIn("кошка", svg)

    def test_empty_svg_when_no_items(self) -> None:
        svg = self.gen.generate_cloud_svg([])
        self.assertIn("<svg", svg)
        # Пустой SVG содержит «Нет данных»
        self.assertIn("Нет данных", svg)

    def test_svg_respects_width_height(self) -> None:
        items = [_dict_item("кошка собака кошка")]
        svg = self.gen.generate_cloud_svg(items, width=1200, height=600)
        self.assertIn('width="1200"', svg)
        self.assertIn('height="600"', svg)

    def test_svg_is_deterministic_with_seed(self) -> None:
        items = [_dict_item("кошка собака кошка птица рыба")]
        svg1 = self.gen.generate_cloud_svg(items)
        svg2 = self.gen.generate_cloud_svg(items)
        self.assertEqual(svg1, svg2)


class TestGenerateCloudStopWords(unittest.TestCase):
    """Тесты фильтрации стоп-слов."""

    def setUp(self) -> None:
        self.gen = KeywordCloudGenerator()

    def test_english_stop_words_filtered(self) -> None:
        items = [_dict_item("the cat and the dog and the bird")]
        result = self.gen.generate_cloud(items)
        words = {cw.word for cw in result}
        self.assertNotIn("the", words)
        self.assertNotIn("and", words)
        self.assertIn("cat", words)

    def test_russian_stop_words_filtered(self) -> None:
        items = [_dict_item("кошка и собака в доме")]
        result = self.gen.generate_cloud(items)
        words = {cw.word for cw in result}
        self.assertNotIn("и", words)
        self.assertNotIn("в", words)
        self.assertIn("кошка", words)

    def test_custom_stop_words(self) -> None:
        custom = frozenset({"кошка"})
        gen = KeywordCloudGenerator(stop_words=custom)
        items = [_dict_item("кошка собака кошка")]
        result = gen.generate_cloud(items)
        words = {cw.word for cw in result}
        self.assertNotIn("кошка", words)
        self.assertIn("собака", words)


class TestMinWordLength(unittest.TestCase):
    """Тесты минимальной длины слова."""

    def test_short_words_filtered(self) -> None:
        # Слова длиной 1 символ должны быть отфильтрованы по умолчанию
        gen = KeywordCloudGenerator(stop_words=frozenset())
        items = [_dict_item("я я я кошка кошка")]
        result = gen.generate_cloud(items)
        words = {cw.word for cw in result}
        self.assertNotIn("я", words)
        self.assertIn("кошка", words)

    def test_custom_min_length(self) -> None:
        gen = KeywordCloudGenerator(stop_words=frozenset(), min_word_length=5)
        items = [_dict_item("кот кошка собака")]
        result = gen.generate_cloud(items)
        words = {cw.word for cw in result}
        self.assertNotIn("кот", words)  # 3 символа — отфильтровано
        self.assertIn("кошка", words)   # 5 символов — ок
        self.assertIn("собака", words)  # 6 символов — ок


if __name__ == "__main__":
    unittest.main(verbosity=2)
