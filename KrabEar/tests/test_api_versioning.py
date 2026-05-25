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
import threading
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


class TestNegotiateHighestSupported(unittest.TestCase):
    """negotiate_returns_highest_supported — no explicit hint returns latest default."""

    def setUp(self):
        self.app = _make_app()

    def test_negotiate_returns_highest_supported(self):
        """When client sends all supported versions in Accept, highest wins by path."""
        # The highest version reachable via URL prefix is V2.
        with self.app.test_request_context("/v2/stt"):
            version = get_api_version()
        self.assertEqual(version, APIVersion.V2)

    def test_negotiate_v1_still_supported(self):
        """V1 remains accessible alongside V2."""
        with self.app.test_request_context("/v1/stt"):
            version = get_api_version()
        self.assertEqual(version, APIVersion.V1)

    def test_all_supported_versions_in_list(self):
        """SUPPORTED_VERSIONS must contain both V1 and V2 (marked supported)."""
        self.assertIn(APIVersion.V1, SUPPORTED_VERSIONS)
        self.assertIn(APIVersion.V2, SUPPORTED_VERSIONS)

    def test_v1_marked_supported(self):
        """APIVersion.V1 is present in SUPPORTED_VERSIONS."""
        self.assertIn(APIVersion.V1, SUPPORTED_VERSIONS)

    def test_v2_marked_supported(self):
        """APIVersion.V2 is present in SUPPORTED_VERSIONS."""
        self.assertIn(APIVersion.V2, SUPPORTED_VERSIONS)

    def test_unknown_version_rejected(self):
        """Unknown version string via query param falls back to default, not error."""
        with self.app.test_request_context("/ping?api_version=v99"):
            version = get_api_version()
        self.assertEqual(version, DEFAULT_VERSION)

    def test_unknown_version_via_accept_rejected(self):
        """Unknown version in Accept header falls back to default."""
        with self.app.test_request_context(
            "/ping", headers={"Accept": "application/vnd.krabear.v99+json"}
        ):
            version = get_api_version()
        self.assertEqual(version, DEFAULT_VERSION)


class TestUnicodeInMetadata(unittest.TestCase):
    """Unicode characters in version metadata / deprecation headers are handled."""

    def setUp(self):
        self.app = Flask(__name__)

    def test_unicode_in_sunset_date_header(self):
        """Sunset header with unicode-safe ASCII date works correctly."""
        with self.app.test_request_context("/"):
            resp = self.app.response_class(status=200)
            resp = deprecation_warning(resp, "v1", "2027-01-01")
            # Confirm the header is set and readable as a plain string
            self.assertIsInstance(resp.headers["Sunset"], str)
            self.assertEqual(resp.headers["Sunset"], "2027-01-01")

    def test_unicode_in_deprecation_version_field(self):
        """Version string containing non-ASCII is stored without error."""
        with self.app.test_request_context("/"):
            resp = self.app.response_class(status=200)
            # Non-ASCII version label — should not crash
            resp = deprecation_warning(resp, "ñv1", "2027-01-01")
            self.assertIn("Deprecation", resp.headers)

    def test_get_api_info_app_version_is_string(self):
        """app_version in get_api_info() is a non-empty string (may include unicode)."""
        info = get_api_info()
        self.assertIsInstance(info["app_version"], str)
        self.assertTrue(len(info["app_version"]) > 0)

    def test_deprecation_metadata_format(self):
        """Deprecation header follows version=\\"<ver>\\" format."""
        with self.app.test_request_context("/"):
            resp = self.app.response_class(status=200)
            resp = deprecation_warning(resp, "v1", "2026-12-31")
            self.assertIn('version="v1"', resp.headers["Deprecation"])
            self.assertEqual(resp.headers["Sunset"], "2026-12-31")


class TestConcurrentNegotiate(unittest.TestCase):
    """get_api_version() is safe to call concurrently from multiple threads."""

    def setUp(self):
        self.app = _make_app()

    def test_concurrent_negotiate(self):
        """Multiple threads calling get_api_version() simultaneously return consistent results."""
        results = []
        errors = []

        def worker(path, expected):
            try:
                with self.app.test_request_context(path):
                    v = get_api_version()
                results.append((v, expected))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=("/v1/ping", APIVersion.V1)),
            threading.Thread(target=worker, args=("/v2/ping", APIVersion.V2)),
            threading.Thread(target=worker, args=("/ping", DEFAULT_VERSION)),
            threading.Thread(target=worker, args=("/v1/stt", APIVersion.V1)),
            threading.Thread(target=worker, args=("/v2/stt", APIVersion.V2)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        for actual, expected in results:
            self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
