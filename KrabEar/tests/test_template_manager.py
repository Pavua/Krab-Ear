"""Тесты для TemplateManager — управление текстовыми шаблонами быстрой вставки."""

from __future__ import annotations
from backend.template_manager import TemplateManager

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Добавляем пути для импорта
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


class TestTemplateManagerBuiltins(unittest.TestCase):
    """Проверяем встроенные шаблоны."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tm = TemplateManager(data_dir=self.tmpdir)

    def test_get_templates_returns_builtins(self):
        templates = self.tm.get_templates()
        names = {t["name"] for t in templates}
        self.assertIn("greeting_ru", names)
        self.assertIn("farewell_ru", names)
        self.assertIn("email_signature", names)

    def test_builtins_have_required_fields(self):
        templates = self.tm.get_templates()
        for t in templates:
            self.assertIn("name", t)
            self.assertIn("text", t)
            self.assertIn("category", t)
            self.assertTrue(t["name"])
            self.assertTrue(t["text"])


class TestTemplateManagerAdd(unittest.TestCase):
    """Тесты добавления шаблонов."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tm = TemplateManager(data_dir=self.tmpdir)

    def test_add_template_returns_dict(self):
        result = self.tm.add_template("test_tpl", "Привет, {name}!", "greeting")
        self.assertEqual(result["name"], "test_tpl")
        self.assertEqual(result["text"], "Привет, {name}!")
        self.assertEqual(result["category"], "greeting")

    def test_add_template_persists(self):
        self.tm.add_template("persist_tpl", "Текст шаблона", "general")
        # Создаём новый экземпляр с той же директорией
        tm2 = TemplateManager(data_dir=self.tmpdir)
        names = {t["name"] for t in tm2.get_templates()}
        self.assertIn("persist_tpl", names)

    def test_add_template_updates_existing(self):
        self.tm.add_template("update_tpl", "Старый текст")
        self.tm.add_template("update_tpl", "Новый текст")
        templates = self.tm.get_templates()
        matching = [t for t in templates if t["name"] == "update_tpl"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["text"], "Новый текст")

    def test_add_template_default_category(self):
        result = self.tm.add_template("no_cat_tpl", "Текст")
        self.assertEqual(result["category"], "general")

    def test_add_template_invalid_name_raises(self):
        with self.assertRaises(ValueError):
            self.tm.add_template("bad name!", "Текст")

    def test_add_template_empty_name_raises(self):
        with self.assertRaises(ValueError):
            self.tm.add_template("", "Текст")

    def test_add_template_empty_text_raises(self):
        with self.assertRaises(ValueError):
            self.tm.add_template("valid_name", "")


class TestTemplateManagerRemove(unittest.TestCase):
    """Тесты удаления шаблонов."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tm = TemplateManager(data_dir=self.tmpdir)

    def test_remove_existing_template(self):
        self.tm.add_template("remove_me", "Текст для удаления")
        removed = self.tm.remove_template("remove_me")
        self.assertTrue(removed)
        names = {t["name"] for t in self.tm.get_templates()}
        self.assertNotIn("remove_me", names)

    def test_remove_nonexistent_returns_false(self):
        removed = self.tm.remove_template("does_not_exist")
        self.assertFalse(removed)

    def test_remove_does_not_affect_other_templates(self):
        self.tm.add_template("keep_me", "Оставить")
        self.tm.add_template("delete_me", "Удалить")
        self.tm.remove_template("delete_me")
        names = {t["name"] for t in self.tm.get_templates()}
        self.assertIn("keep_me", names)


class TestTemplateManagerApply(unittest.TestCase):
    """Тесты применения шаблонов с подстановкой переменных."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tm = TemplateManager(data_dir=self.tmpdir)

    def test_apply_builtin_greeting(self):
        text = self.tm.apply_template("greeting_ru", {"name": "Иван"})
        self.assertIn("Иван", text)

    def test_apply_with_multiple_variables(self):
        self.tm.add_template("multi_var", "Привет, {name}! Ваш заказ {order_id} готов.")
        text = self.tm.apply_template("multi_var", {"name": "Мария", "order_id": "12345"})
        self.assertEqual(text, "Привет, Мария! Ваш заказ 12345 готов.")

    def test_apply_without_variables(self):
        self.tm.add_template("no_vars", "Фиксированный текст без переменных.")
        text = self.tm.apply_template("no_vars")
        self.assertEqual(text, "Фиксированный текст без переменных.")

    def test_apply_nonexistent_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.tm.apply_template("nonexistent_template")

    def test_apply_partial_substitution(self):
        """Незаполненные переменные остаются как есть."""
        self.tm.add_template("partial", "Привет, {name}! {unset_var}")
        text = self.tm.apply_template("partial", {"name": "Алекс"})
        self.assertIn("Алекс", text)
        self.assertIn("{unset_var}", text)


class TestTemplateManagerIPC(unittest.TestCase):
    """Тесты IPC-обработчиков."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tm = TemplateManager(data_dir=self.tmpdir)

    def test_handle_get_templates(self):
        result = self.tm.handle_get_templates({})
        self.assertIn("templates", result)
        self.assertIsInstance(result["templates"], list)

    def test_handle_add_template(self):
        result = self.tm.handle_add_template({
            "name": "ipc_test",
            "text": "IPC тест {var}",
            "category": "test",
        })
        self.assertIn("template", result)
        self.assertEqual(result["template"]["name"], "ipc_test")

    def test_handle_remove_template(self):
        self.tm.add_template("to_remove_ipc", "Текст")
        result = self.tm.handle_remove_template({"name": "to_remove_ipc"})
        self.assertTrue(result["removed"])
        self.assertEqual(result["name"], "to_remove_ipc")

    def test_handle_apply_template(self):
        self.tm.add_template("apply_ipc", "Привет, {name}!")
        result = self.tm.handle_apply_template({
            "name": "apply_ipc",
            "variables": {"name": "Тест"},
        })
        self.assertEqual(result["text"], "Привет, Тест!")
        self.assertEqual(result["name"], "apply_ipc")

    def test_handle_apply_template_no_variables(self):
        self.tm.add_template("apply_novars_ipc", "Без переменных.")
        result = self.tm.handle_apply_template({"name": "apply_novars_ipc"})
        self.assertEqual(result["text"], "Без переменных.")


class TestTemplateManagerPersistence(unittest.TestCase):
    """Тесты сохранения и загрузки из файла."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_file_created_after_add(self):
        tm = TemplateManager(data_dir=self.tmpdir)
        tm.add_template("file_test", "Файловый тест")
        filepath = Path(self.tmpdir) / "templates.json"
        self.assertTrue(filepath.exists())

    def test_file_format_is_valid_json(self):
        tm = TemplateManager(data_dir=self.tmpdir)
        tm.add_template("json_test", "JSON тест")
        filepath = Path(self.tmpdir) / "templates.json"
        data = json.loads(filepath.read_text(encoding="utf-8"))
        self.assertIsInstance(data, list)

    def test_builtins_not_written_to_file(self):
        """Встроенные шаблоны не должны дублироваться в файле."""
        TemplateManager(data_dir=self.tmpdir)
        # Просто читаем — файл не создаётся без явного add
        filepath = Path(self.tmpdir) / "templates.json"
        self.assertFalse(filepath.exists())

    def test_corrupt_file_falls_back_to_builtins(self):
        filepath = Path(self.tmpdir) / "templates.json"
        filepath.write_text("this is not valid json", encoding="utf-8")
        tm = TemplateManager(data_dir=self.tmpdir)
        templates = tm.get_templates()
        # Должны вернуться хотя бы встроенные
        self.assertTrue(len(templates) > 0)
        names = {t["name"] for t in templates}
        self.assertIn("greeting_ru", names)


class TestTemplateManagerEdgeCases(unittest.TestCase):
    """Тесты граничных случаев и специальных символов."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tm = TemplateManager(data_dir=self.tmpdir)

    def test_template_name_with_underscore(self):
        result = self.tm.add_template("my_template_name", "Текст")
        self.assertEqual(result["name"], "my_template_name")

    def test_template_name_with_hyphens(self):
        result = self.tm.add_template("my-template-name", "Текст")
        self.assertEqual(result["name"], "my-template-name")

    def test_template_name_with_numbers(self):
        result = self.tm.add_template("template123", "Текст")
        self.assertEqual(result["name"], "template123")

    def test_template_text_with_cyrillic(self):
        text = "Привет, это кириллица! Спасибо, {name}!"
        result = self.tm.add_template("cyrillic_tpl", text)
        self.assertEqual(result["text"], text)

    def test_template_text_with_special_chars(self):
        text = "Текст с символами: @#$%^&*() и переменная {var}"
        result = self.tm.add_template("special_chars", text)
        self.assertEqual(result["text"], text)

    def test_template_with_multiline_text(self):
        text = "Строка 1\nСтрока 2\nСтрока 3 с {var}"
        result = self.tm.add_template("multiline", text)
        self.assertEqual(result["text"], text)

    def test_apply_template_with_empty_variables_dict(self):
        self.tm.add_template("empty_vars", "Привет, {name}!")
        text = self.tm.apply_template("empty_vars", {})
        self.assertIn("{name}", text)

    def test_apply_template_with_numeric_variable_value(self):
        self.tm.add_template("numeric_var", "Количество: {count}")
        text = self.tm.apply_template("numeric_var", {"count": 42})
        self.assertEqual(text, "Количество: 42")

    def test_apply_template_with_float_variable_value(self):
        self.tm.add_template("float_var", "Цена: {price} рублей")
        text = self.tm.apply_template("float_var", {"price": 99.99})
        self.assertEqual(text, "Цена: 99.99 рублей")

    def test_template_with_repeated_variables(self):
        self.tm.add_template("repeated", "{name} - это {name}!")
        text = self.tm.apply_template("repeated", {"name": "Краб"})
        self.assertEqual(text, "Краб - это Краб!")

    def test_template_name_strip_whitespace(self):
        """add_template должен trimить имя и текст."""
        result = self.tm.add_template("  spaced_name  ", "  spaced text  ")
        self.assertEqual(result["name"], "spaced_name")
        self.assertEqual(result["text"], "spaced text")

    def test_remove_template_with_whitespace_name(self):
        """remove_template должен корректно работать с whitespace."""
        self.tm.add_template("to_remove", "Текст")
        removed = self.tm.remove_template("  to_remove  ")
        self.assertTrue(removed)

    def test_apply_template_return_type_is_string(self):
        self.tm.add_template("type_test", "Текст {var}")
        text = self.tm.apply_template("type_test", {"var": "значение"})
        self.assertIsInstance(text, str)

    def test_user_template_overrides_builtin(self):
        """Пользовательский шаблон должен переопределять встроенный."""
        custom_text = "Переопределённое приветствие для {name}"
        self.tm.add_template("greeting_ru", custom_text)
        templates = self.tm.get_templates()
        matching = [t for t in templates if t["name"] == "greeting_ru"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["text"], custom_text)
        self.assertFalse(matching[0].get("builtin", False))

    def test_template_category_default_general(self):
        result = self.tm.add_template("no_category", "Текст")
        self.assertEqual(result["category"], "general")

    def test_template_category_strip_whitespace(self):
        result = self.tm.add_template("with_cat", "Текст", "  custom_cat  ")
        self.assertEqual(result["category"], "custom_cat")


class TestTemplateManagerThreadSafety(unittest.TestCase):
    """Тесты потокобезопасности (базовые)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tm = TemplateManager(data_dir=self.tmpdir)

    def test_concurrent_get_templates(self):
        """get_templates должен работать безопасно в многопоточной среде."""
        import threading
        results = []

        def get_and_append():
            templates = self.tm.get_templates()
            results.append(len(templates) > 0)

        threads = [threading.Thread(target=get_and_append) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 5)
        self.assertTrue(all(results))

    def test_lock_exists(self):
        """TemplateManager должен иметь _lock для потокобезопасности."""
        self.assertTrue(hasattr(self.tm, "_lock"))
        # Проверяем, что _lock имеет методы acquire/release (интерфейс Lock)
        self.assertTrue(hasattr(self.tm._lock, "acquire"))
        self.assertTrue(hasattr(self.tm._lock, "release"))


class TestTemplateManagerIPCEdgeCases(unittest.TestCase):
    """Тесты IPC-обработчиков с edge case'ами."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tm = TemplateManager(data_dir=self.tmpdir)

    def test_handle_add_template_with_missing_params(self):
        """handle_add_template должен обрабатывать пустые параметры."""
        with self.assertRaises(ValueError):
            # Пустое имя вызовет ValueError в add_template
            self.tm.handle_add_template({})

    def test_handle_add_template_with_nondict_params(self):
        """handle_add_template должен приводить типы к строкам."""
        # Передаём целое число как имя
        result = self.tm.handle_add_template({
            "name": 123,
            "text": "Текст",
        })
        self.assertEqual(result["template"]["name"], "123")

    def test_handle_apply_template_with_invalid_variables(self):
        """handle_apply_template должен игнорировать невалидные переменные."""
        self.tm.add_template("ipc_edge", "Привет, {name}!")
        result = self.tm.handle_apply_template({
            "name": "ipc_edge",
            "variables": "not_a_dict",  # Строка вместо dict
        })
        self.assertEqual(result["text"], "Привет, {name}!")

    def test_handle_remove_template_with_nonexistent(self):
        result = self.tm.handle_remove_template({"name": "nonexistent_via_ipc"})
        self.assertFalse(result["removed"])

    def test_handle_get_templates_returns_correct_structure(self):
        self.tm.add_template("ipc_struct", "Текст")
        result = self.tm.handle_get_templates({})
        self.assertIn("templates", result)
        self.assertIsInstance(result["templates"], list)
        self.assertTrue(len(result["templates"]) > 0)


class TestTemplatePlaceholderExtraction(unittest.TestCase):
    """Тесты для обнаружения и работы с плейсхолдерами {variable}."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tm = TemplateManager(data_dir=self.tmpdir)

    def _placeholders_in(self, text: str) -> list:
        """Вспомогательный метод: извлекает {variable} из текста."""
        import re
        return re.findall(r"\{(\w+)\}", text)

    def test_single_placeholder_detected(self):
        self.tm.add_template("single_ph", "Привет, {name}!")
        templates = self.tm.get_templates()
        t = next(x for x in templates if x["name"] == "single_ph")
        phs = self._placeholders_in(t["text"])
        self.assertEqual(phs, ["name"])

    def test_multiple_placeholders_detected(self):
        self.tm.add_template("multi_ph", "{greeting}, {name}! Ваш заказ {order}.")
        templates = self.tm.get_templates()
        t = next(x for x in templates if x["name"] == "multi_ph")
        phs = self._placeholders_in(t["text"])
        self.assertIn("greeting", phs)
        self.assertIn("name", phs)
        self.assertIn("order", phs)

    def test_no_placeholders_gives_empty_list(self):
        self.tm.add_template("no_ph", "Текст без переменных.")
        templates = self.tm.get_templates()
        t = next(x for x in templates if x["name"] == "no_ph")
        phs = self._placeholders_in(t["text"])
        self.assertEqual(phs, [])

    def test_render_substitutes_all_placeholders(self):
        """apply_template (render) заменяет все вхождения переменных."""
        self.tm.add_template("render_tpl", "{a} и {b}, и снова {a}")
        result = self.tm.apply_template("render_tpl", {"a": "Кот", "b": "Пёс"})
        self.assertEqual(result, "Кот и Пёс, и снова Кот")

    def test_render_with_none_variables_leaves_placeholders(self):
        """apply_template без variables оставляет {var} нетронутыми."""
        self.tm.add_template("ph_none", "Привет, {name}!")
        result = self.tm.apply_template("ph_none", None)
        self.assertEqual(result, "Привет, {name}!")

    def test_get_template_by_name_via_get_templates(self):
        """Получение шаблона по имени через get_templates (list/get паттерн)."""
        self.tm.add_template("lookup", "Найти меня", "test")
        templates = self.tm.get_templates()
        found = [t for t in templates if t["name"] == "lookup"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["text"], "Найти меня")
        self.assertEqual(found[0]["category"], "test")

    def test_list_returns_all_categories(self):
        """get_templates возвращает шаблоны разных категорий."""
        self.tm.add_template("cat_a", "Текст A", "alpha")
        self.tm.add_template("cat_b", "Текст B", "beta")
        templates = self.tm.get_templates()
        cats = {t["category"] for t in templates}
        self.assertIn("alpha", cats)
        self.assertIn("beta", cats)

    def test_placeholder_with_underscore_in_name(self):
        """Плейсхолдеры вида {first_name} корректно подставляются."""
        self.tm.add_template("underscore_ph", "Добрый день, {first_name} {last_name}!")
        result = self.tm.apply_template(
            "underscore_ph", {"first_name": "Иван", "last_name": "Петров"}
        )
        self.assertEqual(result, "Добрый день, Иван Петров!")

    def test_add_and_delete_template_lifecycle(self):
        """Полный цикл: добавить → найти → удалить → не найти."""
        self.tm.add_template("lifecycle", "Текст жизненного цикла")
        # Найти
        found = [t for t in self.tm.get_templates() if t["name"] == "lifecycle"]
        self.assertEqual(len(found), 1)
        # Удалить
        result = self.tm.remove_template("lifecycle")
        self.assertTrue(result)
        # Убедиться, что удалён
        found_after = [t for t in self.tm.get_templates() if t["name"] == "lifecycle"]
        self.assertEqual(len(found_after), 0)

    def test_render_nonexistent_raises_key_error(self):
        """apply_template (render) на несуществующем шаблоне → KeyError."""
        with self.assertRaises(KeyError):
            self.tm.apply_template("no_such_template_xyz", {"x": "y"})

    def test_builtin_template_has_placeholders(self):
        """Встроенные шаблоны содержат плейсхолдеры в тексте."""
        templates = self.tm.get_templates()
        greeting = next(t for t in templates if t["name"] == "greeting_ru")
        phs = self._placeholders_in(greeting["text"])
        self.assertIn("name", phs)

    def test_persistence_preserves_placeholders(self):
        """Плейсхолдеры в тексте сохраняются и восстанавливаются из файла."""
        self.tm.add_template("ph_persist", "Уважаемый {title} {name}!")
        tm2 = TemplateManager(data_dir=self.tmpdir)
        t = next(x for x in tm2.get_templates() if x["name"] == "ph_persist")
        phs = self._placeholders_in(t["text"])
        self.assertIn("title", phs)
        self.assertIn("name", phs)


if __name__ == "__main__":
    unittest.main()
