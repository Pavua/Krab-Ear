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


class TestPunctuationFixerW1377DottedAbbrev(unittest.TestCase):
    """W1374 F2 MED: PunctuationFixer must NOT corrupt dotted abbreviations and version strings."""

    def setUp(self):
        self.fixer = PunctuationFixer()

    # ── Аббревиатуры сохраняются ────────────────────────────────────────────

    def test_т_е_preserved(self):
        """т.е. не должна превращаться в 'Т.Е.' или разрушаться."""
        result = self.fixer.fix("Он пришёл, т.е. появился.", language="ru")
        # Аббревиатура должна остаться нетронутой (без лишних пробелов внутри)
        self.assertIn("т.е.", result, f"Аббревиатура т.е. разрушена: {result!r}")

    def test_т_к_preserved(self):
        """т.к. (так как) не должна разрушаться."""
        result = self.fixer.fix("Хорошо, т.к. погода отличная.", language="ru")
        self.assertIn("т.к.", result, f"Аббревиатура т.к. разрушена: {result!r}")

    def test_т_д_preserved(self):
        """т.д. (так далее) не должна разрушаться."""
        result = self.fixer.fix("Купи хлеб, молоко и т.д.", language="ru")
        self.assertIn("т.д.", result, f"Аббревиатура т.д. разрушена: {result!r}")

    def test_сша_войска_no_extra_period(self):
        """США.Войска не должна получать лишний пробел/точку → США. Войска."""
        # Это легитимный случай: США завершает аббревиатуру перед новым словом с заглавной
        # Ожидаем "США. Войска" (пробел вставляется, это правильно для нового предложения)
        text = "США.Войска вошли в город."
        result = self.fixer.fix(text, language="ru")
        # НЕ должно быть двойной точки "США.."
        self.assertNotIn("США..", result, f"Двойная точка недопустима: {result!r}")

    # ── Версионные строки сохраняются ────────────────────────────────────────

    def test_version_v1_0_beta_preserved(self):
        """Версия v1.0.Beta не должна разрушаться."""
        result = self.fixer.fix("Обновление v1.0.Beta вышло.", language="ru")
        # Версионная строка должна остаться нетронутой (без лишних пробелов)
        self.assertIn("v1.0", result, f"Версионная строка v1.0 разрушена: {result!r}")
        # НЕ должно быть "1.0. Beta" или "1.0 .Beta"
        self.assertNotIn("1.0. Beta", result, f"Версия разбита пробелом: {result!r}")
        self.assertNotIn("1.0 .Beta", result, f"Версия разбита пробелом: {result!r}")

    def test_version_2_5_1_preserved(self):
        """Версия 2.5.1 не должна разрушаться."""
        result = self.fixer.fix("Текущая версия 2.5.1 стабильна.", language="ru")
        self.assertIn("2.5", result, f"Версионная строка 2.5 разрушена: {result!r}")
        self.assertNotIn("2.5. 1", result, f"Версия разбита пробелом: {result!r}")

    def test_version_digit_dot_uppercase_no_extra_space(self):
        """Цифра перед точкой → не добавляем пробел (версии и числа)."""
        result = self.fixer.fix("Версия 3.Alpha тестируется.", language="ru")
        self.assertNotIn("3. Alpha", result, f"Пробел не должен быть вставлен после '3.': {result!r}")

    # ── Нормальные предложения всё ещё получают пробел ─────────────────────

    def test_normal_sentence_split_works(self):
        """'Это.Хорошо' → получает пробел (обычное предложение без аббревиатур)."""
        result = self.fixer.fix("Это.Хорошо", language="ru")
        # "Хорошо" — заглавная без признаков аббревиатуры, пробел должен вставиться
        self.assertIn("Это. Хорошо", result, f"Пробел должен был вставиться: {result!r}")

    def test_multiple_sentences_split_works(self):
        """Два предложения без пробела → пробел вставляется."""
        result = self.fixer.fix("Первое.Второе предложение.", language="ru")
        self.assertIn(". Второе", result, f"Пробел между предложениями должен быть: {result!r}")

    # ── Инициалы ────────────────────────────────────────────────────────────

    def test_initial_single_letter_preserved(self):
        """Однобуквенная аббревиатура (инициал) 'А.Б.' не разрушается пробелами."""
        result = self.fixer.fix("Иванов А.Б. пришёл.", language="ru")
        # Пробел внутри инициалов не должен добавляться
        self.assertNotIn("А. Б", result, f"Инициалы не должны разбиваться: {result!r}")


if __name__ == "__main__":
    unittest.main()
