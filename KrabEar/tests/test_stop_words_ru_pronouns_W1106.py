"""Tests for RU oblique pronouns and который paradigm in stop_words (W1106).

W1095 F2 MED — verifies that 16 personal pronoun oblique forms + 10 который
forms were added to the RU stop-word set, and that the existing set is intact.
"""
from __future__ import annotations

import ast
import os
import sys
import unittest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.stop_words import StopWords  # noqa: E402


class TestRuObliquePronounsInStopWords(unittest.TestCase):
    """Assert all 25 personal pronoun oblique forms are present (W1106)."""

    PERSONAL_OBLIQUE = [
        # 1st person singular
        "меня", "мне", "мной", "мною",
        # 2nd person singular
        "тебя", "тебе", "тобой", "тобою",
        # 3rd person singular masculine/neuter accusative/genitive
        "его", "ему", "им", "ним",
        # 3rd person singular feminine
        "неё", "ней", "ею", "нею",
        # 1st person plural
        "нас", "нам", "нами",
        # 2nd person plural
        "вас", "вам", "вами",
        # 3rd person plural
        "их", "ими", "ними",
    ]

    def test_ru_oblique_pronouns_in_stop_words(self) -> None:
        ru_set = StopWords.get_stop_words("ru")
        missing = [w for w in self.PERSONAL_OBLIQUE if w not in ru_set]
        self.assertFalse(
            missing,
            f"Missing personal pronoun oblique forms in RU stop-words: {missing}",
        )

    def test_oblique_pronouns_via_is_stop_word(self) -> None:
        """StopWords.is_stop_word() must recognise each form."""
        for word in self.PERSONAL_OBLIQUE:
            with self.subTest(word=word):
                self.assertTrue(
                    StopWords.is_stop_word(word, "ru"),
                    f"is_stop_word({word!r}, 'ru') returned False",
                )


class TestKotoryiParadigmInStopWords(unittest.TestCase):
    """Assert all 10 который paradigm forms are present (W1106)."""

    KOTORYJ_FORMS = [
        "которого",
        "которому",
        "которым",
        "котором",
        "которая",
        "которой",
        "которую",
        "которые",
        "которых",
        "которыми",
    ]

    def test_kotoryj_paradigm_in_stop_words(self) -> None:
        ru_set = StopWords.get_stop_words("ru")
        missing = [w for w in self.KOTORYJ_FORMS if w not in ru_set]
        self.assertFalse(
            missing,
            f"Missing который paradigm forms in RU stop-words: {missing}",
        )

    def test_kotoryj_via_is_stop_word(self) -> None:
        for word in self.KOTORYJ_FORMS:
            with self.subTest(word=word):
                self.assertTrue(
                    StopWords.is_stop_word(word, "ru"),
                    f"is_stop_word({word!r}, 'ru') returned False",
                )


class TestExistingRuStopWordsUnchanged(unittest.TestCase):
    """Regression guard — core existing RU stop words must still be present."""

    CORE_WORDS = [
        # Предлоги
        "в", "на", "с", "по", "из", "от",
        # Союзы
        "и", "а", "но", "что", "как",
        # Частицы
        "не", "бы", "же",
        # Местоимения (base forms already present before W1106)
        "он", "она", "оно", "они", "мы", "вы", "я",
        # Глаголы
        "быть", "был", "была", "были",
        # Наречия
        "там", "здесь", "очень",
        # Прочее
        "можно", "нужно",
    ]

    def test_existing_ru_stop_words_unchanged(self) -> None:
        ru_set = StopWords.get_stop_words("ru")
        missing = [w for w in self.CORE_WORDS if w not in ru_set]
        self.assertFalse(
            missing,
            f"Regression: previously-present RU stop words now missing: {missing}",
        )

    def test_ru_set_is_frozenset(self) -> None:
        ru_set = StopWords.get_stop_words("ru")
        self.assertIsInstance(ru_set, frozenset)

    def test_supported_languages_includes_ru(self) -> None:
        self.assertIn("ru", StopWords.supported_languages())

    def test_ast_comment_blocks_present(self) -> None:
        """Verify the W1106 comment blocks exist in source via AST+grep."""
        source_path = os.path.join(
            os.path.dirname(_HERE), "core", "stop_words.py"
        )
        with open(source_path, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn(
            "Personal pronouns oblique forms (W1106)",
            source,
            "Comment block 'Personal pronouns oblique forms (W1106)' not found",
        )
        self.assertIn(
            'Relative pronoun "который" paradigm (W1106)',
            source,
            'Comment block \'Relative pronoun "который" paradigm (W1106)\' not found',
        )

    def test_ast_parses_cleanly(self) -> None:
        """stop_words.py must parse without syntax errors."""
        source_path = os.path.join(
            os.path.dirname(_HERE), "core", "stop_words.py"
        )
        with open(source_path, encoding="utf-8") as fh:
            source = fh.read()
        try:
            ast.parse(source)
        except SyntaxError as exc:
            self.fail(f"stop_words.py has syntax errors: {exc}")


if __name__ == "__main__":
    unittest.main()
