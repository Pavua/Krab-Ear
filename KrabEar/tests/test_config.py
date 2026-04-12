"""Тесты для core/config.py — DEFAULT_SETTINGS, singleton settings, env overrides."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))


class DefaultSettingsExistTestCase(unittest.TestCase):
    """DEFAULT_SETTINGS содержит ожидаемые ключи."""

    def test_default_settings_exist(self):
        from core.config import DEFAULT_SETTINGS
        expected_keys = [
            "mode",
            "auto_paste",
            "quality_profile",
            "network_mode",
            "hotkey",
            "history_policy",
            "history_page_size",
            "cleanup_profile",
            "translation_mode",
            "clipboard_mode",
            "silence_guard_enabled",
            "background_guard_enabled",
            "llm_rewrite_enabled",
        ]
        for key in expected_keys:
            self.assertIn(key, DEFAULT_SETTINGS, msg=f"Ключ '{key}' отсутствует в DEFAULT_SETTINGS")


class SettingsSingletonTestCase(unittest.TestCase):
    """settings — importable singleton с корректными атрибутами."""

    def test_settings_singleton(self):
        from core.config import settings
        self.assertTrue(hasattr(settings, "DATA_DIR"))
        self.assertTrue(hasattr(settings, "MODEL_BALANCED"))
        self.assertTrue(hasattr(settings, "TRANSCRIBE_LANGUAGE"))
        self.assertTrue(hasattr(settings, "NETWORK_MODE"))
        self.assertTrue(hasattr(settings, "LLM_ENABLED"))
        self.assertTrue(hasattr(settings, "LOG_FORMAT"))
        # Тип
        self.assertIsInstance(settings.DATA_DIR, Path)
        self.assertIsInstance(settings.TRANSCRIBE_LANGUAGE, str)
        self.assertIsInstance(settings.LLM_ENABLED, bool)


class EnvOverrideTestCase(unittest.TestCase):
    """KRAB_EAR_* переменные окружения переопределяют настройки."""

    def test_env_override(self):
        env_patch = {
            "KRAB_EAR_TRANSCRIBE_LANGUAGE": "es",
            "KRAB_EAR_NETWORK_MODE": "online",
            "KRAB_EAR_MAX_AUDIO_MB": "99",
        }
        with patch.dict(os.environ, env_patch):
            from core.config import Settings
            s = Settings(_env_file=())
            self.assertEqual(s.TRANSCRIBE_LANGUAGE, "es")
            self.assertEqual(s.NETWORK_MODE, "online")
            self.assertEqual(s.MAX_AUDIO_MB, 99)


class LogFormatValuesTestCase(unittest.TestCase):
    """LOG_FORMAT принимает значения 'text' и 'json'."""

    def test_log_format_values(self):
        for value in ("text", "json"):
            with patch.dict(os.environ, {"KRAB_EAR_LOG_FORMAT": value}):
                from core.config import Settings
                s = Settings(_env_file=())
                self.assertEqual(s.LOG_FORMAT, value)

    def test_log_format_default_is_text(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KRAB_EAR_LOG_FORMAT", None)
            from core.config import Settings
            s = Settings(_env_file=())
            self.assertEqual(s.LOG_FORMAT, "text")


class LLMTimeoutRangeTestCase(unittest.TestCase):
    """LLM_TIMEOUT_SEC — положительное число с плавающей точкой."""

    def test_llm_timeout_range(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KRAB_EAR_LLM_TIMEOUT_SEC", None)
            from core.config import Settings
            s = Settings(_env_file=())
            self.assertIsInstance(s.LLM_TIMEOUT_SEC, float)
            self.assertGreater(s.LLM_TIMEOUT_SEC, 0.0)

    def test_llm_timeout_env_override(self):
        with patch.dict(os.environ, {"KRAB_EAR_LLM_TIMEOUT_SEC": "15.5"}):
            from core.config import Settings
            s = Settings(_env_file=())
            self.assertEqual(s.LLM_TIMEOUT_SEC, 15.5)
            self.assertGreater(s.LLM_TIMEOUT_SEC, 0.0)


if __name__ == "__main__":
    unittest.main()
