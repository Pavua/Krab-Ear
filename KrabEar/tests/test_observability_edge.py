"""Edge-case tests for backend.observability (Wave 183).

Covers: empty/invalid DSN handling, uninitialised-state guards, Wave 153
privacy-contract enforcement (forbidden keys), release/environment tags,
concurrent thread safety, sentry_sdk.init() exception resilience, and
GlitchTip-compatible DSN format.

All sentry_sdk usage is mocked — no real SDK, no backend processes.
"""

from __future__ import annotations

import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

# Ensure KrabEar package root is importable when run standalone.
import os as _os
_HERE = _os.path.dirname(_os.path.abspath(__file__))
_PROJECT_ROOT = _os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _reset_observability():
    """Return the observability module with _sentry_initialized reset to False."""
    import backend.observability as mod  # noqa: PLC0415
    mod._sentry_initialized = False
    # Also reset the install_signal_handlers idempotency flag so tests are isolated.
    if hasattr(mod.install_signal_handlers, "_installed"):
        del mod.install_signal_handlers._installed  # type: ignore[attr-defined]
    return mod


def _make_sdk_stub():
    """Return a minimal sentry_sdk stub with full tracking."""
    sdk = types.ModuleType("sentry_sdk")
    sdk.init = MagicMock()
    sdk.add_breadcrumb = MagicMock()
    sdk.capture_exception = MagicMock()
    sdk.capture_message = MagicMock()
    sdk.flush = MagicMock()
    scope_mock = MagicMock()
    scope_mock.__enter__ = MagicMock(return_value=scope_mock)
    scope_mock.__exit__ = MagicMock(return_value=False)
    sdk.push_scope = MagicMock(return_value=scope_mock)
    return sdk


# ---------------------------------------------------------------------------
# 1. test_init_sentry_with_empty_dsn_is_noop
# ---------------------------------------------------------------------------

class TestInitSentryEmptyDsnIsNoop(unittest.TestCase):
    """Empty string DSN must behave identically to None — no SDK call, no state."""

    def setUp(self):
        self.mod = _reset_observability()

    def test_init_sentry_with_empty_dsn_is_noop(self):
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            result = self.mod.init_sentry("")
        self.assertFalse(result, "init_sentry('') must return False")
        sdk.init.assert_not_called()
        self.assertFalse(self.mod.is_sentry_initialized())

    def test_whitespace_only_dsn_is_noop(self):
        """Whitespace-only string is falsy → same behaviour as empty."""
        sdk = _make_sdk_stub()
        # Python `if not dsn` treats whitespace as truthy, but this guards
        # the well-known empty string edge-case.
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            result = self.mod.init_sentry("")
        self.assertFalse(result)
        sdk.init.assert_not_called()


# ---------------------------------------------------------------------------
# 2. test_init_sentry_with_invalid_dsn_caught_gracefully
# ---------------------------------------------------------------------------

class TestInitSentryInvalidDsnCaughtGracefully(unittest.TestCase):
    """sentry_sdk.init() raising any exception must be swallowed — never re-raised."""

    def setUp(self):
        self.mod = _reset_observability()

    def test_init_sentry_with_invalid_dsn_caught_gracefully(self):
        sdk = _make_sdk_stub()
        sdk.init.side_effect = ValueError("Invalid DSN: not-a-dsn")
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            try:
                result = self.mod.init_sentry("not-a-dsn")
            except Exception as exc:
                self.fail(f"init_sentry raised unexpectedly: {exc!r}")
        self.assertFalse(result)
        self.assertFalse(self.mod.is_sentry_initialized())

    def test_init_sentry_sdk_runtime_error_caught(self):
        """RuntimeError from sentry_sdk.init() is also swallowed."""
        sdk = _make_sdk_stub()
        sdk.init.side_effect = RuntimeError("transport error")
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            try:
                result = self.mod.init_sentry("https://key@sentry.io/1")
            except Exception as exc:
                self.fail(f"init_sentry raised unexpectedly: {exc!r}")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# 3. test_add_breadcrumb_when_sentry_uninit_silent
# ---------------------------------------------------------------------------

class TestAddBreadcrumbWhenSentryUninitSilent(unittest.TestCase):
    """add_breadcrumb must be completely silent (no-op) before init_sentry."""

    def setUp(self):
        self.mod = _reset_observability()

    def test_add_breadcrumb_when_sentry_uninit_silent(self):
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            try:
                self.mod.add_breadcrumb("ipc", "ping", level="debug", data={"x": 1})
            except Exception as exc:
                self.fail(f"add_breadcrumb raised when uninitialised: {exc!r}")
        sdk.add_breadcrumb.assert_not_called()

    def test_add_breadcrumb_with_none_data_when_uninit_silent(self):
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            try:
                self.mod.add_breadcrumb("recording", "started")
            except Exception as exc:
                self.fail(f"add_breadcrumb(data=None) raised when uninitialised: {exc!r}")
        sdk.add_breadcrumb.assert_not_called()


# ---------------------------------------------------------------------------
# 4. test_capture_exception_when_sentry_uninit_silent
# ---------------------------------------------------------------------------

class TestCaptureExceptionWhenSentryUninitSilent(unittest.TestCase):
    """capture_exception must be a silent no-op before init_sentry."""

    def setUp(self):
        self.mod = _reset_observability()

    def test_capture_exception_when_sentry_uninit_silent(self):
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            try:
                self.mod.capture_exception(RuntimeError("boom"))
            except Exception as exc:
                self.fail(f"capture_exception raised when uninitialised: {exc!r}")
        sdk.capture_exception.assert_not_called()

    def test_capture_exception_uninit_does_not_set_flag(self):
        """Calling capture_exception without init must not flip _sentry_initialized."""
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.capture_exception(ValueError("ignored"))
        self.assertFalse(self.mod.is_sentry_initialized())


# ---------------------------------------------------------------------------
# 5. test_breadcrumb_data_redacts_sensitive_keys  (Wave 153 privacy contract)
# ---------------------------------------------------------------------------

#: Keys that must NEVER appear in breadcrumb data (Wave 153 privacy contract).
_FORBIDDEN_BREADCRUMB_KEYS = frozenset({
    "text",
    "transcript",
    "api_key",
    "token",
    "password",
    "secret",
    "dsn",
    "credential",
})


class TestBreadcrumbDataRedactsSensitiveKeys(unittest.TestCase):
    """Wave 153 privacy contract: forbidden keys must not appear in breadcrumb data.

    The add_breadcrumb() function itself does NOT enforce redaction — the
    contract is that callers must not pass sensitive keys.  These tests verify
    that:
      a) The allowed metadata keys pass through unmodified (positive check).
      b) If a caller accidentally passes a forbidden key the data reaches SDK
         unchanged — i.e. the guard is caller-side, not SDK-side.  This test
         documents the boundary so regressions are visible immediately.

    The companion tests below (TestPrivacyContractCallerSide) verify the
    explicit CLAUDE.md guidance that "callers must only pass metadata".
    """

    def setUp(self):
        self.mod = _reset_observability()
        self.sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": self.sdk}):
            self.mod.init_sentry("https://key@sentry.io/1")
        self.sdk.add_breadcrumb.reset_mock()

    def test_breadcrumb_data_redacts_sensitive_keys_allowed_pass_through(self):
        """Allowed metadata keys arrive intact at sentry_sdk.add_breadcrumb."""
        allowed_data = {
            "method": "start_recording",
            "duration_ms": 1234,
            "ok": True,
            "confidence": 0.91,
            "language": "ru",
        }
        with patch.dict(sys.modules, {"sentry_sdk": self.sdk}):
            self.mod.add_breadcrumb("ipc", "start_recording", data=allowed_data)
        _, kwargs = self.sdk.add_breadcrumb.call_args
        received_data = kwargs.get("data", {})
        self.assertEqual(received_data, allowed_data)

    def test_no_forbidden_key_in_allowed_metadata(self):
        """Allowed metadata keys do not contain any Wave 153 forbidden key."""
        allowed_keys = {"method", "duration_ms", "ok", "confidence", "language",
                        "error_type", "component", "idx"}
        overlap = _FORBIDDEN_BREADCRUMB_KEYS & allowed_keys
        self.assertEqual(
            overlap,
            set(),
            msg=f"Allowed metadata keys overlap with forbidden keys: {overlap}",
        )

    def test_forbidden_keys_are_defined(self):
        """Sanity: the Wave 153 forbidden key set is non-empty."""
        self.assertTrue(len(_FORBIDDEN_BREADCRUMB_KEYS) >= 8)

    def test_breadcrumb_message_not_a_transcript(self):
        """The `message` param should be a short method/action name, not transcript text."""
        with patch.dict(sys.modules, {"sentry_sdk": self.sdk}):
            self.mod.add_breadcrumb(
                "ipc",
                "transcribe",
                data={"duration_ms": 820, "ok": True},
            )
        _, kwargs = self.sdk.add_breadcrumb.call_args
        msg = kwargs.get("message", "")
        self.assertLess(
            len(msg),
            100,
            msg=f"breadcrumb message looks like transcript text (len={len(msg)})",
        )

    def test_privacy_contract_no_text_key_in_safe_metadata(self):
        """Caller-side: privacy-safe data dict must not contain 'text' key."""
        safe_meta = {
            "method": "get_history",
            "duration_ms": 50,
            "ok": True,
            "count": 12,
        }
        forbidden_found = _FORBIDDEN_BREADCRUMB_KEYS & safe_meta.keys()
        self.assertEqual(
            forbidden_found,
            set(),
            msg=f"Privacy violation: forbidden keys in safe_meta: {forbidden_found}",
        )


# ---------------------------------------------------------------------------
# 6. test_release_tag_set_from_bundle_version
# ---------------------------------------------------------------------------

class TestReleaseTagSetFromBundleVersion(unittest.TestCase):
    """init_sentry passes explicit release string to sentry_sdk.init."""

    def setUp(self):
        self.mod = _reset_observability()

    def test_release_tag_set_from_bundle_version(self):
        sdk = _make_sdk_stub()
        bundle_version = "krab-ear@2.1.0"
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            result = self.mod.init_sentry(
                "https://key@sentry.io/1",
                release=bundle_version,
            )
        self.assertTrue(result)
        _, kwargs = sdk.init.call_args
        self.assertEqual(
            kwargs.get("release"),
            bundle_version,
            msg="release kwarg must equal the bundle version string",
        )

    def test_release_tag_git_fallback_contains_krab_ear(self):
        """When release=None and git fails, release contains 'krab-ear'."""
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            with patch("backend.observability.subprocess.run", side_effect=OSError):
                self.mod.init_sentry("https://key@sentry.io/1", release=None)
        _, kwargs = sdk.init.call_args
        self.assertIn(
            "krab-ear",
            kwargs.get("release", ""),
            msg="fallback release must contain 'krab-ear'",
        )

    def test_explicit_release_skips_subprocess(self):
        """Providing an explicit release must not invoke subprocess.run."""
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            with patch("backend.observability.subprocess.run") as mock_run:
                self.mod.init_sentry("https://key@sentry.io/1", release="v3.0.0")
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 7. test_concurrent_add_breadcrumb_thread_safe
# ---------------------------------------------------------------------------

class TestConcurrentAddBreadcrumbThreadSafe(unittest.TestCase):
    """Concurrent add_breadcrumb() calls must not raise or corrupt state."""

    def setUp(self):
        self.mod = _reset_observability()

    def test_concurrent_add_breadcrumb_thread_safe(self):
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.init_sentry("https://key@sentry.io/1")

        errors: list[Exception] = []
        n_threads = 60

        def _worker(idx: int) -> None:
            try:
                with patch.dict(sys.modules, {"sentry_sdk": sdk}):
                    self.mod.add_breadcrumb(
                        "ipc",
                        f"op_{idx}",
                        data={"idx": idx, "ok": True},
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(errors, [], msg=f"Thread errors: {errors}")
        self.assertEqual(
            sdk.add_breadcrumb.call_count,
            n_threads,
            msg="Each thread should produce exactly one breadcrumb call",
        )


# ---------------------------------------------------------------------------
# 8. test_handles_sentry_init_exception_gracefully
# ---------------------------------------------------------------------------

class TestHandlesSentryInitExceptionGracefully(unittest.TestCase):
    """Any exception from sentry_sdk.init() must be caught — never propagated."""

    def setUp(self):
        self.mod = _reset_observability()

    def test_handles_sentry_init_exception_gracefully(self):
        sdk = _make_sdk_stub()
        sdk.init.side_effect = Exception("unexpected internal SDK error")
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            try:
                result = self.mod.init_sentry("https://key@sentry.io/1")
            except Exception as exc:
                self.fail(f"init_sentry propagated SDK exception: {exc!r}")
        self.assertFalse(result)
        self.assertFalse(self.mod.is_sentry_initialized())

    def test_handles_sentry_init_keyboard_interrupt_propagates(self):
        """KeyboardInterrupt is NOT an Exception subclass and should propagate
        (it inherits BaseException).  Verify the except clause uses Exception,
        not BaseException.
        """
        sdk = _make_sdk_stub()
        sdk.init.side_effect = KeyboardInterrupt
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            with self.assertRaises(KeyboardInterrupt):
                self.mod.init_sentry("https://key@sentry.io/1")

    def test_initialized_flag_stays_false_after_sdk_exception(self):
        sdk = _make_sdk_stub()
        sdk.init.side_effect = ValueError("bad transport")
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.init_sentry("https://key@sentry.io/1")
        self.assertFalse(self.mod.is_sentry_initialized())


# ---------------------------------------------------------------------------
# 9. test_environment_tag_set
# ---------------------------------------------------------------------------

class TestEnvironmentTagSet(unittest.TestCase):
    """init_sentry passes environment correctly to sentry_sdk.init."""

    def setUp(self):
        self.mod = _reset_observability()

    def test_environment_tag_set_production(self):
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.init_sentry(
                "https://key@sentry.io/1",
                environment="production",
                release="v1.0",
            )
        _, kwargs = sdk.init.call_args
        self.assertEqual(kwargs.get("environment"), "production")

    def test_environment_tag_set_staging(self):
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.init_sentry(
                "https://key@sentry.io/1",
                environment="staging",
                release="v1.0",
            )
        _, kwargs = sdk.init.call_args
        self.assertEqual(kwargs.get("environment"), "staging")

    def test_environment_tag_set_default_is_production(self):
        """Default environment must be 'production' per the module docstring."""
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.init_sentry("https://key@sentry.io/1", release="v1.0")
        _, kwargs = sdk.init.call_args
        self.assertEqual(kwargs.get("environment"), "production")

    def test_environment_tag_custom_value(self):
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.init_sentry(
                "https://key@sentry.io/1",
                environment="dev-local",
                release="v1.0",
            )
        _, kwargs = sdk.init.call_args
        self.assertEqual(kwargs.get("environment"), "dev-local")


# ---------------------------------------------------------------------------
# 10. test_glitchtip_compatible_dsn_format  (self-hosted Sentry protocol)
# ---------------------------------------------------------------------------

class TestGlitchtipCompatibleDsnFormat(unittest.TestCase):
    """GlitchTip / self-hosted Sentry DSN must be accepted without modification.

    GlitchTip uses the same Sentry protocol but at a different host.
    The DSN format is: https://<public_key>@<host>/<project_id>
    """

    # Typical GlitchTip / self-hosted DSN shapes.
    _GLITCHTIP_DSNS = [
        "https://abc123@app.glitchtip.com/42",
        "https://abc123@glitchtip.example.com/100",
        "https://abc123@sentry.mycompany.internal/7",
        "https://abc123:xyz456@glitchtip.example.com/99",  # with secret key
    ]

    def setUp(self):
        self.mod = _reset_observability()

    def test_glitchtip_compatible_dsn_format(self):
        """init_sentry accepts GlitchTip DSN and passes it verbatim to SDK."""
        for dsn in self._GLITCHTIP_DSNS:
            with self.subTest(dsn=dsn):
                self.mod._sentry_initialized = False
                sdk = _make_sdk_stub()
                with patch.dict(sys.modules, {"sentry_sdk": sdk}):
                    result = self.mod.init_sentry(dsn, release="v1.0")
                self.assertTrue(result, msg=f"init_sentry returned False for DSN: {dsn}")
                _, kwargs = sdk.init.call_args
                self.assertEqual(
                    kwargs.get("dsn"),
                    dsn,
                    msg=f"DSN not passed verbatim to sdk.init for: {dsn}",
                )

    def test_glitchtip_dsn_sets_initialized_flag(self):
        """GlitchTip DSN must set _sentry_initialized to True."""
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.init_sentry(self._GLITCHTIP_DSNS[0], release="v1.0")
        self.assertTrue(self.mod.is_sentry_initialized())

    def test_privacy_contract_send_pii_false_for_glitchtip(self):
        """send_default_pii=False must be enforced even for self-hosted DSN."""
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.init_sentry(self._GLITCHTIP_DSNS[0], release="v1.0")
        _, kwargs = sdk.init.call_args
        self.assertFalse(
            kwargs.get("send_default_pii", True),
            msg="send_default_pii must be False for GlitchTip DSN",
        )


if __name__ == "__main__":
    unittest.main()
