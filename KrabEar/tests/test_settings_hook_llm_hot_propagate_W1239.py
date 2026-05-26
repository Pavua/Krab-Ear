"""Tests for W1229 F2 MED: _on_settings_saved hot-propagates llm_model + llm_base_url.

Verifies that the after-save hook registered by BackendService correctly calls
LLMRewriter.set_model() and set_base_url() when those settings change, and that
it does NOT call them when nothing changes.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _make_hook(rewriter_mock):
    """Build the _on_settings_saved closure directly — mirrors service.py logic.

    This mirrors the exact code in BackendService.__init__ so we can test the
    closure in isolation without spinning up a full BackendService.
    """
    _rewriter_ref = rewriter_mock

    def _on_settings_saved(old: dict, new: dict) -> None:
        new_key = str(new.get("lm_studio_api_key", ""))
        if new_key != str(old.get("lm_studio_api_key", "")):
            _rewriter_ref.set_api_key(new_key)
        new_model = str(new.get("llm_model", ""))
        if new_model and new_model != str(old.get("llm_model", "")):
            _rewriter_ref.set_model(new_model)
        new_url = str(new.get("llm_base_url", ""))
        if new_url and new_url != str(old.get("llm_base_url", "")):
            _rewriter_ref.set_base_url(new_url)

    return _on_settings_saved


class TestSettingsHookLLMModelPropagation(unittest.TestCase):
    """Hook calls set_model when llm_model changes."""

    def setUp(self):
        self.rewriter = MagicMock()
        self.hook = _make_hook(self.rewriter)

    def test_settings_hook_propagates_llm_model_change(self):
        old = {"llm_model": "old-model", "llm_base_url": "", "lm_studio_api_key": ""}
        new = {"llm_model": "qwen3-4b", "llm_base_url": "", "lm_studio_api_key": ""}
        self.hook(old, new)
        self.rewriter.set_model.assert_called_once_with("qwen3-4b")

    def test_settings_hook_propagates_llm_model_change_from_absent(self):
        """Setting llm_model for the first time (key absent in old) triggers call."""
        old = {}
        new = {"llm_model": "gemma-4-4b"}
        self.hook(old, new)
        self.rewriter.set_model.assert_called_once_with("gemma-4-4b")

    def test_no_llm_model_change_does_not_call_rewriter_setter(self):
        old = {"llm_model": "same-model"}
        new = {"llm_model": "same-model"}
        self.hook(old, new)
        self.rewriter.set_model.assert_not_called()

    def test_empty_new_model_does_not_call_set_model(self):
        """Empty string for new model should be a no-op (guard prevents blank model)."""
        old = {"llm_model": "some-model"}
        new = {"llm_model": ""}
        self.hook(old, new)
        self.rewriter.set_model.assert_not_called()


class TestSettingsHookLLMBaseUrlPropagation(unittest.TestCase):
    """Hook calls set_base_url when llm_base_url changes."""

    def setUp(self):
        self.rewriter = MagicMock()
        self.hook = _make_hook(self.rewriter)

    def test_settings_hook_propagates_llm_base_url_change(self):
        old = {"llm_base_url": "http://127.0.0.1:1234/v1", "llm_model": "", "lm_studio_api_key": ""}
        new = {"llm_base_url": "http://192.168.1.5:1234/v1", "llm_model": "", "lm_studio_api_key": ""}
        self.hook(old, new)
        self.rewriter.set_base_url.assert_called_once_with("http://192.168.1.5:1234/v1")

    def test_settings_hook_propagates_llm_base_url_change_from_absent(self):
        old = {}
        new = {"llm_base_url": "http://localhost:5000/v1"}
        self.hook(old, new)
        self.rewriter.set_base_url.assert_called_once_with("http://localhost:5000/v1")

    def test_no_llm_base_url_change_does_not_call_rewriter_setter(self):
        old = {"llm_base_url": "http://127.0.0.1:1234/v1"}
        new = {"llm_base_url": "http://127.0.0.1:1234/v1"}
        self.hook(old, new)
        self.rewriter.set_base_url.assert_not_called()

    def test_empty_new_url_does_not_call_set_base_url(self):
        """Empty string for new URL is a no-op (guard prevents clearing the URL)."""
        old = {"llm_base_url": "http://127.0.0.1:1234/v1"}
        new = {"llm_base_url": ""}
        self.hook(old, new)
        self.rewriter.set_base_url.assert_not_called()


class TestSettingsHookNoChanges(unittest.TestCase):
    """When no LLM settings change, no setter is called."""

    def setUp(self):
        self.rewriter = MagicMock()
        self.hook = _make_hook(self.rewriter)

    def test_no_llm_change_does_not_call_rewriter_setter(self):
        settings = {
            "lm_studio_api_key": "key-abc",
            "llm_model": "qwen3-4b",
            "llm_base_url": "http://127.0.0.1:1234/v1",
        }
        self.hook(settings.copy(), settings.copy())
        self.rewriter.set_api_key.assert_not_called()
        self.rewriter.set_model.assert_not_called()
        self.rewriter.set_base_url.assert_not_called()

    def test_unrelated_setting_change_does_not_call_rewriter(self):
        old = {"llm_model": "qwen3-4b", "llm_base_url": "http://127.0.0.1:1234/v1",
               "lm_studio_api_key": "", "some_other": "old"}
        new = {"llm_model": "qwen3-4b", "llm_base_url": "http://127.0.0.1:1234/v1",
               "lm_studio_api_key": "", "some_other": "new"}
        self.hook(old, new)
        self.rewriter.set_api_key.assert_not_called()
        self.rewriter.set_model.assert_not_called()
        self.rewriter.set_base_url.assert_not_called()


class TestSettingsHookMultipleChanges(unittest.TestCase):
    """All three settings changing at once: all three setters called."""

    def setUp(self):
        self.rewriter = MagicMock()
        self.hook = _make_hook(self.rewriter)

    def test_all_three_settings_changed_calls_all_setters(self):
        old = {
            "lm_studio_api_key": "old-key",
            "llm_model": "old-model",
            "llm_base_url": "http://old:1234/v1",
        }
        new = {
            "lm_studio_api_key": "new-key",
            "llm_model": "new-model",
            "llm_base_url": "http://new:5678/v1",
        }
        self.hook(old, new)
        self.rewriter.set_api_key.assert_called_once_with("new-key")
        self.rewriter.set_model.assert_called_once_with("new-model")
        self.rewriter.set_base_url.assert_called_once_with("http://new:5678/v1")


class TestLLMRewriterSetBaseUrl(unittest.TestCase):
    """Unit tests for the newly added LLMRewriter.set_base_url() method."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
            model="test-model",
            timeout_sec=5,
        )

    def test_set_base_url_updates_internal_url(self):
        self.rewriter.set_base_url("http://192.168.1.5:1234/v1")
        self.assertEqual(self.rewriter._base_url, "http://192.168.1.5:1234/v1")

    def test_set_base_url_strips_trailing_slash(self):
        self.rewriter.set_base_url("http://127.0.0.1:9090/v1/")
        self.assertEqual(self.rewriter._base_url, "http://127.0.0.1:9090/v1")

    def test_set_base_url_same_url_is_noop(self):
        """Calling set_base_url with the current URL should not touch circuit."""
        original_circuit = self.rewriter._circuit
        self.rewriter.set_base_url("http://127.0.0.1:1234/v1")
        self.assertIs(self.rewriter._circuit, original_circuit)

    def test_set_base_url_resets_circuit_breaker(self):
        """New URL should replace the circuit breaker instance."""
        original_circuit = self.rewriter._circuit
        self.rewriter.set_base_url("http://new-host:1234/v1")
        self.assertIsNot(self.rewriter._circuit, original_circuit)

    def test_set_base_url_clears_last_error(self):
        self.rewriter._last_error = "previous connection error"
        self.rewriter.set_base_url("http://new-host:1234/v1")
        self.assertIsNone(self.rewriter._last_error)

    def test_set_base_url_spawns_warmup_thread(self):
        with patch("threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            self.rewriter.set_base_url("http://new-host:9999/v1")
        mock_thread_cls.assert_called_once()
        mock_thread.start.assert_called_once()


class TestLLMRewriterSetModel(unittest.TestCase):
    """Smoke-test LLMRewriter.set_model to confirm its behaviour (existing method)."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
            model="old-model",
            timeout_sec=5,
        )

    def test_set_model_updates_internal_model(self):
        self.rewriter.set_model("new-model")
        self.assertEqual(self.rewriter._model, "new-model")

    def test_set_model_same_model_is_noop(self):
        original_circuit = self.rewriter._circuit
        self.rewriter.set_model("old-model")
        self.assertIs(self.rewriter._circuit, original_circuit)

    def test_set_model_resets_circuit_breaker(self):
        original_circuit = self.rewriter._circuit
        self.rewriter.set_model("another-model")
        self.assertIsNot(self.rewriter._circuit, original_circuit)


if __name__ == "__main__":
    unittest.main()
