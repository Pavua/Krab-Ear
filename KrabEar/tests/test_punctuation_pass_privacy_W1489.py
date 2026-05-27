"""Tests for W1482 F2 MED: _punctuation_pass_allowed() must check privacy_mode_enabled.

When privacy mode is on, ANY LLM call (including punctuation pass) must be blocked.
"""
import unittest
from unittest.mock import MagicMock
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class PunctuationPassPrivacyModeTestCase(unittest.TestCase):
    """_punctuation_pass_allowed() must return False when privacy_mode_enabled=True."""

    def _make_engine(self, settings: dict):
        from core.engine import AudioEngine
        engine = AudioEngine()
        engine._llm_rewriter = MagicMock()  # non-None so rewriter gate passes
        engine._settings_get = lambda k, d=None: settings.get(k, d)
        return engine

    def test_punctuation_pass_blocked_in_privacy_mode(self):
        """privacy_mode_enabled=True + stt_punctuation_llm_pass_enabled=True → False."""
        engine = self._make_engine({
            "privacy_mode_enabled": True,
            "stt_punctuation_llm_pass_enabled": True,
        })
        result = engine._punctuation_pass_allowed()
        self.assertFalse(
            result,
            "_punctuation_pass_allowed() must return False when privacy mode is on, "
            "even if stt_punctuation_llm_pass_enabled=True",
        )

    def test_punctuation_pass_allowed_when_privacy_disabled(self):
        """privacy_mode_enabled=False + stt_punctuation_llm_pass_enabled=True → True."""
        engine = self._make_engine({
            "privacy_mode_enabled": False,
            "stt_punctuation_llm_pass_enabled": True,
        })
        result = engine._punctuation_pass_allowed()
        self.assertTrue(
            result,
            "_punctuation_pass_allowed() must return True when privacy mode is off "
            "and stt_punctuation_llm_pass_enabled=True",
        )

    def test_punctuation_pass_safe_when_settings_getter_raises(self):
        """If settings_getter raises, the privacy guard must catch and continue safely.

        The outer rewriter-None guard still blocks when _llm_rewriter is None.
        When _llm_rewriter is not None and stt_punctuation_llm_pass_enabled=True,
        an exception from settings_getter for the privacy key should be swallowed
        (not propagate) and the method should proceed to check the pass-enabled flag.
        """
        from core.engine import AudioEngine
        engine = AudioEngine()
        engine._llm_rewriter = MagicMock()  # non-None

        call_count = [0]

        def raising_getter(key, default=None):
            call_count[0] += 1
            if key == "privacy_mode_enabled":
                raise RuntimeError("settings unavailable")
            if key == "stt_punctuation_llm_pass_enabled":
                return True
            return default

        engine._settings_get = raising_getter

        # Must NOT raise even though settings_getter raises for privacy_mode_enabled
        try:
            result = engine._punctuation_pass_allowed()
        except Exception as exc:  # pragma: no cover
            self.fail(
                f"_punctuation_pass_allowed() raised {exc!r} when settings_getter raised"
            )

        # Privacy check must have been attempted (call_count >= 1)
        self.assertGreaterEqual(call_count[0], 1)
        # After swallowing the exception, it should fall through to the pass-enabled check
        self.assertTrue(
            result,
            "After swallowing the privacy_mode settings exception, "
            "stt_punctuation_llm_pass_enabled=True should win",
        )


if __name__ == "__main__":
    unittest.main()
