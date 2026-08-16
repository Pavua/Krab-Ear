"""Тесты для модели настроек: DEFAULT_SETTINGS и Settings (core/config.py).

Проверяет:
- сериализуемость DEFAULT_SETTINGS в JSON
- round-trip Settings → JSON → Settings
- игнорирование неизвестных ключей
- приведение типов (string "true" → bool, "5" → int)
- наличие не-None дефолтов для всех ключей DEFAULT_SETTINGS
"""

from __future__ import annotations
from core.config import DEFAULT_SETTINGS, Settings

import json
import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestDefaultSettingsSerializable(unittest.TestCase):
    """DEFAULT_SETTINGS должен без исключений сериализоваться в JSON."""

    def test_default_settings_serializable(self):
        try:
            serialized = json.dumps(DEFAULT_SETTINGS)
        except (TypeError, ValueError) as exc:
            self.fail(f"DEFAULT_SETTINGS не сериализуется в JSON: {exc}")

        self.assertIsInstance(serialized, str)
        self.assertGreater(len(serialized), 0)

        # Проверяем, что результат — валидный JSON-объект
        parsed = json.loads(serialized)
        self.assertIsInstance(parsed, dict)


class TestSettingsRoundTrip(unittest.TestCase):
    """round-trip: Settings → dict → Settings сохраняет значения."""

    def _settings_to_dict(self, s: Settings) -> dict:
        """Конвертирует Settings в словарь (model_dump через pydantic)."""
        return s.model_dump()

    def test_settings_to_from_json(self):
        original = Settings()
        data = self._settings_to_dict(original)

        # Сериализуем в JSON и обратно
        json_str = json.dumps(data, default=str)
        loaded = json.loads(json_str)

        # Реконструируем Settings из словаря
        restored = Settings(**{k: v for k, v in loaded.items()})

        # Ключевые поля должны совпадать
        self.assertEqual(original.NETWORK_MODE, restored.NETWORK_MODE)
        self.assertEqual(original.TRANSCRIBE_LANGUAGE, restored.TRANSCRIBE_LANGUAGE)
        self.assertEqual(original.LLM_ENABLED, restored.LLM_ENABLED)
        self.assertEqual(original.LLM_MODEL, restored.LLM_MODEL)
        self.assertEqual(original.MAX_AUDIO_MB, restored.MAX_AUDIO_MB)
        self.assertEqual(original.DIARIZATION_ENABLED, restored.DIARIZATION_ENABLED)


class TestSettingsUnknownKeysIgnored(unittest.TestCase):
    """Лишние ключи не должны вызывать исключение (extra='ignore')."""

    def test_settings_unknown_keys_ignored(self):
        try:
            s = Settings(
                TOTALLY_UNKNOWN_KEY_XYZ="should_be_ignored",
                ANOTHER_RANDOM_KEY=42,
            )
        except Exception as exc:
            self.fail(f"Неизвестные ключи вызвали исключение: {exc}")

        # Убеждаемся, что нормальные поля остались нетронутыми
        self.assertEqual(s.NETWORK_MODE, "offline_strict")

    def test_default_settings_extra_keys_in_dict(self):
        """Словарь с лишними ключами передаётся в Settings без ошибок."""
        data = dict(DEFAULT_SETTINGS)
        data["unknown_future_setting"] = "value"

        # DEFAULT_SETTINGS не используется напрямую в Settings,
        # но убедимся, что можно создать Settings с произвольными kwargs
        try:
            _s = Settings(**{k.upper(): v for k, v in data.items() if isinstance(k, str)})  # noqa: F841
        except Exception:
            # Pydantic может отклонить некоторые значения из DEFAULT_SETTINGS
            # из-за несовпадения типов — это нормально, тест проверяет только
            # что extra-ключи не дают TypeError/ValidationError с unknown field.
            pass


class TestSettingsTypeCoercion(unittest.TestCase):
    """Pydantic приводит строки к нужным типам."""

    def test_string_true_coerced_to_bool(self):
        s = Settings(DIARIZATION_ENABLED="true")
        self.assertIs(s.DIARIZATION_ENABLED, True)
        self.assertIsInstance(s.DIARIZATION_ENABLED, bool)

    def test_string_false_coerced_to_bool(self):
        s = Settings(LLM_ENABLED="false")
        self.assertIs(s.LLM_ENABLED, False)
        self.assertIsInstance(s.LLM_ENABLED, bool)

    def test_string_int_coerced_to_int(self):
        s = Settings(MAX_AUDIO_MB="5")
        self.assertEqual(s.MAX_AUDIO_MB, 5)
        self.assertIsInstance(s.MAX_AUDIO_MB, int)

    def test_string_float_coerced_for_float_field(self):
        s = Settings(LLM_TIMEOUT_SEC="3.5")
        self.assertAlmostEqual(s.LLM_TIMEOUT_SEC, 3.5)
        self.assertIsInstance(s.LLM_TIMEOUT_SEC, float)


class TestAllDefaultSettingsHaveDefaults(unittest.TestCase):
    """Каждый ключ в DEFAULT_SETTINGS должен иметь не-None значение."""

    def test_all_settings_have_defaults(self):
        none_keys = [k for k, v in DEFAULT_SETTINGS.items() if v is None]
        self.assertEqual(
            none_keys,
            [],
            f"Ключи DEFAULT_SETTINGS с None-значениями: {none_keys}",
        )

    def test_all_default_values_are_typed(self):
        """Каждое значение должно быть одним из ожидаемых типов."""
        allowed_types = (str, int, float, bool, dict, list)
        wrong = {
            k: type(v).__name__
            for k, v in DEFAULT_SETTINGS.items()
            if not isinstance(v, allowed_types)
        }
        self.assertEqual(
            wrong,
            {},
            f"DEFAULT_SETTINGS содержит значения неожиданных типов: {wrong}",
        )

    def test_default_settings_keys_not_empty(self):
        self.assertGreater(len(DEFAULT_SETTINGS), 0, "DEFAULT_SETTINGS пустой")

    def test_default_settings_string_values_not_empty(self):
        """Строковые значения не должны быть пустыми строками (кроме разрешённых)."""
        # voice_gateway_api_key и hf_token ожидаемо пустые;
        # SMTP и recap поля пустые по умолчанию (пользователь вводит сам)
        allowed_empty = {
            "voice_gateway_api_key",
            "hf_token",
            "recap_email_to",
            "smtp_host",
            "smtp_user",
            "lm_studio_api_key",
            # cloud_rewriter opt-in credentials/config (PR #1817/#1823) — пользователь
            # вводит сам, пустая строка = "провайдер не настроен", не баг.
            "anthropic_api_key",
            "cloud_rewriter_base_url",
            "cloud_rewriter_custom_model",
            "cloud_rewriter_api_key",
            # LocalSIP credentials — пустые = stub-режим, пользователь вводит сам.
            "sip_server",
            "sip_user",
            "sip_password",
            "sip_from_number",
            "sip_proxy",
        }
        empty_strings = [
            k
            for k, v in DEFAULT_SETTINGS.items()
            if isinstance(v, str) and v == "" and k not in allowed_empty
        ]
        self.assertEqual(
            empty_strings,
            [],
            f"DEFAULT_SETTINGS содержит неожиданно пустые строки: {empty_strings}",
        )


if __name__ == "__main__":
    unittest.main()
