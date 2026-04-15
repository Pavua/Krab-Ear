"""Tests for KrabEar/backend/api_versioning.py."""

from backend.api_versioning import (
    APIVersion,
    DEFAULT_VERSION,
    SUPPORTED_VERSIONS,
    DEPRECATED_VERSIONS,
    get_api_version,
    api_version_header,
    deprecation_warning,
    get_api_info,
)
from flask import Flask
import sys
import os
import unittest

# Ensure project root is on sys.path so ``backend.*`` imports resolve.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _make_app() -> Flask:
    """Return a minimal Flask app with the version header handler wired up."""
    app = Flask(__name__)
    app.after_request(api_version_header())

    @app.route("/v1/ping")
    def v1_ping():
        from flask import jsonify
        return jsonify({"ok": True})

    @app.route("/v2/ping")
    def v2_ping():
        from flask import jsonify
        return jsonify({"ok": True})

    @app.route("/ping")
    def plain_ping():
        from flask import jsonify
        return jsonify({"ok": True})

    return app


class TestAPIVersionEnum(unittest.TestCase):
    """APIVersion enum smoke tests."""

    def test_v1_value(self):
        self.assertEqual(APIVersion.V1.value, "v1")

    def test_v2_value(self):
        self.assertEqual(APIVersion.V2.value, "v2")

    def test_default_version_is_v1(self):
        self.assertEqual(DEFAULT_VERSION, APIVersion.V1)

    def test_supported_versions_contains_v1_and_v2(self):
        self.assertIn(APIVersion.V1, SUPPORTED_VERSIONS)
        self.assertIn(APIVersion.V2, SUPPORTED_VERSIONS)


class TestGetApiVersion(unittest.TestCase):
    """get_api_version() detection priority tests."""

    def setUp(self):
        self.app = _make_app()

    def test_url_prefix_v1(self):
        with self.app.test_request_context("/v1/readiness"):
            self.assertEqual(get_api_version(), APIVersion.V1)

    def test_url_prefix_v2(self):
        with self.app.test_request_context("/v2/readiness"):
            self.assertEqual(get_api_version(), APIVersion.V2)

    def test_accept_header_v1(self):
        with self.app.test_request_context(
            "/ping", headers={"Accept": "application/vnd.krabear.v1+json"}
        ):
            self.assertEqual(get_api_version(), APIVersion.V1)

    def test_accept_header_v2(self):
        with self.app.test_request_context(
            "/ping", headers={"Accept": "application/vnd.krabear.v2+json"}
        ):
            self.assertEqual(get_api_version(), APIVersion.V2)

    def test_query_param_v1(self):
        with self.app.test_request_context("/ping?api_version=v1"):
            self.assertEqual(get_api_version(), APIVersion.V1)

    def test_query_param_v2(self):
        with self.app.test_request_context("/ping?api_version=v2"):
            self.assertEqual(get_api_version(), APIVersion.V2)

    def test_fallback_to_default_when_no_hint(self):
        with self.app.test_request_context("/ping"):
            self.assertEqual(get_api_version(), DEFAULT_VERSION)

    def test_url_prefix_takes_priority_over_query_param(self):
        # URL says v1, query param says v2 — URL wins.
        with self.app.test_request_context("/v1/stt?api_version=v2"):
            self.assertEqual(get_api_version(), APIVersion.V1)

    def test_unknown_query_param_falls_back_to_default(self):
        with self.app.test_request_context("/ping?api_version=v99"):
            self.assertEqual(get_api_version(), DEFAULT_VERSION)

    def test_exact_path_without_trailing_slash(self):
        # e.g. /v1 (no slash) should still resolve to V1
        with self.app.test_request_context("/v1"):
            self.assertEqual(get_api_version(), APIVersion.V1)


class TestApiVersionHeader(unittest.TestCase):
    """after_request handler adds X-API-Version header."""

    def setUp(self):
        self.app = _make_app()
        self.client = self.app.test_client()

    def test_x_api_version_present_on_v1_route(self):
        resp = self.client.get("/v1/ping")
        self.assertIn("X-API-Version", resp.headers)
        self.assertEqual(resp.headers["X-API-Version"], "v1")

    def test_x_api_version_present_on_v2_route(self):
        resp = self.client.get("/v2/ping")
        self.assertEqual(resp.headers["X-API-Version"], "v2")

    def test_x_api_version_defaults_on_unversioned_route(self):
        resp = self.client.get("/ping")
        self.assertEqual(resp.headers["X-API-Version"], DEFAULT_VERSION.value)

    def test_header_via_query_param(self):
        resp = self.client.get("/ping?api_version=v2")
        self.assertEqual(resp.headers["X-API-Version"], "v2")


class TestDeprecationWarning(unittest.TestCase):
    """deprecation_warning() injects Sunset and Deprecation headers."""

    def setUp(self):
        self.app = Flask(__name__)

    def test_adds_deprecation_header(self):
        with self.app.test_request_context("/"):
            with self.app.app_context():
                resp = self.app.response_class(status=200)
                resp = deprecation_warning(resp, "v1", "2027-01-01")
                self.assertIn("Deprecation", resp.headers)
                self.assertIn("v1", resp.headers["Deprecation"])

    def test_adds_sunset_header(self):
        with self.app.test_request_context("/"):
            resp = self.app.response_class(status=200)
            resp = deprecation_warning(resp, "v1", "2027-01-01")
            self.assertEqual(resp.headers["Sunset"], "2027-01-01")

    def test_custom_sunset_date(self):
        with self.app.test_request_context("/"):
            resp = self.app.response_class(status=200)
            resp = deprecation_warning(resp, "v2", "2028-06-15")
            self.assertEqual(resp.headers["Sunset"], "2028-06-15")
            self.assertIn("v2", resp.headers["Deprecation"])


class TestGetApiInfo(unittest.TestCase):
    """get_api_info() returns correct metadata dict."""

    def test_contains_current_version(self):
        info = get_api_info()
        self.assertIn("current_version", info)
        self.assertEqual(info["current_version"], DEFAULT_VERSION.value)

    def test_supported_versions_is_list(self):
        info = get_api_info()
        self.assertIsInstance(info["supported_versions"], list)

    def test_supported_versions_contains_v1_and_v2(self):
        info = get_api_info()
        self.assertIn("v1", info["supported_versions"])
        self.assertIn("v2", info["supported_versions"])

    def test_deprecated_versions_is_list(self):
        info = get_api_info()
        self.assertIsInstance(info["deprecated_versions"], list)

    def test_no_deprecated_versions_by_default(self):
        # By default DEPRECATED_VERSIONS is empty.
        info = get_api_info()
        self.assertEqual(info["deprecated_versions"], [])

    def test_deprecated_versions_format_when_populated(self):
        """If a version is added to DEPRECATED_VERSIONS it appears in the output."""
        original = dict(DEPRECATED_VERSIONS)
        try:
            DEPRECATED_VERSIONS[APIVersion.V1] = "2026-12-31"
            info = get_api_info()
            self.assertTrue(
                any(
                    d["version"] == "v1" and d["sunset_date"] == "2026-12-31"
                    for d in info["deprecated_versions"]
                )
            )
        finally:
            DEPRECATED_VERSIONS.clear()
            DEPRECATED_VERSIONS.update(original)

    def test_info_keys_present(self):
        info = get_api_info()
        self.assertSetEqual(
            set(info.keys()),
            {"app_version", "current_version", "supported_versions", "deprecated_versions"},
        )


class TestApiInfoEndpoint(unittest.TestCase):
    """/api/info endpoint integration smoke test using a local mini-app."""

    def setUp(self):
        mini_app = Flask(__name__)

        @mini_app.route("/api/info", methods=["GET"])
        def api_info_route():
            from flask import jsonify
            return jsonify(get_api_info())

        self.client = mini_app.test_client()

    def test_endpoint_returns_200(self):
        resp = self.client.get("/api/info")
        self.assertEqual(resp.status_code, 200)

    def test_endpoint_returns_json_with_current_version(self):
        resp = self.client.get("/api/info")
        data = resp.get_json()
        self.assertIsNotNone(data)
        self.assertIn("current_version", data)

    def test_endpoint_supported_versions_non_empty(self):
        resp = self.client.get("/api/info")
        data = resp.get_json()
        self.assertGreater(len(data["supported_versions"]), 0)


if __name__ == "__main__":
    unittest.main()
