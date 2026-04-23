"""Tests for backend.observability — Sentry/GlitchTip integration.

All tests verify no-op behaviour when DSN is absent and correct SDK
initialisation when a DSN is provided (SDK mocked).
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_observability():
    """Re-import observability with a clean module state."""
    import backend.observability as mod  # noqa: PLC0415
    # Reset module-level flag between tests
    mod._sentry_initialized = False
    return mod


class TestInitSentryNoOp(unittest.TestCase):
    """init_sentry returns False and never crashes when DSN is missing."""

    def setUp(self):
        _reload_observability()

    def test_none_dsn_returns_false(self):
        from backend.observability import init_sentry
        result = init_sentry(None)
        self.assertFalse(result)

    def test_empty_string_dsn_returns_false(self):
        from backend.observability import init_sentry
        result = init_sentry("")
        self.assertFalse(result)

    def test_none_dsn_no_exception(self):
        from backend.observability import init_sentry
        try:
            init_sentry(None)
        except Exception as exc:
            self.fail(f"init_sentry(None) raised {exc!r}")

    def test_none_dsn_does_not_set_initialized_flag(self):
        import backend.observability as mod
        mod.init_sentry(None)
        self.assertFalse(mod.is_sentry_initialized())


class TestInitSentryWithDsn(unittest.TestCase):
    """init_sentry calls sentry_sdk.init with correct args when DSN provided."""

    def _make_fake_sentry(self):
        """Return a minimal sentry_sdk stub."""
        fake = types.ModuleType("sentry_sdk")
        fake.init = MagicMock()
        return fake

    def setUp(self):
        _reload_observability()

    def test_returns_true_with_fake_dsn(self):
        import backend.observability as mod
        mod._sentry_initialized = False
        fake_sdk = self._make_fake_sentry()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            result = mod.init_sentry("https://fake@sentry.io/123")
        self.assertTrue(result)

    def test_calls_sentry_init_once(self):
        import backend.observability as mod
        mod._sentry_initialized = False
        fake_sdk = self._make_fake_sentry()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.init_sentry("https://fake@sentry.io/123", environment="staging")
        fake_sdk.init.assert_called_once()

    def test_traces_sample_rate_is_005(self):
        import backend.observability as mod
        mod._sentry_initialized = False
        fake_sdk = self._make_fake_sentry()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.init_sentry("https://fake@sentry.io/123")
        _, kwargs = fake_sdk.init.call_args
        self.assertAlmostEqual(kwargs.get("traces_sample_rate", -1), 0.05, places=5)

    def test_send_default_pii_is_false(self):
        import backend.observability as mod
        mod._sentry_initialized = False
        fake_sdk = self._make_fake_sentry()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.init_sentry("https://fake@sentry.io/123")
        _, kwargs = fake_sdk.init.call_args
        self.assertFalse(kwargs.get("send_default_pii", True))

    def test_environment_passed_through(self):
        import backend.observability as mod
        mod._sentry_initialized = False
        fake_sdk = self._make_fake_sentry()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.init_sentry("https://fake@sentry.io/123", environment="production")
        _, kwargs = fake_sdk.init.call_args
        self.assertEqual(kwargs.get("environment"), "production")

    def test_release_fallback_when_not_provided(self):
        import backend.observability as mod
        mod._sentry_initialized = False
        fake_sdk = self._make_fake_sentry()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.init_sentry("https://fake@sentry.io/123")
        _, kwargs = fake_sdk.init.call_args
        release = kwargs.get("release", "")
        self.assertIn("krab-ear", release)

    def test_custom_release_passed_through(self):
        import backend.observability as mod
        mod._sentry_initialized = False
        fake_sdk = self._make_fake_sentry()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.init_sentry("https://fake@sentry.io/123", release="krab-ear@1.2.3")
        _, kwargs = fake_sdk.init.call_args
        self.assertEqual(kwargs.get("release"), "krab-ear@1.2.3")


class TestCaptureException(unittest.TestCase):
    """capture_exception is no-op when not initialized, forwards when initialized."""

    def setUp(self):
        _reload_observability()

    def test_no_op_when_not_initialized(self):
        """Should not raise even if sentry_sdk not in sys.modules."""
        import backend.observability as mod
        mod._sentry_initialized = False
        exc = ValueError("test error")
        try:
            mod.capture_exception(exc)
        except Exception as e:
            self.fail(f"capture_exception raised {e!r} when not initialized")


if __name__ == "__main__":
    unittest.main()
