"""Интеграционные тесты команд backend-сервиса Krab Ear."""

from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile
import time
import unittest

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.service import BackendService
from backend.state_store import StateStore
from backend.translator import TranslationResult


class FakeRecorder:
    """Фейковый рекордер для детерминированных тестов сервиса."""

    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000
        self._snapshot_counter = 0

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self):
        if not self.is_recording:
            return None
        self.is_recording = False
        return np.ones(1600, dtype=np.float32), 1.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        self._snapshot_counter += 1
        return np.ones(32000, dtype=np.float32), float(self._snapshot_counter)


class FakeTranscriber:
    """Фейковый transcriber, генерирующий последовательные строки."""

    def __init__(self) -> None:
        self.counter = 0
        self.preview_counter = 0

    def transcribe(self, audio_data, quality_profile: str, cleanup_profile: str = "soft") -> str:
        self.counter += 1
        return f"тестовая строка #{self.counter} ({quality_profile}/{cleanup_profile})"

    def transcribe_preview(self, audio_data, quality_profile: str = "balanced") -> str:
        self.preview_counter += 1
        return f"preview#{self.preview_counter} ({quality_profile})"


class FakeTranslator:
    """Фейковый переводчик для проверки pipeline и офлайн-контракта."""

    def __init__(self) -> None:
        self.last_mode = "off"
        self.last_network_mode = "offline_default"

    def translate(
        self,
        text: str,
        mode: str,
        network_mode: str,
        translation_style: str = "neutral",
        glossary: dict[str, str] | None = None,
    ) -> TranslationResult:
        self.last_mode = mode
        self.last_network_mode = network_mode

        normalized = mode.strip().lower()
        if normalized == "off":
            return TranslationResult(
                text="",
                status="not_requested",
                source_lang="",
                target_lang="",
                mode="off",
                engine="fake",
            )

        if normalized == "ru_to_es":
            return TranslationResult(
                text=f"ES:{text}",
                status="ok",
                source_lang="ru",
                target_lang="es",
                mode="ru_to_es",
                engine="fake",
            )

        if normalized == "es_to_ru":
            return TranslationResult(
                text=f"RU:{text}",
                status="ok",
                source_lang="es",
                target_lang="ru",
                mode="es_to_ru",
                engine="fake",
            )

        if normalized == "en_to_ru":
            return TranslationResult(
                text=f"RU:{text}",
                status="ok",
                source_lang="en",
                target_lang="ru",
                mode="en_to_ru",
                engine="fake",
            )

        if normalized == "bilingual_ru_es":
            return TranslationResult(
                text=f"RU: {text}\nES: ES:{text}",
                status="ok",
                source_lang="ru",
                target_lang="ru+es",
                mode="bilingual_ru_es",
                engine="fake",
            )

        return TranslationResult(
            text="",
            status="cannot_detect_language",
            source_lang="",
            target_lang="",
            mode=normalized,
            engine="fake",
        )


class BackendServiceTestCase(unittest.TestCase):
    """Проверяет командный контракт сервиса, включая 1000 циклов записи."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def request(self, method: str, params=None, request_id="t1"):
        return self.service.handle_request(
            {"id": request_id, "method": method, "params": params or {}}
        )

    def test_ping_and_settings(self) -> None:
        ping = self.request("ping")
        self.assertTrue(ping["ok"])

        get_settings = self.request("get_settings")
        self.assertTrue(get_settings["ok"])
        self.assertEqual(get_settings["result"]["history_policy"], "unlimited")
        self.assertEqual(get_settings["result"]["translation_mode"], "off")
        self.assertEqual(get_settings["result"]["hotkey_profile"], "default")
        self.assertFalse(get_settings["result"]["translate_and_paste"])
        self.assertEqual(get_settings["result"]["translation_style"], "neutral")
        self.assertEqual(get_settings["result"]["clipboard_mode"], "always_copy")
        self.assertTrue(get_settings["result"]["audio_ducking_enabled"])
        self.assertEqual(get_settings["result"]["audio_ducking_percent"], 50)
        self.assertEqual(get_settings["result"]["overlay_opacity_percent"], 45)
        self.assertEqual(get_settings["result"]["voice_gateway_url"], "http://127.0.0.1:8090")
        self.assertEqual(get_settings["result"]["voice_gateway_api_key"], "")
        self.assertEqual(get_settings["result"]["update_channel"], "stable")
        self.assertTrue(get_settings["result"]["call_notify_default"])
        self.assertTrue(get_settings["result"]["call_auto_summary"])
        self.assertEqual(get_settings["result"]["capture_source_mode"], "mic")
        self.assertEqual(get_settings["result"]["ui_last_tab"], "history")
        self.assertTrue(get_settings["result"]["history_focus_mode"])
        self.assertEqual(get_settings["result"]["history_text_density"], "normal")

        set_settings = self.request(
            "set_settings",
            {
                "mode": "menubar",
                "history_page_size": 77,
                "hotkey_profile": "meeting",
                "translation_mode": "ru_to_es",
                "translate_and_paste": True,
                "translation_style": "formal",
                "clipboard_mode": "copy_on_fail",
                "audio_ducking_enabled": False,
                "audio_ducking_percent": 75,
                "overlay_opacity_percent": 60,
                "voice_gateway_url": "http://127.0.0.1:9000",
                "voice_gateway_api_key": "token",
                "update_channel": "beta",
                "call_notify_default": False,
                "call_auto_summary": False,
                "capture_source_mode": "mic_plus_system",
                "ui_last_tab": "live_translation",
                "history_focus_mode": False,
                "history_text_density": "compact",
            },
        )
        self.assertTrue(set_settings["ok"])
        self.assertEqual(set_settings["result"]["mode"], "menubar")
        self.assertEqual(set_settings["result"]["history_page_size"], 77)
        self.assertEqual(set_settings["result"]["hotkey_profile"], "meeting")
        self.assertEqual(set_settings["result"]["translation_mode"], "ru_to_es")
        self.assertTrue(set_settings["result"]["translate_and_paste"])
        self.assertEqual(set_settings["result"]["translation_style"], "formal")
        self.assertEqual(set_settings["result"]["clipboard_mode"], "copy_on_fail")
        self.assertFalse(set_settings["result"]["audio_ducking_enabled"])
        self.assertEqual(set_settings["result"]["audio_ducking_percent"], 75)
        self.assertEqual(set_settings["result"]["overlay_opacity_percent"], 60)
        self.assertEqual(set_settings["result"]["voice_gateway_url"], "http://127.0.0.1:9000")
        self.assertEqual(set_settings["result"]["voice_gateway_api_key"], "token")
        self.assertEqual(set_settings["result"]["update_channel"], "beta")
        self.assertFalse(set_settings["result"]["call_notify_default"])
        self.assertFalse(set_settings["result"]["call_auto_summary"])
        self.assertEqual(set_settings["result"]["capture_source_mode"], "mic_plus_system")
        self.assertEqual(set_settings["result"]["ui_last_tab"], "live_translation")
        self.assertFalse(set_settings["result"]["history_focus_mode"])
        self.assertEqual(set_settings["result"]["history_text_density"], "compact")

    def test_capabilities(self) -> None:
        response = self.request("get_capabilities")
        self.assertTrue(response["ok"])
        result = response["result"]
        self.assertIn("translation", result)
        self.assertIn("modes", result["translation"])
        self.assertIn("hotkey", result)
        self.assertIn("profiles", result["hotkey"])
        self.assertIn("ru_to_es", result["translation"]["modes"])
        self.assertIn("bilingual_ru_es", result["translation"]["modes"])
        self.assertIn("audio_ducking", result)
        self.assertIn("batch_import", result)
        self.assertIn("diarization", result)
        self.assertIn("call_assist", result)
        self.assertIn("history", result)
        self.assertTrue(result["call_assist"]["available"])
        self.assertIn("default_auto_summary", result["call_assist"])
        self.assertTrue(result["system_audio"]["capture_translation"])
        self.assertIn("quick_phrase", result["call_assist"]["tools"])
        self.assertIn("timeline", result["call_assist"]["tools"])
        self.assertIn("timeline_export", result["call_assist"]["tools"])
        self.assertIn("timeline_clear", result["call_assist"]["tools"])
        self.assertIn("compact", result["history"]["density_modes"])
        self.assertTrue(result["history"]["overview"])

    def test_get_history_overview_method(self) -> None:
        self.request(
            "add_history_item",
            {
                "text": "hola",
                "paste_status": "ok",
                "translation_mode": "ru_to_es",
                "translation_status": "ok",
            },
        )
        self.request(
            "add_history_item",
            {
                "text": "ошибка",
                "paste_status": "failed",
                "translation_mode": "ru_to_es",
                "translation_status": "translate_error",
            },
        )
        self.request(
            "add_history_item",
            {
                "text": "без перевода",
                "paste_status": "failed",
                "translation_mode": "off",
                "translation_status": "not_requested",
            },
        )
        response = self.request("get_history_overview")
        self.assertTrue(response["ok"])
        result = response["result"]
        self.assertEqual(result["active_count"], 3)
        self.assertEqual(result["paste_ok"], 1)
        self.assertEqual(result["paste_failed"], 2)
        self.assertEqual(result["translated_ok"], 1)
        self.assertEqual(result["translated_error"], 1)
        self.assertEqual(result["no_translation"], 1)
        self.assertIn("top_modes", result)

    def test_glossary_management(self) -> None:
        add = self.request(
            "set_translation_glossary_item",
            {"source": "cliente", "target": "клиент"},
        )
        self.assertTrue(add["ok"])
        settings = self.request("get_settings")
        self.assertEqual(settings["result"]["translation_glossary"].get("cliente"), "клиент")

        remove = self.request(
            "remove_translation_glossary_item",
            {"source": "cliente"},
        )
        self.assertTrue(remove["ok"])
        settings_after = self.request("get_settings")
        self.assertNotIn("cliente", settings_after["result"]["translation_glossary"])

    def test_settings_normalize_translation_mode(self) -> None:
        response = self.request("set_settings", {"translation_mode": "broken_mode"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["translation_mode"], "off")

        bilingual = self.request("set_settings", {"translation_mode": "bilingual_ru_es"})
        self.assertTrue(bilingual["ok"])
        self.assertEqual(bilingual["result"]["translation_mode"], "bilingual_ru_es")

        auto_to_ru = self.request("set_settings", {"translation_mode": "auto_to_ru"})
        self.assertTrue(auto_to_ru["ok"])
        self.assertEqual(auto_to_ru["result"]["translation_mode"], "auto_to_ru")

    def test_settings_normalize_call_assist_fields(self) -> None:
        response = self.request(
            "set_settings",
            {
                "capture_source_mode": "wrong",
                "ui_last_tab": "wrong",
                "voice_gateway_url": "  http://x  ",
                "voice_gateway_api_key": "  key  ",
                "call_auto_summary": "off",
                "hotkey_profile": "unknown_profile",
                "update_channel": "unknown",
                "text_templates": "not_dict",
                "history_focus_mode": "invalid_bool",
                "history_text_density": "invalid_density",
            },
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["capture_source_mode"], "mic")
        self.assertEqual(response["result"]["ui_last_tab"], "history")
        self.assertEqual(response["result"]["voice_gateway_url"], "http://x")
        self.assertEqual(response["result"]["voice_gateway_api_key"], "key")
        self.assertFalse(response["result"]["call_auto_summary"])
        self.assertEqual(response["result"]["hotkey_profile"], "default")
        self.assertEqual(response["result"]["update_channel"], "stable")
        self.assertIsInstance(response["result"]["text_templates"], dict)
        self.assertTrue(response["result"]["history_focus_mode"])
        self.assertEqual(response["result"]["history_text_density"], "normal")

    def test_summarize_text_method(self) -> None:
        summary_short = self.request(
            "summarize_text",
            {
                "text": "Первое предложение. Второе предложение! Третье предложение?",
                "mode": "summary_short",
                "max_points": 2,
            },
        )
        self.assertTrue(summary_short["ok"])
        self.assertEqual(summary_short["result"]["mode"], "summary_short")
        self.assertTrue(summary_short["result"]["summary"])
        self.assertGreaterEqual(len(summary_short["result"]["bullets"]), 1)

        summary_detailed = self.request(
            "summarize_text",
            {
                "text": "Один. Два. Три. Четыре.",
                "mode": "summary_detailed",
                "max_points": 3,
            },
        )
        self.assertTrue(summary_detailed["ok"])
        self.assertEqual(summary_detailed["result"]["mode"], "summary_detailed")
        self.assertGreaterEqual(len(summary_detailed["result"]["bullets"]), 2)

    def test_settings_normalize_audio_ducking_percent(self) -> None:
        too_high = self.request("set_settings", {"audio_ducking_percent": 999})
        self.assertTrue(too_high["ok"])
        self.assertEqual(too_high["result"]["audio_ducking_percent"], 100)
        bad_type = self.request("set_settings", {"audio_ducking_percent": "oops"})
        self.assertTrue(bad_type["ok"])
        self.assertEqual(bad_type["result"]["audio_ducking_percent"], 50)

    def test_settings_normalize_overlay_opacity_percent(self) -> None:
        too_low = self.request("set_settings", {"overlay_opacity_percent": 1})
        self.assertTrue(too_low["ok"])
        self.assertEqual(too_low["result"]["overlay_opacity_percent"], 15)
        too_high = self.request("set_settings", {"overlay_opacity_percent": 999})
        self.assertTrue(too_high["ok"])
        self.assertEqual(too_high["result"]["overlay_opacity_percent"], 90)

    def test_recording_flow(self) -> None:
        self.assertTrue(self.request("start_recording")["ok"])
        time.sleep(1.2)
        state = self.request("get_recording_state")
        self.assertTrue(state["ok"])
        self.assertTrue(state["result"]["is_recording"])
        self.assertIn("preview_text", state["result"])
        stop = self.request("stop_recording", {"quality_profile": "max"})
        self.assertTrue(stop["ok"])
        self.assertTrue(stop["result"]["text"].startswith("тестовая строка"))
        self.assertIsNotNone(stop["result"]["history_id"])
        self.assertEqual(stop["result"]["cleanup_profile"], "soft")

    def test_start_recording_is_idempotent(self) -> None:
        first = self.request("start_recording")
        self.assertTrue(first["ok"])
        self.assertEqual(first["result"]["status"], "recording")

        second = self.request("start_recording")
        self.assertTrue(second["ok"])
        self.assertEqual(second["result"]["status"], "already_recording")
        self.assertTrue(second["result"]["is_recording"])

    def test_translation_pipeline_and_inserted_text(self) -> None:
        self.assertTrue(
            self.request(
                "set_settings",
                {
                    "translation_mode": "ru_to_es",
                    "translate_and_paste": True,
                    "network_mode": "offline_default",
                },
            )["ok"]
        )
        self.assertTrue(self.request("start_recording")["ok"])
        stop = self.request("stop_recording", {"quality_profile": "balanced"})
        self.assertTrue(stop["ok"])
        self.assertEqual(stop["result"]["translation_mode"], "ru_to_es")
        self.assertEqual(stop["result"]["translation_status"], "ok")
        self.assertTrue(stop["result"]["translated_text"].startswith("ES:"))
        self.assertEqual(stop["result"]["text"], stop["result"]["translated_text"])
        self.assertTrue(stop["result"]["original_text"].startswith("тестовая строка"))

    def test_translate_text_method(self) -> None:
        response = self.request(
            "translate_text",
            {
                "text": "привет",
                "translation_mode": "ru_to_es",
            },
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["status"], "ok")
        self.assertEqual(response["result"]["translation_mode"], "ru_to_es")
        self.assertTrue(response["result"]["text"].startswith("ES:"))

    def test_call_assist_flow_and_list_audio_inputs(self) -> None:
        self.service._request_voice_gateway_start = lambda **kwargs: {  # type: ignore[method-assign]
            "ok": True,
            "session_id": "gw-session-1",
        }
        self.service._request_voice_gateway_stop = lambda **kwargs: {  # type: ignore[method-assign]
            "ok": True,
        }
        self.service._list_audio_inputs = lambda: [  # type: ignore[method-assign]
            {
                "id": 3,
                "name": "BlackHole 2ch",
                "is_default": True,
                "tags": ["loopback"],
            }
        ]
        self.assertTrue(self.request("set_settings", {"call_auto_summary": False})["ok"])

        start = self.request(
            "start_call_assist",
            {
                "translation_mode": "auto_to_ru",
                "notify_mode": "auto_off",
                "tts_mode": "hybrid",
            },
        )
        self.assertTrue(start["ok"])
        self.assertTrue(start["result"]["active"])
        self.assertEqual(start["result"]["status"], "running")
        self.assertEqual(start["result"]["gateway_session_id"], "gw-session-1")
        self.assertEqual(start["result"]["notify_mode"], "auto_off")
        self.assertEqual(start["result"]["translation_mode"], "auto_to_ru")

        state = self.request("get_call_assist_state")
        self.assertTrue(state["ok"])
        self.assertTrue(state["result"]["active"])

        inputs = self.request("list_audio_inputs")
        self.assertTrue(inputs["ok"])
        self.assertEqual(inputs["result"]["count"], 1)
        self.assertEqual(inputs["result"]["default_input_id"], 3)

        stop = self.request("stop_call_assist", {"auto_summary": False})
        self.assertTrue(stop["ok"])
        self.assertFalse(stop["result"]["active"])
        self.assertEqual(stop["result"]["status"], "stopped")
        self.assertEqual(stop["result"]["summary_status"], "skipped")

    def test_call_assist_stop_auto_summary_saves_to_history(self) -> None:
        called_summary_paths: list[str] = []
        called_summary_payloads: list[dict[str, object]] = []
        self.service._request_voice_gateway_start = lambda **kwargs: {  # type: ignore[method-assign]
            "ok": True,
            "session_id": "gw-session-summary-1",
        }

        def fake_gateway_post(*args, **kwargs):  # type: ignore[no-untyped-def]
            path = str(kwargs.get("path", ""))
            payload = kwargs.get("payload", {})
            if not path and len(args) >= 3:
                path = str(args[2])
            if (not payload or not isinstance(payload, dict)) and len(args) >= 4 and isinstance(args[3], dict):
                payload = dict(args[3])
            if path.endswith("/summary"):
                called_summary_paths.append(path)
                called_summary_payloads.append(dict(payload) if isinstance(payload, dict) else {})
                return {
                    "ok": True,
                    "payload": {
                        "summary": "Обсудили доставку и документы.",
                        "tasks": ["Отправить копию паспорта", "Подтвердить адрес"],
                    },
                }
            return {"ok": True, "payload": {"ok": True}}

        self.service._request_voice_gateway_post = fake_gateway_post  # type: ignore[method-assign]
        self.service._request_voice_gateway_stop = lambda **kwargs: {"ok": True}  # type: ignore[method-assign]

        start = self.request("start_call_assist", {"translation_mode": "auto_to_ru"})
        self.assertTrue(start["ok"])
        self.assertEqual(start["result"]["gateway_session_id"], "gw-session-summary-1")

        stop = self.request("stop_call_assist", {"auto_summary": True, "summary_max_items": 12})
        self.assertTrue(stop["ok"])
        self.assertEqual(stop["result"]["summary_status"], "ok")
        self.assertIn("summary", stop["result"])
        self.assertTrue(stop["result"].get("summary_history_id"))
        self.assertEqual(called_summary_paths, ["/v1/sessions/gw-session-summary-1/summary"])
        self.assertEqual(called_summary_payloads[0]["max_items"], 12)

        page = self.request("get_history_page", {"cursor": None, "limit": 20})
        self.assertTrue(page["ok"])
        texts = [item["text"] for item in page["result"]["items"]]
        self.assertTrue(any("[Call Summary]" in text for text in texts))

    def test_call_assist_gateway_tools(self) -> None:
        get_paths: list[str] = []
        post_paths: list[str] = []
        delete_paths: list[str] = []

        self.service._request_voice_gateway_start = lambda **kwargs: {  # type: ignore[method-assign]
            "ok": True,
            "session_id": "gw-session-77",
        }

        def fake_gateway_get(**kwargs):  # type: ignore[no-untyped-def]
            path = str(kwargs.get("path", ""))
            get_paths.append(path)
            if path.startswith("/v1/telephony/cost/estimate"):
                return {
                    "ok": True,
                    "payload": {
                        "ok": True,
                        "country": "ES",
                        "rates_source": "manual",
                        "telephony_usd": {"total": 1.3},
                        "ai_usd": {"total": 0.8},
                        "total_usd": 2.1,
                    },
                }
            if "/timeline/summary" in path:
                return {
                    "ok": True,
                    "payload": {
                        "ok": True,
                        "summary": "Краткая сводка звонка",
                        "tasks": ["Отправить договор", "Перезвонить завтра"],
                        "stats": {"count": 4},
                        "items_used": 4,
                    },
                }
            if "/timeline/stats" in path:
                return {
                    "ok": True,
                    "payload": {
                        "ok": True,
                        "stats": {
                            "count": 4,
                            "text_chars": 40,
                            "first_ts": "2026-02-12T10:00:00+00:00",
                            "last_ts": "2026-02-12T10:00:10+00:00",
                            "by_kind": {"stt.partial": 2, "translation.partial": 2},
                        },
                    },
                }
            if "/timeline/export" in path:
                return {"ok": True, "payload": {"ok": True, "format": "md", "content": "# timeline\nentry"}}
            if "/timeline" in path:
                return {"ok": True, "payload": {"ok": True, "count": 2, "items": [{"kind": "stt.partial", "text": "x"}]}}
            return {"ok": True, "payload": {"ok": True, "count": 2, "items": [{"source_text": "x"}]}}

        def fake_gateway_post(*args, **kwargs):  # type: ignore[no-untyped-def]
            path = str(kwargs.get("path", ""))
            if not path and len(args) >= 3:
                path = str(args[2])
            post_paths.append(path)
            return {"ok": True, "payload": {"ok": True, "summary": "sum", "tasks": [], "translated_text": "hola"}}

        def fake_gateway_delete(**kwargs):  # type: ignore[no-untyped-def]
            path = str(kwargs.get("path", ""))
            delete_paths.append(path)
            return {"ok": True, "payload": {"ok": True, "before": 10, "after": 1, "keep_last": 1}}

        self.service._request_voice_gateway_get = fake_gateway_get  # type: ignore[method-assign]
        self.service._request_voice_gateway_post = fake_gateway_post  # type: ignore[method-assign]
        self.service._request_voice_gateway_delete = fake_gateway_delete  # type: ignore[method-assign]

        start = self.request("start_call_assist", {"translation_mode": "auto_to_ru"})
        self.assertTrue(start["ok"])
        self.assertEqual(start["result"]["gateway_session_id"], "gw-session-77")

        diag = self.request("call_assist_diagnostics", {"include_why": True})
        self.assertTrue(diag["ok"])
        self.assertEqual(diag["result"]["gateway_session_id"], "gw-session-77")
        self.assertIn("diagnostics", diag["result"])

        summary = self.request("call_assist_summary", {"max_items": 40})
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["result"]["gateway_session_id"], "gw-session-77")
        self.assertIn("summary", summary["result"])

        quick = self.request(
            "call_assist_quick_phrase",
            {"text": "Говорите медленнее", "source_lang": "ru", "target_lang": "es"},
        )
        self.assertTrue(quick["ok"])
        self.assertEqual(quick["result"]["gateway_session_id"], "gw-session-77")
        self.assertIn("quick_phrase", quick["result"])

        phrases = self.request(
            "list_call_assist_quick_phrases",
            {"source_lang": "ru", "target_lang": "es", "limit": 5},
        )
        self.assertTrue(phrases["ok"])
        self.assertEqual(phrases["result"]["ok"], True)

        cost = self.request(
            "call_assist_cost_estimate",
            {
                "country": "ES",
                "use_live_pricing": False,
                "minutes_inbound": 10,
                "minutes_outbound_landline": 20,
                "minutes_outbound_mobile": 30,
                "minutes_media_stream": 60,
                "inbound_rate_override": 0.01,
                "outbound_landline_rate_override": 0.02,
                "outbound_mobile_rate_override": 0.03,
            },
        )
        self.assertTrue(cost["ok"])
        self.assertEqual(cost["result"]["country"], "ES")
        self.assertGreater(cost["result"]["total_usd"], 0)

        timeline = self.request(
            "call_assist_timeline",
            {"limit": 25, "kind": "translation.partial", "contains": "hola"},
        )
        self.assertTrue(timeline["ok"])
        self.assertEqual(timeline["result"]["count"], 2)

        timeline_export = self.request(
            "call_assist_timeline_export",
            {"format": "md", "limit": 100},
        )
        self.assertTrue(timeline_export["ok"])
        self.assertEqual(timeline_export["result"]["format"], "md")
        self.assertIn("content", timeline_export["result"])

        timeline_stats = self.request(
            "call_assist_timeline_stats",
            {"limit": 1000},
        )
        self.assertTrue(timeline_stats["ok"])
        self.assertEqual(timeline_stats["result"]["stats"]["count"], 4)

        timeline_summary = self.request(
            "call_assist_timeline_summary",
            {"limit": 500, "max_tasks": 5},
        )
        self.assertTrue(timeline_summary["ok"])
        self.assertIn("summary", timeline_summary["result"])
        self.assertGreaterEqual(len(timeline_summary["result"].get("tasks", [])), 1)

        timeline_clear = self.request(
            "call_assist_timeline_clear",
            {"keep_last": 1},
        )
        self.assertTrue(timeline_clear["ok"])
        self.assertEqual(timeline_clear["result"]["keep_last"], 1)

        timeline_to_history = self.request(
            "call_assist_timeline_to_history",
            {"format": "md", "limit": 120},
        )
        self.assertTrue(timeline_to_history["ok"])
        self.assertEqual(timeline_to_history["result"]["format"], "md")
        self.assertTrue(timeline_to_history["result"]["history_id"])
        self.assertTrue(timeline_to_history["result"]["summary_included"])
        self.assertTrue(timeline_to_history["result"]["stats_included"])

        self.assertIn("/v1/sessions/gw-session-77/summary", post_paths)
        self.assertIn("/v1/sessions/gw-session-77/quick-phrase", post_paths)
        self.assertTrue(
            any(path.startswith("/v1/sessions/gw-session-77/timeline?limit=25") for path in get_paths)
        )
        self.assertIn("/v1/sessions/gw-session-77/timeline/stats?limit=1000", get_paths)
        self.assertIn("/v1/sessions/gw-session-77/timeline/summary?limit=500&max_tasks=5", get_paths)
        self.assertIn("/v1/sessions/gw-session-77/timeline/summary?limit=120&max_tasks=8", get_paths)
        self.assertIn("/v1/sessions/gw-session-77/timeline/stats?limit=120", get_paths)
        self.assertIn("/v1/sessions/gw-session-77/timeline/export?format=md&limit=100", get_paths)
        self.assertIn("/v1/sessions/gw-session-77/timeline/export?format=md&limit=120", get_paths)
        self.assertTrue(any(path.startswith("/v1/telephony/cost/estimate?country=ES") for path in get_paths))
        self.assertIn("/v1/sessions/gw-session-77/timeline?keep_last=1", delete_paths)

    def test_preview_updates_even_when_snapshot_size_constant(self) -> None:
        self.assertTrue(self.request("start_recording")["ok"])
        time.sleep(2.4)
        state = self.request("get_recording_state")
        self.assertTrue(state["ok"])
        preview_text = state["result"]["preview_text"]
        self.assertIn("preview#", preview_text)
        self.assertRegex(preview_text, r"preview#\d+")
        stop = self.request("stop_recording", {"quality_profile": "balanced"})
        self.assertTrue(stop["ok"])

    def test_history_methods(self) -> None:
        added = self.request("add_history_item", {"text": "alpha", "paste_status": "failed"})
        self.assertTrue(added["ok"])
        item_id = added["result"]["id"]

        status = self.request("set_paste_status", {"id": item_id, "paste_status": "ok"})
        self.assertTrue(status["ok"])

        page = self.request("get_history_page", {"cursor": None, "limit": 50})
        self.assertTrue(page["ok"])
        self.assertEqual(page["result"]["items"][0]["paste_status"], "ok")

        search = self.request("search_history", {"query": "alpha", "cursor": None, "limit": 50})
        self.assertTrue(search["ok"])
        self.assertEqual(len(search["result"]["items"]), 1)

        deleted = self.request("delete_history_item", {"id": item_id})
        self.assertTrue(deleted["ok"])

        page_after = self.request("get_history_page", {"cursor": None, "limit": 50})
        self.assertEqual(page_after["result"]["items"], [])

    def test_history_filters_via_service(self) -> None:
        add_ok = self.request(
            "add_history_item",
            {
                "text": "uno",
                "paste_status": "ok",
                "translation_mode": "ru_to_es",
                "translation_status": "ok",
            },
        )
        self.assertTrue(add_ok["ok"])
        add_failed = self.request(
            "add_history_item",
            {
                "text": "dos",
                "paste_status": "failed",
                "translation_mode": "off",
            },
        )
        self.assertTrue(add_failed["ok"])

        page = self.request(
            "get_history_page",
            {
                "cursor": None,
                "limit": 50,
                "paste_status": "ok",
            },
        )
        self.assertTrue(page["ok"])
        self.assertEqual(len(page["result"]["items"]), 1)
        self.assertEqual(page["result"]["items"][0]["paste_status"], "ok")

        search = self.request(
            "search_history",
            {
                "query": "",
                "cursor": None,
                "limit": 50,
                "translation_mode": "ru_to_es",
            },
        )
        self.assertTrue(search["ok"])
        self.assertEqual(len(search["result"]["items"]), 1)
        self.assertEqual(search["result"]["items"][0]["translation_mode"], "ru_to_es")

        search_status = self.request(
            "search_history",
            {
                "query": "",
                "cursor": None,
                "limit": 50,
                "translation_status": "ok",
            },
        )
        self.assertTrue(search_status["ok"])
        self.assertEqual(len(search_status["result"]["items"]), 1)

        today = search_status["result"]["items"][0]["ts"][:10]
        page_today = self.request(
            "get_history_page",
            {
                "cursor": None,
                "limit": 50,
                "from_ts": today,
                "to_ts": today,
            },
        )
        self.assertTrue(page_today["ok"])
        self.assertGreaterEqual(len(page_today["result"]["items"]), 1)

    def test_import_history_ndjson_method(self) -> None:
        self.request(
            "add_history_item",
            {"text": "alpha", "paste_status": "failed"},
        )
        page = self.request("get_history_page", {"cursor": None, "limit": 10})
        existing_id = page["result"]["items"][0]["id"]

        ndjson_path = Path(self.tmp.name) / "external_history.ndjson"
        payloads = [
            {
                "id": existing_id,
                "ts": "2026-02-11T10:00:00",
                "text": "duplicate",
                "paste_status": "failed",
            },
            {
                "id": "ext-imported-1",
                "ts": "2026-02-11T10:00:01",
                "text": "external text",
                "paste_status": "ok",
                "translation_mode": "off",
            },
        ]
        ndjson_path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in payloads) + "\n", encoding="utf-8")

        imported = self.request(
            "import_history_ndjson",
            {"path": str(ndjson_path)},
        )
        self.assertTrue(imported["ok"])
        self.assertEqual(imported["result"]["imported"], 1)
        self.assertEqual(imported["result"]["skipped"], 1)

    def test_integration_1000_cycles(self) -> None:
        for idx in range(1000):
            start = self.request("start_recording", request_id=f"s{idx}")
            self.assertTrue(start["ok"])
            stop = self.request("stop_recording", {"quality_profile": "balanced"}, request_id=f"e{idx}")
            self.assertTrue(stop["ok"])
            self.assertTrue(stop["result"]["history_id"])

        page = self.request("get_history_page", {"cursor": None, "limit": 50})
        self.assertEqual(len(page["result"]["items"]), 50)

        compacted = self.request("compact_history")
        self.assertTrue(compacted["ok"])

    def test_transcribe_paths(self) -> None:
        src_dir = Path(self.tmp.name) / "audio_src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "a.wav").write_bytes(b"stub")
        (src_dir / "b.mp3").write_bytes(b"stub")
        (src_dir / "ignore.txt").write_text("x", encoding="utf-8")

        response = self.request(
            "transcribe_paths",
            {"paths": [str(src_dir)], "quality_profile": "balanced"},
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["processed"], 2)
        self.assertEqual(len(response["result"]["items"]), 2)

    def test_preview_transcribe_paths(self) -> None:
        src_dir = Path(self.tmp.name) / "audio_preview"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "a.wav").write_bytes(b"stub")
        (src_dir / "b.mp3").write_bytes(b"stub")
        (src_dir / "ignore.txt").write_text("x", encoding="utf-8")

        response = self.request(
            "preview_transcribe_paths",
            {"paths": [str(src_dir)], "sample_limit": 2},
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["audio_count"], 2)
        self.assertEqual(len(response["result"]["sample"]), 2)
        self.assertEqual(response["result"]["by_ext"].get(".wav"), 1)
        self.assertEqual(response["result"]["by_ext"].get(".mp3"), 1)
        self.assertGreater(response["result"]["total_bytes"], 0)

    def test_preview_transcribe_paths_empty(self) -> None:
        response = self.request(
            "preview_transcribe_paths",
            {"paths": [str(Path(self.tmp.name) / "missing_dir")]},
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["audio_count"], 0)

    def test_transcribe_paths_with_translation(self) -> None:
        src_dir = Path(self.tmp.name) / "audio_src_translate"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "clip.wav").write_bytes(b"stub")

        self.assertTrue(
            self.request(
                "set_settings",
                {
                    "translation_mode": "ru_to_es",
                    "translate_and_paste": True,
                    "network_mode": "offline_default",
                },
            )["ok"]
        )

        response = self.request(
            "transcribe_paths",
            {
                "paths": [str(src_dir)],
                "quality_profile": "balanced",
                "translation_mode": "ru_to_es",
                "translate_and_paste": True,
            },
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["processed"], 1)
        item = response["result"]["items"][0]
        self.assertEqual(item["translation_mode"], "ru_to_es")
        self.assertEqual(item["translation_status"], "ok")
        self.assertTrue(item["translated_text"].startswith("ES:"))
        self.assertEqual(item["text"], item["translated_text"])

    def test_compact_and_history_stats(self) -> None:
        item = self.request("add_history_item", {"text": "one", "paste_status": "failed"})
        self.assertTrue(item["ok"])
        item_id = item["result"]["id"]
        self.assertTrue(self.request("set_paste_status", {"id": item_id, "paste_status": "ok"})["ok"])
        self.assertTrue(self.request("delete_history_item", {"id": item_id})["ok"])

        compacted = self.request("compact_history")
        self.assertTrue(compacted["ok"])
        self.assertIn("before_total_bytes", compacted["result"])
        self.assertIn("after_total_bytes", compacted["result"])
        self.assertIn("reclaimed_bytes", compacted["result"])

        stats = self.request("get_history_stats")
        self.assertTrue(stats["ok"])
        self.assertIn("active_count", stats["result"])
        self.assertIn("total_bytes", stats["result"])


if __name__ == "__main__":
    unittest.main()
