"""Security tests for Wave 1741 — LLM base_url SSRF fix.

Verifies:
1. _validate_llm_url rejects disallowed schemes (file, gopher, ftp, data).
2. _validate_llm_url allows http and https (including localhost / LAN).
3. passive_health_check in LLMRewriter: disallowed scheme → (False, False), no GET fired.
4. passive_health_check: allow_redirects=False enforced on legitimate GET.
5. LLMOpsService.handle_list_llm_models: disallowed scheme → no GET, returns error.
6. LLMOpsService.handle_list_llm_models: allow_redirects=False enforced.
7. BackendService._handle_list_llm_models: disallowed scheme → no GET, returns error.
8. BackendService._handle_list_llm_models: allow_redirects=False enforced.
9. localhost http URL still works end-to-end (no regression).
"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Helper: _validate_llm_url unit tests
# ---------------------------------------------------------------------------

class TestValidateLlmUrl(unittest.TestCase):
    """Direct unit tests for the shared _validate_llm_url helper."""

    def _validate(self, url):
        from backend.llm_rewriter import _validate_llm_url
        return _validate_llm_url(url)

    # -- disallowed schemes must raise ValueError --

    def test_file_scheme_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._validate("file:///etc/passwd")
        self.assertIn("disallowed scheme", str(ctx.exception))

    def test_file_scheme_uppercase_raises(self):
        with self.assertRaises(ValueError):
            self._validate("FILE:///etc/passwd")

    def test_gopher_scheme_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._validate("gopher://127.0.0.1:70/1/secret")
        self.assertIn("disallowed scheme", str(ctx.exception))

    def test_ftp_scheme_raises(self):
        with self.assertRaises(ValueError):
            self._validate("ftp://evil.example.com/payload")

    def test_data_scheme_raises(self):
        with self.assertRaises(ValueError):
            self._validate("data:text/html,<script>alert(1)</script>")

    def test_dict_scheme_raises(self):
        with self.assertRaises(ValueError):
            self._validate("dict://127.0.0.1:11211/stat")

    # -- allowed schemes must return the url unchanged --

    def test_http_localhost_allowed(self):
        url = "http://127.0.0.1:1234/api/v1/models"
        result = self._validate(url)
        self.assertEqual(result, url)

    def test_http_lan_allowed(self):
        url = "http://192.168.1.50:1234/api/v1/models"
        result = self._validate(url)
        self.assertEqual(result, url)

    def test_https_remote_allowed(self):
        url = "https://api.example.com/api/v1/models"
        result = self._validate(url)
        self.assertEqual(result, url)

    def test_http_localhost_hostname_allowed(self):
        url = "http://localhost:1234/api/v1/models"
        result = self._validate(url)
        self.assertEqual(result, url)


# ---------------------------------------------------------------------------
# Site 1: LLMRewriter.passive_health_check
# ---------------------------------------------------------------------------

class TestPassiveHealthCheckSSRF(unittest.TestCase):
    """LLMRewriter.passive_health_check must not fire GET for bad schemes."""

    def _make_rewriter(self, base_url: str):
        from backend.llm_rewriter import LLMRewriter
        rw = LLMRewriter.__new__(LLMRewriter)
        rw._base_url = base_url
        rw._model = "test-model"
        rw._api_key = ""
        # _session mock — records calls
        rw._session = MagicMock()
        return rw

    def test_file_scheme_no_get_returns_false_false(self):
        rw = self._make_rewriter("file:///etc/passwd")
        result = rw.passive_health_check()
        self.assertEqual(result, (False, False))
        rw._session.get.assert_not_called()

    def test_gopher_scheme_no_get_returns_false_false(self):
        rw = self._make_rewriter("gopher://127.0.0.1:70/1")
        result = rw.passive_health_check()
        self.assertEqual(result, (False, False))
        rw._session.get.assert_not_called()

    def test_allow_redirects_false_on_legitimate_get(self):
        rw = self._make_rewriter("http://127.0.0.1:1234/v1")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "test-model"}]}
        rw._session.get.return_value = mock_resp
        rw._lm_studio_get_headers = MagicMock(return_value={})

        result = rw.passive_health_check()

        self.assertEqual(result, (True, True))
        _, kwargs = rw._session.get.call_args
        self.assertFalse(
            kwargs.get("allow_redirects", True),
            "allow_redirects must be False to prevent redirect-based SSRF",
        )

    def test_localhost_http_still_works(self):
        rw = self._make_rewriter("http://127.0.0.1:1234/v1")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "some-model"}]}
        rw._session.get.return_value = mock_resp
        rw._lm_studio_get_headers = MagicMock(return_value={})

        reachable, has_model = rw.passive_health_check()
        self.assertTrue(reachable)


# ---------------------------------------------------------------------------
# Site 2: LLMOpsService.handle_list_llm_models
# ---------------------------------------------------------------------------

class TestLLMOpsServiceSSRF(unittest.TestCase):
    """LLMOpsService.handle_list_llm_models must not GET for bad schemes."""

    def _make_svc(self, llm_base_url: str):
        from backend.llm_ops_service import LLMOpsService
        svc = LLMOpsService.__new__(LLMOpsService)
        settings_svc = MagicMock()
        settings_svc.cached_settings.return_value = {
            "llm_base_url": llm_base_url,
            "llm_api_key": "",
        }
        svc._settings_svc = settings_svc
        svc._store = MagicMock()
        svc._transcriber = MagicMock()
        return svc

    def test_file_scheme_no_get_returns_error(self):
        svc = self._make_svc("file:///etc/passwd")
        with patch("requests.get") as mock_get:
            result = svc.handle_list_llm_models({})
        mock_get.assert_not_called()
        self.assertEqual(result["models"], [])
        self.assertIsNotNone(result.get("error"))

    def test_gopher_scheme_no_get_returns_error(self):
        svc = self._make_svc("gopher://internal.host:70/1")
        with patch("requests.get") as mock_get:
            result = svc.handle_list_llm_models({})
        mock_get.assert_not_called()
        self.assertEqual(result["models"], [])

    def test_allow_redirects_false_on_legitimate_get(self):
        svc = self._make_svc("http://127.0.0.1:1234/v1")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "qwen3-4b"}]}

        with patch("requests.get", return_value=mock_resp) as mock_get:
            result = svc.handle_list_llm_models({})

        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        self.assertFalse(
            kwargs.get("allow_redirects", True),
            "allow_redirects must be False",
        )
        self.assertIn("qwen3-4b", result["models"])

    def test_localhost_http_still_works(self):
        svc = self._make_svc("http://127.0.0.1:1234/v1")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "my-model"}]}

        with patch("requests.get", return_value=mock_resp):
            result = svc.handle_list_llm_models({})

        self.assertIsNone(result["error"])
        self.assertIn("my-model", result["models"])


# ---------------------------------------------------------------------------
# Site 3: BackendService._handle_list_llm_models
# ---------------------------------------------------------------------------

class TestBackendServiceListLlmModelsSSRF(unittest.TestCase):
    """BackendService._handle_list_llm_models must not GET for bad schemes."""

    def _make_service(self, llm_base_url: str):
        from backend.service import BackendService
        svc = BackendService.__new__(BackendService)
        settings_svc = MagicMock()
        settings_svc.cached_settings.return_value = {
            "llm_base_url": llm_base_url,
            "llm_api_key": "",
        }
        svc._settings_svc = settings_svc
        return svc

    def test_file_scheme_no_get_returns_error(self):
        svc = self._make_service("file:///etc/passwd")
        with patch("requests.get") as mock_get:
            result = svc._handle_list_llm_models({})
        mock_get.assert_not_called()
        self.assertEqual(result["models"], [])
        self.assertIsNotNone(result.get("error"))

    def test_gopher_scheme_no_get_returns_error(self):
        svc = self._make_service("gopher://127.0.0.1:70/1")
        with patch("requests.get") as mock_get:
            result = svc._handle_list_llm_models({})
        mock_get.assert_not_called()
        self.assertEqual(result["models"], [])

    def test_allow_redirects_false_on_legitimate_get(self):
        svc = self._make_service("http://127.0.0.1:1234/v1")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "test-model"}]}

        with patch("requests.get", return_value=mock_resp) as mock_get:
            svc._handle_list_llm_models({})

        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        self.assertFalse(
            kwargs.get("allow_redirects", True),
            "allow_redirects must be False to prevent redirect-based SSRF",
        )

    def test_localhost_http_still_works(self):
        svc = self._make_service("http://localhost:1234/v1")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "my-model"}]}

        with patch("requests.get", return_value=mock_resp):
            result = svc._handle_list_llm_models({})

        self.assertIsNone(result["error"])
        self.assertIn("my-model", result["models"])

    def test_https_remote_still_works(self):
        svc = self._make_service("https://api.myhost.com/v1")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "cloud-model"}]}

        with patch("requests.get", return_value=mock_resp):
            result = svc._handle_list_llm_models({})

        self.assertIsNone(result["error"])


if __name__ == "__main__":
    unittest.main()
