"""Tests for W1229 F3 MED: LLM probe and engine rewrite must respect privacy_mode_enabled.

W1240 fix verifies:
1. LLMHttpProbe._tick() skips the probe entirely when privacy_mode_enabled=True.
2. AudioEngine._llm_rewrite_allowed() returns False when privacy_mode_enabled=True.
3. Runtime toggle: switching privacy_mode on pauses the probe (no health-check calls).
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import unittest
from unittest.mock import MagicMock

from backend.llm_probe import LLMHttpProbe


# ---------------------------------------------------------------------------
# Helpers shared across probe tests
# ---------------------------------------------------------------------------

class _FakeRewriter:
    """Minimal stand-in that tracks passive_health_check call count."""

    def __init__(self, *, reachable: bool = True, has_model: bool = True):
        self.check_calls = 0
        self._reachable = reachable
        self._has_model = has_model

    def passive_health_check(self) -> tuple[bool, bool]:
        self.check_calls += 1
        return (self._reachable, self._has_model)


def _make_probe(settings: dict, rewriter=None) -> LLMHttpProbe:
    if rewriter is None:
        rewriter = _FakeRewriter()
    return LLMHttpProbe(
        rewriter=rewriter,
        error_bus=MagicMock(),
        event_bus=MagicMock(),
        settings_provider=lambda: settings,
        base_interval_sec=30.0,
    )


# ---------------------------------------------------------------------------
# 1. LLMHttpProbe skips tick when privacy_mode_enabled=True
# ---------------------------------------------------------------------------

class TestLLMProbeSkipsTickInPrivacyMode(unittest.TestCase):
    """test_llm_probe_skips_tick_in_privacy_mode — no HTTP call in privacy mode."""

    def test_llm_probe_skips_tick_in_privacy_mode(self):
        """When privacy_mode_enabled=True, _tick() must return early — no health check."""
        rewriter = _FakeRewriter(reachable=True, has_model=True)
        probe = _make_probe(
            settings={"llm_rewrite_enabled": True, "privacy_mode_enabled": True},
            rewriter=rewriter,
        )
        probe._tick()
        self.assertEqual(
            rewriter.check_calls,
            0,
            "passive_health_check must NOT be called when privacy_mode_enabled=True",
        )

    def test_probe_proceeds_when_privacy_mode_false(self):
        """Sanity: when privacy_mode_enabled=False and rewrite enabled, probe fires."""
        rewriter = _FakeRewriter(reachable=True, has_model=True)
        probe = _make_probe(
            settings={"llm_rewrite_enabled": True, "privacy_mode_enabled": False},
            rewriter=rewriter,
        )
        probe._tick()
        self.assertEqual(rewriter.check_calls, 1, "probe must call health check when privacy_mode=False")

    def test_probe_proceeds_when_privacy_key_absent(self):
        """When privacy_mode_enabled is not in settings dict, probe fires normally."""
        rewriter = _FakeRewriter(reachable=True, has_model=True)
        probe = _make_probe(
            settings={"llm_rewrite_enabled": True},
            rewriter=rewriter,
        )
        probe._tick()
        self.assertEqual(rewriter.check_calls, 1, "probe must call health check when key absent (defaults to False)")


# ---------------------------------------------------------------------------
# 2. engine._llm_rewrite_allowed() returns False in privacy mode
# ---------------------------------------------------------------------------

class TestLLMRewriteAllowedReturnsFalseInPrivacyMode(unittest.TestCase):
    """test_llm_rewrite_allowed_returns_false_in_privacy_mode."""

    def _make_engine(self, settings: dict):
        from core.engine import AudioEngine
        engine = AudioEngine()
        engine._llm_rewriter = MagicMock()  # non-None so only settings matter
        engine._settings_get = lambda key, default=None: settings.get(key, default)
        return engine

    def test_llm_rewrite_allowed_returns_false_in_privacy_mode(self):
        """_llm_rewrite_allowed() must be False when privacy_mode_enabled=True."""
        engine = self._make_engine({
            "llm_rewrite_enabled": True,
            "privacy_mode_enabled": True,
        })
        self.assertFalse(
            engine._llm_rewrite_allowed(),
            "_llm_rewrite_allowed() must return False when privacy_mode_enabled=True",
        )

    def test_llm_rewrite_allowed_true_when_privacy_off(self):
        """Sanity: _llm_rewrite_allowed() is True when both flags are correct."""
        engine = self._make_engine({
            "llm_rewrite_enabled": True,
            "privacy_mode_enabled": False,
        })
        self.assertTrue(engine._llm_rewrite_allowed())

    def test_llm_rewrite_allowed_false_when_rewrite_disabled_regardless_of_privacy(self):
        """_llm_rewrite_allowed() is False when llm_rewrite_enabled=False even without privacy."""
        engine = self._make_engine({
            "llm_rewrite_enabled": False,
            "privacy_mode_enabled": False,
        })
        self.assertFalse(engine._llm_rewrite_allowed())

    def test_llm_rewrite_allowed_false_when_rewriter_is_none(self):
        """_llm_rewrite_allowed() is False when _llm_rewriter is None (no rewriter injected)."""
        from core.engine import AudioEngine
        engine = AudioEngine()
        engine._llm_rewriter = None
        engine._settings_get = lambda key, default=None: True  # everything enabled
        self.assertFalse(engine._llm_rewrite_allowed())


# ---------------------------------------------------------------------------
# 3. Runtime toggle: switching privacy on mid-session pauses probe
# ---------------------------------------------------------------------------

class TestRuntimeTogglePrivacyPausesProbe(unittest.TestCase):
    """test_runtime_toggle_privacy_pauses_probe — settings read fresh each tick."""

    def test_runtime_toggle_privacy_pauses_probe(self):
        """After toggling privacy_mode_enabled=True, subsequent ticks must not probe."""
        settings = {"llm_rewrite_enabled": True, "privacy_mode_enabled": False}
        rewriter = _FakeRewriter(reachable=True, has_model=True)
        probe = _make_probe(settings=settings, rewriter=rewriter)

        # First tick with privacy off — probe fires
        probe._tick()
        self.assertEqual(rewriter.check_calls, 1, "tick 1: probe should fire with privacy=False")

        # Toggle privacy mode on at runtime
        settings["privacy_mode_enabled"] = True

        # Subsequent ticks should not probe
        probe._tick()
        probe._tick()
        self.assertEqual(
            rewriter.check_calls,
            1,
            "ticks 2+3: probe must be suppressed after privacy_mode toggled on",
        )

    def test_runtime_toggle_privacy_off_resumes_probe(self):
        """After toggling privacy_mode back off, the probe resumes."""
        settings = {"llm_rewrite_enabled": True, "privacy_mode_enabled": True}
        rewriter = _FakeRewriter(reachable=True, has_model=True)
        probe = _make_probe(settings=settings, rewriter=rewriter)

        # Tick while privacy on — no probe
        probe._tick()
        self.assertEqual(rewriter.check_calls, 0)

        # Toggle privacy off
        settings["privacy_mode_enabled"] = False

        # Now probe should resume
        probe._tick()
        self.assertEqual(rewriter.check_calls, 1, "probe must resume after privacy_mode toggled off")


if __name__ == "__main__":
    unittest.main()
