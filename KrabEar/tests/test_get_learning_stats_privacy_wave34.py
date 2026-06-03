"""wave-34 MED — privacy gate for _handle_get_learning_stats.

FINDING (MED, privacy read-leak): BackendService._handle_get_learning_stats had
no privacy gate. The LanguageLearningManager derives top words from transcript
history. Without the gate, get_learning_stats IPC returned transcript-derived
vocabulary even when privacy_mode_enabled=True.

FIX: add at the very top of _handle_get_learning_stats:
    if self._get_runtime_setting('privacy_mode_enabled', False):
        return {'ok': False, 'reason': 'privacy_mode_active'}

This test validates:
1. privacy_mode=True  → ok=False, reason='privacy_mode_active', no delegate call.
2. privacy_mode=False → language_learning.handle_get_learning_stats is called.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Minimal stub: just the handler under test + _get_runtime_setting
# ---------------------------------------------------------------------------

class _StubService:
    """Minimal stub that replicates only _handle_get_learning_stats logic."""

    def __init__(self, privacy_on: bool, learning_result: dict) -> None:
        self._privacy_on = privacy_on
        self._language_learning = MagicMock()
        self._language_learning.handle_get_learning_stats.return_value = learning_result
        self.store = MagicMock()

    def _get_runtime_setting(self, key: str, default):
        if key == 'privacy_mode_enabled':
            return self._privacy_on
        return default

    # Copy of the actual fixed handler body from service.py:
    def _handle_get_learning_stats(self, params: dict) -> dict:
        if self._get_runtime_setting('privacy_mode_enabled', False):
            return {'ok': False, 'reason': 'privacy_mode_active'}
        params_with_store = dict(params)
        params_with_store.setdefault("store", self.store)
        return self._language_learning.handle_get_learning_stats(params_with_store)


class TestGetLearningStatsPrivacyGate(unittest.TestCase):
    """Privacy gate for get_learning_stats (wave-34 MED)."""

    _DUMMY_STATS = {"ok": True, "words_learned": 12, "sessions": 3}

    # ------------------------------------------------------------------
    # Gate active
    # ------------------------------------------------------------------

    def test_privacy_on_returns_ok_false(self):
        svc = _StubService(privacy_on=True, learning_result=self._DUMMY_STATS)
        result = svc._handle_get_learning_stats({})
        self.assertFalse(result.get("ok"))

    def test_privacy_on_returns_privacy_mode_active_reason(self):
        svc = _StubService(privacy_on=True, learning_result=self._DUMMY_STATS)
        result = svc._handle_get_learning_stats({})
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_on_does_not_call_delegate(self):
        svc = _StubService(privacy_on=True, learning_result=self._DUMMY_STATS)
        svc._handle_get_learning_stats({})
        svc._language_learning.handle_get_learning_stats.assert_not_called()

    def test_privacy_on_result_has_exactly_two_keys(self):
        """Guard against accidentally leaking extra fields in the privacy response."""
        svc = _StubService(privacy_on=True, learning_result=self._DUMMY_STATS)
        result = svc._handle_get_learning_stats({})
        self.assertEqual(set(result.keys()), {"ok", "reason"})

    # ------------------------------------------------------------------
    # Gate inactive
    # ------------------------------------------------------------------

    def test_privacy_off_delegates_to_language_learning(self):
        svc = _StubService(privacy_on=False, learning_result=self._DUMMY_STATS)
        result = svc._handle_get_learning_stats({})
        svc._language_learning.handle_get_learning_stats.assert_called_once()
        self.assertEqual(result, self._DUMMY_STATS)

    def test_privacy_off_passes_store_to_delegate(self):
        svc = _StubService(privacy_on=False, learning_result=self._DUMMY_STATS)
        svc._handle_get_learning_stats({})
        call_params = svc._language_learning.handle_get_learning_stats.call_args[0][0]
        self.assertIn("store", call_params)
        self.assertIs(call_params["store"], svc.store)

    def test_privacy_off_passes_extra_params_to_delegate(self):
        svc = _StubService(privacy_on=False, learning_result=self._DUMMY_STATS)
        svc._handle_get_learning_stats({"lang": "ru", "limit": 10})
        call_params = svc._language_learning.handle_get_learning_stats.call_args[0][0]
        self.assertEqual(call_params.get("lang"), "ru")
        self.assertEqual(call_params.get("limit"), 10)

    # ------------------------------------------------------------------
    # Verify the live service.py contains the gate (regression sentinel)
    # ------------------------------------------------------------------

    def test_service_py_handler_contains_privacy_gate(self):
        """Smoke: confirms the production file has the gate, not just the stub."""
        service_py = PROJECT_ROOT / "backend" / "service.py"
        src = service_py.read_text(encoding="utf-8")
        # Find the handler definition and the few lines after it.
        start = src.find("def _handle_get_learning_stats")
        self.assertGreater(start, 0, "_handle_get_learning_stats not found in service.py")
        snippet = src[start: start + 400]
        self.assertIn("privacy_mode_enabled", snippet,
                      "privacy gate missing from _handle_get_learning_stats in service.py")
        self.assertIn("privacy_mode_active", snippet,
                      "privacy_mode_active reason missing from _handle_get_learning_stats")


if __name__ == "__main__":
    unittest.main()
