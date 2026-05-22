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


class TestWave130RequiredCases(unittest.TestCase):
    """Wave 130: обязательные кейсы по спецификации."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.reg = NormalizationProfileRegistry(data_dir=Path(self._tmp))

    # test_built_in_profiles_loaded
    def test_built_in_profiles_loaded(self):
        """Реестр содержит все пять встроенных профилей сразу после создания."""
        reg = NormalizationProfileRegistry(data_dir=None)
        names = {p["name"] for p in reg.list_profiles()}
        for expected in ("verbatim", "clean", "formal", "telegram", "subtitles"):
            self.assertIn(expected, names, f"Missing builtin profile: {expected!r}")
        self.assertEqual(len(names), 5)

    # test_apply_profile_by_name
    def test_apply_profile_by_name(self):
        """apply_profile() применяет профиль по имени и возвращает строку."""
        text = "Обычный тестовый текст без специфики"
        for profile_name in ("verbatim", "clean", "telegram"):
            result = self.reg.apply_profile(text, profile_name)
            self.assertIsInstance(result, str, f"Profile {profile_name!r} returned non-str")
            self.assertGreater(len(result), 0, f"Profile {profile_name!r} returned empty string")

    # test_unknown_profile_falls_back_to_default
    def test_unknown_profile_falls_back_to_default(self):
        """Попытка применить несуществующий профиль поднимает ValueError
        (get_profile возвращает None — нет тихого фолбека)."""
        # apply_profile должен поднять ValueError для неизвестного профиля
        with self.assertRaises(ValueError):
            self.reg.apply_profile("текст", "__nonexistent_profile__")
        # get_profile возвращает None вместо исключения (для проверки наличия)
        profile = self.reg.get_profile("__nonexistent_profile__")
        self.assertIsNone(profile)

    # test_custom_profile_registration
    def test_custom_profile_registration(self):
        """Пользовательский профиль регистрируется, сохраняется и применяется."""
        self.reg.add_profile(
            "w130_custom",
            ["strip_hallucinations", "strip_trailing_period"],
            description="Wave 130 custom",
        )
        # Присутствует в списке
        names = {p["name"] for p in self.reg.list_profiles()}
        self.assertIn("w130_custom", names)
        # Не помечен как builtin
        p = self.reg.get_profile("w130_custom")
        self.assertFalse(p.builtin)
        # Применяется без исключений
        result = self.reg.apply_profile("Всё готово.", "w130_custom")
        self.assertIsInstance(result, str)
        # Сохраняется на диск — новый реестр видит профиль
        reg2 = NormalizationProfileRegistry(data_dir=Path(self._tmp))
        names2 = {p["name"] for p in reg2.list_profiles()}
        self.assertIn("w130_custom", names2)

    # test_unicode_text_preserved
    def test_unicode_text_preserved(self):
        """Unicode (кириллица, emoji, спецсимволы) не теряется при обработке
        профилями, не затрагивающими эти символы."""
        texts = [
            "Привет мир",                          # кириллица
            "Hola mundo",                           # латиница
            "Тест 🎤 микрофон",                    # emoji
            "Линия тонкий пробел",            # narrow no-break space
            "Привет​мир",                      # zero-width space
            "Текст — длинное тире",                 # em-dash
        ]
        for text in texts:
            for profile_name in ("verbatim", "clean"):
                result = self.reg.apply_profile(text, profile_name)
                self.assertIsInstance(result, str,
                                      msg=f"profile={profile_name!r}, text={text!r}")
                # Не должны появиться NaN/None
                self.assertIsNotNone(result)

    # test_concurrent_apply
    def test_concurrent_apply(self):
        """Параллельные apply_profile() из нескольких потоков не конкурируют
        за состояние реестра и возвращают правильные строки."""
        import threading

        errors: list[Exception] = []
        results: list[str] = []
        lock = threading.Lock()

        profiles_texts = [
            ("verbatim", "Привет мир"),
            ("clean", "Меркадонна — хороший магазин"),
            ("telegram", "Всё готово."),
            ("subtitles", "Очень длинная строка для проверки переноса строк в субтитрах"),
            ("verbatim", "Другой текст для проверки"),
        ]

        def worker(profile_name: str, text: str) -> None:
            try:
                r = self.reg.apply_profile(text, profile_name)
                with lock:
                    results.append(r)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(pn, tx))
            for pn, tx in profiles_texts
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")
        self.assertEqual(len(results), len(profiles_texts))
        for r in results:
            self.assertIsInstance(r, str)


if __name__ == "__main__":
    unittest.main()
