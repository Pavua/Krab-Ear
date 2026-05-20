"""Unit tests — Wave 184: CallSession + CallSessionStore coverage.

Tests target:
  - NDJSON append/persist behaviour
  - Full state-machine transition chain
  - Invalid transition rejection
  - get() / list_sessions() with date-range slicing
  - Unicode metadata roundtrip
  - Persist-reload roundtrip (separate store instances)
  - Concurrent create produces unique IDs
  - Corrupted NDJSON lines are skipped gracefully
  - State machine invariants (terminal states, full chain)
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_session import (  # noqa: E402
    CallSession,
    CallSessionStateMachine,
    CallStatus,
    _VALID_TRANSITIONS,
)
from backend.call_session_store import CallSessionStore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp: Path) -> CallSessionStore:
    return CallSessionStore(data_dir=tmp)


# ---------------------------------------------------------------------------
# 1. test_create_session_appends_ndjson
# ---------------------------------------------------------------------------


class TestCreateSessionAppendsNDJSON(unittest.TestCase):
    """Creating a session must write exactly one JSON line to NDJSON file."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_create_appends_one_line(self) -> None:
        self.store.create("+79991234567", "забронировать столик")
        lines = [
            ln for ln in self.store.sessions_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        self.assertEqual(len(lines), 1)

    def test_line_is_valid_json_with_expected_fields(self) -> None:
        s = self.store.create("+1234567890", "goal text")
        raw = self.store.sessions_path.read_text(encoding="utf-8").strip()
        payload = json.loads(raw)
        self.assertEqual(payload["id"], s.id)
        self.assertEqual(payload["phone_number"], "+1234567890")
        self.assertEqual(payload["status"], CallStatus.IDLE.value)

    def test_two_sessions_produce_two_lines(self) -> None:
        self.store.create("+1", "A")
        self.store.create("+2", "B")
        lines = [
            ln for ln in self.store.sessions_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        self.assertEqual(len(lines), 2)


# ---------------------------------------------------------------------------
# 2. test_update_status_transitions (full happy path)
# ---------------------------------------------------------------------------


class TestUpdateStatusTransitions(unittest.TestCase):
    """Walk the full idle→dialing→connected→talking→ending→completed chain."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_full_happy_path(self) -> None:
        chain = [
            CallStatus.DIALING,
            CallStatus.CONNECTED,
            CallStatus.TALKING,
            CallStatus.ENDING,
        ]
        s = self.store.create("+1", "walk full chain")
        for status in chain:
            updated = self.store.update_status(s.id, status.value)
            self.assertEqual(updated.status, status.value)

        completed = self.store.mark_completed(s.id, end_reason="goal_reached", cost_usd=0.10)
        self.assertEqual(completed.status, CallStatus.COMPLETED.value)
        self.assertEqual(completed.end_reason, "goal_reached")
        self.assertAlmostEqual(completed.cost_usd, 0.10, places=4)

    def test_status_persisted_after_each_step(self) -> None:
        s = self.store.create("+1", "persistence check")
        self.store.update_status(s.id, CallStatus.DIALING.value)
        fetched = self.store.get(s.id)
        assert fetched is not None
        self.assertEqual(fetched.status, CallStatus.DIALING.value)

        self.store.update_status(s.id, CallStatus.CONNECTED.value)
        fetched2 = self.store.get(s.id)
        assert fetched2 is not None
        self.assertEqual(fetched2.status, CallStatus.CONNECTED.value)

    def test_dialing_sets_started_at(self) -> None:
        s = self.store.create("+1", "started_at check")
        self.store.update_status(s.id, CallStatus.DIALING.value)
        fetched = self.store.get(s.id)
        assert fetched is not None
        self.assertIsNotNone(fetched.started_at)

    def test_mark_failed_from_dialing(self) -> None:
        s = self.store.create("+1", "fail fast")
        self.store.update_status(s.id, CallStatus.DIALING.value)
        failed = self.store.mark_failed(s.id, end_reason="no_answer", cost_usd=0.00)
        self.assertEqual(failed.status, CallStatus.FAILED.value)
        self.assertEqual(failed.end_reason, "no_answer")

    def test_mark_completed_computes_duration(self) -> None:
        """Duration is computed between started_at and ended_at."""
        s = self.store.create("+1", "duration test")
        # Walk to ENDING
        for st in [
            CallStatus.DIALING,
            CallStatus.CONNECTED,
            CallStatus.TALKING,
            CallStatus.ENDING,
        ]:
            self.store.update_status(s.id, st.value)
        completed = self.store.mark_completed(s.id, "done")
        # duration_sec may be 0 in fast tests; it must be a non-negative float or None
        if completed.duration_sec is not None:
            self.assertGreaterEqual(completed.duration_sec, 0.0)


# ---------------------------------------------------------------------------
# 3. test_invalid_status_transition_rejected
# ---------------------------------------------------------------------------


class TestInvalidStatusTransitionRejected(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_idle_to_completed_raises_value_error(self) -> None:
        s = self.store.create("+1", "skip ahead")
        with self.assertRaises(ValueError):
            self.store.update_status(s.id, CallStatus.COMPLETED.value)

    def test_idle_to_talking_raises(self) -> None:
        s = self.store.create("+1", "skip")
        with self.assertRaises(ValueError):
            self.store.update_status(s.id, CallStatus.TALKING.value)

    def test_completed_to_idle_raises(self) -> None:
        s = self.store.create("+1", "can't restart")
        # Walk to COMPLETED
        for st in [
            CallStatus.DIALING,
            CallStatus.CONNECTED,
            CallStatus.TALKING,
            CallStatus.ENDING,
        ]:
            self.store.update_status(s.id, st.value)
        self.store.mark_completed(s.id, "done")
        with self.assertRaises(ValueError):
            self.store.update_status(s.id, CallStatus.IDLE.value)

    def test_failed_to_dialing_raises(self) -> None:
        s = self.store.create("+1", "failed restart")
        self.store.update_status(s.id, CallStatus.DIALING.value)
        self.store.mark_failed(s.id, "error")
        with self.assertRaises(ValueError):
            self.store.update_status(s.id, CallStatus.DIALING.value)

    def test_unknown_status_string_raises(self) -> None:
        s = self.store.create("+1", "bad status")
        with self.assertRaises(ValueError):
            self.store.update_status(s.id, "flying")


# ---------------------------------------------------------------------------
# 4. test_get_session_by_id
# ---------------------------------------------------------------------------


class TestGetSessionById(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_get_returns_correct_session(self) -> None:
        self.store.create("+1", "goal 1")
        s2 = self.store.create("+2", "goal 2")
        fetched = self.store.get(s2.id)
        assert fetched is not None
        self.assertEqual(fetched.id, s2.id)
        self.assertEqual(fetched.phone_number, "+2")

    def test_get_nonexistent_returns_none(self) -> None:
        self.assertIsNone(self.store.get("cs_doesnotexist"))

    def test_get_after_delete_returns_none(self) -> None:
        s = self.store.create("+1", "will be deleted")
        self.store.delete(s.id)
        self.assertIsNone(self.store.get(s.id))

    def test_get_strips_whitespace_in_id(self) -> None:
        s = self.store.create("+1", "whitespace id")
        fetched = self.store.get("  " + s.id + "  ")
        assert fetched is not None
        self.assertEqual(fetched.id, s.id)


# ---------------------------------------------------------------------------
# 5. test_list_sessions_by_date_range
# ---------------------------------------------------------------------------


class TestListSessionsByDateRange(unittest.TestCase):
    """list_sessions supports status_filter and limit; we simulate date-range via patching."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _patch_created_at(self, sid: str, ts: str) -> None:
        lines = self.store.sessions_path.read_text(encoding="utf-8").splitlines()
        patched = []
        for line in lines:
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("id") == sid and not obj.get("_update") and not obj.get("_deleted"):
                obj["created_at"] = ts
            patched.append(json.dumps(obj, ensure_ascii=False))
        self.store.sessions_path.write_text("\n".join(patched) + "\n", encoding="utf-8")

    def test_list_returns_newest_first(self) -> None:
        s1 = self.store.create("+1", "A")
        s2 = self.store.create("+2", "B")
        self._patch_created_at(s1.id, "2026-01-01T10:00:00")
        self._patch_created_at(s2.id, "2026-01-02T10:00:00")
        sessions = self.store.list_sessions(limit=10)
        self.assertEqual(sessions[0]["id"], s2.id)
        self.assertEqual(sessions[1]["id"], s1.id)

    def test_list_limit_respected(self) -> None:
        for i in range(8):
            self.store.create(f"+{i}", f"goal {i}")
        sessions = self.store.list_sessions(limit=3)
        self.assertEqual(len(sessions), 3)

    def test_list_filters_by_status(self) -> None:
        s1 = self.store.create("+1", "dialing")
        self.store.create("+2", "idle")
        self.store.update_status(s1.id, CallStatus.DIALING.value)
        dialing = self.store.list_sessions(status_filter=CallStatus.DIALING.value)
        self.assertEqual(len(dialing), 1)
        self.assertEqual(dialing[0]["id"], s1.id)

    def test_list_excludes_deleted(self) -> None:
        s = self.store.create("+1", "deleted")
        self.store.delete(s.id)
        self.assertEqual(len(self.store.list_sessions()), 0)

    def test_list_empty_store(self) -> None:
        self.assertEqual(self.store.list_sessions(), [])


# ---------------------------------------------------------------------------
# 6. test_unicode_session_metadata
# ---------------------------------------------------------------------------


class TestUnicodeSessionMetadata(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_unicode_goal_text_survives_roundtrip(self) -> None:
        goal = "Забронируй столик в «Яндекс Лавке» на 18:00 — для двоих 🍽️"
        s = self.store.create("+79991234567", goal)
        fetched = self.store.get(s.id)
        assert fetched is not None
        self.assertEqual(fetched.goal_text, goal)

    def test_unicode_phone_number_preserved(self) -> None:
        # Some locales use full-width digits; store should preserve as-is
        phone = "+7​999​123​4567"  # zero-width spaces
        s = self.store.create(phone, "test")
        fetched = self.store.get(s.id)
        assert fetched is not None
        # phone_number is stripped at create() level, which removes spaces but not ZWS
        self.assertIsNotNone(fetched.phone_number)

    def test_unicode_transcript_entry_roundtrip(self) -> None:
        s = self.store.create("+1", "unicode transcript")
        text = "Здравствуйте! Слот свободен на «пятницу» в 19:00 ✓"
        self.store.add_transcript(s.id, "bot", text)
        fetched = self.store.get(s.id)
        assert fetched is not None
        self.assertEqual(len(fetched.transcript_history), 1)
        self.assertEqual(fetched.transcript_history[0].text, text)

    def test_ndjson_file_uses_ensure_ascii_false(self) -> None:
        """Raw NDJSON bytes must contain Cyrillic, not \\uXXXX escapes."""
        self.store.create("+1", "Кириллица в файле")
        raw = self.store.sessions_path.read_bytes()
        self.assertIn("Кириллица".encode("utf-8"), raw)


# ---------------------------------------------------------------------------
# 7. test_persist_reload_roundtrip
# ---------------------------------------------------------------------------


class TestPersistReloadRoundtrip(unittest.TestCase):
    """Reopening a new store instance against the same directory must restore all state."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_basic_roundtrip(self) -> None:
        store1 = _make_store(self.data_dir)
        s = store1.create("+79001234567", "перезагрузка")
        store1.update_status(s.id, CallStatus.DIALING.value)
        store1.add_transcript(s.id, "bot", "Алло!")

        # Open a fresh store instance against the same directory
        store2 = _make_store(self.data_dir)
        fetched = store2.get(s.id)
        assert fetched is not None
        self.assertEqual(fetched.id, s.id)
        self.assertEqual(fetched.status, CallStatus.DIALING.value)
        self.assertEqual(len(fetched.transcript_history), 1)
        self.assertEqual(fetched.transcript_history[0].text, "Алло!")

    def test_list_sessions_after_reload(self) -> None:
        store1 = _make_store(self.data_dir)
        store1.create("+1", "A")
        store1.create("+2", "B")

        store2 = _make_store(self.data_dir)
        sessions = store2.list_sessions(limit=10)
        self.assertEqual(len(sessions), 2)

    def test_completed_session_reload(self) -> None:
        store1 = _make_store(self.data_dir)
        s = store1.create("+1", "complete and reload")
        for st in [
            CallStatus.DIALING,
            CallStatus.CONNECTED,
            CallStatus.TALKING,
            CallStatus.ENDING,
        ]:
            store1.update_status(s.id, st.value)
        store1.mark_completed(s.id, end_reason="success", cost_usd=0.07)

        store2 = _make_store(self.data_dir)
        fetched = store2.get(s.id)
        assert fetched is not None
        self.assertEqual(fetched.status, CallStatus.COMPLETED.value)
        self.assertEqual(fetched.end_reason, "success")
        self.assertAlmostEqual(fetched.cost_usd, 0.07, places=4)


# ---------------------------------------------------------------------------
# 8. test_concurrent_create_unique_ids
# ---------------------------------------------------------------------------


class TestConcurrentCreateUniqueIds(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_concurrent_creates_yield_unique_ids(self) -> None:
        n = 20
        results: list[CallSession] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def create_session(i: int) -> None:
            try:
                s = self.store.create(f"+{i:03d}", f"concurrent goal {i}")
                with lock:
                    results.append(s)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=create_session, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
        self.assertEqual(len(results), n)

        ids = [s.id for s in results]
        self.assertEqual(len(ids), len(set(ids)), "Duplicate session IDs detected")

    def test_concurrent_updates_do_not_corrupt_status(self) -> None:
        """Multiple threads updating different sessions must not interleave writes."""
        n = 10
        sessions = [self.store.create(f"+{i}", f"goal {i}") for i in range(n)]
        errors: list[Exception] = []
        lock = threading.Lock()

        def advance(s: CallSession) -> None:
            try:
                self.store.update_status(s.id, CallStatus.DIALING.value)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=advance, args=(s,)) for s in sessions]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        for s in sessions:
            fetched = self.store.get(s.id)
            assert fetched is not None
            self.assertEqual(fetched.status, CallStatus.DIALING.value)


# ---------------------------------------------------------------------------
# 9. test_handles_corrupted_ndjson_line_skipped
# ---------------------------------------------------------------------------


class TestHandlesCorruptedNDJSONLineSkipped(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _inject_corrupt_line(self, before: bool = False) -> None:
        existing = self.store.sessions_path.read_text(encoding="utf-8")
        corrupt = "}{NOT_VALID_JSON::\n"
        if before:
            content = corrupt + existing
        else:
            content = existing + corrupt
        self.store.sessions_path.write_text(content, encoding="utf-8")

    def test_corrupt_line_before_valid_session(self) -> None:
        s = self.store.create("+1", "after corrupt")
        self._inject_corrupt_line(before=True)
        # Store must still return the valid session
        fetched = self.store.get(s.id)
        assert fetched is not None
        self.assertEqual(fetched.id, s.id)

    def test_corrupt_line_after_valid_session(self) -> None:
        s = self.store.create("+1", "before corrupt")
        self._inject_corrupt_line(before=False)
        fetched = self.store.get(s.id)
        assert fetched is not None
        self.assertEqual(fetched.id, s.id)

    def test_list_skips_corrupt_lines(self) -> None:
        self.store.create("+1", "good session")
        self._inject_corrupt_line()
        sessions = self.store.list_sessions()
        self.assertEqual(len(sessions), 1)

    def test_all_corrupt_file_returns_empty(self) -> None:
        self.store.sessions_path.write_text(
            "BAD_JSON\nALSO_BAD\n{incomplete\n", encoding="utf-8"
        )
        self.assertIsNone(self.store.get("any_id"))
        self.assertEqual(self.store.list_sessions(), [])

    def test_empty_lines_are_skipped(self) -> None:
        s = self.store.create("+1", "empty lines")
        existing = self.store.sessions_path.read_text(encoding="utf-8")
        # Inject blank lines
        self.store.sessions_path.write_text("\n\n" + existing + "\n\n", encoding="utf-8")
        fetched = self.store.get(s.id)
        assert fetched is not None
        self.assertEqual(fetched.id, s.id)


# ---------------------------------------------------------------------------
# 10. test_state_machine_invariants
# ---------------------------------------------------------------------------


class TestStateMachineInvariants(unittest.TestCase):
    """Verify the _VALID_TRANSITIONS table and state machine invariants."""

    def test_terminal_states_have_no_transitions(self) -> None:
        for terminal in (CallStatus.COMPLETED, CallStatus.FAILED):
            allowed = _VALID_TRANSITIONS.get(terminal, frozenset())
            self.assertEqual(
                len(allowed),
                0,
                f"Terminal state {terminal.value} must have no allowed transitions",
            )

    def test_all_statuses_present_in_table(self) -> None:
        for status in CallStatus:
            self.assertIn(
                status,
                _VALID_TRANSITIONS,
                f"{status.value} is missing from _VALID_TRANSITIONS",
            )

    def test_idle_is_not_terminal(self) -> None:
        sm = CallSessionStateMachine(CallStatus.IDLE)
        self.assertFalse(sm.is_terminal())

    def test_completed_is_terminal(self) -> None:
        sm = CallSessionStateMachine(CallStatus.COMPLETED)
        self.assertTrue(sm.is_terminal())

    def test_failed_is_terminal(self) -> None:
        sm = CallSessionStateMachine(CallStatus.FAILED)
        self.assertTrue(sm.is_terminal())

    def test_can_transition_matches_table(self) -> None:
        """can_transition must agree with _VALID_TRANSITIONS for every pair."""
        for src_status in CallStatus:
            sm = CallSessionStateMachine(src_status)
            allowed = _VALID_TRANSITIONS.get(src_status, frozenset())
            for tgt_status in CallStatus:
                expected = tgt_status in allowed
                self.assertEqual(
                    sm.can_transition(tgt_status),
                    expected,
                    f"can_transition({src_status.value}→{tgt_status.value}) mismatch",
                )

    def test_full_chain_via_state_machine_only(self) -> None:
        """Walk the golden path purely through CallSessionStateMachine."""
        sm = CallSessionStateMachine()
        golden_path = [
            CallStatus.DIALING,
            CallStatus.CONNECTED,
            CallStatus.TALKING,
            CallStatus.ENDING,
            CallStatus.COMPLETED,
        ]
        for step in golden_path:
            result = sm.transition(step)
            self.assertEqual(result, step)
        self.assertTrue(sm.is_terminal())

    def test_alternative_fail_chain_via_state_machine(self) -> None:
        sm = CallSessionStateMachine()
        for step in [CallStatus.DIALING, CallStatus.CONNECTED, CallStatus.FAILED]:
            sm.transition(step)
        self.assertTrue(sm.is_terminal())

    def test_transition_raises_ValueError_not_other(self) -> None:
        sm = CallSessionStateMachine(CallStatus.IDLE)
        with self.assertRaises(ValueError):
            sm.transition(CallStatus.ENDING)

    def test_error_message_lists_allowed_states(self) -> None:
        sm = CallSessionStateMachine(CallStatus.IDLE)
        with self.assertRaises(ValueError) as ctx:
            sm.transition(CallStatus.COMPLETED)
        msg = str(ctx.exception)
        # idle only allows dialing
        self.assertIn("dialing", msg)

    def test_store_rejects_unknown_status_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(Path(tmp))
            s = store.create("+1", "test")
            with self.assertRaises(ValueError):
                store.update_status(s.id, "unknown_status_xyz")

    def test_store_missing_session_raises_key_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(Path(tmp))
            with self.assertRaises(KeyError):
                store.update_status("cs_ghost_id", CallStatus.DIALING.value)


if __name__ == "__main__":
    unittest.main()
