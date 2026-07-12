"""Wave-31 REST hardening tests.

Covers two MED fixes:

  H1 — rest_server.py: POST /v1/vocabulary 512 KB pre-cap
    A 513 KB POST body is rejected with 413 BEFORE flask-smorest's
    @v1_blp.arguments() deserialises the JSON, preventing a 500 MB
    allocation before the 500-word / 100-char cap validation runs.

  H2 — rest_auth.py: verify_token() lazy last_used write
    When verify_token() is called 10 times within 1 second, _save()
    (which holds flock(LOCK_EX) on every call) is invoked at most once —
    the lazy-write defers the disk flush until >60 s have elapsed.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_rest_wave31_hardening.py -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# RestAuth import (no Flask needed)
# ---------------------------------------------------------------------------

from backend.rest_auth import RestAuth  # noqa: E402


def _make_auth(tmp_dir: str) -> RestAuth:
    return RestAuth(data_dir=tmp_dir)


# ---------------------------------------------------------------------------
# Lazy REST server import with stubs (same pattern as test_rest_hardening.py)
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
    (red CI 2026-07-12 — chunk-pollution class, see CLAUDE.md).
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
                "add_history_item": lambda self, **kw: MagicMock(id="hist-w31"),
            }),
        },
        "backend.transcriber": {
            "Transcriber": type("_FT", (), {
                "__init__": lambda self, *a, **k: None,
                "transcribe": lambda self, *a, **kw: {
                    "text": "test",
                    "raw_text": "test",
                    "confidence": 0.9,
                    "duration_ms": 300,
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
    # Снимаем ВСТАВЛЕННЫЕ НАМИ фейки из sys.modules: rest_server уже связал
    # свои top-level ссылки на них (module-level `from backend.state_store
    # import StateStore` и т.п. в rest_server.py), а соседи по chunk-процессу
    # должны получать НАСТОЯЩИЕ backend.service/state_store/... — иначе
    # фейк _FBS/_FSS отравляет все последующие тест-файлы чанка (красный
    # CI 2026-07-12: test_search_by_speaker.py / test_send_imessage.py /
    # test_rsf_silence_ranges_wiring_W1139.py получали `_FSS`/`_FBS`
    # вместо реальных StateStore/BackendService).
    for _name in _inserted_stub_modules:
        sys.modules.pop(_name, None)


def _make_client():
    app = _rest_mod.app
    app.config["TESTING"] = True
    return app.test_client()


class _RestBase(unittest.TestCase):
    """Base class: patches runtime singletons, disables rate limiting + auth."""

    def setUp(self):
        self.mock_store = MagicMock()
        self.mock_store.load_vocabulary.return_value = []
        self.mock_store.is_idempotent.return_value = False
        self.mock_store.add_history_item.return_value = MagicMock(id="hist-w31")
        self.mock_store.load_settings.return_value = {}

        self.mock_transcriber = MagicMock()
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
# H1 — POST /v1/vocabulary 512 KB pre-cap
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestVocabularyPreCap(_RestBase):
    """POST /v1/vocabulary: body > 512 KB must return 413 BEFORE JSON parse."""

    def test_513kb_body_returns_413(self):
        """A 513 KB Content-Length triggers 413 before any JSON materialisation."""
        body = b"x" * (513 * 1024)
        resp = self.client.post(
            "/v1/vocabulary",
            data=body,
            content_type="application/json",
        )
        self.assertEqual(
            resp.status_code, 413,
            f"Expected 413 for 513 KB vocabulary POST, got {resp.status_code}",
        )

    def test_413_response_is_json(self):
        """The 413 response body must be machine-readable JSON."""
        body = b"x" * (513 * 1024)
        resp = self.client.post(
            "/v1/vocabulary",
            data=body,
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 413)
        data = resp.get_json()
        self.assertIsNotNone(data, "413 body must be valid JSON")
        self.assertIn("error", data, "413 JSON must have an 'error' key")

    def test_512kb_body_not_rejected_by_pre_cap(self):
        """A body exactly at the 512 KB limit is NOT rejected by the pre-cap check.

        (It may still fail JSON parse if the content is not valid JSON, but
        the pre-cap guard itself must not block it.)
        """
        # Build exactly 512 KB of valid-ish JSON content
        body = b"x" * (512 * 1024)
        resp = self.client.post(
            "/v1/vocabulary",
            data=body,
            content_type="application/json",
        )
        # 413 from the pre-cap check is wrong; 400/422 from JSON parse is fine
        self.assertNotEqual(
            resp.status_code, 413,
            "Exactly 512 KB body must not be rejected by the vocabulary pre-cap",
        )

    def test_small_valid_vocabulary_accepted(self):
        """A small well-formed vocabulary POST must succeed (200)."""
        payload = json.dumps({"words": ["hello", "world"]}).encode()
        self.mock_store.load_vocabulary.return_value = []
        resp = self.client.post(
            "/v1/vocabulary",
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(
            resp.status_code, 200,
            f"Small vocabulary POST must succeed, got {resp.status_code}",
        )

    def test_pre_cap_constant_is_512kb(self):
        """_VOCABULARY_POST_MAX_BYTES must be 512 * 1024 bytes."""
        self.assertEqual(
            _rest_mod._VOCABULARY_POST_MAX_BYTES,
            512 * 1024,
            "_VOCABULARY_POST_MAX_BYTES must equal 512 KB",
        )

    def test_other_endpoints_not_affected_by_vocabulary_precap(self):
        """The pre-cap hook must only fire on POST /v1/vocabulary, not on GET."""
        resp = self.client.get("/v1/vocabulary")
        # GET is allowed regardless of the vocabulary pre-cap
        self.assertNotEqual(
            resp.status_code, 413,
            "GET /v1/vocabulary must not be blocked by the POST pre-cap",
        )


# ===========================================================================
# H2 — verify_token() lazy last_used write
# ===========================================================================

class TestVerifyTokenLazyWrite(unittest.TestCase):
    """verify_token() called N times within 1 second must call _save() at most once."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.auth = _make_auth(self._tmp)

    def test_10_calls_within_1s_saves_at_most_once(self):
        """10 rapid verify_token() calls must trigger _save() at most 1 time.

        The lazy-write interval is 60 s.  All 10 calls arrive within <1 s,
        so only the first call (last_used=None → delta=+inf > 60) flushes.
        Subsequent calls see a recent last_used timestamp → no flush.
        """
        raw, _ = self.auth.create_token("lazy-write-test")

        save_calls = []
        original_save = self.auth._save

        def counting_save(tokens):
            save_calls.append(time.time())
            original_save(tokens)

        self.auth._save = counting_save

        for _ in range(10):
            result = self.auth.verify_token(raw)
            self.assertIsNotNone(result, "verify_token must succeed")

        # The first call flushes (last_used was None → delta is large).
        # All subsequent calls within the same second must be deferred.
        self.assertLessEqual(
            len(save_calls), 1,
            f"_save() called {len(save_calls)} times for 10 rapid verify_token() "
            f"calls — expected at most 1 (lazy-write deferred interval is 60 s)",
        )

    def test_verify_still_returns_meta_even_when_save_deferred(self):
        """Even when _save() is deferred, verify_token must return valid meta."""
        raw, _ = self.auth.create_token("deferred-meta")
        # First call
        meta1 = self.auth.verify_token(raw)
        self.assertIsNotNone(meta1)
        # Rapid second call — _save() should be deferred but meta still returned
        meta2 = self.auth.verify_token(raw)
        self.assertIsNotNone(meta2)
        self.assertEqual(meta1["id"], meta2["id"])

    def test_in_memory_last_used_updated_even_when_save_deferred(self):
        """last_used must be updated in-memory on every call, even if not flushed."""
        raw, _ = self.auth.create_token("in-mem-update")
        # Suppress disk writes entirely to isolate in-memory behaviour
        self.auth._save = lambda _tokens: None

        self.auth.verify_token(raw)
        lu1 = self.auth._tokens[0].get("last_used")
        self.assertIsNotNone(lu1, "last_used must be set in-memory after first verify")

        # Brief pause so the second timestamp is distinguishable
        time.sleep(0.01)
        self.auth.verify_token(raw)
        lu2 = self.auth._tokens[0].get("last_used")
        self.assertIsNotNone(lu2, "last_used must be updated on second verify")

    def test_save_called_after_interval_elapsed(self):
        """When last_used is older than 60 s, the next verify must flush to disk."""
        raw, _ = self.auth.create_token("flush-after-interval")

        save_calls = []
        original_save = self.auth._save

        def counting_save(tokens):
            save_calls.append(time.time())
            original_save(tokens)

        self.auth._save = counting_save

        # Manually backdate last_used to simulate 61 seconds ago
        from datetime import timedelta
        stale_ts = (
            datetime.now(timezone.utc) - timedelta(seconds=61)
        ).isoformat()
        self.auth._tokens[0]["last_used"] = stale_ts

        result = self.auth.verify_token(raw)
        self.assertIsNotNone(result)
        self.assertEqual(
            len(save_calls), 1,
            "_save() must be called exactly once when last_used is stale (>60 s)",
        )

    def test_wrong_token_never_calls_save(self):
        """A failed verification must never call _save()."""
        self.auth.create_token("no-save-on-fail")

        save_calls = []
        original_save = self.auth._save

        def counting_save(tokens):
            save_calls.append(time.time())
            original_save(tokens)

        self.auth._save = counting_save
        result = self.auth.verify_token("definitely_wrong_token_abc")
        self.assertIsNone(result)
        self.assertEqual(len(save_calls), 0, "_save() must not be called on failed verify")

    def test_lazy_write_interval_is_60_seconds(self):
        """_LAST_USED_WRITE_INTERVAL_SEC must be 60."""
        self.assertEqual(
            RestAuth._LAST_USED_WRITE_INTERVAL_SEC,
            60,
            "_LAST_USED_WRITE_INTERVAL_SEC must be 60 seconds",
        )


# ---------------------------------------------------------------------------
# Import guard
# ---------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402  (used in test above)

if __name__ == "__main__":
    unittest.main()
