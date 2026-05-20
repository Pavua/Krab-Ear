"""Тесты для VoiceCommandProcessor.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_voice_commands.py -v
"""

import sys
import os
import threading
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.voice_commands import VoiceCommandProcessor  # noqa: E402


def _make_proc(enabled: bool = True, languages=None) -> VoiceCommandProcessor:
    """Фабрика: создаёт процессор с заданными настройками."""
    if languages is None:
        languages = ["ru", "es", "en"]
    settings = {
        "voice_commands_enabled": enabled,
        "voice_commands_languages": languages,
    }
    return VoiceCommandProcessor(settings_get=lambda k, d: settings.get(k, d))


class TestRussianCommands(unittest.TestCase):
    """Тесты русских голосовых команд."""

    def setUp(self):
        self.proc = _make_proc()

    # --- Базовые знаки препинания ---

    def test_zapyataya_basic(self):
        """«запятая» → «,»"""
        result = self.proc.process("Привет запятая мир", language="ru")
        self.assertEqual(result, "Привет, мир")

    def test_tochka_basic(self):
        """«точка» → «.»"""
        result = self.proc.process("Всё хорошо точка", language="ru")
        self.assertEqual(result, "Всё хорошо.")

    def test_tochka_s_zapyatoy(self):
        """«точка с запятой» → «;» (составная команда)."""
        result = self.proc.process("раз точка с запятой два", language="ru")
        self.assertEqual(result, "раз; два")

    def test_dvotochie(self):
        """«двоеточие» → «:»"""
        result = self.proc.process("результат двоеточие победа", language="ru")
        self.assertEqual(result, "результат: победа")

    def test_tire(self):
        """«тире» → « — »"""
        result = self.proc.process("вход тире выход", language="ru")
        self.assertEqual(result, "вход — выход")

    def test_vosklitsatelny_znak(self):
        """«восклицательный знак» → «!»"""
        result = self.proc.process("Ура восклицательный знак", language="ru")
        self.assertEqual(result, "Ура!")

    def test_voskritsanie_short(self):
        """«восклицание» → «!» (короткий вариант)."""
        result = self.proc.process("Победа восклицание", language="ru")
        self.assertEqual(result, "Победа!")

    def test_voprositelny_znak(self):
        """«вопросительный знак» → «?»"""
        result = self.proc.process("Как дела вопросительный знак", language="ru")
        self.assertEqual(result, "Как дела?")

    def test_novaya_stroka(self):
        """«новая строка» → «\n»"""
        result = self.proc.process("первая строка новая строка вторая строка", language="ru")
        self.assertEqual(result, "первая строка\nвторая строка")

    def test_novy_abzac(self):
        """«новый абзац» → «\n\n»"""
        result = self.proc.process("первый абзац новый абзац второй абзац", language="ru")
        self.assertEqual(result, "первый абзац\n\nвторой абзац")

    # --- Составная команда: точка + новая строка ---

    def test_tochka_novaya_stroka(self):
        """«точка новая строка» → «.\n» следующее предложение."""
        result = self.proc.process("Точка новая строка следующее предложение", language="ru")
        # Точка с заглавной буквы в начале = просто "Точка." → но "Точка" → "."
        # Значит: "." + "\n" + "следующее предложение"
        self.assertIn(".\n", result)
        self.assertIn("следующее предложение", result)

    # --- Удаление ---

    def test_udalit_poslednee_slovo(self):
        """«удалить последнее слово» удаляет последнее слово."""
        result = self.proc.process("Привет восклицательный знак удалить последнее слово", language="ru")
        # "Привет!" затем удаляем последнее слово "Привет!"
        # Результат: ""
        # Логика: output="Привет!", delete_last("word", "Привет!") → ""
        self.assertEqual(result, "")

    def test_udalit_poslednee_slovo_partial(self):
        """«удалить последнее слово» удаляет только последнее слово."""
        result = self.proc.process("Первое слово второе слово удалить последнее слово", language="ru")
        self.assertEqual(result, "Первое слово второе")

    def test_udalit_poslednee_predlozhenie(self):
        """«удалить последнее предложение» — удаляет всё после последней точки."""
        result = self.proc.process("Первое предложение. удалить последнее предложение", language="ru")
        self.assertEqual(result, "Первое предложение.")

    # --- Регистр ---

    def test_bolshaya_bukva(self):
        """«большая буква» капитализирует следующее слово."""
        result = self.proc.process("привет большая буква мир", language="ru")
        self.assertIn("М", result)  # "мир" → "Мир"
        self.assertEqual(result, "привет Мир")

    def test_caps_verkhniy_registr(self):
        """«верхний регистр» → следующее предложение в UPPERCASE."""
        result = self.proc.process("ok верхний регистр важно.", language="ru")
        self.assertIn("ВАЖНО", result)

    # --- Edge cases ---

    def test_command_at_start(self):
        """Команда в начале текста."""
        result = self.proc.process("запятая мир", language="ru")
        self.assertEqual(result, ", мир")

    def test_command_at_end(self):
        """Команда в конце текста."""
        result = self.proc.process("привет запятая", language="ru")
        self.assertEqual(result, "привет,")

    def test_multiple_consecutive_commands(self):
        """Несколько команд подряд."""
        result = self.proc.process("раз запятая два точка три", language="ru")
        self.assertEqual(result, "раз, два. три")

    def test_word_boundary_preserved(self):
        """Подстрока команды внутри слова НЕ обрабатывается."""
        # «запятой» не является целым словом «запятая»
        result = self.proc.process("в запятой строке", language="ru")
        self.assertEqual(result, "в запятой строке")

    def test_tabulation(self):
        """«табуляция» → «\t»"""
        result = self.proc.process("раз табуляция два", language="ru")
        self.assertEqual(result, "раз\tдва")

    def test_probel(self):
        """«пробел» → « »"""
        no_boundary = self.proc.process("ракетапробелполёт", language="ru")
        # "пробел" — не whole-word в этом случае (нет пробелов вокруг), не обрабатывается
        self.assertEqual(no_boundary, "ракетапробелполёт")
        result2 = self.proc.process("ракета пробел полёт", language="ru")
        # "пробел" whole-word → заменяем пробелом
        self.assertIn("ракета", result2)
        self.assertIn("полёт", result2)


class TestSpanishCommands(unittest.TestCase):
    """Тесты испанских голосовых команд."""

    def setUp(self):
        self.proc = _make_proc()

    def test_coma(self):
        """«coma» → «,»"""
        result = self.proc.process("hola coma mundo", language="es")
        self.assertEqual(result, "hola, mundo")

    def test_punto(self):
        """«punto» → «.»"""
        result = self.proc.process("muy bien punto", language="es")
        self.assertEqual(result, "muy bien.")

    def test_punto_y_coma(self):
        """«punto y coma» → «;» (составная)."""
        result = self.proc.process("uno punto y coma dos", language="es")
        self.assertEqual(result, "uno; dos")

    def test_nueva_linea(self):
        """«nueva línea» → «\n»"""
        result = self.proc.process("primera línea nueva línea segunda línea", language="es")
        self.assertEqual(result, "primera línea\nsegunda línea")

    def test_punto_y_aparte(self):
        """«punto y aparte» → «\n\n»"""
        result = self.proc.process("primer párrafo punto y aparte segundo párrafo", language="es")
        self.assertEqual(result, "primer párrafo\n\nsegundo párrafo")

    def test_mayuscula(self):
        """«mayúscula» → следующее слово с заглавной."""
        result = self.proc.process("hola mayúscula mundo", language="es")
        self.assertIn("M", result)
        self.assertEqual(result, "hola Mundo")

    def test_borrar_ultima_palabra(self):
        """«borrar última palabra» удаляет последнее слово."""
        result = self.proc.process("hola mundo borrar última palabra", language="es")
        self.assertEqual(result, "hola")


class TestEnglishCommands(unittest.TestCase):
    """Тесты английских голосовых команд."""

    def setUp(self):
        self.proc = _make_proc()

    def test_comma(self):
        """«comma» → «,»"""
        result = self.proc.process("hello comma world", language="en")
        self.assertEqual(result, "hello, world")

    def test_period(self):
        """«period» → «.»"""
        result = self.proc.process("all done period", language="en")
        self.assertEqual(result, "all done.")

    def test_full_stop(self):
        """«full stop» → «.»"""
        result = self.proc.process("finished full stop", language="en")
        self.assertEqual(result, "finished.")

    def test_semicolon(self):
        """«semicolon» → «;»"""
        result = self.proc.process("one semicolon two", language="en")
        self.assertEqual(result, "one; two")

    def test_new_line(self):
        """«new line» → «\n»"""
        result = self.proc.process("first line new line second line", language="en")
        self.assertEqual(result, "first line\nsecond line")

    def test_new_paragraph(self):
        """«new paragraph» → «\n\n»"""
        result = self.proc.process("intro new paragraph body", language="en")
        self.assertEqual(result, "intro\n\nbody")

    def test_exclamation_point(self):
        """«exclamation point» → «!»"""
        result = self.proc.process("great exclamation point", language="en")
        self.assertEqual(result, "great!")

    def test_question_mark(self):
        """«question mark» → «?»"""
        result = self.proc.process("what question mark", language="en")
        self.assertEqual(result, "what?")

    def test_capitalize_next(self):
        """«capitalize next» капитализирует следующее слово."""
        result = self.proc.process("say capitalize next hello", language="en")
        self.assertEqual(result, "say Hello")

    def test_delete_last_word(self):
        """«delete last word» удаляет последнее слово."""
        result = self.proc.process("hello world delete last word", language="en")
        self.assertEqual(result, "hello")

    def test_delete_last_sentence(self):
        """«delete last sentence» удаляет после последней точки."""
        result = self.proc.process("keep this. delete this delete last sentence", language="en")
        self.assertEqual(result, "keep this.")

    def test_colon(self):
        """«colon» → «:»"""
        result = self.proc.process("note colon important", language="en")
        self.assertEqual(result, "note: important")


class TestCodeSwitching(unittest.TestCase):
    """Команды применяются только для заявленного языка."""

    def test_ru_command_not_applied_for_en(self):
        """Русская команда не срабатывает при language='en'."""
        proc = _make_proc()
        result = proc.process("Привет запятая мир", language="en")
        # «запятая» — не английская команда, остаётся как есть
        self.assertEqual(result, "Привет запятая мир")

    def test_en_command_not_applied_for_ru(self):
        """Английская команда не срабатывает при language='ru'."""
        proc = _make_proc()
        result = proc.process("hello comma world", language="ru")
        # «comma» — не русская команда, остаётся как есть
        self.assertEqual(result, "hello comma world")

    def test_language_not_in_allowed_list(self):
        """Язык вне allowed_languages → без изменений."""
        proc = _make_proc(languages=["ru"])
        result = proc.process("hello comma world", language="en")
        self.assertEqual(result, "hello comma world")


class TestDisabledFlag(unittest.TestCase):
    """При voice_commands_enabled=False — никаких преобразований."""

    def test_disabled_no_transform(self):
        proc = _make_proc(enabled=False)
        result = proc.process("Привет запятая мир", language="ru")
        self.assertEqual(result, "Привет запятая мир")

    def test_disabled_en_no_transform(self):
        proc = _make_proc(enabled=False)
        result = proc.process("hello comma world", language="en")
        self.assertEqual(result, "hello comma world")


class TestEdgeCases(unittest.TestCase):
    """Граничные случаи."""

    def test_empty_string(self):
        proc = _make_proc()
        self.assertEqual(proc.process("", language="ru"), "")

    def test_only_command(self):
        """Только команда без текста."""
        proc = _make_proc()
        result = proc.process("запятая", language="ru")
        self.assertEqual(result, ",")

    def test_punctuation_inside_word_not_triggered(self):
        """«запятая» внутри составного слова не срабатывает (whole-word boundary)."""
        proc = _make_proc()
        # "незапятаянный" — нет boundary, не должно срабатывать
        result = proc.process("незапятаянный", language="ru")
        self.assertEqual(result, "незапятаянный")

    def test_lang_with_region_code(self):
        """Язык вида 'ru-RU' нормализуется до 'ru'."""
        proc = _make_proc()
        result = proc.process("Привет запятая мир", language="ru-RU")
        self.assertEqual(result, "Привет, мир")

    def test_no_double_space_after_insert(self):
        """После вставки символа не должно быть двойного пробела."""
        proc = _make_proc()
        result = proc.process("раз запятая два", language="ru")
        self.assertNotIn("  ", result)
        self.assertEqual(result, "раз, два")

    def test_multiple_commands_no_gaps(self):
        """Несколько вставок подряд без лишних пробелов."""
        proc = _make_proc()
        result = proc.process("раз запятая два запятая три точка", language="ru")
        self.assertEqual(result, "раз, два, три.")

    def test_delete_last_word_empty_then_no_error(self):
        """Удаление последнего слова из пустого текста не падает."""
        proc = _make_proc()
        result = proc.process("удалить последнее слово", language="ru")
        # Нечего удалять — результат пустой
        self.assertEqual(result, "")

    def test_delete_then_continue(self):
        """Удаление с последующим продолжением."""
        proc = _make_proc()
        result = proc.process("один два удалить последнее слово три", language="ru")
        # "один два" → delete "два" → "один" → continue " три" → "один три"
        self.assertEqual(result, "один три")


class TestConcurrentProcess(unittest.TestCase):
    """VoiceCommandProcessor потокобезопасен при параллельных вызовах process()."""

    def test_concurrent_process_ru(self):
        """Параллельные вызовы process() для RU возвращают корректные результаты."""
        proc = _make_proc()
        errors: list[Exception] = []
        results: list[str] = []

        def worker() -> None:
            try:
                r = proc.process("Привет запятая мир", language="ru")
                results.append(r)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent process errors: {errors}")
        self.assertEqual(len(results), 20)
        for r in results:
            self.assertEqual(r, "Привет, мир")

    def test_concurrent_process_mixed_languages(self):
        """Параллельный вызов process() для разных языков не вызывает гонки."""
        proc = _make_proc()
        errors: list[Exception] = []

        def ru_worker() -> None:
            for _ in range(10):
                try:
                    r = proc.process("раз запятая два точка три", language="ru")
                    assert r == "раз, два. три", f"RU mismatch: {r!r}"
                except Exception as exc:
                    errors.append(exc)

        def en_worker() -> None:
            for _ in range(10):
                try:
                    r = proc.process("hello comma world", language="en")
                    assert r == "hello, world", f"EN mismatch: {r!r}"
                except Exception as exc:
                    errors.append(exc)

        def es_worker() -> None:
            for _ in range(10):
                try:
                    r = proc.process("hola coma mundo", language="es")
                    assert r == "hola, mundo", f"ES mismatch: {r!r}"
                except Exception as exc:
                    errors.append(exc)

        threads = (
            [threading.Thread(target=ru_worker) for _ in range(3)]
            + [threading.Thread(target=en_worker) for _ in range(3)]
            + [threading.Thread(target=es_worker) for _ in range(3)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Mixed-lang concurrent errors: {errors}")


if __name__ == "__main__":
    unittest.main()
