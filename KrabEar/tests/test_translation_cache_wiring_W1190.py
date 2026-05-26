"""W1190 — TranslationCache wiring tests.

Проверяем три вещи:
1. BackendService инстанцирует TranslationCache и инжектирует в self.translator
   (статическая проверка исходного кода service.py — без тяжёлой инициализации).
2. Translator использует персистентный кэш при наличии _translation_cache.
3. Translator корректно работает без _translation_cache (None — fallback).
4. clear_translation_cache IPC handler очищает кэш корректно.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translation_cache import TranslationCache
from backend.translator import Translator, TranslationResult


# ---------------------------------------------------------------------------
# 1. Статическая проверка wiring в service.py
# ---------------------------------------------------------------------------

class TestTranslationCacheInstantiatedInBackend(unittest.TestCase):
    """Проверяет что service.py содержит код wiring TranslationCache.

    Используем чтение исходного кода вместо полной инициализации BackendService
    чтобы не зависеть от LLM warmup / audio devices / LM Studio.
    """

    def _read_service_py(self) -> str:
        service_py = Path(__file__).resolve().parents[1] / "backend" / "service.py"
        return service_py.read_text(encoding="utf-8")

    def test_translation_cache_import_present(self):
        """service.py содержит 'from backend.translation_cache import TranslationCache'."""
        src = self._read_service_py()
        self.assertIn(
            "from backend.translation_cache import TranslationCache",
            src,
            "TranslationCache должен быть импортирован в service.py",
        )

    def test_translation_cache_instantiated_in_init(self):
        """service.py содержит 'TranslationCache(data_dir=' в BackendService.__init__."""
        src = self._read_service_py()
        self.assertIn(
            "TranslationCache(data_dir=",
            src,
            "TranslationCache должен быть инстанцирован в service.py",
        )

    def test_translation_cache_injected_into_translator(self):
        """service.py содержит инжекцию _translation_cache в translator."""
        src = self._read_service_py()
        self.assertIn(
            "self.translator._translation_cache = self._translation_cache",
            src,
            "translator._translation_cache должен быть инжектирован в service.py",
        )

    def test_clear_translation_cache_handler_registered(self):
        """service.py регистрирует 'clear_translation_cache' в dispatch-таблице."""
        src = self._read_service_py()
        self.assertIn(
            '"clear_translation_cache"',
            src,
            "'clear_translation_cache' должен быть зарегистрирован как IPC handler",
        )

    def test_handle_clear_translation_cache_method_defined(self):
        """service.py содержит '_handle_clear_translation_cache' метод."""
        src = self._read_service_py()
        self.assertIn(
            "def _handle_clear_translation_cache",
            src,
            "_handle_clear_translation_cache метод должен быть определён в service.py",
        )


# ---------------------------------------------------------------------------
# 2. Translator использует персистентный кэш
# ---------------------------------------------------------------------------

class TestTranslatorUsesPersistentCacheWhenPresent(unittest.TestCase):
    """Translator обращается к _translation_cache при наличии объекта."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def _make_translator_with_cache(self) -> tuple[Translator, TranslationCache]:
        t = Translator()
        cache = TranslationCache(data_dir=self._tmpdir)
        t._translation_cache = cache
        return t, cache

    def _patch_pipeline(self, translated_text: str):
        """Подменяет _build_pipeline чтобы вернуть fake pipeline."""
        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipe(text: str):
                return [{"translation_text": translated_text}]
            return fake_pipe

        return patch.object(Translator, "_build_pipeline", staticmethod(fake_builder))

    def test_successful_result_is_stored_in_persistent_cache(self):
        """Успешный перевод сохраняется в TranslationCache."""
        translator, cache = self._make_translator_with_cache()
        with self._patch_pipeline("TRANSLATED"):
            result = translator.translate(
                "Привет", mode="ru_to_es", network_mode="offline_default"
            )

        self.assertEqual(result.status, "ok")
        stats = cache.get_stats()
        self.assertEqual(stats["entries"], 1, "После перевода должна быть 1 запись в кэше")

    def test_persistent_cache_hit_returns_cached_translation(self):
        """Повторный запрос на новом Translator возвращает результат из персистентного кэша."""
        translator, cache = self._make_translator_with_cache()
        with self._patch_pipeline("ORIGINAL_TRANSLATION"):
            result1 = translator.translate(
                "Привет", mode="ru_to_es", network_mode="offline_default"
            )

        self.assertEqual(result1.status, "ok")
        self.assertEqual(result1.text, "ORIGINAL_TRANSLATION")

        # Создаём НОВЫЙ Translator с тем же персистентным кэшем (симуляция рестарта).
        # НЕ патчим pipeline — если кэш работает, pipeline не должен вызываться.
        translator2 = Translator()
        translator2._translation_cache = cache

        result2 = translator2.translate(
            "Привет", mode="ru_to_es", network_mode="offline_default"
        )

        self.assertEqual(result2.status, "ok")
        self.assertEqual(result2.text, "ORIGINAL_TRANSLATION")
        # engine должен содержать "_cached" суффикс
        self.assertIn("_cached", result2.engine)

    def test_persistent_cache_stats_include_hit(self):
        """После попадания в персистентный кэш hits увеличивается."""
        translator, cache = self._make_translator_with_cache()
        with self._patch_pipeline("TRANSLATION_A"):
            translator.translate("test text", mode="ru_to_es", network_mode="offline_default")

        # Создаём новый translator с тем же кэшем
        translator2 = Translator()
        translator2._translation_cache = cache

        # Запрос — должен быть cache hit
        translator2.translate("test text", mode="ru_to_es", network_mode="offline_default")

        stats = cache.get_stats()
        self.assertGreater(stats["hits"], 0, "После попадания hits должен быть > 0")

    def test_failed_translation_not_stored_in_persistent_cache(self):
        """Неуспешный перевод (model_unavailable) НЕ сохраняется в кэше."""
        translator, cache = self._make_translator_with_cache()
        # НЕ патчим pipeline → _build_pipeline вернёт None (модель недоступна)
        result = translator.translate(
            "Привет", mode="ru_to_es", network_mode="offline_default"
        )
        # Модель недоступна офлайн — result.ok должен быть False
        self.assertFalse(result.ok)
        stats = cache.get_stats()
        self.assertEqual(stats["entries"], 0, "Неуспешный перевод не должен сохраняться в кэш")

    def test_off_mode_not_stored_in_persistent_cache(self):
        """mode='off' не сохраняется в персистентном кэше."""
        translator, cache = self._make_translator_with_cache()
        result = translator.translate(
            "Привет", mode="off", network_mode="offline_default"
        )
        self.assertEqual(result.status, "not_requested")
        stats = cache.get_stats()
        self.assertEqual(stats["entries"], 0, "mode=off не должен сохраняться в персистентный кэш")


# ---------------------------------------------------------------------------
# 3. Translator корректно работает без _translation_cache (None)
# ---------------------------------------------------------------------------

class TestTranslatorFallsBackToMemoryCacheWhenCacheNone(unittest.TestCase):
    """Translator работает нормально когда _translation_cache = None."""

    def test_translate_off_mode_without_cache(self):
        """translate(mode='off') работает без _translation_cache."""
        translator = Translator()
        self.assertIsNone(translator._translation_cache)
        result = translator.translate("Привет", mode="off", network_mode="offline_default")
        self.assertEqual(result.status, "not_requested")
        self.assertEqual(result.text, "")

    def test_translate_with_pipeline_without_persistent_cache(self):
        """Успешный перевод работает без _translation_cache — использует только in-memory cache."""
        translator = Translator()
        self.assertIsNone(translator._translation_cache)

        def fake_builder(model_name: str, allow_network: bool):
            def fake_pipe(text: str):
                return [{"translation_text": f"TRANSLATED_{text}"}]
            return fake_pipe

        with patch.object(Translator, "_build_pipeline", staticmethod(fake_builder)):
            result = translator.translate(
                "Привет", mode="ru_to_es", network_mode="offline_default"
            )

        self.assertEqual(result.status, "ok")
        self.assertIn("TRANSLATED_", result.text)
        # Повторный вызов — из in-memory cache (pipeline недоступен → кэш спасает)
        with patch.object(Translator, "_build_pipeline", staticmethod(lambda *a: None)):
            result2 = translator.translate(
                "Привет", mode="ru_to_es", network_mode="offline_default"
            )
        self.assertEqual(result2.status, "ok")

    def test_empty_text_without_persistent_cache(self):
        """Пустой текст возвращает empty_text без _translation_cache."""
        translator = Translator()
        result = translator.translate("", mode="ru_to_es", network_mode="offline_default")
        self.assertEqual(result.status, "empty_text")


# ---------------------------------------------------------------------------
# 4. clear_translation_cache IPC handler (lightweight stub test)
# ---------------------------------------------------------------------------

class _StubBackendForClearHandler:
    """Минимальный stub для тестирования логики _handle_clear_translation_cache
    без полной инициализации BackendService."""

    def __init__(self, tmpdir: str) -> None:
        self._translation_cache: TranslationCache | None = TranslationCache(data_dir=tmpdir)

    def _handle_clear_translation_cache(self, params: dict) -> dict:
        """Точная копия логики реального handler из service.py."""
        entries_before = 0
        if self._translation_cache is not None:
            stats = self._translation_cache.get_stats()
            entries_before = stats.get("entries", 0)
            self._translation_cache.clear()
        return {"ok": True, "entries_cleared": entries_before}


class TestClearTranslationCacheHandler(unittest.TestCase):
    """_handle_clear_translation_cache очищает кэш и возвращает корректный ответ."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def test_clear_cache_returns_ok(self):
        """Handler возвращает ok=True и entries_cleared >= 0."""
        svc = _StubBackendForClearHandler(self._tmpdir)
        result = svc._handle_clear_translation_cache({})
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["entries_cleared"], 0)

    def test_clear_cache_empties_persistent_cache(self):
        """После clear — entries в TranslationCache равно 0."""
        svc = _StubBackendForClearHandler(self._tmpdir)
        svc._translation_cache.put("text", "src", "tgt", "eng", "result")
        self.assertEqual(svc._translation_cache.get_stats()["entries"], 1)

        svc._handle_clear_translation_cache({})
        self.assertEqual(svc._translation_cache.get_stats()["entries"], 0)

    def test_clear_cache_reports_entries_before_clear(self):
        """entries_cleared содержит число записей ДО очистки."""
        svc = _StubBackendForClearHandler(self._tmpdir)
        for i in range(3):
            svc._translation_cache.put(f"text{i}", "src", "tgt", "eng", f"result{i}")

        result = svc._handle_clear_translation_cache({})
        self.assertEqual(result["entries_cleared"], 3)

    def test_clear_cache_returns_zero_entries_when_cache_is_none(self):
        """Если _translation_cache=None — ok=True, entries_cleared=0."""
        svc = _StubBackendForClearHandler(self._tmpdir)
        svc._translation_cache = None
        result = svc._handle_clear_translation_cache({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries_cleared"], 0)


if __name__ == "__main__":
    unittest.main()
