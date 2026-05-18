"""Unit tests — CallSessionService (6 IPC handlers).

Tests each handler directly against mocked store + auto_end collaborators.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(
    session_id: str = "sess-001",
    status: str = "idle",
    phone: str = "+1234567890",
    goal: str = "Test goal",
    created_at: str = "2026-05-18T10:00:00",
    duration_sec: float | None = None,
    cost_usd: float = 0.0,
    end_reason: str | None = None,
    transcript_history: list | None = None,
) -> MagicMock:
    """Возвращает mock CallSession с атрибутами."""
    s = MagicMock()
    s.id = session_id
    s.status = status
    s.phone_number = phone
    s.goal_text = goal
    s.created_at = created_at
    s.duration_sec = duration_sec
    s.cost_usd = cost_usd
    s.end_reason = end_reason
    s.transcript_history = transcript_history or []
    s.to_dict.return_value = {
        "id": session_id,
        "status": status,
        "phone_number": phone,
        "goal_text": goal,
        "created_at": created_at,
    }
    return s


def _make_service() -> tuple[CallSessionService, MagicMock, MagicMock]:
    store = MagicMock()
    auto_end = MagicMock()
    svc = CallSessionService(store=store, auto_end=auto_end)
    return svc, store, auto_end


# ---------------------------------------------------------------------------
# handle_call_session_create
# ---------------------------------------------------------------------------

class TestCallSessionCreate(unittest.TestCase):
    def test_create_ok(self) -> None:
        svc, store, _ = _make_service()
        sess = _make_session(session_id="s1", status="idle", created_at="2026-05-18T10:00:00")
        store.create.return_value = sess

        result = svc.handle_call_session_create({"phone": "+7999", "goal_text": "Купить хлеб"})

        store.create.assert_called_once_with(phone_number="+7999", goal_text="Купить хлеб")
        self.assertEqual(result["session_id"], "s1")
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["created_at"], "2026-05-18T10:00:00")

    def test_create_missing_phone(self) -> None:
        svc, _, _ = _make_service()
        with self.assertRaises(ValueError, msg="phone required"):
            svc.handle_call_session_create({"goal_text": "goal"})

    def test_create_empty_phone(self) -> None:
        svc, _, _ = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_call_session_create({"phone": "   ", "goal_text": "goal"})

    def test_create_missing_goal(self) -> None:
        svc, _, _ = _make_service()
        with self.assertRaises(ValueError, msg="goal_text required"):
            svc.handle_call_session_create({"phone": "+1234"})


# ---------------------------------------------------------------------------
# handle_call_session_get
# ---------------------------------------------------------------------------

class TestCallSessionGet(unittest.TestCase):
    def test_get_existing_session(self) -> None:
        svc, store, _ = _make_service()
        sess = _make_session(session_id="s1")
        store.get.return_value = sess

        result = svc.handle_call_session_get({"id": "s1"})

        store.get.assert_called_once_with("s1")
        self.assertIn("id", result)

    def test_get_not_found_raises(self) -> None:
        svc, store, _ = _make_service()
        store.get.return_value = None

        with self.assertRaises(KeyError):
            svc.handle_call_session_get({"id": "nonexistent"})

    def test_get_missing_id_raises(self) -> None:
        svc, _, _ = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_call_session_get({})


# ---------------------------------------------------------------------------
# handle_call_session_list
# ---------------------------------------------------------------------------

class TestCallSessionList(unittest.TestCase):
    def test_list_default_params(self) -> None:
        svc, store, _ = _make_service()
        store.list_sessions.return_value = [{"id": "s1"}, {"id": "s2"}]

        result = svc.handle_call_session_list({})

        store.list_sessions.assert_called_once_with(limit=50, status_filter=None)
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["sessions"]), 2)

    def test_list_with_status_filter(self) -> None:
        svc, store, _ = _make_service()
        store.list_sessions.return_value = [{"id": "s3"}]

        result = svc.handle_call_session_list({"limit": 10, "status_filter": "completed"})

        store.list_sessions.assert_called_once_with(limit=10, status_filter="completed")
        self.assertEqual(result["total"], 1)

    def test_list_limit_clamped(self) -> None:
        svc, store, _ = _make_service()
        store.list_sessions.return_value = []

        svc.handle_call_session_list({"limit": 9999})

        call_kwargs = store.list_sessions.call_args
        self.assertEqual(call_kwargs.kwargs["limit"], 500)


# ---------------------------------------------------------------------------
# handle_call_session_update_status
# ---------------------------------------------------------------------------

class TestCallSessionUpdateStatus(unittest.TestCase):
    def test_update_status_ok(self) -> None:
        svc, store, _ = _make_service()
        sess = _make_session(session_id="s1", status="dialing")
        store.update_status.return_value = sess

        result = svc.handle_call_session_update_status({"id": "s1", "status": "dialing"})

        store.update_status.assert_called_once_with(session_id="s1", new_status="dialing")
        self.assertEqual(result["session_id"], "s1")
        self.assertEqual(result["status"], "dialing")

    def test_update_status_missing_id(self) -> None:
        svc, _, _ = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_call_session_update_status({"status": "dialing"})

    def test_update_status_missing_status(self) -> None:
        svc, _, _ = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_call_session_update_status({"id": "s1"})

    def test_update_status_invalid_transition_propagates(self) -> None:
        """Если store.update_status бросает ValueError — он пробрасывается наружу."""
        svc, store, _ = _make_service()
        store.update_status.side_effect = ValueError("Недопустимый переход")

        with self.assertRaises(ValueError, msg="invalid transition should propagate"):
            svc.handle_call_session_update_status({"id": "s1", "status": "completed"})


# ---------------------------------------------------------------------------
# handle_call_session_add_transcript
# ---------------------------------------------------------------------------

class TestCallSessionAddTranscript(unittest.TestCase):
    def test_add_transcript_ok(self) -> None:
        svc, store, _ = _make_service()
        entry1 = MagicMock()
        entry2 = MagicMock()
        sess = _make_session(session_id="s1", transcript_history=[entry1, entry2])
        store.add_transcript.return_value = sess

        result = svc.handle_call_session_add_transcript(
            {"id": "s1", "speaker": "agent", "text": "Привет"}
        )

        store.add_transcript.assert_called_once_with(
            session_id="s1", speaker="agent", text="Привет", ts=None
        )
        self.assertEqual(result["session_id"], "s1")
        self.assertEqual(result["transcript_count"], 2)

    def test_add_transcript_with_ts(self) -> None:
        svc, store, _ = _make_service()
        sess = _make_session(transcript_history=[MagicMock()])
        store.add_transcript.return_value = sess

        svc.handle_call_session_add_transcript(
            {"id": "s1", "speaker": "caller", "text": "Ok", "ts": "2026-05-18T10:01:00"}
        )

        store.add_transcript.assert_called_once_with(
            session_id="s1", speaker="caller", text="Ok", ts="2026-05-18T10:01:00"
        )

    def test_add_transcript_missing_id(self) -> None:
        svc, _, _ = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_call_session_add_transcript({"speaker": "agent", "text": "Hi"})

    def test_add_transcript_empty_text(self) -> None:
        svc, _, _ = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_call_session_add_transcript({"id": "s1", "speaker": "agent", "text": ""})

    def test_add_transcript_not_found_propagates(self) -> None:
        svc, store, _ = _make_service()
        store.add_transcript.side_effect = KeyError("Сессия не найдена")

        with self.assertRaises(KeyError):
            svc.handle_call_session_add_transcript(
                {"id": "missing", "speaker": "agent", "text": "Hi"}
            )


# ---------------------------------------------------------------------------
# handle_call_session_end
# ---------------------------------------------------------------------------

class TestCallSessionEnd(unittest.TestCase):
    def test_end_completed(self) -> None:
        svc, store, _ = _make_service()
        sess = _make_session(
            session_id="s1", status="completed",
            duration_sec=120.0, cost_usd=0.05, end_reason="completed"
        )
        store.mark_completed.return_value = sess

        result = svc.handle_call_session_end({"id": "s1", "reason": "completed"})

        store.mark_completed.assert_called_once_with(
            session_id="s1", end_reason="completed", cost_usd=0.0
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["duration_sec"], 120.0)

    def test_end_failed(self) -> None:
        svc, store, _ = _make_service()
        sess = _make_session(
            session_id="s1", status="failed",
            duration_sec=30.0, cost_usd=0.01, end_reason="no_answer"
        )
        store.mark_failed.return_value = sess

        result = svc.handle_call_session_end(
            {"id": "s1", "reason": "no_answer", "cost_usd": 0.01, "failed": True}
        )

        store.mark_failed.assert_called_once_with(
            session_id="s1", end_reason="no_answer", cost_usd=0.01
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["end_reason"], "no_answer")

    def test_end_missing_id(self) -> None:
        svc, _, _ = _make_service()
        with self.assertRaises(ValueError):
            svc.handle_call_session_end({})

    def test_end_not_found_propagates(self) -> None:
        svc, store, _ = _make_service()
        store.mark_completed.side_effect = KeyError("Сессия не найдена")

        with self.assertRaises(KeyError):
            svc.handle_call_session_end({"id": "ghost"})


if __name__ == "__main__":
    unittest.main()
