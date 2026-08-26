"""Unit tests for _handle_list_llm_models URL construction (Wave 68 fix).

🔴 2026-08-26: файл закреплял СЛОМАННЫЙ endpoint. Замер на живом LM Studio
0.4.21 (105 моделей): /api/v0/models → 200/105, /v1/models → 200/105,
/api/v1/models → 200/0. Тесты перецелены на каталожный /api/v0/models.
Было: verifies that service._handle_list_llm_models uses /api/v1/models (LM Studio
correct endpoint) instead of /v1/models (which returns 200 but logs ERROR).
Sister fix to PR #396 (llm_rewriter.py passive_health_check).
"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestListLlmModelsEndpoint(unittest.TestCase):
    """_handle_list_llm_models обязан звать каталожный /api/v0/models."""

    def _make_service(self, llm_base_url="http://127.0.0.1:1234/v1"):
        """Build the LIVE extracted LLMOpsService with only what handle_list_llm_models needs.

        Repointed off the deleted dead-duplicate BackendService._handle_list_llm_models (#47);
        exposes a `_handle_list_llm_models` alias so existing test bodies stay unchanged.
        """
        from backend.llm_ops_service import LLMOpsService

        settings_svc = MagicMock()
        settings_svc.cached_settings.return_value = {
            "llm_base_url": llm_base_url,
            "llm_api_key": "",
        }
        svc = LLMOpsService(store=None, settings_svc=settings_svc, transcriber=None)
        # Alias to the live handler so test bodies can keep calling _handle_list_llm_models.
        svc._handle_list_llm_models = svc.handle_list_llm_models
        return svc

    def _mock_response(self, ids=None):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [{"id": mid} for mid in (ids or ["qwen3-4b-abliterated"])]
        }
        return mock_resp

    # ------------------------------------------------------------------
    # URL construction tests
    # ------------------------------------------------------------------

    def test_default_base_url_uses_catalog_endpoint(self):
        """Default http://127.0.0.1:1234/v1 → GET http://127.0.0.1:1234/api/v0/models."""
        svc = self._make_service("http://127.0.0.1:1234/v1")
        captured = []

        def fake_get(url, **kwargs):
            captured.append(url)
            return self._mock_response()

        with patch("requests.get", side_effect=fake_get):
            result = svc._handle_list_llm_models({})

        self.assertEqual(len(captured), 1, "requests.get должен вызваться ровно один раз")
        self.assertIn("/api/v0/models", captured[0])
        self.assertIsNone(result["error"])

    def test_trailing_slash_stripped(self):
        """Trailing slash on base_url stripped correctly."""
        svc = self._make_service("http://127.0.0.1:1234/v1/")
        captured = []

        def fake_get(url, **kwargs):
            captured.append(url)
            return self._mock_response()

        with patch("requests.get", side_effect=fake_get):
            svc._handle_list_llm_models({})

        self.assertTrue(
            captured[0].endswith("/api/v0/models"),
            f"URL должен заканчиваться на /api/v0/models, got: {captured[0]}",
        )

    def test_base_url_without_v1_suffix(self):
        """base_url without /v1 (e.g. http://host:1234) → /api/v0/models appended."""
        svc = self._make_service("http://127.0.0.1:1234")
        captured = []

        def fake_get(url, **kwargs):
            captured.append(url)
            return self._mock_response()

        with patch("requests.get", side_effect=fake_get):
            svc._handle_list_llm_models({})

        self.assertEqual(captured[0], "http://127.0.0.1:1234/api/v0/models")

    def test_v2_suffix_stripped(self):
        """Regex strips any /vN suffix, not just /v1."""
        svc = self._make_service("http://127.0.0.1:1234/v2")
        captured = []

        def fake_get(url, **kwargs):
            captured.append(url)
            return self._mock_response()

        with patch("requests.get", side_effect=fake_get):
            svc._handle_list_llm_models({})

        self.assertEqual(captured[0], "http://127.0.0.1:1234/api/v0/models")

    # ------------------------------------------------------------------
    # Response handling tests
    # ------------------------------------------------------------------

    def test_returns_sorted_model_ids(self):
        """Model IDs are returned sorted alphabetically."""
        svc = self._make_service()
        ids = ["zephyr-7b", "gemma-4-e4b-it-mlx", "qwen3-4b-abliterated"]
        with patch("requests.get", return_value=self._mock_response(ids)):
            result = svc._handle_list_llm_models({})

        self.assertEqual(result["models"], sorted(ids))
        self.assertIsNone(result["error"])

    def test_http_error_returns_empty_list(self):
        """Non-200 response returns empty models list with error code."""
        svc = self._make_service()
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        with patch("requests.get", return_value=mock_resp):
            result = svc._handle_list_llm_models({})

        self.assertEqual(result["models"], [])
        self.assertEqual(result["error"], "http_503")

    def test_connection_error_returns_error_string(self):
        """Connection refused returns empty models with error string (not exception)."""
        svc = self._make_service()
        with patch("requests.get", side_effect=ConnectionError("Connection refused")):
            result = svc._handle_list_llm_models({})

        self.assertEqual(result["models"], [])
        self.assertIsNotNone(result["error"])
        self.assertIn("Connection refused", result["error"])

    def test_recommended_models_always_present(self):
        """recommended_models обязан присутствовать И быть непустым при ошибке.

        🔴 До 2026-08-26 тест закреплял `== []`, противореча собственному
        докстрингу: при выключенном LM Studio GUI оставался вообще без вариантов
        выбора модели. Рекомендации статичны и от сети не зависят.
        """
        svc = self._make_service()
        with patch("requests.get", side_effect=ConnectionError("refused")):
            result = svc._handle_list_llm_models({})

        self.assertIn("recommended_models", result)
        self.assertTrue(result["recommended_models"])


if __name__ == "__main__":
    unittest.main()
