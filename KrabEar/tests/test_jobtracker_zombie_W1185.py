"""Tests for W1182 F2 HIGH fix — JobTracker prune() zombie MLX worker bug.

Fixes verified:
- prune() sets cancel_event BEFORE evicting the dict entry.
- After eviction, get_cancel_event() still returns the event (grace period).
- Worker holding a reference to the Event observes cancellation after eviction.
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.job_tracker import JobTracker, _PRUNE_CANCEL_EVENT_TTL  # noqa: E402


class PruneCancelEventBeforeEvictTestCase(unittest.TestCase):
    """prune() must set the cancel_event before removing the job entry."""

    def test_prune_sets_cancel_event_before_evict(self) -> None:
        """cancel_event is set when a stale job is pruned.

        After prune(), the event held by the worker (obtained before eviction
        via get_cancel_event) must be in the set state so the worker can exit.
        """
        tracker = JobTracker()
        jid = tracker.create_job(3)

        # Mark job as done with finished_at far in the past so prune() picks it up.
        tracker.update(jid, status="done")
        with tracker._lock:
            tracker._jobs[jid]["finished_at"] = time.monotonic() - 9999
            tracker._jobs[jid]["status"] = "done"

        # Worker obtains the event reference BEFORE prune() runs.
        event = tracker.get_cancel_event(jid)
        self.assertIsNotNone(event, "get_cancel_event() must return Event before prune")
        self.assertFalse(event.is_set(), "Event must be clear before prune")

        # prune() evicts the job.
        tracker.prune(max_age_sec=1)

        # The job dict entry is gone.
        self.assertIsNone(tracker.get(jid), "Job must be evicted from _jobs")

        # But the event that the worker holds must now be set.
        self.assertTrue(
            event.is_set(),
            "cancel_event must be SET after prune() evicts the job (zombie fix)",
        )


class EvictedJobCancelCheckTestCase(unittest.TestCase):
    """After eviction the event returned by get_cancel_event stays set (grace period)."""

    def test_evicted_job_cancel_check_still_returns_true(self) -> None:
        """get_cancel_event() returns the set event during grace period after eviction.

        The event object must remain accessible via get_cancel_event() for at least
        _PRUNE_CANCEL_EVENT_TTL seconds after the job is evicted, so that workers
        that call get_cancel_event() after eviction still get the signal.
        """
        tracker = JobTracker()
        jid = tracker.create_job(1)

        # Force job into done state with old finished_at.
        with tracker._lock:
            tracker._jobs[jid]["status"] = "done"
            tracker._jobs[jid]["finished_at"] = time.monotonic() - 9999

        # prune() removes the job.
        tracker.prune(max_age_sec=1)
        self.assertIsNone(tracker.get(jid), "Job should be evicted")

        # get_cancel_event() should still return a set event during grace period.
        event_after_eviction = tracker.get_cancel_event(jid)
        self.assertIsNotNone(
            event_after_eviction,
            "cancel_event must remain accessible during grace period after eviction",
        )
        self.assertTrue(
            event_after_eviction.is_set(),
            "cancel_event must be SET during grace period so late-calling workers exit",
        )

    def test_cancel_event_cleaned_up_after_grace_period(self) -> None:
        """cancel_event is removed from tracker after grace period expires.

        After _PRUNE_CANCEL_EVENT_TTL seconds the event is cleaned up from
        _cancel_events to prevent unbounded memory growth.
        """
        tracker = JobTracker()
        jid = tracker.create_job(1)

        with tracker._lock:
            tracker._jobs[jid]["status"] = "done"
            tracker._jobs[jid]["finished_at"] = time.monotonic() - 9999

        tracker.prune(max_age_sec=1)

        # Manually back-date the evict_time beyond grace period.
        with tracker._lock:
            tracker._evict_times[jid] = time.monotonic() - _PRUNE_CANCEL_EVENT_TTL - 1.0

        # Second prune() call should clean up the stale event.
        tracker.prune(max_age_sec=1)

        event_after_grace = tracker.get_cancel_event(jid)
        self.assertIsNone(
            event_after_grace,
            "cancel_event should be cleaned up after grace period to avoid memory leak",
        )


class WorkerObservesCancellationAfterEvictionTestCase(unittest.TestCase):
    """Worker thread observes cancellation via Event even after job is evicted."""

    def test_worker_can_observe_cancellation_after_eviction(self) -> None:
        """Simulate a worker that holds an Event ref and detects cancel post-eviction.

        This is the core zombie fix scenario:
        1. Worker starts and gets cancel_event reference.
        2. Job is evicted by prune() (simulating old stale entry cleanup).
        3. Worker calls _cancel_check() equivalent — checks event.is_set().
        4. Worker MUST see the cancellation and exit, not run indefinitely.
        """
        tracker = JobTracker()
        jid = tracker.create_job(5)
        tracker.update(jid, status="running")

        # Worker obtains cancel_event reference at startup (the fix pattern).
        cancel_event = tracker.get_cancel_event(jid)
        self.assertIsNotNone(cancel_event)

        worker_observed_cancel = threading.Event()
        worker_iterations = []

        def simulate_worker() -> None:
            """Simulated worker that checks cancel_event between iterations."""
            for i in range(100):
                worker_iterations.append(i)
                # Worker's _cancel_check equivalent — checks the event it holds.
                if cancel_event.is_set():
                    worker_observed_cancel.set()
                    return
                time.sleep(0.01)
            # If we get here, worker ran all 100 iterations — zombie behaviour!

        thread = threading.Thread(target=simulate_worker, daemon=True)
        thread.start()

        # Evict the job (simulating prune()).
        with tracker._lock:
            tracker._jobs[jid]["status"] = "done"
            tracker._jobs[jid]["finished_at"] = time.monotonic() - 9999
        tracker.prune(max_age_sec=1)

        # Job is gone from dict.
        self.assertIsNone(tracker.get(jid), "Job must be evicted")

        # Worker must observe cancellation and stop within reasonable time.
        worker_observed_cancel.wait(timeout=2.0)
        thread.join(timeout=2.0)

        self.assertTrue(
            worker_observed_cancel.is_set(),
            "Worker must observe cancellation via cancel_event after prune() eviction "
            "(zombie MLX worker fix W1182 F2)",
        )
        # Worker should have stopped well before 100 iterations.
        self.assertLess(
            len(worker_iterations),
            100,
            "Worker must exit early on cancellation, not run all iterations",
        )

    def test_worker_without_event_ref_is_zombie_scenario(self) -> None:
        """Regression guard: worker using only get() after eviction returns None.

        This test documents the ORIGINAL bug: if a worker only calls
        tracker.get(job_id) to check cancel_requested, it gets None after
        eviction and interprets that as "not cancelled" — continuing to run.
        The fix is to use get_cancel_event() instead.
        """
        tracker = JobTracker()
        jid = tracker.create_job(1)
        tracker.update(jid, status="running")

        # OLD (broken) cancel check: reads via get().
        def old_cancel_check() -> bool:
            state = tracker.get(jid)
            return bool(state and state.get("cancel_requested"))

        # Evict the job.
        with tracker._lock:
            tracker._jobs[jid]["status"] = "done"
            tracker._jobs[jid]["finished_at"] = time.monotonic() - 9999
        tracker.prune(max_age_sec=1)

        # OLD check returns False after eviction — this is the bug.
        self.assertFalse(
            old_cancel_check(),
            "OLD cancel_check via get() returns False after eviction — "
            "demonstrates the zombie bug that the fix addresses",
        )

        # NEW check via cancel_event returns True — fix works.
        event = tracker.get_cancel_event(jid)
        self.assertIsNotNone(event, "cancel_event must be accessible in grace period")
        self.assertTrue(
            event.is_set(),
            "NEW cancel_event.is_set() returns True after prune() eviction — fix confirmed",
        )


if __name__ == "__main__":
    unittest.main()
