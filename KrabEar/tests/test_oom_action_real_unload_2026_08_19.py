"""Кнопка тоста mlx.oom обязана РЕАЛЬНО выгружать модель, а не возвращать заглушку.

🔴 Живая находка 2026-08-19: `_kill_lm_studio_via_telegram` безусловно возвращал
``{"executed": False, "reason": "feature_disabled"}`` — ни одной ветки логики, ни одного
вызова наружу. Владелец жал кнопку «выгрузить», не происходило ровным счётом ничего.
Заявлено в мае (Phase B.1 «pending separate spec»), не реализовано ни разу.

При этом механизм выгрузки в проекте ЕСТЬ и работает: `lm_studio_lifecycle.unload_model_async`
(REST `/api/v0/models/{id}/unload` + CLI-фоллбэк `lms unload`). Он просто никогда не звался
из обработки ошибки — только при старте записи.

🔴 Гейт на чужую работу: выгружать нельзя вслепую. `brain_lease` — advisory-координация
доступа к одному Metal GPU между Krab Ear и Главным Крабом; если лизу держит ДРУГОЙ владелец,
выгрузка оборвала бы его inference. Направление отказа fail-safe: не уверены — не выгружаем.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.error_actions import handle_action  # noqa: E402
from backend.error_codes import ERROR_REGISTRY  # noqa: E402

_ACTION_ID = "unload_lm_studio_model"


def _settings_service(**overrides):
    """Фейк SettingsService: отдаёт ровно те ключи, что читает обработчик."""
    base = {
        "llm_brain_model": "qwen/qwen3.6-27b",
        "LLM_BASE_URL": "http://localhost:1234/v1",
    }
    base.update(overrides)
    svc = MagicMock()
    svc.cached_settings.return_value = base
    return svc


class OomActionRealUnloadTest(unittest.TestCase):
    def test_action_actually_requests_unload(self):
        """Обработчик обязан позвать реальную выгрузку, а не вернуть заглушку."""
        with patch("backend.error_actions.unload_model_async") as unload, \
                patch("backend.error_actions.current_lease_holder", return_value=None):
            result = handle_action(_ACTION_ID, settings_service=_settings_service())
        self.assertTrue(result["executed"], f"кнопка снова заглушка: {result}")
        self.assertNotEqual(result.get("reason"), "feature_disabled")
        unload.assert_called_once()
        _args, kwargs = unload.call_args
        called = f"{_args} {kwargs}"
        self.assertIn("qwen/qwen3.6-27b", called, "выгружается не та модель, что в настройках")

    def test_foreign_lease_holder_blocks_unload(self):
        """Лизу держит Главный Краб — его inference обрывать нельзя."""
        foreign = {"owner": "krab_main", "pid": 4242, "acquired_ts": 0, "exp_ts": 9e18}
        with patch("backend.error_actions.unload_model_async") as unload, \
                patch("backend.error_actions.current_lease_holder", return_value=foreign):
            result = handle_action(_ACTION_ID, settings_service=_settings_service())
        unload.assert_not_called()
        self.assertFalse(result["executed"])
        self.assertIn("lease", (result.get("reason") or "").lower())

    def test_own_lease_does_not_block(self):
        """Своя лиза — не препятствие: выгружаем свою же модель."""
        own = {"owner": "krab_ear", "pid": 1, "acquired_ts": 0, "exp_ts": 9e18}
        with patch("backend.error_actions.unload_model_async") as unload, \
                patch("backend.error_actions.current_lease_holder", return_value=own):
            result = handle_action(_ACTION_ID, settings_service=_settings_service())
        unload.assert_called_once()
        self.assertTrue(result["executed"])

    def test_missing_model_is_reported_not_silently_ignored(self):
        """Пустая модель в настройках — честный отказ, а не тихий no-op."""
        with patch("backend.error_actions.unload_model_async") as unload, \
                patch("backend.error_actions.current_lease_holder", return_value=None):
            result = handle_action(
                _ACTION_ID, settings_service=_settings_service(llm_brain_model=""),
            )
        unload.assert_not_called()
        self.assertFalse(result["executed"])
        self.assertTrue(result.get("reason"), "отказ обязан назвать причину")

    def test_registry_points_mlx_oom_at_this_action(self):
        """Реестр обязан ссылаться на живой обработчик, иначе кнопка снова осиротеет."""
        entry = ERROR_REGISTRY["mlx.oom"]
        self.assertTrue(entry["actionable"])
        self.assertEqual(entry["action_id"], _ACTION_ID)
        self.assertNotIn(
            "Telegram", entry["action_label"],
            "подпись обещает Telegram, которого в реализации нет",
        )


if __name__ == "__main__":
    unittest.main()
