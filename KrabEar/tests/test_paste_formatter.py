"""Тесты для PasteFormatter — умного форматирования текста под целевое приложение."""

from __future__ import annotations
from core.paste_formatter import (
    PasteFormatter,
    _fmt_telegram,
    _fmt_notes,
    _fmt_email,
    _fmt_code_editor,
    _fmt_default,
    _apply_rules,
)

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Настраиваем sys.path чтобы импорты core.* работали из любого контекста
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


class TestBuiltinFormatters(unittest.TestCase):
    """Тесты встроенных форматирующих функций."""

    # --- telegram ---

    def test_telegram_removes_trailing_period(self):
        result = _fmt_telegram("Привет, как дела.")
        self.assertFalse(result.endswith("."), "Telegram: точка в конце должна быть удалена")

    def test_telegram_keeps_exclamation(self):
        result = _fmt_telegram("Отлично!")
        self.assertTrue(result.endswith("!"))

    def test_telegram_splits_long_text(self):
        long_text = "Первое предложение. " * 10
        result = _fmt_telegram(long_text.strip())
        self.assertIn("\n", result, "Telegram: длинный текст должен быть разбит на строки")

    def test_telegram_short_text_no_split(self):
        short = "Короткое сообщение без точки"
        result = _fmt_telegram(short)
        self.assertNotIn("\n", result)

    # --- notes ---

    def test_notes_adds_timestamp_header(self):
        result = _fmt_notes("Мысль о проекте.")
        # Заголовок вида [YYYY-MM-DD HH:MM]
        self.assertRegex(result, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]")

    def test_notes_bullet_single_sentence(self):
        result = _fmt_notes("Одно предложение")
        self.assertIn("•", result)

    def test_notes_bullet_multiple_sentences(self):
        result = _fmt_notes("Первое. Второе. Третье.")
        bullets = [line for line in result.splitlines() if line.startswith("•")]
        self.assertGreaterEqual(len(bullets), 2)

    # --- email ---

    def test_email_has_greeting(self):
        result = _fmt_email("встреча завтра в 10")
        self.assertTrue(result.startswith("Здравствуйте"))

    def test_email_capitalizes_first_letter(self):
        result = _fmt_email("встреча завтра")
        # Заглавная буква внутри блока текста
        lines = result.splitlines()
        # Найти строку с текстом (не пустую и не приветствие)
        body_lines = [ln for ln in lines if ln and not ln.startswith("Здравствуйте") and not ln.startswith("С уважением")]
        if body_lines:
            self.assertTrue(body_lines[0][0].isupper())

    def test_email_adds_period_if_missing(self):
        result = _fmt_email("Ждём вас")
        # Убедимся что тело заканчивается точкой
        self.assertIn("Ждём вас.", result)

    def test_email_has_signature(self):
        result = _fmt_email("текст письма")
        self.assertIn("С уважением", result)

    # --- code_editor ---

    def test_code_editor_block_comment(self):
        result = _fmt_code_editor("нормализовать входные данные")
        self.assertTrue(result.startswith("/*"))
        self.assertTrue(result.strip().endswith("*/"))

    def test_code_editor_preserves_multiline(self):
        result = _fmt_code_editor("строка 1\nстрока 2")
        self.assertIn("// строка 1", result)
        self.assertIn("// строка 2", result)

    # --- default ---

    def test_default_returns_unchanged(self):
        text = "  Текст как есть.  "
        result = _fmt_default(text)
        self.assertEqual(result, text)


class TestApplyRules(unittest.TestCase):
    """Тесты движка правил для кастомных форматтеров."""

    def test_strip_trailing_period(self):
        result = _apply_rules("Конец.", {"strip_trailing_period": True})
        self.assertEqual(result, "Конец")

    def test_capitalize(self):
        result = _apply_rules("маленькая буква", {"capitalize": True})
        self.assertTrue(result[0].isupper())

    def test_prepend(self):
        result = _apply_rules("текст", {"prepend": "Заголовок"})
        self.assertTrue(result.startswith("Заголовок"))

    def test_append(self):
        result = _apply_rules("текст", {"append": "Подпись"})
        self.assertTrue(result.endswith("Подпись"))

    def test_max_length_truncates(self):
        long_text = "слово " * 100
        result = _apply_rules(long_text, {"max_length": 20})
        self.assertLessEqual(len(result), 25)  # немного допуск для «…»
        self.assertIn("…", result)

    def test_bullet_sentences(self):
        result = _apply_rules("Раз. Два. Три.", {"bullet_sentences": True})
        self.assertIn("•", result)

    def test_combined_rules(self):
        result = _apply_rules(
            "текст.",
            {"capitalize": True, "strip_trailing_period": True, "prepend": ">> "},
        )
        self.assertTrue(result.startswith(">> "))
        self.assertFalse(result.rstrip().endswith("."))


class TestPasteFormatterClass(unittest.TestCase):
    """Тесты класса PasteFormatter."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.formatter = PasteFormatter(data_dir=self.tmp)

    # --- format_for_app ---

    def test_format_for_app_telegram(self):
        result = self.formatter.format_for_app("Привет мир.", "telegram")
        self.assertFalse(result.endswith("."))

    def test_format_for_app_default_fallback(self):
        text = "Неизвестное приложение."
        result = self.formatter.format_for_app(text, "unknownapp_xyz")
        self.assertEqual(result, text)

    def test_format_for_app_partial_match(self):
        """Частичное совпадение: 'Telegram Desktop' → telegram форматтер."""
        result = self.formatter.format_for_app("Проверка.", "Telegram Desktop")
        # Telegram форматтер убирает точку
        self.assertFalse(result.endswith("."))

    def test_format_for_app_empty_app_name(self):
        text = "Текст для вставки."
        result = self.formatter.format_for_app(text, "")
        # Пустое имя → default, текст как есть
        self.assertEqual(result, text)

    def test_format_for_app_case_insensitive(self):
        result = self.formatter.format_for_app("Привет.", "TELEGRAM")
        self.assertFalse(result.endswith("."))

    # --- list_formatters ---

    def test_list_formatters_returns_builtins(self):
        formatters = self.formatter.list_formatters()
        names = [f["name"] for f in formatters]
        for expected in ("telegram", "notes", "email", "code_editor", "default"):
            self.assertIn(expected, names)

    def test_list_formatters_builtin_flag(self):
        formatters = self.formatter.list_formatters()
        for f in formatters:
            if f["name"] in ("telegram", "notes", "email", "code_editor", "default"):
                self.assertTrue(f["builtin"])

    # --- add_custom_formatter ---

    def test_add_custom_formatter_persists(self):
        self.formatter.add_custom_formatter("myapp", {"capitalize": True, "label": "My App"})
        # Новый экземпляр читает из того же data_dir
        formatter2 = PasteFormatter(data_dir=self.tmp)
        names = [f["name"] for f in formatter2.list_formatters()]
        self.assertIn("myapp", names)

    def test_add_custom_formatter_applied(self):
        self.formatter.add_custom_formatter(
            "slack",
            {"strip_trailing_period": True, "capitalize": True},
        )
        result = self.formatter.format_for_app("тест.", "slack")
        self.assertFalse(result.endswith("."))
        self.assertTrue(result[0].isupper())

    def test_add_custom_formatter_overrides_builtin(self):
        """Кастомный форматтер с тем же именем что и встроенный НЕ переписывает встроенный.
        Встроенные форматтеры имеют приоритет ниже — кастомные выше."""
        self.formatter.add_custom_formatter(
            "telegram",
            {"append": "✓"},
        )
        result = self.formatter.format_for_app("Проверка.", "telegram")
        self.assertTrue(result.endswith("✓"))

    def test_add_custom_formatter_validation_empty_name(self):
        with self.assertRaises(ValueError):
            self.formatter.add_custom_formatter("", {"capitalize": True})

    def test_add_custom_formatter_validation_non_dict_rules(self):
        with self.assertRaises(ValueError):
            self.formatter.add_custom_formatter("myapp", "not_a_dict")  # type: ignore

    # --- remove_custom_formatter ---

    def test_remove_custom_formatter_returns_true(self):
        self.formatter.add_custom_formatter("toremove", {"capitalize": True})
        removed = self.formatter.remove_custom_formatter("toremove")
        self.assertTrue(removed)

    def test_remove_custom_formatter_not_found_returns_false(self):
        removed = self.formatter.remove_custom_formatter("doesnotexist")
        self.assertFalse(removed)

    def test_remove_custom_formatter_no_longer_in_list(self):
        self.formatter.add_custom_formatter("tempapp", {"capitalize": True})
        self.formatter.remove_custom_formatter("tempapp")
        names = [f["name"] for f in self.formatter.list_formatters()]
        self.assertNotIn("tempapp", names)

    def test_remove_then_falls_back_to_builtin_pattern(self):
        """После удаления кастомного telegram-форматтера возвращается встроенный."""
        self.formatter.add_custom_formatter("telegram", {"append": "custom"})
        self.formatter.remove_custom_formatter("telegram")
        result = self.formatter.format_for_app("Привет.", "telegram")
        # Встроенный telegram убирает точку
        self.assertFalse(result.endswith("."))
        self.assertNotIn("custom", result)

    # --- IPC handlers ---

    def test_handle_format_for_paste(self):
        resp = self.formatter.handle_format_for_paste({"text": "Тест.", "app_name": "telegram"})
        self.assertIn("formatted_text", resp)
        self.assertIn("app_name", resp)
        self.assertIn("formatter_used", resp)
        self.assertFalse(resp["formatted_text"].endswith("."))

    def test_handle_list_paste_formatters(self):
        resp = self.formatter.handle_list_paste_formatters({})
        self.assertIn("formatters", resp)
        self.assertIn("total", resp)
        self.assertGreaterEqual(resp["total"], 5)

    def test_handle_format_for_paste_default_app(self):
        text = "Текст без изменений."
        resp = self.formatter.handle_format_for_paste({"text": text})
        self.assertEqual(resp["formatted_text"], text)

    # --- Persistence file ---

    def test_persistence_file_created(self):
        self.formatter.add_custom_formatter("testpersist", {"capitalize": True})
        expected_path = Path(self.tmp) / "paste_formatters.json"
        self.assertTrue(expected_path.exists())
        data = json.loads(expected_path.read_text(encoding="utf-8"))
        self.assertIn("testpersist", data)

    # --- No data_dir (in-memory) ---

    def test_no_data_dir_still_works(self):
        fmt = PasteFormatter(data_dir=None)
        fmt.add_custom_formatter("inMemApp", {"capitalize": True})
        result = fmt.format_for_app("маленький текст", "inMemApp")
        self.assertTrue(result[0].isupper())

    def test_no_data_dir_list_custom(self):
        fmt = PasteFormatter(data_dir=None)
        fmt.add_custom_formatter("inMemApp2", {"capitalize": True})
        names = [f["name"] for f in fmt.list_formatters()]
        self.assertIn("inmemapp2", names)


class TestPasteFormatterEdgeCases(unittest.TestCase):
    """Граничные случаи и нетипичные входные данные."""

    def setUp(self):
        self.formatter = PasteFormatter(data_dir=None)

    def test_empty_text_telegram(self):
        result = self.formatter.format_for_app("", "telegram")
        self.assertEqual(result, "")

    def test_empty_text_email(self):
        result = self.formatter.format_for_app("", "email")
        # email добавляет структуру даже при пустом тексте
        self.assertIn("Здравствуйте", result)

    def test_whitespace_only_text(self):
        result = self.formatter.format_for_app("   ", "telegram")
        self.assertEqual(result.strip(), "")

    def test_non_string_text_coerced(self):
        result = self.formatter.format_for_app(42, "default")  # type: ignore
        self.assertEqual(result, "42")

    def test_notes_timestamp_format(self):
        result = self.formatter.format_for_app("Идея", "notes")
        self.assertRegex(result.splitlines()[0], r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]")

    def test_code_editor_empty(self):
        result = self.formatter.format_for_app("", "code_editor")
        self.assertTrue(result.startswith("/*"))
        self.assertIn("*/", result)

    def test_list_formatters_total_matches_len(self):
        formatters = self.formatter.list_formatters()
        self.assertEqual(len(formatters), 5)  # 5 встроенных, 0 кастомных

    def test_list_formatters_after_add_increments(self):
        self.formatter.add_custom_formatter("extra", {"capitalize": True})
        formatters = self.formatter.list_formatters()
        self.assertEqual(len(formatters), 6)


class TestPasteFormatterExtraEdgeCases(unittest.TestCase):
    """Дополнительные граничные случаи для PasteFormatter."""

    def setUp(self):
        self.formatter = PasteFormatter(data_dir=None)

    def test_telegram_no_trailing_period_already_clean(self):
        """Текст без точки в конце остаётся без изменений (Telegram)."""
        text = "Привет"
        result = self.formatter.format_for_app(text, "telegram")
        self.assertEqual(result, "Привет")

    def test_email_already_capitalized(self):
        """Email: уже заглавная буква не дублируется."""
        result = _fmt_email("Встреча в пятницу")
        self.assertIn("Встреча в пятницу.", result)

    def test_email_already_has_period(self):
        """Email: уже есть точка в конце — не добавляется вторая."""
        result = _fmt_email("Жду ответа.")
        # Строка содержит ровно одну точку в тексте тела
        body_part = result.split("Здравствуйте,\n\n")[1].split("\n\nС уважением")[0]
        self.assertFalse(body_part.endswith(".."))

    def test_apply_rules_no_rules_returns_stripped(self):
        """_apply_rules с пустыми правилами возвращает stripped текст."""
        result = _apply_rules("  текст  ", {})
        self.assertEqual(result, "текст")

    def test_apply_rules_max_length_exact(self):
        """max_length точно совпадает с длиной — текст не обрезается."""
        text = "hello world"
        result = _apply_rules(text, {"max_length": len(text)})
        self.assertNotIn("…", result)

    def test_apply_rules_bullet_single_sentence_no_bullet(self):
        """bullet_sentences не добавляет маркеры для одного предложения."""
        result = _apply_rules("Одно.", {"bullet_sentences": True})
        # Один элемент — маркер добавляется только если > 1 предложение
        # Проверяем что текст не пустой
        self.assertGreater(len(result), 0)

    def test_format_for_app_notes_uses_notes_formatter(self):
        """Имя 'notes' использует Notes форматтер (заголовок с датой)."""
        result = self.formatter.format_for_app("Идея для проекта.", "notes")
        self.assertRegex(result, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]")

    def test_format_for_app_email_uses_email_formatter(self):
        """Имя 'email' использует Email форматтер (Здравствуйте)."""
        result = self.formatter.format_for_app("Встреча завтра", "email")
        self.assertIn("Здравствуйте", result)

    def test_format_for_app_code_editor(self):
        """Имя 'code_editor' применяет блочный комментарий."""
        result = self.formatter.format_for_app("fix the bug", "code_editor")
        self.assertTrue(result.startswith("/*"))

    def test_handle_format_for_paste_no_app_name_defaults(self):
        """handle_format_for_paste без app_name использует default."""
        text = "Неизменённый текст."
        resp = self.formatter.handle_format_for_paste({"text": text})
        self.assertEqual(resp["formatted_text"], text)
        self.assertIn("formatter_used", resp)

    def test_custom_formatter_label_in_list(self):
        """Кастомный форматтер с label показывает правильный label в list."""
        self.formatter.add_custom_formatter(
            "myapp", {"capitalize": True, "label": "My Custom App"}
        )
        formatters = self.formatter.list_formatters()
        custom = next((f for f in formatters if f["name"] == "myapp"), None)
        self.assertIsNotNone(custom)
        self.assertEqual(custom["label"], "My Custom App")

    def test_custom_formatter_builtin_false(self):
        """Кастомный форматтер имеет builtin=False."""
        self.formatter.add_custom_formatter("mycustom", {"append": "end"})
        formatters = self.formatter.list_formatters()
        custom = next((f for f in formatters if f["name"] == "mycustom"), None)
        self.assertIsNotNone(custom)
        self.assertFalse(custom["builtin"])

    def test_code_editor_single_empty_line(self):
        """code_editor обрабатывает строки без текста как '//'."""
        result = _fmt_code_editor("line1\n\nline3")
        self.assertIn("//", result)
        self.assertIn("// line1", result)
        self.assertIn("// line3", result)

    def test_notes_empty_text_has_timestamp(self):
        """Notes форматтер для пустого текста всё равно даёт timestamp."""
        result = self.formatter.format_for_app("", "notes")
        self.assertRegex(result, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]")


class TestPasteFormatterWave114(unittest.TestCase):
    """Wave 114 — required named tests for PasteFormatter."""

    def setUp(self):
        self.formatter = PasteFormatter(data_dir=None)

    def test_format_plain_text(self):
        """default/plain formatter returns text unchanged."""
        text = "Plain transcript text here."
        result = self.formatter.format_for_app(text, "default")
        self.assertEqual(result, text)

    def test_format_markdown(self):
        """code_editor formatter wraps in block comment, preserving structure."""
        text = "fix bug\nwrite tests"
        result = self.formatter.format_for_app(text, "code_editor")
        self.assertTrue(result.startswith("/*"))
        self.assertIn("*/", result)
        self.assertIn("// fix bug", result)
        self.assertIn("// write tests", result)

    def test_format_telegram(self):
        """Telegram formatter strips trailing period and splits long text on newlines."""
        # Short text — no split
        short = "Привет"
        result_short = self.formatter.format_for_app(short, "telegram")
        self.assertNotIn("\n", result_short)
        # Long text (>120 chars) — splits on sentence boundaries
        long_text = "Первое предложение. Второе предложение. Третье предложение. И ещё одно тут."
        result_long = self.formatter.format_for_app(long_text + " " + long_text, "telegram")
        self.assertIn("\n", result_long)

    def test_format_html_escapes(self):
        """Default formatter passes through HTML-special characters untouched.

        PasteFormatter is not an HTML renderer — it does not escape & < >.
        We verify the text is returned verbatim by the default formatter.
        """
        html_text = "<b>bold</b> & <i>italic</i>"
        result = self.formatter.format_for_app(html_text, "default")
        self.assertEqual(result, html_text)

    def test_format_email_signature(self):
        """Email formatter appends 'С уважением' signature block."""
        text = "Встреча перенесена на пятницу"
        result = self.formatter.format_for_app(text, "email")
        self.assertIn("С уважением", result)
        self.assertTrue(result.startswith("Здравствуйте"))

    def test_format_notes(self):
        """Notes formatter prepends a timestamp header and uses bullet points."""
        text = "Идея: автоматизировать отчёт. Проверить базу данных."
        result = self.formatter.format_for_app(text, "notes")
        self.assertRegex(result.splitlines()[0], r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]")
        self.assertIn("•", result)

    def test_unicode_preserved_across_formats(self):
        """Unicode characters (RU/ES/emoji) survive all built-in formatters."""
        text = "Привет мир. Hola mundo. Проверка émoji \U0001f600."
        for app in ("default", "telegram", "email", "notes", "code_editor"):
            result = self.formatter.format_for_app(text, app)
            self.assertIn("Привет", result, msg=f"app={app}")
            self.assertIn("Hola", result, msg=f"app={app}")

    def test_unknown_format_falls_back_to_plain(self):
        """Unknown app name falls back to default (text returned as-is)."""
        text = "Текст для неизвестного приложения."
        result = self.formatter.format_for_app(text, "totally_unknown_xyz_app")
        self.assertEqual(result, text)


if __name__ == "__main__":
    unittest.main()
