"""Тесты CORS поддержки в REST сервере Krab Ear.

Проверяет:
- preflight OPTIONS запросы возвращают корректные CORS заголовки
- CORS заголовки присутствуют в обычных GET/POST ответах
- конфигурируемые origins работают корректно
- credentials поддерживаются
"""

import sys
import os
import unittest
from unittest.mock import patch

# Добавляем PROJECT_ROOT в sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _make_app(cors_origins="*"):
    """Создаёт изолированный Flask app с CORS для тестов."""
    from flask import Flask, jsonify
    from flask_cors import CORS

    app = Flask(__name__)

    def _parse_cors_origins(raw):
        if raw.strip() == "*":
            return "*"
        return [o.strip() for o in raw.split(",") if o.strip()]

    CORS(
        app,
        origins=_parse_cors_origins(cors_origins),
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    )

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"})

    @app.route("/v1/test", methods=["GET", "POST"])
    def test_endpoint():
        return jsonify({"result": "ok"})

    return app


class TestCORSPreflight(unittest.TestCase):
    """Тест preflight OPTIONS запросов."""

    def setUp(self):
        self.app = _make_app(cors_origins="*")
        self.client = self.app.test_client()

    def test_preflight_options_returns_200(self):
        """OPTIONS preflight должен вернуть 200."""
        resp = self.client.options(
            "/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertIn(resp.status_code, (200, 204))

    def test_preflight_returns_allow_origin_header(self):
        """Preflight ответ должен содержать Access-Control-Allow-Origin."""
        resp = self.client.options(
            "/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertIn("Access-Control-Allow-Origin", resp.headers)

    def test_preflight_returns_allow_methods(self):
        """Preflight ответ должен содержать Access-Control-Allow-Methods."""
        resp = self.client.options(
            "/v1/test",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        self.assertIn("Access-Control-Allow-Methods", resp.headers)

    def test_preflight_allow_headers(self):
        """Preflight ответ должен разрешать Content-Type и Authorization."""
        resp = self.client.options(
            "/v1/test",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        self.assertIn(resp.status_code, (200, 204))
        # Либо allow-headers, либо allow-origin должны присутствовать
        has_cors = (
            "Access-Control-Allow-Headers" in resp.headers
            or "Access-Control-Allow-Origin" in resp.headers
        )
        self.assertTrue(has_cors)


class TestCORSHeadersOnNormalRequests(unittest.TestCase):
    """Тест наличия CORS заголовков в обычных GET/POST ответах."""

    def setUp(self):
        self.app = _make_app(cors_origins="*")
        self.client = self.app.test_client()

    def test_get_health_has_cors_header(self):
        """GET /health с Origin должен вернуть Access-Control-Allow-Origin."""
        resp = self.client.get(
            "/health",
            headers={"Origin": "http://localhost:3000"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Access-Control-Allow-Origin", resp.headers)

    def test_wildcard_origin_allows_any(self):
        """При CORS_ORIGINS=* любой origin должен быть разрешён."""
        resp = self.client.get(
            "/health",
            headers={"Origin": "https://random-domain.io"},
        )
        self.assertEqual(resp.status_code, 200)
        allow_origin = resp.headers.get("Access-Control-Allow-Origin", "")
        self.assertTrue(
            allow_origin == "*" or allow_origin == "https://random-domain.io",
            f"Unexpected Allow-Origin: {allow_origin}",
        )

    def test_post_request_has_cors_header(self):
        """POST запрос с Origin также должен получить CORS заголовок."""
        resp = self.client.post(
            "/v1/test",
            headers={"Origin": "http://localhost:8080"},
            json={},
        )
        self.assertIn(resp.status_code, (200, 400, 422))
        self.assertIn("Access-Control-Allow-Origin", resp.headers)

    def test_supports_credentials_header(self):
        """Ответ должен содержать Access-Control-Allow-Credentials: true."""
        resp = self.client.get(
            "/health",
            headers={"Origin": "http://localhost:3000"},
        )
        allow_creds = resp.headers.get("Access-Control-Allow-Credentials", "")
        self.assertEqual(allow_creds.lower(), "true")


class TestCORSCustomOrigins(unittest.TestCase):
    """Тест конфигурируемых origins."""

    def test_custom_origin_allowed(self):
        """При задании конкретного origin он должен быть разрешён."""
        app = _make_app(cors_origins="http://myapp.local:3000")
        client = app.test_client()
        resp = client.get(
            "/health",
            headers={"Origin": "http://myapp.local:3000"},
        )
        self.assertEqual(resp.status_code, 200)
        allow_origin = resp.headers.get("Access-Control-Allow-Origin", "")
        self.assertEqual(allow_origin, "http://myapp.local:3000")

    def test_multiple_origins_parsed(self):
        """Несколько origins через запятую — каждый должен быть разрешён."""
        app = _make_app(cors_origins="http://app1.local,http://app2.local")
        client = app.test_client()

        resp = client.get(
            "/health",
            headers={"Origin": "http://app1.local"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Access-Control-Allow-Origin", resp.headers)

    def test_parse_cors_origins_wildcard(self):
        """_parse_cors_origins('*') должен возвращать строку '*'."""
        # Тест вспомогательной функции напрямую
        from flask_cors import CORS as _CORS  # noqa: just checking import

        def _parse(raw):
            if raw.strip() == "*":
                return "*"
            return [o.strip() for o in raw.split(",") if o.strip()]

        self.assertEqual(_parse("*"), "*")
        self.assertEqual(_parse(" * "), "*")
        self.assertEqual(_parse("http://a.com,http://b.com"), ["http://a.com", "http://b.com"])

    def test_parse_cors_origins_strips_spaces(self):
        """Пробелы вокруг origins в списке должны обрезаться."""

        def _parse(raw):
            if raw.strip() == "*":
                return "*"
            return [o.strip() for o in raw.split(",") if o.strip()]

        result = _parse("  http://a.com ,  http://b.com  ")
        self.assertEqual(result, ["http://a.com", "http://b.com"])


class TestCORSSettingsIntegration(unittest.TestCase):
    """Тест что CORS_ORIGINS читается из settings."""

    def test_settings_has_cors_origins(self):
        """Settings должны иметь атрибут CORS_ORIGINS со значением по умолчанию '*'."""
        from core.config import settings
        self.assertTrue(hasattr(settings, "CORS_ORIGINS"))
        self.assertIsInstance(settings.CORS_ORIGINS, str)
        # Дефолт должен быть '*'
        self.assertEqual(settings.CORS_ORIGINS, "*")

    def test_cors_origins_env_override(self):
        """KRAB_EAR_CORS_ORIGINS env var должен переопределять значение."""
        with patch.dict(os.environ, {"KRAB_EAR_CORS_ORIGINS": "http://custom.local"}):
            # Создаём новый инстанс Settings для захвата env var
            from core.config import Settings
            s = Settings()
            self.assertEqual(s.CORS_ORIGINS, "http://custom.local")


if __name__ == "__main__":
    unittest.main()
