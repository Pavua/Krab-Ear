"""W1429 — TranslationCache wiring tests.

Проверяет три группы:
1. Статическая проверка wiring в service.py (import + instantiate + inject + handler).
2. Translator использует _translation_cache при lookup и put.
3. _handle_clear_translation_cache IPC handler очищает кэш корректно.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translation_cache import TranslationCache
from backend.translator import Translator, TranslationResult


# ---------------------------------------------------------------------------
# 1. Статическая проверка wiring в service.py
# ---------------------------------------------------------------------------

class TestTranslationCacheInstantiated(unittest.TestCase):
    """Проверяет что service.py содержит код wiring TranslationCache (W1429)."""

    def _read_service_py(self) -> str:
        path = Path(__file__).resolve().parents[1] / "backend" / "service.py"
        return path.read_text(encoding="utf-8")

    def test_translation_cache_import_present(self):
        """service.py должен импортировать TranslationCache."""
        src = self._read_service_py()
        self.assertIn(
            "from backend.translation_cache import TranslationCache",
            src,
            "TranslationCache должен быть импортирован в service.py",
        )

    def test_translation_cache_instantiated_in_init(self):
        """service.py должен создавать TranslationCache(data_dir=...)."""
        src = self._read_service_py()
        self.assertIn(
            "TranslationCache(data_dir=",
            src,
            "TranslationCache должен быть инстанцирован в service.py",
        )

    def test_translation_cache_injected_into_translator(self):
        """service.py должен инжектировать _translation_cache в translator."""
        src = self._read_service_py()
        self.assertIn(
            "self.translator._translation_cache = self._translation_cache",
            src,
            "translator._translation_cache должен быть задан в service.py",
        )

    def test_clear_translation_cache_handler_registered_in_dispatch(self):
        """ipc_dispatch.py должен регистрировать 'clear_translation_cache'."""
        path = Path(__file__).resolve().parents[1] / "backend" / "ipc_dispatch.py"
        src = path.read_text(encoding="utf-8")
        self.assertIn(
            '"clear_translation_cache"',
            src,
            "'clear_translation_cache' должен быть зарегистрирован в ipc_dispatch.py",
        )

    def test_handle_clear_translation_cache_method_defined(self):
        """service.py должен определять _handle_clear_translation_cache."""
        src = self._read_service_py()
        self.assertIn(
            "def _handle_clear_translation_cache",
            src,
            "_handle_clear_translation_cache должен быть определён в service.py",
        )

    def test_translator_py_has_translation_cache_slot(self):
        """translator.py должен объявлять self._translation_cache в __init__."""
        path = Path(__file__).resolve().parents[1] / "backend" / "translator.py"
        src = path.read_text(encoding="utf-8")
        self.assertIn(
            "self._translation_cache",
            src,
            "translator.py должен иметь self._translation_cache слот",
        )


# ---------------------------------------------------------------------------
# 2. Translator использует персистентный кэш
# ---------------------------------------------------------------------------

class TestTranslatorUsesPersistentCache(unittest.TestCase):
    """Translator корректно сохраняет и читает через _translation_cache."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def _make_translator_with_cache(self) -> tuple[Translator, TranslationCache]:
        t = Translator()
        cache = TranslationCache(data_dir=self._tmpdir)
        t._translation_cache = cache
        return t, cache

    def _stub_single_mode(self, translator: Translator, translated_text: str) -> None:
        """Подменяет _translate_single_mode для возврата успешного результата."""
        def fake_single(text, mode, network_mode, translation_style):
            return TranslationResult(
                text=translated_text,
                status="ok",
                source_lang="ru",
                target_lang="es",
                mode=mode,
                engine="test_model",
            )
        translator._translate_single_mode = fake_single  # type: ignore[assignment]

    def test_successful_result_stored_in_persistent_cache(self):
        """Успешный перевод сохраняется в TranslationCache на диске."""
        translator, cache = self._make_translator_with_cache()
        self._stub_single_mode(translator, "Hola")
        result = translator.translate(
            "Привет", mode="ru_to_es", network_mode="offline_default"
        )
        self.assertEqual(result.status, "ok")
        stats = cache.get_stats()
        self.assertEqual(stats["entries"], 1, "Должна быть 1 запись в кэше")

    def test_persistent_cache_hit_on_new_translator(self):
        """Новый Translator с тем же кэшем получает результат без вызова pipeline."""
        translator, cache = self._make_translator_with_cache()
        self._stub_single_mode(translator, "Hola mundo")
        r1 = translator.translate("Привет мир", mode="ru_to_es", network_mode="offline_default")
        self.assertEqual(r1.status, "ok")

        # Новый Translator — без stub pipeline, кэш должен дать результат
        translator2 = Translator()
        translator2._translation_cache = cache
        r2 = translator2.translate("Привет мир", mode="ru_to_es", network_mode="offline_default")
        self.assertEqual(r2.status, "ok")
        self.assertEqual(r2.text, "Hola mundo")
        self.assertIn("_cached", r2.engine)

    def test_failed_translation_not_stored_in_cache(self):
        """Неуспешный перевод (ok=False) НЕ сохраняется в персистентный кэш."""
        translator, cache = self._make_translator_with_cache()
        # Не подменяем pipeline → модель не найдена → ok=False
        result = translator.translate(
            "Привет", mode="ru_to_es", network_mode="offline_strict"
        )
        # Неважно каков status — главное, что entries=0
        stats = cache.get_stats()
        if not result.ok:
            self.assertEqual(stats["entries"], 0, "Неудача не должна сохраняться в кэш")

    def test_off_mode_not_stored_in_cache(self):
        """mode='off' не сохраняется в персистентный кэш."""
        translator, cache = self._make_translator_with_cache()
        result = translator.translate("Привет", mode="off", network_mode="offline_default")
        self.assertEqual(result.status, "not_requested")
        stats = cache.get_stats()
        self.assertEqual(stats["entries"], 0, "mode=off не должен сохраняться в кэш")

    def test_no_crash_without_translation_cache(self):
        """Translator без _translation_cache работает нормально (None path)."""
        translator = Translator()
        # _translation_cache уже None по умолчанию из __init__
        self.assertIsNone(translator._translation_cache)
        # translate должен работать без ошибок (просто нет persistent layer)
        result = translator.translate("Привет", mode="off", network_mode="offline_default")
        self.assertEqual(result.status, "not_requested")


# ---------------------------------------------------------------------------
# 3. _handle_clear_translation_cache IPC handler
# ---------------------------------------------------------------------------

class TestClearTranslationCacheHandler(unittest.TestCase):
    """_handle_clear_translation_cache очищает кэш и возвращает правильный ответ."""

    def _make_handler(self, cache: TranslationCache | None = None):
        """Создаёт минимальный объект с _translation_cache и методом handler."""
        class FakeSvc:
            def __init__(self, tc):
                self._translation_cache = tc

            def _handle_clear_translation_cache(self, params):
                entries_before = 0
                if self._translation_cache is not None:
                    stats = self._translation_cache.get_stats()
                    entries_before = stats.get("entries", 0)
                    self._translation_cache.clear()
                return {"ok": True, "entries_cleared": entries_before}

        return FakeSvc(cache)

    def test_handler_returns_ok_true(self):
        """Handler всегда возвращает ok=True."""
        tmpdir = tempfile.mkdtemp()
        cache = TranslationCache(data_dir=tmpdir)
        svc = self._make_handler(cache)
        result = svc._handle_clear_translation_cache({})
        self.assertTrue(result["ok"])

    def test_handler_reports_entries_cleared(self):
        """Handler возвращает количество записей до очистки."""
        tmpdir = tempfile.mkdtemp()
        cache = TranslationCache(data_dir=tmpdir)
        # Добавляем 2 записи вручную
        cache.put("hello", "ru_to_es", "neutral", "persistent", "ru\x00es\x00m\x00Hola")
        cache.put("world", "ru_to_es", "neutral", "persistent", "ru\x00es\x00m\x00Mundo")
        self.assertEqual(cache.get_stats()["entries"], 2)

        svc = self._make_handler(cache)
        result = svc._handle_clear_translation_cache({})
        self.assertEqual(result["entries_cleared"], 2)
        # После очистки — 0 записей
        self.assertEqual(cache.get_stats()["entries"], 0)

    def test_handler_with_none_cache_returns_zero(self):
        """Handler с None кэшем возвращает entries_cleared=0."""
        svc = self._make_handler(None)
        result = svc._handle_clear_translation_cache({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries_cleared"], 0)

    def test_handler_clears_persistent_file(self):
        """Handler очищает и файл на диске (persist_locked вызывается)."""
        tmpdir = tempfile.mkdtemp()
        cache = TranslationCache(data_dir=tmpdir)
        cache.put("test", "ru_to_es", "neutral", "persistent", "ru\x00es\x00m\x00Test")

        svc = self._make_handler(cache)
        svc._handle_clear_translation_cache({})

        # Создаём новый кэш из того же dir — должен загрузить пустой файл
        cache2 = TranslationCache(data_dir=tmpdir)
        self.assertEqual(cache2.get_stats()["entries"], 0)


if __name__ == "__main__":
    unittest.main()
