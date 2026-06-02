"""Тесты для VoiceCommandProcessor.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_voice_commands.py -v
"""

import sys
import os
import time
import threading
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.voice_commands import VoiceCommandProcessor, _delete_last_sentence  # noqa: E402


def _make_proc(enabled: bool = True, languages=None) -> VoiceCommandProcessor:
    """Фабрика: создаёт процессор с заданными настройками.

    NOTE: strict_mode=False (legacy) so that tests covering single-word
    ambiguous triggers («точка», «period», «coma», etc.) continue to pass.
    These tests document the legacy behaviour, which remains available via
    non-strict mode. For strict-mode behaviour see test_voice_commands_w1256_ambiguous.py.
    """
    if languages is None:
        languages = ["ru", "es", "en"]
    settings = {
        "voice_commands_enabled": enabled,
        "voice_commands_languages": languages,
        # Legacy mode: all single-word triggers active (W1256: strict=True is the new default)
        "voice_commands_strict_mode": False,
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
    """VoiceCommandProcessor потокобезопасен при параллельном вызове process()."""

    def test_concurrent_process(self):
        """Несколько потоков вызывают process() одновременно — нет гонок/сбоев."""
        proc = _make_proc()
        errors: list[Exception] = []
        results: list[str] = []
        lock = threading.Lock()

        inputs = [
            ("Привет запятая мир", "ru"),
            ("hello comma world", "en"),
            ("hola coma mundo", "es"),
            ("Всё хорошо точка", "ru"),
            ("what question mark", "en"),
        ]
        expected = [
            "Привет, мир",
            "hello, world",
            "hola, mundo",
            "Всё хорошо.",
            "what?",
        ]

        def worker(text, lang, expected_result):
            try:
                res = proc.process(text, language=lang)
                with lock:
                    results.append((res, expected_result))
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(t, l, e))
            for (t, l), e in zip(inputs, expected)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)

        self.assertEqual(errors, [], msg=f"Exceptions in threads: {errors}")
        self.assertEqual(len(results), len(inputs))
        for actual, expected_result in results:
            self.assertEqual(actual, expected_result)


class TestLookaroundBoundary(unittest.TestCase):
    r"""W989 F1: lookaround (?<!\w)/(?!\w) vs \b boundary regression tests.

    \b fails when a word-char command is immediately preceded/followed by a
    non-word char that is itself non-word (e.g. «(точка)» — ')' is non-word,
    'а' is word → no \b at end; (?!\w) only checks the char after, not before).
    """

    def setUp(self):
        self.proc = _make_proc()

    def test_command_recognized_inside_parens_ru(self):
        """«(точка)» — команда «точка» внутри скобок распознаётся.

        F1 regression: trailing \\b не создавалась перед ')' (non-word),
        т.к. предыдущий символ 'а' — word-char; \\b ожидает W→NW границу.
        (?!\\w) работает корректно — просто проверяет, что следующий символ
        не является word-char.

        Процессор вставляет пробел после символа если есть продолжение текста,
        поэтому результат «(. )» (пробел перед «)»), а не «(.)».
        Главное — команда «точка» распознана (заменена на «.»), а не пропущена.
        """
        # «(точка)» — скобки не являются словом, команда внутри скобок
        result = self.proc.process("(точка)", language="ru")
        # Команда должна сработать — «точка» → «.»
        self.assertIn(".", result)
        self.assertNotIn("точка", result)
        # Структура: «(» + «.» (+ опциональный пробел) + «)»
        self.assertTrue(result.startswith("("), msg=f"Ожидали '(' в начале, получили: {result!r}")
        self.assertTrue(result.endswith(")"), msg=f"Ожидали ')' в конце, получили: {result!r}")

    def test_command_followed_by_dash(self):
        """«точка—» — команда «точка» перед тире распознаётся.

        Тире (—, U+2014) — non-word char. (?!\\w) корректно матчит.
        """
        result = self.proc.process("точка—продолжение", language="ru")
        # «точка» в начале строки перед «—»: (?<!\w) OK (начало строки),
        # (?!\w) OK (следующий — '—', non-word). Команда срабатывает.
        self.assertIn(".", result)
        self.assertNotIn("точка", result)

    def test_es_commands_no_duplicate(self):
        """W989 F2: _ES_COMMANDS не содержит дубликатов «nueva línea»."""
        from core.voice_commands import _ES_COMMANDS  # noqa: PLC0415

        # Считаем уникальные паттерны
        patterns = [p for p, _, _ in _ES_COMMANDS]
        duplicates = [p for p in set(patterns) if patterns.count(p) > 1]
        self.assertEqual(
            duplicates,
            [],
            msg=f"Дублирующиеся паттерны в _ES_COMMANDS: {duplicates}",
        )

    def test_es_nueva_linea_unique_count(self):
        """«nueva línea» встречается в _ES_COMMANDS ровно 1 раз (F2 dedup)."""
        from core.voice_commands import _ES_COMMANDS  # noqa: PLC0415

        count = sum(1 for p, _, _ in _ES_COMMANDS if p == r"nueva línea")
        self.assertEqual(count, 1, msg=f"Ожидалось 1 вхождение «nueva línea», нашли {count}")


class TestW1761ReDosRegression(unittest.TestCase):
    """W1761: регрессия ReDoS / квадратичный backtracking в _delete_last_sentence.

    Исходный re.search(r"[.!?\\n](?!.*[.!?\\n])") на длинной строке без
    терминатора в конце давал O(n²) backtracking — 30 000 слов без точки
    вешали single-thread backend на несколько секунд.

    Новая реализация (max rfind) — O(n), должна завершаться за < 0.2s.
    """

    def _make_proc_en(self) -> VoiceCommandProcessor:
        settings = {
            "voice_commands_enabled": True,
            "voice_commands_languages": ["en"],
            "voice_commands_strict_mode": False,
        }
        return VoiceCommandProcessor(settings_get=lambda k, d: settings.get(k, d))

    def test_delete_last_sentence_no_terminator_is_fast(self):
        """30 000 слов без завершающей точки — _delete_last_sentence < 0.2s (W1761).

        Тест вызывает _delete_last_sentence напрямую (минуя _apply_commands),
        чтобы изолировать именно regex-уязвимость: исходный lookahead «(?!.*[.!?\\n])»
        давал O(n²) на строке без терминатора.  rfind-реализация — O(n).

        Строка длиннее _MAX_INPUT_LEN — guard в process() сюда не применяется,
        т.к. мы обходим process().
        """
        # 180 000 символов без .!?\n — именно этот паттерн был квадратичным
        long_no_terminator = "word " * 30_000

        t0 = time.perf_counter()
        result = _delete_last_sentence(long_no_terminator)
        elapsed = time.perf_counter() - t0

        # W1776 HIGH 2: нет терминатора → no-op (возвращаем текст БЕЗ изменений),
        # а НЕ "" — раньше это молча стирало весь транскрипт.
        self.assertEqual(
            result,
            long_no_terminator,
            msg=f"Ожидался неизменённый текст (no-op), получили: {result[:80]!r}…",
        )
        # Производительность: rfind O(n) должен уложиться в 0.2s
        self.assertLess(
            elapsed,
            0.2,
            msg=f"ReDoS регрессия: _delete_last_sentence заняла {elapsed:.3f}s (лимит 0.2s)",
        )

    def test_delete_last_sentence_correctness_multi_sentence(self):
        """Корректность: команда удаляет хвост за последним терминатором."""
        proc = self._make_proc_en()
        # Паттерн: «Sentence A. sentence B trailing delete last sentence»
        # output перед командой = «Sentence A. sentence B trailing»
        # _delete_last_sentence находит «.» как последний терминатор →
        # возвращает «Sentence A.» (всё от начала до «.» включительно)
        text = "Sentence A. sentence B trailing delete last sentence"
        result = proc.process(text, language="en")
        self.assertIn("Sentence A.", result)
        self.assertNotIn("sentence B", result)
        self.assertNotIn("trailing", result)

    def test_delete_last_sentence_single_sentence_is_noop(self):
        """W1776 HIGH 2: единственное предложение без терминатора → NO-OP.

        Раньше «delete last sentence» на single-sentence тексте стирало весь
        транскрипт (`rfind` не находил терминатор → возвращался ""). Теперь
        накопленный текст сохраняется без изменений — стирать нечего.
        """
        proc = self._make_proc_en()
        text = "Only sentence here delete last sentence"
        result = proc.process(text, language="en")
        # Нет терминатора в накопленном output → текст сохраняется (no-op),
        # а НЕ стирается. Команда «съедена», но накопленный текст остаётся.
        self.assertEqual(result, "Only sentence here")

    def test_input_size_guard_skips_pathological_input(self):
        """W1761: входная строка > 100 000 символов возвращается без обработки."""
        from core.voice_commands import _MAX_INPUT_LEN  # noqa: PLC0415

        proc = self._make_proc_en()
        # Строка ровно на один символ длиннее лимита
        oversized = "a" * (_MAX_INPUT_LEN + 1)
        result = proc.process(oversized, language="en")
        # Процессор должен вернуть текст как есть
        self.assertEqual(result, oversized)


if __name__ == "__main__":
    unittest.main()
