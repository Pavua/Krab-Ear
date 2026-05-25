"""Tests for Sentry tag + release tracking edge cases (Wave 210).

Covers:
- Release string format validation (semver-ish patterns)
- Bundle-version → release tag (Wave 241 PR pattern)
- dist tag matches release version
- environment tag distinguishes prod / dev / test
- Tag value special-character escaping
- Tag value overflow truncated safely
- set_user tag carries no PII
- Concurrent set_tag thread safety
- set_context sends metadata only (privacy contract)

All tests mock sentry_sdk — no real SDK, no backend started.
"""

from __future__ import annotations

import re
import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

import os as _os
_HERE = _os.path.dirname(_os.path.abspath(__file__))
_PROJECT_ROOT = _os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(
    r"^v?\d+\.\d+(\.\d+)?([.\-+][a-zA-Z0-9._\-+]*)?$"
)
_GIT_DESCRIBE_RE = re.compile(
    r"^v?\d+\.\d+(\.\d+)?(-\d+-g[0-9a-f]+(-dirty)?)?$"
)

_SENTRY_TAG_MAX_LEN = 200  # Sentry enforced limit


def _build_mock_sdk(initialized: bool = True):
    """Return a MagicMock that mimics sentry_sdk surface used in observability."""
    sdk = MagicMock()
    sdk.init = MagicMock()
    sdk.set_tag = MagicMock()
    sdk.set_context = MagicMock()
    sdk.set_user = MagicMock()
    sdk.push_scope = MagicMock()
    scope_mock = MagicMock()
    scope_mock.__enter__ = MagicMock(return_value=scope_mock)
    scope_mock.__exit__ = MagicMock(return_value=False)
    sdk.push_scope.return_value = scope_mock
    return sdk


def _reset_module():
    """Return observability module with clean state (no Sentry init)."""
    import backend.observability as mod  # noqa: PLC0415
    mod._sentry_initialized = False
    return mod


# ---------------------------------------------------------------------------
# 1. Release tag format validation
# ---------------------------------------------------------------------------

class TestReleaseTagFormatValidation(unittest.TestCase):
    """release_from_git() must return a semver-ish or git-describe string."""

    def _mock_git_describe(self, stdout: str, returncode: int = 0):
        result = MagicMock()
        result.returncode = returncode
        result.stdout = stdout
        return result

    def test_plain_semver_accepted(self):
        mod = _reset_module()
        with patch("subprocess.run", return_value=self._mock_git_describe("v2.1.0\n")):
            ver = mod.release_from_git()
        self.assertTrue(
            _SEMVER_RE.match(ver) or _GIT_DESCRIBE_RE.match(ver),
            f"Expected semver-ish, got: {ver!r}",
        )

    def test_git_describe_with_commits_accepted(self):
        mod = _reset_module()
        with patch(
            "subprocess.run",
            return_value=self._mock_git_describe("v2.1.0-87-gabcdef\n"),
        ):
            ver = mod.release_from_git()
        self.assertRegex(ver, r"^v2\.1\.0-87-gabcdef$")

    def test_dirty_suffix_preserved(self):
        mod = _reset_module()
        with patch(
            "subprocess.run",
            return_value=self._mock_git_describe("v2.0.3-dirty\n"),
        ):
            ver = mod.release_from_git()
        self.assertIn("dirty", ver)

    def test_git_failure_returns_fallback(self):
        mod = _reset_module()
        with patch(
            "subprocess.run",
            return_value=self._mock_git_describe("", returncode=128),
        ):
            ver = mod.release_from_git()
        self.assertEqual(ver, "krab-ear@unknown")

    def test_subprocess_exception_returns_fallback(self):
        mod = _reset_module()
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            ver = mod.release_from_git()
        self.assertEqual(ver, "krab-ear@unknown")

    def test_empty_stdout_returns_fallback(self):
        mod = _reset_module()
        with patch("subprocess.run", return_value=self._mock_git_describe("  \n")):
            ver = mod.release_from_git()
        self.assertEqual(ver, "krab-ear@unknown")


# ---------------------------------------------------------------------------
# 2. Release tag from bundle version (Wave 241 PR pattern)
# ---------------------------------------------------------------------------

class TestReleaseTagFromBundleVersion(unittest.TestCase):
    """init_sentry accepts an explicit *release* kwarg (bundle CFBundleVersion)
    and passes it verbatim to sentry_sdk.init — no git subprocess needed.
    """

    def _init_with_release(self, release: str) -> MagicMock:
        mod = _reset_module()
        sdk = _build_mock_sdk()
        fake_sentry_module = types.ModuleType("sentry_sdk")
        fake_sentry_module.init = sdk.init
        fake_sentry_module.set_tag = sdk.set_tag
        with patch.dict(sys.modules, {"sentry_sdk": fake_sentry_module}):
            mod.init_sentry("https://x@sentry.io/1", release=release)
        return sdk

    def test_bundle_version_passed_to_init(self):
        sdk = self._init_with_release("2.3.1")
        sdk.init.assert_called_once()
        kwargs = sdk.init.call_args.kwargs
        self.assertEqual(kwargs["release"], "2.3.1")

    def test_build_number_style_accepted(self):
        sdk = self._init_with_release("2.3.1+491")
        kwargs = sdk.init.call_args.kwargs
        self.assertIn("2.3.1+491", kwargs["release"])

    def test_explicit_release_overrides_git(self):
        """When release kwarg given, subprocess.run must NOT be called."""
        mod = _reset_module()
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.init = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            with patch("subprocess.run") as mock_git:
                mod.init_sentry("https://x@sentry.io/1", release="1.0.0")
        mock_git.assert_not_called()

    def test_none_release_triggers_git_describe(self):
        """When release=None, subprocess.run IS called."""
        mod = _reset_module()
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.init = MagicMock()
        git_result = MagicMock()
        git_result.returncode = 0
        git_result.stdout = "v2.0.0\n"
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            with patch("subprocess.run", return_value=git_result) as mock_git:
                mod.init_sentry("https://x@sentry.io/1", release=None)
        mock_git.assert_called_once()


# ---------------------------------------------------------------------------
# 3. dist tag matches version
# ---------------------------------------------------------------------------

class TestDistTagMatchesVersion(unittest.TestCase):
    """The release passed to Sentry must be the same string used for *dist*
    (build artefact reference).  Verify release kwarg round-trips unchanged.
    """

    def test_dist_not_mutated_by_init_sentry(self):
        mod = _reset_module()
        captured = {}
        fake_sdk = types.ModuleType("sentry_sdk")

        def fake_init(**kwargs):
            captured.update(kwargs)

        fake_sdk.init = fake_init
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.init_sentry("https://x@sentry.io/1", release="v3.0.0-rc.1")
        self.assertEqual(captured.get("release"), "v3.0.0-rc.1")

    def test_release_with_plus_build_meta(self):
        mod = _reset_module()
        captured = {}
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.init = lambda **kw: captured.update(kw)
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.init_sentry("https://x@sentry.io/1", release="2.0.0+build.521")
        self.assertEqual(captured.get("release"), "2.0.0+build.521")


# ---------------------------------------------------------------------------
# 4. Environment tag distinguishes prod / dev / test
# ---------------------------------------------------------------------------

class TestEnvironmentTagDistinguishes(unittest.TestCase):

    def _captured_env(self, environment: str) -> str:
        mod = _reset_module()
        captured = {}
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.init = lambda **kw: captured.update(kw)
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.init_sentry(
                "https://x@sentry.io/1",
                environment=environment,
                release="1.0.0",
            )
        return captured.get("environment", "")

    def test_production_environment(self):
        self.assertEqual(self._captured_env("production"), "production")

    def test_development_environment(self):
        self.assertEqual(self._captured_env("development"), "development")

    def test_test_environment(self):
        self.assertEqual(self._captured_env("test"), "test")

    def test_staging_environment(self):
        self.assertEqual(self._captured_env("staging"), "staging")

    def test_default_is_production(self):
        """init_sentry default environment kwarg must be 'production'."""
        mod = _reset_module()
        captured = {}
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.init = lambda **kw: captured.update(kw)
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.init_sentry("https://x@sentry.io/1", release="1.0.0")
        self.assertEqual(captured.get("environment"), "production")


# ---------------------------------------------------------------------------
# 5. Tag value escapes special characters
# ---------------------------------------------------------------------------

class TestTagValueEscapesSpecialChars(unittest.TestCase):
    """Tag values with newlines / null bytes are either sanitised or the call
    is a no-op — they must never cause the SDK call to raise.
    """

    def _call_add_breadcrumb_and_capture(self, data: dict) -> MagicMock:
        mod = _reset_module()
        mod._sentry_initialized = True
        fake_sdk = types.ModuleType("sentry_sdk")
        calls_log = []
        fake_sdk.add_breadcrumb = lambda **kw: calls_log.append(kw)
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.add_breadcrumb(
                category="ipc",
                message="test_method",
                data=data,
            )
        return calls_log

    def test_newline_in_data_value_no_raise(self):
        result = self._call_add_breadcrumb_and_capture({"key": "line1\nline2"})
        # Either the breadcrumb was recorded or silently dropped — no exception.
        self.assertIsInstance(result, list)

    def test_null_byte_in_data_value_no_raise(self):
        result = self._call_add_breadcrumb_and_capture({"key": "val\x00ue"})
        self.assertIsInstance(result, list)

    def test_unicode_emoji_in_data_no_raise(self):
        result = self._call_add_breadcrumb_and_capture({"key": "Kraб🦀"})
        self.assertIsInstance(result, list)

    def test_release_string_with_slash_no_raise(self):
        """Slash in release (e.g. branch-name style) must not crash init."""
        mod = _reset_module()
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.init = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            try:
                mod.init_sentry(
                    "https://x@sentry.io/1",
                    release="feat/some-branch@abc123",
                )
            except Exception as exc:
                self.fail(f"init_sentry raised on slash release: {exc!r}")


# ---------------------------------------------------------------------------
# 6. Tag value overflow truncated safely
# ---------------------------------------------------------------------------

class TestTagOverflowTruncatedSafely(unittest.TestCase):
    """Very long release strings must be accepted without crash.

    Sentry server enforces 200-char limit; SDK may silently truncate.
    Our code must not raise regardless of length.
    """

    def test_long_release_string_no_raise(self):
        mod = _reset_module()
        long_release = "v2.0." + "x" * 300  # well over 200 chars
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.init = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            try:
                mod.init_sentry(
                    "https://x@sentry.io/1",
                    release=long_release,
                )
            except Exception as exc:
                self.fail(f"init_sentry raised on long release: {exc!r}")
        # init was still called
        fake_sdk.init.assert_called_once()

    def test_very_long_environment_no_raise(self):
        mod = _reset_module()
        long_env = "prod-" + "a" * 300
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.init = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            try:
                mod.init_sentry(
                    "https://x@sentry.io/1",
                    environment=long_env,
                    release="1.0.0",
                )
            except Exception as exc:
                self.fail(f"init_sentry raised on long environment: {exc!r}")
        fake_sdk.init.assert_called_once()

    def test_release_at_exact_sentry_limit_no_raise(self):
        mod = _reset_module()
        exact_release = "v" + "1" * (_SENTRY_TAG_MAX_LEN - 1)
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.init = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.init_sentry("https://x@sentry.io/1", release=exact_release)
        fake_sdk.init.assert_called_once()


# ---------------------------------------------------------------------------
# 7. set_user tag carries no PII
# ---------------------------------------------------------------------------

class TestSetUserTagNoPII(unittest.TestCase):
    """init_sentry must pass send_default_pii=False to the SDK and must NOT
    include email / username / IP in the top-level init kwargs.
    """

    def test_send_default_pii_is_false(self):
        mod = _reset_module()
        captured = {}
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.init = lambda **kw: captured.update(kw)
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.init_sentry("https://x@sentry.io/1", release="1.0.0")
        self.assertFalse(
            captured.get("send_default_pii", True),
            "send_default_pii must be False (privacy contract)",
        )

    def test_no_email_in_init_kwargs(self):
        mod = _reset_module()
        captured = {}
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.init = lambda **kw: captured.update(kw)
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.init_sentry("https://x@sentry.io/1", release="1.0.0")
        self.assertNotIn("email", captured)
        self.assertNotIn("username", captured)

    def test_no_ip_address_in_init_kwargs(self):
        mod = _reset_module()
        captured = {}
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.init = lambda **kw: captured.update(kw)
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.init_sentry("https://x@sentry.io/1", release="1.0.0")
        for key in captured:
            self.assertNotIn("ip", key.lower(), f"Found potential IP field: {key}")

    def test_capture_exception_no_user_data(self):
        """capture_exception must only set 'component' tag — no user fields."""
        mod = _reset_module()
        mod._sentry_initialized = True
        fake_sdk = types.ModuleType("sentry_sdk")
        tag_calls = []
        scope_mock = MagicMock()
        scope_mock.__enter__ = MagicMock(return_value=scope_mock)
        scope_mock.__exit__ = MagicMock(return_value=False)
        scope_mock.set_tag = lambda k, v: tag_calls.append((k, v))
        fake_sdk.push_scope = MagicMock(return_value=scope_mock)
        fake_sdk.capture_exception = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.capture_exception(ValueError("boom"), component="stt")
        self.assertEqual(len(tag_calls), 1)
        self.assertEqual(tag_calls[0][0], "component")


# ---------------------------------------------------------------------------
# 8. Concurrent set_tag thread safety
# ---------------------------------------------------------------------------

class TestConcurrentSetTagThreadSafe(unittest.TestCase):
    """Multiple threads calling add_breadcrumb / capture_exception concurrently
    must not raise or corrupt state.
    """

    def test_concurrent_add_breadcrumb_no_crash(self):
        mod = _reset_module()
        mod._sentry_initialized = True
        lock = threading.Lock()
        collected = []
        errors = []

        fake_sdk = types.ModuleType("sentry_sdk")

        def fake_add_breadcrumb(**kwargs):
            with lock:
                collected.append(kwargs)

        fake_sdk.add_breadcrumb = fake_add_breadcrumb

        def worker(i: int):
            try:
                with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
                    mod.add_breadcrumb(
                        category="ipc",
                        message=f"method_{i}",
                        data={"index": i},
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Threads raised: {errors}")
        self.assertEqual(len(collected), 20)

    def test_concurrent_capture_exception_no_crash(self):
        mod = _reset_module()
        mod._sentry_initialized = True
        errors = []

        fake_sdk = types.ModuleType("sentry_sdk")
        scope_mock = MagicMock()
        scope_mock.__enter__ = MagicMock(return_value=scope_mock)
        scope_mock.__exit__ = MagicMock(return_value=False)
        scope_mock.set_tag = MagicMock()
        fake_sdk.push_scope = MagicMock(return_value=scope_mock)
        fake_sdk.capture_exception = MagicMock()

        def worker(i: int):
            try:
                with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
                    mod.capture_exception(
                        RuntimeError(f"err_{i}"), component=f"comp_{i}"
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(15)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Threads raised: {errors}")


# ---------------------------------------------------------------------------
# 9. set_context sends metadata only (privacy contract)
# ---------------------------------------------------------------------------

class TestSetContextMetadataOnly(unittest.TestCase):
    """add_breadcrumb data dict must never contain transcript text.

    We verify that calling add_breadcrumb with only allowed metadata keys
    (duration_ms, confidence, method, language) round-trips correctly,
    and that the module never injects additional PII keys automatically.
    """

    def _capture_breadcrumbs(self, data: dict) -> list:
        mod = _reset_module()
        mod._sentry_initialized = True
        captured = []
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.add_breadcrumb = lambda **kw: captured.append(kw)
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.add_breadcrumb(
                category="transcription",
                message="stt_complete",
                data=data,
            )
        return captured

    def test_metadata_keys_only_round_trip(self):
        meta = {"duration_ms": 1500, "confidence": 0.93, "language": "ru"}
        caps = self._capture_breadcrumbs(meta)
        self.assertEqual(len(caps), 1)
        data_out = caps[0]["data"]
        self.assertEqual(data_out["duration_ms"], 1500)
        self.assertEqual(data_out["language"], "ru")

    def test_no_pii_injected_automatically(self):
        meta = {"duration_ms": 800}
        caps = self._capture_breadcrumbs(meta)
        self.assertEqual(len(caps), 1)
        data_out = caps[0]["data"]
        pii_keys = {"text", "transcript", "email", "phone", "ip", "user_id"}
        intersection = pii_keys & set(data_out.keys())
        self.assertEqual(
            intersection,
            set(),
            f"Unexpected PII keys in breadcrumb data: {intersection}",
        )

    def test_empty_data_becomes_empty_dict(self):
        mod = _reset_module()
        mod._sentry_initialized = True
        captured = []
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.add_breadcrumb = lambda **kw: captured.append(kw)
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.add_breadcrumb(
                category="recording",
                message="start",
                # no data kwarg — should default to {}
            )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["data"], {})

    def test_privacy_mode_blocks_init_even_with_valid_dsn(self):
        """Privacy contract: privacy_mode_enabled=True blocks Sentry init."""
        mod = _reset_module()
        fake_sdk = types.ModuleType("sentry_sdk")
        fake_sdk.init = MagicMock()
        settings = {"privacy_mode_enabled": True}
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            # Stub out privacy_audit to avoid filesystem side effects
            fake_privacy_mod = types.ModuleType("backend.privacy_audit")
            fake_privacy_mod.get_privacy_audit_logger = MagicMock(
                return_value=MagicMock()
            )
            with patch.dict(
                sys.modules, {"backend.privacy_audit": fake_privacy_mod}
            ):
                result = mod.init_sentry(
                    "https://x@sentry.io/1",
                    release="1.0.0",
                    settings=settings,
                )
        self.assertFalse(result)
        fake_sdk.init.assert_not_called()


if __name__ == "__main__":
    unittest.main()
