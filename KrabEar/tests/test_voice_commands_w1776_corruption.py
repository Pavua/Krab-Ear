"""W1776: regression tests for the three transcript-CORRUPTION bugs in
`core/voice_commands.py`.

`VoiceCommandProcessor.process()` runs on EVERY STT transcript (engine.py:1099,
`voice_commands_enabled` defaults to True), so each of these silently corrupted
real user dictation.

HIGH 1 — homonym commands fired in NORMAL speech and mutated text. Strict mode
         (default) now gates ALL command words/phrases that are also ordinary
         nouns/verbs in RU/ES/EN.  Confirmed corruptions (must NOT mutate now):
             «это просто пробел между нами»  -> unchanged
             «поставь тире здесь»            -> unchanged
             «dame un espacio aquí»          -> unchanged
             «a new line of code»            -> unchanged
             «we made a new paragraph today» -> unchanged

HIGH 2 — `_delete_last_paragraph` / `_delete_last_sentence` did an all-or-nothing
         rfind: with no separator (the common single-paragraph case) they returned
         "" and DELETED THE WHOLE TRANSCRIPT.  Now a NO-OP: text preserved.

MED 3  — leading-space artifact when a capitalize/delete command is the first
         token; the final result is now stripped.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m unittest \
        KrabEar/tests/test_voice_commands_w1776_corruption.py -v
"""

import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.voice_commands import (  # noqa: E402
    VoiceCommandProcessor,
    _AMBIGUOUS_SINGLE_WORD_PATTERNS,
    _delete_last_paragraph,
    _delete_last_sentence,
)


def _make_strict_proc() -> VoiceCommandProcessor:
    """Процессор в строгом режиме (default, production behaviour)."""
    settings: dict = {
        "voice_commands_enabled": True,
        "voice_commands_languages": ["ru", "es", "en"],
        "voice_commands_strict_mode": True,
    }
    return VoiceCommandProcessor(settings_get=lambda k, d: settings.get(k, d))


def _make_lenient_proc() -> VoiceCommandProcessor:
    settings: dict = {
        "voice_commands_enabled": True,
        "voice_commands_languages": ["ru", "es", "en"],
        "voice_commands_strict_mode": False,
    }
    return VoiceCommandProcessor(settings_get=lambda k, d: settings.get(k, d))


# ---------------------------------------------------------------------------
# HIGH 1 — homonym commands no longer corrupt ordinary speech
# ---------------------------------------------------------------------------

class TestHigh1HomonymsNotMutated(unittest.TestCase):
    """The 5 confirmed production-damage phrases must pass through unchanged."""

    def setUp(self):
        self.proc = _make_strict_proc()

    def test_ru_probel_in_speech_unchanged(self):
        """«пробел» = обычное слово → не вставляет « » (раньше съедало слово)."""
        result = self.proc.process("это просто пробел между нами", language="ru")
        self.assertEqual(result, "это просто пробел между нами")

    def test_ru_tire_in_speech_unchanged(self):
        """«тире» = обычное слово → не вставляет « — »."""
        result = self.proc.process("поставь тире здесь", language="ru")
        self.assertEqual(result, "поставь тире здесь")

    def test_es_espacio_in_speech_unchanged(self):
        """«espacio» = обычное слово → не вставляет « » (раньше → «dame un aquí»)."""
        result = self.proc.process("dame un espacio aquí", language="es")
        self.assertEqual(result, "dame un espacio aquí")

    def test_en_new_line_in_speech_unchanged(self):
        """«new line» = обычная фраза → не вставляет «\\n» (раньше → «a\\nof code»)."""
        result = self.proc.process("a new line of code", language="en")
        self.assertEqual(result, "a new line of code")

    def test_en_new_paragraph_in_speech_unchanged(self):
        """«new paragraph» = обычная фраза → не вставляет «\\n\\n»."""
        result = self.proc.process("we made a new paragraph today", language="en")
        self.assertEqual(result, "we made a new paragraph today")

    # Extra homonyms gated in W1776
    def test_en_dash_in_speech_unchanged(self):
        result = self.proc.process("add a dash of salt", language="en")
        self.assertEqual(result, "add a dash of salt")

    def test_es_guion_largo_unchanged(self):
        result = self.proc.process("usa un guión largo aquí", language="es")
        self.assertEqual(result, "usa un guión largo aquí")

    def test_es_punto_y_aparte_unchanged(self):
        result = self.proc.process("ese es un punto y aparte importante", language="es")
        self.assertEqual(result, "ese es un punto y aparte importante")

    def test_ru_vosklicanie_unchanged(self):
        result = self.proc.process("это было восклицание радости", language="ru")
        self.assertEqual(result, "это было восклицание радости")

    def test_ru_tabulyaciya_unchanged(self):
        result = self.proc.process("включи табуляцию в редакторе", language="ru")
        self.assertEqual(result, "включи табуляцию в редакторе")


class TestHigh1RegistryCompleteness(unittest.TestCase):
    """Every newly-required homonym is present in the strict-mode skip set."""

    def test_required_homonyms_are_gated(self):
        required = {
            "пробел", "тире", "espacio", "dash", "new line", "new paragraph",
            "guión largo", "punto y aparte", "tabulación", "табуляция",
            "новая строка", "новый абзац", "nueva línea", "восклицание",
        }
        missing = required - set(_AMBIGUOUS_SINGLE_WORD_PATTERNS)
        self.assertEqual(missing, set(), msg=f"Не gated: {missing}")


class TestHigh1GenuineCommandsStillWork(unittest.TestCase):
    """Unambiguous commands and the explicit strict-mode cues still fire."""

    def setUp(self):
        self.proc = _make_strict_proc()

    def test_ru_zapyataya_still_fires(self):
        self.assertEqual(self.proc.process("привет запятая мир", language="ru"), "привет, мир")

    def test_ru_voprositelny_znak_still_fires(self):
        self.assertEqual(self.proc.process("как дела вопросительный знак", language="ru"), "как дела?")

    def test_en_question_mark_still_fires(self):
        self.assertEqual(self.proc.process("how are you question mark", language="en"), "how are you?")

    def test_en_em_dash_still_fires(self):
        """«em dash» — unambiguous form — still inserts « — » (only «dash» is gated)."""
        self.assertEqual(self.proc.process("one em dash two", language="en"), "one — two")

    def test_es_signo_de_interrogacion_still_fires(self):
        self.assertEqual(
            self.proc.process("cómo estás signo de interrogación", language="es"),
            "cómo estás?",
        )

    def test_homonyms_still_fire_in_lenient_mode(self):
        """The gated homonyms remain available when strict mode is OFF."""
        lp = _make_lenient_proc()
        self.assertEqual(lp.process("a new line of code", language="en"), "a\nof code")
        self.assertEqual(lp.process("это просто пробел между нами", language="ru"),
                         "это просто между нами")


# ---------------------------------------------------------------------------
# HIGH 2 — delete-last on single paragraph/sentence is a NO-OP (never empties)
# ---------------------------------------------------------------------------

class TestHigh2DeleteNoOp(unittest.TestCase):
    def setUp(self):
        self.proc = _make_strict_proc()

    def test_en_delete_last_paragraph_single_para_noop(self):
        """«delete last paragraph» on single-paragraph text → UNCHANGED, not ''."""
        result = self.proc.process("a b c delete last paragraph", language="en")
        self.assertEqual(result, "a b c")

    def test_ru_delete_last_paragraph_single_para_noop(self):
        """«удалить последний абзац» on single-paragraph text → UNCHANGED, not ''."""
        result = self.proc.process("a b c удалить последний абзац", language="ru")
        self.assertEqual(result, "a b c")

    def test_en_delete_last_sentence_single_sentence_noop(self):
        result = self.proc.process("only sentence here delete last sentence", language="en")
        self.assertEqual(result, "only sentence here")

    def test_delete_last_paragraph_helper_noop(self):
        self.assertEqual(_delete_last_paragraph("just one paragraph"), "just one paragraph")
        self.assertEqual(_delete_last_paragraph(""), "")

    def test_delete_last_sentence_helper_noop(self):
        self.assertEqual(_delete_last_sentence("no terminator here"), "no terminator here")
        self.assertEqual(_delete_last_sentence(""), "")

    def test_delete_last_paragraph_multi_still_deletes(self):
        """With a real paragraph boundary the delete still trims the last paragraph."""
        result = self.proc.process(
            "para one new paragraph para two delete last paragraph", language="en"
        )
        # «new paragraph» gated in strict mode → no \n\n boundary → no-op.
        self.assertEqual(result, "para one new paragraph para two")
        # In lenient mode «new paragraph» creates the boundary → last para removed.
        lenient = _make_lenient_proc().process(
            "para one new paragraph para two delete last paragraph", language="en"
        )
        self.assertEqual(lenient, "para one")

    def test_delete_last_sentence_multi_still_deletes(self):
        result = self.proc.process(
            "Sentence A. sentence B delete last sentence", language="en"
        )
        self.assertEqual(result, "Sentence A.")


# ---------------------------------------------------------------------------
# MED 3 — leading command produces no leading space; result is stripped
# ---------------------------------------------------------------------------

class TestMed3NoLeadingSpace(unittest.TestCase):
    def setUp(self):
        self.proc = _make_strict_proc()

    def test_ru_leading_capitalize_no_leading_space(self):
        """«большая буква» first → «Мир», not « Мир»."""
        result = self.proc.process("большая буква мир", language="ru")
        self.assertEqual(result, "Мир")
        self.assertFalse(result.startswith(" "))

    def test_en_leading_capitalize_no_leading_space(self):
        result = self.proc.process("capitalize next hello", language="en")
        self.assertEqual(result, "Hello")
        self.assertFalse(result.startswith(" "))

    def test_leading_delete_no_leading_space(self):
        """Delete as first token (nothing to delete) → no leading space artifact."""
        result = self.proc.process("delete last word hello world", language="en")
        self.assertFalse(result.startswith(" "))
        self.assertEqual(result, "hello world")

    def test_no_trailing_space_after_delete_noop(self):
        """The delete no-op must not leave a trailing space artifact."""
        result = self.proc.process("a b c delete last paragraph", language="en")
        self.assertFalse(result.endswith(" "))
        self.assertEqual(result, "a b c")


if __name__ == "__main__":
    unittest.main()
