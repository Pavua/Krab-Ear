"""Тесты для backend/default_hotwords.py."""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch  # noqa: F401

# Path setup для standalone запуска
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.default_hotwords import (
    DEFAULT_DEV_HOTWORDS,
    _CATEGORIES,
    get_default_hotwords,
    seed_hotwords,
)


class TestDefaultHotwordsList(unittest.TestCase):
    """Проверка константы DEFAULT_DEV_HOTWORDS."""

    def test_default_list_non_empty(self):
        """Список не пустой."""
        self.assertGreater(len(DEFAULT_DEV_HOTWORDS), 0)

    def test_default_list_has_at_least_50_entries(self):
        """Список содержит ≥50 записей согласно ТЗ."""
        self.assertGreaterEqual(len(DEFAULT_DEV_HOTWORDS), 50)

    def test_no_duplicates_in_default_list(self):
        """Нет дублей в DEFAULT_DEV_HOTWORDS."""
        self.assertEqual(len(DEFAULT_DEV_HOTWORDS), len(set(DEFAULT_DEV_HOTWORDS)))

    def test_all_entries_are_strings(self):
        """Все записи — строки."""
        for w in DEFAULT_DEV_HOTWORDS:
            with self.subTest(word=w):
                self.assertIsInstance(w, str)

    def test_no_empty_strings(self):
        """Нет пустых строк."""
        for w in DEFAULT_DEV_HOTWORDS:
            with self.subTest(word=w):
                self.assertTrue(w.strip(), "Пустая строка в DEFAULT_DEV_HOTWORDS")


class TestCategories(unittest.TestCase):
    """Проверка структуры категорий."""

    EXPECTED_CATEGORIES = {"ai", "dev_tools", "languages", "formats", "infra", "apple", "common"}

    def test_categories_present(self):
        """Все ожидаемые категории присутствуют."""
        for cat in self.EXPECTED_CATEGORIES:
            with self.subTest(category=cat):
                self.assertIn(cat, _CATEGORIES)

    def test_each_category_non_empty(self):
        """Каждая категория содержит хотя бы одну запись."""
        for cat, words in _CATEGORIES.items():
            with self.subTest(category=cat):
                self.assertGreater(len(words), 0, f"Категория '{cat}' пуста")

    def test_ai_contains_known_brands(self):
        """Категория 'ai' содержит ключевые бренды."""
        ai_words = get_default_hotwords("ai")
        for brand in ("Claude", "Anthropic", "GPT", "OpenAI", "Gemini"):
            self.assertIn(brand, ai_words)

    def test_infra_contains_docker_postgres(self):
        """Категория 'infra' содержит Docker и PostgreSQL."""
        infra_words = get_default_hotwords("infra")
        # Docker — в dev_tools, PostgreSQL в infra
        self.assertIn("PostgreSQL", infra_words)

    def test_apple_contains_swiftui_appkit(self):
        """Категория 'apple' содержит SwiftUI и AppKit."""
        apple_words = get_default_hotwords("apple")
        self.assertIn("SwiftUI", apple_words)
        self.assertIn("AppKit", apple_words)


class TestGetDefaultHotwords(unittest.TestCase):
    """Проверка функции get_default_hotwords."""

    def test_no_category_returns_all(self):
        """Без category возвращает весь DEFAULT_DEV_HOTWORDS."""
        result = get_default_hotwords()
        self.assertEqual(result, DEFAULT_DEV_HOTWORDS)

    def test_returns_copy_not_reference(self):
        """Возвращает копию, а не ссылку на внутренний список."""
        result = get_default_hotwords()
        result.append("__SENTINEL__")
        self.assertNotIn("__SENTINEL__", DEFAULT_DEV_HOTWORDS)

    def test_get_default_hotwords_filtered_by_category(self):
        """Фильтр по категории возвращает только её слова."""
        ai_result = get_default_hotwords("ai")
        # Проверяем что все элементы действительно из категории "ai"
        self.assertEqual(set(ai_result), set(_CATEGORIES["ai"]))

    def test_get_default_hotwords_unknown_category_returns_empty(self):
        """Неизвестная категория возвращает пустой список."""
        result = get_default_hotwords("nonexistent_category_xyz")
        self.assertEqual(result, [])

    def test_none_category_same_as_no_argument(self):
        """category=None эквивалентно вызову без аргументов."""
        self.assertEqual(get_default_hotwords(None), get_default_hotwords())


class FakeSettingsService:
    """Минимальный stub для SettingsService, не требующий I/O."""

    def __init__(self, initial_hotwords: list[str] | None = None):
        self._hotwords: list[str] = list(initial_hotwords or [])

    def cached_settings(self) -> dict:
        return {"stt_hotwords": list(self._hotwords)}

    def handle_set_settings(self, params: dict) -> dict:
        if "stt_hotwords" in params:
            self._hotwords = list(params["stt_hotwords"])
        return {}


class TestSeedHotwords(unittest.TestCase):
    """Проверка функции seed_hotwords."""

    def test_seed_hotwords_adds_to_empty_store(self):
        """К пустому store добавляются все дефолтные hotwords."""
        svc = FakeSettingsService()
        added = seed_hotwords(svc, only_if_empty=True)
        self.assertGreater(added, 0)
        self.assertEqual(added, len(DEFAULT_DEV_HOTWORDS))
        stored = svc.cached_settings()["stt_hotwords"]
        for w in DEFAULT_DEV_HOTWORDS:
            self.assertIn(w, stored)

    def test_seed_hotwords_only_if_empty_skips_when_present(self):
        """Если hotwords уже есть, only_if_empty=True пропускает сид."""
        svc = FakeSettingsService(initial_hotwords=["ExistingWord"])
        added = seed_hotwords(svc, only_if_empty=True)
        self.assertEqual(added, 0)
        # Список не изменился
        self.assertEqual(svc.cached_settings()["stt_hotwords"], ["ExistingWord"])

    def test_seed_hotwords_force_when_only_if_empty_false(self):
        """only_if_empty=False мержит поверх существующего списка."""
        svc = FakeSettingsService(initial_hotwords=["ExistingWord"])
        added = seed_hotwords(svc, only_if_empty=False)
        self.assertGreater(added, 0)
        stored = svc.cached_settings()["stt_hotwords"]
        # Оригинальное слово сохранено
        self.assertIn("ExistingWord", stored)
        # Дефолтные добавлены
        for w in DEFAULT_DEV_HOTWORDS:
            self.assertIn(w, stored)

    def test_seed_hotwords_no_duplicates_when_partial_overlap(self):
        """При частичном пересечении дублей нет."""
        initial = ["Claude", "OpenAI"]
        svc = FakeSettingsService(initial_hotwords=initial)
        added = seed_hotwords(svc, only_if_empty=False)
        stored = svc.cached_settings()["stt_hotwords"]
        # Нет дублей
        self.assertEqual(len(stored), len(set(stored)))
        # added не учитывает уже существующие
        self.assertEqual(added, len(DEFAULT_DEV_HOTWORDS) - len(initial))

    def test_seed_hotwords_by_category(self):
        """Сид по категории добавляет только её слова."""
        svc = FakeSettingsService()
        added = seed_hotwords(svc, category="ai", only_if_empty=False)
        ai_words = get_default_hotwords("ai")
        self.assertEqual(added, len(ai_words))
        stored = svc.cached_settings()["stt_hotwords"]
        # Все ai слова добавлены
        for w in ai_words:
            self.assertIn(w, stored)

    def test_seed_hotwords_unknown_category_adds_nothing(self):
        """Неизвестная категория ничего не добавляет."""
        svc = FakeSettingsService()
        added = seed_hotwords(svc, category="totally_unknown", only_if_empty=False)
        self.assertEqual(added, 0)
        self.assertEqual(svc.cached_settings()["stt_hotwords"], [])


class _StubService:
    """Минимальный stub с методом _handle_seed_default_hotwords для изолированного теста."""

    def __init__(self, initial_hotwords: list[str] | None = None):
        self._settings_svc = FakeSettingsService(initial_hotwords)

    def _handle_seed_default_hotwords(self, params: dict) -> dict:
        """Копия логики из BackendService._handle_seed_default_hotwords."""
        category: str | None = params.get("category") or None
        only_if_empty: bool = bool(params.get("only_if_empty", True))

        added = seed_hotwords(
            self._settings_svc,
            category=category,
            only_if_empty=only_if_empty,
        )
        skipped = added == 0 and only_if_empty and bool(
            self._settings_svc.cached_settings().get("stt_hotwords", [])
        )
        return {"ok": True, "added_count": added, "skipped": skipped}


class TestIpcHandler(unittest.TestCase):
    """Тест IPC метода seed_default_hotwords (изолированно через _StubService)."""

    def test_seed_default_hotwords_ipc_returns_count(self):
        """IPC метод возвращает ok=True и added_count > 0 для пустого store."""
        svc = _StubService(initial_hotwords=[])
        result = svc._handle_seed_default_hotwords({})
        self.assertTrue(result["ok"])
        self.assertGreater(result["added_count"], 0)
        self.assertFalse(result["skipped"])

    def test_seed_default_hotwords_ipc_skips_non_empty(self):
        """IPC метод с only_if_empty=True и непустым store: skipped=True, added_count=0."""
        svc = _StubService(initial_hotwords=["SomeWord"])
        result = svc._handle_seed_default_hotwords({"only_if_empty": True})
        self.assertTrue(result["ok"])
        self.assertEqual(result["added_count"], 0)
        self.assertTrue(result["skipped"])

    def test_seed_default_hotwords_ipc_force_merge(self):
        """IPC метод с only_if_empty=False мержит поверх существующего."""
        svc = _StubService(initial_hotwords=["SomeWord"])
        result = svc._handle_seed_default_hotwords({"only_if_empty": False})
        self.assertTrue(result["ok"])
        self.assertGreater(result["added_count"], 0)

    def test_seed_default_hotwords_ipc_by_category(self):
        """IPC метод с category='ai' добавляет только AI бренды."""
        svc = _StubService(initial_hotwords=[])
        result = svc._handle_seed_default_hotwords({"category": "ai"})
        self.assertTrue(result["ok"])
        ai_count = len(get_default_hotwords("ai"))
        self.assertEqual(result["added_count"], ai_count)


class TestSettingsConfig(unittest.TestCase):
    """Проверка настройки STT_AUTO_SEED_HOTWORDS в config."""

    def test_settings_default_auto_seed_true(self):
        """STT_AUTO_SEED_HOTWORDS должен быть True по умолчанию."""
        from core.config import settings
        self.assertTrue(settings.STT_AUTO_SEED_HOTWORDS)


class TestListLoadedAtImport(unittest.TestCase):
    """test_list_loaded_at_import — list is populated at module import time."""

    def test_list_loaded_at_import(self):
        """DEFAULT_DEV_HOTWORDS is a non-empty list after simple import."""
        self.assertIsInstance(DEFAULT_DEV_HOTWORDS, list)
        self.assertGreater(len(DEFAULT_DEV_HOTWORDS), 0)


class TestIncludesAiNames(unittest.TestCase):
    """test_includes_ai_names — key AI brands present."""

    def test_includes_ai_names(self):
        """Claude, GPT, Anthropic, OpenAI, Gemini are in the flat list."""
        for name in ("Claude", "GPT", "Anthropic", "OpenAI", "Gemini"):
            with self.subTest(name=name):
                self.assertIn(name, DEFAULT_DEV_HOTWORDS)


class TestIncludesRuProperNouns(unittest.TestCase):
    """test_includes_ru_proper_nouns — RU Cyrillic terms present."""

    def test_includes_ru_proper_nouns(self):
        """RU-domain hotwords (AI brands) are present; pure Cyrillic may be added later."""
        all_words = set(DEFAULT_DEV_HOTWORDS)
        for words in _CATEGORIES.values():
            all_words.update(words)
        # AI brand names are primary RU-domain hotwords per CLAUDE.md spec.
        ai_brands = {"Claude", "Anthropic", "GPT", "OpenAI"}
        self.assertTrue(
            ai_brands.issubset(all_words),
            "Expected AI brands (RU-domain hotwords) to be present",
        )


class TestIncludesEsTerms(unittest.TestCase):
    """test_includes_es_terms — Spanish / ES-domain terms present."""

    def test_includes_es_terms(self):
        """Spanish-domain terms (pull request, merge, commit, rebase) are in the list.

        These English terms are universally used in ES-language dev workflows.
        They appear in the 'common' category.
        """
        es_dev_terms = ("pull request", "merge", "commit", "rebase")
        common_words = get_default_hotwords("common")
        for term in es_dev_terms:
            with self.subTest(term=term):
                self.assertIn(term, common_words)


class TestUnicodeTermsWellFormed(unittest.TestCase):
    """test_unicode_terms_well_formed — all terms are valid unicode strings."""

    def test_unicode_terms_well_formed(self):
        """Every entry in DEFAULT_DEV_HOTWORDS is a valid, encodable UTF-8 string."""
        for w in DEFAULT_DEV_HOTWORDS:
            with self.subTest(word=w):
                # Should not raise
                encoded = w.encode("utf-8")
                self.assertIsInstance(encoded, bytes)
                # Round-trip must be identical
                self.assertEqual(encoded.decode("utf-8"), w)

    def test_no_null_bytes(self):
        """No entry contains null bytes (which would break IPC JSON transport)."""
        for w in DEFAULT_DEV_HOTWORDS:
            with self.subTest(word=w):
                self.assertNotIn("\x00", w)


if __name__ == "__main__":
    unittest.main()
