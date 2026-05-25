"""Тесты HallucinationManager — управление паттернами галлюцинаций."""

from __future__ import annotations
from core.hallucination_manager import HallucinationManager, HallucinationMatch

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

# Настройка путей для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


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


class TestStripHallucinationsEdgeCases(unittest.TestCase):
    """Граничные случаи strip_hallucinations."""

    def setUp(self):
        self.mgr = HallucinationManager()

    def test_strip_text_that_is_entirely_hallucination(self):
        # Текст — только галлюцинация → должен вернуть ""
        text = "спасибо за внимание."
        result = self.mgr.strip_hallucinations(text)
        self.assertEqual(result, "")

    def test_strip_hallucinations_empty_string(self):
        self.assertEqual(self.mgr.strip_hallucinations(""), "")

    def test_strip_does_not_alter_clean_text(self):
        text = "Это чистый текст без галлюцинаций."
        self.assertEqual(self.mgr.strip_hallucinations(text), text)


class TestCheckTextMatchDetails(unittest.TestCase):
    """Проверка деталей HallucinationMatch."""

    def setUp(self):
        self.mgr = HallucinationManager()

    def test_match_position_is_non_negative(self):
        text = "Обсуждаем план. Спасибо за просмотр."
        matches = self.mgr.check_text(text)
        for m in matches:
            self.assertGreaterEqual(m.position, 0)

    def test_match_matched_text_non_empty(self):
        text = "Хорошее видео. Спасибо за внимание."
        matches = self.mgr.check_text(text)
        for m in matches:
            self.assertIsInstance(m.matched_text, str)
            self.assertGreater(len(m.matched_text), 0)

    def test_multiple_patterns_can_match(self):
        # Добавляем второй паттерн и проверяем, что оба могут совпасть
        mgr = HallucinationManager()
        mgr.add_pattern(r"конец трансляции[.!?]*\s*$", category="broadcast")
        text1 = "Хорошее выступление. Спасибо за просмотр."
        text2 = "Хорошее выступление. Конец трансляции."
        matches1 = mgr.check_text(text1)
        matches2 = mgr.check_text(text2)
        self.assertGreater(len(matches1), 0)
        self.assertGreater(len(matches2), 0)
        self.assertIn("broadcast", {m.category for m in matches2})

    def test_case_insensitive_matching(self):
        # check_text работает с lowercased, поэтому регистр не важен
        text = "СПАСИБО ЗА ПРОСМОТР."
        matches = self.mgr.check_text(text)
        self.assertGreater(len(matches), 0, "Должно совпасть при верхнем регистре")


class TestCustomPatternMatchAfterRemove(unittest.TestCase):
    """После удаления паттерн не должен срабатывать."""

    def test_removed_pattern_no_longer_matches(self):
        mgr = HallucinationManager()
        mgr.add_pattern(r"удалённый паттерн\s*$", category="temp")
        text = "Полезный контент. Удалённый паттерн"
        self.assertGreater(len(mgr.check_text(text)), 0)

        mgr.remove_pattern(r"удалённый паттерн\s*$")
        # Теперь совпадения только от встроенных паттернов
        remaining = [m for m in mgr.check_text(text) if m.category == "temp"]
        self.assertEqual(remaining, [])


class TestUnicodePatterns(unittest.TestCase):
    """Тесты поддержки Unicode паттернов (кириллица, испанский и т.д.)."""

    def test_add_cyrillic_pattern(self):
        mgr = HallucinationManager()
        entry = mgr.add_pattern(r"конец вещания[.!?]*\s*$", category="broadcast_ru")
        self.assertEqual(entry["pattern"], r"конец вещания[.!?]*\s*$")
        self.assertEqual(entry["category"], "broadcast_ru")

    def test_cyrillic_pattern_matches_text(self):
        mgr = HallucinationManager()
        mgr.add_pattern(r"конец вещания[.!?]*\s*$", category="broadcast_ru")
        text = "Сегодня обсудили всё. Конец вещания."
        matches = mgr.check_text(text)
        cats = {m.category for m in matches}
        self.assertIn("broadcast_ru", cats)

    def test_add_spanish_unicode_pattern(self):
        mgr = HallucinationManager()
        entry = mgr.add_pattern(r"gracias por ver[.!?]*\s*$", category="youtube_es")
        self.assertEqual(entry["category"], "youtube_es")
        text = "Muy bien. Gracias por ver."
        matches = mgr.check_text(text)
        cats = {m.category for m in matches}
        self.assertIn("youtube_es", cats)

    def test_pattern_with_unicode_chars_special_class(self):
        """Паттерн с Unicode символами в character class."""
        mgr = HallucinationManager()
        mgr.add_pattern(r"[аеиоуыёэюя]{3,}\s*$", category="vowel_run")
        # Текст заканчивается на 3+ гласных кириллицей
        text = "какое-то слово уоауе"
        matches = mgr.check_text(text)
        cats = {m.category for m in matches}
        self.assertIn("vowel_run", cats)

    def test_strip_unicode_pattern(self):
        mgr = HallucinationManager()
        mgr.add_pattern(r"спасибо за вашу поддержку[.!?]*\s*$", category="support")
        text = "Хорошее видео. Спасибо за вашу поддержку."
        result = mgr.strip_hallucinations(text)
        self.assertNotIn("поддержку", result.lower())
        self.assertIn("Хорошее видео", result)


class TestConcurrentAddRemove(unittest.TestCase):
    """Тесты потокобезопасности add_pattern / remove_pattern."""

    def test_concurrent_add(self):
        """Concurrent add_pattern не приводит к гонке или потере записей."""
        mgr = HallucinationManager()
        errors: list[Exception] = []

        def add_patterns(start: int) -> None:
            for i in range(start, start + 10):
                try:
                    mgr.add_pattern(f"concurrent pattern {i}\\s*$", category="stress")
                except ValueError:
                    pass  # дубликаты ожидаемы при некотором перекрытии
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=add_patterns, args=(i * 10,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        custom = [p for p in mgr.list_patterns() if not p["builtin"]]
        # Должно быть ровно 50 уникальных паттернов (0..49)
        self.assertEqual(len(custom), 50)

    def test_concurrent_add_remove(self):
        """Concurrent add + remove не вызывает corrupt state или исключений."""
        mgr = HallucinationManager()
        # Предварительно добавляем паттерны для удаления
        for i in range(20):
            mgr.add_pattern(f"remove me {i}\\s*$", category="temp")

        errors: list[Exception] = []

        def remover() -> None:
            for i in range(20):
                try:
                    mgr.remove_pattern(f"remove me {i}\\s*$")
                except Exception as exc:
                    errors.append(exc)

        def adder() -> None:
            for i in range(20):
                try:
                    mgr.add_pattern(f"add new {i}\\s*$", category="new")
                except Exception as exc:
                    errors.append(exc)

        t1 = threading.Thread(target=remover)
        t2 = threading.Thread(target=adder)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(errors, [], f"Unexpected errors in concurrent add/remove: {errors}")
        # list_patterns() не должен падать после конкурентных операций
        patterns = mgr.list_patterns()
        self.assertIsInstance(patterns, list)

    def test_concurrent_check_text(self):
        """check_text безопасен для вызова из нескольких потоков."""
        mgr = HallucinationManager()
        mgr.add_pattern(r"check concurrent[.!?]*\s*$", category="check")
        errors: list[Exception] = []
        results: list[list] = []

        def checker() -> None:
            try:
                m = mgr.check_text("Текст. Check concurrent.")
                results.append(m)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=checker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent check_text errors: {errors}")
        self.assertEqual(len(results), 20)
        for r in results:
            cats = {m.category for m in r}
            self.assertIn("check", cats)


if __name__ == "__main__":
    unittest.main()
