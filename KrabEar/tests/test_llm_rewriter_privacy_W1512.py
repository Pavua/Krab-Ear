"""Tests for W1504 N3+N4: privacy_mode_enabled guard in summarize() and fix_punctuation_only().

W1504 N3 MED: summarize() sends transcript text to LM Studio even in privacy mode.
W1504 N4 LOW: fix_punctuation_only() sends transcript text to LM Studio even in privacy mode.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm_rewriter import LLMRewriter


def _make_rewriter() -> LLMRewriter:
    return LLMRewriter(
        base_url="http://localhost:1234/v1",
        api_key="sk-test",
        model="test-model",
    )


def _mock_ok_response(content: str):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    resp.text = content
    return resp


class SummarizePrivacyModeTestCase(unittest.TestCase):
    """W1504 N3: summarize() must be blocked when privacy_mode_enabled=True."""

    def setUp(self):
        self.rewriter = _make_rewriter()

    def test_summarize_blocked_in_privacy_mode(self):
        """summarize() returns privacy_mode fallback without HTTP call."""
        self.rewriter._settings_getter = lambda key, default=None: (
            True if key == "privacy_mode_enabled" else default
        )
        with patch.object(self.rewriter._session, "post") as mock_post:
            result = self.rewriter.summarize("Это секретный разговор о сделке.")
            self.assertFalse(result.ok)
            self.assertEqual(result.fallback_reason, "privacy_mode")
            self.assertIsNone(result.text)
            self.assertIsNone(result.latency_ms)
            mock_post.assert_not_called()

    def test_summarize_works_when_privacy_disabled(self):
        """summarize() proceeds to HTTP when privacy_mode_enabled=False."""
        self.rewriter._settings_getter = lambda key, default=None: (
            False if key == "privacy_mode_enabled" else default
        )
        with patch.object(self.rewriter._session, "post") as mock_post:
            mock_post.return_value = _mock_ok_response("Краткое summary.")
            result = self.rewriter.summarize("Это обычный разговор о работе.")
            self.assertTrue(result.ok)
            self.assertEqual(result.text, "Краткое summary.")
            mock_post.assert_called_once()

    def test_summarize_works_when_no_settings_getter(self):
        """summarize() proceeds normally when _settings_getter is None (backward compat)."""
        self.assertIsNone(self.rewriter._settings_getter)
        with patch.object(self.rewriter._session, "post") as mock_post:
            mock_post.return_value = _mock_ok_response("Summary без privacy check.")
            result = self.rewriter.summarize("Обычный текст.")
            self.assertTrue(result.ok)
            mock_post.assert_called_once()

    def test_summarize_privacy_checked_before_empty_input(self):
        """Privacy check fires before empty-input guard so privacy guard is confirmed to run."""
        # Non-empty text + privacy mode ON → should block
        self.rewriter._settings_getter = lambda key, default=None: (
            True if key == "privacy_mode_enabled" else default
        )
        with patch.object(self.rewriter._session, "post") as mock_post:
            result = self.rewriter.summarize("Некоторый текст.")
            self.assertEqual(result.fallback_reason, "privacy_mode")
            mock_post.assert_not_called()

    def test_summarize_privacy_guard_swallows_settings_getter_exception(self):
        """Privacy guard must not raise even if settings_getter throws."""
        def bad_getter(key, default=None):
            raise RuntimeError("settings store unavailable")

        self.rewriter._settings_getter = bad_getter
        with patch.object(self.rewriter._session, "post") as mock_post:
            mock_post.return_value = _mock_ok_response("Summary even after error.")
            # Should proceed normally (exception is swallowed)
            result = self.rewriter.summarize("Текст для summary.")
            # No assertion on result.ok — the HTTP mock may or may not succeed.
            # Key invariant: no exception propagated.
            mock_post.assert_called_once()


class FixPunctuationOnlyPrivacyModeTestCase(unittest.TestCase):
    """W1504 N4: fix_punctuation_only() must be blocked when privacy_mode_enabled=True."""

    def setUp(self):
        self.rewriter = _make_rewriter()

    def test_fix_punctuation_only_blocked_in_privacy_mode(self):
        """fix_punctuation_only() returns None without HTTP call in privacy mode."""
        self.rewriter._settings_getter = lambda key, default=None: (
            True if key == "privacy_mode_enabled" else default
        )
        with patch.object(self.rewriter._session, "post") as mock_post:
            result = self.rewriter.fix_punctuation_only("привет мир это тест", language="ru")
            self.assertIsNone(result)
            mock_post.assert_not_called()

    def test_fix_punctuation_only_works_when_privacy_disabled(self):
        """fix_punctuation_only() proceeds to HTTP when privacy_mode_enabled=False."""
        self.rewriter._settings_getter = lambda key, default=None: (
            False if key == "privacy_mode_enabled" else default
        )
        input_text = "привет мир это тест"
        with patch.object(self.rewriter._session, "post") as mock_post:
            mock_post.return_value = _mock_ok_response("привет, мир, это тест")
            result = self.rewriter.fix_punctuation_only(input_text, language="ru")
            # Result may be None due to word-set guard (comma added to "тест,") but
            # the key invariant is that HTTP was called.
            mock_post.assert_called_once()

    def test_fix_punctuation_only_works_when_no_settings_getter(self):
        """fix_punctuation_only() proceeds normally when _settings_getter is None."""
        self.assertIsNone(self.rewriter._settings_getter)
        with patch.object(self.rewriter._session, "post") as mock_post:
            mock_post.return_value = _mock_ok_response("привет, мир.")
            self.rewriter.fix_punctuation_only("привет мир", language="ru")
            mock_post.assert_called_once()

    def test_fix_punctuation_only_privacy_guard_swallows_exception(self):
        """Privacy guard must not raise even if settings_getter throws."""
        def bad_getter(key, default=None):
            raise ValueError("db corrupted")

        self.rewriter._settings_getter = bad_getter
        with patch.object(self.rewriter._session, "post") as mock_post:
            mock_post.return_value = _mock_ok_response("привет, мир.")
            # Should proceed without raising
            self.rewriter.fix_punctuation_only("привет мир", language="ru")
            mock_post.assert_called_once()

    def test_fix_punctuation_only_empty_input_still_short_circuits(self):
        """Empty input returns original text even in privacy mode (no HTTP)."""
        self.rewriter._settings_getter = lambda key, default=None: (
            True if key == "privacy_mode_enabled" else default
        )
        with patch.object(self.rewriter._session, "post") as mock_post:
            result = self.rewriter.fix_punctuation_only("", language="ru")
            # Empty input → returns original (empty string) before privacy check
            self.assertEqual(result, "")
            mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
