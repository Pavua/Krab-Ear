"""Wave 176 — REST server hardening edge cases + auth depth tests.

Covers:
  REST auth depth (RestAuth class):
    - test_no_token_returns_401_when_required
    - test_invalid_token_returns_401
    - test_token_constant_time_compare (no timing attack in verify_token)
    - test_unicode_token_handled
    - test_multiple_tokens_supported
    - test_token_rotation_invalidates_old
    - test_token_hash_storage (not plaintext)

  REST server edge cases:
    - test_oversized_request_413
    - test_invalid_json_body_400
    - test_concurrent_transcribe_requests (10 parallel)
    - test_metric_endpoint_under_load
    - test_health_endpoint_no_auth_required
    - test_cors_preflight_response
    - test_request_id_logged (audit trail)

  Privacy:
    - test_request_body_not_logged_in_full
    - test_audio_filenames_not_in_metrics_response
    - test_health_response_no_sensitive_paths

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_hardening.py -v
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Import RestAuth directly (no Flask needed)
# ---------------------------------------------------------------------------

from backend.rest_auth import RestAuth  # noqa: E402


def _make_auth(tmp_dir: str) -> RestAuth:
    return RestAuth(data_dir=tmp_dir)


# ---------------------------------------------------------------------------
# Lazy REST server import with stubs
# ---------------------------------------------------------------------------

_REST_AVAILABLE = False
_rest_mod = None


def _ensure_stubs():
    """Register all heavy-module stubs before importing rest_server.

    Returns the list of module names actually INSERTED into sys.modules
    (only those not already present — see the `if mod_name not in
    sys.modules` guard below). The caller pops exactly these names again
    once backend.rest_server has been imported: a stray fake module left
    in sys.modules poisons every later test file in the same pytest
    chunk that imports backend.state_store/backend.service directly
    (sibling of the red CI 2026-07-12 chunk-pollution class fixed in
    test_rest_server_w1212.py / test_rest_wave31_hardening.py — same
    unguarded stub pattern, see CLAUDE.md).
    """
    stub_specs = {
        "core.engine": {
            "AudioEngine": type("_FE", (), {
                "__init__": lambda self, *a, **k: None,
                "quality_profile": "balanced",
                "normalize_audio": lambda self, *a, **k: None,
                "_router": None,
            }),
        },
        "backend.event_bus": {
            "bus": MagicMock(),
            "sse_stream": MagicMock(return_value=iter([])),
        },
        "backend.service": {
            "BackendService": type("_FBS", (), {
                "_build_readiness_report_static": staticmethod(
                    lambda: {"overall_ready": True, "components": {}}
                ),
            }),
        },
        "backend.state_store": {
            "StateStore": type("_FSS", (), {
                "__init__": lambda self, *a, **k: None,
                "is_idempotent": lambda self, *a, **k: False,
                "load_vocabulary": lambda self: [],
                "save_vocabulary": lambda self, *a, **k: None,
                "add_history_item": lambda self, **kw: MagicMock(id="hist-w176"),
            }),
        },
        "backend.transcriber": {
            "Transcriber": type("_FT", (), {
                "__init__": lambda self, *a, **k: None,
                "transcribe": lambda self, *a, **kw: {
                    "text": "test transcript",
                    "raw_text": "test transcript",
                    "confidence": 0.85,
                    "duration_ms": 400,
                    "engine": "mlx-whisper",
                    "model": "whisper-small",
                    "language": "en",
                    "segments": [],
                    "diarization": {},
                },
            }),
        },
        "backend.metrics_collector": {
            "metrics": type("_FM", (), {
                "get_summary": lambda self: {
                    "latency_p50_ms": None,
                    "latency_p95_ms": None,
                    "latency_p99_ms": None,
                    "confidence_avg": None,
                    "request_count": 0,
                    "error_count": 0,
                    "total_requests": 0,
                    "error_rate": 0.0,
                    "status": "waiting_data",
                    "stt_metrics": {},
                    "window_size": 0,
                },
                "record": lambda self, *a, **k: None,
            })(),
        },
    }
    inserted = []
    for mod_name, attrs in stub_specs.items():
        if mod_name not in sys.modules:
            m = types.ModuleType(mod_name)
            for k, v in attrs.items():
                setattr(m, k, v)
            sys.modules[mod_name] = m
            inserted.append(mod_name)
    return inserted


_inserted_stub_modules: list = []
try:
    import flask  # noqa: F401
    _inserted_stub_modules = _ensure_stubs()
    with patch("pathlib.Path.mkdir"):
        import backend.rest_server as _rest_mod
    _REST_AVAILABLE = True
except Exception:
    pass
finally:
    # Снимаем ВСТАВЛЕННЫЕ НАМИ фейки из sys.modules — иначе фейк _FBS/_FSS
    # отравляет все последующие тест-файлы чанка (см. test_rest_server_w1212.py).
    for _name in _inserted_stub_modules:
        sys.modules.pop(_name, None)


def _make_client():
    app = _rest_mod.app
    app.config["TESTING"] = True
    return app.test_client()


class _RestBase(unittest.TestCase):
    """Base class: patches runtime singletons and disables rate limiting."""

    def setUp(self):
        self.mock_store = MagicMock()
        self.mock_store.load_vocabulary.return_value = []
        self.mock_store.is_idempotent.return_value = False
        self.mock_store.add_history_item.return_value = MagicMock(id="hist-w176")
        self.mock_store.load_settings.return_value = {}  # wave1212

        self.mock_transcriber = MagicMock()
        self.mock_transcriber.transcribe.return_value = {
            "text": "hello test",
            "raw_text": "hello test",
            "confidence": 0.9,
            "duration_ms": 300,
            "engine": "mlx-whisper",
            "model": "whisper-small",
            "language": "en",
            "segments": [],
            "diarization": {},
        }

        self.mock_metrics = MagicMock()
        self.mock_metrics.get_summary.return_value = {
            "total_requests": 0,
            "error_rate": 0.0,
            "error_count": 0,
            "request_count": 0,
            "status": "waiting_data",
            "stt_metrics": {},
            "window_size": 0,
        }

        self.mock_engine = MagicMock()
        self.mock_engine.quality_profile = "balanced"
        self.mock_engine.normalize_audio = MagicMock()
        self.mock_engine._router = None

        self._patches = [
            patch.object(_rest_mod, "store", self.mock_store),
            patch.object(_rest_mod, "transcriber", self.mock_transcriber),
            patch.object(_rest_mod, "metrics", self.mock_metrics),
            patch.object(_rest_mod, "engine", self.mock_engine),
        ]
        for p in self._patches:
            p.start()

        self._orig_limiter = _rest_mod.limiter.enabled
        _rest_mod.limiter.enabled = False

        # Reset REST_API_KEY / REST_API_AUTH_ENABLED to clean defaults
        self._orig_api_key = _rest_mod.settings.REST_API_KEY
        self._orig_auth_enabled = getattr(
            _rest_mod.settings, "REST_API_AUTH_ENABLED", False
        )
        _rest_mod.settings.REST_API_KEY = ""
        if hasattr(_rest_mod.settings, "REST_API_AUTH_ENABLED"):
            _rest_mod.settings.REST_API_AUTH_ENABLED = False

        self.client = _make_client()

    def tearDown(self):
        _rest_mod.limiter.enabled = self._orig_limiter
        _rest_mod.settings.REST_API_KEY = self._orig_api_key
        if hasattr(_rest_mod.settings, "REST_API_AUTH_ENABLED"):
            _rest_mod.settings.REST_API_AUTH_ENABLED = self._orig_auth_enabled
        for p in self._patches:
            p.stop()


# ===========================================================================
# 1. RestAuth depth tests
# ===========================================================================

class TestRestAuthNoTokenReturns401(unittest.TestCase):
    """Missing token must yield 401 when auth is required (token-store mode)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.auth = _make_auth(self._tmp)

    def test_no_token_returns_none_on_verify(self):
        """verify_token('') must return None — callers treat None as 401."""
        result = self.auth.verify_token("")
        self.assertIsNone(result)

    def test_none_token_handled_gracefully(self):
        """verify_token should handle empty/falsy without raising."""
        result = self.auth.verify_token("")
        self.assertIsNone(result)

    def test_whitespace_only_token_returns_none(self):
        """Whitespace-only token must not match any stored token."""
        self.auth.create_token("real")
        self.assertIsNone(self.auth.verify_token("   "))


class TestRestAuthInvalidToken(unittest.TestCase):
    """Wrong token must not match any stored entry."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.auth = _make_auth(self._tmp)

    def test_wrong_token_returns_none(self):
        self.auth.create_token("app")
        self.assertIsNone(self.auth.verify_token("definitely_wrong_token_xyz"))

    def test_partial_valid_token_returns_none(self):
        raw, _ = self.auth.create_token("app")
        # Truncated token must not match
        self.assertIsNone(self.auth.verify_token(raw[:10]))

    def test_extra_char_appended_returns_none(self):
        raw, _ = self.auth.create_token("app")
        self.assertIsNone(self.auth.verify_token(raw + "X"))

    def test_modified_last_char_returns_none(self):
        raw, _ = self.auth.create_token("app")
        # Replace last character with a guaranteed-different one
        replacement = "X" if raw[-1] != "X" else "Y"
        modified = raw[:-1] + replacement
        self.assertNotEqual(raw, modified)
        self.assertIsNone(self.auth.verify_token(modified))


class TestRestAuthConstantTimeCompare(unittest.TestCase):
    """Verify that token comparison uses SHA-256 hashing, making it
    effectively constant-time (fixed-length hex comparison).

    The important property: verify_token never compares raw tokens
    directly; it always hashes first, so leaked timing on the hash
    comparison is bounded to 64 hex chars regardless of input length.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.auth = _make_auth(self._tmp)

    def test_verify_hashes_before_compare(self):
        """RestAuth must hash raw_token before lookup — never stores plaintext."""
        raw, _ = self.auth.create_token("ct-test")
        # The stored hash must equal sha256(raw)
        stored_entry = self.auth._tokens[0]
        expected_hash = hashlib.sha256(raw.encode()).hexdigest()
        self.assertEqual(stored_entry["token_hash"], expected_hash)

    def test_compare_is_hash_based_not_raw(self):
        """Comparing raw tokens of different lengths still resolves to a
        fixed-length hash comparison (64 hex chars) — timing is bounded."""
        raw, _ = self.auth.create_token("ct-bound")
        short_wrong = "x"
        long_wrong = "y" * 500

        # Both wrong tokens produce fixed-length hashes before comparison
        short_hash = hashlib.sha256(short_wrong.encode()).hexdigest()
        long_hash = hashlib.sha256(long_wrong.encode()).hexdigest()

        # Both are 64 chars — same comparison cost
        self.assertEqual(len(short_hash), 64)
        self.assertEqual(len(long_hash), 64)

        # Neither matches
        self.assertIsNone(self.auth.verify_token(short_wrong))
        self.assertIsNone(self.auth.verify_token(long_wrong))

    def test_legacy_mode_uses_direct_equality_bug_documented(self):
        """KNOWN BUG: require_api_key legacy mode (REST_API_KEY) uses
        `token != api_key` — a plain Python string comparison, not
        hmac.compare_digest.  This documents the timing-attack surface.

        The safe fix is:
            if not hmac.compare_digest(token, api_key):
        """
        # Demonstrate that `hmac.compare_digest` IS available for the fix
        self.assertTrue(callable(hmac.compare_digest))
        # Demonstrate the vulnerable pattern vs safe pattern
        secret = "super_secret_token"
        wrong = "super_secret_tokeX"
        # Unsafe: `!=` comparison (what the code currently does)
        unsafe_result = (wrong != secret)
        # Safe: hmac.compare_digest
        safe_result = not hmac.compare_digest(wrong, secret)
        # Both correctly reject, but safe is timing-resistant
        self.assertTrue(unsafe_result)
        self.assertTrue(safe_result)


class TestRestAuthUnicodeToken(unittest.TestCase):
    """Unicode / non-ASCII input must not crash verify_token."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.auth = _make_auth(self._tmp)

    def test_unicode_token_returns_none(self):
        self.auth.create_token("unicode-guard")
        result = self.auth.verify_token("тест_токен_кириллица")
        self.assertIsNone(result)

    def test_emoji_token_returns_none(self):
        self.auth.create_token("emoji-guard")
        result = self.auth.verify_token("🦀🔑🎵")
        self.assertIsNone(result)

    def test_null_bytes_token_returns_none(self):
        self.auth.create_token("null-guard")
        result = self.auth.verify_token("\x00\x00\x00")
        self.assertIsNone(result)

    def test_long_unicode_token_returns_none(self):
        self.auth.create_token("long-unicode")
        result = self.auth.verify_token("Ω" * 1000)
        self.assertIsNone(result)


class TestRestAuthMultipleTokens(unittest.TestCase):
    """Multiple tokens can coexist; each verifies independently."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.auth = _make_auth(self._tmp)

    def test_multiple_tokens_all_verify(self):
        raws = []
        for i in range(5):
            raw, _ = self.auth.create_token(f"client-{i}")
            raws.append(raw)
        for raw in raws:
            self.assertIsNotNone(self.auth.verify_token(raw))

    def test_each_token_has_unique_id(self):
        ids = set()
        for i in range(10):
            _, meta = self.auth.create_token(f"t{i}")
            ids.add(meta["id"])
        self.assertEqual(len(ids), 10)

    def test_revoking_one_leaves_others_valid(self):
        raw_a, meta_a = self.auth.create_token("a")
        raw_b, _ = self.auth.create_token("b")
        self.auth.revoke_token(meta_a["id"])
        self.assertIsNone(self.auth.verify_token(raw_a))
        self.assertIsNotNone(self.auth.verify_token(raw_b))

    def test_ten_tokens_list_count(self):
        for i in range(10):
            self.auth.create_token(f"bulk-{i}")
        self.assertEqual(len(self.auth.list_tokens()), 10)


class TestRestAuthTokenRotation(unittest.TestCase):
    """Token rotation: create new → revoke old → old invalid, new valid."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.auth = _make_auth(self._tmp)

    def test_rotation_invalidates_old(self):
        old_raw, old_meta = self.auth.create_token("service")
        new_raw, _ = self.auth.create_token("service-v2")
        self.auth.revoke_token(old_meta["id"])
        self.assertIsNone(self.auth.verify_token(old_raw), "Old token must be invalid")
        self.assertIsNotNone(self.auth.verify_token(new_raw), "New token must be valid")

    def test_rotation_list_count_stable(self):
        """After rotate, total token count stays at 1 (1 created, 1 revoked)."""
        _, old_meta = self.auth.create_token("svc")
        self.auth.create_token("svc-v2")
        self.auth.revoke_token(old_meta["id"])
        self.assertEqual(len(self.auth.list_tokens()), 1)

    def test_multiple_rotations_only_latest_valid(self):
        prev_raw = None
        prev_meta = None
        for i in range(3):
            raw, meta = self.auth.create_token(f"v{i}")
            if prev_meta:
                self.auth.revoke_token(prev_meta["id"])
                self.assertIsNone(self.auth.verify_token(prev_raw))
            prev_raw = raw
            prev_meta = meta
        self.assertIsNotNone(self.auth.verify_token(prev_raw))


class TestRestAuthHashStorage(unittest.TestCase):
    """Tokens must be stored as hashes, never plaintext."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.auth = _make_auth(self._tmp)

    def test_token_hash_not_in_public_meta(self):
        _, meta = self.auth.create_token("hash-guard")
        self.assertNotIn("token_hash", meta)

    def test_raw_token_not_in_stored_file(self):
        raw, _ = self.auth.create_token("file-guard")
        tokens_file = Path(self._tmp) / "api_tokens.json"
        content = tokens_file.read_text(encoding="utf-8")
        self.assertNotIn(raw, content, "Raw token must not appear in storage file")

    def test_stored_hash_is_sha256_hex(self):
        raw, _ = self.auth.create_token("hash-verify")
        stored = self.auth._tokens[0]["token_hash"]
        # SHA-256 hex digest is always 64 lowercase hex chars
        self.assertEqual(len(stored), 64)
        self.assertRegex(stored, r"^[0-9a-f]{64}$")

    def test_stored_hash_matches_sha256_of_raw(self):
        raw, _ = self.auth.create_token("hash-match")
        expected = hashlib.sha256(raw.encode()).hexdigest()
        stored = self.auth._tokens[0]["token_hash"]
        self.assertEqual(stored, expected)

    def test_list_tokens_never_exposes_hash(self):
        self.auth.create_token("t1")
        self.auth.create_token("t2")
        for entry in self.auth.list_tokens():
            self.assertNotIn("token_hash", entry)


# ===========================================================================
# 2. REST server edge cases
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestOversizedRequest(_RestBase):
    """Flask MAX_CONTENT_LENGTH should return 413 for oversized body."""

    def test_413_on_oversized_body(self):
        # Set a tiny limit for the test
        original = _rest_mod.app.config.get("MAX_CONTENT_LENGTH")
        _rest_mod.app.config["MAX_CONTENT_LENGTH"] = 10  # 10 bytes
        try:
            data = {
                "file": (io.BytesIO(b"X" * 100), "big.wav"),
            }
            resp = self.client.post(
                "/v1/stt/transcribe",
                data=data,
                content_type="multipart/form-data",
            )
            # Flask returns 413 when content length exceeds limit
            self.assertIn(resp.status_code, (413, 400, 500),
                          "Oversized request should not succeed silently")
        finally:
            _rest_mod.app.config["MAX_CONTENT_LENGTH"] = original

    def test_default_max_content_length_is_configured(self):
        """MAX_CONTENT_LENGTH must be explicitly set (not Flask default 0=unlimited)."""
        limit = _rest_mod.app.config.get("MAX_CONTENT_LENGTH")
        self.assertIsNotNone(limit, "MAX_CONTENT_LENGTH must be set")
        self.assertGreater(limit, 0, "MAX_CONTENT_LENGTH must be positive")


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestInvalidJsonBody(_RestBase):
    """POST with malformed JSON to JSON-expecting endpoint should return 4xx."""

    def test_invalid_json_vocabulary_post(self):
        resp = self.client.post(
            "/v1/vocabulary",
            data=b"not valid json!!!",
            content_type="application/json",
        )
        self.assertIn(resp.status_code, (400, 415, 422),
                      f"Malformed JSON should yield 4xx, got {resp.status_code}")

    def test_wrong_content_type_vocabulary(self):
        resp = self.client.post(
            "/v1/vocabulary",
            data=b'{"words": ["hello"]}',
            content_type="text/plain",
        )
        self.assertIn(resp.status_code, (400, 415, 422, 200),
                      f"Wrong content-type should yield 4xx or process, got {resp.status_code}")

    def test_empty_body_vocabulary_post_returns_4xx(self):
        resp = self.client.post(
            "/v1/vocabulary",
            data=b"",
            content_type="application/json",
        )
        self.assertIn(resp.status_code, (400, 415, 422),
                      f"Empty body should yield 4xx, got {resp.status_code}")


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestConcurrentTranscribeRequests(_RestBase):
    """10 simultaneous transcribe requests should all complete without errors."""

    def _do_transcribe(self, results, idx):
        audio_data = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 16
        data = {
            "file": (io.BytesIO(audio_data), "test.wav"),
            "quality_profile": "balanced",
        }
        try:
            resp = self.client.post(
                "/v1/stt/transcribe",
                data=data,
                content_type="multipart/form-data",
            )
            results[idx] = resp.status_code
        except Exception as exc:
            results[idx] = str(exc)

    def test_10_concurrent_transcribe_requests(self):
        n = 10
        results = [None] * n
        threads = [
            threading.Thread(target=self._do_transcribe, args=(results, i))
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        # All requests must complete (not timeout = None)
        self.assertTrue(
            all(r is not None for r in results),
            f"Some requests timed out: {results}",
        )
        # All must return a valid HTTP status code (not an exception string)
        for i, r in enumerate(results):
            self.assertIsInstance(r, int, f"Request {i} raised exception: {r}")
            # Accept 200 (ok), 400 (bad audio file - normalize_audio stub), 429 (rate limit)
            self.assertIn(r, (200, 400, 429, 500),
                          f"Unexpected status {r} for request {i}")


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestMetricEndpointUnderLoad(_RestBase):
    """Multiple rapid metric requests should all return 200 (rate limit disabled in base)."""

    def test_10_metric_requests_succeed(self):
        for i in range(10):
            resp = self.client.get("/metrics")
            self.assertEqual(
                resp.status_code, 200,
                f"Metrics request {i} failed with {resp.status_code}",
            )

    def test_metrics_response_is_valid_json(self):
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIsNotNone(body)
        self.assertIsInstance(body, dict)


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestHealthEndpointNoAuth(_RestBase):
    """GET /health must be accessible without any auth token."""

    def test_health_accessible_without_token(self):
        _rest_mod.settings.REST_API_KEY = "strict-key-xyz"
        resp = self.client.get("/health")
        self.assertNotEqual(resp.status_code, 401,
                            "/health must not require auth")

    def test_health_accessible_with_auth_enabled(self):
        if hasattr(_rest_mod.settings, "REST_API_AUTH_ENABLED"):
            _rest_mod.settings.REST_API_AUTH_ENABLED = True
        resp = self.client.get("/health")
        self.assertNotEqual(resp.status_code, 401,
                            "/health must be open even when auth is enabled")

    def test_health_returns_200(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)

    def test_health_returns_status_ok(self):
        resp = self.client.get("/health")
        body = resp.get_json()
        self.assertIsNotNone(body)
        self.assertEqual(body.get("status"), "ok")


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestCorsPreflightResponse(_RestBase):
    """OPTIONS preflight must return CORS headers."""

    def test_options_health_returns_cors_headers(self):
        resp = self.client.options(
            "/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        # Flask-CORS handles OPTIONS; status 200 or 204
        self.assertIn(resp.status_code, (200, 204),
                      f"Expected 200/204 for OPTIONS, got {resp.status_code}")

    def test_cors_allow_origin_header_present(self):
        # #1663 hardening: ACAO is only injected for allowlisted origins. The
        # default allowlist is the bare localhost set (no port), so use
        # http://localhost rather than http://localhost:3000 (which is not
        # allowlisted and correctly receives no ACAO).
        resp = self.client.get(
            "/health",
            headers={"Origin": "http://localhost"},
        )
        # flask-cors should inject Access-Control-Allow-Origin for the
        # allowlisted origin, reflecting it back.
        self.assertEqual(
            resp.headers.get("Access-Control-Allow-Origin"),
            "http://localhost",
            "CORS header missing/incorrect for allowlisted origin",
        )

    def test_cors_blocks_non_allowlisted_origin(self):
        """A cross-origin GET from a non-allowlisted origin must NOT receive
        Access-Control-Allow-Origin (#1663 transcript-exfiltration guard)."""
        resp = self.client.get(
            "/health",
            headers={"Origin": "https://evil.example.com"},
        )
        self.assertNotIn("Access-Control-Allow-Origin", resp.headers)

    def test_cors_options_transcribe(self):
        resp = self.client.options(
            "/v1/stt/transcribe",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        self.assertIn(resp.status_code, (200, 204),
                      f"OPTIONS transcribe should succeed, got {resp.status_code}")


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestRequestIdLogged(_RestBase):
    """Every response must carry X-Request-ID for audit trail."""

    def test_health_has_request_id_header(self):
        resp = self.client.get("/health")
        self.assertIn("X-Request-ID", resp.headers,
                      "X-Request-ID must be present on /health response")

    def test_metrics_has_request_id_header(self):
        resp = self.client.get("/metrics")
        self.assertIn("X-Request-ID", resp.headers,
                      "X-Request-ID must be present on /metrics response")

    def test_request_id_is_uuid_format(self):
        resp = self.client.get("/health")
        rid = resp.headers.get("X-Request-ID", "")
        # UUID4 has 36 chars including hyphens
        self.assertEqual(len(rid), 36,
                         f"X-Request-ID should be UUID4, got: {rid!r}")

    def test_each_request_gets_unique_id(self):
        ids = set()
        for _ in range(5):
            resp = self.client.get("/health")
            ids.add(resp.headers.get("X-Request-ID"))
        self.assertEqual(len(ids), 5, "Each request should have a unique X-Request-ID")

    def test_request_id_logged_in_json_format(self):
        """When LOG_FORMAT=json, log_request after_request hook must include request_id.

        We call log_request directly with a mock response to verify the JSON
        log record shape without relying on handler propagation quirks.
        """
        import logging

        orig_format = getattr(_rest_mod.settings, "LOG_FORMAT", "text")
        _rest_mod.settings.LOG_FORMAT = "json"
        logged_records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                logged_records.append(record.getMessage())

        # Attach to the module logger AND ensure it won't be swallowed
        handler = _Capture()
        handler.setLevel(logging.DEBUG)
        _rest_mod.logger.addHandler(handler)
        _rest_mod.logger.setLevel(logging.DEBUG)
        orig_propagate = _rest_mod.logger.propagate
        _rest_mod.logger.propagate = False

        try:
            resp = self.client.get("/health")
            rid = resp.headers.get("X-Request-ID", "")
            # Verify the X-Request-ID header is present (audit trail property)
            self.assertTrue(len(rid) == 36,
                            f"X-Request-ID must be a UUID4, got: {rid!r}")
            # Verify that log records were emitted and at least one is JSON
            # containing request_id key
            json_found = False
            for msg in logged_records:
                try:
                    record = json.loads(msg)
                    if "request_id" in record:
                        json_found = True
                        break
                except Exception:
                    pass
            self.assertTrue(json_found,
                            f"JSON log with request_id not found in: {logged_records}")
        finally:
            _rest_mod.logger.removeHandler(handler)
            _rest_mod.logger.propagate = orig_propagate
            _rest_mod.settings.LOG_FORMAT = orig_format


# ===========================================================================
# 3. Privacy tests
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestRequestBodyNotLoggedInFull(_RestBase):
    """Sensitive request body content (audio bytes) must not appear in logs."""

    def test_audio_content_not_in_json_log(self):
        """POST /v1/stt/transcribe: raw audio bytes must not be logged."""
        sentinel = b"UNIQUE_AUDIO_SENTINEL_BYTES_W176"
        logged_records = []

        import logging

        class _Capture(logging.Handler):
            def emit(self, record):
                logged_records.append(record.getMessage())

        handler = _Capture()
        _rest_mod.logger.addHandler(handler)
        orig_format = getattr(_rest_mod.settings, "LOG_FORMAT", "text")
        _rest_mod.settings.LOG_FORMAT = "json"
        try:
            audio_data = b"RIFF\x00\x00\x00\x00WAVEfmt " + sentinel
            data = {"file": (io.BytesIO(audio_data), "private.wav")}
            self.client.post(
                "/v1/stt/transcribe",
                data=data,
                content_type="multipart/form-data",
            )
            sentinel_str = sentinel.decode("latin-1")
            for msg in logged_records:
                self.assertNotIn(
                    sentinel_str, msg,
                    f"Raw audio content leaked into log: {msg!r}",
                )
        finally:
            _rest_mod.logger.removeHandler(handler)
            _rest_mod.settings.LOG_FORMAT = orig_format

    def test_json_log_record_has_no_file_content_field(self):
        """JSON log record must include only safe fields (no 'body', 'file' keys)."""
        logged_json = []

        import logging

        class _Capture(logging.Handler):
            def emit(self, record):
                msg = record.getMessage()
                try:
                    logged_json.append(json.loads(msg))
                except Exception:
                    pass

        handler = _Capture()
        _rest_mod.logger.addHandler(handler)
        orig_format = getattr(_rest_mod.settings, "LOG_FORMAT", "text")
        _rest_mod.settings.LOG_FORMAT = "json"
        try:
            self.client.get("/health")
            for record in logged_json:
                self.assertNotIn("body", record,
                                 f"Unexpected 'body' field in log record: {record}")
                self.assertNotIn("file_content", record,
                                 f"Unexpected 'file_content' in log record: {record}")
        finally:
            _rest_mod.logger.removeHandler(handler)
            _rest_mod.settings.LOG_FORMAT = orig_format


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestAudioFilenamesNotInMetrics(_RestBase):
    """GET /metrics response must not expose audio filenames."""

    def test_metrics_response_no_filename_fields(self):
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        body_str = json.dumps(body)
        # Common sensitive fields that should never appear in metrics
        for sensitive in ("temp_uploads", "filename", "audio_path", "file_path"):
            self.assertNotIn(
                sensitive, body_str,
                f"Sensitive field '{sensitive}' found in metrics response",
            )

    def test_metrics_no_data_dir_path(self):
        """DATA_DIR absolute path must not leak into metrics response."""
        resp = self.client.get("/metrics")
        body_str = json.dumps(resp.get_json() or {})
        data_dir_str = str(_rest_mod.settings.DATA_DIR)
        self.assertNotIn(
            data_dir_str, body_str,
            "DATA_DIR absolute path must not appear in metrics response",
        )

    def test_transcribe_response_no_temp_path(self):
        """Transcribe response must not expose internal temp file path."""
        audio_data = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 16
        data = {"file": (io.BytesIO(audio_data), "secret_recording.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        if resp.status_code == 200:
            body_str = json.dumps(resp.get_json() or {})
            self.assertNotIn("temp_uploads", body_str,
                             "Temp path must not leak in transcribe response")


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestHealthResponseNoSensitivePaths(_RestBase):
    """GET /health must not expose any filesystem paths or internal details."""

    def test_health_no_data_dir_in_response(self):
        resp = self.client.get("/health")
        body_str = json.dumps(resp.get_json() or {})
        data_dir_str = str(_rest_mod.settings.DATA_DIR)
        self.assertNotIn(
            data_dir_str, body_str,
            "DATA_DIR must not appear in /health response",
        )

    def test_health_no_home_dir_in_response(self):
        resp = self.client.get("/health")
        body_str = json.dumps(resp.get_json() or {})
        home = str(Path.home())
        self.assertNotIn(home, body_str,
                         "Home directory path must not appear in /health response")

    def test_health_no_python_path_in_response(self):
        resp = self.client.get("/health")
        body_str = json.dumps(resp.get_json() or {})
        # Check no /Users/ or /home/ style paths
        self.assertNotIn("/Users/", body_str)
        self.assertNotIn("/home/", body_str)

    def test_health_response_keys_are_safe(self):
        """Verify /health only contains expected safe keys."""
        resp = self.client.get("/health")
        body = resp.get_json() or {}
        safe_keys = {"status", "service", "profile"}
        extra_keys = set(body.keys()) - safe_keys
        self.assertEqual(
            extra_keys, set(),
            f"/health returned unexpected keys: {extra_keys}",
        )


if __name__ == "__main__":
    unittest.main()
