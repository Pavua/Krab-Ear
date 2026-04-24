"""Unit tests — CallSession data model, state machine and CallSessionStore.

Phase 3 Call Automation — step 1/4.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_session import (  # noqa: E402
    CallSession,
    CallSessionStateMachine,
    CallStatus,
    Speaker,
    TranscriptEntry,
)
from backend.call_session_store import CallSessionStore  # noqa: E402


# ---------------------------------------------------------------------------
# CallSessionStateMachine
# ---------------------------------------------------------------------------


class TestCallSessionStateMachineTransitions(unittest.TestCase):
    """Валидные и недопустимые переходы состояний."""

    def _sm(self, initial: CallStatus = CallStatus.IDLE) -> CallSessionStateMachine:
        return CallSessionStateMachine(initial)

    def test_idle_to_dialing_ok(self) -> None:
        sm = self._sm()
        result = sm.transition(CallStatus.DIALING)
        self.assertEqual(result, CallStatus.DIALING)
        self.assertEqual(sm.status, CallStatus.DIALING)

    def test_dialing_to_connected_ok(self) -> None:
        sm = self._sm(CallStatus.DIALING)
        sm.transition(CallStatus.CONNECTED)
        self.assertEqual(sm.status, CallStatus.CONNECTED)

    def test_connected_to_talking_ok(self) -> None:
        sm = self._sm(CallStatus.CONNECTED)
        sm.transition(CallStatus.TALKING)
        self.assertEqual(sm.status, CallStatus.TALKING)

    def test_talking_to_ending_ok(self) -> None:
        sm = self._sm(CallStatus.TALKING)
        sm.transition(CallStatus.ENDING)
        self.assertEqual(sm.status, CallStatus.ENDING)

    def test_ending_to_completed_ok(self) -> None:
        sm = self._sm(CallStatus.ENDING)
        sm.transition(CallStatus.COMPLETED)
        self.assertEqual(sm.status, CallStatus.COMPLETED)

    def test_dialing_to_failed_ok(self) -> None:
        sm = self._sm(CallStatus.DIALING)
        sm.transition(CallStatus.FAILED)
        self.assertEqual(sm.status, CallStatus.FAILED)

    def test_connected_to_failed_ok(self) -> None:
        sm = self._sm(CallStatus.CONNECTED)
        sm.transition(CallStatus.FAILED)
        self.assertEqual(sm.status, CallStatus.FAILED)

    def test_idle_to_completed_raises(self) -> None:
        sm = self._sm()
        with self.assertRaises(ValueError):
            sm.transition(CallStatus.COMPLETED)

    def test_idle_to_talking_raises(self) -> None:
        sm = self._sm()
        with self.assertRaises(ValueError):
            sm.transition(CallStatus.TALKING)

    def test_completed_is_terminal(self) -> None:
        sm = self._sm(CallStatus.COMPLETED)
        self.assertTrue(sm.is_terminal())
        with self.assertRaises(ValueError):
            sm.transition(CallStatus.IDLE)

    def test_failed_is_terminal(self) -> None:
        sm = self._sm(CallStatus.FAILED)
        self.assertTrue(sm.is_terminal())

    def test_can_transition_returns_bool(self) -> None:
        sm = self._sm(CallStatus.IDLE)
        self.assertTrue(sm.can_transition(CallStatus.DIALING))
        self.assertFalse(sm.can_transition(CallStatus.COMPLETED))

    def test_error_message_contains_target_status(self) -> None:
        sm = self._sm(CallStatus.IDLE)
        with self.assertRaises(ValueError) as ctx:
            sm.transition(CallStatus.TALKING)
        self.assertIn("talking", str(ctx.exception))


# ---------------------------------------------------------------------------
# CallSession data model
# ---------------------------------------------------------------------------


class TestCallSessionModel(unittest.TestCase):

    def test_create_sets_idle_status(self) -> None:
        s = CallSession.create("+79991234567", "узнай про слот")
        self.assertEqual(s.status, CallStatus.IDLE.value)
        self.assertTrue(s.id.startswith("cs_"))
        self.assertEqual(s.phone_number, "+79991234567")

    def test_round_trip_serialization(self) -> None:
        s = CallSession.create("+1234567890", "test goal")
        s.transcript_history.append(
            TranscriptEntry(speaker=Speaker.BOT.value, text="Здравствуйте")
        )
        data = s.to_dict()
        restored = CallSession.from_dict(data)
        self.assertEqual(restored.id, s.id)
        self.assertEqual(restored.phone_number, s.phone_number)
        self.assertEqual(len(restored.transcript_history), 1)
        self.assertEqual(restored.transcript_history[0].text, "Здравствуйте")

    def test_speaker_enum_values(self) -> None:
        self.assertEqual(Speaker.USER.value, "user")
        self.assertEqual(Speaker.BOT.value, "bot")
        self.assertEqual(Speaker.OPERATOR.value, "operator")


# ---------------------------------------------------------------------------
# CallSessionStore persistence
# ---------------------------------------------------------------------------


class TestCallSessionStorePersistence(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = CallSessionStore(data_dir=Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_create_and_get_round_trip(self) -> None:
        s = self.store.create("+79991234567", "узнай про слот")
        fetched = self.store.get(s.id)
        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched.id, s.id)
        self.assertEqual(fetched.phone_number, "+79991234567")
        self.assertEqual(fetched.status, CallStatus.IDLE.value)

    def test_get_nonexistent_returns_none(self) -> None:
        result = self.store.get("cs_doesnotexist")
        self.assertIsNone(result)

    def test_list_returns_created_session(self) -> None:
        self.store.create("+1", "goal A")
        self.store.create("+2", "goal B")
        sessions = self.store.list_sessions(limit=50)
        self.assertEqual(len(sessions), 2)

    def test_list_status_filter(self) -> None:
        s1 = self.store.create("+1", "goal A")
        self.store.create("+2", "goal B")
        # Advance s1 to DIALING
        self.store.update_status(s1.id, CallStatus.DIALING.value)
        dialing = self.store.list_sessions(status_filter=CallStatus.DIALING.value)
        self.assertEqual(len(dialing), 1)
        self.assertEqual(dialing[0]["id"], s1.id)

    def test_update_status_valid(self) -> None:
        s = self.store.create("+1", "goal")
        updated = self.store.update_status(s.id, CallStatus.DIALING.value)
        self.assertEqual(updated.status, CallStatus.DIALING.value)
        # Verify persisted
        fetched = self.store.get(s.id)
        assert fetched is not None
        self.assertEqual(fetched.status, CallStatus.DIALING.value)

    def test_update_status_invalid_transition_raises(self) -> None:
        s = self.store.create("+1", "goal")
        with self.assertRaises(ValueError):
            self.store.update_status(s.id, CallStatus.COMPLETED.value)

    def test_add_transcript_entry(self) -> None:
        s = self.store.create("+1", "goal")
        updated = self.store.add_transcript(s.id, "bot", "Здравствуйте")
        self.assertEqual(updated.transcript_count if hasattr(updated, "transcript_count")
                         else len(updated.transcript_history), 1)
        fetched = self.store.get(s.id)
        assert fetched is not None
        self.assertEqual(len(fetched.transcript_history), 1)
        self.assertEqual(fetched.transcript_history[0].text, "Здравствуйте")
        self.assertEqual(fetched.transcript_history[0].speaker, "bot")

    def test_multiple_transcript_entries(self) -> None:
        s = self.store.create("+1", "goal")
        self.store.add_transcript(s.id, "bot", "Привет")
        self.store.add_transcript(s.id, "user", "Да, слушаю")
        self.store.add_transcript(s.id, "bot", "Спасибо")
        fetched = self.store.get(s.id)
        assert fetched is not None
        self.assertEqual(len(fetched.transcript_history), 3)

    def test_mark_completed_sets_status_and_duration(self) -> None:
        s = self.store.create("+1", "goal")
        self.store.update_status(s.id, CallStatus.DIALING.value)
        self.store.update_status(s.id, CallStatus.CONNECTED.value)
        self.store.update_status(s.id, CallStatus.TALKING.value)
        self.store.update_status(s.id, CallStatus.ENDING.value)
        completed = self.store.mark_completed(s.id, end_reason="completed", cost_usd=0.05)
        self.assertEqual(completed.status, CallStatus.COMPLETED.value)
        self.assertEqual(completed.end_reason, "completed")
        self.assertAlmostEqual(completed.cost_usd, 0.05, places=4)
        fetched = self.store.get(s.id)
        assert fetched is not None
        self.assertEqual(fetched.status, CallStatus.COMPLETED.value)

    def test_mark_failed(self) -> None:
        s = self.store.create("+1", "goal")
        self.store.update_status(s.id, CallStatus.DIALING.value)
        failed = self.store.mark_failed(s.id, end_reason="network_error", cost_usd=0.01)
        self.assertEqual(failed.status, CallStatus.FAILED.value)
        self.assertEqual(failed.end_reason, "network_error")

    def test_tombstone_delete(self) -> None:
        s = self.store.create("+1", "goal")
        deleted = self.store.delete(s.id)
        self.assertTrue(deleted)
        fetched = self.store.get(s.id)
        self.assertIsNone(fetched)
        sessions = self.store.list_sessions()
        self.assertEqual(len(sessions), 0)

    def test_delete_nonexistent_returns_false(self) -> None:
        result = self.store.delete("cs_ghost")
        self.assertFalse(result)

    def test_list_newest_first(self) -> None:
        # Create two sessions with explicitly different created_at strings.
        s1 = self.store.create("+1", "goal A")
        s2 = self.store.create("+2", "goal B")
        # Patch created_at so ordering is deterministic regardless of clock resolution.
        # Rewrite the NDJSON with correct timestamps.
        import json
        lines = self.store.sessions_path.read_text(encoding="utf-8").splitlines()
        patched = []
        for line in lines:
            obj = json.loads(line)
            if obj.get("id") == s1.id:
                obj["created_at"] = "2026-01-01T00:00:00"
            elif obj.get("id") == s2.id:
                obj["created_at"] = "2026-01-02T00:00:00"
            patched.append(json.dumps(obj, ensure_ascii=False))
        self.store.sessions_path.write_text("\n".join(patched) + "\n", encoding="utf-8")
        sessions = self.store.list_sessions(limit=50)
        self.assertEqual(sessions[0]["id"], s2.id)
        self.assertEqual(sessions[1]["id"], s1.id)

    def test_list_limit(self) -> None:
        for i in range(10):
            self.store.create(f"+{i}", f"goal {i}")
        sessions = self.store.list_sessions(limit=3)
        self.assertEqual(len(sessions), 3)

    def test_transcript_entry_missing_id_raises(self) -> None:
        self.store.create("+1", "goal")
        with self.assertRaises(KeyError):
            self.store.add_transcript("cs_missing", "bot", "hello")


if __name__ == "__main__":
    unittest.main()
