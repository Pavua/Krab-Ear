import unittest
from unittest.mock import MagicMock, patch

from backend.error_actions import handle_action, ACTION_HANDLERS


class ActionDispatcherTests(unittest.TestCase):
    def test_unknown_action_returns_error(self):
        result = handle_action("nonexistent_action", settings_service=MagicMock())
        self.assertFalse(result["executed"])
        self.assertIn("unknown", result["reason"].lower())

    def test_disable_rewriter_writes_settings(self):
        settings_service = MagicMock()
        result = handle_action("disable_rewriter", settings_service=settings_service)
        self.assertTrue(result["executed"])
        settings_service.handle_set_settings.assert_called_once()
        call_args = settings_service.handle_set_settings.call_args
        params = call_args[0][0] if call_args[0] else call_args[1]
        self.assertEqual(params.get("llm_rewrite_enabled"), False)

    def test_kill_lm_studio_via_telegram_feature_disabled(self):
        # B.1: feature flag default False — should return feature_disabled
        result = handle_action("kill_lm_studio_via_telegram", settings_service=MagicMock())
        self.assertFalse(result["executed"])
        self.assertEqual(result["reason"], "feature_disabled")

    @patch("backend.error_actions.subprocess.run")
    def test_open_privacy_settings_invokes_subprocess(self, mock_run):
        result = handle_action("open_privacy_settings", settings_service=MagicMock())
        self.assertTrue(result["executed"])
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "open")
        self.assertIn("Privacy", " ".join(cmd))

    def test_all_registered_action_ids_callable(self):
        for action_id in ACTION_HANDLERS:
            with self.subTest(action_id=action_id):
                self.assertTrue(callable(ACTION_HANDLERS[action_id]))
