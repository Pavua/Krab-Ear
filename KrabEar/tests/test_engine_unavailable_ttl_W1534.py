"""W1534: regression guard for _UNAVAILABLE_TTL_SEC eviction in AudioEngine.

W1525 meta-audit confirmed W1304 TTL logic was reverted by the W1497 cherry-pick
train.  W1534 restores the missing _handle_clear_unavailable_models handler in
service.py and provides a named regression suite so future cherry-pick trains
cannot silently drop the TTL without CI catching it.

Three tests required by the W1534 spec:
- test_unavailable_models_ttl_constant_present   — constant exported, positive
- test_unavailable_eviction_after_ttl            — entry deleted after TTL expiry
- test_unavailable_stays_blocked_within_ttl      — entry kept while within TTL
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import AudioEngine, _UNAVAILABLE_MODEL_TTL_SEC


def _make_engine() -> AudioEngine:
    """Minimal AudioEngine via object.__new__ — skips heavy __init__."""
    engine = object.__new__(AudioEngine)
    engine._unavailable_models = {}
    engine._router = None
    return engine


class UnavailableTTLW1534TestCase(unittest.TestCase):
    """W1534 regression guard: _UNAVAILABLE_TTL_SEC eviction."""

    def setUp(self) -> None:
        self.engine = _make_engine()

    # ------------------------------------------------------------------
    # 1. Constant present and positive
    # ------------------------------------------------------------------

    def test_unavailable_models_ttl_constant_present(self) -> None:
        """_UNAVAILABLE_MODEL_TTL_SEC must be importable and positive (> 0)."""
        self.assertIsInstance(_UNAVAILABLE_MODEL_TTL_SEC, (int, float))
        self.assertGreater(_UNAVAILABLE_MODEL_TTL_SEC, 0,
                           "_UNAVAILABLE_MODEL_TTL_SEC must be positive")

    # ------------------------------------------------------------------
    # 2. Entry evicted after TTL — fast-forward via patch
    # ------------------------------------------------------------------

    def test_unavailable_eviction_after_ttl(self) -> None:
        """After TTL expires, _is_model_unavailable returns False and evicts the entry.

        Uses patch.object(time, 'monotonic') to fast-forward past the TTL without
        actually sleeping.
        """
        model = "whisper-large-v3-mlx"
        mark_ts = 1000.0  # arbitrary anchor

        # Record at t=1000
        with patch.object(time, "monotonic", return_value=mark_ts):
            self.engine._unavailable_models[model] = time.monotonic()

        # Check at t = 1000 + TTL + 1 (expired)
        future_ts = mark_ts + _UNAVAILABLE_MODEL_TTL_SEC + 1.0
        with patch.object(time, "monotonic", return_value=future_ts):
            result = self.engine._is_model_unavailable(model)

        self.assertFalse(result, "Model should be unblocked after TTL expiry")
        self.assertNotIn(model, self.engine._unavailable_models,
                         "Expired entry must be evicted from the dict")

    # ------------------------------------------------------------------
    # 3. Entry stays blocked within TTL — fast-forward via patch
    # ------------------------------------------------------------------

    def test_unavailable_stays_blocked_within_ttl(self) -> None:
        """Within TTL, _is_model_unavailable returns True and keeps the entry.

        Uses patch.object(time, 'monotonic') to advance time to just before TTL
        expiry without actually sleeping.
        """
        model = "gigaam-rnnt"
        mark_ts = 2000.0  # arbitrary anchor

        # Record at t=2000
        with patch.object(time, "monotonic", return_value=mark_ts):
            self.engine._unavailable_models[model] = time.monotonic()

        # Check at t = 2000 + TTL - 1 (still within window)
        near_expiry_ts = mark_ts + _UNAVAILABLE_MODEL_TTL_SEC - 1.0
        with patch.object(time, "monotonic", return_value=near_expiry_ts):
            result = self.engine._is_model_unavailable(model)

        self.assertTrue(result, "Model should still be blocked within TTL window")
        self.assertIn(model, self.engine._unavailable_models,
                      "Entry must not be evicted while within TTL")


if __name__ == "__main__":
    unittest.main()
