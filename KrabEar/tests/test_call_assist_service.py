"""Unit-тесты для CallAssistService и VoiceGatewayClient."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_assist_service import CallAssistService, VoiceGatewayClient
from backend.state_store import StateStore


class FakeStore:
    """Минимальный фейк StateStore для тестов call assist."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self._settings = settings or {
            "voice_gateway_url": "http://127.0.0.1:8090",
            "voice_gateway_api_key": "test-key",
            "call_auto_summary": True,
            "call_notify_default": True,
        }
        self._history: list[Any] = []

    def load_settings(self) -> dict[str, Any]:
        return dict(self._settings)

    def save_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        self._settings = dict(settings)
        return self._settings

    def add_history_item(self, **kwargs: Any) -> Any:
        class FakeItem:
            def __init__(self, item_id: str):
                self.id = item_id
        item = FakeItem(f"hist_{len(self._history) + 1}")
        self._history.append(kwargs)
        return item


class FakeRecorder:
    """Минимальный фейк рекордера."""

    def __init__(self) -> None:
        self.is_recording = False

    def start(self) -> bool:
        self.is_recording = True
        return True


class BuildCallSummaryTestCase(unittest.TestCase):
    """Тесты для _build_call_summary_history_text — чистая функция."""

    def test_empty_payload(self) -> None:
        result = CallAssistService._build_call_summary_history_text({}, "sess-1")
        self.assertEqual(result, "")

    def test_summary_only(self) -> None:
        result = CallAssistService._build_call_summary_history_text(
            {"summary": "Обсудили план.", "tasks": []}, "sess-1"
        )
        self.assertIn("[Call Summary]", result)
        self.assertIn("Сессия: sess-1", result)
        self.assertIn("Кратко:", result)
        self.assertIn("Обсудили план.", result)
        self.assertNotIn("Задачи:", result)

    def test_tasks_only(self) -> None:
        result = CallAssistService._build_call_summary_history_text(
            {"summary": "", "tasks": ["Отправить", "Проверить"]}, "sess-2"
        )
        self.assertIn("Задачи:", result)
        self.assertIn("1. Отправить", result)
        self.assertIn("2. Проверить", result)
        self.assertNotIn("Кратко:", result)

    def test_summary_and_tasks(self) -> None:
        result = CallAssistService._build_call_summary_history_text(
            {
                "summary": "Итог.",
                "tasks": [
                    {"task": "Задача один"},
                    {"title": "Задача два"},
                    "Задача три",
                ],
            },
            "sess-3",
        )
        self.assertIn("Кратко:", result)
        self.assertIn("Итог.", result)
        self.assertIn("1. Задача один", result)
        self.assertIn("2. Задача два", result)
        self.assertIn("3. Задача три", result)

    def test_tasks_limited_to_12(self) -> None:
        tasks = [f"Задача {i}" for i in range(20)]
        result = CallAssistService._build_call_summary_history_text(
            {"summary": "s", "tasks": tasks}, ""
        )
        self.assertIn("12.", result)
        self.assertNotIn("13.", result)

    def test_no_session_id(self) -> None:
        result = CallAssistService._build_call_summary_history_text(
            {"summary": "Test"}, ""
        )
        self.assertNotIn("Сессия:", result)


class NormalizeTemplatesTestCase(unittest.TestCase):
    """Тесты для _normalize_templates — чистая функция."""

    def test_empty_list(self) -> None:
        self.assertEqual(CallAssistService._normalize_templates([]), [])

    def test_valid_templates(self) -> None:
        raw = [
            {"name": "Hello", "text": "Привет", "source_lang": "ru", "target_lang": "es"},
            {"name": "Bye", "text": "Пока"},
        ]
        result = CallAssistService._normalize_templates(raw)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "Hello")
        self.assertEqual(result[1]["source_lang"], "ru")  # default

    def test_filters_empty_entries(self) -> None:
        raw = [
            {"name": "", "text": "something"},
            {"name": "Valid", "text": ""},
            {"name": "Good", "text": "ok"},
        ]
        result = CallAssistService._normalize_templates(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Good")

    def test_non_dict_entries_skipped(self) -> None:
        raw = ["string", 42, {"name": "Ok", "text": "fine"}]
        result = CallAssistService._normalize_templates(raw)
        self.assertEqual(len(result), 1)

    def test_non_list_input(self) -> None:
        self.assertEqual(CallAssistService._normalize_templates("not a list"), [])
        self.assertEqual(CallAssistService._normalize_templates(None), [])


class CallAssistServiceStateTestCase(unittest.TestCase):
    """Тесты для state management без реальных HTTP-запросов."""

    def setUp(self) -> None:
        self.store = FakeStore()
        self.recorder = FakeRecorder()
        self.preview_reset_called = False
        self.preview_start_profile = ""

        def fake_reset():
            self.preview_reset_called = True

        def fake_start(qp: str):
            self.preview_start_profile = qp

        # Создаем gateway, который мокает start_session
        self.gateway = VoiceGatewayClient()
        self.gateway.start_session = lambda **kwargs: {  # type: ignore[method-assign]
            "ok": True,
            "session_id": "gw-test-1",
        }
        self.gateway.stop_session = lambda **kwargs: {"ok": True}  # type: ignore[method-assign]
        self.gateway.post = lambda **kwargs: {"ok": True, "payload": {}}  # type: ignore[method-assign]
        self.gateway.get = lambda **kwargs: {"ok": True, "payload": {}}  # type: ignore[method-assign]
        self.gateway.delete = lambda **kwargs: {"ok": True, "payload": {}}  # type: ignore[method-assign]

        self.service = CallAssistService(
            store=self.store,
            recorder=self.recorder,
            transcriber=None,
            gateway=self.gateway,
            reset_preview_fn=fake_reset,
            start_preview_fn=fake_start,
        )

    def test_initial_state_is_idle(self) -> None:
        state = self.service.state
        self.assertFalse(state["active"])
        self.assertEqual(state["status"], "idle")

    def test_start_activates_session(self) -> None:
        result = self.service.handle_start({"translation_mode": "auto_to_ru"})
        self.assertTrue(result["active"])
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["gateway_session_id"], "gw-test-1")
        self.assertTrue(self.preview_reset_called)
        self.assertEqual(self.preview_start_profile, "balanced")

    def test_stop_deactivates_session(self) -> None:
        self.service.handle_start({"translation_mode": "auto_to_ru"})
        result = self.service.handle_stop({"auto_summary": False})
        self.assertFalse(result["active"])
        self.assertEqual(result["status"], "stopped")

    def test_stop_when_idle_returns_idle(self) -> None:
        result = self.service.handle_stop({})
        self.assertFalse(result["active"])
        self.assertEqual(result["status"], "idle")

    def test_get_state_returns_copy(self) -> None:
        self.service.handle_start({"translation_mode": "auto_to_ru"})
        state = self.service.handle_get_state({})
        self.assertTrue(state["active"])
        # Мутация копии не влияет на оригинал
        state["active"] = False
        self.assertTrue(self.service.state["active"])

    def test_gateway_failure_sets_degraded(self) -> None:
        self.gateway.start_session = lambda **kwargs: {  # type: ignore[method-assign]
            "ok": False,
            "error": "connection_refused",
        }
        result = self.service.handle_start({})
        self.assertTrue(result["active"])
        self.assertEqual(result["gateway_status"], "degraded")
        self.assertIsNone(result["gateway_session_id"])


class DefaultCoerceBoolTestCase(unittest.TestCase):
    """Тесты для _default_coerce_bool."""

    def test_bool_values(self) -> None:
        self.assertTrue(CallAssistService._default_coerce_bool(True, False))
        self.assertFalse(CallAssistService._default_coerce_bool(False, True))

    def test_none_returns_default(self) -> None:
        self.assertTrue(CallAssistService._default_coerce_bool(None, True))
        self.assertFalse(CallAssistService._default_coerce_bool(None, False))

    def test_string_values(self) -> None:
        for truthy in ("1", "true", "on", "yes", "True", "YES"):
            self.assertTrue(CallAssistService._default_coerce_bool(truthy, False), f"Failed for {truthy}")
        for falsy in ("0", "false", "off", "no"):
            self.assertFalse(CallAssistService._default_coerce_bool(falsy, True), f"Failed for {falsy}")

    def test_int_values(self) -> None:
        self.assertTrue(CallAssistService._default_coerce_bool(1, False))
        self.assertFalse(CallAssistService._default_coerce_bool(0, True))

    def test_unknown_string_returns_default(self) -> None:
        self.assertTrue(CallAssistService._default_coerce_bool("maybe", True))
        self.assertFalse(CallAssistService._default_coerce_bool("maybe", False))


if __name__ == "__main__":
    unittest.main()
