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


if __name__ == "__main__":
    unittest.main()
