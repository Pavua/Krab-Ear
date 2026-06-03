"""Tests for wave-31 fixes in call_session_service.py and call_assist_service.py.

C1 (HIGH) — call_session_service: privacy gate on get/list (phone + transcript redaction)
C2 (MED)  — call_assist_service: privacy gate on diagnostics/summary/timeline
C3 (LOW)  — call_assist_service: handle_stop only stops recorder if call assist was active
C4 (LOW)  — call_session_service: call_session_end on IDLE → graceful ok:False instead of crash
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_session_service import CallSessionService  # noqa: E402
from backend.call_assist_service import CallAssistService    # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session_dict(
    session_id: str = "sess-001",
    status: str = "idle",
    phone: str = "+1234567890",
) -> dict:
    return {
        "id": session_id,
        "status": status,
        "phone_number": phone,
        "goal_text": "Test goal",
        "created_at": "2026-06-03T10:00:00",
        "transcript_history": [{"speaker": "agent", "text": "Привет", "ts": "2026-06-03T10:00:01"}],
        "started_at": None,
        "ended_at": None,
        "cost_usd": 0.0,
        "operator_interruptions": [],
        "end_reason": None,
        "duration_sec": None,
    }


def _make_session_mock(
    session_id: str = "sess-001",
    status: str = "idle",
    phone: str = "+1234567890",
) -> MagicMock:
    s = MagicMock()
    s.id = session_id
    s.status = status
    s.phone_number = phone
    s.goal_text = "Test goal"
    s.created_at = "2026-06-03T10:00:00"
    s.duration_sec = None
    s.cost_usd = 0.0
    s.end_reason = None
    s.transcript_history = [MagicMock()]
    s.to_dict.return_value = _make_session_dict(session_id, status, phone)
    return s


def _make_call_session_svc(privacy: bool = False) -> tuple[CallSessionService, MagicMock]:
    store = MagicMock()
    svc = CallSessionService(
        store=store,
        settings_get=lambda k, d: privacy if k == "privacy_mode_enabled" else d,
    )
    return svc, store


def _make_call_assist_svc(privacy: bool = False) -> CallAssistService:
    store = MagicMock()
    store.load_settings.return_value = {
        "voice_gateway_url": "http://127.0.0.1:8090",
        "voice_gateway_api_key": "",
    }
    recorder = MagicMock()
    recorder.is_recording = False
    transcriber = MagicMock()
    svc = CallAssistService(
        store=store,
        recorder=recorder,
        transcriber=transcriber,
        settings_get=lambda k, d: privacy if k == "privacy_mode_enabled" else d,
    )
    return svc


# ===========================================================================
# C1 — call_session_get privacy gate
# ===========================================================================

class TestC1CallSessionGetPrivacy(unittest.TestCase):

    def test_get_normal_mode_returns_full_session(self) -> None:
        """Without privacy mode, full session including phone returned."""
        svc, store = _make_call_session_svc(privacy=False)
        store.get.return_value = _make_session_mock(phone="+79991234567")

        result = svc.handle_call_session_get({"id": "sess-001"})

        self.assertEqual(result["phone_number"], "+79991234567")
        self.assertTrue(len(result["transcript_history"]) > 0)

    def test_get_privacy_mode_redacts_phone(self) -> None:
        """Privacy mode: phone_number is REDACTED."""
        svc, store = _make_call_session_svc(privacy=True)
        store.get.return_value = _make_session_mock(phone="+79991234567")

        result = svc.handle_call_session_get({"id": "sess-001"})

        self.assertEqual(result["phone_number"], "REDACTED")

    def test_get_privacy_mode_clears_transcript(self) -> None:
        """Privacy mode: transcript_history is empty list."""
        svc, store = _make_call_session_svc(privacy=True)
        store.get.return_value = _make_session_mock()

        result = svc.handle_call_session_get({"id": "sess-001"})

        self.assertEqual(result["transcript_history"], [])

    def test_get_privacy_mode_preserves_schema(self) -> None:
        """Privacy mode: all other keys still present (schema parity)."""
        svc, store = _make_call_session_svc(privacy=True)
        store.get.return_value = _make_session_mock(session_id="s42", status="completed")

        result = svc.handle_call_session_get({"id": "s42"})

        for key in ("id", "status", "goal_text", "created_at"):
            self.assertIn(key, result, f"Expected key {key!r} in privacy-mode response")
        self.assertEqual(result["id"], "s42")
        self.assertEqual(result["status"], "completed")

    def test_get_not_found_still_raises(self) -> None:
        """Not-found KeyError is not affected by privacy mode."""
        svc, store = _make_call_session_svc(privacy=True)
        store.get.return_value = None

        with self.assertRaises(KeyError):
            svc.handle_call_session_get({"id": "ghost"})


# ===========================================================================
# C1 — call_session_list privacy gate
# ===========================================================================

class TestC1CallSessionListPrivacy(unittest.TestCase):

    def test_list_normal_mode_returns_full(self) -> None:
        """Without privacy mode, full sessions are returned."""
        svc, store = _make_call_session_svc(privacy=False)
        store.list_sessions.return_value = [
            _make_session_dict(phone="+7001"),
            _make_session_dict(session_id="s2", phone="+7002"),
        ]

        result = svc.handle_call_session_list({})

        self.assertEqual(result["total"], 2)
        phones = [s["phone_number"] for s in result["sessions"]]
        self.assertIn("+7001", phones)
        self.assertIn("+7002", phones)

    def test_list_privacy_mode_redacts_phones(self) -> None:
        """Privacy mode: all phone numbers are REDACTED."""
        svc, store = _make_call_session_svc(privacy=True)
        store.list_sessions.return_value = [
            _make_session_dict(phone="+7001"),
            _make_session_dict(session_id="s2", phone="+7002"),
        ]

        result = svc.handle_call_session_list({})

        self.assertEqual(result["total"], 2)
        for s in result["sessions"]:
            self.assertEqual(s["phone_number"], "REDACTED")

    def test_list_privacy_mode_clears_transcripts(self) -> None:
        """Privacy mode: transcript_history is empty in each session."""
        svc, store = _make_call_session_svc(privacy=True)
        store.list_sessions.return_value = [_make_session_dict()]

        result = svc.handle_call_session_list({})

        for s in result["sessions"]:
            self.assertEqual(s["transcript_history"], [])

    def test_list_privacy_mode_empty_list(self) -> None:
        """Privacy mode: empty list works correctly."""
        svc, store = _make_call_session_svc(privacy=True)
        store.list_sessions.return_value = []

        result = svc.handle_call_session_list({})

        self.assertEqual(result["sessions"], [])
        self.assertEqual(result["total"], 0)


# ===========================================================================
# C2 — call_assist diagnostics/summary/timeline privacy gate
# ===========================================================================

class TestC2CallAssistPrivacy(unittest.TestCase):

    def test_diagnostics_privacy_mode_returns_empty(self) -> None:
        """Privacy mode: diagnostics returns empty payload without live transcripts."""
        svc = _make_call_assist_svc(privacy=True)

        result = svc.handle_diagnostics({})

        self.assertIn("active", result)
        self.assertIn("diagnostics", result)
        self.assertIn("why", result)
        self.assertEqual(result["diagnostics"], {})
        self.assertEqual(result["why"], {})
        self.assertIsNone(result["gateway_session_id"])
        self.assertTrue(result.get("privacy_mode_active"))

    def test_diagnostics_normal_mode_checks_gateway(self) -> None:
        """Normal mode: diagnostics raises RuntimeError if no gateway session."""
        svc = _make_call_assist_svc(privacy=False)
        # No active gateway session → RuntimeError
        with self.assertRaises(RuntimeError):
            svc.handle_diagnostics({})

    def test_summary_privacy_mode_returns_empty(self) -> None:
        """Privacy mode: summary returns empty payload without transcript data."""
        svc = _make_call_assist_svc(privacy=True)

        result = svc.handle_summary({})

        self.assertIn("summary", result)
        self.assertEqual(result["summary"], {})
        self.assertIsNone(result["gateway_session_id"])
        self.assertTrue(result.get("privacy_mode_active"))

    def test_summary_normal_mode_checks_gateway(self) -> None:
        """Normal mode: summary raises RuntimeError if no gateway session."""
        svc = _make_call_assist_svc(privacy=False)
        with self.assertRaises(RuntimeError):
            svc.handle_summary({})

    def test_timeline_privacy_mode_returns_empty(self) -> None:
        """Privacy mode: timeline returns empty items list."""
        svc = _make_call_assist_svc(privacy=True)

        result = svc.handle_timeline({})

        self.assertIn("items", result)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["count"], 0)
        self.assertTrue(result.get("privacy_mode_active"))

    def test_timeline_normal_mode_checks_gateway(self) -> None:
        """Normal mode: timeline raises RuntimeError if no gateway session."""
        svc = _make_call_assist_svc(privacy=False)
        with self.assertRaises(RuntimeError):
            svc.handle_timeline({})


# ===========================================================================
# C3 — handle_stop only stops recorder when call assist was active
# ===========================================================================

class TestC3RecorderStopGuard(unittest.TestCase):

    def test_stop_when_active_stops_recorder(self) -> None:
        """When a session is active and recorder is running, stop() is called."""
        svc = _make_call_assist_svc()
        svc._state["active"] = True
        svc._state["status"] = "running"
        svc.recorder.is_recording = True

        svc.handle_stop({})

        svc.recorder.stop.assert_called_once()

    def test_stop_when_idle_does_not_stop_recorder(self) -> None:
        """When no session was active, stop() must NOT be called on the recorder.

        This prevents aborting a separate unrelated recording running in the
        background when call_assist was never started.
        """
        svc = _make_call_assist_svc()
        # Ensure state starts as idle (default)
        svc._state["active"] = False
        svc._state["status"] = "idle"
        svc.recorder.is_recording = True  # simulates an unrelated recording

        svc.handle_stop({})

        svc.recorder.stop.assert_not_called()

    def test_stop_when_active_but_recorder_not_recording_no_error(self) -> None:
        """Guard must not crash when recorder.is_recording is False."""
        svc = _make_call_assist_svc()
        svc._state["active"] = True
        svc._state["status"] = "running"
        svc.recorder.is_recording = False

        # Should complete without error
        result = svc.handle_stop({})
        svc.recorder.stop.assert_not_called()
        self.assertIn("active", result)


# ===========================================================================
# C4 — call_session_end on IDLE → graceful ok:False
# ===========================================================================

class TestC4CallSessionEndIdleGrace(unittest.TestCase):

    def _make_svc_with_idle_session(self) -> tuple[CallSessionService, MagicMock]:
        """Returns a service whose store raises ValueError on mark_completed (IDLE FSM guard)."""
        svc, store = _make_call_session_svc()
        # Simulate FSM rejection: IDLE → COMPLETED is invalid
        store.mark_completed.side_effect = ValueError(
            "Недопустимый переход: 'idle' → 'completed'. Допустимые: ['dialing']"
        )
        store.mark_failed.side_effect = ValueError(
            "Недопустимый переход: 'idle' → 'failed'. Допустимые: ['dialing']"
        )
        # get() returns the idle session for current_state lookup
        store.get.return_value = _make_session_mock(status="idle")
        return svc, store

    def test_end_idle_returns_ok_false(self) -> None:
        """FSM ValueError on IDLE session → graceful ok:False dict."""
        svc, _ = self._make_svc_with_idle_session()

        result = svc.handle_call_session_end({"id": "sess-001"})

        self.assertFalse(result.get("ok"), "Expected ok=False for invalid FSM transition")

    def test_end_idle_includes_reason(self) -> None:
        """Graceful error response includes reason=invalid_state_transition."""
        svc, _ = self._make_svc_with_idle_session()

        result = svc.handle_call_session_end({"id": "sess-001"})

        self.assertEqual(result.get("reason"), "invalid_state_transition")

    def test_end_idle_includes_current_state(self) -> None:
        """Graceful error response includes current_state=idle."""
        svc, _ = self._make_svc_with_idle_session()

        result = svc.handle_call_session_end({"id": "sess-001"})

        self.assertEqual(result.get("current_state"), "idle")

    def test_end_idle_failed_flag_same_behavior(self) -> None:
        """Same graceful handling when failed=True on IDLE session."""
        svc, _ = self._make_svc_with_idle_session()

        result = svc.handle_call_session_end({"id": "sess-001", "failed": True})

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("reason"), "invalid_state_transition")

    def test_end_other_exception_still_propagates(self) -> None:
        """Non-ValueError exceptions (e.g. KeyError) must still propagate."""
        svc, store = _make_call_session_svc()
        store.mark_completed.side_effect = KeyError("Сессия не найдена")

        with self.assertRaises(KeyError):
            svc.handle_call_session_end({"id": "ghost"})

    def test_end_valid_session_returns_normal_result(self) -> None:
        """Normal (non-IDLE) session end path still works correctly."""
        svc, store = _make_call_session_svc()
        sess = _make_session_mock(session_id="s1", status="completed")
        sess.duration_sec = 120.0
        sess.cost_usd = 0.05
        sess.end_reason = "completed"
        store.mark_completed.return_value = sess

        result = svc.handle_call_session_end({"id": "s1"})

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["duration_sec"], 120.0)


if __name__ == "__main__":
    unittest.main()
