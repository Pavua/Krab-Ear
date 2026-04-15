"""Тесты AutoTitleGenerator — автоматическая генерация заголовков для записей."""

from __future__ import annotations
from core.auto_title import AutoTitleGenerator

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestGenerateTitleRussianText(unittest.TestCase):
    """Генерация заголовка для русского текста."""

    def setUp(self) -> None:
        self.gen = AutoTitleGenerator()

    def test_basic_russian_sentence(self):
        """Первое предложение русского текста становится заголовком."""
        text = "Обсуждаем архитектуру бэкенда. Нужно разделить сервисы на модули."
        title = self.gen.generate_title(text)
        self.assertIn("Обсуждаем", title)
        self.assertTrue(title[0].isupper())

    def test_russian_multisentence(self):
        """Берётся только первое предложение из длинного текста."""
        text = (
            "Сегодня проводим код-ревью. Обсуждаем pull request. "
            "Есть несколько замечаний по стилю. Нужно добавить тесты."
        )
        title = self.gen.generate_title(text)
        self.assertIn("Сегодня", title)
        # Заголовок не должен содержать весь текст
        self.assertLessEqual(len(title), 53)  # 50 + "..."

    def test_capitalize_first_letter(self):
        """Первая буква заголовка всегда заглавная."""
        text = "нужно обсудить планы на следующую неделю"
        title = self.gen.generate_title(text)
        self.assertTrue(title[0].isupper(), f"Первая буква должна быть заглавной: {title!r}")

    def test_max_length_enforced(self):
        """Заголовок не превышает max_length символов."""
        text = "Это очень длинная транскрибация которая содержит много слов и должна быть обрезана по границе слова."
        title = self.gen.generate_title(text, max_length=30)
        self.assertLessEqual(len(title), 30)

    def test_truncation_at_word_boundary(self):
        """Обрезка происходит по границе слова, а не посередине."""
        text = "Обсуждение результатов квартального отчёта по продажам и маркетингу."
        title = self.gen.generate_title(text, max_length=30)
        # Не должно быть обрезанных слов (если обрезан — заканчивается на «...»)
        if title.endswith("..."):
            body = title[:-3]
            self.assertFalse(body.endswith(" "), f"Не должно быть пробела перед ...: {title!r}")
            # Последний символ перед «...» — буква (слово не обрезано посередине)
            self.assertTrue(body[-1].isalpha() or body[-1] in "!?,", f"Слово обрезано: {title!r}")


class TestGreetingSkip(unittest.TestCase):
    """Пропуск слов-заполнителей в начале фразы."""

    def setUp(self) -> None:
        self.gen = AutoTitleGenerator()

    def test_skip_nu(self):
        """Слово «ну» в начале пропускается."""
        text = "Ну вот мы и собрались обсудить новые задачи команды."
        title = self.gen.generate_title(text)
        self.assertNotEqual(title.lower()[:2], "ну", f"«ну» не должно быть в начале: {title!r}")

    def test_skip_tak(self):
        """Слово «так» в начале пропускается."""
        text = "Так давайте начнём обсуждение архитектуры системы."
        title = self.gen.generate_title(text)
        self.assertFalse(
            title.lower().startswith("так "),
            f"«так» не должно быть в начале: {title!r}"
        )

    def test_skip_koroche(self):
        """Слово «короче» в начале пропускается."""
        text = "Короче говоря, нужно срочно исправить баг в продакшне."
        title = self.gen.generate_title(text)
        self.assertFalse(
            title.lower().startswith("короче"),
            f"«короче» не должно быть в начале: {title!r}"
        )

    def test_skip_multiple_fillers(self):
        """Несколько заполнителей подряд пропускаются."""
        text = "Ну так вот, сегодня обсуждаем план релиза."
        title = self.gen.generate_title(text)
        # После пропуска заполнителей должно начаться с «сегодня» или «план»
        self.assertFalse(
            title.lower().startswith("ну"),
            f"Заполнители не были пропущены: {title!r}"
        )


class TestShortText(unittest.TestCase):
    """Обработка очень коротких текстов."""

    def setUp(self) -> None:
        self.gen = AutoTitleGenerator()

    def test_very_short_text_used_as_is(self):
        """Текст с менее 5 словами используется as-is."""
        text = "Тест запись"
        title = self.gen.generate_title(text)
        self.assertIn("Тест", title)

    def test_short_text_three_words(self):
        """Текст из трёх слов не обрезается."""
        text = "короткий тест текст"
        title = self.gen.generate_title(text)
        # Хотя «короткий» не заполнитель, текст из 3 слов используется as-is
        self.assertGreater(len(title), 0)
        self.assertTrue(title[0].isupper())

    def test_empty_text_returns_default(self):
        """Пустой текст возвращает заглавие по умолчанию."""
        self.assertEqual(self.gen.generate_title(""), "Запись")
        self.assertEqual(self.gen.generate_title("   "), "Запись")

    def test_whitespace_only_returns_default(self):
        """Текст только из пробелов возвращает заглавие по умолчанию."""
        title = self.gen.generate_title("\n\t  \n")
        self.assertEqual(title, "Запись")


class TestDiarization(unittest.TestCase):
    """Обработка диаризованного текста (с метками спикеров)."""

    def setUp(self) -> None:
        self.gen = AutoTitleGenerator()

    def test_diarized_text_uses_first_speaker(self):
        """Из диаризованного текста берётся фраза первого спикера."""
        text = (
            "Speaker 0: Давайте обсудим результаты спринта.\n"
            "Speaker 1: Согласен, у меня есть несколько вопросов."
        )
        title = self.gen.generate_title(text)
        # Должна быть фраза первого спикера
        self.assertIn("Давайте", title)

    def test_diarized_uppercase_speaker(self):
        """Формат SPEAKER_01: тоже обрабатывается."""
        text = (
            "SPEAKER_01: Начинаем встречу по планированию.\n"
            "SPEAKER_02: Хорошо, я готов."
        )
        title = self.gen.generate_title(text)
        self.assertIn("Начинаем", title)

    def test_diarized_bracket_format(self):
        """Формат [Speaker 1]: тоже обрабатывается."""
        text = (
            "[Speaker 1]: Рассмотрим задачи на неделю.\n"
            "[Speaker 2]: Первая задача — рефакторинг."
        )
        title = self.gen.generate_title(text)
        self.assertIn("Рассмотрим", title)


class TestTitleWithDate(unittest.TestCase):
    """Генерация заголовка с датой."""

    def setUp(self) -> None:
        self.gen = AutoTitleGenerator()

    def test_title_with_date_format(self):
        """Формат заголовка с датой: «YYYY-MM-DD — Фраза...»."""
        text = "Обсуждаем архитектуру нового сервиса."
        timestamp = "2026-04-12T10:30:00Z"
        title = self.gen.generate_title_with_date(text, timestamp)
        self.assertTrue(title.startswith("2026-04-12"), f"Неверный формат: {title!r}")
        self.assertIn(" — ", title)
        self.assertIn("Обсуждаем", title)

    def test_title_with_date_only_string(self):
        """Работает с форматом YYYY-MM-DD без времени."""
        text = "Встреча по итогам квартала."
        title = self.gen.generate_title_with_date(text, "2026-04-12")
        self.assertTrue(title.startswith("2026-04-12"))

    def test_title_with_date_separator(self):
        """Дата и заголовок разделяются «—»."""
        text = "Ежедневный стендап команды разработки."
        title = self.gen.generate_title_with_date(text, "2026-04-12")
        parts = title.split(" — ", 1)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0], "2026-04-12")


class TestBatchGenerate(unittest.TestCase):
    """Пакетная генерация заголовков."""

    def setUp(self) -> None:
        self.gen = AutoTitleGenerator()

    def test_batch_returns_all_items(self):
        """Пакетная генерация возвращает по одному заголовку для каждого элемента."""
        items = [
            {"id": "1", "text": "Первая запись о задачах команды."},
            {"id": "2", "text": "Вторая запись о планах на неделю."},
            {"id": "3", "text": "Третья запись с результатами ревью."},
        ]
        results = self.gen.batch_generate(items)
        self.assertEqual(len(results), 3)

    def test_batch_preserves_ids(self):
        """ID элементов сохраняются в результатах."""
        items = [
            {"id": "abc-123", "text": "Запись для проверки ID."},
        ]
        results = self.gen.batch_generate(items)
        self.assertEqual(results[0]["id"], "abc-123")

    def test_batch_with_timestamp(self):
        """При наличии timestamp заголовок включает дату."""
        items = [
            {
                "id": "1",
                "text": "Обсуждение архитектуры.",
                "timestamp": "2026-04-12T09:00:00Z",
            }
        ]
        results = self.gen.batch_generate(items)
        self.assertTrue(results[0]["title"].startswith("2026-04-12"))

    def test_batch_empty_list(self):
        """Пустой список возвращает пустой результат."""
        results = self.gen.batch_generate([])
        self.assertEqual(results, [])

    def test_batch_has_generated_at(self):
        """Каждый результат содержит поле generated_at."""
        items = [{"id": "x", "text": "Тест поля generated_at."}]
        results = self.gen.batch_generate(items)
        self.assertIn("generated_at", results[0])
        self.assertTrue(results[0]["generated_at"].endswith("Z"))

    def test_batch_title_key_present(self):
        """Каждый результат содержит поле title."""
        items = [{"id": "1", "text": "Тест наличия поля title."}]
        results = self.gen.batch_generate(items)
        self.assertIn("title", results[0])
        self.assertIsInstance(results[0]["title"], str)
        self.assertGreater(len(results[0]["title"]), 0)


class TestEdgeCases(unittest.TestCase):
    """Граничные случаи."""

    def setUp(self) -> None:
        self.gen = AutoTitleGenerator()

    def test_text_without_sentence_end(self):
        """Текст без точки в конце обрабатывается корректно."""
        text = "Обсуждаем текущие задачи и приоритеты команды на следующий спринт"
        title = self.gen.generate_title(text)
        self.assertGreater(len(title), 0)
        self.assertTrue(title[0].isupper())

    def test_long_single_word_sequence(self):
        """Длинная последовательность слов обрезается корректно."""
        text = "Транскрибация совещания руководителей технического департамента по вопросам планирования."
        title = self.gen.generate_title(text, max_length=40)
        self.assertLessEqual(len(title), 40)

    def test_title_not_empty_for_meaningful_text(self):
        """Для содержательного текста заголовок не пустой."""
        texts = [
            "Сегодня провели ревью кода.",
            "Встреча с командой по планированию спринта.",
            "Обсуждение новых требований к системе.",
        ]
        for text in texts:
            title = self.gen.generate_title(text)
            self.assertGreater(len(title), 0, f"Пустой заголовок для: {text!r}")
            self.assertNotEqual(title, "...", f"Заголовок состоит только из ...: {text!r}")

    def test_only_filler_words_returns_text(self):
        """Текст только из заполнителей возвращается как есть (graceful fallback)."""
        text = "Ну ладно окей хорошо"
        title = self.gen.generate_title(text)
        # Не должен упасть и должен вернуть что-то
        self.assertIsInstance(title, str)
        self.assertGreater(len(title), 0)


if __name__ == "__main__":
    unittest.main()
