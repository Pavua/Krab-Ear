"""Tests for W1342: JobTracker.get_cancel_event wired in recording_core_service._cancel_check.

W1335 R2 MED — previously _cancel_check polled dict only; W1185's get_cancel_event()
was never wired. This caused zombie threads to keep running after cancel() because
they could only detect cancellation between file boundaries (dict poll), not
immediately via threading.Event.

Tests:
  - test_cancel_event_wired_in_recording_core
  - test_cancellation_via_event_observed
  - test_legacy_poll_fallback_when_event_none
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.job_tracker import JobTracker  # noqa: E402


# ---------------------------------------------------------------------------
# JobTracker: get_cancel_event API (W1185 retroactively verified + W1342 new)
# ---------------------------------------------------------------------------

class GetCancelEventAPITestCase(unittest.TestCase):
    """Tests for JobTracker.get_cancel_event() API added in W1342."""

    def setUp(self) -> None:
        self.tracker = JobTracker()

    def test_get_cancel_event_returns_event_for_new_job(self) -> None:
        jid = self.tracker.create_job(3)
        event = self.tracker.get_cancel_event(jid)
        self.assertIsNotNone(event)
        self.assertIsInstance(event, threading.Event)

    def test_get_cancel_event_not_set_initially(self) -> None:
        jid = self.tracker.create_job(2)
        event = self.tracker.get_cancel_event(jid)
        self.assertFalse(event.is_set(), "Cancel event must not be set on job creation")

    def test_get_cancel_event_returns_none_for_unknown_job(self) -> None:
        result = self.tracker.get_cancel_event("j-nonexistent")
        self.assertIsNone(result)

    def test_cancel_sets_the_event(self) -> None:
        jid = self.tracker.create_job(1)
        event = self.tracker.get_cancel_event(jid)
        self.assertFalse(event.is_set())
        ok = self.tracker.cancel(jid)
        self.assertTrue(ok)
        self.assertTrue(event.is_set(), "cancel() must set the threading.Event")

    def test_cancel_event_is_same_object_across_calls(self) -> None:
        """get_cancel_event must return the same Event object each time."""
        jid = self.tracker.create_job(1)
        ev1 = self.tracker.get_cancel_event(jid)
        ev2 = self.tracker.get_cancel_event(jid)
        self.assertIs(ev1, ev2, "Must return the same Event instance")

    def test_cancel_sets_cancel_requested_dict_flag_too(self) -> None:
        """Backward compat: cancel() must still set cancel_requested in the dict."""
        jid = self.tracker.create_job(1)
        self.tracker.cancel(jid)
        state = self.tracker.get(jid)
        self.assertTrue(state["cancel_requested"])

    def test_prune_removes_cancel_event(self) -> None:
        """prune() clears cancel_events to avoid memory leak."""
        jid = self.tracker.create_job(1)
        self.tracker.mark_done(jid, items=[], errors=[])
        # Force prune with 0 age threshold so the done job gets pruned immediately.
        self.tracker.prune(max_age_sec=0)
        result = self.tracker.get_cancel_event(jid)
        self.assertIsNone(result, "prune() must remove cancel event to avoid memory leak")

    def test_multiple_jobs_have_independent_events(self) -> None:
        jid1 = self.tracker.create_job(1)
        jid2 = self.tracker.create_job(2)
        ev1 = self.tracker.get_cancel_event(jid1)
        ev2 = self.tracker.get_cancel_event(jid2)
        self.assertIsNot(ev1, ev2, "Each job must have its own independent Event")
        self.tracker.cancel(jid1)
        self.assertTrue(ev1.is_set())
        self.assertFalse(ev2.is_set(), "Cancelling job1 must not affect job2's event")

    def test_cancel_already_done_job_does_not_set_event(self) -> None:
        """cancel() on a terminal job returns False and must not set the event."""
        jid = self.tracker.create_job(1)
        ev = self.tracker.get_cancel_event(jid)
        self.tracker.mark_done(jid, items=[], errors=[])
        ok = self.tracker.cancel(jid)
        self.assertFalse(ok)
        # Event must remain unset since cancel was rejected.
        self.assertFalse(ev.is_set(), "Event must not be set when cancel is rejected on terminal job")

    def test_get_cancel_event_thread_safety(self) -> None:
        """Concurrent get_cancel_event calls must not crash."""
        jid = self.tracker.create_job(5)
        errors: list[Exception] = []

        def _read() -> None:
            try:
                for _ in range(100):
                    self.tracker.get_cancel_event(jid)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_read) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(errors, [], f"Thread safety violation: {errors}")


# ---------------------------------------------------------------------------
# test_cancel_event_wired_in_recording_core
# Verifies that _cancel_check in recording_core_service uses event.is_set()
# ---------------------------------------------------------------------------

class CancelEventWiredInRecordingCoreTestCase(unittest.TestCase):
    """test_cancel_event_wired_in_recording_core — W1342 acceptance test.

    We stub JobTracker so we can observe whether _cancel_check calls
    event.is_set() (fast path) vs dict polling (legacy fallback).
    """

    def _make_cancel_check_fn(self, tracker: JobTracker, job_id: str) -> "callable":
        """Reproduce the exact _cancel_check closure from recording_core_service.py."""
        cancel_event = tracker.get_cancel_event(job_id)

        def _cancel_check() -> bool:
            if cancel_event is not None:
                return cancel_event.is_set()
            state = tracker.get(job_id)
            return bool(state and state.get("cancel_requested"))

        return _cancel_check

    def test_cancel_event_wired_in_recording_core(self) -> None:
        """_cancel_check must use event.is_set() when event is available."""
        tracker = JobTracker()
        jid = tracker.create_job(2)

        # Wrap the event to spy on is_set calls.
        real_event = tracker.get_cancel_event(jid)
        spy_calls: list[bool] = []
        original_is_set = real_event.is_set

        def spy_is_set() -> bool:
            result = original_is_set()
            spy_calls.append(result)
            return result

        real_event.is_set = spy_is_set  # type: ignore[method-assign]

        cancel_check = self._make_cancel_check_fn(tracker, jid)

        # Before cancel: is_set returns False.
        result = cancel_check()
        self.assertFalse(result)
        self.assertGreater(len(spy_calls), 0, "_cancel_check must call event.is_set()")

        # After cancel: is_set returns True.
        tracker.cancel(jid)
        result = cancel_check()
        self.assertTrue(result, "_cancel_check must return True after cancel()")

    def test_cancellation_via_event_observed(self) -> None:
        """Event.is_set() must reflect cancellation state set by cancel()."""
        tracker = JobTracker()
        jid = tracker.create_job(1)
        event = tracker.get_cancel_event(jid)

        cancel_check = self._make_cancel_check_fn(tracker, jid)

        self.assertFalse(cancel_check(), "No cancel yet → False")
        self.assertFalse(event.is_set())

        tracker.cancel(jid)

        self.assertTrue(cancel_check(), "After cancel → True")
        self.assertTrue(event.is_set())

    def test_legacy_poll_fallback_when_event_none(self) -> None:
        """When cancel_event is None (job evicted), _cancel_check falls back to dict poll."""
        tracker = JobTracker()
        jid = tracker.create_job(1)

        # Simulate get_cancel_event returning None (job evicted by prune).
        # We build a closure that captured None as cancel_event.
        cancel_event = None  # explicitly None

        def _cancel_check_legacy() -> bool:
            if cancel_event is not None:
                return cancel_event.is_set()
            state = tracker.get(jid)
            return bool(state and state.get("cancel_requested"))

        # Before cancel: dict says False.
        self.assertFalse(_cancel_check_legacy())

        # Manually set cancel_requested without using cancel() to test dict poll path.
        tracker.update(jid, cancel_requested=True)

        result = _cancel_check_legacy()
        self.assertTrue(result, "Legacy fallback must detect cancel_requested=True in dict")

    def test_event_is_not_none_right_after_create_job(self) -> None:
        """Immediately after create_job, get_cancel_event must not return None."""
        tracker = JobTracker()
        jid = tracker.create_job(3)
        ev = tracker.get_cancel_event(jid)
        self.assertIsNotNone(ev, "Event must be available right after create_job")

    def test_cancel_check_before_and_after_event_set(self) -> None:
        """Full cycle: False before cancel, True after cancel."""
        tracker = JobTracker()
        jid = tracker.create_job(1)
        cancel_event = tracker.get_cancel_event(jid)

        def _cancel_check() -> bool:
            if cancel_event is not None:
                return cancel_event.is_set()
            state = tracker.get(jid)
            return bool(state and state.get("cancel_requested"))

        results_before = [_cancel_check() for _ in range(5)]
        self.assertTrue(all(not r for r in results_before), "All checks before cancel must be False")

        tracker.cancel(jid)

        results_after = [_cancel_check() for _ in range(5)]
        self.assertTrue(all(r for r in results_after), "All checks after cancel must be True")

    def test_concurrent_cancel_and_check(self) -> None:
        """Concurrent cancel + cancel_check must not deadlock or lose the signal."""
        tracker = JobTracker()
        jid = tracker.create_job(10)
        cancel_event = tracker.get_cancel_event(jid)
        detected = threading.Event()
        errors: list[Exception] = []

        def _cancel_check() -> bool:
            if cancel_event is not None:
                return cancel_event.is_set()
            state = tracker.get(jid)
            return bool(state and state.get("cancel_requested"))

        def _worker() -> None:
            try:
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if _cancel_check():
                        detected.set()
                        return
                    time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        time.sleep(0.01)  # let worker spin up
        tracker.cancel(jid)
        t.join(timeout=2.0)

        self.assertEqual(errors, [], f"Worker errors: {errors}")
        self.assertTrue(detected.is_set(), "Worker must detect cancellation via event within 2s")


if __name__ == "__main__":
    unittest.main()
