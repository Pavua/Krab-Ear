"""Tests for Wave 171 — Metal GPU stream misclassification fix (BACKEND-J).

Covers:
- test_gpu_stream_body_matches_gpu_stream_error_code
- test_metal_command_body_matches_gpu_stream_error_code
- test_timeout_unrelated_body_uses_rewriter_timeout
- test_case_insensitive_matching
- test_empty_body_falls_back_to_timeout
- test_gpu_stream_error_uses_open_lm_studio_settings_action
"""
from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Path setup for standalone execution
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rewriter(model="qwen3-4b-abliterated"):
    """Create an LLMRewriter with error_bus injected and circuit breaker lenient."""
    from backend.llm_rewriter import LLMRewriter
    rw = LLMRewriter(
        base_url="http://localhost:1234/v1",
        api_key="",
        model=model,
        timeout_sec=5.0,
        circuit_fail_threshold=50,   # won't open during tests
        circuit_initial_reset_sec=60,
        circuit_max_reset_sec=600,
    )
    error_bus = MagicMock()
    rw._error_bus = error_bus
    return rw, error_bus


def _mock_response(status_code: int, body: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = body
    return resp


# ---------------------------------------------------------------------------
# 1. HTTP 400 with LM Studio GPU stream body → rewriter.gpu_stream_error
# ---------------------------------------------------------------------------

class TestGpuStreamBodyMatchesGpuStreamErrorCode(unittest.TestCase):
    """HTTP 400 body containing 'There is no Stream(gpu' pushes rewriter.gpu_stream_error."""

    def test_gpu_stream_body_matches_gpu_stream_error_code(self):
        rw, error_bus = _make_rewriter()
        body = (
            '{"error": "RuntimeError: There is no Stream(gpu, 0) in current thread"}'
        )
        resp = _mock_response(400, body)

        with patch.object(rw._session, "post", return_value=resp):
            result = rw.rewrite("Тест текст для металлического GPU-потока")

        self.assertFalse(result.ok)
        self.assertTrue(error_bus.push.called, "error_bus.push должен быть вызван")
        pushed = error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "rewriter.gpu_stream_error")


# ---------------------------------------------------------------------------
# 2. HTTP 400 with "Metal command stream" variant → rewriter.gpu_stream_error
# ---------------------------------------------------------------------------

class TestMetalCommandBodyMatchesGpuStreamErrorCode(unittest.TestCase):
    """'metal command stream' substring in body also triggers rewriter.gpu_stream_error."""

    def test_metal_command_body_matches_gpu_stream_error_code(self):
        rw, error_bus = _make_rewriter()
        body = '{"error": "Metal command stream was interrupted"}'
        resp = _mock_response(400, body)

        with patch.object(rw._session, "post", return_value=resp):
            result = rw.rewrite("Тест текст для команды Metal")

        self.assertFalse(result.ok)
        self.assertTrue(error_bus.push.called)
        pushed = error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "rewriter.gpu_stream_error")


# ---------------------------------------------------------------------------
# 3. HTTP 400 with unrelated body → rewriter.timeout (no regression)
# ---------------------------------------------------------------------------

class TestTimeoutUnrelatedBodyUsesRewriterTimeout(unittest.TestCase):
    """HTTP 400 with an unrelated body still falls through to rewriter.timeout."""

    def test_timeout_unrelated_body_uses_rewriter_timeout(self):
        rw, error_bus = _make_rewriter()
        body = '{"error": "Bad request: missing required field"}'
        resp = _mock_response(400, body)

        with patch.object(rw._session, "post", return_value=resp):
            result = rw.rewrite("Тест текст без связи с GPU потоком")

        self.assertFalse(result.ok)
        self.assertTrue(error_bus.push.called)
        pushed = error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "rewriter.timeout")


# ---------------------------------------------------------------------------
# 4. Case-insensitive matching
# ---------------------------------------------------------------------------

class TestCaseInsensitiveMatching(unittest.TestCase):
    """GPU stream detection is case-insensitive (uppercase, mixed case)."""

    def test_case_insensitive_matching(self):
        rw, error_bus = _make_rewriter()
        # Uppercase variant as might appear in some LM Studio versions
        body = '{"error": "THERE IS NO STREAM(GPU, 1) IN CURRENT THREAD"}'
        resp = _mock_response(400, body)

        with patch.object(rw._session, "post", return_value=resp):
            result = rw.rewrite("Тест регистронезависимого совпадения")

        self.assertFalse(result.ok)
        self.assertTrue(error_bus.push.called)
        pushed = error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "rewriter.gpu_stream_error")


# ---------------------------------------------------------------------------
# 5. Empty body → falls back to rewriter.timeout
# ---------------------------------------------------------------------------

class TestEmptyBodyFallsBackToTimeout(unittest.TestCase):
    """Empty HTTP 400 body has no GPU stream markers — falls back to rewriter.timeout."""

    def test_empty_body_falls_back_to_timeout(self):
        rw, error_bus = _make_rewriter()
        resp = _mock_response(400, "")

        with patch.object(rw._session, "post", return_value=resp):
            result = rw.rewrite("Тест пустого тела ответа")

        self.assertFalse(result.ok)
        self.assertTrue(error_bus.push.called)
        pushed = error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "rewriter.timeout")


# ---------------------------------------------------------------------------
# 6. Error registry: rewriter.gpu_stream_error uses open_lm_studio_settings action
# ---------------------------------------------------------------------------

class TestGpuStreamErrorUsesOpenLmStudioSettingsAction(unittest.TestCase):
    """rewriter.gpu_stream_error registry entry must have correct action_id and severity."""

    def test_gpu_stream_error_uses_open_lm_studio_settings_action(self):
        from backend.error_codes import ERROR_REGISTRY

        entry = ERROR_REGISTRY.get("rewriter.gpu_stream_error")
        self.assertIsNotNone(entry, "rewriter.gpu_stream_error должен быть в ERROR_REGISTRY")
        # wave1233: severity=warn, actionable=False, action_id=None
        self.assertEqual(entry["severity"], "warn")
        self.assertFalse(entry["actionable"])

    def test_gpu_stream_error_dedupe_window_is_300s(self):
        """300 s dedupe prevents Sentry flood from burst of 400s while circuit is opening."""
        from backend.error_codes import ERROR_REGISTRY

        entry = ERROR_REGISTRY["rewriter.gpu_stream_error"]
        self.assertEqual(entry["dedupe_seconds"], 600)  # wave1233 changed to 600


if __name__ == "__main__":
    unittest.main()
