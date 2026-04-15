"""Тесты системы профилей нормализации текста."""

from core.normalization_profiles import (
    NormalizationProfileRegistry,
    apply_profile,
    list_profiles,
)
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


class TestBuiltinProfiles(unittest.TestCase):
    """Проверяем наличие и поведение встроенных профилей."""

    def setUp(self):
        self.registry = NormalizationProfileRegistry(data_dir=None)

    def test_all_builtin_profiles_present(self):
        names = {p["name"] for p in self.registry.list_profiles()}
        self.assertIn("verbatim", names)
        self.assertIn("clean", names)
        self.assertIn("formal", names)
        self.assertIn("telegram", names)
        self.assertIn("subtitles", names)

    def test_builtin_flag_is_true(self):
        for p in self.registry.list_profiles():
            if p["name"] in ("verbatim", "clean", "formal", "telegram", "subtitles"):
                self.assertTrue(p["builtin"], f"Profile {p['name']} should be builtin")

    def test_each_builtin_has_description(self):
        for p in self.registry.list_profiles():
            self.assertIsInstance(p["description"], str)
            self.assertGreater(len(p["description"]), 0, f"No description for {p['name']}")

    def test_each_builtin_has_rules_list(self):
        for p in self.registry.list_profiles():
            self.assertIsInstance(p["rules"], list)


class TestVerbatimProfile(unittest.TestCase):

    def setUp(self):
        self.reg = NormalizationProfileRegistry()

    def test_verbatim_removes_hallucination(self):
        text = "Привет мир. Спасибо за просмотр."
        result = self.reg.apply_profile(text, "verbatim")
        self.assertNotIn("спасибо за просмотр", result.lower())

    def test_verbatim_keeps_repeated_words(self):
        # verbatim не трогает повторы — только галлюцинации
        text = "Привет привет привет"
        result = self.reg.apply_profile(text, "verbatim")
        # Текст не должен быть пустым и должен содержать хотя бы одно «Привет»
        self.assertIn("Привет", result)


class TestCleanProfile(unittest.TestCase):

    def setUp(self):
        self.reg = NormalizationProfileRegistry()

    def test_clean_normalizes_brand(self):
        text = "Это Меркадонна рядом с домом"
        result = self.reg.apply_profile(text, "clean")
        self.assertIn("Mercadona", result)

    def test_clean_normalizes_time(self):
        text = "Встреча в 15.00 часов"
        result = self.reg.apply_profile(text, "clean")
        self.assertIn("15:00", result)

    def test_clean_removes_trailing_hallucination(self):
        text = "Всё хорошо. Спасибо за просмотр."
        result = self.reg.apply_profile(text, "clean")
        self.assertNotIn("просмотр", result.lower())


class TestFormalProfile(unittest.TestCase):

    def setUp(self):
        self.reg = NormalizationProfileRegistry()

    def test_formal_capitalizes_first_letter(self):
        text = "привет, это тест"
        result = self.reg.apply_profile(text, "formal")
        self.assertTrue(result[0].isupper(), f"Expected uppercase start: {result!r}")


class TestTelegramProfile(unittest.TestCase):

    def setUp(self):
        self.reg = NormalizationProfileRegistry()

    def test_telegram_strips_trailing_period(self):
        text = "Всё готово."
        result = self.reg.apply_profile(text, "telegram")
        self.assertFalse(result.endswith("."), f"Should not end with period: {result!r}")

    def test_telegram_no_period_on_clean_text(self):
        text = "Привет как дела"
        result = self.reg.apply_profile(text, "telegram")
        self.assertFalse(result.endswith("."))


class TestSubtitlesProfile(unittest.TestCase):

    def setUp(self):
        self.reg = NormalizationProfileRegistry()

    def test_subtitles_wraps_long_lines(self):
        text = "Это очень длинная строка которая должна быть разбита на несколько более коротких строк"
        result = self.reg.apply_profile(text, "subtitles")
        for line in result.split("\n"):
            self.assertLessEqual(len(line), 42, f"Line too long ({len(line)}): {line!r}")

    def test_subtitles_short_text_unchanged_structure(self):
        text = "Короткий текст"
        result = self.reg.apply_profile(text, "subtitles")
        self.assertIn("Короткий", result)


class TestCustomProfiles(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.reg = NormalizationProfileRegistry(data_dir=Path(self._tmp))

    def test_add_custom_profile(self):
        self.reg.add_profile("myprofile", ["strip_hallucinations"], description="Мой профиль")
        names = {p["name"] for p in self.reg.list_profiles()}
        self.assertIn("myprofile", names)

    def test_custom_profile_not_builtin(self):
        self.reg.add_profile("myprofile", ["strip_hallucinations"])
        p = next(p for p in self.reg.list_profiles() if p["name"] == "myprofile")
        self.assertFalse(p["builtin"])

    def test_custom_profile_persisted_to_disk(self):
        self.reg.add_profile("diskprofile", ["cleanup_soft"], description="disk test")
        # Создаём новый реестр из той же директории
        reg2 = NormalizationProfileRegistry(data_dir=Path(self._tmp))
        names = {p["name"] for p in reg2.list_profiles()}
        self.assertIn("diskprofile", names)

    def test_cannot_overwrite_builtin_without_flag(self):
        with self.assertRaises(ValueError):
            self.reg.add_profile("clean", ["strip_hallucinations"])

    def test_remove_custom_profile(self):
        self.reg.add_profile("removeme", ["strip_hallucinations"])
        removed = self.reg.remove_profile("removeme")
        self.assertTrue(removed)
        names = {p["name"] for p in self.reg.list_profiles()}
        self.assertNotIn("removeme", names)

    def test_cannot_remove_builtin_profile(self):
        with self.assertRaises(ValueError):
            self.reg.remove_profile("clean")

    def test_unknown_profile_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.reg.apply_profile("текст", "nonexistent_profile")

    def test_empty_profile_name_raises(self):
        with self.assertRaises(ValueError):
            self.reg.add_profile("", ["strip_hallucinations"])


class TestModuleLevelFunctions(unittest.TestCase):
    """Проверяем публичные функции верхнего уровня модуля."""

    def test_list_profiles_returns_list(self):
        profiles = list_profiles()
        self.assertIsInstance(profiles, list)
        self.assertGreater(len(profiles), 0)

    def test_apply_profile_clean(self):
        result = apply_profile("Меркадонна — хороший магазин.", "clean")
        self.assertIn("Mercadona", result)

    def test_apply_profile_verbatim_passthrough(self):
        text = "Обычный текст без галлюцинаций"
        result = apply_profile(text, "verbatim")
        self.assertEqual(result, text)


if __name__ == "__main__":
    unittest.main()
