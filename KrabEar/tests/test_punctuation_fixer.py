"""Тесты для PunctuationFixer.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_punctuation_fixer.py -v
"""

from core.punctuation_fixer import PunctuationFixer
import unittest
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestPunctuationFixerRussian(unittest.TestCase):

    def setUp(self):
        self.fixer = PunctuationFixer()

    def test_capitalize_first_letter(self):
        result = self.fixer.fix("привет, как дела", language="ru")
        self.assertTrue(result[0].isupper(), f"Первая буква должна быть заглавной: {result!r}")

    def test_add_missing_period(self):
        result = self.fixer.fix("Привет, как дела", language="ru")
        self.assertTrue(result.endswith("."), f"Ожидается точка в конце: {result!r}")

    def test_no_double_period(self):
        result = self.fixer.fix("Привет, как дела.", language="ru")
        self.assertFalse(result.endswith(".."), f"Не должно быть двойной точки: {result!r}")

    def test_remove_double_spaces(self):
        result = self.fixer.fix("привет  как  дела", language="ru")
        self.assertNotIn("  ", result, f"Двойные пробелы должны быть убраны: {result!r}")

    def test_no_space_before_comma(self):
        result = self.fixer.fix("Привет , как дела.", language="ru")
        self.assertNotIn(" ,", result, f"Пробел перед запятой должен быть убран: {result!r}")

    def test_space_after_comma(self):
        result = self.fixer.fix("Раз,два,три.", language="ru")
        self.assertIn(", ", result, f"Пробел после запятой должен быть добавлен: {result!r}")

    def test_capitalize_after_period(self):
        result = self.fixer.fix("Первое предложение. второе предложение.", language="ru")
        # "второе" должно стать "Второе"
        self.assertIn("Второе", result, f"Ожидается капитализация после точки: {result!r}")

    def test_capitalize_standalone_ya(self):
        result = self.fixer.fix("я думаю, что я прав.", language="ru")
        self.assertNotIn(" я ", result, f"Одиночное 'я' должно быть 'Я': {result!r}")

    def test_fix_ascii_quotes_to_russian(self):
        result = self.fixer.fix('он сказал "привет" мне.', language="ru")
        self.assertIn("«", result, f"ASCII-кавычки должны стать «»: {result!r}")
        self.assertIn("»", result, f"ASCII-кавычки должны стать «»: {result!r}")

    def test_already_correct_text_unchanged_structure(self):
        text = "Всё хорошо."
        result = self.fixer.fix(text, language="ru")
        # Основное содержание должно сохраниться
        self.assertIn("Всё хорошо", result)

    def test_no_period_after_question_mark(self):
        result = self.fixer.fix("Как дела?", language="ru")
        # Вопросительный знак уже есть — точку добавлять не нужно
        self.assertFalse(result.endswith("?."), f"Не должно быть '?.' в конце: {result!r}")

    def test_no_period_after_exclamation(self):
        result = self.fixer.fix("Привет!", language="ru")
        self.assertFalse(result.endswith("!."), f"Не должно быть '!.' в конце: {result!r}")

    def test_empty_string(self):
        result = self.fixer.fix("", language="ru")
        self.assertEqual(result, "")

    def test_whitespace_only(self):
        result = self.fixer.fix("   ", language="ru")
        self.assertEqual(result.strip(), "")


class TestPunctuationFixerSpanish(unittest.TestCase):

    def setUp(self):
        self.fixer = PunctuationFixer()

    def test_add_inverted_question_mark(self):
        result = self.fixer.fix("cómo estás?", language="es")
        self.assertTrue(result.lstrip().startswith("¿"), f"Должен быть ¿ в начале вопроса: {result!r}")

    def test_add_inverted_exclamation_mark(self):
        result = self.fixer.fix("qué bueno!", language="es")
        self.assertTrue(result.lstrip().startswith("¡"), f"Должен быть ¡ в начале восклицания: {result!r}")

    def test_no_iquest_if_already_present(self):
        result = self.fixer.fix("¿cómo estás?", language="es")
        self.assertFalse(result.startswith("¿¿"), f"Не должно быть двойного ¿: {result!r}")

    def test_capitalize_first_letter_es(self):
        result = self.fixer.fix("hola, qué tal", language="es")
        self.assertTrue(result[0].isupper(), f"Первая буква должна быть заглавной: {result!r}")


class TestGetFixesApplied(unittest.TestCase):

    def setUp(self):
        self.fixer = PunctuationFixer()

    def test_no_changes_returns_empty(self):
        text = "Всё хорошо."
        fixes = self.fixer.get_fixes_applied(text, text)
        self.assertEqual(fixes, [])

    def test_detects_double_spaces(self):
        original = "привет  мир."
        fixed = self.fixer.fix(original, language="ru")
        fixes = self.fixer.get_fixes_applied(original, fixed)
        self.assertIn("removed double spaces", fixes)

    def test_detects_missing_period(self):
        original = "Привет"
        fixed = self.fixer.fix(original, language="ru")
        fixes = self.fixer.get_fixes_applied(original, fixed)
        self.assertIn("added missing period", fixes)

    def test_detects_capitalization(self):
        original = "привет мир."
        fixed = self.fixer.fix(original, language="ru")
        fixes = self.fixer.get_fixes_applied(original, fixed)
        self.assertIn("capitalized first letter", fixes)

    def test_detects_iquest(self):
        original = "cómo estás?"
        fixed = self.fixer.fix(original, language="es")
        fixes = self.fixer.get_fixes_applied(original, fixed)
        self.assertIn("added ¿ before question", fixes)


class TestTextUtilsIntegration(unittest.TestCase):
    """Проверяет, что TextUtils.fix_punctuation корректно делегирует в PunctuationFixer."""

    def test_text_utils_fix_punctuation(self):
        from core.utils import TextUtils
        result = TextUtils.fix_punctuation("привет мир", language="ru")
        self.assertTrue(result[0].isupper())
        self.assertTrue(result.endswith("."))


class TestPunctuationFixerExplicitRequirements(unittest.TestCase):
    """Explicit requirement scenarios from task spec."""

    def setUp(self):
        self.fixer = PunctuationFixer()

    def test_space_after_comma_added(self):
        """'тест,ok' → space inserted after comma."""
        result = self.fixer.fix("тест,ok", language="ru")
        self.assertIn(", ", result)
        self.assertNotIn(",o", result)

    def test_space_after_comma_not_doubled(self):
        """'тест, ok' already correct → no extra space added."""
        result = self.fixer.fix("тест, ok уже норм.", language="ru")
        self.assertNotIn(",  ", result)

    def test_spanish_inverted_question_mark(self):
        """'qué pasa?' → '¿qué pasa?'."""
        result = self.fixer.fix("qué pasa?", language="es")
        self.assertTrue(result.lstrip().startswith("¿"))
        self.assertIn("?", result)

    def test_spanish_inverted_exclamation_mark(self):
        """'qué bien!' → '¡qué bien!'."""
        result = self.fixer.fix("qué bien!", language="es")
        self.assertTrue(result.lstrip().startswith("¡"))
        self.assertIn("!", result)

    def test_russian_ascii_quotes_to_guillemets(self):
        """'\"text\"' → '«text»'."""
        result = self.fixer.fix('"текст"', language="ru")
        self.assertIn("«", result)
        self.assertIn("»", result)
        self.assertNotIn('"', result)

    def test_already_correct_text_unchanged(self):
        """Already-correct Russian text structure is preserved."""
        text = "Всё хорошо."
        result = self.fixer.fix(text, language="ru")
        self.assertIn("Всё хорошо", result)
        self.assertTrue(result.endswith("."))

    def test_already_correct_spanish_no_double_iquest(self):
        """'¿cómo estás?' does not gain extra '¿'."""
        result = self.fixer.fix("¿cómo estás?", language="es")
        self.assertFalse(result.startswith("¿¿"))

    def test_no_space_before_period(self):
        """Space before period is removed."""
        result = self.fixer.fix("Привет .", language="ru")
        self.assertNotIn(" .", result)

    def test_capitalize_first_letter_ru_explicit(self):
        """Lowercase first letter is capitalised for Russian text."""
        result = self.fixer.fix("проверка.", language="ru")
        self.assertTrue(result[0].isupper())

    def test_capitalize_first_letter_es_explicit(self):
        """Lowercase first letter is capitalised for Spanish text."""
        result = self.fixer.fix("hola mundo.", language="es")
        self.assertTrue(result[0].isupper())

    def test_english_like_text_passes_through_without_error(self):
        """Text treated as default language does not raise exceptions."""
        result = self.fixer.fix("hello world")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)


if __name__ == "__main__":
    unittest.main()
