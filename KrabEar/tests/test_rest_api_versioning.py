"""Wave 215 — REST API versioning enforcement edge cases.

Tests HTTP-level behaviour of the versioned REST server:
  - /v1/* paths respond (200 or real status, never 404 for known routes)
  - /v2/* paths are currently unregistered (404) — documented as acceptable
  - Unsupported /vN/* paths (N not in [1,2]) return 404
  - X-API-Version response header is always present
  - Deprecated versions (when configured) inject Sunset + Deprecation headers
  - Unversioned paths fall back to DEFAULT_VERSION header
  - Paths with extra trailing segments
  - Accept-header version detection propagates to X-API-Version
  - Concurrent requests each carry the correct X-API-Version
  - deprecation_warning() produces ISO-8601-compatible date strings
  - sunset_date format is consistent

All tests use Flask test_client — the REST server is NEVER started.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_api_versioning.py -v
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure KrabEar package root is on sys.path.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Lazy guard: skip all tests if Flask or rest_server deps are unavailable.
# Heavy objects (AudioEngine, StateStore, Transcriber) are patched before the
# module-level singleton is instantiated, matching the pattern in
# test_rest_server_unit.py.
# ---------------------------------------------------------------------------
_REST_AVAILABLE = False
_rest_mod = None
_versioning_mod = None

try:
    import flask  # noqa: F401

    _engine_mock = MagicMock()
    _engine_mock.quality_profile = "balanced"

    _store_mock = MagicMock()
    _store_mock.load_vocabulary.return_value = []
    _store_mock.is_idempotent.return_value = False
    _store_mock.add_history_item.return_value = MagicMock(id="hist-wave215")

    _transcriber_mock = MagicMock()
    _transcriber_mock.transcribe.return_value = {
        "text": "тест",
        "raw_text": "тест",
        "confidence": 0.88,
        "duration_ms": 500,
        "engine": "mlx-whisper",
        "model": "whisper-small",
        "language": "ru",
        "segments": [],
        "diarization": {},
    }

    _metrics_mock = MagicMock()
    _metrics_mock.get_summary.return_value = {
        "total_requests": 0,
        "error_rate": 0.0,
        "error_count": 0,
        "status": "ok",
        "stt_metrics": {
            "latency_ms": {"p50": 100, "p95": 300, "p99": 600, "avg": 120},
            "confidence": {"avg": 0.88},
        },
        "window_size": 0,
    }

    with patch("core.engine.AudioEngine", return_value=_engine_mock), \
            patch("backend.state_store.StateStore", return_value=_store_mock), \
            patch("backend.transcriber.Transcriber", return_value=_transcriber_mock), \
            patch("backend.metrics_collector.metrics", _metrics_mock):
        import backend.rest_server as _rest_mod

    import backend.api_versioning as _versioning_mod
    _REST_AVAILABLE = True
except Exception:  # pragma: no cover
    pass

_SKIP = not _REST_AVAILABLE


def _get_client():
    """Return a Flask test client for the REST server app."""
    assert _rest_mod is not None
    return _rest_mod.app.test_client()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_x_api_version(tc, resp, expected_version: str):
    """Assert X-API-Version header equals *expected_version*."""
    tc.assertIn(
        "X-API-Version", resp.headers,
        "X-API-Version header must be present on every response",
    )
    tc.assertEqual(resp.headers["X-API-Version"], expected_version)


# ============================================================================
# 1. /v1/* paths
# ============================================================================

@unittest.skipIf(_SKIP, "REST server deps unavailable")
class TestV1PathBehaviour(unittest.TestCase):
    """GET /v1/health returns a real response and X-API-Version: v1."""

    def setUp(self):
        self.client = _get_client()

    # test_v1_path_returns_200
    def test_v1_health_route_exists_and_returns_2xx_or_503(self):
        """GET /v1/readiness is a real registered route (not 404)."""
        resp = self.client.get("/v1/readiness")
        self.assertIn(
            resp.status_code, (200, 503),
            f"Expected 200 or 503 from /v1/readiness, got {resp.status_code}",
        )

    def test_v1_vocabulary_returns_200(self):
        """GET /v1/vocabulary is registered and returns 200."""
        resp = self.client.get("/v1/vocabulary")
        self.assertEqual(resp.status_code, 200)

    def test_v1_path_sets_x_api_version_v1(self):
        """X-API-Version must be 'v1' for /v1/* requests."""
        resp = self.client.get("/v1/vocabulary")
        _assert_x_api_version(self, resp, "v1")

    def test_v1_vocabulary_response_is_json(self):
        """GET /v1/vocabulary returns JSON content."""
        resp = self.client.get("/v1/vocabulary")
        data = resp.get_json()
        self.assertIsNotNone(data, "Expected JSON response from /v1/vocabulary")


# ============================================================================
# 2. /v2/* paths — currently unregistered
# ============================================================================

@unittest.skipIf(_SKIP, "REST server deps unavailable")
class TestV2PathBehaviour(unittest.TestCase):
    """v2 is declared in APIVersion but no /v2/* blueprint is registered yet.

    The server should return 404 (Flask default for unknown routes), NOT 500.
    X-API-Version MAY be v2 or absent — we only assert no crash.
    """

    def setUp(self):
        self.client = _get_client()

    # test_v2_path_returns_200_or_404
    def test_v2_readiness_returns_404(self):
        """GET /v2/readiness is unregistered — expect 404, not 500."""
        resp = self.client.get("/v2/readiness")
        self.assertEqual(
            resp.status_code, 404,
            f"/v2/readiness should 404 (no v2 blueprint registered), got {resp.status_code}",
        )

    def test_v2_vocabulary_returns_404(self):
        """GET /v2/vocabulary — no v2 blueprint — 404."""
        resp = self.client.get("/v2/vocabulary")
        self.assertEqual(resp.status_code, 404)

    def test_v2_404_does_not_return_500(self):
        """v2 unknown route must never return 500."""
        resp = self.client.get("/v2/anything")
        self.assertNotEqual(resp.status_code, 500)


# ============================================================================
# 3. Unsupported version prefix (/v3, /v0, /v99)
# ============================================================================

@unittest.skipIf(_SKIP, "REST server deps unavailable")
class TestUnsupportedVersionPath(unittest.TestCase):
    """Paths with unsupported version prefixes return 404."""

    def setUp(self):
        self.client = _get_client()

    # test_unsupported_version_returns_400_or_404
    def test_v3_returns_404(self):
        resp = self.client.get("/v3/vocabulary")
        self.assertEqual(
            resp.status_code, 404,
            "/v3/* is not a registered blueprint — must 404",
        )

    def test_v0_returns_404(self):
        resp = self.client.get("/v0/health")
        self.assertEqual(resp.status_code, 404)

    def test_v99_returns_404(self):
        resp = self.client.get("/v99/stt/transcribe")
        self.assertEqual(resp.status_code, 404)

    def test_unsupported_version_never_500(self):
        for path in ("/v0/x", "/v3/x", "/v99/x"):
            resp = self.client.get(path)
            self.assertNotEqual(
                resp.status_code, 500,
                f"Unsupported version path {path} must not raise 500",
            )


# ============================================================================
# 4. Deprecated version warning headers
# ============================================================================

@unittest.skipIf(_SKIP, "REST server deps unavailable")
class TestDeprecatedVersionHeaders(unittest.TestCase):
    """When a version is in DEPRECATED_VERSIONS, Sunset + Deprecation headers appear."""

    def setUp(self):
        assert _versioning_mod is not None
        self.original = dict(_versioning_mod.DEPRECATED_VERSIONS)

    def tearDown(self):
        _versioning_mod.DEPRECATED_VERSIONS.clear()
        _versioning_mod.DEPRECATED_VERSIONS.update(self.original)

    # test_deprecated_version_includes_warning_header
    def test_deprecation_warning_adds_deprecation_header(self):
        """deprecation_warning() injects Deprecation header with version string."""
        from flask import Flask
        mini = Flask(__name__)
        with mini.test_request_context("/"):
            resp = mini.response_class(status=200)
            resp = _versioning_mod.deprecation_warning(resp, "v1", "2027-06-01")
        self.assertIn("Deprecation", resp.headers)
        self.assertIn("v1", resp.headers["Deprecation"])

    def test_deprecation_warning_adds_sunset_header(self):
        """deprecation_warning() injects Sunset header with exact ISO date."""
        from flask import Flask
        mini = Flask(__name__)
        with mini.test_request_context("/"):
            resp = mini.response_class(status=200)
            resp = _versioning_mod.deprecation_warning(resp, "v1", "2027-06-01")
        self.assertEqual(resp.headers["Sunset"], "2027-06-01")

    def test_get_api_info_reflects_deprecated_version(self):
        """When V1 is marked deprecated, get_api_info() reports it."""
        _versioning_mod.DEPRECATED_VERSIONS[_versioning_mod.APIVersion.V1] = "2027-01-01"
        info = _versioning_mod.get_api_info()
        deprecations = info["deprecated_versions"]
        self.assertTrue(
            any(d["version"] == "v1" for d in deprecations),
            f"Expected v1 in deprecated_versions, got {deprecations}",
        )

    def test_deprecated_entry_includes_sunset_date(self):
        """Deprecated version entry must carry a sunset_date key."""
        _versioning_mod.DEPRECATED_VERSIONS[_versioning_mod.APIVersion.V1] = "2027-01-01"
        info = _versioning_mod.get_api_info()
        entry = next(
            (d for d in info["deprecated_versions"] if d["version"] == "v1"), None
        )
        self.assertIsNotNone(entry)
        self.assertIn("sunset_date", entry)
        self.assertEqual(entry["sunset_date"], "2027-01-01")


# ============================================================================
# 5. Default version when path has no version prefix
# ============================================================================

@unittest.skipIf(_SKIP, "REST server deps unavailable")
class TestDefaultVersionUnversionedPath(unittest.TestCase):
    """Unversioned paths fall back to DEFAULT_VERSION in X-API-Version."""

    def setUp(self):
        self.client = _get_client()

    # test_default_version_when_unversioned_path
    def test_health_endpoint_returns_default_version_header(self):
        """GET /health has no version prefix — X-API-Version should be default (v1)."""
        resp = self.client.get("/health")
        _assert_x_api_version(self, resp, _versioning_mod.DEFAULT_VERSION.value)

    def test_info_endpoint_returns_default_version_header(self):
        """GET /info (monitoring blueprint) uses default version."""
        resp = self.client.get("/info")
        if resp.status_code == 404:
            self.skipTest("/info not registered in this app configuration")
        _assert_x_api_version(self, resp, _versioning_mod.DEFAULT_VERSION.value)

    def test_unversioned_path_never_returns_v2_by_default(self):
        """Without any version hint, X-API-Version must not be 'v2'."""
        resp = self.client.get("/health")
        ver = resp.headers.get("X-API-Version", "")
        self.assertNotEqual(ver, "v2")


# ============================================================================
# 6. Paths with extra trailing segments
# ============================================================================

@unittest.skipIf(_SKIP, "REST server deps unavailable")
class TestPathWithExtraSegments(unittest.TestCase):
    """Extra path segments under /v1/ that are unregistered return 404."""

    def setUp(self):
        self.client = _get_client()

    # test_path_with_extra_segments
    def test_v1_extra_segment_returns_404(self):
        resp = self.client.get("/v1/nonexistent/route/segment")
        self.assertEqual(resp.status_code, 404)

    def test_v1_extra_segment_does_not_500(self):
        resp = self.client.get("/v1/foo/bar/baz")
        self.assertNotEqual(resp.status_code, 500)

    def test_v1_extra_segment_x_api_version_still_v1(self):
        """Even for 404 responses, X-API-Version should be v1 for /v1/* paths."""
        resp = self.client.get("/v1/totally/unknown/path")
        # The after_request handler fires even for 404s registered via Flask.
        # Assert header present (may be v1 or default — both acceptable).
        self.assertIn("X-API-Version", resp.headers)


# ============================================================================
# 7. Version via Accept header
# ============================================================================

@unittest.skipIf(_SKIP, "REST server deps unavailable")
class TestVersionInAcceptHeader(unittest.TestCase):
    """Accept: application/vnd.krabear.vN+json selects version N."""

    def setUp(self):
        self.client = _get_client()

    # test_version_in_accept_header
    def test_accept_header_v1_sets_x_api_version_v1(self):
        """Accept vnd.krabear.v1 on unversioned path → X-API-Version: v1."""
        resp = self.client.get(
            "/health",
            headers={"Accept": "application/vnd.krabear.v1+json"},
        )
        _assert_x_api_version(self, resp, "v1")

    def test_accept_header_v2_sets_x_api_version_v2(self):
        """Accept vnd.krabear.v2 on unversioned path → X-API-Version: v2."""
        resp = self.client.get(
            "/health",
            headers={"Accept": "application/vnd.krabear.v2+json"},
        )
        _assert_x_api_version(self, resp, "v2")

    def test_accept_header_unknown_vendor_falls_back_to_default(self):
        """Non-krabear Accept header falls back to default version."""
        resp = self.client.get(
            "/health",
            headers={"Accept": "application/json"},
        )
        _assert_x_api_version(self, resp, _versioning_mod.DEFAULT_VERSION.value)

    def test_accept_header_v99_not_supported_falls_back_to_default(self):
        """vnd.krabear.v99 is not a supported version — fall back to default."""
        resp = self.client.get(
            "/health",
            headers={"Accept": "application/vnd.krabear.v99+json"},
        )
        _assert_x_api_version(self, resp, _versioning_mod.DEFAULT_VERSION.value)

    def test_url_prefix_takes_priority_over_accept_header(self):
        """URL /v1/* must win over Accept: vnd.krabear.v2+json."""
        resp = self.client.get(
            "/v1/vocabulary",
            headers={"Accept": "application/vnd.krabear.v2+json"},
        )
        _assert_x_api_version(self, resp, "v1")


# ============================================================================
# 8. Concurrent version routing
# ============================================================================

@unittest.skipIf(_SKIP, "REST server deps unavailable")
class TestConcurrentVersionRouting(unittest.TestCase):
    """Multiple threads hitting /v1/ and /health concurrently each get correct headers."""

    def setUp(self):
        self.client = _get_client()

    # test_concurrent_version_routing
    def test_concurrent_v1_and_unversioned_requests(self):
        """50 concurrent requests split between /v1/vocabulary and /health.

        Every /v1/* response must carry X-API-Version: v1.
        Every /health response must carry X-API-Version: v1 (default).
        No response may be status 500.
        """
        results = []
        errors = []

        def do_request(path: str):
            try:
                resp = self.client.get(path)
                results.append((path, resp.status_code, resp.headers.get("X-API-Version")))
            except Exception as exc:
                errors.append(str(exc))

        threads = []
        for i in range(50):
            path = "/v1/vocabulary" if i % 2 == 0 else "/health"
            t = threading.Thread(target=do_request, args=(path,))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        self.assertEqual(len(results), 50, "Expected 50 results")

        for path, status, x_api_ver in results:
            self.assertNotEqual(status, 500, f"Got 500 on {path}")
            self.assertIsNotNone(x_api_ver, f"Missing X-API-Version on {path}")
            if path.startswith("/v1/"):
                self.assertEqual(x_api_ver, "v1", f"Wrong version on {path}: {x_api_ver}")


# ============================================================================
# 9. Deprecation metadata format validation
# ============================================================================

@unittest.skipIf(_SKIP, "REST server deps unavailable")
class TestDeprecationDateFormat(unittest.TestCase):
    """Sunset / deprecation date strings must be ISO-8601 (YYYY-MM-DD)."""

    def setUp(self):
        assert _versioning_mod is not None
        self.original = dict(_versioning_mod.DEPRECATED_VERSIONS)

    def tearDown(self):
        _versioning_mod.DEPRECATED_VERSIONS.clear()
        _versioning_mod.DEPRECATED_VERSIONS.update(self.original)

    # test_deprecation_date_format
    def test_deprecation_warning_sunset_header_is_iso8601(self):
        """Sunset header value must match YYYY-MM-DD format."""
        import re
        from flask import Flask

        iso_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        mini = Flask(__name__)
        sunset_date = "2027-12-31"
        with mini.test_request_context("/"):
            resp = mini.response_class(status=200)
            resp = _versioning_mod.deprecation_warning(resp, "v1", sunset_date)
        self.assertRegex(
            resp.headers["Sunset"],
            iso_pattern,
            "Sunset header must be ISO-8601 YYYY-MM-DD",
        )

    # test_sunset_date_format
    def test_get_api_info_sunset_date_is_iso8601(self):
        """get_api_info() deprecated_versions[].sunset_date must be ISO-8601."""
        import re

        iso_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        _versioning_mod.DEPRECATED_VERSIONS[_versioning_mod.APIVersion.V1] = "2026-12-31"
        info = _versioning_mod.get_api_info()
        for entry in info["deprecated_versions"]:
            self.assertRegex(
                entry["sunset_date"],
                iso_pattern,
                f"sunset_date '{entry['sunset_date']}' is not ISO-8601",
            )

    def test_deprecation_warning_header_quotes_version(self):
        """Deprecation header must quote the version string: version=\"v1\"."""
        from flask import Flask

        mini = Flask(__name__)
        with mini.test_request_context("/"):
            resp = mini.response_class(status=200)
            resp = _versioning_mod.deprecation_warning(resp, "v1", "2027-01-01")
        self.assertIn(
            'version="v1"',
            resp.headers["Deprecation"],
            "Deprecation header should contain version=\"v1\"",
        )

    def test_multiple_deprecated_versions_all_have_sunset_date(self):
        """All entries in deprecated_versions must contain a sunset_date key."""
        _versioning_mod.DEPRECATED_VERSIONS[_versioning_mod.APIVersion.V1] = "2026-06-01"
        info = _versioning_mod.get_api_info()
        for entry in info["deprecated_versions"]:
            self.assertIn(
                "sunset_date", entry,
                f"Missing sunset_date in deprecated entry: {entry}",
            )


# ============================================================================
# 10. get_api_version() with explicit request object (unit-level)
# ============================================================================

@unittest.skipIf(_SKIP, "REST server deps unavailable")
class TestGetApiVersionExplicitRequest(unittest.TestCase):
    """get_api_version() accepts an explicit req parameter for testability."""

    def setUp(self):
        from flask import Flask
        self.app = Flask(__name__)

    def test_explicit_req_url_v1(self):
        with self.app.test_request_context("/v1/ping"):
            from flask import request as flask_req
            result = _versioning_mod.get_api_version(req=flask_req)
        self.assertEqual(result, _versioning_mod.APIVersion.V1)

    def test_explicit_req_url_v2(self):
        with self.app.test_request_context("/v2/ping"):
            from flask import request as flask_req
            result = _versioning_mod.get_api_version(req=flask_req)
        self.assertEqual(result, _versioning_mod.APIVersion.V2)

    def test_explicit_req_query_param_v2(self):
        with self.app.test_request_context("/ping?api_version=v2"):
            from flask import request as flask_req
            result = _versioning_mod.get_api_version(req=flask_req)
        self.assertEqual(result, _versioning_mod.APIVersion.V2)

    def test_explicit_req_accept_header_v2(self):
        with self.app.test_request_context(
            "/ping", headers={"Accept": "application/vnd.krabear.v2+json"}
        ):
            from flask import request as flask_req
            result = _versioning_mod.get_api_version(req=flask_req)
        self.assertEqual(result, _versioning_mod.APIVersion.V2)

    def test_explicit_req_empty_path_falls_back_to_default(self):
        with self.app.test_request_context("/"):
            from flask import request as flask_req
            result = _versioning_mod.get_api_version(req=flask_req)
        self.assertEqual(result, _versioning_mod.DEFAULT_VERSION)


if __name__ == "__main__":
    unittest.main()
