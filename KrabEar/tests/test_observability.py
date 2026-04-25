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

    def test_release_fallback_when_not_provided_and_git_fails(self):
        """When release=None and git is unavailable, fallback contains 'krab-ear'."""
        import backend.observability as mod
        mod._sentry_initialized = False
        fake_sdk = self._make_fake_sentry()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            with patch("backend.observability.subprocess.run", side_effect=OSError):
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


# ---------------------------------------------------------------------------
# Breadcrumb tests
# ---------------------------------------------------------------------------

class TestAddBreadcrumbNoOp(unittest.TestCase):
    """add_breadcrumb is a no-op when Sentry is not initialized."""

    def setUp(self):
        _reload_observability()

    def test_no_op_when_not_initialized(self):
        """Must not raise and must not call sentry_sdk when uninitialized."""
        import backend.observability as mod
        mod._sentry_initialized = False
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.add_breadcrumb = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.add_breadcrumb("recording", "started")
        fake_sdk.add_breadcrumb.assert_not_called()

    def test_no_op_does_not_raise(self):
        import backend.observability as mod
        mod._sentry_initialized = False
        try:
            mod.add_breadcrumb("ipc", "ping", level="debug", data={"x": 1})
        except Exception as e:
            self.fail(f"add_breadcrumb raised {e!r} when not initialized")


class TestAddBreadcrumbWithDsn(unittest.TestCase):
    """add_breadcrumb calls sentry_sdk.add_breadcrumb when initialized."""

    def _init_with_fake_sdk(self):
        import backend.observability as mod
        mod._sentry_initialized = False
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.init = MagicMock()
        fake_sdk.add_breadcrumb = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.init_sentry("https://fake@sentry.io/123")
        return mod, fake_sdk

    def test_calls_sentry_add_breadcrumb(self):
        mod, fake_sdk = self._init_with_fake_sdk()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.add_breadcrumb("recording", "started", level="info", data={"quality_profile": "balanced"})
        fake_sdk.add_breadcrumb.assert_called_once()

    def test_passes_correct_category_and_message(self):
        mod, fake_sdk = self._init_with_fake_sdk()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.add_breadcrumb("translation", "translate_text", level="info", data={"source_lang": "ru"})
        _, kwargs = fake_sdk.add_breadcrumb.call_args
        self.assertEqual(kwargs.get("category"), "translation")
        self.assertEqual(kwargs.get("message"), "translate_text")

    def test_passes_level(self):
        mod, fake_sdk = self._init_with_fake_sdk()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.add_breadcrumb("ipc", "stop_recording", level="warning")
        _, kwargs = fake_sdk.add_breadcrumb.call_args
        self.assertEqual(kwargs.get("level"), "warning")

    def test_data_defaults_to_empty_dict_when_none(self):
        mod, fake_sdk = self._init_with_fake_sdk()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.add_breadcrumb("ipc", "ping")
        _, kwargs = fake_sdk.add_breadcrumb.call_args
        self.assertEqual(kwargs.get("data"), {})


class TestReleaseFromGit(unittest.TestCase):
    """release_from_git returns a non-empty string in all cases."""

    def setUp(self):
        _reload_observability()

    def test_returns_string(self):
        from backend.observability import release_from_git
        result = release_from_git()
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_returns_fallback_when_git_fails(self):
        """When subprocess raises, release_from_git returns the fallback string."""
        from backend.observability import release_from_git
        with patch("backend.observability.subprocess.run", side_effect=OSError("no git")):
            result = release_from_git()
        self.assertEqual(result, "krab-ear@unknown")

    def test_returns_fallback_when_git_nonzero(self):
        """When git describe exits non-zero, returns fallback."""
        from backend.observability import release_from_git
        fake = MagicMock()
        fake.returncode = 128
        fake.stdout = ""
        with patch("backend.observability.subprocess.run", return_value=fake):
            result = release_from_git()
        self.assertEqual(result, "krab-ear@unknown")

    def test_returns_git_tag_when_successful(self):
        """When git describe succeeds, that string is returned verbatim."""
        from backend.observability import release_from_git
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "v2.0.0-14-gabcdef1\n"
        with patch("backend.observability.subprocess.run", return_value=fake):
            result = release_from_git()
        self.assertEqual(result, "v2.0.0-14-gabcdef1")

    def test_strips_trailing_whitespace(self):
        from backend.observability import release_from_git
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "v1.0.0-dirty  \n"
        with patch("backend.observability.subprocess.run", return_value=fake):
            result = release_from_git()
        self.assertEqual(result, "v1.0.0-dirty")


class TestInitSentryReleaseAutoDetect(unittest.TestCase):
    """init_sentry uses release_from_git when release=None."""

    def _make_fake_sentry(self):
        fake = types.ModuleType("sentry_sdk")
        fake.init = MagicMock()
        return fake

    def setUp(self):
        _reload_observability()

    def test_auto_detects_release_when_none(self):
        """release=None triggers release_from_git; result passed to sentry_sdk.init."""
        import backend.observability as mod
        mod._sentry_initialized = False
        fake_sdk = self._make_fake_sentry()
        fake_run = MagicMock()
        fake_run.returncode = 0
        fake_run.stdout = "v3.1.0-5-gdeadbeef\n"
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            with patch("backend.observability.subprocess.run", return_value=fake_run):
                mod.init_sentry("https://fake@sentry.io/123", release=None)
        _, kwargs = fake_sdk.init.call_args
        self.assertEqual(kwargs.get("release"), "v3.1.0-5-gdeadbeef")

    def test_explicit_release_skips_git(self):
        """When release is given, subprocess.run must NOT be called."""
        import backend.observability as mod
        mod._sentry_initialized = False
        fake_sdk = self._make_fake_sentry()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            with patch("backend.observability.subprocess.run") as mock_run:
                mod.init_sentry("https://fake@sentry.io/123", release="custom@0.9")
        mock_run.assert_not_called()

    def test_fallback_release_used_when_git_unavailable(self):
        """If git fails, sentry_sdk.init still receives 'krab-ear@unknown'."""
        import backend.observability as mod
        mod._sentry_initialized = False
        fake_sdk = self._make_fake_sentry()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            with patch("backend.observability.subprocess.run", side_effect=OSError):
                mod.init_sentry("https://fake@sentry.io/123", release=None)
        _, kwargs = fake_sdk.init.call_args
        self.assertEqual(kwargs.get("release"), "krab-ear@unknown")

    def test_init_returns_true_with_auto_release(self):
        import backend.observability as mod
        mod._sentry_initialized = False
        fake_sdk = self._make_fake_sentry()
        fake_run = MagicMock()
        fake_run.returncode = 0
        fake_run.stdout = "v1.0.0\n"
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            with patch("backend.observability.subprocess.run", return_value=fake_run):
                result = mod.init_sentry("https://fake@sentry.io/123", release=None)
        self.assertTrue(result)


class TestMaskPhone(unittest.TestCase):
    """mask_phone helper returns only last 4 digits."""

    def setUp(self):
        _reload_observability()

    def test_masks_international_number(self):
        from backend.observability import mask_phone
        result = mask_phone("+34666123456")
        self.assertTrue(result.endswith("3456"))
        self.assertNotIn("666", result)

    def test_masks_local_number(self):
        from backend.observability import mask_phone
        result = mask_phone("0501234567")
        self.assertTrue(result.endswith("4567"))

    def test_short_number_returns_masked(self):
        from backend.observability import mask_phone
        result = mask_phone("1234")
        self.assertIn("*", result)
        self.assertTrue(result.endswith("1234"))

    def test_empty_string_returns_masked(self):
        from backend.observability import mask_phone
        result = mask_phone("")
        self.assertTrue(result.startswith("*"))


if __name__ == "__main__":
    unittest.main()
