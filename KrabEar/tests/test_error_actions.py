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

    def test_unload_lm_studio_model_actually_unloads(self):
        # 2026-08-19: заглушка feature_disabled заменена реальной выгрузкой.
        svc = MagicMock()
        svc.cached_settings.return_value = {
            "llm_brain_model": "qwen/qwen3.6-27b",
            # Реальный рантайм-ключ (нижний регистр) — см.
            # test_unload_lm_studio_model_uses_real_base_url_not_empty ниже.
            "llm_base_url": "http://localhost:1234/v1",
        }
        with patch("backend.error_actions.unload_model_async") as unload, \
                patch("backend.error_actions.current_lease_holder", return_value=None):
            result = handle_action("unload_lm_studio_model", settings_service=svc)
        unload.assert_called_once()
        self.assertTrue(result["executed"])

    def test_unload_lm_studio_model_uses_real_base_url_not_empty(self):
        """🔴 2026-08-20: тот же баг регистра ключа, что и в OomAutoRelief._relieve —
        этот хендлер обслуживает РУЧНУЮ кнопку «выгрузить» на тосте mlx.oom. Читал
        `settings.get("LLM_BASE_URL")` из рантайм-словаря, где ключ на деле
        `llm_base_url` (нижний, как в SettingsService/settings.json) — base_url
        всегда был "", `lm_studio_lifecycle` отказывался выгружать
        (`_scheme_allowed("")` == False). Владелец жал кнопку — ничего не
        происходило, и подтверждения о неудаче тоже не было (executed=True врёт).
        """
        distinctive_url = "http://127.0.0.1:1234/v1"
        svc = MagicMock()
        svc.cached_settings.return_value = {
            "llm_brain_model": "qwen/qwen3.6-27b",
            "llm_base_url": distinctive_url,
        }
        with patch("backend.error_actions.unload_model_async") as unload, \
                patch("backend.error_actions.current_lease_holder", return_value=None):
            handle_action("unload_lm_studio_model", settings_service=svc)
        unload.assert_called_once_with(distinctive_url, "qwen/qwen3.6-27b")

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


class CrossReferenceInvariantTests(unittest.TestCase):
    """Wave 51 — guards the contract between ERROR_REGISTRY (codes) and
    ACTION_HANDLERS (dispatch table). Catches drift early.
    """

    def test_every_actionable_code_has_handler(self):
        """Every action_id referenced in ERROR_REGISTRY must exist in
        ACTION_HANDLERS — otherwise clicking the toast yields
        `unknown action_id`."""
        from backend.error_codes import ERROR_REGISTRY

        referenced = {
            entry["action_id"]
            for entry in ERROR_REGISTRY.values()
            if entry.get("action_id")
        }
        registered = set(ACTION_HANDLERS.keys())
        missing = referenced - registered
        self.assertSetEqual(
            missing,
            set(),
            f"Action IDs referenced in ERROR_REGISTRY but missing from "
            f"ACTION_HANDLERS: {missing}. Add handler in error_actions.py.",
        )

    def test_no_orphan_handlers(self):
        """Every handler in ACTION_HANDLERS must be referenced by at least
        one ERROR_REGISTRY entry — otherwise it's dead code."""
        from backend.error_codes import ERROR_REGISTRY

        referenced = {
            entry["action_id"]
            for entry in ERROR_REGISTRY.values()
            if entry.get("action_id")
        }
        registered = set(ACTION_HANDLERS.keys())
        orphans = registered - referenced
        self.assertSetEqual(
            orphans,
            set(),
            f"Handlers in ACTION_HANDLERS but no code references them: "
            f"{orphans}. Either delete the handler or add a matching "
            f"ERROR_REGISTRY entry.",
        )

    def test_actionable_codes_have_non_empty_metadata(self):
        """Every actionable=True code in ERROR_REGISTRY must have a
        non-empty action_id AND non-empty action_label — otherwise
        the toast button renders blank."""
        from backend.error_codes import ERROR_REGISTRY

        bad = []
        for code, entry in ERROR_REGISTRY.items():
            if entry.get("actionable"):
                if not entry.get("action_id"):
                    bad.append(f"{code}: actionable=True but action_id empty")
                if not entry.get("action_label"):
                    bad.append(f"{code}: actionable=True but action_label empty")
        self.assertEqual(
            bad,
            [],
            "Actionable codes missing UI metadata:\n  " + "\n  ".join(bad),
        )
