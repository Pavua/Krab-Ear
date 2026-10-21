"""Completion observability: controlled native leaves, no HTTP/model imports."""
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
import threading
import unittest
from unittest.mock import Mock

from backend import activity_diagnostics as activity


class RestCompletionTests(unittest.TestCase):
    def tracker(self):
        return activity.ActivityReservations()

    def test_reserved_and_running_are_different_and_versioned(self):
        tracker = self.tracker()
        initial = tracker.snapshot()['generation']
        token = tracker.reserve('post_stt')
        before = tracker.snapshot()
        self.assertEqual((before['reservations'], before['inflight']), (1, 0))
        self.assertEqual(before['state'], 'busy')
        self.assertGreater(before['generation'], initial)
        seen = []
        self.assertEqual(tracker.run(token, lambda: seen.append(tracker.snapshot()) or 42), 42)
        self.assertEqual(seen[0]['inflight'], 1)
        self.assertEqual(tracker.snapshot()['reservations'], 0)
        self.assertGreater(tracker.snapshot()['generation'], before['generation'])

    def test_http_timeout_does_not_complete_native_future(self):
        tracker = self.tracker()
        started, finish = threading.Event(), threading.Event()
        token = tracker.reserve('post_stt')

        def native():
            started.set()
            finish.wait(3)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(tracker.run, token, native)
            tracker.follow_future(token, future)
            try:
                self.assertTrue(started.wait(1))
                with self.assertRaises(TimeoutError):
                    future.result(timeout=0.001)
                self.assertFalse(future.cancel())
                self.assertEqual(tracker.snapshot()['inflight'], 1)
            finally:
                finish.set()
            future.result(timeout=1)
        self.assertEqual(tracker.snapshot()['reservations'], 0)

    def test_cancel_before_start_releases_reservation(self):
        tracker = self.tracker()
        future = Future()
        token = tracker.reserve('post_stt')
        tracker.follow_future(token, future)
        self.assertTrue(future.cancel())
        self.assertEqual(tracker.snapshot()['reservations'], 0)

    def test_callable_completion_does_not_claim_nested_adapter_completion(self):
        tracker = self.tracker()
        nested = Future()
        self.assertTrue(nested.set_running_or_notify_cancel())
        token = tracker.reserve('post_stt')
        try:
            with self.assertRaises(TimeoutError):
                tracker.run(token, Mock(side_effect=TimeoutError))
            self.assertFalse(nested.cancel())
            result = activity.rest_activity_snapshot(tracker)
            native = result['components']['native_stt']
            self.assertEqual(native['state'], 'idle')
            self.assertEqual(native['completion_scope'], 'transcriber_callable')
            self.assertEqual(result['state'], 'unknown')
            self.assertIn('nested_adapter_futures', result['coverage']['untracked'])
            self.assertIn('ws_translation_emit', result['coverage']['untracked'])
        finally:
            nested.set_result(None)

    def test_worker_exception_and_duplicate_completion_are_safe(self):
        tracker = self.tracker()
        token = tracker.reserve('post_stt')
        with self.assertRaises(RuntimeError):
            tracker.run(token, Mock(side_effect=RuntimeError))
        generation = tracker.snapshot()['generation']
        tracker.complete(token)
        self.assertEqual(tracker.snapshot()['reservations'], 0)
        self.assertEqual(tracker.snapshot()['generation'], generation)

    def test_unobservable_future_is_unknown_and_retained(self):
        tracker = self.tracker()
        token = tracker.reserve('post_stt')
        tracker.follow_future(token, object())
        result = tracker.snapshot()
        self.assertFalse(result['known'])
        self.assertEqual(result['reservations'], 1)
        self.assertEqual(result['unknown'], 1)

    def test_snapshot_lock_contention_is_unknown(self):
        tracker = self.tracker()
        with tracker._lock:
            result = tracker.snapshot()
        self.assertFalse(result['known'])
        self.assertEqual(result['state'], 'unknown')

    def test_ws_disconnect_does_not_release_worker_slot(self):
        tracker = self.tracker()
        slot = threading.Lock()
        acquire, release = tracker.slot_callbacks(lambda timeout: slot.acquire(timeout=timeout), slot.release, 'native_ws')
        started, finish = threading.Event(), threading.Event()

        def worker():
            if acquire(0):
                started.set()
                try:
                    finish.wait(3)
                finally:
                    release()

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(started.wait(1))
            # Client/request has ended; actual server worker is still running.
            self.assertEqual(tracker.snapshot()['inflight'], 1)
            self.assertFalse(acquire(0))
        finally:
            finish.set()
            thread.join(2)
        self.assertEqual(tracker.snapshot()['reservations'], 0)

    def test_metadata_records_are_bounded_without_false_idle(self):
        tracker = self.tracker()
        for _ in range(300):
            tracker.reserve('post_stt')
        result = tracker.snapshot()
        self.assertLessEqual(result['reservations'], 256)
        self.assertFalse(result['known'])
        self.assertTrue(result['coverage_lost'])


if __name__ == '__main__':
    unittest.main()
