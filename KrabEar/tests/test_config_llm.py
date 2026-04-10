"""Тесты для LLM настроек в core/config.py."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch


class ConfigSecretsLoadingTestCase(unittest.TestCase):
    """Проверяет что .secrets файл правильно подхватывается pydantic-settings."""

    def test_secrets_file_path_is_absolute_and_correct(self):
        """config._SECRETS_FILE должен указывать на ~/Library/Application Support/KrabEar/.secrets."""
        from core.config import _SECRETS_FILE
        expected = Path.home() / "Library" / "Application Support" / "KrabEar" / ".secrets"
        self.assertEqual(_SECRETS_FILE, expected)

    def test_env_file_tuple_contains_secrets_and_dotenv(self):
        """model_config.env_file должен быть tuple (.env, .secrets) — .secrets последним,
        чтобы в pydantic-settings v2 иметь высший приоритет среди файлов."""
        from core.config import Settings, _SECRETS_FILE
        env_file = Settings.model_config.get("env_file")
        self.assertIsInstance(env_file, tuple)
        self.assertEqual(env_file[0], ".env")
        self.assertEqual(env_file[1], str(_SECRETS_FILE))


class ConfigLLMFieldsTestCase(unittest.TestCase):
    """Проверяет что новые LLM поля существуют с правильными дефолтами."""

    def test_llm_enabled_default_false(self):
        """LLM_ENABLED должен быть False по умолчанию."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KRAB_EAR_LLM_ENABLED", None)
            from core.config import Settings
            s = Settings(_env_file=())
            self.assertFalse(s.LLM_ENABLED)

    def test_llm_base_url_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KRAB_EAR_LLM_BASE_URL", None)
            from core.config import Settings
            s = Settings(_env_file=())
            self.assertEqual(s.LLM_BASE_URL, "http://localhost:1234/v1")

    def test_llm_model_default(self):
        # Убираем возможный override из .secrets / окружения
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KRAB_EAR_LLM_MODEL", None)
            from core.config import Settings
            s = Settings(_env_file=())
            self.assertEqual(s.LLM_MODEL, "huihui-qwen3-4b-instruct-2507-abliterated-hi-mlx")

    def test_llm_api_key_default(self):
        """LLM_API_KEY должен быть пустой строкой по умолчанию (security-sensitive)."""
        os.environ.pop("KRAB_EAR_LLM_API_KEY", None)
        from core.config import Settings
        s = Settings(_env_file=())
        self.assertEqual(s.LLM_API_KEY, "")

    def test_llm_timeout_sec_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KRAB_EAR_LLM_TIMEOUT_SEC", None)
            from core.config import Settings
            s = Settings(_env_file=())
            self.assertEqual(s.LLM_TIMEOUT_SEC, 5.0)

    def test_llm_circuit_fail_threshold_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KRAB_EAR_LLM_CIRCUIT_FAIL_THRESHOLD", None)
            from core.config import Settings
            s = Settings(_env_file=())
            self.assertEqual(s.LLM_CIRCUIT_FAIL_THRESHOLD, 3)

    def test_llm_circuit_initial_reset_sec_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KRAB_EAR_LLM_CIRCUIT_INITIAL_RESET_SEC", None)
            from core.config import Settings
            s = Settings(_env_file=())
            self.assertEqual(s.LLM_CIRCUIT_INITIAL_RESET_SEC, 60)

    def test_llm_circuit_max_reset_sec_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KRAB_EAR_LLM_CIRCUIT_MAX_RESET_SEC", None)
            from core.config import Settings
            s = Settings(_env_file=())
            self.assertEqual(s.LLM_CIRCUIT_MAX_RESET_SEC, 600)

    def test_env_var_override(self):
        """KRAB_EAR_LLM_ENABLED=true переопределяет дефолт."""
        with patch.dict(os.environ, {"KRAB_EAR_LLM_ENABLED": "true"}):
            from core.config import Settings
            s = Settings()
            self.assertTrue(s.LLM_ENABLED)


class DefaultSettingsLLMToggleTestCase(unittest.TestCase):
    """Проверяет что llm_rewrite_enabled добавлен в DEFAULT_SETTINGS."""

    def test_llm_rewrite_enabled_in_default_settings(self):
        from core.config import DEFAULT_SETTINGS
        self.assertIn("llm_rewrite_enabled", DEFAULT_SETTINGS)

    def test_llm_rewrite_enabled_default_false(self):
        from core.config import DEFAULT_SETTINGS
        self.assertFalse(DEFAULT_SETTINGS["llm_rewrite_enabled"])


if __name__ == "__main__":
    unittest.main()
