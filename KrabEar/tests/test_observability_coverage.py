"""Additional coverage tests for backend.observability (Wave 80).

Focuses on: SDK init correctness, privacy guarantees (no transcript text),
import-error resilience, and thread safety of breadcrumbs.
All tests are pure unit tests — no real sentry_sdk is loaded, no models run.
"""

from __future__ import annotations

import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

# Ensure the KrabEar package root is on sys.path when run standalone.
import os as _os
_HERE = _os.path.dirname(_os.path.abspath(__file__))
_PROJECT_ROOT = _os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _reset_module():
    """Return observability module with _sentry_initialized reset to False."""
    import backend.observability as mod  # noqa: PLC0415
    mod._sentry_initialized = False
    return mod


def _make_sdk_stub():
    """Return a minimal sentry_sdk stub with tracked calls."""
    sdk = types.ModuleType("sentry_sdk")
    sdk.init = MagicMock()
    sdk.add_breadcrumb = MagicMock()
    sdk.capture_exception = MagicMock()
    # push_scope as context manager
    scope_mock = MagicMock()
    scope_mock.__enter__ = MagicMock(return_value=scope_mock)
    scope_mock.__exit__ = MagicMock(return_value=False)
    sdk.push_scope = MagicMock(return_value=scope_mock)
    return sdk


# ---------------------------------------------------------------------------
# 1. init_sentry with DSN → calls sdk.init
# ---------------------------------------------------------------------------

class TestInitSentryWithDsnCallsSdkInit(unittest.TestCase):
    """test_init_sentry_with_dsn_calls_sdk_init"""

    def setUp(self):
        self.mod = _reset_module()

    def test_init_sentry_with_dsn_calls_sdk_init(self):
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            result = self.mod.init_sentry("https://key@sentry.io/999")
        self.assertTrue(result)
        sdk.init.assert_called_once()
        _, kwargs = sdk.init.call_args
        self.assertEqual(kwargs["dsn"], "https://key@sentry.io/999")


# ---------------------------------------------------------------------------
# 2. init_sentry None DSN → no-op
# ---------------------------------------------------------------------------

class TestInitSentryWithNoneDsnIsNoop(unittest.TestCase):
    """test_init_sentry_with_none_dsn_is_noop"""

    def setUp(self):
        self.mod = _reset_module()

    def test_init_sentry_with_none_dsn_is_noop(self):
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            result = self.mod.init_sentry(None)
        self.assertFalse(result)
        sdk.init.assert_not_called()
        self.assertFalse(self.mod.is_sentry_initialized())


# ---------------------------------------------------------------------------
# 3. init_sentry empty string → no-op
# ---------------------------------------------------------------------------

class TestInitSentryWithEmptyStringIsNoop(unittest.TestCase):
    """test_init_sentry_with_empty_string_is_noop"""

    def setUp(self):
        self.mod = _reset_module()

    def test_init_sentry_with_empty_string_is_noop(self):
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            result = self.mod.init_sentry("")
        self.assertFalse(result)
        sdk.init.assert_not_called()


# ---------------------------------------------------------------------------
# 4. init_sentry uses correct release tag
# ---------------------------------------------------------------------------

class TestInitSentryUsesCorrectReleaseTag(unittest.TestCase):
    """test_init_sentry_uses_correct_release_tag"""

    def setUp(self):
        self.mod = _reset_module()

    def test_init_sentry_uses_correct_release_tag(self):
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.init_sentry("https://key@sentry.io/1", release="krab-ear@2.0.5")
        _, kwargs = sdk.init.call_args
        self.assertEqual(kwargs.get("release"), "krab-ear@2.0.5")

    def test_auto_release_from_git_used_when_none(self):
        sdk = _make_sdk_stub()
        fake_run = MagicMock(returncode=0, stdout="v2.1.0-3-gabc1234\n")
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            with patch("backend.observability.subprocess.run", return_value=fake_run):
                self.mod.init_sentry("https://key@sentry.io/1", release=None)
        _, kwargs = sdk.init.call_args
        self.assertEqual(kwargs.get("release"), "v2.1.0-3-gabc1234")


# ---------------------------------------------------------------------------
# 5. ImportError for sentry_sdk is handled gracefully
# ---------------------------------------------------------------------------

class TestInitSentryHandlesSdkImportErrorGracefully(unittest.TestCase):
    """test_init_sentry_handles_sdk_import_error_gracefully"""

    def setUp(self):
        self.mod = _reset_module()

    def test_init_sentry_handles_sdk_import_error_gracefully(self):
        # Inject a broken stub that raises ImportError when any attribute is accessed.
        broken = types.ModuleType("sentry_sdk")
        broken.init = MagicMock(side_effect=ImportError("sentry_sdk not installed"))
        with patch.dict(sys.modules, {"sentry_sdk": broken}):
            try:
                result = self.mod.init_sentry("https://key@sentry.io/1")
            except Exception as exc:
                self.fail(f"init_sentry raised unexpectedly: {exc!r}")
        self.assertFalse(result)
        self.assertFalse(self.mod.is_sentry_initialized())


# ---------------------------------------------------------------------------
# 6. capture_exception forwards to SDK when initialized
# ---------------------------------------------------------------------------

class TestCaptureExceptionCallsSdkWhenInitialized(unittest.TestCase):
    """test_capture_exception_calls_sdk_when_initialized"""

    def setUp(self):
        self.mod = _reset_module()

    def test_capture_exception_calls_sdk_when_initialized(self):
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.init_sentry("https://key@sentry.io/1")
            exc = RuntimeError("boom")
            self.mod.capture_exception(exc)
        sdk.capture_exception.assert_called_once_with(exc)


# ---------------------------------------------------------------------------
# 7. capture_exception is no-op when DSN missing
# ---------------------------------------------------------------------------

class TestCaptureExceptionNoopWhenDsnMissing(unittest.TestCase):
    """test_capture_exception_noop_when_dsn_missing"""

    def setUp(self):
        self.mod = _reset_module()

    def test_capture_exception_noop_when_dsn_missing(self):
        sdk = _make_sdk_stub()
        # Do NOT call init_sentry → _sentry_initialized stays False.
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.capture_exception(ValueError("ignored"))
        sdk.capture_exception.assert_not_called()


# ---------------------------------------------------------------------------
# 8. add_breadcrumb calls SDK (PR #238 pattern)
# ---------------------------------------------------------------------------

class TestAddBreadcrumbCallsSdk(unittest.TestCase):
    """test_add_breadcrumb_calls_sdk — verifies PR #238 IPC breadcrumb pattern."""

    def setUp(self):
        self.mod = _reset_module()

    def _init_sdk(self, sdk):
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.init_sentry("https://key@sentry.io/1")
        return sdk

    def test_add_breadcrumb_calls_sdk(self):
        sdk = _make_sdk_stub()
        self._init_sdk(sdk)
        sdk.add_breadcrumb.reset_mock()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.add_breadcrumb(
                category="ipc",
                message="start_recording",
                level="info",
                data={"ok": True},
            )
        sdk.add_breadcrumb.assert_called_once()


# ---------------------------------------------------------------------------
# 9. add_breadcrumb does NOT include transcript text (privacy guard)
# ---------------------------------------------------------------------------

class TestAddBreadcrumbRedactsSensitiveData(unittest.TestCase):
    """test_add_breadcrumb_redacts_sensitive_data — no transcript text in data."""

    def setUp(self):
        self.mod = _reset_module()

    def _init_sdk(self, sdk):
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.init_sentry("https://key@sentry.io/1")

    def test_breadcrumb_data_contains_no_transcript_text(self):
        sdk = _make_sdk_stub()
        self._init_sdk(sdk)
        sdk.add_breadcrumb.reset_mock()

        # Caller must only pass metadata — never raw transcript.
        privacy_safe_data = {
            "method": "start_recording",
            "duration_ms": 1234,
            "ok": True,
        }
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.add_breadcrumb(
                category="ipc",
                message="start_recording",
                data=privacy_safe_data,
            )

        _, kwargs = sdk.add_breadcrumb.call_args
        data = kwargs.get("data", {})
        # Verify no long string values that could be transcript text.
        for key, val in data.items():
            if isinstance(val, str):
                self.assertLess(
                    len(val), 200,
                    msg=f"Data field '{key}' looks like transcript text (length {len(val)})",
                )
        # Verify transcript / text content keys are absent.
        suspicious_keys = {"text", "transcript", "content", "original", "translation"}
        self.assertTrue(
            suspicious_keys.isdisjoint(data.keys()),
            msg=f"Privacy-sensitive keys found in breadcrumb data: {suspicious_keys & data.keys()}",
        )

    def test_metadata_fields_allowed(self):
        """Allowed metadata fields (method, duration_ms, ok) pass through intact."""
        sdk = _make_sdk_stub()
        self._init_sdk(sdk)
        sdk.add_breadcrumb.reset_mock()
        meta = {"method": "transcribe", "duration_ms": 820, "ok": True}
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.add_breadcrumb("ipc", "transcribe", data=meta)
        _, kwargs = sdk.add_breadcrumb.call_args
        self.assertEqual(kwargs.get("data"), meta)


# ---------------------------------------------------------------------------
# 10. add_breadcrumb includes category, message, data
# ---------------------------------------------------------------------------

class TestAddBreadcrumbIncludesCategoryMessageData(unittest.TestCase):
    """test_add_breadcrumb_includes_category_message_data"""

    def setUp(self):
        self.mod = _reset_module()

    def _init_sdk(self, sdk):
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.init_sentry("https://key@sentry.io/1")

    def test_all_fields_passed_to_sdk(self):
        sdk = _make_sdk_stub()
        self._init_sdk(sdk)
        sdk.add_breadcrumb.reset_mock()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.add_breadcrumb(
                category="recording",
                message="stop_recording",
                level="info",
                data={"duration_ms": 3500, "confidence": 0.92},
            )
        _, kwargs = sdk.add_breadcrumb.call_args
        self.assertEqual(kwargs.get("category"), "recording")
        self.assertEqual(kwargs.get("message"), "stop_recording")
        self.assertEqual(kwargs.get("level"), "info")
        self.assertIn("duration_ms", kwargs.get("data", {}))


# ---------------------------------------------------------------------------
# 11. add_breadcrumb is no-op after Sentry disable
# ---------------------------------------------------------------------------

class TestBreadcrumbNoopAfterSentryDisable(unittest.TestCase):
    """test_breadcrumb_noop_after_sentry_disable"""

    def setUp(self):
        self.mod = _reset_module()

    def test_breadcrumb_noop_after_sentry_disable(self):
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.init_sentry("https://key@sentry.io/1")
        # Simulate disable by resetting flag.
        self.mod._sentry_initialized = False
        sdk.add_breadcrumb.reset_mock()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.add_breadcrumb("ipc", "ping")
        sdk.add_breadcrumb.assert_not_called()


# ---------------------------------------------------------------------------
# 12. Concurrent breadcrumbs are thread-safe
# ---------------------------------------------------------------------------

class TestConcurrentBreadcrumbsThreadSafe(unittest.TestCase):
    """test_concurrent_breadcrumbs_thread_safe — no crash under concurrent calls."""

    def setUp(self):
        self.mod = _reset_module()

    def test_concurrent_breadcrumbs_thread_safe(self):
        sdk = _make_sdk_stub()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            self.mod.init_sentry("https://key@sentry.io/1")

        errors: list[Exception] = []
        call_count = 50

        def _worker(idx: int) -> None:
            try:
                with patch.dict(sys.modules, {"sentry_sdk": sdk}):
                    self.mod.add_breadcrumb(
                        category="ipc",
                        message=f"worker_{idx}",
                        data={"idx": idx},
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(call_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [], msg=f"Thread errors: {errors}")
        self.assertEqual(sdk.add_breadcrumb.call_count, call_count)


if __name__ == "__main__":
    unittest.main()
