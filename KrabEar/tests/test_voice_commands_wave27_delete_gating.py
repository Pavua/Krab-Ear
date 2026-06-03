"""wave-27: gate голых delete_last fallback-форм за strict_mode + position-check омонимов.

КОНТЕКСТ (HIGH silent data loss):
`VoiceCommandProcessor.process()` бежит на КАЖДОМ STT-транскрипте (engine.py:1101,
`voice_commands_enabled` defaults True, `voice_commands_strict_mode` defaults True —
т.е. production работает в strict-режиме).

Голые fallback-формы команд удаления — это перфектные омонимы обычной речи:
    «удалить последнее …»  (фраза обрывается / продолжается обычным словом)
    «delete last …»        («the delete last time we spoke»)
    «borrar último …»      («borrar último archivo» — обычный оборот)
Раньше они НЕ были gated → негейтнутая голая форма молча стирала реальную
диктовку. Теперь:
  1. strict-режим (production default): голые fallback-формы НЕ срабатывают —
     остаются буквальным текстом (как и прочие омонимы в _AMBIGUOUS_SINGLE_WORD_PATTERNS).
  2. lenient-режим: голые fallback-формы срабатывают ТОЛЬКО в конце высказывания
     (position-check) — «delete last» в середине речи трактуется как обычный текст.

Явные ПОЛНЫЕ формы («delete last sentence/word/paragraph», «удалить последнее
слово» и т.д.) — намеренные команды и срабатывают в ОБОИХ режимах как прежде.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_voice_commands_wave27_delete_gating.py -p no:xdist -q
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
    _DELETE_LAST_FALLBACK_PATTERNS,
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
    """Процессор в нестрогом (legacy) режиме."""
    settings: dict = {
        "voice_commands_enabled": True,
        "voice_commands_languages": ["ru", "es", "en"],
        "voice_commands_strict_mode": False,
    }
    return VoiceCommandProcessor(settings_get=lambda k, d: settings.get(k, d))


# ---------------------------------------------------------------------------
# Registry completeness: голые fallback-формы gated как омонимы
# ---------------------------------------------------------------------------

class TestRegistryGating(unittest.TestCase):
    """Голые delete_last fallback-формы зарегистрированы как ambiguous (strict-skip)."""

    def test_bare_fallbacks_are_ambiguous(self):
        required = {"удалить последнее", "borrar último", "delete last"}
        missing = required - set(_AMBIGUOUS_SINGLE_WORD_PATTERNS)
        self.assertEqual(missing, set(), msg=f"Не gated в strict-режиме: {missing}")

    def test_fallback_subset_defined(self):
        """_DELETE_LAST_FALLBACK_PATTERNS — подмножество ambiguous-набора."""
        self.assertTrue(
            _DELETE_LAST_FALLBACK_PATTERNS.issubset(_AMBIGUOUS_SINGLE_WORD_PATTERNS),
            msg="fallback-формы должны входить и в _AMBIGUOUS_SINGLE_WORD_PATTERNS",
        )
        self.assertEqual(
            _DELETE_LAST_FALLBACK_PATTERNS,
            {"удалить последнее", "borrar último", "delete last"},
        )


# ---------------------------------------------------------------------------
# STRICT MODE (production default): голые fallback-формы НЕ срабатывают
# ---------------------------------------------------------------------------

class TestStrictModeBareFallbackPreserved(unittest.TestCase):
    """В strict-режиме голая «delete last» в естественной речи → буквальный текст."""

    def setUp(self):
        self.proc = _make_strict_proc()

    def test_en_delete_last_in_speech_preserved(self):
        """«the delete last time we spoke» — не команда, текст сохраняется."""
        text = "the delete last time we spoke"
        self.assertEqual(self.proc.process(text, language="en"), text)

    def test_ru_udalit_poslednee_in_speech_preserved(self):
        """«надо удалить последнее сообщение» — НЕ должно стирать предыдущее слово."""
        text = "надо удалить последнее сообщение"
        self.assertEqual(self.proc.process(text, language="ru"), text)

    def test_es_borrar_ultimo_in_speech_preserved(self):
        """«quiero borrar último archivo» — обычный оборот, текст сохраняется."""
        text = "quiero borrar último archivo"
        self.assertEqual(self.proc.process(text, language="es"), text)

    def test_en_bare_delete_last_trailing_preserved_in_strict(self):
        """Даже в конце высказывания голая «delete last» в strict-режиме — НЕ команда.

        strict-режим намеренно консервативен: голая форма неоднозначна, поэтому
        не трогает накопленный текст. Намеренное удаление требует явной полной
        формы («delete last word»).
        """
        result = self.proc.process("keep this text delete last", language="en")
        self.assertEqual(result, "keep this text delete last")


# ---------------------------------------------------------------------------
# STRICT MODE: явные ПОЛНЫЕ формы по-прежнему срабатывают (как требует задача)
# ---------------------------------------------------------------------------

class TestStrictModeExplicitDeleteStillFires(unittest.TestCase):
    """«delete last sentence/word/paragraph» — намеренные команды, срабатывают в strict."""

    def setUp(self):
        self.proc = _make_strict_proc()

    def test_en_delete_last_sentence_fires_in_strict(self):
        """Задача: in strict mode «delete last sentence» → actually deletes."""
        result = self.proc.process("Sentence A. sentence B delete last sentence", language="en")
        self.assertEqual(result, "Sentence A.")

    def test_en_delete_last_word_fires_in_strict(self):
        result = self.proc.process("hello world delete last word", language="en")
        self.assertEqual(result, "hello")

    def test_ru_udalit_poslednee_slovo_fires_in_strict(self):
        result = self.proc.process("один два удалить последнее слово три", language="ru")
        self.assertEqual(result, "один три")

    def test_es_borrar_ultima_palabra_fires_in_strict(self):
        result = self.proc.process("hola mundo borrar última palabra fin", language="es")
        self.assertEqual(result, "hola fin")

    def test_en_delete_last_paragraph_single_para_noop_in_strict(self):
        """Single-paragraph → no-op (W1776 safety сохраняется), не стирает весь текст."""
        result = self.proc.process("a b c delete last paragraph", language="en")
        self.assertEqual(result, "a b c")


# ---------------------------------------------------------------------------
# LENIENT MODE: position-check — голая форма только в конце высказывания
# ---------------------------------------------------------------------------

class TestLenientModePositionCheck(unittest.TestCase):
    """В lenient-режиме голая «delete last» срабатывает ТОЛЬКО в конце высказывания."""

    def setUp(self):
        self.proc = _make_lenient_proc()

    def test_en_bare_delete_last_at_end_fires(self):
        """Голая «delete last» в конце → удаляет последнее слово (legacy-поведение)."""
        result = self.proc.process("hello world delete last", language="en")
        self.assertEqual(result, "hello")

    def test_en_bare_delete_last_mid_speech_preserved(self):
        """«the delete last time we spoke» — продолжение есть → НЕ команда (омоним)."""
        text = "the delete last time we spoke"
        self.assertEqual(self.proc.process(text, language="en"), text)

    def test_ru_bare_udalit_poslednee_mid_speech_preserved(self):
        """«удалить последнее сообщение» — за командой текст → омоним, сохраняем."""
        text = "надо удалить последнее сообщение"
        self.assertEqual(self.proc.process(text, language="ru"), text)

    def test_ru_bare_udalit_poslednee_at_end_fires(self):
        """«один два удалить последнее» в конце → удаляет «два» → «один»."""
        result = self.proc.process("один два удалить последнее", language="ru")
        self.assertEqual(result, "один")

    def test_es_bare_borrar_ultimo_mid_speech_preserved(self):
        text = "quiero borrar último archivo importante"
        self.assertEqual(self.proc.process(text, language="es"), text)

    def test_full_form_unaffected_by_position_check(self):
        """Полная форма «delete last word X» удаляет даже при продолжении (не fallback)."""
        result = self.proc.process("hello world delete last word bye", language="en")
        self.assertEqual(result, "hello bye")


# ---------------------------------------------------------------------------
# Регрессия: омонимы пунктуации в естественной речи (MED) — strict-режим
# ---------------------------------------------------------------------------

class TestPunctuationHomonymsStrict(unittest.TestCase):
    """Подтверждаем: пунктуационные омонимы в strict-режиме не мутируют речь."""

    def setUp(self):
        self.proc = _make_strict_proc()

    def test_en_full_stop_at_end_still_fires(self):
        """Намеренная «full stop» в конце — по-прежнему вставляет «.» (не gated)."""
        self.assertEqual(self.proc.process("finished full stop", language="en"), "finished.")

    def test_en_period_homonym_in_speech_preserved(self):
        """«the period of time is long» — омоним, не вставляет «.» (strict)."""
        text = "the period of time is long"
        self.assertEqual(self.proc.process(text, language="en"), text)


if __name__ == "__main__":
    unittest.main()
