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


if __name__ == "__main__":
    unittest.main()
