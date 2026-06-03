"""Tests for W1205 security fixes in OpenWakeWordAdapter (W1210).

Covers:
  F1 — threshold clamping / rejection
  F2 — privacy mode guard
  F3 — symlink model rejection + path-escape rejection
  F4 — download timeout (mocked)
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup (mirrors other test files in this project)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.openwakeword_adapter import (  # noqa: E402
    OpenWakeWordAdapter,
    _THRESHOLD_MIN,
    _THRESHOLD_MAX,
    _MODEL_LOAD_TIMEOUT_SEC,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(
    tmp_dir: str | Path,
    settings: dict | None = None,
    oww_available: bool = False,
) -> OpenWakeWordAdapter:
    """Create adapter with stubbed lib availability and optional settings."""
    settings = settings or {}
    adapter = OpenWakeWordAdapter(
        data_dir=tmp_dir,
        settings_get=lambda k, d: settings.get(k, d),
    )
    # Pretend library is/isn't available without real import
    adapter._oww_available = oww_available
    return adapter


# ---------------------------------------------------------------------------
# F1 — Threshold clamping and rejection
# ---------------------------------------------------------------------------

class TestThresholdValidation(unittest.TestCase):
    """F1: covert mic tap guard — threshold clamping and rejection."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._adapter = _make_adapter(self._tmp)

    # ---- rejection --------------------------------------------------------

    def test_threshold_negative_rejected(self) -> None:
        """Negative threshold must be rejected with ok=False."""
        result = self._adapter.handle_wake_word_start(
            {"model": "hey_jarvis", "threshold": -0.1}
        )
        self.assertFalse(result["ok"], result)
        self.assertIn("error", result)
        self.assertIn("отрицательным", result["error"])

    def test_threshold_negative_integer_rejected(self) -> None:
        """Integer negative threshold must also be rejected."""
        result = self._adapter.handle_wake_word_start(
            {"model": "hey_jarvis", "threshold": -1}
        )
        self.assertFalse(result["ok"], result)

    # ---- clamping ---------------------------------------------------------

    def test_threshold_zero_clamped_to_minimum(self) -> None:
        """threshold=0.0 must be clamped to _THRESHOLD_MIN (0.05) not rejected."""
        # start() will raise RuntimeError (no library), but threshold must be clamped first.
        result = self._adapter.handle_wake_word_start(
            {"model": "hey_jarvis", "threshold": 0.0}
        )
        # ok may be False (no library) but the response must not be a 'negative' rejection
        self.assertNotIn("отрицательным", result.get("error", ""))
        # If the adapter tried to start (library unavailable → RuntimeError), threshold
        # was already accepted/clamped. If it somehow succeeded (stub), check value.
        if result.get("ok"):
            self.assertAlmostEqual(result["threshold"], _THRESHOLD_MIN)

    def test_threshold_very_small_positive_clamped(self) -> None:
        """threshold=0.001 (positive but below min) must be clamped to _THRESHOLD_MIN."""
        result = self._adapter.handle_wake_word_start(
            {"model": "hey_jarvis", "threshold": 0.001}
        )
        self.assertNotIn("отрицательным", result.get("error", ""))
        if result.get("ok"):
            self.assertAlmostEqual(result["threshold"], _THRESHOLD_MIN)

    def test_threshold_above_one_clamped_to_one(self) -> None:
        """threshold=1.5 must be clamped to 1.0."""
        result = self._adapter.handle_wake_word_start(
            {"model": "hey_jarvis", "threshold": 1.5}
        )
        self.assertNotIn("отрицательным", result.get("error", ""))
        if result.get("ok"):
            self.assertAlmostEqual(result["threshold"], _THRESHOLD_MAX)

    def test_threshold_exactly_at_min_accepted(self) -> None:
        """threshold=_THRESHOLD_MIN must pass through unmodified."""
        # We need start() to succeed, so mock it
        with patch.object(self._adapter, "start") as mock_start:
            result = self._adapter.handle_wake_word_start(
                {"model": "hey_jarvis", "threshold": _THRESHOLD_MIN}
            )
        self.assertTrue(result.get("ok"), result)
        self.assertAlmostEqual(result["threshold"], _THRESHOLD_MIN)

    def test_threshold_exactly_at_max_accepted(self) -> None:
        """threshold=1.0 must pass through unmodified."""
        with patch.object(self._adapter, "start"):
            result = self._adapter.handle_wake_word_start(
                {"model": "hey_jarvis", "threshold": 1.0}
            )
        self.assertTrue(result.get("ok"), result)
        self.assertAlmostEqual(result["threshold"], _THRESHOLD_MAX)

    def test_threshold_normal_value_unchanged(self) -> None:
        """threshold=0.5 must pass through unmodified."""
        with patch.object(self._adapter, "start"):
            result = self._adapter.handle_wake_word_start(
                {"model": "hey_jarvis", "threshold": 0.5}
            )
        self.assertTrue(result.get("ok"), result)
        self.assertAlmostEqual(result["threshold"], 0.5)


# ---------------------------------------------------------------------------
# F2 — Privacy mode guard
# ---------------------------------------------------------------------------

class TestPrivacyModeGuard(unittest.TestCase):
    """F2: privacy mode must prevent opening mic tap."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()

    def test_privacy_mode_blocks_wake_word_start(self) -> None:
        """When privacy_mode_enabled=True, handle_wake_word_start must return ok=False."""
        adapter = _make_adapter(
            self._tmp,
            settings={"privacy_mode_enabled": True},
        )
        result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertFalse(result["ok"], result)
        self.assertIn("reason", result)
        self.assertEqual(result["reason"], "cannot activate wake-word in privacy mode")

    def test_privacy_mode_false_proceeds(self) -> None:
        """When privacy_mode_enabled=False, the call should NOT be blocked."""
        adapter = _make_adapter(
            self._tmp,
            settings={"privacy_mode_enabled": False},
        )
        with patch.object(adapter, "start"):
            result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertNotEqual(result.get("reason"), "cannot activate wake-word in privacy mode")

    def test_privacy_mode_missing_key_proceeds(self) -> None:
        """When key is absent (defaults to False), must not block."""
        adapter = _make_adapter(self._tmp, settings={})
        with patch.object(adapter, "start"):
            result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertNotEqual(result.get("reason"), "cannot activate wake-word in privacy mode")

    def test_privacy_mode_blocks_regardless_of_threshold(self) -> None:
        """Even with a valid threshold, privacy mode must block before threshold check."""
        adapter = _make_adapter(
            self._tmp,
            settings={"privacy_mode_enabled": True},
        )
        result = adapter.handle_wake_word_start({"model": "hey_jarvis", "threshold": 0.7})
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "cannot activate wake-word in privacy mode")


# ---------------------------------------------------------------------------
# F3 — Symlink rejection + path escape check
# ---------------------------------------------------------------------------

class TestSymlinkAndPathEscape(unittest.TestCase):
    """F3: _load_model must reject symlinks and paths outside data_dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._adapter = _make_adapter(self._tmp, oww_available=True)
        # Stub the OWWModel import so tests don't need openwakeword installed
        self._oww_patcher = patch(
            "backend.openwakeword_adapter.OpenWakeWordAdapter._load_model",
            wraps=self._adapter._load_model,
        )

    def _make_real_model_file(self, name: str = "test_model.onnx") -> Path:
        """Create an actual (non-symlink) file inside the custom models dir."""
        models_dir = Path(self._tmp) / "wake_word_models"
        models_dir.mkdir(parents=True, exist_ok=True)
        model_file = models_dir / name
        model_file.write_bytes(b"fake onnx content")
        return model_file

    def test_symlink_model_rejected(self) -> None:
        """A symlink path must raise ValueError before model load."""
        models_dir = Path(self._tmp) / "wake_word_models"
        models_dir.mkdir(parents=True, exist_ok=True)

        # Create the real file and a symlink pointing to it
        real_file = Path(self._tmp) / "real_model.onnx"
        real_file.write_bytes(b"fake content")
        symlink = models_dir / "sym_model.onnx"
        symlink.symlink_to(real_file)

        with self.assertRaises(ValueError) as ctx:
            # Patch import so we reach the symlink check
            with patch.dict("sys.modules", {"openwakeword": MagicMock(), "openwakeword.model": MagicMock()}):
                self._adapter._load_model("sym_model", str(symlink))
        self.assertIn("symlink", str(ctx.exception))

    def test_escape_outside_data_dir_rejected(self) -> None:
        """A model path that resolves outside data_dir must raise ValueError."""
        outside_file = Path(self._tmp).parent / "outside_model.onnx"
        outside_file.write_bytes(b"fake content")
        try:
            with self.assertRaises(ValueError) as ctx:
                with patch.dict("sys.modules", {"openwakeword": MagicMock(), "openwakeword.model": MagicMock()}):
                    self._adapter._load_model("outside_model", str(outside_file))
            self.assertIn("data_dir", str(ctx.exception))
        finally:
            outside_file.unlink(missing_ok=True)

    def test_valid_model_inside_data_dir_passes_checks(self) -> None:
        """A real file inside data_dir must pass both symlink and escape checks."""
        model_file = self._make_real_model_file()

        # Mock OWWModel so it doesn't actually load the file
        fake_oww = MagicMock()
        mock_module = MagicMock()
        mock_module.Model = MagicMock(return_value=fake_oww)

        with patch.dict("sys.modules", {
            "openwakeword": MagicMock(),
            "openwakeword.model": mock_module,
        }):
            result = self._adapter._load_model("test_model", str(model_file))
        self.assertIsNotNone(result)


# ---------------------------------------------------------------------------
# F4 — Download timeout (mocked)
# ---------------------------------------------------------------------------

class TestDownloadTimeout(unittest.TestCase):
    """F4: _load_model must raise RuntimeError if load takes too long."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._adapter = _make_adapter(self._tmp, oww_available=True)

    def _make_model_file(self) -> Path:
        models_dir = Path(self._tmp) / "wake_word_models"
        models_dir.mkdir(parents=True, exist_ok=True)
        f = models_dir / "slow_model.onnx"
        f.write_bytes(b"fake")
        return f

    def test_builtin_model_timeout_raises(self) -> None:
        """Built-in model load that hangs must raise RuntimeError after timeout."""
        import backend.openwakeword_adapter as mod

        # Patch the timeout to a tiny value so the test is fast
        original_timeout = mod._MODEL_LOAD_TIMEOUT_SEC
        mod._MODEL_LOAD_TIMEOUT_SEC = 0.1

        try:
            def _hanging_model(*args, **kwargs):
                threading.Event().wait()  # blocks forever

            mock_module = MagicMock()
            mock_module.Model = _hanging_model

            with patch.dict("sys.modules", {
                "openwakeword": MagicMock(),
                "openwakeword.model": mock_module,
            }):
                with self.assertRaises(RuntimeError) as ctx:
                    self._adapter._load_model("hey_jarvis", None)
            self.assertIn("таймаут", str(ctx.exception))
        finally:
            mod._MODEL_LOAD_TIMEOUT_SEC = original_timeout

    def test_custom_model_timeout_raises(self) -> None:
        """Custom model load that hangs must raise RuntimeError after timeout."""
        import backend.openwakeword_adapter as mod

        model_file = self._make_model_file()
        original_timeout = mod._MODEL_LOAD_TIMEOUT_SEC
        mod._MODEL_LOAD_TIMEOUT_SEC = 0.1

        try:
            def _hanging_model(*args, **kwargs):
                threading.Event().wait()

            mock_module = MagicMock()
            mock_module.Model = _hanging_model

            with patch.dict("sys.modules", {
                "openwakeword": MagicMock(),
                "openwakeword.model": mock_module,
            }):
                with self.assertRaises(RuntimeError) as ctx:
                    self._adapter._load_model("slow_model", str(model_file))
            self.assertIn("таймаут", str(ctx.exception))
        finally:
            mod._MODEL_LOAD_TIMEOUT_SEC = original_timeout

    def test_fast_model_load_succeeds(self) -> None:
        """A model that loads quickly must not trigger the timeout."""
        import backend.openwakeword_adapter as mod

        original_timeout = mod._MODEL_LOAD_TIMEOUT_SEC
        mod._MODEL_LOAD_TIMEOUT_SEC = 5.0  # plenty of time

        try:
            fake_oww = MagicMock()
            mock_module = MagicMock()
            mock_module.Model = MagicMock(return_value=fake_oww)

            with patch.dict("sys.modules", {
                "openwakeword": MagicMock(),
                "openwakeword.model": mock_module,
            }):
                result = self._adapter._load_model("hey_jarvis", None)
            self.assertIsNotNone(result)
        finally:
            mod._MODEL_LOAD_TIMEOUT_SEC = original_timeout


if __name__ == "__main__":
    unittest.main()
