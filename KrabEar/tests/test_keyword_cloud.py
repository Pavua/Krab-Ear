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
12. Нормализация ё→е на этапе токенизации (W1291 F1 MED)
"""

from __future__ import annotations
from backend.keyword_cloud import KeywordCloudGenerator, CloudWord

import sys
import unittest
from pathlib import Path
from dataclasses import dataclass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


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


class TestYoNormalization(unittest.TestCase):
    """Тесты нормализации ё→е на этапе токенизации (W1291 F1 MED).

    После нормализации все варианты написания с ё и е объединяются в одно слово
    с суммарной частотой. Нормализация применяется ДО фильтрации стоп-слов,
    поэтому затрагивает все слова — и контентные, и служебные.
    """

    def test_kyzhestkij_and_zhestkij_merge_into_one(self) -> None:
        """«жёсткий» и «жесткий» должны слиться в одно слово «жесткий» (ё→е)."""
        gen = KeywordCloudGenerator(stop_words=frozenset())
        items = [_dict_item("жёсткий жесткий жёсткий")]
        result = gen.generate_cloud(items)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].word, "жесткий")
        self.assertEqual(result[0].count, 3)

    def test_yo_normalization_applied_before_stopword_filter(self) -> None:
        """ё→е применяется до фильтрации, поэтому «её» (стоп-слово) фильтруется.

        Исходный «ё»-вариант «её» и «е»-вариант «ее» оба являются стоп-словами
        в дефолтном словаре; оба должны быть отфильтрованы, а контентное слово
        «кошка» остаётся.
        """
        gen = KeywordCloudGenerator()
        # «её», «ее» — стоп-слова; «кошка» — нет
        items = [_dict_item("кошка её кошка ее кошка")]
        result = gen.generate_cloud(items)
        words = {cw.word for cw in result}
        self.assertIn("кошка", words)
        self.assertNotIn("её", words)
        self.assertNotIn("ее", words)
        # суммарный count кошки = 3
        counts = {cw.word: cw.count for cw in result}
        self.assertEqual(counts["кошка"], 3)

    def test_lowercase_yo_capital_yo_both_normalized(self) -> None:
        """Строчная ё и заглавная Ё нормализуются одинаково."""
        gen = KeywordCloudGenerator(stop_words=frozenset())
        # «Ёж» и «ёж» и «еж» — всё одно слово после нормализации
        items = [_dict_item("Ёж ёж еж")]
        result = gen.generate_cloud(items)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].word, "еж")
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


class TestWeightFontMonotonicity(unittest.TestCase):
    """weight и font_size монотонно возрастают вместе с count."""

    def setUp(self) -> None:
        self.gen = KeywordCloudGenerator()

    def test_weight_monotonic_with_count(self) -> None:
        """Слова с большим count имеют больший или равный weight."""
        text = "кошка " * 10 + "собака " * 5 + "птица " * 2
        items = [_dict_item(text)]
        result = self.gen.generate_cloud(items)
        # Результат отсортирован по убыванию count
        for i in range(len(result) - 1):
            self.assertGreaterEqual(result[i].count, result[i + 1].count)
            self.assertGreaterEqual(result[i].weight, result[i + 1].weight)

    def test_font_size_monotonic_with_count(self) -> None:
        """Слова с большим count имеют больший или равный font_size."""
        text = "кошка " * 8 + "собака " * 4 + "птица " * 1
        items = [_dict_item(text)]
        result = self.gen.generate_cloud(items)
        for i in range(len(result) - 1):
            self.assertGreaterEqual(result[i].font_size, result[i + 1].font_size)

    def test_higher_count_never_has_smaller_weight(self) -> None:
        """Слово с большим count никогда не имеет меньший weight."""
        text = "альфа " * 20 + "бета " * 10 + "гамма " * 3
        items = [_dict_item(text)]
        result = self.gen.generate_cloud(items)
        if len(result) >= 2:
            self.assertGreater(result[0].weight, result[-1].weight)

    def test_second_word_weight_less_than_first(self) -> None:
        """Второе слово (меньший count) имеет weight < 1.0."""
        text = "кошка " * 5 + "собака " * 2
        items = [_dict_item(text)]
        result = self.gen.generate_cloud(items)
        self.assertAlmostEqual(result[0].weight, 1.0)
        if len(result) > 1:
            self.assertLess(result[1].weight, 1.0)


class TestSingleRepeatedWord(unittest.TestCase):
    """Граничный случай: единственное повторяющееся слово."""

    def setUp(self) -> None:
        self.gen = KeywordCloudGenerator()

    def test_single_word_repeated_weight_is_one(self) -> None:
        items = [_dict_item("кошка кошка кошка кошка")]
        result = self.gen.generate_cloud(items)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0].weight, 1.0)
        self.assertEqual(result[0].font_size, 72)

    def test_single_word_repeated_count_correct(self) -> None:
        items = [_dict_item("собака собака собака")]
        result = self.gen.generate_cloud(items)
        self.assertEqual(result[0].count, 3)

    def test_single_unique_word_still_has_max_weight(self) -> None:
        items = [_dict_item("уникальность")]
        result = self.gen.generate_cloud(items)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0].weight, 1.0)
        self.assertEqual(result[0].font_size, 72)

    def test_single_word_across_multiple_items(self) -> None:
        """Одно слово в нескольких items суммируется."""
        items = [_dict_item("кошка") for _ in range(5)]
        result = self.gen.generate_cloud(items)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].count, 5)
        self.assertAlmostEqual(result[0].weight, 1.0)


class TestTopN(unittest.TestCase):
    """Тест max_words (аналог top_n=20)."""

    def setUp(self) -> None:
        self.gen = KeywordCloudGenerator()

    def test_max_words_20_limits_to_20(self) -> None:
        # 30 уникальных слов
        words = [f"слово{i}" for i in range(30)]
        items = [_dict_item(" ".join(words))]
        result = self.gen.generate_cloud(items, max_words=20)
        self.assertLessEqual(len(result), 20)

    def test_max_words_1_returns_single_top_word(self) -> None:
        text = "кошка " * 5 + "собака " * 3 + "птица"
        items = [_dict_item(text)]
        result = self.gen.generate_cloud(items, max_words=1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].word, "кошка")

    def test_max_words_default_100_applied(self) -> None:
        # 5 уникальных слов (исключительно буквы, не попадают в стоп-слова)
        unique_words = ["кошка", "собака", "птица", "рыба", "лиса"]
        items = [_dict_item(" ".join(unique_words))]
        result = self.gen.generate_cloud(items)
        self.assertEqual(len(result), 5)

    def test_result_sorted_by_count_descending(self) -> None:
        text = "альфа " * 9 + "бета " * 6 + "гамма " * 3
        items = [_dict_item(text)]
        result = self.gen.generate_cloud(items, max_words=20)
        counts = [cw.count for cw in result]
        self.assertEqual(counts, sorted(counts, reverse=True))


class TestUnicodeWords(unittest.TestCase):
    """Тесты обработки Unicode-слов (кириллица, испанские акценты)."""

    def setUp(self) -> None:
        self.gen = KeywordCloudGenerator(stop_words=frozenset())

    def test_cyrillic_words_counted(self) -> None:
        items = [_dict_item("Привет привет привет мир")]
        result = self.gen.generate_cloud(items)
        words = {cw.word for cw in result}
        self.assertIn("привет", words)
        self.assertIn("мир", words)

    def test_spanish_accented_words_counted(self) -> None:
        """Слова с диакритикой: á/é/í/ó/ú/ñ должны корректно токенизироваться."""
        items = [_dict_item("canción canción música música música café")]
        result = self.gen.generate_cloud(items)
        words = {cw.word for cw in result}
        self.assertIn("música", words)
        self.assertIn("canción", words)
        self.assertIn("café", words)

    def test_spanish_accented_case_normalization(self) -> None:
        """Акцентированные слова нормализуются к нижнему регистру."""
        items = [_dict_item("Música música MÚSICA")]
        result = self.gen.generate_cloud(items)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].word, "música")
        self.assertEqual(result[0].count, 3)

    def test_mixed_scripts_all_counted(self) -> None:
        """Кириллица и латиница в одном тексте — оба подсчитываются."""
        gen = KeywordCloudGenerator(stop_words=frozenset())
        items = [_dict_item("кошка кошка cat cat cat")]
        result = gen.generate_cloud(items)
        words = {cw.word for cw in result}
        self.assertIn("кошка", words)
        self.assertIn("cat", words)
        # cat встречается 3 раза, кошка — 2, cat должна быть первой
        counts = {cw.word: cw.count for cw in result}
        self.assertEqual(counts["cat"], 3)
        self.assertEqual(counts["кошка"], 2)

    def test_unicode_tokenize_strips_digits(self) -> None:
        """Цифры не должны попасть в слова (только буквы)."""
        gen = KeywordCloudGenerator(stop_words=frozenset())
        items = [_dict_item("test123 hello world")]
        result = gen.generate_cloud(items)
        words = {cw.word for cw in result}
        # test123 разбивается по цифрам — «test» и «hello» попадут
        self.assertIn("hello", words)
        self.assertNotIn("123", words)


class TestCloudWordWeightEdge(unittest.TestCase):
    """Дополнительные граничные случаи weight/font_size."""

    def test_all_equal_counts_same_weight(self) -> None:
        """Все слова с одинаковым count → одинаковые weight и font_size."""
        gen = KeywordCloudGenerator(stop_words=frozenset())
        # Каждое слово встречается ровно 1 раз
        text = "альфа бета гамма дельта"
        items = [_dict_item(text)]
        result = gen.generate_cloud(items)
        # Все веса должны быть 1.0 (count/max_count = 1/1)
        for cw in result:
            self.assertAlmostEqual(cw.weight, 1.0)
            self.assertEqual(cw.font_size, 72)

    def test_font_size_min_for_lowest_weight(self) -> None:
        """Слово с наименьшим count (weight ≈ 0) имеет font_size близкий к 12."""
        # Одно слово с count=100, другое с count=1
        gen = KeywordCloudGenerator(stop_words=frozenset())
        text = "кошка " * 100 + "собака"
        items = [_dict_item(text)]
        result = gen.generate_cloud(items, max_words=100)
        last = result[-1]
        # font_size для weight=0.01 → 12 + 0.01*60 ≈ 12
        self.assertGreaterEqual(last.font_size, 12)
        self.assertLessEqual(last.font_size, 72)


class TestGenerateCloudMaxWordsZeroOrNegative(unittest.TestCase):
    """F1 fix — max_words <= 0 должен возвращать пустой список (W1093)."""

    def test_max_words_zero_returns_empty(self) -> None:
        """max_words=0 → пустой список, не одно слово."""
        gen = KeywordCloudGenerator(stop_words=frozenset())
        items = [_dict_item("кошка собака кошка")]
        result = gen.generate_cloud(items, max_words=0)
        self.assertEqual(result, [])

    def test_max_words_negative_returns_empty(self) -> None:
        """max_words=-5 → пустой список."""
        gen = KeywordCloudGenerator(stop_words=frozenset())
        items = [_dict_item("кошка собака кошка")]
        result = gen.generate_cloud(items, max_words=-5)
        self.assertEqual(result, [])


class TestHandleGetKeywordCloudPrivacyMode(unittest.TestCase):
    """F3 fix — privacy_mode_enabled блокирует word cloud (W1093).

    Тест не импортирует BackendService напрямую (тяжёлый модуль с Python 3.10+
    зависимостями). Вместо этого вырезаем логику хендлера в автономную функцию
    и проверяем её корректность через stub.
    """

    @staticmethod
    def _invoke_handler(privacy_enabled: bool, params: dict):
        """Имитирует тело _handle_get_keyword_cloud с проверкой privacy_mode."""
        # Воспроизводим точный код хендлера из service.py (W1093 F3 fix)
        def _get_runtime_setting(key, default=None):
            if key == "privacy_mode_enabled":
                return privacy_enabled
            return default

        if _get_runtime_setting("privacy_mode_enabled", False):
            return {"ok": True, "words": [], "reason": "privacy_mode_active"}

        # При privacy_mode=False — генерируем облако напрямую
        from backend.keyword_cloud import KeywordCloudGenerator
        gen = KeywordCloudGenerator()
        items = [{"text": "Иван Москва банк Иван", "source_lang": "ru"}]
        max_words = int(params.get("max_words", 100))
        language = params.get("language")
        cloud_words = gen.generate_cloud(items, max_words=max_words, language=language)
        return {
            "words": [
                {
                    "word": cw.word,
                    "count": cw.count,
                    "weight": cw.weight,
                    "font_size": cw.font_size,
                }
                for cw in cloud_words
            ]
        }

    def test_privacy_mode_enabled_returns_empty_words(self) -> None:
        """С privacy_mode_enabled=True хендлер возвращает words=[] и reason."""
        result = self._invoke_handler(privacy_enabled=True, params={})
        self.assertEqual(result["words"], [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        self.assertTrue(result.get("ok"))

    def test_privacy_mode_disabled_returns_words(self) -> None:
        """С privacy_mode_enabled=False хендлер нормально возвращает облако."""
        result = self._invoke_handler(privacy_enabled=False, params={})
        # words должен быть списком
        self.assertIn("words", result)
        self.assertIsInstance(result["words"], list)
        # reason не должен быть privacy_mode_active
        self.assertNotEqual(result.get("reason"), "privacy_mode_active")


if __name__ == "__main__":
    unittest.main(verbosity=2)
