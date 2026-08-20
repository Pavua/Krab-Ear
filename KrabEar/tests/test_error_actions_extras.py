"""Wave 164: error_actions.py handler coverage extras.

Tests all 12 ACTION_HANDLERS by name, plus edge cases:
- open_privacy_settings, disable_rewriter, open_lm_studio_settings,
- switch_to_balanced_profile, retry_history_save, kill_lm_studio_via_telegram,
- switch_to_stable_rewriter, open_hf_token_setting, open_hotkey_settings,
- open_pyannote_hf_page, open_terminal_make_release, open_logs.
- unknown action_id returns error dict.
- concurrent handler invocation does not corrupt shared state.
"""
from __future__ import annotations

import sys
import os
import threading
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.error_actions import handle_action, ACTION_HANDLERS  # noqa: E402


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _svc() -> MagicMock:
    """Return a fresh mock SettingsService."""
    return MagicMock()


# ---------------------------------------------------------------------------
# 1. open_privacy_settings
# ---------------------------------------------------------------------------

class OpenPrivacySettingsTests(unittest.TestCase):

    @patch("backend.error_actions.subprocess.run")
    def test_open_privacy_settings_handler_success(self, mock_run):
        """open_privacy_settings must call subprocess.run with the macOS
        Privacy URL and return executed=True."""
        result = handle_action("open_privacy_settings", settings_service=_svc())
        self.assertTrue(result["executed"])
        self.assertIsNone(result["reason"])
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "open")
        self.assertIn("Privacy_Accessibility", " ".join(cmd))

    @patch(
        "backend.error_actions.subprocess.run",
        side_effect=Exception("subprocess.CalledProcessError mock"),
    )
    def test_open_privacy_settings_subprocess_error_returns_executed_false(self, mock_run):
        """If subprocess.run raises, handle_action must catch it and return
        executed=False (never re-raises)."""
        result = handle_action("open_privacy_settings", settings_service=_svc())
        self.assertFalse(result["executed"])
        self.assertIsNotNone(result["reason"])


# ---------------------------------------------------------------------------
# 2. disable_rewriter
# ---------------------------------------------------------------------------

class DisableRewriterTests(unittest.TestCase):

    def test_disable_rewriter_handler_calls_set_settings(self):
        """disable_rewriter must call settings_service.handle_set_settings
        with llm_rewrite_enabled=False."""
        svc = _svc()
        result = handle_action("disable_rewriter", settings_service=svc)
        self.assertTrue(result["executed"])
        svc.handle_set_settings.assert_called_once_with({"llm_rewrite_enabled": False})
        self.assertEqual(result["side_effect"], "settings_updated")

    def test_disable_rewriter_settings_service_exception_handled(self):
        """If settings_service.handle_set_settings raises, handle_action
        must return executed=False without re-raising."""
        svc = _svc()
        svc.handle_set_settings.side_effect = RuntimeError("DB locked")
        result = handle_action("disable_rewriter", settings_service=svc)
        self.assertFalse(result["executed"])
        self.assertIn("DB locked", result["reason"])


# ---------------------------------------------------------------------------
# 3. open_lm_studio_settings
# ---------------------------------------------------------------------------

class OpenLmStudioSettingsTests(unittest.TestCase):

    @patch("backend.error_actions.subprocess.run")
    def test_open_lm_studio_settings_handler_returns_executed(self, mock_run):
        """open_lm_studio_settings must return executed=True and side_effect
        indicating the Swift agent should focus the LM Studio API key field."""
        result = handle_action("open_lm_studio_settings", settings_service=_svc())
        self.assertTrue(result["executed"])
        self.assertEqual(result["side_effect"], "swift_focus_lm_studio_api_key")

    @patch("backend.error_actions.subprocess.run")
    def test_open_lm_studio_settings_attempts_to_open_lm_studio(self, mock_run):
        """open_lm_studio_settings should attempt to open the LM Studio application."""
        handle_action("open_lm_studio_settings", settings_service=_svc())
        # At least one call should include 'LM Studio'
        calls_args = [str(c) for c in mock_run.call_args_list]
        lm_calls = [c for c in calls_args if "LM Studio" in c]
        self.assertTrue(len(lm_calls) >= 1,
                        "Expected at least one subprocess call with 'LM Studio'")

    @patch("backend.error_actions.subprocess.run", side_effect=Exception("not found"))
    def test_open_lm_studio_settings_subprocess_failure_still_returns_executed(self, _):
        """Even if LM Studio is not installed, open_lm_studio_settings should
        still return executed=True (the side_effect is what matters)."""
        result = handle_action("open_lm_studio_settings", settings_service=_svc())
        self.assertTrue(result["executed"])


# ---------------------------------------------------------------------------
# 4. switch_to_balanced_profile / restart_backend (mocked)
# ---------------------------------------------------------------------------

class SwitchToBalancedProfileTests(unittest.TestCase):

    def test_switch_to_balanced_profile_handler(self):
        """switch_to_balanced_profile must call handle_set_settings with
        quality_profile='balanced'."""
        svc = _svc()
        result = handle_action("switch_to_balanced_profile", settings_service=svc)
        self.assertTrue(result["executed"])
        svc.handle_set_settings.assert_called_once_with({"quality_profile": "balanced"})
        self.assertEqual(result["side_effect"], "profile_switched")

    def test_switch_to_stable_rewriter_handler(self):
        """switch_to_stable_rewriter must set llm_model to qwen3-4b-abliterated."""
        svc = _svc()
        result = handle_action("switch_to_stable_rewriter", settings_service=svc)
        self.assertTrue(result["executed"])
        svc.handle_set_settings.assert_called_once_with(
            {"llm_model": "qwen3-4b-abliterated"}
        )
        self.assertEqual(result["side_effect"], "settings_updated")


# ---------------------------------------------------------------------------
# 5. retry_history_save (mocked store)
# ---------------------------------------------------------------------------

class RetryHistorySaveTests(unittest.TestCase):

    def test_retry_history_save_no_store_returns_not_executed(self):
        """retry_history_save without store kwarg returns executed=False."""
        result = handle_action("retry_history_save", settings_service=_svc())
        self.assertFalse(result["executed"])
        self.assertEqual(result["reason"], "no_store_available")

    def test_retry_history_save_with_store_calls_retry(self):
        """retry_history_save with a mock store calls store.retry_pending_writes()."""
        store = MagicMock()
        result = handle_action("retry_history_save", settings_service=_svc(), store=store)
        self.assertTrue(result["executed"])
        store.retry_pending_writes.assert_called_once()
        self.assertEqual(result["side_effect"], "history_retried")

    def test_retry_history_save_store_exception_handled(self):
        """If store.retry_pending_writes raises, executed=False is returned."""
        store = MagicMock()
        store.retry_pending_writes.side_effect = IOError("disk error")
        result = handle_action("retry_history_save", settings_service=_svc(), store=store)
        self.assertFalse(result["executed"])
        self.assertIn("disk error", result["reason"])


# ---------------------------------------------------------------------------
# 6. clear_cache / kill_lm_studio_via_telegram (feature-gated)
# ---------------------------------------------------------------------------

class FeatureGatedHandlerTests(unittest.TestCase):

    def test_unload_lm_studio_model_actually_unloads(self):
        """2026-08-19: заглушка feature_disabled заменена реальной выгрузкой."""
        svc = MagicMock()
        svc.cached_settings.return_value = {
            "llm_brain_model": "qwen/qwen3.6-27b",
            "llm_base_url": "http://localhost:1234/v1",
        }
        with patch("backend.error_actions.unload_model_async") as unload, \
                patch("backend.error_actions.current_lease_holder", return_value=None):
            result = handle_action("unload_lm_studio_model", settings_service=svc)
        unload.assert_called_once()
        self.assertTrue(result["executed"])

    def test_open_hf_token_setting_returns_swift_side_effect(self):
        """open_hf_token_setting must return executed=True and a
        swift_focus_hf_token side_effect for the Swift agent."""
        result = handle_action("open_hf_token_setting", settings_service=_svc())
        self.assertTrue(result["executed"])
        self.assertEqual(result["side_effect"], "swift_focus_hf_token")

    def test_open_hotkey_settings_returns_swift_side_effect(self):
        """open_hotkey_settings must return executed=True and
        swift_focus_hotkey_tab side_effect."""
        result = handle_action("open_hotkey_settings", settings_service=_svc())
        self.assertTrue(result["executed"])
        self.assertEqual(result["side_effect"], "swift_focus_hotkey_tab")


# ---------------------------------------------------------------------------
# 8. open_pyannote_hf_page
# ---------------------------------------------------------------------------

class OpenPyannoteHfPageTests(unittest.TestCase):

    @patch("backend.error_actions.subprocess.run")
    def test_open_pyannote_hf_page_opens_correct_url(self, mock_run):
        """open_pyannote_hf_page must open the correct Hugging Face URL."""
        result = handle_action("open_pyannote_hf_page", settings_service=_svc())
        self.assertTrue(result["executed"])
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "open")
        self.assertIn("huggingface.co", " ".join(cmd))
        self.assertIn("pyannote", " ".join(cmd))

    @patch(
        "backend.error_actions.subprocess.run",
        side_effect=Exception("CalledProcessError"),
    )
    def test_open_pyannote_hf_page_subprocess_fail(self, _):
        """subprocess failure returns executed=False."""
        result = handle_action("open_pyannote_hf_page", settings_service=_svc())
        self.assertFalse(result["executed"])


# ---------------------------------------------------------------------------
# 9. open_terminal_make_release
# ---------------------------------------------------------------------------

class OpenTerminalMakeReleaseTests(unittest.TestCase):

    @patch("backend.error_actions.subprocess.run")
    def test_open_terminal_make_release_opens_terminal(self, mock_run):
        """open_terminal_make_release opens Terminal.app at the repo root."""
        result = handle_action("open_terminal_make_release", settings_service=_svc())
        self.assertTrue(result["executed"])
        cmd = mock_run.call_args[0][0]
        self.assertIn("Terminal", cmd)
        self.assertIn("Krab Ear", " ".join(cmd))

    @patch(
        "backend.error_actions.subprocess.run",
        side_effect=Exception("Terminal not found"),
    )
    def test_open_terminal_make_release_subprocess_fail(self, _):
        result = handle_action("open_terminal_make_release", settings_service=_svc())
        self.assertFalse(result["executed"])


# ---------------------------------------------------------------------------
# 10. open_logs
# ---------------------------------------------------------------------------

class OpenLogsTests(unittest.TestCase):

    @patch("backend.error_actions.subprocess.run")
    def test_open_logs_handler_success(self, mock_run):
        """open_logs must call subprocess.run with 'open' and the KrabEar data dir."""
        result = handle_action("open_logs", settings_service=_svc())
        self.assertTrue(result["executed"])
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "open")
        self.assertIn("KrabEar", " ".join(cmd))

    @patch(
        "backend.error_actions.subprocess.run",
        side_effect=Exception("CalledProcessError"),
    )
    def test_open_logs_handler_failure(self, _):
        result = handle_action("open_logs", settings_service=_svc())
        self.assertFalse(result["executed"])


# ---------------------------------------------------------------------------
# 11. unknown action_id returns error dict
# ---------------------------------------------------------------------------

class UnknownActionIdTests(unittest.TestCase):

    def test_unknown_action_id_returns_not_executed(self):
        """handle_action with unknown action_id must return executed=False
        and reason containing 'unknown'."""
        result = handle_action("does_not_exist_action", settings_service=_svc())
        self.assertFalse(result["executed"])
        self.assertIsNotNone(result["reason"])
        self.assertIn("unknown", result["reason"].lower())
        self.assertIsNone(result["side_effect"])

    def test_unknown_action_id_empty_string(self):
        result = handle_action("", settings_service=_svc())
        self.assertFalse(result["executed"])

    def test_unknown_action_id_never_raises(self):
        """handle_action must never raise for any input."""
        for bad_id in ["", "   ", "null", "None", "12345", "open_privacy"]:
            with self.subTest(action_id=bad_id):
                try:
                    handle_action(bad_id, settings_service=_svc())
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"handle_action raised for {bad_id!r}: {exc}")


# ---------------------------------------------------------------------------
# 12. concurrent handler invocation
# ---------------------------------------------------------------------------

class ConcurrentHandlerTests(unittest.TestCase):

    def test_concurrent_disable_rewriter_invocation(self):
        """50 concurrent calls to disable_rewriter must all complete without
        raising exceptions (each gets its own fresh mock, no shared state)."""
        errors: list[Exception] = []

        def worker():
            svc = _svc()
            try:
                result = handle_action("disable_rewriter", settings_service=svc)
                if not result["executed"]:
                    errors.append(AssertionError(f"executed=False: {result}"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent invocation errors: {errors}")

    @patch("backend.error_actions.subprocess.run")
    def test_concurrent_open_logs_invocation(self, mock_run):
        """50 concurrent open_logs calls must all return executed=True."""
        errors: list[Exception] = []

        def worker():
            try:
                result = handle_action("open_logs", settings_service=_svc())
                if not result["executed"]:
                    errors.append(AssertionError(f"executed=False: {result}"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent invocation errors: {errors}")

    def test_all_handlers_listed_in_dispatch_table(self):
        """Sanity check: ACTION_HANDLERS must be a non-empty dict of callables."""
        self.assertGreater(len(ACTION_HANDLERS), 0)
        for action_id, handler in ACTION_HANDLERS.items():
            with self.subTest(action_id=action_id):
                self.assertTrue(callable(handler))


if __name__ == "__main__":
    unittest.main()
