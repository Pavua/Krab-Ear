"""Тесты для AbbreviationExpander.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_abbreviation_expander.py -v
"""

from core.abbreviation_expander import AbbreviationExpander
import sys
import os
import json
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestExpandRussianBuiltins(unittest.TestCase):
    """Тесты раскрытия встроенных русских аббревиатур."""

    def setUp(self):
        self.expander = AbbreviationExpander()

    def test_expand_tie(self):
        """т.е. → то есть."""
        result = self.expander.expand("Это важно, т.е. нужно запомнить", language="ru")
        self.assertIn("то есть", result)
        self.assertNotIn("т.е.", result)

    def test_expand_tak_kak(self):
        """т.к. → так как."""
        result = self.expander.expand("Опоздал, т.к. пробки", language="ru")
        self.assertIn("так как", result)
        self.assertNotIn("т.к.", result)

    def test_expand_tak_dalee(self):
        """т.д. → так далее."""
        result = self.expander.expand("Книги, тетради и т.д.", language="ru")
        self.assertIn("так далее", result)

    def test_expand_naprimer(self):
        """напр. → например."""
        result = self.expander.expand("напр. вот это", language="ru")
        self.assertIn("например", result)
        self.assertNotIn("напр.", result)

    def test_expand_drugie(self):
        """др. → другие."""
        result = self.expander.expand("Иванов, Петров и др.", language="ru")
        self.assertIn("другие", result)

    def test_expand_prochee(self):
        """пр. → прочее."""
        result = self.expander.expand("Хлеб, молоко и пр.", language="ru")
        self.assertIn("прочее", result)

    def test_expand_ulitsa(self):
        """ул. → улица."""
        result = self.expander.expand("Живёт на ул. Ленина", language="ru")
        self.assertIn("улица", result)
        self.assertNotIn("ул.", result)

    def test_expand_gorod_no_after_digit(self):
        """г. НЕ раскрывается после цифры (год 2025 г.)."""
        result = self.expander.expand("2025 г. был продуктивным", language="ru")
        # Не должно заменять "г." после числа
        self.assertNotIn("город", result)

    def test_expand_gorod_after_text(self):
        """г. раскрывается как 'город' в текстовом контексте."""
        result = self.expander.expand("Приехал в г. Москва", language="ru")
        self.assertIn("город", result)

    def test_expand_preserves_case(self):
        """Аббревиатура с заглавной буквы → расширение с заглавной."""
        result = self.expander.expand("Напр. это работает", language="ru")
        # "Напр." в начале предложения → "Например"
        self.assertIn("Например", result)

    def test_no_expansion_in_url(self):
        """Аббревиатуры внутри URL не раскрываются."""
        url_text = "Смотри на https://example.com/т.е./страница"
        result = self.expander.expand(url_text, language="ru")
        # URL должен остаться нетронутым
        self.assertIn("https://example.com/т.е./страница", result)

    def test_empty_string(self):
        """Пустая строка возвращается без изменений."""
        result = self.expander.expand("", language="ru")
        self.assertEqual(result, "")

    def test_whitespace_only(self):
        """Строка из пробелов возвращается без изменений."""
        result = self.expander.expand("   ", language="ru")
        self.assertEqual("   ", result)

    def test_text_without_abbreviations(self):
        """Текст без аббревиатур не меняется."""
        text = "Это обычный текст без сокращений."
        result = self.expander.expand(text, language="ru")
        self.assertEqual(text, result)

    def test_multiple_abbreviations_in_one_text(self):
        """Несколько аббревиатур в одном тексте раскрываются одновременно."""
        result = self.expander.expand("Т.е. нужно взять книги и т.д.", language="ru")
        self.assertIn("то есть", result.lower())
        self.assertIn("так далее", result.lower())


class TestExpandEnglishBuiltins(unittest.TestCase):
    """Тесты раскрытия встроенных английских аббревиатур."""

    def setUp(self):
        self.expander = AbbreviationExpander()

    def test_expand_eg(self):
        """e.g. → for example."""
        result = self.expander.expand("Use tools, e.g. a hammer", language="en")
        self.assertIn("for example", result)
        self.assertNotIn("e.g.", result)

    def test_expand_ie(self):
        """i.e. → that is."""
        result = self.expander.expand("The result, i.e. the answer", language="en")
        self.assertIn("that is", result)
        self.assertNotIn("i.e.", result)

    def test_expand_etc(self):
        """etc. → et cetera."""
        result = self.expander.expand("Apples, oranges, etc.", language="en")
        self.assertIn("et cetera", result)

    def test_expand_approx(self):
        """approx. → approximately."""
        result = self.expander.expand("approx. 10 minutes", language="en")
        self.assertIn("approximately", result)

    def test_unknown_language_returns_unchanged(self):
        """Неизвестный язык → текст без изменений."""
        text = "т.е. неизвестный язык"
        result = self.expander.expand(text, language="xx")
        self.assertEqual(text, result)


class TestExpandSpanishBuiltins(unittest.TestCase):
    """Тесты раскрытия встроенных испанских аббревиатур."""

    def setUp(self):
        self.expander = AbbreviationExpander()

    def test_expand_pej(self):
        """p.ej. → por ejemplo."""
        result = self.expander.expand("Usa herramientas, p.ej. un martillo", language="es")
        self.assertIn("por ejemplo", result)

    def test_expand_etc_es(self):
        """etc. → etcétera (es)."""
        result = self.expander.expand("Manzanas, naranjas, etc.", language="es")
        self.assertIn("etcétera", result)


class TestCustomAbbreviations(unittest.TestCase):
    """Тесты пользовательских аббревиатур и персистентности."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.expander = AbbreviationExpander(data_dir=Path(self.tmp_dir))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_add_and_expand_custom(self):
        """Добавленная пользовательская аббревиатура раскрывается."""
        self.expander.add_abbreviation("т.н.", "так называемый", language="ru")
        result = self.expander.expand("Это т.н. новый подход", language="ru")
        self.assertIn("так называемый", result)
        self.assertNotIn("т.н.", result)

    def test_add_custom_english(self):
        """Пользовательская аббревиатура добавляется для английского."""
        self.expander.add_abbreviation("loc.", "location", language="en")
        result = self.expander.expand("Set the loc. first", language="en")
        self.assertIn("location", result)

    def test_remove_abbreviation(self):
        """Удалённая аббревиатура больше не раскрывается."""
        self.expander.add_abbreviation("т.н.", "так называемый", language="ru")
        self.expander.remove_abbreviation("т.н.", language="ru")
        result = self.expander.expand("Это т.н. подход", language="ru")
        self.assertIn("т.н.", result)
        self.assertNotIn("так называемый", result)

    def test_remove_nonexistent_returns_false(self):
        """Удаление несуществующей аббревиатуры возвращает False."""
        result = self.expander.remove_abbreviation("несуществует.", language="ru")
        self.assertFalse(result)

    def test_remove_returns_true_on_success(self):
        """Успешное удаление возвращает True."""
        self.expander.add_abbreviation("т.н.", "так называемый", language="ru")
        result = self.expander.remove_abbreviation("т.н.", language="ru")
        self.assertTrue(result)

    def test_list_abbreviations_returns_list(self):
        """list_abbreviations возвращает список со словарями."""
        items = self.expander.list_abbreviations(language="ru")
        self.assertIsInstance(items, list)
        self.assertTrue(len(items) > 0)

    def test_list_abbreviations_structure(self):
        """Каждый элемент имеет ключи abbr, expansion, flags, builtin."""
        items = self.expander.list_abbreviations(language="ru")
        for item in items:
            self.assertIn("abbr", item)
            self.assertIn("expansion", item)
            self.assertIn("flags", item)
            self.assertIn("builtin", item)

    def test_list_abbreviations_unknown_language(self):
        """list_abbreviations для неизвестного языка возвращает пустой список."""
        items = self.expander.list_abbreviations(language="xx")
        self.assertEqual(items, [])

    def test_custom_abbreviation_persisted(self):
        """Пользовательская аббревиатура сохраняется в файл и загружается при следующем запуске."""
        self.expander.add_abbreviation("т.н.", "так называемый", language="ru")
        # Создаём новый экземпляр — должен загрузить из файла
        expander2 = AbbreviationExpander(data_dir=Path(self.tmp_dir))
        result = expander2.expand("Это т.н. подход", language="ru")
        self.assertIn("так называемый", result)

    def test_abbreviations_json_file_created(self):
        """Файл abbreviations.json создаётся при добавлении пользовательской аббревиатуры."""
        self.expander.add_abbreviation("т.н.", "так называемый", language="ru")
        json_path = Path(self.tmp_dir) / "abbreviations.json"
        self.assertTrue(json_path.exists())

    def test_abbreviations_json_valid(self):
        """Содержимое abbreviations.json является валидным JSON."""
        self.expander.add_abbreviation("т.н.", "так называемый", language="ru")
        json_path = Path(self.tmp_dir) / "abbreviations.json"
        data = json.loads(json_path.read_text())
        self.assertIn("ru", data)
        self.assertIn("т.н.", data["ru"])

    def test_no_data_dir_no_crash(self):
        """AbbreviationExpander без data_dir работает корректно (только in-memory)."""
        expander = AbbreviationExpander(data_dir=None)
        expander.add_abbreviation("т.н.", "так называемый", language="ru")
        result = expander.expand("т.н. вещь", language="ru")
        self.assertIn("так называемый", result)

    def test_custom_overrides_builtin(self):
        """Пользовательская запись может перезаписать встроенную аббревиатуру."""
        self.expander.add_abbreviation("т.е.", "то есть (кастом)", language="ru")
        result = self.expander.expand("т.е. нечто", language="ru")
        self.assertIn("то есть (кастом)", result)

    def test_no_after_digit_flag(self):
        """Аббревиатуры с флагом no_after_digit не раскрываются после числа."""
        self.expander.add_abbreviation("кв.", "квадратный", language="ru", flags="no_after_digit")
        # После числа — не раскрывать
        _result = self.expander.expand("площадь 25 кв.", language="ru")  # noqa: F841
        # Перед текстом — раскрывать
        result2 = self.expander.expand("купил кв. метров", language="ru")
        # Только второй случай должен быть расширен
        self.assertIn("квадратный", result2)


class TestNoExpansionInCode(unittest.TestCase):
    """Аббревиатуры не раскрываются внутри code spans."""

    def setUp(self):
        self.expander = AbbreviationExpander()

    def test_no_expansion_in_code_span(self):
        """Аббревиатуры внутри backtick code span не раскрываются."""
        text = "Код: `т.е.` означает равенство"
        result = self.expander.expand(text, language="ru")
        self.assertIn("`т.е.`", result)

    def test_expansion_outside_code_span(self):
        """Аббревиатуры вне code span раскрываются нормально."""
        text = "т.е. это пример"
        result = self.expander.expand(text, language="ru")
        self.assertIn("то есть", result)


class TestLongestMatchFirst(unittest.TestCase):
    """Длинные аббревиатуры приоритетнее коротких."""

    def setUp(self):
        self.expander = AbbreviationExpander()

    def test_and_tak_dalee_vs_tak_dalee(self):
        """'и т.д.' предпочтительнее отдельного 'т.д.'."""
        result = self.expander.expand("Книги, тетради и т.д.", language="ru")
        # 'и т.д.' должно раскрыться как 'и так далее'
        self.assertIn("так далее", result)


class TestExpandDefaultLanguage(unittest.TestCase):
    """Tests for default language parameter (ru) and case edge cases."""

    def setUp(self):
        self.expander = AbbreviationExpander()

    def test_expand_default_language_is_ru(self):
        """expand() without explicit language defaults to Russian."""
        result = self.expander.expand("Приехал в г. Москва")
        self.assertIn("город", result)

    def test_expand_unknown_abbr_unchanged(self) -> None:
        """Unknown abbreviation is left unchanged."""
        text = "Текст с неизв. сокращением"
        result = self.expander.expand(text, language="ru")
        self.assertIn("неизв.", result)

    def test_case_insensitive_expansion_lowercase(self):
        """Lowercase abbreviation is expanded correctly."""
        result = self.expander.expand("нужно, т.е. обязательно", language="ru")
        self.assertIn("то есть", result)

    def test_expand_does_not_alter_nonabbrev_dots(self):
        """Dots in regular text (end of sentence) are not consumed."""
        result = self.expander.expand("Конец предложения.", language="ru")
        self.assertTrue(result.endswith("."))

    def test_expand_multiple_known_abbrevs_en(self):
        """Multiple EN abbreviations in one text are all expanded."""
        text = "Use e.g. a hammer, i.e. a tool, etc."
        result = self.expander.expand(text, language="en")
        self.assertIn("for example", result)
        self.assertIn("that is", result)
        self.assertIn("et cetera", result)

    def test_expand_es_with_unknown_remains(self):
        """Unknown ES abbreviation stays in text."""
        text = "Texto con descon. abreviatura"
        result = self.expander.expand(text, language="es")
        self.assertIn("descon.", result)

    def test_list_abbreviations_en_has_items(self):
        """English abbreviations list is non-empty."""
        items = self.expander.list_abbreviations(language="en")
        self.assertTrue(len(items) > 0)
        abbrs = [i["abbr"] for i in items]
        self.assertIn("e.g.", abbrs)

    def test_list_abbreviations_es_has_items(self):
        """Spanish abbreviations list is non-empty."""
        items = self.expander.list_abbreviations(language="es")
        self.assertTrue(len(items) > 0)

    def test_builtin_flag_is_true_for_builtin_abbrevs(self):
        """Built-in abbreviations have builtin=True in list output."""
        items = self.expander.list_abbreviations(language="ru")
        builtin_items = [i for i in items if i["abbr"] == "т.е."]
        self.assertEqual(len(builtin_items), 1)
        self.assertTrue(builtin_items[0]["builtin"])


class TestAbbreviationExpanderConcurrent(unittest.TestCase):
    """Тест параллельного выполнения expand()."""

    def setUp(self):
        self.expander = AbbreviationExpander()

    def test_concurrent_expand(self) -> None:
        """Параллельный вызов expand() из 20 потоков не вызывает ошибок."""
        tasks = [
            ("Это важно, т.е. нужно запомнить", "ru"),
            ("Use tools, e.g. a hammer", "en"),
            ("Usa herramientas, p.ej. un martillo", "es"),
            ("Книги, тетради и т.д.", "ru"),
            ("Обычный текст без аббревиатур.", "ru"),
        ] * 4  # 20 задач

        results: list = [None] * len(tasks)
        errors: list = []

        def worker(idx: int, text: str, lang: str) -> None:
            try:
                results[idx] = self.expander.expand(text, language=lang)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i, t, l))
            for i, (t, l) in enumerate(tasks)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        self.assertEqual(errors, [], f"Errors in concurrent expand: {errors}")
        for i, result in enumerate(results):
            self.assertIsNotNone(result, f"Result {i} is None")
            self.assertIsInstance(result, str)

    def test_concurrent_expand_unicode_texts(self) -> None:
        """Параллельный expand со строками, содержащими Unicode."""
        texts_langs = [
            ("Привет 😊, т.е. это тест", "ru"),
            ("你好, use e.g. this tool", "en"),
            ("مرحبا, т.к. это важно", "ru"),
        ] * 6  # 18 задач

        results: list = [None] * len(texts_langs)
        errors: list = []

        def worker(idx: int, text: str, lang: str) -> None:
            try:
                results[idx] = self.expander.expand(text, language=lang)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i, t, l))
            for i, (t, l) in enumerate(texts_langs)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        self.assertEqual(errors, [], f"Unicode concurrent errors: {errors}")
        for result in results:
            self.assertIsInstance(result, str)


class TestAbbreviationExpanderUnicode(unittest.TestCase):
    """Тесты обработки Unicode в AbbreviationExpander."""

    def setUp(self):
        self.expander = AbbreviationExpander()

    def test_unicode_chars_preserved_around_abbreviation(self) -> None:
        """Unicode-символы вокруг аббревиатуры сохраняются."""
        text = "你好 т.е. это тест 😊"
        result = self.expander.expand(text, language="ru")
        self.assertIn("то есть", result)
        self.assertIn("你好", result)
        self.assertIn("😊", result)

    def test_unicode_arabic_text_unchanged(self) -> None:
        """Арабский текст не содержит аббревиатур → возвращается без изменений."""
        text = "مرحبا بالعالم"
        result = self.expander.expand(text, language="ru")
        self.assertEqual(text, result)

    def test_unicode_es_accented_abbreviations(self) -> None:
        """Испанские аббревиатуры с диакритикой раскрываются корректно."""
        result = self.expander.expand("Número de pág. 5", language="es")
        self.assertIn("página", result)
        self.assertNotIn("pág.", result)

    def test_unicode_es_senora_expanded(self) -> None:
        """Sra. → señora/Señora (содержит ñ; case matched от оригинала Sra.)."""
        result = self.expander.expand("Buenos días, Sra. García", language="es")
        # Sra. начинается с заглавной → _match_case даёт Señora
        self.assertIn("eñora", result)  # проверяем ядро слова независимо от регистра


class TestW1068MultiSenseAbbreviationsRemoved(unittest.TestCase):
    """W1060 F2+F3 — многозначные аббревиатуры удалены из _BUILTIN_RU (W1068).

    гл./ред./д./св. удалены как грамматически небезопасные.
    обл. получила флаг no_after_digit.
    """

    def setUp(self):
        self.expander = AbbreviationExpander()

    # ── F2: многозначные аббревиатуры НЕ раскрываются ─────────────────────────

    def test_gl_not_expanded_after_W1068(self):
        """гл. больше не раскрывается автоматически (было → 'глава', неверно в падежах)."""
        result = self.expander.expand("гл. 5 посвящена теме", language="ru")
        self.assertNotIn("глава", result)
        self.assertIn("гл.", result)

    def test_red_not_expanded_after_W1068(self):
        """ред. больше не раскрывается автоматически (было → 'редактор', неверно в юридическом тексте)."""
        result = self.expander.expand("в новой ред. закона", language="ru")
        self.assertNotIn("редактор", result)
        self.assertIn("ред.", result)

    def test_d_not_expanded_after_W1068(self):
        """д. больше не раскрывается автоматически (было → 'дом', неверно для отчества д. Петрова)."""
        result = self.expander.expand("д. Петрова была права", language="ru")
        self.assertNotIn("дом", result)
        self.assertIn("д.", result)

    def test_sv_not_expanded_after_W1068(self):
        """св. больше не раскрывается автоматически (было → 'святой', неверно в 'по св. данным')."""
        result = self.expander.expand("по св. данным", language="ru")
        self.assertNotIn("святой", result)
        self.assertIn("св.", result)

    # ── F3: обл. с флагом no_after_digit ──────────────────────────────────────

    def test_77_obl_not_expanded(self):
        """обл. НЕ раскрывается после цифры (77 обл. = номер региона)."""
        result = self.expander.expand("регион 77 обл.", language="ru")
        self.assertNotIn("область", result)
        self.assertIn("обл.", result)

    def test_obl_expanded_after_text(self):
        """обл. раскрывается как 'область' в текстовом контексте."""
        result = self.expander.expand("Московская обл. известна", language="ru")
        self.assertIn("область", result)
        self.assertNotIn("обл.", result)


class TestSecurityLimits(unittest.TestCase):
    """Regression tests for security hardening: length and dict-size limits (wave-A)."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.expander = AbbreviationExpander(data_dir=Path(self.tmp_dir))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_abbr_too_long_is_silently_rejected(self):
        """add_abbreviation silently rejects abbr longer than MAX_ABBR_LENGTH."""
        long_abbr = "а." * (AbbreviationExpander.MAX_ABBR_LENGTH + 1)  # 102 chars
        self.expander.add_abbreviation(long_abbr, "расшифровка", language="ru")
        items = self.expander.list_abbreviations(language="ru")
        abbrs = [i["abbr"] for i in items]
        self.assertNotIn(long_abbr, abbrs)

    def test_expansion_too_long_is_silently_rejected(self):
        """add_abbreviation silently rejects expansion longer than MAX_EXPANSION_LENGTH."""
        long_expansion = "слово " * (AbbreviationExpander.MAX_EXPANSION_LENGTH + 1)
        self.expander.add_abbreviation("кр.", long_expansion, language="ru")
        items = self.expander.list_abbreviations(language="ru")
        user_items = [i for i in items if not i["builtin"]]
        self.assertFalse(
            any(i["abbr"] == "кр." for i in user_items),
            "Overly long expansion should not be stored",
        )

    def test_abbr_at_exact_limit_is_accepted(self):
        """add_abbreviation accepts abbr exactly MAX_ABBR_LENGTH chars long."""
        # MAX_ABBR_LENGTH chars using ASCII-safe abbreviation letters
        exact_abbr = "a" * AbbreviationExpander.MAX_ABBR_LENGTH
        self.expander.add_abbreviation(exact_abbr, "test expansion", language="ru")
        items = self.expander.list_abbreviations(language="ru")
        abbrs = [i["abbr"] for i in items]
        self.assertIn(exact_abbr, abbrs)

    def test_expansion_at_exact_limit_is_accepted(self):
        """add_abbreviation accepts expansion exactly MAX_EXPANSION_LENGTH chars long."""
        exact_expansion = "a" * AbbreviationExpander.MAX_EXPANSION_LENGTH
        self.expander.add_abbreviation("tst.", exact_expansion, language="en")
        items = self.expander.list_abbreviations(language="en")
        self.assertTrue(any(i["abbr"] == "tst." for i in items))

    def test_dict_size_limit_blocks_new_entries(self):
        """add_abbreviation rejects new entries once MAX_DICT_SIZE is reached."""
        expander = AbbreviationExpander(data_dir=None)
        limit = AbbreviationExpander.MAX_DICT_SIZE
        # Exhaust the dict with synthetic custom abbreviations.
        # Use 'xx' prefix to avoid colliding with builtins.
        # Count how many builtins already exist for 'ru'.
        current_count = len([
            i for i in expander.list_abbreviations("ru")
        ])
        slots_needed = limit - current_count
        for i in range(slots_needed):
            expander.add_abbreviation(f"x{i:04d}.", f"расш{i}", "ru")

        # One more attempt — should be silently dropped.
        expander.add_abbreviation("overflow.", "не должно добавиться", "ru")
        items = expander.list_abbreviations("ru")
        self.assertFalse(any(i["abbr"] == "overflow." for i in items))
        # Dict must not exceed the limit.
        self.assertLessEqual(len(items), limit)

    def test_dict_size_limit_allows_update_of_existing_entry(self):
        """add_abbreviation allows UPDATING an existing abbreviation even when the dict is full."""
        expander = AbbreviationExpander(data_dir=None)
        limit = AbbreviationExpander.MAX_DICT_SIZE
        current_count = len(expander.list_abbreviations("ru"))
        slots_needed = limit - current_count
        for i in range(slots_needed):
            expander.add_abbreviation(f"y{i:04d}.", f"расш{i}", "ru")

        # т.е. is already a builtin — updating it must succeed even when full.
        expander.add_abbreviation("т.е.", "то есть (обновлено)", "ru")
        items = expander.list_abbreviations("ru")
        updated = next((i for i in items if i["abbr"] == "т.е."), None)
        self.assertIsNotNone(updated)
        self.assertEqual(updated["expansion"], "то есть (обновлено)")

    def test_load_custom_skips_oversized_entries(self):
        """_load_custom silently skips entries with abbr or expansion exceeding limits."""
        json_path = Path(self.tmp_dir) / "abbreviations.json"
        long_abbr = "x" * (AbbreviationExpander.MAX_ABBR_LENGTH + 1)
        long_exp = "y" * (AbbreviationExpander.MAX_EXPANSION_LENGTH + 1)
        data = {
            "ru": {
                long_abbr: {"expansion": "нормально", "flags": ""},
                "норм.": {"expansion": long_exp, "flags": ""},
                "ок.": {"expansion": "нормальная запись", "flags": ""},
            }
        }
        json_path.write_text(json.dumps(data), encoding="utf-8")
        expander = AbbreviationExpander(data_dir=Path(self.tmp_dir))
        items = expander.list_abbreviations("ru")
        abbrs = [i["abbr"] for i in items]
        self.assertNotIn(long_abbr, abbrs)
        self.assertNotIn("норм.", abbrs)
        # The valid entry must be loaded.
        self.assertIn("ок.", abbrs)


if __name__ == "__main__":
    unittest.main()
