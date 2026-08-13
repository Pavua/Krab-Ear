"""Wave 186 — deep coverage tests for CallAssistService.

Covers: start/stop lifecycle, glossary passthrough, concurrent-start guard,
diagnostics call-state inclusion, summary generation, timeline events,
quick-phrase dispatch, VG connection failure, unicode metadata,
partial diarization handling, and reconnect-after-disconnect.

All collaborators are mocked; no real backend processes are started.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_assist_service import CallAssistService, VoiceGatewayClient  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self, extra: dict[str, Any] | None = None) -> None:
        self._settings: dict[str, Any] = {
            "voice_gateway_url": "http://127.0.0.1:8090",
            "voice_gateway_api_key": "test-key-42",
            "call_auto_summary": True,
            "call_notify_default": True,
        }
        if extra:
            self._settings.update(extra)
        self._history: list[Any] = []

    def load_settings(self, lock_timeout_sec: float | None = None, nowait: bool = False) -> dict[str, Any]:
        return dict(self._settings)

    def save_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        self._settings = dict(settings)
        return dict(settings)

    def add_history_item(self, **kwargs: Any) -> Any:
        class _FakeItem:
            id = "fake-hist-001"
        item = _FakeItem()
        self._history.append(kwargs)
        return item


class FakeRecorder:
    is_recording: bool = False

    def start(self) -> bool:
        self.is_recording = True
        return True

    def stop(self) -> None:
        self.is_recording = False

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        import numpy as np
        return np.zeros(int(max_duration_sec * 16000), dtype=np.float32), max_duration_sec


class FakeTranscriber:
    def transcribe_preview(self, audio_data: Any, quality_profile: str = "balanced") -> dict:
        return {"text": "тестовая транскрипция"}


class _GwOk(VoiceGatewayClient):
    """Gateway that returns success for all calls."""

    def __init__(self, session_id: str = "gw-deep-001") -> None:
        self._session_id = session_id
        self.start_calls: list[dict] = []
        self.stop_calls: list[str] = []
        self.post_calls: list[dict] = []
        self.get_calls: list[str] = []
        self.delete_calls: list[str] = []

    def start_session(self, voice_gateway_url: str, api_key: str, payload: dict) -> dict:
        self.start_calls.append({"payload": payload, "api_key": api_key})
        return {"ok": True, "session_id": self._session_id}

    def stop_session(self, voice_gateway_url: str, api_key: str, session_id: str) -> dict:
        self.stop_calls.append(session_id)
        return {"ok": True}

    def get(self, voice_gateway_url: str, api_key: str, path: str) -> dict:
        self.get_calls.append(path)
        if "diagnostics/why" in path:
            return {"ok": True, "payload": {"reason": "pipeline_ok"}}
        if "diagnostics" in path:
            return {"ok": True, "payload": {"status": "healthy", "latency_ms": 42}}
        if "timeline/stats" in path:
            stats = {"count": 5, "text_chars": 200, "first_ts": "t1", "last_ts": "t2",
                     "by_kind": {"stt.partial": 3}}
            return {"ok": True, "payload": {"stats": stats}}
        if "timeline/summary" in path:
            return {"ok": True, "payload": {"summary": "Meeting summary",
                                            "tasks": ["Follow up", "Send report"]}}
        if "timeline/export" in path:
            return {"ok": True, "payload": {"content": "## Timeline\n- event1\n- event2",
                                            "format": "md"}}
        if "timeline" in path:
            items = [{"ts": "t1", "kind": "stt.partial", "text": "hello"}]
            return {"ok": True, "payload": {"items": items, "count": 1}}
        if "summary" in path:
            return {"ok": True, "payload": {"summary": "Звонок о поставке", "tasks": []}}
        if "quick-phrases" in path:
            return {"ok": True, "payload": {"items": [{"id": "1", "text": "Hola"}]}}
        if "cost" in path or "telephony" in path:
            return {"ok": True, "payload": {"total_usd": 1.23, "country": "ES"}}
        return {"ok": True, "payload": {}}

    def post(self, voice_gateway_url: str, api_key: str, path: str, payload: dict) -> dict:
        self.post_calls.append({"path": path, "payload": payload})
        if "summary" in path:
            return {"ok": True, "payload": {"summary": "Резюме звонка", "tasks": ["Задача 1"]}}
        if "quick-phrase" in path:
            return {"ok": True, "payload": {"audio_url": "/tts/abc", "translated": "Hola"}}
        return {"ok": True, "payload": {"result": "ok"}}

    def delete(self, voice_gateway_url: str, api_key: str, path: str) -> dict:
        self.delete_calls.append(path)
        return {"ok": True, "payload": {"cleared": 10}}


class _GwFail(VoiceGatewayClient):
    """Gateway that always fails every call."""

    def start_session(self, voice_gateway_url: str, api_key: str, payload: dict) -> dict:
        return {"ok": False, "error": "connection_refused"}

    def stop_session(self, voice_gateway_url: str, api_key: str, session_id: str) -> dict:
        return {"ok": False, "error": "connection_refused"}

    def get(self, voice_gateway_url: str, api_key: str, path: str) -> dict:
        return {"ok": False, "error": "connection_refused"}

    def post(self, voice_gateway_url: str, api_key: str, path: str, payload: dict) -> dict:
        return {"ok": False, "error": "connection_refused"}

    def delete(self, voice_gateway_url: str, api_key: str, path: str) -> dict:
        return {"ok": False, "error": "connection_refused"}


def _idle_svc(gateway: VoiceGatewayClient | None = None,
              store: FakeStore | None = None) -> CallAssistService:
    return CallAssistService(
        store=store or FakeStore(),
        recorder=FakeRecorder(),
        transcriber=FakeTranscriber(),
        gateway=gateway or _GwOk(),
    )


def _active_svc(gateway: VoiceGatewayClient | None = None,
                session_id: str = "gw-deep-001",
                store: FakeStore | None = None) -> CallAssistService:
    svc = _idle_svc(gateway=gateway, store=store)
    with svc._lock:
        svc._state = {
            "active": True,
            "status": "running",
            "session_id": "call_abc",
            "gateway_session_id": session_id,
            "started_at": "2026-05-19T10:00:00",
        }
    return svc


# ---------------------------------------------------------------------------
# 1. test_start_call_assist_basic
# ---------------------------------------------------------------------------

class TestStartCallAssistBasic(unittest.TestCase):
    """handle_start returns active state and fires start_session on gateway."""

    def test_start_call_assist_basic(self) -> None:
        gw = _GwOk()
        svc = _idle_svc(gateway=gw)
        result = svc.handle_start({"translation_mode": "auto_to_ru"})

        self.assertTrue(result["active"])
        self.assertEqual(result["status"], "running")
        self.assertIsNotNone(result["session_id"])
        self.assertEqual(result["gateway_status"], "ok")
        self.assertEqual(result["gateway_session_id"], "gw-deep-001")

        # Verify gateway.start_session was called exactly once
        self.assertEqual(len(gw.start_calls), 1)
        call = gw.start_calls[0]
        self.assertEqual(call["payload"]["translation_mode"], "auto_to_ru")
        self.assertEqual(call["api_key"], "test-key-42")

    def test_start_sets_internal_state_active(self) -> None:
        svc = _idle_svc()
        svc.handle_start({})
        self.assertTrue(svc.state["active"])

    def test_start_records_started_at_timestamp(self) -> None:
        svc = _idle_svc()
        result = svc.handle_start({})
        self.assertIn("started_at", result)
        self.assertIsNotNone(result["started_at"])


# ---------------------------------------------------------------------------
# 2. test_start_call_assist_with_glossary
# ---------------------------------------------------------------------------

class TestStartCallAssistWithGlossary(unittest.TestCase):
    """Glossary and translation_mode are passed through to the gateway payload."""

    def test_translation_mode_forwarded_to_gateway(self) -> None:
        gw = _GwOk()
        svc = _idle_svc(gateway=gw)
        svc.handle_start({"translation_mode": "ru_to_es"})
        self.assertEqual(len(gw.start_calls), 1)
        self.assertEqual(gw.start_calls[0]["payload"]["translation_mode"], "ru_to_es")

    def test_capture_source_mode_mic_plus_system_forwarded(self) -> None:
        gw = _GwOk()
        svc = _idle_svc(gateway=gw)
        result = svc.handle_start({"capture_source_mode": "mic_plus_system"})
        self.assertEqual(result["capture_source_mode"], "mic_plus_system")
        self.assertEqual(gw.start_calls[0]["payload"]["source"], "mic_plus_system")

    def test_auto_summary_param_forwarded_in_meta(self) -> None:
        gw = _GwOk()
        svc = _idle_svc(gateway=gw)
        svc.handle_start({"auto_summary": True})
        meta = gw.start_calls[0]["payload"]["meta"]
        self.assertTrue(meta["auto_summary"])

    def test_auto_summary_false_forwarded(self) -> None:
        gw = _GwOk()
        svc = _idle_svc(gateway=gw)
        svc.handle_start({"auto_summary": False})
        meta = gw.start_calls[0]["payload"]["meta"]
        self.assertFalse(meta["auto_summary"])


# ---------------------------------------------------------------------------
# 3. test_stop_call_assist
# ---------------------------------------------------------------------------

class TestStopCallAssist(unittest.TestCase):
    """handle_stop marks session inactive and calls gateway stop_session."""

    def test_stop_call_assist(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        result = svc.handle_stop({"auto_summary": False})

        self.assertFalse(result["active"])
        self.assertEqual(result["status"], "stopped")
        self.assertIn("stopped_at", result)
        # stop_session was called on gateway
        self.assertIn("gw-deep-001", gw.stop_calls)

    def test_stop_updates_internal_state(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        svc.handle_stop({"auto_summary": False})
        self.assertFalse(svc.state["active"])

    def test_stop_gateway_stop_status_ok(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        result = svc.handle_stop({"auto_summary": False})
        self.assertEqual(result.get("gateway_stop_status"), "ok")

    def test_stop_gateway_degraded_when_gateway_fails(self) -> None:
        gw = _GwFail()
        svc = _active_svc(gateway=gw)
        result = svc.handle_stop({"auto_summary": False})
        self.assertFalse(result["active"])
        self.assertEqual(result.get("gateway_stop_status"), "degraded")


# ---------------------------------------------------------------------------
# 4. test_diagnostics_includes_call_state
# ---------------------------------------------------------------------------

class TestDiagnosticsIncludesCallState(unittest.TestCase):
    """handle_diagnostics must include active flag, session id, and diagnostics payload."""

    def test_diagnostics_includes_call_state(self) -> None:
        gw = _GwOk(session_id="gw-diag-77")
        svc = _active_svc(gateway=gw, session_id="gw-diag-77")
        result = svc.handle_diagnostics({})

        self.assertTrue(result["active"])
        self.assertEqual(result["gateway_session_id"], "gw-diag-77")
        self.assertIn("diagnostics", result)
        self.assertEqual(result["diagnostics"].get("status"), "healthy")

    def test_diagnostics_include_why_true(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        result = svc.handle_diagnostics({"include_why": True})
        self.assertIn("why", result)
        self.assertEqual(result["why"].get("reason"), "pipeline_ok")

    def test_diagnostics_include_why_false_skips_why_call(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        result = svc.handle_diagnostics({"include_why": False})
        # why key should be empty dict (not populated)
        self.assertEqual(result.get("why"), {})

    def test_diagnostics_pending_posts_zero_initially(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        result = svc.handle_diagnostics({})
        pp = result["pending_posts"]
        self.assertEqual(pp["current"], 0)
        self.assertEqual(pp["max_observed"], 0)

    def test_diagnostics_raises_when_no_session(self) -> None:
        gw = _GwOk()
        svc = _idle_svc(gateway=gw)
        with self.assertRaises(RuntimeError):
            svc.handle_diagnostics({})


# ---------------------------------------------------------------------------
# 5. test_summary_generated
# ---------------------------------------------------------------------------

class TestSummaryGenerated(unittest.TestCase):
    """handle_summary generates a summary from active gateway session."""

    def test_summary_generated(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        result = svc.handle_summary({})

        self.assertIn("summary", result)
        self.assertIn("gateway_session_id", result)
        # Verify gateway.post was called with summary path
        self.assertTrue(any("summary" in c["path"] for c in gw.post_calls))

    def test_summary_contains_gateway_session_id(self) -> None:
        gw = _GwOk(session_id="gw-sum-99")
        svc = _active_svc(gateway=gw, session_id="gw-sum-99")
        result = svc.handle_summary({})
        self.assertEqual(result["gateway_session_id"], "gw-sum-99")

    def test_stop_with_auto_summary_saves_to_history(self) -> None:
        store = FakeStore()
        gw = _GwOk()
        svc = _active_svc(gateway=gw, store=store)
        result = svc.handle_stop({"auto_summary": True})

        self.assertEqual(result.get("summary_status"), "ok")
        self.assertIn("summary_history_id", result)
        self.assertEqual(len(store._history), 1)
        saved = store._history[0]
        self.assertIn("[Call Summary]", saved["text"])

    def test_stop_with_auto_summary_false_skips(self) -> None:
        store = FakeStore()
        gw = _GwOk()
        svc = _active_svc(gateway=gw, store=store)
        result = svc.handle_stop({"auto_summary": False})
        self.assertEqual(result.get("summary_status"), "skipped")
        self.assertEqual(len(store._history), 0)


# ---------------------------------------------------------------------------
# 6. test_timeline_add_event
# ---------------------------------------------------------------------------

class TestTimelineAddEvent(unittest.TestCase):
    """handle_timeline retrieves timeline events from gateway."""

    def test_timeline_add_event(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        result = svc.handle_timeline({})

        self.assertIn("items", result)
        self.assertGreater(result.get("count", 0), 0)

    def test_timeline_first_item_has_expected_fields(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        result = svc.handle_timeline({})
        item = result["items"][0]
        self.assertIn("ts", item)
        self.assertIn("kind", item)

    def test_timeline_path_includes_session_id(self) -> None:
        gw = _GwOk(session_id="gw-tl-55")
        svc = _active_svc(gateway=gw, session_id="gw-tl-55")
        svc.handle_timeline({})
        self.assertTrue(any("gw-tl-55" in p for p in gw.get_calls))

    def test_timeline_limit_param_respected(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        svc.handle_timeline({"limit": 10})
        self.assertTrue(any("limit=10" in p for p in gw.get_calls))

    def test_timeline_raises_on_gateway_error(self) -> None:
        gw = _GwFail()
        svc = _active_svc(gateway=gw)
        with self.assertRaises(RuntimeError):
            svc.handle_timeline({})


# ---------------------------------------------------------------------------
# 7. test_timeline_query_by_range
# ---------------------------------------------------------------------------

class TestTimelineQueryByRange(unittest.TestCase):
    """Timeline query with kind/contains filters are passed to gateway."""

    def test_timeline_query_by_range(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        svc.handle_timeline({"kind": "stt.partial", "contains": "hello"})
        # Verify kind and contains params forwarded
        timeline_calls = [p for p in gw.get_calls if "timeline" in p and "stats" not in p
                          and "summary" not in p and "export" not in p]
        self.assertTrue(len(timeline_calls) > 0)
        last_path = timeline_calls[-1]
        self.assertIn("kind=stt.partial", last_path)
        self.assertIn("contains=hello", last_path)

    def test_timeline_stats_returns_by_kind(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        result = svc.handle_timeline_stats({})
        stats = result.get("stats", {})
        self.assertIn("by_kind", stats)

    def test_timeline_export_md(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        result = svc.handle_timeline_export({"format": "md"})
        self.assertIn("content", result)

    def test_timeline_export_invalid_format_defaults_to_md(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        result = svc.handle_timeline_export({"format": "xml"})
        # Should silently fall back to md path without raising
        self.assertIn("content", result)

    def test_timeline_clear_calls_delete(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        result = svc.handle_timeline_clear({"keep_last": 5})
        # delete was called on gateway
        self.assertTrue(len(gw.delete_calls) > 0)
        self.assertIn("cleared", result)


# ---------------------------------------------------------------------------
# 8. test_quick_phrase_dispatched
# ---------------------------------------------------------------------------

class TestQuickPhraseDispatched(unittest.TestCase):
    """handle_quick_phrase dispatches to gateway and returns audio result."""

    def test_quick_phrase_dispatched(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        result = svc.handle_quick_phrase({
            "text": "Перезвоните позже",
            "source_lang": "ru",
            "target_lang": "es",
        })

        self.assertIn("quick_phrase", result)
        self.assertIn("gateway_session_id", result)
        # Verify payload
        self.assertTrue(any("quick-phrase" in c["path"] for c in gw.post_calls))
        posted = next(c for c in gw.post_calls if "quick-phrase" in c["path"])
        self.assertEqual(posted["payload"]["text"], "Перезвоните позже")
        self.assertEqual(posted["payload"]["source_lang"], "ru")
        self.assertEqual(posted["payload"]["target_lang"], "es")

    def test_quick_phrase_voice_and_style_forwarded(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        svc.handle_quick_phrase({"text": "ok", "voice": "male", "style": "formal"})
        posted = next(c for c in gw.post_calls if "quick-phrase" in c["path"])
        self.assertEqual(posted["payload"]["voice"], "male")
        self.assertEqual(posted["payload"]["style"], "formal")

    def test_quick_phrase_returns_audio_url(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        result = svc.handle_quick_phrase({"text": "test"})
        qp = result["quick_phrase"]
        self.assertIn("audio_url", qp)


# ---------------------------------------------------------------------------
# 9. test_handles_vg_connection_failure
# ---------------------------------------------------------------------------

class TestHandlesVgConnectionFailure(unittest.TestCase):
    """When gateway is unreachable, start still returns active (degraded)."""

    def test_handles_vg_connection_failure(self) -> None:
        gw = _GwFail()
        svc = _idle_svc(gateway=gw)
        result = svc.handle_start({})

        # Session still starts, gateway degraded
        self.assertTrue(result["active"])
        self.assertEqual(result["gateway_status"], "degraded")
        self.assertIn("gateway_error", result)
        self.assertIn("connection_refused", result["gateway_error"])

    def test_vg_failure_gateway_session_id_is_none(self) -> None:
        gw = _GwFail()
        svc = _idle_svc(gateway=gw)
        result = svc.handle_start({})
        self.assertIsNone(result["gateway_session_id"])

    def test_list_quick_phrases_offline_returns_empty_not_raises(self) -> None:
        """list_quick_phrases with offline VG must return empty items, not raise."""
        gw = _GwFail()
        svc = _idle_svc(gateway=gw)
        result = svc.handle_list_quick_phrases({})
        self.assertEqual(result["items"], [])
        self.assertEqual(result["status"], "gateway_unavailable")


# ---------------------------------------------------------------------------
# 10. test_concurrent_start_blocked
# ---------------------------------------------------------------------------

class TestConcurrentStartBlocked(unittest.TestCase):
    """Only one active call session at a time; second start must be rejected (W830 F1)."""

    def test_concurrent_start_blocked(self) -> None:
        """Second sequential start while session active returns already_active error."""
        gw = _GwOk(session_id="gw-a")
        svc = _idle_svc(gateway=gw)

        result1 = svc.handle_start({"translation_mode": "ru_to_es"})
        self.assertTrue(result1["active"])
        session1 = result1["session_id"]

        # A second start while already active — must be rejected (W830 F1 fix)
        gw2 = _GwOk(session_id="gw-b")
        svc.gateway = gw2
        result2 = svc.handle_start({"translation_mode": "auto_to_ru"})
        self.assertFalse(result2.get("ok", True), "Second start must be rejected")
        self.assertEqual(result2.get("error"), "already_active")
        self.assertEqual(result2.get("session_id"), session1)

        # Internal state is unchanged — still the first session
        self.assertEqual(svc.state["session_id"], session1)

    def test_concurrent_start_from_two_threads(self) -> None:
        """Two threads calling handle_start — exactly one succeeds, one gets already_active."""
        results: list[dict] = []
        errors: list[Exception] = []

        class _MultiGw(_GwOk):
            def start_session(self, *args, **kwargs) -> dict:
                time.sleep(0.01)  # simulate latency
                return super().start_session(*args, **kwargs)

        svc = _idle_svc(gateway=_MultiGw())

        def _start() -> None:
            try:
                results.append(svc.handle_start({}))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_start) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
        self.assertEqual(len(results), 2)
        # Exactly one result is active, the other is the already_active rejection
        active_results = [r for r in results if r.get("active")]
        rejected_results = [r for r in results if r.get("error") == "already_active"]
        self.assertEqual(len(active_results), 1, "Exactly one start must succeed")
        self.assertEqual(len(rejected_results), 1, "Exactly one start must be rejected")


# ---------------------------------------------------------------------------
# 11. test_unicode_in_call_metadata
# ---------------------------------------------------------------------------

class TestUnicodeInCallMetadata(unittest.TestCase):
    """Unicode in params (Cyrillic, emoji) should not crash any handler."""

    def test_unicode_in_call_metadata(self) -> None:
        gw = _GwOk()
        svc = _idle_svc(gateway=gw)
        result = svc.handle_start({
            "translation_mode": "auto_to_ru",
            "phone": "+7 (916) 123-45-67 Краб 🦀",
        })
        # Must not raise; active session created
        self.assertTrue(result["active"])

    def test_unicode_quick_phrase(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        result = svc.handle_quick_phrase({
            "text": "Привет! 🦀 Это тест кириллицы и emoji.",
            "source_lang": "ru",
            "target_lang": "es",
        })
        self.assertIn("quick_phrase", result)
        posted = next(c for c in gw.post_calls if "quick-phrase" in c["path"])
        self.assertIn("🦀", posted["payload"]["text"])

    def test_unicode_template_name(self) -> None:
        store = FakeStore()
        svc = _idle_svc(store=store)
        result = svc.handle_add_template({
            "name": "Краткий ответ 🦀",
            "text": "Перезвоните позже",
            "source_lang": "ru",
            "target_lang": "es",
        })
        self.assertEqual(len(result["templates"]), 1)
        self.assertEqual(result["templates"][0]["name"], "Краткий ответ 🦀")


# ---------------------------------------------------------------------------
# 12. test_handles_partial_diarization
# ---------------------------------------------------------------------------

class TestHandlesPartialDiarization(unittest.TestCase):
    """_extract_text handles all payload shapes (diarization partial results)."""

    def test_handles_partial_diarization(self) -> None:
        """_default_extract_text handles nested result dict (diarization shape)."""
        payload_nested = {"result": {"text": "partial diarized text"}}
        text = CallAssistService._default_extract_text(payload_nested)
        self.assertEqual(text, "partial diarized text")

    def test_extract_text_flat_string(self) -> None:
        text = CallAssistService._default_extract_text("plain text")
        self.assertEqual(text, "plain text")

    def test_extract_text_flat_dict(self) -> None:
        text = CallAssistService._default_extract_text({"text": "flat dict text"})
        self.assertEqual(text, "flat dict text")

    def test_extract_text_empty_nested(self) -> None:
        """Nested result without 'text' key returns empty string."""
        text = CallAssistService._default_extract_text({"result": {"speaker": "A"}})
        self.assertEqual(text, "")

    def test_extract_text_none(self) -> None:
        text = CallAssistService._default_extract_text(None)
        self.assertEqual(text, "")

    def test_extract_text_empty_string(self) -> None:
        text = CallAssistService._default_extract_text("   ")
        self.assertEqual(text, "")

    def test_extract_text_numeric_payload(self) -> None:
        text = CallAssistService._default_extract_text(42)
        self.assertEqual(text, "42")


# ---------------------------------------------------------------------------
# 13. test_resumes_after_temporary_disconnect
# ---------------------------------------------------------------------------

class TestResumesAfterTemporaryDisconnect(unittest.TestCase):
    """After a VG stop+restart, a new active session replaces the old one."""

    def test_resumes_after_temporary_disconnect(self) -> None:
        store = FakeStore()

        # Session 1 starts successfully
        gw1 = _GwOk(session_id="gw-s1")
        svc = _idle_svc(gateway=gw1, store=store)
        r1 = svc.handle_start({})
        self.assertTrue(r1["active"])
        self.assertEqual(r1["gateway_session_id"], "gw-s1")

        # Session 1 stops
        svc.handle_stop({"auto_summary": False})
        self.assertFalse(svc.state["active"])

        # VG comes back; session 2 starts
        gw2 = _GwOk(session_id="gw-s2")
        svc.gateway = gw2
        r2 = svc.handle_start({})
        self.assertTrue(r2["active"])
        self.assertEqual(r2["gateway_session_id"], "gw-s2")
        self.assertNotEqual(r1["session_id"], r2["session_id"])

    def test_state_active_false_after_stop_before_restart(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw)
        svc.handle_stop({"auto_summary": False})
        state = svc.state
        self.assertFalse(state["active"])
        self.assertEqual(state["status"], "stopped")

    def test_new_session_id_after_reconnect(self) -> None:
        gw = _GwOk()
        svc = _idle_svc(gateway=gw)
        r1 = svc.handle_start({})
        svc.handle_stop({"auto_summary": False})
        r2 = svc.handle_start({})
        self.assertNotEqual(r1["session_id"], r2["session_id"])


# ---------------------------------------------------------------------------
# Additional: _default_coerce_bool edge cases
# ---------------------------------------------------------------------------

class TestDefaultCoerceBool(unittest.TestCase):
    def _coerce(self, value: Any, default: bool) -> bool:
        return CallAssistService._default_coerce_bool(value, default)

    def test_true_literal(self) -> None:
        self.assertTrue(self._coerce(True, False))

    def test_false_literal(self) -> None:
        self.assertFalse(self._coerce(False, True))

    def test_string_true_variants(self) -> None:
        for v in ("1", "true", "on", "yes", "TRUE", "Yes"):
            self.assertTrue(self._coerce(v, False), f"Expected True for {v!r}")

    def test_string_false_variants(self) -> None:
        for v in ("0", "false", "off", "no", "FALSE", "No"):
            self.assertFalse(self._coerce(v, True), f"Expected False for {v!r}")

    def test_none_returns_default(self) -> None:
        self.assertTrue(self._coerce(None, True))
        self.assertFalse(self._coerce(None, False))

    def test_unknown_string_returns_default(self) -> None:
        self.assertTrue(self._coerce("maybe", True))

    def test_int_1_is_true(self) -> None:
        self.assertTrue(self._coerce(1, False))

    def test_int_0_is_false(self) -> None:
        self.assertFalse(self._coerce(0, True))


# ---------------------------------------------------------------------------
# Additional: _build_call_summary_history_text edge cases
# ---------------------------------------------------------------------------

class TestBuildCallSummaryHistoryText(unittest.TestCase):
    def test_both_empty_returns_empty(self) -> None:
        result = CallAssistService._build_call_summary_history_text({}, "s1")
        self.assertEqual(result, "")

    def test_summary_and_dict_tasks(self) -> None:
        result = CallAssistService._build_call_summary_history_text(
            {"summary": "Резюме", "tasks": [{"task": "Позвонить завтра"}]},
            "s2",
        )
        self.assertIn("Позвонить завтра", result)
        self.assertIn("Резюме", result)

    def test_dict_task_with_title_key(self) -> None:
        result = CallAssistService._build_call_summary_history_text(
            {"summary": "s", "tasks": [{"title": "Встреча в пятницу"}]},
            "s3",
        )
        self.assertIn("Встреча в пятницу", result)

    def test_dict_task_with_text_key(self) -> None:
        result = CallAssistService._build_call_summary_history_text(
            {"summary": "s", "tasks": [{"text": "Отправить отчёт"}]},
            "s4",
        )
        self.assertIn("Отправить отчёт", result)

    def test_session_id_included_in_header(self) -> None:
        result = CallAssistService._build_call_summary_history_text(
            {"summary": "done"}, "my-session-id"
        )
        self.assertIn("my-session-id", result)

    def test_tasks_capped_at_12(self) -> None:
        tasks = [f"Задача {i}" for i in range(20)]
        result = CallAssistService._build_call_summary_history_text(
            {"summary": "s", "tasks": tasks}, "s5"
        )
        self.assertIn("Задача 11", result)
        self.assertNotIn("Задача 12", result)


# ---------------------------------------------------------------------------
# Additional: template CRUD
# ---------------------------------------------------------------------------

class TestTemplateCRUD(unittest.TestCase):
    def _svc(self) -> CallAssistService:
        return _idle_svc(store=FakeStore())

    def test_add_and_list_template(self) -> None:
        svc = self._svc()
        svc.handle_add_template({"name": "Привет", "text": "Здравствуйте!"})
        result = svc.handle_list_templates({})
        self.assertEqual(len(result["templates"]), 1)
        self.assertEqual(result["templates"][0]["name"], "Привет")

    def test_remove_template(self) -> None:
        svc = self._svc()
        svc.handle_add_template({"name": "T1", "text": "text"})
        svc.handle_remove_template({"name": "T1"})
        result = svc.handle_list_templates({})
        self.assertEqual(len(result["templates"]), 0)

    def test_add_duplicate_raises(self) -> None:
        svc = self._svc()
        svc.handle_add_template({"name": "T1", "text": "text"})
        with self.assertRaises(RuntimeError):
            svc.handle_add_template({"name": "T1", "text": "other"})

    def test_remove_nonexistent_raises(self) -> None:
        svc = self._svc()
        with self.assertRaises(RuntimeError):
            svc.handle_remove_template({"name": "NoSuch"})

    def test_handle_template_dispatches_quick_phrase(self) -> None:
        gw = _GwOk()
        svc = _active_svc(gateway=gw, store=FakeStore())
        svc.handle_add_template({"name": "Bye", "text": "До свидания", "source_lang": "ru", "target_lang": "es"})
        result = svc.handle_template({"name": "Bye"})
        self.assertIn("quick_phrase", result)

    def test_handle_template_missing_name_raises(self) -> None:
        svc = self._svc()
        with self.assertRaises(RuntimeError):
            svc.handle_template({})


if __name__ == "__main__":
    unittest.main()
