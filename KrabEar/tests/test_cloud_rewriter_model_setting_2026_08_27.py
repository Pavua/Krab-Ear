"""Модель облачного рерайтера — настройка, а не константа в коде.

Аудит селекторов (2026-08-27): `gpt-4o-mini` и `claude-haiku-4-5-20251001`
зашиты классовыми константами `_MODEL` в `backend/cloud_rewriter.py`. Владелец
не может ни выбрать другую модель, ни узнать из UI, какая используется, — а
модели облачных провайдеров меняются чаще, чем выходят наши релизы.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class CloudRewriterModelSettingTest(unittest.TestCase):
    def test_defaults_exist_and_match_previous_hardcode(self):
        """Дефолты обязаны совпадать с прежним поведением — тихой смены модели быть не должно."""
        from core.config import DEFAULT_SETTINGS

        self.assertEqual(DEFAULT_SETTINGS["cloud_rewriter_openai_model"], "gpt-4o-mini")
        self.assertEqual(
            DEFAULT_SETTINGS["cloud_rewriter_anthropic_model"], "claude-haiku-4-5-20251001"
        )

    def test_openai_provider_reads_model_from_settings(self):
        from backend.cloud_rewriter import OpenAIRewriterProvider

        settings = {"openai_api_key": "k", "cloud_rewriter_openai_model": "gpt-4.1-mini"}
        with patch("backend.cloud_rewriter._load_settings", return_value=settings):
            self.assertEqual(OpenAIRewriterProvider()._model_name(), "gpt-4.1-mini")

    def test_anthropic_provider_reads_model_from_settings(self):
        from backend.cloud_rewriter import AnthropicRewriterProvider

        settings = {"anthropic_api_key": "k", "cloud_rewriter_anthropic_model": "claude-sonnet-5"}
        with patch("backend.cloud_rewriter._load_settings", return_value=settings):
            self.assertEqual(AnthropicRewriterProvider()._model_name(), "claude-sonnet-5")

    def test_blank_setting_falls_back_to_default(self):
        """Пустая строка в настройках не должна отправлять запрос без модели."""
        from backend.cloud_rewriter import OpenAIRewriterProvider

        with patch("backend.cloud_rewriter._load_settings",
                   return_value={"cloud_rewriter_openai_model": "   "}):
            self.assertEqual(OpenAIRewriterProvider()._model_name(), "gpt-4o-mini")

    def test_model_actually_used_in_request_payload(self):
        """🔴 Настройка обязана доходить до ЗАПРОСА, а не оставаться декоративной.

        Бьём в РЕАЛЬНЫЙ транспорт: провайдер ходит через `urllib.request`,
        а не через `requests` — мок мимо транспорта проверял бы фантазию.
        """
        import json as _json

        from backend.cloud_rewriter import OpenAIRewriterProvider

        captured: dict = {}

        class _FakeResp:
            def read(self):
                return _json.dumps(
                    {"choices": [{"message": {"content": "готово"}}]}
                ).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def _fake_urlopen(req, timeout=None):
            captured.update(_json.loads(req.data.decode("utf-8")))
            return _FakeResp()

        settings = {"openai_api_key": "k", "cloud_rewriter_openai_model": "gpt-4.1-mini"}
        with patch("backend.cloud_rewriter._load_settings", return_value=settings), \
             patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            OpenAIRewriterProvider().rewrite("привет", "ru")
        self.assertEqual(captured.get("model"), "gpt-4.1-mini")


if __name__ == "__main__":
    unittest.main()
