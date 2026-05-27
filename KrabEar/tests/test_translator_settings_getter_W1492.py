"""W1492 F3 HIGH — _settings_getter injection tests (WAVE 1500 MILESTONE).

Three test groups:
1. test_settings_getter_injected_by_backend_service  — service.py AST / source check.
2. test_translator_detects_privacy_mode_change_via_getter  — functional behaviour.
3. test_translator_safe_when_getter_none  — no-op when getter not injected.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translator import Translator, TranslationResult


# ---------------------------------------------------------------------------
# 1. Static wiring check — service.py must inject _settings_getter
# ---------------------------------------------------------------------------

class TestSettingsGetterInjectedByBackendService(unittest.TestCase):
    """Проверяет что service.py содержит строку инжекции _settings_getter."""

    def _read_service_py(self) -> str:
        path = Path(__file__).resolve().parents[1] / "backend" / "service.py"
        return path.read_text(encoding="utf-8")

    def test_injection_line_present(self):
        """service.py должен присваивать _settings_getter после создания translator."""
        src = self._read_service_py()
        self.assertIn(
            "self.translator._settings_getter = self._get_runtime_setting",
            src,
            "service.py должен инжектировать _settings_getter в self.translator",
        )

    def test_injection_after_translator_construction(self):
        """Строка инжекции должна идти после 'self.translator = translator or Translator()'."""
        src = self._read_service_py()
        construction_idx = src.find("self.translator = translator or Translator()")
        injection_idx = src.find(
            "self.translator._settings_getter = self._get_runtime_setting"
        )
        self.assertGreater(
            construction_idx, -1,
            "Строка создания translator не найдена в service.py",
        )
        self.assertGreater(
            injection_idx, -1,
            "Строка инжекции _settings_getter не найдена в service.py",
        )
        self.assertGreater(
            injection_idx,
            construction_idx,
            "_settings_getter должен инжектироваться ПОСЛЕ создания translator",
        )

    def test_slot_declared_in_translator_init(self):
        """Translator.__init__ должен объявлять _settings_getter как атрибут (не только через getattr)."""
        src = (Path(__file__).resolve().parents[1] / "backend" / "translator.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "self._settings_getter",
            src,
            "Translator.__init__ должен объявлять self._settings_getter",
        )


# ---------------------------------------------------------------------------
# 2. Functional — translator detects privacy mode change via injected getter
# ---------------------------------------------------------------------------

class TestTranslatorDetectsPrivacyModeChangeViaGetter(unittest.TestCase):
    """Проверяет что _check_privacy_mode_changed() работает через injected getter."""

    def _make_translator_with_getter(self, privacy_value: bool) -> tuple[Translator, list]:
        """Создаёт Translator с injected getter, возвращает (translator, calls_log)."""
        translator = Translator()
        calls: list[tuple[str, object]] = []

        def fake_getter(key: str, default: object = None) -> object:
            calls.append((key, default))
            if key == "privacy_mode_enabled":
                return privacy_value
            return default

        translator._settings_getter = fake_getter
        return translator, calls

    def test_getter_called_on_check(self):
        """_check_privacy_mode_changed() должен вызывать getter с ключом 'privacy_mode_enabled'."""
        translator, calls = self._make_translator_with_getter(False)
        # Инициализируем _last_privacy_mode через первый вызов
        translator._check_privacy_mode_changed()
        self.assertTrue(
            any(k == "privacy_mode_enabled" for k, _ in calls),
            "getter должен быть вызван с 'privacy_mode_enabled'",
        )

    def test_cache_cleared_on_false_to_true_transition(self):
        """При переходе privacy_mode False→True кэш должен сброситься."""
        translator = Translator()
        # Заполняем кэш
        from collections import OrderedDict
        from backend.translator import TranslationResult
        translator._cache[("hello", "ru_to_es", "ru", "es")] = TranslationResult(
            text="hola", status="ok", source_lang="ru", target_lang="es",
            mode="ru_to_es", engine="test"
        )
        self.assertEqual(len(translator._cache), 1)

        privacy_flag = [False]

        def dynamic_getter(key: str, default: object = None) -> object:
            if key == "privacy_mode_enabled":
                return privacy_flag[0]
            return default

        translator._settings_getter = dynamic_getter

        # Первый вызов — инициализация (кэш не трогаем)
        translator._check_privacy_mode_changed()
        self.assertEqual(len(translator._cache), 1, "Первый вызов не должен сбрасывать кэш")

        # Меняем privacy_mode на True — должен сброситься кэш
        privacy_flag[0] = True
        translator._check_privacy_mode_changed()
        self.assertEqual(
            len(translator._cache), 0,
            "Переход False→True должен сбросить кэш через getter",
        )

    def test_no_cache_clear_on_true_to_false_transition(self):
        """При переходе privacy_mode True→False кэш НЕ должен сбрасываться."""
        translator = Translator()
        privacy_flag = [True]

        def dynamic_getter(key: str, default: object = None) -> object:
            if key == "privacy_mode_enabled":
                return privacy_flag[0]
            return default

        translator._settings_getter = dynamic_getter

        # Первый вызов — инициализация (True)
        translator._check_privacy_mode_changed()

        # Заполняем кэш после инициализации
        translator._cache[("hi", "ru_to_es", "ru", "es")] = TranslationResult(
            text="hola", status="ok", source_lang="ru", target_lang="es",
            mode="ru_to_es", engine="test"
        )

        # Переходим к False — кэш должен остаться
        privacy_flag[0] = False
        translator._check_privacy_mode_changed()
        self.assertEqual(
            len(translator._cache), 1,
            "Переход True→False не должен сбрасывать кэш",
        )


# ---------------------------------------------------------------------------
# 3. Safety — no exception when getter is None
# ---------------------------------------------------------------------------

class TestTranslatorSafeWhenGetterNone(unittest.TestCase):
    """Проверяет что Translator корректно работает без injected getter."""

    def test_slot_is_none_by_default(self):
        """Свежий Translator должен иметь _settings_getter = None."""
        translator = Translator()
        self.assertIsNone(
            translator._settings_getter,
            "_settings_getter должен быть None по умолчанию",
        )

    def test_check_privacy_mode_noop_when_getter_none(self):
        """_check_privacy_mode_changed() должен быть no-op когда getter == None."""
        translator = Translator()
        # Должен выполниться без исключений
        translator._check_privacy_mode_changed()
        translator._check_privacy_mode_changed()

    def test_translate_off_mode_noop_when_getter_none(self):
        """translate() в режиме 'off' не должен падать без injected getter."""
        translator = Translator()
        result = translator.translate("Привет", mode="off", network_mode="offline_default")
        self.assertEqual(result.status, "not_requested")

    def test_cache_unaffected_when_getter_none(self):
        """Кэш не должен сбрасываться при вызове _check_privacy_mode_changed() без getter."""
        translator = Translator()
        translator._cache[("test", "ru_to_es", "ru", "es")] = TranslationResult(
            text="prueba", status="ok", source_lang="ru", target_lang="es",
            mode="ru_to_es", engine="test"
        )
        self.assertEqual(len(translator._cache), 1)

        translator._check_privacy_mode_changed()
        # Кэш должен остаться нетронутым
        self.assertEqual(
            len(translator._cache), 1,
            "Кэш не должен изменяться без injected getter",
        )


if __name__ == "__main__":
    unittest.main()
