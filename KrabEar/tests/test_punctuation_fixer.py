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


class TestPunctuationFixerWave132(unittest.TestCase):
    """Wave 132 required test cases."""

    def setUp(self):
        self.fixer = PunctuationFixer()

    def test_ru_comma_before_который_что(self):
        """Запятая перед 'который'/'что' в придаточном не должна нарушаться/дублироваться."""
        # Запятая уже есть — не должна дублироваться
        text = "Я думаю, что всё хорошо."
        result = self.fixer.fix(text, language="ru")
        self.assertNotIn(",,", result, f"Двойная запятая недопустима: {result!r}")
        self.assertIn("что", result, f"Слово 'что' должно сохраниться: {result!r}")

        # Фраза с 'который' — запятая не должна дублироваться
        text2 = "Книга, которую я читал, интересная."
        result2 = self.fixer.fix(text2, language="ru")
        self.assertNotIn(",,", result2)
        self.assertIn("которую", result2)

    def test_unicode_preserved(self):
        """Unicode-символы (кириллица, спецзнаки) сохраняются без искажений."""
        text = "Привет мир — всё хорошо."
        result = self.fixer.fix(text, language="ru")
        self.assertIn("Привет", result)
        self.assertIn("—", result, f"Тире должно сохраниться: {result!r}")
        self.assertIn("всё", result, f"'ё' должна сохраниться: {result!r}")

        # Кириллические буквы с диакритикой не теряются
        text_es = "¡Hola señor!"
        result_es = self.fixer.fix(text_es, language="es")
        self.assertIn("ñ", result_es, f"Буква ñ должна сохраниться: {result_es!r}")

    def test_concurrent_fix(self):
        """PunctuationFixer.fix() потокобезопасен при параллельных вызовах."""
        import threading

        results = {}
        errors = []

        def worker(idx, text, lang):
            try:
                results[idx] = self.fixer.fix(text, language=lang)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(i, f"тест номер {i} работает хорошо", "ru"))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Ошибки при параллельных вызовах: {errors}")
        self.assertEqual(len(results), 20)
        for idx, result in results.items():
            self.assertIsInstance(result, str)
            self.assertTrue(len(result) > 0, f"Пустой результат для idx={idx}")


class TestPunctuationFixerW1348RuleOrder(unittest.TestCase):
    """Regression tests for W1348 R1 HIGH: rule-ordering bug.

    Bug: step 'remove-space-before-punct' ran BEFORE 'add-space-after-punct',
    so spaces introduced by the add-step were never cleaned up.
    Fix: swap order — add-space-after first, then remove-space-before.
    """

    def setUp(self):
        self.fixer = PunctuationFixer()

    def test_quoted_string_followed_by_period(self):
        """'Он сказал «стоп».' must NOT produce 'Он сказал «стоп» .'

        Before the fix the pipeline was:
          1. remove space before '»' (no-op here)
          2. add space after '»' because next char is '.' → «стоп» .
          Step 1 already done → the new space before '.' was never removed.
        After the fix (add-space first, then remove-space):
          1. add space after '»' → «стоп» .
          2. remove space before '.' → «стоп».
        """
        result = self.fixer.fix("Он сказал «стоп».", language="ru")
        self.assertNotIn("» .", result, f"Пробел перед точкой после » недопустим: {result!r}")
        self.assertIn("».", result, f"Ожидается '».' без пробела: {result!r}")

    def test_idempotency_single_pass(self):
        """Single fix() call must produce the same result as two consecutive calls.

        This verifies that the pipeline is effectively idempotent — no further
        clean-up is needed after one pass.
        """
        samples = [
            ("Он сказал «стоп».", "ru"),
            ("привет,мир!", "ru"),
            ("тест . конец", "ru"),
            ("cómo estás?", "es"),
            ("Привет , как дела.", "ru"),
        ]
        for text, lang in samples:
            once = self.fixer.fix(text, language=lang)
            twice = self.fixer.fix(once, language=lang)
            self.assertEqual(once, twice,
                             f"fix() не идемпотентен для {text!r}: "
                             f"once={once!r}, twice={twice!r}")

    def test_ru_es_en_no_regression(self):
        """Canonical samples for RU/ES/EN still produce expected output after reorder."""
        cases = [
            # (input, language, substring_expected, substring_forbidden)
            ("привет , как дела", "ru", "Привет,", " ,"),
            ("Раз,два,три", "ru", ", ", None),
            ("я думаю", "ru", "Я", " я "),
            ("Привет . Мир", "ru", "Привет.", " ."),
            ("hola mundo", "es", "Hola", None),
            ("cómo estás?", "es", "¿", None),
            ("Он сказал «стоп».", "ru", "».", "» ."),
        ]
        for text, lang, expected, forbidden in cases:
            result = self.fixer.fix(text, language=lang)
            if expected:
                self.assertIn(expected, result,
                              f"[{lang}] '{text}' → expected {expected!r} in {result!r}")
            if forbidden:
                self.assertNotIn(forbidden, result,
                                 f"[{lang}] '{text}' → forbidden {forbidden!r} in {result!r}")


if __name__ == "__main__":
    unittest.main()
