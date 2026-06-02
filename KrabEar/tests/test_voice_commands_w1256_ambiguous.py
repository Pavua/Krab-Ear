"""Тесты W1256: удаление ambiguous однословных триггеров (W1251 F1+F2 HIGH).

Проверяет, что в строгом режиме (strict=True, default) слова-омонимы
НЕ вызывают подстановку знаков препинания, а составные команды
по-прежнему работают.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar/tests/test_voice_commands_w1256_ambiguous.py -v
"""

import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.voice_commands import (  # noqa: E402
    VoiceCommandProcessor,
    _VOICE_COMMANDS_STRICT_MODE,
)


def _make_strict_proc() -> VoiceCommandProcessor:
    """Процессор в строгом режиме (default, W1256 safe)."""
    settings: dict = {
        "voice_commands_enabled": True,
        "voice_commands_languages": ["ru", "es", "en"],
        "voice_commands_strict_mode": True,
    }
    return VoiceCommandProcessor(settings_get=lambda k, d: settings.get(k, d))


def _make_lenient_proc() -> VoiceCommandProcessor:
    """Процессор в нестрогом режиме (legacy, все однословные активны)."""
    settings: dict = {
        "voice_commands_enabled": True,
        "voice_commands_languages": ["ru", "es", "en"],
        "voice_commands_strict_mode": False,
    }
    return VoiceCommandProcessor(settings_get=lambda k, d: settings.get(k, d))


class TestModuleDefault(unittest.TestCase):
    """Проверяем, что модульная константа по умолчанию — strict=True."""

    def test_strict_mode_default_is_true(self):
        self.assertTrue(_VOICE_COMMANDS_STRICT_MODE)


# ---------------------------------------------------------------------------
# RU: «вопрос» больше не заменяется на «?» в строгом режиме
# ---------------------------------------------------------------------------

class TestRuVoprosNoLongerFiresCommand(unittest.TestCase):
    """W1251 F1: «вопрос» — обычное слово, НЕ должно превращаться в «?»."""

    def setUp(self):
        self.proc = _make_strict_proc()

    def test_ru_vopros_alone_no_fire(self):
        """«вопрос» в одиночку не должен подставить «?»."""
        result = self.proc.process("вопрос", language="ru")
        self.assertEqual(result, "вопрос")

    def test_ru_vopros_no_longer_fires_command(self):
        """Production damage example: «это важный вопрос» → должно остаться как есть."""
        result = self.proc.process("это важный вопрос", language="ru")
        self.assertEqual(result, "это важный вопрос",
                         "«вопрос» не должен превращаться в «?» в строгом режиме")

    def test_ru_vopros_mid_sentence(self):
        """«вопрос» в середине предложения не должен вставлять знак вопроса."""
        result = self.proc.process("это вопрос чести", language="ru")
        self.assertEqual(result, "это вопрос чести")

    def test_ru_voprositelny_znak_still_works(self):
        """Составная команда «вопросительный знак» по-прежнему работает."""
        result = self.proc.process("как дела вопросительный знак", language="ru")
        self.assertEqual(result, "как дела?")


# ---------------------------------------------------------------------------
# RU: «точка» больше не заменяется на «.» в строгом режиме
# ---------------------------------------------------------------------------

class TestRuTochkaInPhraseNoLongerFires(unittest.TestCase):
    """W1251 F2: «точка» — обычное слово (= dot/period/point), не должно → «.»."""

    def setUp(self):
        self.proc = _make_strict_proc()

    def test_ru_tochka_in_phrase_no_longer_fires(self):
        """«точка зрения» не должно вставлять точку."""
        result = self.proc.process("это важная точка зрения", language="ru")
        self.assertEqual(result, "это важная точка зрения")

    def test_ru_tochka_alone_no_fire(self):
        """Слово «точка» само по себе не должно превращаться в «.»."""
        result = self.proc.process("точка", language="ru")
        self.assertEqual(result, "точка")

    def test_ru_tochka_s_zapyatoy_still_works(self):
        """Составная команда «точка с запятой» по-прежнему работает."""
        result = self.proc.process("раз точка с запятой два", language="ru")
        self.assertEqual(result, "раз; два")


# ---------------------------------------------------------------------------
# EN: «period» больше не заменяется на «.» в строгом режиме
# ---------------------------------------------------------------------------

class TestEnPeriodWordNoLongerFires(unittest.TestCase):
    """«period» — медицинский/финансовый термин, не должно → «.» в strict mode."""

    def setUp(self):
        self.proc = _make_strict_proc()

    def test_en_period_word_no_longer_fires(self):
        """Production damage: «the period of time» не должно вставить точку."""
        result = self.proc.process("the period of time is long", language="en")
        self.assertEqual(result, "the period of time is long")

    def test_en_period_alone_no_fire(self):
        """«period» в одиночку не должен заменяться на «.»."""
        result = self.proc.process("period", language="en")
        self.assertEqual(result, "period")

    def test_en_full_stop_still_works(self):
        """Составная команда «full stop» по-прежнему работает."""
        result = self.proc.process("finished full stop", language="en")
        self.assertEqual(result, "finished.")


# ---------------------------------------------------------------------------
# EN: «colon» больше не заменяется на «:» в строгом режиме
# ---------------------------------------------------------------------------

class TestEnColonWordNoLongerFires(unittest.TestCase):
    """«colon» — медицинский термин (часть кишечника), не должно → «:» в strict mode."""

    def setUp(self):
        self.proc = _make_strict_proc()

    def test_en_colon_word_no_longer_fires(self):
        """Production damage: «colon cancer» не должно превращаться в «: cancer»."""
        result = self.proc.process("colon cancer is serious", language="en")
        self.assertEqual(result, "colon cancer is serious",
                         "«colon» не должен → «:» в строгом режиме")

    def test_en_colon_alone_no_fire(self):
        """«colon» в одиночку не должен превращаться в «:»."""
        result = self.proc.process("colon", language="en")
        self.assertEqual(result, "colon")

    def test_en_semicolon_still_works(self):
        """«semicolon» по-прежнему работает (однозначная команда)."""
        result = self.proc.process("one semicolon two", language="en")
        self.assertEqual(result, "one; two")


# ---------------------------------------------------------------------------
# EN: «tab» больше не заменяется на «\t» в строгом режиме
# ---------------------------------------------------------------------------

class TestEnTabWordNoLongerFires(unittest.TestCase):
    """«tab» — UI-термин (browser tab, keyboard tab), не должно → «\t» в strict mode."""

    def setUp(self):
        self.proc = _make_strict_proc()

    def test_en_tab_word_no_longer_fires(self):
        """Production damage: «switch to the tab» не должно вставить табуляцию."""
        result = self.proc.process("switch to the tab", language="en")
        self.assertEqual(result, "switch to the tab",
                         "«tab» не должен → «\\t» в строгом режиме")

    def test_en_tab_alone_no_fire(self):
        result = self.proc.process("tab", language="en")
        self.assertEqual(result, "tab")

    def test_en_comma_still_works(self):
        """«comma» по-прежнему работает (однозначная команда)."""
        result = self.proc.process("hello comma world", language="en")
        self.assertEqual(result, "hello, world")


# ---------------------------------------------------------------------------
# ES: «coma» больше не заменяется на «,» в строгом режиме
# ---------------------------------------------------------------------------

class TestEsComaWordNoLongerFires(unittest.TestCase):
    """«coma» = медицинская кома по-испански, не должно → «,» в strict mode."""

    def setUp(self):
        self.proc = _make_strict_proc()

    def test_es_coma_word_no_longer_fires(self):
        """Production damage: «paciente en coma grave» не должно → «paciente en, grave»."""
        result = self.proc.process("el paciente está en coma grave", language="es")
        self.assertEqual(result, "el paciente está en coma grave",
                         "«coma» не должен → «,» в строгом режиме")

    def test_es_coma_alone_no_fire(self):
        result = self.proc.process("coma", language="es")
        self.assertEqual(result, "coma")

    def test_es_punto_alone_no_fire(self):
        """«punto» (= point/dot) не должно → «.» в строгом режиме."""
        result = self.proc.process("el punto de vista", language="es")
        self.assertEqual(result, "el punto de vista")

    def test_es_dos_puntos_phrase_no_fire(self):
        """«dos puntos de» не должно вставить «:»."""
        result = self.proc.process("son dos puntos de acuerdo", language="es")
        self.assertEqual(result, "son dos puntos de acuerdo")


# ---------------------------------------------------------------------------
# Compound forms still work — RU, ES, EN
# ---------------------------------------------------------------------------

class TestCompoundFormsStillWorkRuEsEn(unittest.TestCase):
    """Убеждаемся, что все составные (multi-word) команды остаются рабочими."""

    def setUp(self):
        self.proc = _make_strict_proc()

    # RU
    def test_ru_voprositelny_znak(self):
        result = self.proc.process("Как дела вопросительный знак", language="ru")
        self.assertEqual(result, "Как дела?")

    def test_ru_vosklitsatelny_znak(self):
        result = self.proc.process("Ура восклицательный знак", language="ru")
        self.assertEqual(result, "Ура!")

    def test_ru_tochka_s_zapyatoy(self):
        result = self.proc.process("раз точка с запятой два", language="ru")
        self.assertEqual(result, "раз; два")

    def test_ru_novy_abzac_gated_in_strict(self):
        """W1776: «новый абзац» — ходовая фраза, в строгом режиме НЕ вставляет \\n\\n.

        Раньше «мы сделали новый абзац сегодня» молча превращалось в
        «мы сделали\\n\\nсегодня». Теперь strict-режим оставляет текст как есть.
        Команда по-прежнему доступна в lenient-режиме.
        """
        result = self.proc.process("первый абзац новый абзац второй", language="ru")
        self.assertEqual(result, "первый абзац новый абзац второй")
        lenient = _make_lenient_proc().process("первый абзац новый абзац второй", language="ru")
        self.assertEqual(lenient, "первый абзац\n\nвторой")

    def test_ru_novaya_stroka_gated_in_strict(self):
        """W1776: «новая строка» — ходовая фраза, в строгом режиме НЕ вставляет \\n."""
        result = self.proc.process("строка один новая строка строка два", language="ru")
        self.assertEqual(result, "строка один новая строка строка два")
        lenient = _make_lenient_proc().process("строка один новая строка строка два", language="ru")
        self.assertEqual(lenient, "строка один\nстрока два")

    def test_ru_bolshaya_bukva(self):
        result = self.proc.process("привет большая буква мир", language="ru")
        self.assertEqual(result, "привет Мир")

    def test_ru_verkhny_registr(self):
        result = self.proc.process("ok верхний регистр важно.", language="ru")
        self.assertIn("ВАЖНО", result)

    def test_ru_zapyataya_still_works(self):
        """«запятая» — unambiguous, должна остаться рабочей."""
        result = self.proc.process("Привет запятая мир", language="ru")
        self.assertEqual(result, "Привет, мир")

    # ES
    def test_es_punto_y_coma(self):
        result = self.proc.process("uno punto y coma dos", language="es")
        self.assertEqual(result, "uno; dos")

    def test_es_punto_y_aparte_gated_in_strict(self):
        """W1776: «punto y aparte» — обычный оборот, в строгом режиме НЕ вставляет \\n\\n."""
        result = self.proc.process("primer párrafo punto y aparte segundo", language="es")
        self.assertEqual(result, "primer párrafo punto y aparte segundo")
        lenient = _make_lenient_proc().process("primer párrafo punto y aparte segundo", language="es")
        self.assertEqual(lenient, "primer párrafo\n\nsegundo")

    def test_es_signo_de_exclamacion(self):
        result = self.proc.process("genial signo de exclamación", language="es")
        self.assertEqual(result, "genial!")

    def test_es_signo_de_interrogacion(self):
        result = self.proc.process("cómo estás signo de interrogación", language="es")
        self.assertEqual(result, "cómo estás?")

    def test_es_nueva_linea_gated_in_strict(self):
        """W1776: «nueva línea» — ходовая фраза («una nueva línea de productos»),
        в строгом режиме НЕ вставляет \\n."""
        result = self.proc.process("primera línea nueva línea segunda línea", language="es")
        self.assertEqual(result, "primera línea nueva línea segunda línea")
        lenient = _make_lenient_proc().process("primera línea nueva línea segunda línea", language="es")
        self.assertEqual(lenient, "primera línea\nsegunda línea")

    # EN
    def test_en_question_mark(self):
        result = self.proc.process("how are you question mark", language="en")
        self.assertEqual(result, "how are you?")

    def test_en_exclamation_mark(self):
        result = self.proc.process("great exclamation mark", language="en")
        self.assertEqual(result, "great!")

    def test_en_exclamation_point(self):
        result = self.proc.process("great exclamation point", language="en")
        self.assertEqual(result, "great!")

    def test_en_semicolon(self):
        result = self.proc.process("one semicolon two", language="en")
        self.assertEqual(result, "one; two")

    def test_en_new_paragraph_gated_in_strict(self):
        """W1776: «new paragraph» — ходовая фраза, в строгом режиме НЕ вставляет \\n\\n.

        Production damage: «we made a new paragraph today» → «we made a\\n\\ntoday».
        """
        result = self.proc.process("intro new paragraph body", language="en")
        self.assertEqual(result, "intro new paragraph body")
        lenient = _make_lenient_proc().process("intro new paragraph body", language="en")
        self.assertEqual(lenient, "intro\n\nbody")

    def test_en_new_line_gated_in_strict(self):
        """W1776: «new line» — ходовая фраза, в строгом режиме НЕ вставляет \\n.

        Production damage: «a new line of code» → «a\\nof code».
        """
        result = self.proc.process("first line new line second line", language="en")
        self.assertEqual(result, "first line new line second line")
        lenient = _make_lenient_proc().process("first line new line second line", language="en")
        self.assertEqual(lenient, "first line\nsecond line")

    def test_en_full_stop(self):
        result = self.proc.process("finished full stop", language="en")
        self.assertEqual(result, "finished.")

    def test_en_em_dash(self):
        result = self.proc.process("one em dash two", language="en")
        self.assertEqual(result, "one — two")

    def test_en_comma(self):
        result = self.proc.process("hello comma world", language="en")
        self.assertEqual(result, "hello, world")

    # Delete commands
    def test_ru_delete_last_word(self):
        result = self.proc.process("один два удалить последнее слово три", language="ru")
        self.assertEqual(result, "один три")

    def test_en_delete_last_word(self):
        result = self.proc.process("hello world delete last word bye", language="en")
        self.assertEqual(result, "hello bye")

    def test_es_borrar_ultima_palabra(self):
        result = self.proc.process("hola mundo borrar última palabra fin", language="es")
        self.assertEqual(result, "hola fin")


# ---------------------------------------------------------------------------
# Non-strict (legacy) mode: ambiguous single-word triggers work again
# ---------------------------------------------------------------------------

class TestLenientModeRestoresAmbiguousTriggers(unittest.TestCase):
    """Нестрогий режим (enabled=False) возвращает однословные триггеры."""

    def setUp(self):
        self.proc = _make_lenient_proc()

    def test_ru_tochka_fires_in_lenient_mode(self):
        result = self.proc.process("Всё хорошо точка", language="ru")
        self.assertEqual(result, "Всё хорошо.")

    def test_ru_vopros_fires_in_lenient_mode(self):
        result = self.proc.process("это важный вопрос", language="ru")
        self.assertEqual(result, "это важный?")

    def test_en_period_fires_in_lenient_mode(self):
        result = self.proc.process("all done period", language="en")
        self.assertEqual(result, "all done.")

    def test_en_colon_fires_in_lenient_mode(self):
        result = self.proc.process("note colon important", language="en")
        self.assertEqual(result, "note: important")

    def test_en_tab_fires_in_lenient_mode(self):
        result = self.proc.process("one tab two", language="en")
        self.assertEqual(result, "one\ttwo")

    def test_es_coma_fires_in_lenient_mode(self):
        result = self.proc.process("hola coma mundo", language="es")
        self.assertEqual(result, "hola, mundo")

    def test_es_punto_fires_in_lenient_mode(self):
        result = self.proc.process("muy bien punto", language="es")
        self.assertEqual(result, "muy bien.")


# ---------------------------------------------------------------------------
# set_voice_commands_strict_mode IPC toggle
# ---------------------------------------------------------------------------

class TestSetVoiceCommandsStrictModeIpc(unittest.TestCase):
    """Тест IPC-метода set_voice_commands_strict_mode."""

    def test_toggle_from_strict_to_lenient(self):
        proc = _make_strict_proc()
        # In strict mode: «вопрос» should NOT fire
        result_before = proc.process("это важный вопрос", language="ru")
        self.assertEqual(result_before, "это важный вопрос")

        # Toggle to non-strict
        proc.set_voice_commands_strict_mode(False)
        result_after = proc.process("это важный вопрос", language="ru")
        self.assertEqual(result_after, "это важный?")

    def test_toggle_from_lenient_to_strict(self):
        proc = _make_lenient_proc()
        # In lenient mode: «colon cancer» → «: cancer»
        result_before = proc.process("colon cancer", language="en")
        self.assertEqual(result_before, ": cancer")

        # Toggle to strict
        proc.set_voice_commands_strict_mode(True)
        result_after = proc.process("colon cancer", language="en")
        self.assertEqual(result_after, "colon cancer")

    def test_strict_true_keeps_compound_working(self):
        proc = _make_lenient_proc()
        proc.set_voice_commands_strict_mode(True)
        # Compound commands must still work after switching to strict
        result = proc.process("how are you question mark", language="en")
        self.assertEqual(result, "how are you?")

    def test_strict_false_enables_all(self):
        proc = _make_strict_proc()
        proc.set_voice_commands_strict_mode(False)
        result = proc.process("all done period", language="en")
        self.assertEqual(result, "all done.")


if __name__ == "__main__":
    unittest.main()
