"""Edge-case unit tests for CallAssistService (Part B of #115 coverage task)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_assist_service import CallAssistService, VoiceGatewayClient  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self) -> None:
        self._settings: dict[str, Any] = {
            "voice_gateway_url": "http://127.0.0.1:8090",
            "voice_gateway_api_key": "",
        }

    def load_settings(self, lock_timeout_sec: float | None = None, nowait: bool = False) -> dict[str, Any]:
        return dict(self._settings)

    def save_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        self._settings = dict(settings)
        return dict(settings)


class FakeRecorder:
    is_recording = False

    def start(self) -> bool:
        self.is_recording = True
        return True

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        import numpy as np
        return np.zeros(int(max_duration_sec * 16000), dtype=np.float32), max_duration_sec


class FakeTranscriber:
    def transcribe_preview(self, audio_data: Any, quality_profile: str = "balanced") -> dict:
        return {"text": "hello"}


class MockGatewayOk(VoiceGatewayClient):
    """Gateway that always returns success."""

    def __init__(self, session_id: str = "gw-edge-001") -> None:
        self._session_id = session_id
        self.post_calls: list[dict[str, Any]] = []

    def start_session(self, voice_gateway_url: str, api_key: str, payload: dict) -> dict:
        return {"ok": True, "session_id": self._session_id}

    def stop_session(self, voice_gateway_url: str, api_key: str, session_id: str) -> dict:
        return {"ok": True}

    def get(self, voice_gateway_url: str, api_key: str, path: str) -> dict:
        return {"ok": True, "payload": {"status": "healthy", "pipeline_ok": True}}

    def post(self, voice_gateway_url: str, api_key: str, path: str, payload: dict) -> dict:
        self.post_calls.append({"path": path, "payload": payload})
        return {"ok": True, "payload": {"result": "ok"}}

    def delete(self, voice_gateway_url: str, api_key: str, path: str) -> dict:
        return {"ok": True, "payload": {}}


class MockGatewayFail(VoiceGatewayClient):
    """Gateway that always returns failure."""

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


def _make_idle_service(gateway: VoiceGatewayClient) -> CallAssistService:
    return CallAssistService(
        store=FakeStore(),
        recorder=FakeRecorder(),
        transcriber=FakeTranscriber(),
        gateway=gateway,
    )


def _make_active_service(gateway: VoiceGatewayClient, session_id: str = "gw-edge-001") -> CallAssistService:
    svc = _make_idle_service(gateway)
    with svc._lock:
        svc._state = {
            "active": True,
            "status": "running",
            "session_id": "call_abc123",
            "gateway_session_id": session_id,
        }
    return svc


# ---------------------------------------------------------------------------
# A. handle_start edge cases
# ---------------------------------------------------------------------------

class TestHandleStartEdgeCases(unittest.TestCase):
    """handle_start: invalid capture_source_mode is sanitised to 'mic'."""

    def test_invalid_capture_source_mode_defaults_to_mic(self) -> None:
        gw = MockGatewayOk()
        svc = _make_idle_service(gw)
        result = svc.handle_start({"capture_source_mode": "INVALID_VALUE"})
        self.assertEqual(result["capture_source_mode"], "mic")

    def test_invalid_tts_mode_defaults_to_hybrid(self) -> None:
        gw = MockGatewayOk()
        svc = _make_idle_service(gw)
        result = svc.handle_start({"tts_mode": "BOGUS"})
        self.assertEqual(result["tts_mode"], "hybrid")

    def test_gateway_degraded_on_fail_still_returns_active(self) -> None:
        gw = MockGatewayFail()
        svc = _make_idle_service(gw)
        result = svc.handle_start({})
        self.assertTrue(result["active"])
        self.assertEqual(result["gateway_status"], "degraded")
        self.assertIn("gateway_error", result)

    def test_valid_capture_source_modes_accepted(self) -> None:
        for mode in ("mic", "system_audio", "mic_plus_system"):
            gw = MockGatewayOk()
            svc = _make_idle_service(gw)
            result = svc.handle_start({"capture_source_mode": mode})
            self.assertEqual(result["capture_source_mode"], mode)


# ---------------------------------------------------------------------------
# B. handle_stop idempotent when already stopped
# ---------------------------------------------------------------------------

class TestHandleStopIdempotent(unittest.TestCase):
    """handle_stop when already stopped must not raise and return idle state."""

    def test_stop_already_stopped_is_idempotent(self) -> None:
        gw = MockGatewayOk()
        svc = _make_idle_service(gw)
        # Call stop twice — first call transitions from idle, second is no-op.
        result1 = svc.handle_stop({})
        result2 = svc.handle_stop({})
        self.assertFalse(result1["active"])
        self.assertFalse(result2["active"])
        self.assertEqual(result2["status"], "idle")

    def test_stop_active_then_stop_again_idempotent(self) -> None:
        gw = MockGatewayOk()
        svc = _make_active_service(gw)
        result1 = svc.handle_stop({"auto_summary": False})
        self.assertFalse(result1["active"])
        # Second stop while already stopped.
        result2 = svc.handle_stop({"auto_summary": False})
        self.assertFalse(result2["active"])
        self.assertEqual(result2["status"], "idle")

    def test_stop_when_no_gateway_session_skips_gateway(self) -> None:
        """Stop with no gateway_session_id must set gateway_stop_status=skipped."""
        gw = MockGatewayFail()
        svc = _make_idle_service(gw)
        # Inject active state WITHOUT a gateway_session_id.
        with svc._lock:
            svc._state = {
                "active": True,
                "status": "running",
                "session_id": "call_xyz",
                "gateway_session_id": None,
            }
        result = svc.handle_stop({"auto_summary": False})
        self.assertFalse(result["active"])
        self.assertEqual(result.get("gateway_stop_status"), "skipped")


# ---------------------------------------------------------------------------
# C. handle_diagnostics — pending_posts field (from C4 backpressure tracking)
# ---------------------------------------------------------------------------

class TestHandleDiagnosticsPendingPosts(unittest.TestCase):
    """handle_diagnostics exposes pending_posts with current/max_observed."""

    def test_pending_posts_present_in_diagnostics(self) -> None:
        gw = MockGatewayOk()
        svc = _make_active_service(gw)
        with svc._lock:
            svc._pending_post_count = 2
            svc._max_pending_post_depth_observed = 5

        result = svc.handle_diagnostics({})

        self.assertIn("pending_posts", result)
        pp = result["pending_posts"]
        self.assertEqual(pp["current"], 2)
        self.assertEqual(pp["max_observed"], 5)

    def test_diagnostics_includes_active_and_session(self) -> None:
        gw = MockGatewayOk()
        svc = _make_active_service(gw, session_id="gw-diag-42")
        result = svc.handle_diagnostics({"include_why": False})
        self.assertTrue(result["active"])
        self.assertEqual(result["gateway_session_id"], "gw-diag-42")
        self.assertIn("diagnostics", result)

    def test_diagnostics_raises_when_no_active_session(self) -> None:
        gw = MockGatewayOk()
        svc = _make_idle_service(gw)
        with self.assertRaises(RuntimeError):
            svc.handle_diagnostics({})

    def test_diagnostics_raises_when_gateway_fails(self) -> None:
        gw = MockGatewayFail()
        svc = _make_active_service(gw)
        with self.assertRaises(RuntimeError):
            svc.handle_diagnostics({})


# ---------------------------------------------------------------------------
# D. handle_summary — graceful response when no active session
# ---------------------------------------------------------------------------

class TestHandleSummaryNoSession(unittest.TestCase):
    """handle_summary raises RuntimeError gracefully when no gateway session."""

    def test_summary_raises_without_active_session(self) -> None:
        gw = MockGatewayOk()
        svc = _make_idle_service(gw)
        with self.assertRaises(RuntimeError) as ctx:
            svc.handle_summary({})
        self.assertIn("gateway", str(ctx.exception).lower())

    def test_summary_returns_payload_when_active(self) -> None:
        gw = MockGatewayOk()
        svc = _make_active_service(gw)
        result = svc.handle_summary({"max_items": 10})
        self.assertIn("summary", result)
        self.assertIn("gateway_session_id", result)

    def test_summary_raises_when_gateway_fails(self) -> None:
        gw = MockGatewayFail()
        svc = _make_active_service(gw)
        with self.assertRaises(RuntimeError):
            svc.handle_summary({})

    def test_summary_max_items_clamped(self) -> None:
        gw = MockGatewayOk()
        svc = _make_active_service(gw)
        # Should not raise even if max_items is out of range.
        result = svc.handle_summary({"max_items": 9999})
        self.assertIn("summary", result)


# ---------------------------------------------------------------------------
# E. handle_quick_phrase — forwards to gateway
# ---------------------------------------------------------------------------

class TestHandleQuickPhrase(unittest.TestCase):
    """handle_quick_phrase forwards text to gateway and returns payload."""

    def test_quick_phrase_forwarded_to_gateway(self) -> None:
        gw = MockGatewayOk()
        svc = _make_active_service(gw)
        result = svc.handle_quick_phrase({
            "text": "Hola, buenos días",
            "source_lang": "es",
            "target_lang": "ru",
        })
        self.assertIn("quick_phrase", result)
        self.assertIn("gateway_session_id", result)
        # Verify the post was actually dispatched.
        self.assertEqual(len(gw.post_calls), 1)
        posted = gw.post_calls[0]
        self.assertIn("quick-phrase", posted["path"])
        self.assertEqual(posted["payload"]["text"], "Hola, buenos días")
        self.assertEqual(posted["payload"]["source_lang"], "es")
        self.assertEqual(posted["payload"]["target_lang"], "ru")

    def test_quick_phrase_uses_default_langs(self) -> None:
        gw = MockGatewayOk()
        svc = _make_active_service(gw)
        result = svc.handle_quick_phrase({"text": "test phrase"})
        self.assertIn("quick_phrase", result)
        posted = gw.post_calls[0]
        self.assertEqual(posted["payload"]["source_lang"], "ru")
        self.assertEqual(posted["payload"]["target_lang"], "es")

    def test_quick_phrase_raises_with_empty_text(self) -> None:
        gw = MockGatewayOk()
        svc = _make_active_service(gw)
        with self.assertRaises(RuntimeError):
            svc.handle_quick_phrase({"text": ""})

    def test_quick_phrase_raises_with_missing_text(self) -> None:
        gw = MockGatewayOk()
        svc = _make_active_service(gw)
        with self.assertRaises(RuntimeError):
            svc.handle_quick_phrase({})

    def test_quick_phrase_raises_without_active_session(self) -> None:
        gw = MockGatewayOk()
        svc = _make_idle_service(gw)
        with self.assertRaises(RuntimeError):
            svc.handle_quick_phrase({"text": "hi"})

    def test_quick_phrase_raises_when_gateway_fails(self) -> None:
        gw = MockGatewayFail()
        svc = _make_active_service(gw)
        with self.assertRaises(RuntimeError):
            svc.handle_quick_phrase({"text": "hi"})


# ---------------------------------------------------------------------------
# F. handle_get_state
# ---------------------------------------------------------------------------

class TestHandleGetState(unittest.TestCase):
    def test_get_state_returns_copy(self) -> None:
        gw = MockGatewayOk()
        svc = _make_active_service(gw)
        state = svc.handle_get_state({})
        self.assertTrue(state["active"])
        # Mutating the returned copy must not affect internal state.
        state["active"] = False
        self.assertTrue(svc.state["active"])

    def test_get_state_idle(self) -> None:
        gw = MockGatewayOk()
        svc = _make_idle_service(gw)
        state = svc.handle_get_state({})
        self.assertFalse(state["active"])


# ---------------------------------------------------------------------------
# G. _build_call_summary_history_text static helper
# ---------------------------------------------------------------------------

class TestBuildCallSummaryText(unittest.TestCase):
    def test_empty_payload_returns_empty_string(self) -> None:
        result = CallAssistService._build_call_summary_history_text({}, "sess-1")
        self.assertEqual(result, "")

    def test_summary_only(self) -> None:
        result = CallAssistService._build_call_summary_history_text(
            {"summary": "Обсудили поставку."},
            "sess-2",
        )
        self.assertIn("[Call Summary]", result)
        self.assertIn("Обсудили поставку.", result)

    def test_tasks_included(self) -> None:
        result = CallAssistService._build_call_summary_history_text(
            {
                "summary": "Краткое резюме.",
                "tasks": ["Задача 1", "Задача 2"],
            },
            "sess-3",
        )
        self.assertIn("Задача 1", result)
        self.assertIn("Задача 2", result)

    def test_tasks_capped_at_12(self) -> None:
        tasks = [f"Задача {i}" for i in range(20)]
        result = CallAssistService._build_call_summary_history_text(
            {"summary": "s", "tasks": tasks},
            "sess-cap",
        )
        # At most 12 tasks should appear.
        self.assertIn("Задача 11", result)
        self.assertNotIn("Задача 12", result)


if __name__ == "__main__":
    unittest.main()
