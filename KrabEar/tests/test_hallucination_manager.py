"""Тесты HallucinationManager — управление паттернами галлюцинаций."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Настройка путей для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.hallucination_manager import HallucinationManager, HallucinationMatch


class TestBuiltinPatterns(unittest.TestCase):
    """Тесты встроенных паттернов галлюцинаций (из TextUtils)."""

    def setUp(self):
        self.mgr = HallucinationManager()  # data_dir=None → in-memory

    def test_list_patterns_includes_builtins(self):
        patterns = self.mgr.list_patterns()
        self.assertGreater(len(patterns), 0, "Должны быть встроенные паттерны")
        # Все встроенные отмечены builtin=True
        builtin = [p for p in patterns if p["builtin"]]
        self.assertGreater(len(builtin), 0)

    def test_builtin_pattern_has_required_fields(self):
        patterns = self.mgr.list_patterns()
        for p in patterns:
            self.assertIn("pattern", p)
            self.assertIn("category", p)
            self.assertIn("builtin", p)

    def test_check_text_youtube_hallucination(self):
        text = "Это нормальный текст. Спасибо за просмотр."
        matches = self.mgr.check_text(text)
        self.assertTrue(len(matches) > 0, "Должно найти YouTube-галлюцинацию")
        categories = {m.category for m in matches}
        self.assertIn("youtube", categories)

    def test_check_text_no_hallucination(self):
        text = "Завтра встреча в 15:00 обсуждаем бюджет проекта."
        matches = self.mgr.check_text(text)
        self.assertEqual(matches, [], "Нормальный текст не должен иметь совпадений")

    def test_check_text_empty_string(self):
        matches = self.mgr.check_text("")
        self.assertEqual(matches, [])

    def test_strip_hallucinations_removes_trailing(self):
        text = "Хорошее выступление. Спасибо за внимание."
        result = self.mgr.strip_hallucinations(text)
        self.assertNotIn("спасибо за внимание", result.lower())
        self.assertIn("Хорошее выступление", result)

    def test_strip_hallucinations_clean_text_unchanged(self):
        text = "Обсуждаем план на следующий квартал."
        result = self.mgr.strip_hallucinations(text)
        self.assertEqual(result, text)

    def test_cannot_remove_builtin_pattern(self):
        builtin_patterns = [p["pattern"] for p in self.mgr.list_patterns() if p["builtin"]]
        self.assertGreater(len(builtin_patterns), 0)
        with self.assertRaises(ValueError):
            self.mgr.remove_pattern(builtin_patterns[0])


class TestCustomPatterns(unittest.TestCase):
    """Тесты добавления и удаления пользовательских паттернов."""

    def setUp(self):
        self.mgr = HallucinationManager()  # in-memory

    def test_add_custom_pattern(self):
        entry = self.mgr.add_pattern(r"тестовый паттерн\s*$", category="test")
        self.assertEqual(entry["pattern"], r"тестовый паттерн\s*$")
        self.assertEqual(entry["category"], "test")
        self.assertFalse(entry["builtin"])

    def test_add_custom_pattern_appears_in_list(self):
        self.mgr.add_pattern(r"мой паттерн\s*$")
        patterns = self.mgr.list_patterns()
        custom = [p for p in patterns if not p["builtin"]]
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0]["pattern"], r"мой паттерн\s*$")

    def test_default_category_is_custom(self):
        entry = self.mgr.add_pattern(r"пустая категория\s*$")
        self.assertEqual(entry["category"], "custom")

    def test_add_duplicate_pattern_raises(self):
        self.mgr.add_pattern(r"уникальный паттерн\s*$")
        with self.assertRaises(ValueError):
            self.mgr.add_pattern(r"уникальный паттерн\s*$")

    def test_add_invalid_regex_raises(self):
        with self.assertRaises(ValueError):
            self.mgr.add_pattern(r"[невалид(")

    def test_add_empty_pattern_raises(self):
        with self.assertRaises(ValueError):
            self.mgr.add_pattern("   ")

    def test_remove_custom_pattern_returns_true(self):
        self.mgr.add_pattern(r"удаляемый паттерн\s*$")
        result = self.mgr.remove_pattern(r"удаляемый паттерн\s*$")
        self.assertTrue(result)

    def test_remove_custom_pattern_disappears_from_list(self):
        self.mgr.add_pattern(r"временный паттерн\s*$")
        self.mgr.remove_pattern(r"временный паттерн\s*$")
        patterns = self.mgr.list_patterns()
        custom = [p for p in patterns if not p["builtin"]]
        self.assertEqual(custom, [])

    def test_remove_nonexistent_pattern_returns_false(self):
        result = self.mgr.remove_pattern(r"несуществующий паттерн\s*$")
        self.assertFalse(result)

    def test_check_text_with_custom_pattern(self):
        self.mgr.add_pattern(r"конец сессии[.!?]*\s*$", category="session")
        text = "Обсудили все вопросы. Конец сессии."
        matches = self.mgr.check_text(text)
        session_matches = [m for m in matches if m.category == "session"]
        self.assertEqual(len(session_matches), 1)
        self.assertEqual(session_matches[0].pattern, r"конец сессии[.!?]*\s*$")

    def test_strip_hallucinations_uses_custom_pattern(self):
        self.mgr.add_pattern(r"кастомная галлюцинация[.!?]*\s*$", category="custom")
        text = "Важное содержание. Кастомная галлюцинация."
        result = self.mgr.strip_hallucinations(text)
        self.assertNotIn("Кастомная галлюцинация", result)
        self.assertIn("Важное содержание", result)

    def test_hallucination_match_has_correct_fields(self):
        self.mgr.add_pattern(r"матч тест\s*$", category="test_cat")
        text = "Какой-то текст. Матч тест"
        matches = self.mgr.check_text(text)
        test_matches = [m for m in matches if m.category == "test_cat"]
        self.assertEqual(len(test_matches), 1)
        m = test_matches[0]
        self.assertIsInstance(m, HallucinationMatch)
        self.assertEqual(m.category, "test_cat")
        self.assertIsInstance(m.position, int)
        self.assertIsInstance(m.matched_text, str)

    def test_hallucination_match_to_dict(self):
        self.mgr.add_pattern(r"словарь тест\s*$", category="dict_test")
        text = "Текст. Словарь тест"
        matches = self.mgr.check_text(text)
        dict_matches = [m for m in matches if m.category == "dict_test"]
        self.assertEqual(len(dict_matches), 1)
        d = dict_matches[0].to_dict()
        self.assertIn("pattern", d)
        self.assertIn("matched_text", d)
        self.assertIn("position", d)
        self.assertIn("category", d)


class TestPersistence(unittest.TestCase):
    """Тесты персистентности пользовательских паттернов."""

    def test_custom_patterns_saved_to_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            mgr = HallucinationManager(data_dir=data_dir)
            mgr.add_pattern(r"сохранённый паттерн\s*$", category="saved")

            persist_path = data_dir / "hallucination_patterns.json"
            self.assertTrue(persist_path.exists(), "JSON файл должен быть создан")

            data = json.loads(persist_path.read_text(encoding="utf-8"))
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["pattern"], r"сохранённый паттерн\s*$")
            self.assertEqual(data[0]["category"], "saved")

    def test_custom_patterns_loaded_on_init(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            # Первый экземпляр: добавляем паттерн
            mgr1 = HallucinationManager(data_dir=data_dir)
            mgr1.add_pattern(r"загружаемый паттерн\s*$", category="loaded")

            # Второй экземпляр: должен загрузить паттерн из файла
            mgr2 = HallucinationManager(data_dir=data_dir)
            custom = [p for p in mgr2.list_patterns() if not p["builtin"]]
            self.assertEqual(len(custom), 1)
            self.assertEqual(custom[0]["pattern"], r"загружаемый паттерн\s*$")

    def test_remove_updates_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            mgr = HallucinationManager(data_dir=data_dir)
            mgr.add_pattern(r"удаляемый\s*$", category="test")
            mgr.remove_pattern(r"удаляемый\s*$")

            persist_path = data_dir / "hallucination_patterns.json"
            data = json.loads(persist_path.read_text(encoding="utf-8"))
            self.assertEqual(data, [])

    def test_in_memory_no_file_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # data_dir=None → in-memory only, no files
            mgr = HallucinationManager(data_dir=None)
            mgr.add_pattern(r"инмемори\s*$")
            # Не должны создавать файлы в текущей директории
            self.assertFalse((Path(tmpdir) / "hallucination_patterns.json").exists())


if __name__ == "__main__":
    unittest.main()
