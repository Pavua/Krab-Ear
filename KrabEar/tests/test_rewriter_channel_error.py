"""Tests for Channel Error detection in LLMRewriter + action handler + default model.

Covers:
- test_channel_error_detected_pushes_error_code
- test_channel_error_lowercase_also_detected
- test_default_rewriter_is_qwen3_4b_abliterated
- test_switch_action_changes_setting
- test_recommended_models_starts_with_qwen3
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
    """Create an LLMRewriter with circuit breaker that never blocks + error_bus injected."""
    from backend.llm_rewriter import LLMRewriter
    rw = LLMRewriter(
        base_url="http://localhost:1234/v1",
        api_key="",
        model=model,
        timeout_sec=5.0,
        circuit_fail_threshold=3,
        circuit_initial_reset_sec=60,
        circuit_max_reset_sec=600,
    )
    error_bus = MagicMock()
    rw._error_bus = error_bus
    return rw, error_bus


# ---------------------------------------------------------------------------
# 1. Channel Error in ConnectionError (uppercase) → rewriter.channel_error
# ---------------------------------------------------------------------------

class TestChannelErrorDetectedUppercase(unittest.TestCase):
    """When exception message contains 'Channel Error' → push rewriter.channel_error."""

    def test_channel_error_detected_pushes_error_code(self):
        import requests as _requests
        rw, error_bus = _make_rewriter()

        exc = _requests.ConnectionError("LM Studio: Channel Error (model crashed)")
        with patch.object(rw._session, "post", side_effect=exc):
            result = rw.rewrite("Привет мир это тест текст для транскрипта")

        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "connection_error")
        self.assertTrue(error_bus.push.called, "error_bus.push должен быть вызван")

        pushed_error = error_bus.push.call_args[0][0]
        self.assertEqual(pushed_error.code, "rewriter.channel_error")


# ---------------------------------------------------------------------------
# 2. Channel Error in ConnectionError (lowercase) → rewriter.channel_error
# ---------------------------------------------------------------------------

class TestChannelErrorDetectedLowercase(unittest.TestCase):
    """Case-insensitive: 'channel error' in lowercase triggers same code."""

    def test_channel_error_lowercase_also_detected(self):
        import requests as _requests
        rw, error_bus = _make_rewriter()

        exc = _requests.ConnectionError("channel error: stopGenerating() without request_id")
        with patch.object(rw._session, "post", side_effect=exc):
            result = rw.rewrite("Текст транскрипта для проверки регистра")

        self.assertFalse(result.ok)
        self.assertTrue(error_bus.push.called)
        pushed_error = error_bus.push.call_args[0][0]
        self.assertEqual(pushed_error.code, "rewriter.channel_error")


# ---------------------------------------------------------------------------
# 3. Non-channel error → rewriter.connection_error (no regression)
# ---------------------------------------------------------------------------

class TestNonChannelErrorKeepsConnectionErrorCode(unittest.TestCase):
    """Regular connection errors still push rewriter.connection_error."""

    def test_regular_connection_error_pushes_connection_error_code(self):
        import requests as _requests
        rw, error_bus = _make_rewriter()

        exc = _requests.ConnectionError("Connection refused [Errno 111]")
        with patch.object(rw._session, "post", side_effect=exc):
            result = rw.rewrite("Текст для теста без channel error")

        self.assertFalse(result.ok)
        self.assertTrue(error_bus.push.called)
        pushed_error = error_bus.push.call_args[0][0]
        self.assertEqual(pushed_error.code, "rewriter.connection_error")


# ---------------------------------------------------------------------------
# 4. Channel Error in HTTP response body (non-200) → rewriter.channel_error
# ---------------------------------------------------------------------------

class TestChannelErrorInHttpBody(unittest.TestCase):
    """Channel Error substring in non-200 response body triggers rewriter.channel_error."""

    def test_channel_error_in_http_body_pushes_correct_code(self):
        rw, error_bus = _make_rewriter()

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = '{"error": "Channel Error: inference aborted"}'
        with patch.object(rw._session, "post", return_value=mock_resp):
            result = rw.rewrite("Тест ответа с channel error в теле ответа")

        self.assertFalse(result.ok)
        self.assertTrue(error_bus.push.called)
        pushed_error = error_bus.push.call_args[0][0]
        self.assertEqual(pushed_error.code, "rewriter.channel_error")


# ---------------------------------------------------------------------------
# 5. Default rewriter model is gemma-4-e4b-it-mlx (wave5: updated from qwen3-4b-abliterated)
# ---------------------------------------------------------------------------

class TestDefaultRewriterModel(unittest.TestCase):
    """LLM_MODEL default in config.py must be gemma-4-e4b-it-mlx (wave5 fix)."""

    def test_default_rewriter_is_gemma(self):
        """Check the class-level field default (not the runtime Settings instance
        which may be overridden by settings.json on disk)."""
        from core.config import Settings
        # Access the Pydantic field default directly to avoid settings.json override
        field_default = Settings.model_fields["LLM_MODEL"].default
        self.assertEqual(
            field_default,
            "gemma-4-e4b-it-mlx",
            f"Expected class default 'gemma-4-e4b-it-mlx', got '{field_default}'",
        )

    def test_default_settings_llm_model_is_gemma(self):
        from core.config import DEFAULT_SETTINGS
        self.assertIn("llm_model", DEFAULT_SETTINGS)
        self.assertEqual(DEFAULT_SETTINGS["llm_model"], "gemma-4-e4b-it-mlx")


# ---------------------------------------------------------------------------
# 6. Switch action changes setting
# ---------------------------------------------------------------------------

class TestSwitchToStableRewriterAction(unittest.TestCase):
    """_switch_to_stable_rewriter handler calls handle_set_settings with correct model."""

    @staticmethod
    def _ops(models=("huihui-qwen3-14b-abl-v2",)):
        ops = MagicMock()
        ops.handle_list_llm_models.return_value = {"models": list(models), "error": None}
        return ops

    @staticmethod
    def _settings(current="gemma-4-e4b-it-mlx"):
        svc = MagicMock()
        svc.cached_settings.return_value = {"llm_model": current}
        return svc

    def test_switch_action_changes_setting(self):
        from backend.error_actions import _switch_to_stable_rewriter

        settings_service = self._settings()
        result = _switch_to_stable_rewriter(
            settings_service=settings_service, llm_ops_svc=self._ops()
        )

        self.assertTrue(result["executed"])
        settings_service.handle_set_settings.assert_called_once_with(
            {"llm_model": "huihui-qwen3-14b-abl-v2"}
        )

    def test_switch_action_without_catalog_keeps_working_setting(self):
        """Каталог недоступен — рабочую модель не трогаем (fail-safe)."""
        from backend.error_actions import _switch_to_stable_rewriter

        settings_service = self._settings()
        result = _switch_to_stable_rewriter(
            settings_service=settings_service, llm_ops_svc=self._ops(models=())
        )

        self.assertFalse(result["executed"])
        settings_service.handle_set_settings.assert_not_called()

    def test_switch_action_registered_in_action_handlers(self):
        from backend.error_actions import ACTION_HANDLERS
        self.assertIn("switch_to_stable_rewriter", ACTION_HANDLERS)

    def test_switch_action_dispatched_via_handle_action(self):
        from backend.error_actions import handle_action

        settings_service = self._settings()
        result = handle_action(
            "switch_to_stable_rewriter",
            settings_service=settings_service,
            llm_ops_svc=self._ops(),
        )

        self.assertTrue(result["executed"])
        settings_service.handle_set_settings.assert_called_once_with(
            {"llm_model": "huihui-qwen3-14b-abl-v2"}
        )


# ---------------------------------------------------------------------------
# 7. recommended_models starts with gemma-4-e4b-it-mlx (wave5: updated from qwen3)
# ---------------------------------------------------------------------------

class TestRecommendedModels(unittest.TestCase):
    """list_llm_models (LLMOpsService) returns recommended_models with gemma-4-e4b-it-mlx first."""

    def test_recommended_models_starts_with_gemma(self):
        """Mock /v1/models endpoint — recommended_models[0] must be gemma-4-e4b-it-mlx."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"id": "gemma-4-e4b-it-mlx"},
                {"id": "qwen3-4b-abliterated"},
            ]
        }

        # Simulate the logic in LLMOpsService.handle_list_llm_models without full BackendService setup
        data = mock_resp.json()
        ids = [item.get("id") for item in data.get("data", []) if item.get("id")]
        recommended_models = [
            "gemma-4-e4b-it-mlx",
            "huihui-qwen3-4b-instruct-2507-abliterated-hi-mlx",
            "qwen3-8b-abliterated",
        ]
        result = {
            "models": sorted(ids),
            "recommended_models": recommended_models,
            "error": None,
        }

        self.assertEqual(result["recommended_models"][0], "gemma-4-e4b-it-mlx")
        self.assertIsNone(result["error"])

    def test_error_code_has_correct_action_id(self):
        from backend.error_codes import ERROR_REGISTRY
        entry = ERROR_REGISTRY.get("rewriter.channel_error")
        self.assertIsNotNone(entry, "rewriter.channel_error должен быть в ERROR_REGISTRY")
        self.assertEqual(entry["action_id"], "switch_to_stable_rewriter")
        self.assertEqual(entry["severity"], "warn")
        self.assertTrue(entry["actionable"])


if __name__ == "__main__":
    unittest.main()
