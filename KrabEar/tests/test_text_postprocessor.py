"""Тесты для TextPostProcessor.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_text_postprocessor.py -v
"""

from core.text_postprocessor import (
    TextPostProcessor,
    PostProcessResult,
    PostProcessorStep,
    StripWhitespace,
    FixPunctuation,
    ExpandAbbreviations,
    Anonymize,
    NormalizeEntities,
    DEFAULT_CHAIN,
)
import unittest
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ── Вспомогательный шаг для тестов ──────────────────────────────────────────

class UpperCaseStep:
    """Тестовый шаг: переводит текст в верхний регистр."""
    name: str = "to_upper"

    def process(self, text: str) -> str:
        return text.upper()


class NoOpStep:
    """Тестовый шаг, который не изменяет текст."""
    name: str = "noop"

    def process(self, text: str) -> str:
        return text


class RaisingStep:
    """Тестовый шаг, который всегда выбрасывает исключение."""
    name: str = "raise_step"

    def process(self, text: str) -> str:
        raise RuntimeError("намеренная ошибка")


# ── Тесты PostProcessResult ──────────────────────────────────────────────────

class TestPostProcessResult(unittest.TestCase):

    def test_dataclass_defaults(self):
        result = PostProcessResult(text="hello")
        self.assertEqual(result.text, "hello")
        self.assertEqual(result.steps_applied, [])
        self.assertEqual(result.changes_count, 0)

    def test_dataclass_full(self):
        result = PostProcessResult(
            text="world",
            steps_applied=["strip_whitespace", "fix_punctuation"],
            changes_count=1,
        )
        self.assertEqual(result.text, "world")
        self.assertEqual(result.steps_applied, ["strip_whitespace", "fix_punctuation"])
        self.assertEqual(result.changes_count, 1)


# ── Тесты StripWhitespace ────────────────────────────────────────────────────

class TestStripWhitespace(unittest.TestCase):

    def setUp(self):
        self.step = StripWhitespace()

    def test_name(self):
        self.assertEqual(self.step.name, "strip_whitespace")

    def test_removes_leading_trailing_spaces(self):
        result = self.step.process("  hello  ")
        self.assertEqual(result, "hello")

    def test_collapses_multiple_spaces(self):
        result = self.step.process("hello   world")
        self.assertEqual(result, "hello world")

    def test_empty_string_unchanged(self):
        self.assertEqual(self.step.process(""), "")

    def test_normalizes_crlf(self):
        result = self.step.process("line1\r\nline2")
        self.assertNotIn("\r", result)
        self.assertIn("line1", result)
        self.assertIn("line2", result)

    def test_already_clean_unchanged(self):
        text = "чистый текст без лишних пробелов"
        self.assertEqual(self.step.process(text), text)


# ── Тесты FixPunctuation ─────────────────────────────────────────────────────

class TestFixPunctuation(unittest.TestCase):

    def setUp(self):
        self.step = FixPunctuation(language="ru")

    def test_name(self):
        self.assertEqual(self.step.name, "fix_punctuation")

    def test_capitalizes_first_letter(self):
        result = self.step.process("привет мир")
        self.assertTrue(result[0].isupper(), f"Первая буква должна быть заглавной: {result!r}")

    def test_adds_period_at_end(self):
        result = self.step.process("Привет мир")
        self.assertTrue(result.endswith("."), f"Должна быть точка в конце: {result!r}")

    def test_empty_unchanged(self):
        self.assertEqual(self.step.process(""), "")

    def test_spanish_adds_iquest(self):
        step = FixPunctuation(language="es")
        result = step.process("cómo estás?")
        self.assertTrue(result.lstrip().startswith("¿"), f"Должен быть ¿: {result!r}")


# ── Тесты ExpandAbbreviations ────────────────────────────────────────────────

class TestExpandAbbreviations(unittest.TestCase):

    def setUp(self):
        self.step = ExpandAbbreviations(language="ru")

    def test_name(self):
        self.assertEqual(self.step.name, "expand_abbreviations")

    def test_expands_known_abbreviation(self):
        result = self.step.process("т.е. это верно")
        self.assertIn("то есть", result.lower())

    def test_empty_unchanged(self):
        self.assertEqual(self.step.process(""), "")

    def test_no_abbreviation_unchanged(self):
        text = "текст без сокращений"
        result = self.step.process(text)
        self.assertEqual(result, text)


# ── Тесты NormalizeEntities ──────────────────────────────────────────────────

class TestNormalizeEntities(unittest.TestCase):

    def setUp(self):
        self.step = NormalizeEntities()

    def test_name(self):
        self.assertEqual(self.step.name, "normalize_entities")

    def test_replaces_cyrillic_brand(self):
        result = self.step.process("Я пользуюсь Телеграм каждый день")
        self.assertIn("Telegram", result)
        self.assertNotIn("Телеграм", result)

    def test_normalizes_time_format(self):
        result = self.step.process("встреча в 15.30")
        self.assertIn("15:30", result)

    def test_empty_unchanged(self):
        self.assertEqual(self.step.process(""), "")


# ── Тесты Anonymize ──────────────────────────────────────────────────────────

class TestAnonymize(unittest.TestCase):

    def setUp(self):
        self.step = Anonymize()

    def test_name(self):
        self.assertEqual(self.step.name, "anonymize")

    def test_redacts_phone(self):
        result = self.step.process("позвони на +7 999 123-45-67 завтра")
        self.assertNotIn("+7 999", result)
        self.assertIn("[ТЕЛЕФОН]", result)

    def test_redacts_email(self):
        result = self.step.process("пиши на user@example.com")
        self.assertNotIn("user@example.com", result)
        self.assertIn("[EMAIL]", result)

    def test_empty_unchanged(self):
        self.assertEqual(self.step.process(""), "")

    def test_no_pii_unchanged(self):
        text = "просто обычный текст без данных"
        self.assertEqual(self.step.process(text), text)


# ── Тесты TextPostProcessor ──────────────────────────────────────────────────

class TestTextPostProcessorBasic(unittest.TestCase):

    def setUp(self):
        self.processor = TextPostProcessor()

    def test_empty_string_returns_empty_result(self):
        result = self.processor.process("")
        self.assertIsInstance(result, PostProcessResult)
        self.assertEqual(result.text, "")
        self.assertEqual(result.steps_applied, [])
        self.assertEqual(result.changes_count, 0)

    def test_default_chain_applied_when_steps_none(self):
        result = self.processor.process("привет как дела")
        self.assertEqual(result.steps_applied, DEFAULT_CHAIN)

    def test_custom_steps_list(self):
        result = self.processor.process("  hello  ", steps=["strip_whitespace"])
        self.assertEqual(result.steps_applied, ["strip_whitespace"])
        self.assertEqual(result.text, "hello")

    def test_changes_count_increments_when_text_changes(self):
        result = self.processor.process("  много пробелов  ", steps=["strip_whitespace"])
        self.assertEqual(result.changes_count, 1)

    def test_changes_count_zero_when_no_change(self):
        step = NoOpStep()
        self.processor.register_step(step)
        result = self.processor.process("текст без изменений", steps=["noop"])
        self.assertEqual(result.changes_count, 0)

    def test_multiple_steps_applied_in_order(self):
        result = self.processor.process("  hello  ", steps=["strip_whitespace", "normalize_entities"])
        self.assertEqual(result.steps_applied, ["strip_whitespace", "normalize_entities"])
        self.assertEqual(result.text, "hello")

    def test_unknown_step_skipped_with_warning(self):
        # Не должно падать — неизвестный шаг пропускается
        result = self.processor.process("текст", steps=["nonexistent_step"])
        # Неизвестный шаг не добавляется в steps_applied
        self.assertNotIn("nonexistent_step", result.steps_applied)
        self.assertEqual(result.text, "текст")

    def test_returns_post_process_result_type(self):
        result = self.processor.process("тест")
        self.assertIsInstance(result, PostProcessResult)

    def test_steps_applied_is_list(self):
        result = self.processor.process("тест")
        self.assertIsInstance(result.steps_applied, list)

    def test_changes_count_is_int(self):
        result = self.processor.process("тест")
        self.assertIsInstance(result.changes_count, int)


class TestTextPostProcessorRegisterStep(unittest.TestCase):

    def setUp(self):
        self.processor = TextPostProcessor()

    def test_register_custom_step(self):
        step = UpperCaseStep()
        self.processor.register_step(step)
        result = self.processor.process("hello", steps=["to_upper"])
        self.assertEqual(result.text, "HELLO")

    def test_register_step_appears_in_list_steps(self):
        step = UpperCaseStep()
        self.processor.register_step(step)
        self.assertIn("to_upper", self.processor.list_steps())

    def test_register_step_overwrites_existing(self):
        class AlwaysAStep:
            name: str = "to_upper"

            def process(self, text: str) -> str:
                return "A"

        step = UpperCaseStep()
        override = AlwaysAStep()
        self.processor.register_step(step)
        self.processor.register_step(override)
        result = self.processor.process("hello", steps=["to_upper"])
        self.assertEqual(result.text, "A")

    def test_register_invalid_step_raises_type_error(self):
        with self.assertRaises(TypeError):
            self.processor.register_step("not_a_step")  # type: ignore

    def test_register_invalid_step_without_name_raises_type_error(self):
        class BadStep:
            def process(self, text: str) -> str:
                return text
        # Отсутствует name → не удовлетворяет протоколу
        with self.assertRaises(TypeError):
            self.processor.register_step(BadStep())  # type: ignore


class TestTextPostProcessorListSteps(unittest.TestCase):

    def setUp(self):
        self.processor = TextPostProcessor()

    def test_list_steps_returns_builtin_names(self):
        steps = self.processor.list_steps()
        self.assertIn("strip_whitespace", steps)
        self.assertIn("fix_punctuation", steps)
        self.assertIn("expand_abbreviations", steps)
        self.assertIn("anonymize", steps)
        self.assertIn("normalize_entities", steps)

    def test_list_steps_returns_list(self):
        self.assertIsInstance(self.processor.list_steps(), list)


class TestTextPostProcessorErrorHandling(unittest.TestCase):

    def setUp(self):
        self.processor = TextPostProcessor()

    def test_raising_step_does_not_abort_pipeline(self):
        self.processor.register_step(RaisingStep())
        # Должно не упасть и вернуть PostProcessResult
        result = self.processor.process("текст", steps=["raise_step"])
        self.assertIsInstance(result, PostProcessResult)
        # Шаг записывается как применённый (хоть и с ошибкой)
        self.assertIn("raise_step", result.steps_applied)

    def test_raising_step_followed_by_valid_step(self):
        self.processor.register_step(RaisingStep())
        result = self.processor.process("  текст  ", steps=["raise_step", "strip_whitespace"])
        # strip_whitespace должен выполниться и исправить пробелы
        self.assertEqual(result.text, "текст")
        self.assertIn("strip_whitespace", result.steps_applied)


class TestTextPostProcessorDefaultChain(unittest.TestCase):

    def setUp(self):
        self.processor = TextPostProcessor()

    def test_default_chain_is_correct_constant(self):
        self.assertEqual(DEFAULT_CHAIN, ["strip_whitespace", "fix_punctuation", "normalize_entities"])

    def test_default_chain_strips_and_capitalizes(self):
        result = self.processor.process("  привет мир  ")
        # StripWhitespace убирает пробелы, FixPunctuation капитализирует
        self.assertFalse(result.text.startswith(" "))
        self.assertFalse(result.text.endswith(" "))
        self.assertTrue(result.text[0].isupper())

    def test_empty_steps_list_uses_no_steps(self):
        # Явный пустой список ≠ None (None → DEFAULT_CHAIN)
        result = self.processor.process("текст", steps=[])
        self.assertEqual(result.steps_applied, [])
        self.assertEqual(result.text, "текст")
        self.assertEqual(result.changes_count, 0)


class TestTextPostProcessorChainComposition(unittest.TestCase):

    def setUp(self):
        self.processor = TextPostProcessor()

    def test_strip_then_fix_punctuation(self):
        result = self.processor.process(
            "  привет как дела  ",
            steps=["strip_whitespace", "fix_punctuation"],
        )
        self.assertFalse(result.text.startswith(" "))
        self.assertTrue(result.text[0].isupper())

    def test_entity_normalization_in_chain(self):
        result = self.processor.process(
            "Клод помогает с кодом в 15.00",
            steps=["normalize_entities"],
        )
        self.assertIn("Claude", result.text)
        self.assertIn("15:00", result.text)

    def test_custom_step_chained_with_builtin(self):
        step = UpperCaseStep()
        self.processor.register_step(step)
        result = self.processor.process(
            "  hello world  ",
            steps=["strip_whitespace", "to_upper"],
        )
        self.assertEqual(result.text, "HELLO WORLD")
        self.assertEqual(result.changes_count, 2)

    def test_anonymize_step_in_chain(self):
        result = self.processor.process(
            "позвони на +7 999 123-45-67 пожалуйста",
            steps=["anonymize"],
        )
        self.assertIn("[ТЕЛЕФОН]", result.text)
        self.assertNotIn("+7 999", result.text)


class TestTextPostProcessorProtocolCompliance(unittest.TestCase):
    """Проверяет, что встроенные шаги реализуют протокол PostProcessorStep."""

    def test_strip_whitespace_implements_protocol(self):
        self.assertIsInstance(StripWhitespace(), PostProcessorStep)

    def test_fix_punctuation_implements_protocol(self):
        self.assertIsInstance(FixPunctuation(), PostProcessorStep)

    def test_expand_abbreviations_implements_protocol(self):
        self.assertIsInstance(ExpandAbbreviations(), PostProcessorStep)

    def test_anonymize_implements_protocol(self):
        self.assertIsInstance(Anonymize(), PostProcessorStep)

    def test_normalize_entities_implements_protocol(self):
        self.assertIsInstance(NormalizeEntities(), PostProcessorStep)

    def test_custom_valid_step_implements_protocol(self):
        self.assertIsInstance(UpperCaseStep(), PostProcessorStep)


if __name__ == "__main__":
    unittest.main()
