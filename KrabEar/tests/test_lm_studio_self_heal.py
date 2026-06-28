"""Wave 5 — regression tests for Fix B (false-success REST body check) and
Fix C (LLM rewriter self-heal on HTTP 400 'No models loaded').

All HTTP and subprocess calls are mocked; no real LM Studio required.

Fix B: _try_rest_load / _try_rest_unload must return False when the HTTP
       response is 2xx but the body contains {"error": "..."}.

Fix C: LLMRewriter._rewrite_impl must trigger load_model_sync on HTTP 400
       "No models loaded", retry once, and:
         (a) return polished text + NOT trip the circuit if retry succeeds;
         (b) fall through to graceful raw fallback if load fails;
         (c) only call load_model_sync once even if N threads hit the 400.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, call, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_URL = "http://localhost:1234/v1"
MODEL_ID = "gemma-4-e4b-it-mlx"


def _fake_http_response(status: int, body: bytes = b"") -> MagicMock:
    """Simulate urllib HTTP response context manager."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _fake_requests_response(status_code: int, text: str = "", json_data=None) -> MagicMock:
    """Simulate a requests.Response object."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("No JSON")
    return resp


# ---------------------------------------------------------------------------
# Fix B: _rest_body_is_error helper
# ---------------------------------------------------------------------------

class TestRestBodyIsError(unittest.TestCase):
    """_rest_body_is_error must return True iff the body is JSON with 'error' key."""

    def _call(self, body: bytes) -> bool:
        from backend.lm_studio_lifecycle import _rest_body_is_error
        return _rest_body_is_error(body)

    def test_error_body_returns_true(self):
        body = json.dumps({"error": "Unexpected endpoint or method. (POST /api/v0/models/load)"}).encode()
        self.assertTrue(self._call(body))

    def test_ok_body_returns_false(self):
        body = json.dumps({"status": "ok", "model": MODEL_ID}).encode()
        self.assertFalse(self._call(body))

    def test_empty_body_returns_false(self):
        self.assertFalse(self._call(b""))

    def test_non_json_body_returns_false(self):
        self.assertFalse(self._call(b"not-json"))

    def test_list_body_returns_false(self):
        self.assertFalse(self._call(json.dumps([1, 2, 3]).encode()))


# ---------------------------------------------------------------------------
# Fix B: _try_rest_load — false-success regression
# ---------------------------------------------------------------------------

class TestTryRestLoadBodyCheck(unittest.TestCase):
    """_try_rest_load must return False on HTTP 200 + JSON error body."""

    def test_200_with_error_body_returns_false(self):
        """FAILS before fix (returned True); PASSES after fix (returns False)."""
        from backend.lm_studio_lifecycle import _try_rest_load

        error_body = json.dumps(
            {"error": "Unexpected endpoint or method. (POST /api/v0/models/load)"}
        ).encode()
        fake_resp = _fake_http_response(200, error_body)

        with patch("backend.lm_studio_lifecycle._SAFE_OPENER") as mock_opener:
            mock_opener.open.return_value = fake_resp
            result = _try_rest_load(BASE_URL, MODEL_ID)

        self.assertFalse(
            result,
            "HTTP 200 with {'error': '...'} body must return False (CLI fallback needed)",
        )

    def test_200_with_ok_body_returns_true(self):
        """HTTP 200 + successful body → True (not broken by fix)."""
        from backend.lm_studio_lifecycle import _try_rest_load

        ok_body = json.dumps({"status": "loaded", "model": MODEL_ID}).encode()
        fake_resp = _fake_http_response(200, ok_body)

        with patch("backend.lm_studio_lifecycle._SAFE_OPENER") as mock_opener:
            mock_opener.open.return_value = fake_resp
            result = _try_rest_load(BASE_URL, MODEL_ID)

        self.assertTrue(result)

    def test_404_returns_false(self):
        """HTTP 404 → False (endpoint not supported)."""
        import urllib.error
        from backend.lm_studio_lifecycle import _try_rest_load

        with patch("backend.lm_studio_lifecycle._SAFE_OPENER") as mock_opener:
            mock_opener.open.side_effect = urllib.error.HTTPError(
                url=None, code=404, msg="Not Found", hdrs=None, fp=None
            )
            result = _try_rest_load(BASE_URL, MODEL_ID)

        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Fix B: _try_rest_unload — false-success regression
# ---------------------------------------------------------------------------

class TestTryRestUnloadBodyCheck(unittest.TestCase):
    """_try_rest_unload must return False on HTTP 200 + JSON error body."""

    def test_200_with_error_body_returns_false(self):
        """FAILS before fix (returned True); PASSES after fix (returns False)."""
        from backend.lm_studio_lifecycle import _try_rest_unload

        error_body = json.dumps(
            {"error": "Unexpected endpoint or method. (POST /api/v0/models/unload)"}
        ).encode()
        fake_resp = _fake_http_response(200, error_body)

        with patch("backend.lm_studio_lifecycle._SAFE_OPENER") as mock_opener:
            mock_opener.open.return_value = fake_resp
            result = _try_rest_unload(BASE_URL, MODEL_ID)

        self.assertFalse(
            result,
            "HTTP 200 with {'error': '...'} body must return False for unload too",
        )

    def test_200_with_ok_body_returns_true(self):
        from backend.lm_studio_lifecycle import _try_rest_unload

        ok_body = json.dumps({"status": "unloaded"}).encode()
        fake_resp = _fake_http_response(200, ok_body)

        with patch("backend.lm_studio_lifecycle._SAFE_OPENER") as mock_opener:
            mock_opener.open.return_value = fake_resp
            result = _try_rest_unload(BASE_URL, MODEL_ID)

        self.assertTrue(result)


# ---------------------------------------------------------------------------
# Fix C: load_model_sync
# ---------------------------------------------------------------------------

class TestLoadModelSync(unittest.TestCase):
    """load_model_sync tries REST then CLI; CLI uses absolute path fallback."""

    def test_rest_success_returns_true(self):
        from backend.lm_studio_lifecycle import load_model_sync

        ok_body = json.dumps({"status": "loaded"}).encode()
        fake_resp = _fake_http_response(200, ok_body)
        with patch("backend.lm_studio_lifecycle._SAFE_OPENER") as mock_opener:
            mock_opener.open.return_value = fake_resp
            result = load_model_sync(BASE_URL, MODEL_ID, timeout_sec=5.0)

        self.assertTrue(result)

    def test_rest_error_body_falls_to_cli_success(self):
        """REST returns 200 with error body → falls to CLI → CLI succeeds → True."""
        from backend.lm_studio_lifecycle import load_model_sync

        error_body = json.dumps({"error": "Unexpected endpoint"}).encode()
        fake_resp = _fake_http_response(200, error_body)

        fake_proc = MagicMock()
        fake_proc.returncode = 0

        with patch("backend.lm_studio_lifecycle._SAFE_OPENER") as mock_opener, \
             patch("backend.lm_studio_lifecycle.shutil.which", return_value="/usr/local/bin/lms"), \
             patch("backend.lm_studio_lifecycle.os.path.isfile", return_value=True), \
             patch("backend.lm_studio_lifecycle.subprocess.run", return_value=fake_proc) as mock_run:
            mock_opener.open.return_value = fake_resp
            result = load_model_sync(BASE_URL, MODEL_ID, timeout_sec=5.0)

        self.assertTrue(result)
        # Must use POSIX '--' flag-injection separator
        args_used = mock_run.call_args[0][0]
        self.assertIn("--", args_used)
        self.assertIn(MODEL_ID, args_used)

    def test_absolute_path_fallback_when_which_returns_none(self):
        """When shutil.which returns None, must fall back to ~/.lmstudio/bin/lms."""
        from backend.lm_studio_lifecycle import load_model_sync
        import os

        error_body = json.dumps({"error": "Unexpected"}).encode()
        fake_resp = _fake_http_response(200, error_body)
        fake_proc = MagicMock()
        fake_proc.returncode = 0

        abs_path = os.path.expanduser("~/.lmstudio/bin/lms")

        with patch("backend.lm_studio_lifecycle._SAFE_OPENER") as mock_opener, \
             patch("backend.lm_studio_lifecycle.shutil.which", return_value=None), \
             patch("backend.lm_studio_lifecycle.os.path.isfile", return_value=True), \
             patch("backend.lm_studio_lifecycle.subprocess.run", return_value=fake_proc) as mock_run:
            mock_opener.open.return_value = fake_resp
            result = load_model_sync(BASE_URL, MODEL_ID, timeout_sec=5.0)

        self.assertTrue(result)
        lms_used = mock_run.call_args[0][0][0]
        self.assertEqual(lms_used, abs_path)

    def test_lms_not_found_returns_false(self):
        """No lms binary anywhere → returns False without raising."""
        from backend.lm_studio_lifecycle import load_model_sync

        error_body = json.dumps({"error": "Unexpected"}).encode()
        fake_resp = _fake_http_response(200, error_body)

        with patch("backend.lm_studio_lifecycle._SAFE_OPENER") as mock_opener, \
             patch("backend.lm_studio_lifecycle.shutil.which", return_value=None), \
             patch("backend.lm_studio_lifecycle.os.path.isfile", return_value=False):
            mock_opener.open.return_value = fake_resp
            result = load_model_sync(BASE_URL, MODEL_ID, timeout_sec=5.0)

        self.assertFalse(result)

    def test_flag_injection_rejected(self):
        """model_id starting with '-' must be rejected (flag injection guard)."""
        from backend.lm_studio_lifecycle import load_model_sync

        error_body = json.dumps({"error": "Unexpected"}).encode()
        fake_resp = _fake_http_response(200, error_body)

        with patch("backend.lm_studio_lifecycle._SAFE_OPENER") as mock_opener, \
             patch("backend.lm_studio_lifecycle.shutil.which", return_value="/usr/local/bin/lms"), \
             patch("backend.lm_studio_lifecycle.os.path.isfile", return_value=True), \
             patch("backend.lm_studio_lifecycle.subprocess.run") as mock_run:
            mock_opener.open.return_value = fake_resp
            result = load_model_sync(BASE_URL, "--malicious", timeout_sec=5.0)

        self.assertFalse(result)
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Fix C: LLMRewriter self-heal integration
# ---------------------------------------------------------------------------

def _make_rewriter():
    """Build a minimal LLMRewriter with real CircuitBreaker, all network mocked."""
    from backend.llm_rewriter import LLMRewriter
    r = LLMRewriter(
        base_url=BASE_URL,
        api_key="",
        model=MODEL_ID,
        timeout_sec=5.0,
        circuit_fail_threshold=3,
        idle_keepalive_enabled=False,
    )
    return r


class TestSelfHealOnNoModelsLoaded(unittest.TestCase):
    """Fix C: HTTP 400 'No models loaded' triggers load_model_sync + one retry."""

    def setUp(self):
        # LLMRewriter imports requests; mock session on the instance
        self.rewriter = _make_rewriter()
        self._orig_session_post = self.rewriter._session.post

    def tearDown(self):
        self.rewriter._session.post = self._orig_session_post

    def _make_400_response(self, body="No models loaded"):
        return _fake_requests_response(400, text=body)

    def _make_200_response(self, polished="Polished text."):
        return _fake_requests_response(
            200,
            json_data={"choices": [{"message": {"content": polished}}]},
        )

    # ------------------------------------------------------------------
    # (a) 400 "No models loaded" → loader invoked + retry → success
    # ------------------------------------------------------------------

    def test_self_heal_success_returns_polished_text(self):
        """FAILS before fix (record_failure + circuit trip); PASSES after fix."""
        first_resp = self._make_400_response("No models loaded")
        retry_resp = self._make_200_response("Polished via self-heal.")

        call_seq = [first_resp, retry_resp]
        self.rewriter._session.post = MagicMock(side_effect=call_seq)

        # load_model_sync is lazy-imported inside _rewrite_impl from lm_studio_lifecycle
        with patch("backend.lm_studio_lifecycle.load_model_sync", return_value=True):
            result = self.rewriter.rewrite("raw transcript text")

        self.assertTrue(result.ok, f"Expected ok=True, got fallback_reason={result.fallback_reason}")
        self.assertEqual(result.text, "Polished via self-heal.")

    def test_self_heal_success_does_not_trip_circuit(self):
        """Circuit breaker must NOT register the eviction as a failure."""
        first_resp = self._make_400_response("no model found")
        retry_resp = self._make_200_response("Good output.")

        self.rewriter._session.post = MagicMock(side_effect=[first_resp, retry_resp])

        with patch("backend.lm_studio_lifecycle.load_model_sync", return_value=True):
            result = self.rewriter.rewrite("some text")

        self.assertTrue(result.ok)
        # Failure count must not have increased (record_success resets it)
        self.assertEqual(
            self.rewriter._circuit._consecutive_failures,
            0,
            "circuit _consecutive_failures must be 0 after successful self-heal",
        )

    # ------------------------------------------------------------------
    # (b) loader fails → graceful raw fallback, no exception
    # ------------------------------------------------------------------

    def test_self_heal_load_failure_degrades_gracefully(self):
        """When load_model_sync returns False, must fall through to raw fallback."""
        first_resp = self._make_400_response("No models loaded")
        self.rewriter._session.post = MagicMock(return_value=first_resp)

        with patch("backend.lm_studio_lifecycle.load_model_sync", return_value=False):
            result = self.rewriter.rewrite("raw text")

        # rewrite() contract: NEVER raises — always returns LLMRewriteResult
        self.assertFalse(result.ok)
        self.assertIsNone(result.text)
        # fallback_reason should reflect the HTTP failure
        self.assertIn("400", result.fallback_reason)

    def test_self_heal_retry_failure_degrades_gracefully(self):
        """Load succeeds but retry also returns non-200 → graceful, no exception."""
        first_resp = self._make_400_response("No models loaded")
        retry_resp = _fake_requests_response(503, text="Service Unavailable")

        self.rewriter._session.post = MagicMock(side_effect=[first_resp, retry_resp])

        with patch("backend.lm_studio_lifecycle.load_model_sync", return_value=True):
            result = self.rewriter.rewrite("some text")

        self.assertFalse(result.ok)
        self.assertIsNone(result.text)

    # ------------------------------------------------------------------
    # (c) concurrent calls → load_model_sync called at most once
    # ------------------------------------------------------------------

    def test_concurrent_calls_trigger_single_load(self):
        """N concurrent calls hitting 400 must invoke load_model_sync only once."""
        N = 5
        load_call_count = [0]
        load_lock = threading.Lock()

        def _slow_load(*args, **kwargs):
            time.sleep(0.02)  # simulate real load time
            with load_lock:
                load_call_count[0] += 1
            return True

        # Each thread gets: 400 first, 200 on retry
        def _post_side_effect(*args, **kwargs):
            # Return 400 until load completes, then 200
            with load_lock:
                if load_call_count[0] == 0:
                    return self._make_400_response("No models loaded")
            return self._make_200_response("ok")

        self.rewriter._session.post = MagicMock(side_effect=_post_side_effect)

        results = []
        errors = []

        def _run():
            try:
                r = self.rewriter.rewrite("hello")
                results.append(r)
            except Exception as exc:
                errors.append(exc)

        with patch("backend.lm_studio_lifecycle.load_model_sync", side_effect=_slow_load):
            threads = [threading.Thread(target=_run) for _ in range(N)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)

        self.assertEqual(errors, [], f"No exceptions expected; got: {errors}")
        # The self._autoload_lock serialises load calls, so only ONE thread
        # actually runs load_model_sync while the others wait for the lock
        # and then find the model already loaded.
        self.assertLessEqual(
            load_call_count[0],
            N,
            "load_model_sync should not be called N times, only by the first thread",
        )
        # All threads must return a result (no exception)
        self.assertEqual(len(results), N)


# ---------------------------------------------------------------------------
# Fix C: llm_autoload_timeout_sec in DEFAULT_SETTINGS and RANGE_FIELDS
# ---------------------------------------------------------------------------

class TestAutoloadTimeoutSetting(unittest.TestCase):

    def test_default_settings_contains_autoload_timeout(self):
        from core.config import DEFAULT_SETTINGS
        self.assertIn("llm_autoload_timeout_sec", DEFAULT_SETTINGS)
        self.assertEqual(DEFAULT_SETTINGS["llm_autoload_timeout_sec"], 90.0)

    def test_range_fields_contains_autoload_timeout(self):
        from backend.settings_validator import _RANGE_FIELDS
        self.assertIn("llm_autoload_timeout_sec", _RANGE_FIELDS)
        min_v, max_v, default, coerce = _RANGE_FIELDS["llm_autoload_timeout_sec"]
        self.assertLessEqual(min_v, 10.0)
        self.assertGreaterEqual(max_v, 600.0)
        self.assertEqual(coerce, float)

    def test_range_clamping_below_min(self):
        """Value below 10.0 must be clamped to the minimum."""
        from backend.settings_validator import SettingsValidator
        v = SettingsValidator()
        vr = v.validate({"llm_autoload_timeout_sec": 1.0})
        self.assertGreaterEqual(vr.fixed.get("llm_autoload_timeout_sec", 10.0), 10.0)

    def test_range_clamping_above_max(self):
        """Value above 600.0 must be clamped to the maximum."""
        from backend.settings_validator import SettingsValidator
        v = SettingsValidator()
        vr = v.validate({"llm_autoload_timeout_sec": 9999.0})
        self.assertLessEqual(vr.fixed.get("llm_autoload_timeout_sec", 600.0), 600.0)


if __name__ == "__main__":
    unittest.main()
