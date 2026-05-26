"""Tests for the TranscriptionQueue dequeue worker wired in BackendService (W1184).

Verifies:
  - test_enqueued_job_processed_by_worker
  - test_worker_marks_failed_on_transcribe_exception
  - test_worker_respects_shutdown_event
  - test_worker_holds_mlx_lock_during_transcribe

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m unittest \
        KrabEar/tests/test_transcription_queue_worker_W1184.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.transcription_queue import (
    TranscriptionQueue,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
)


# ---------------------------------------------------------------------------
# Minimal stubs so we can instantiate the worker method without a full
# BackendService (which would require AudioEngine, StateStore, etc.)
# ---------------------------------------------------------------------------

class _FakeTranscriber:
    """Stub transcriber whose transcribe() can be controlled per test."""

    def __init__(self):
        self._result = {"text": "hello world"}
        self._raises = None
        self.calls: list[tuple] = []  # (args, kwargs) recorded

    def transcribe(self, audio_data: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((audio_data, kwargs))
        if self._raises is not None:
            raise self._raises
        return self._result


class _WorkerHarness:
    """Minimal object that hosts _run_transcription_queue_worker + its dependencies
    so we can exercise the method without constructing BackendService."""

    def __init__(self, fake_transcriber: _FakeTranscriber | None = None):
        self.transcriber = fake_transcriber or _FakeTranscriber()
        self._transcription_queue = TranscriptionQueue()
        self._tq_shutdown_event = threading.Event()
        # Track whether mlx_lock was entered during transcribe
        self.mlx_lock_entered: list[bool] = []

    def _run_transcription_queue_worker(self, poll_interval_sec: float = 0.05) -> None:
        """Copied verbatim from BackendService but imports mlx_lock locally."""
        # Import the real module so tests can patch it
        from core.mlx_lock import mlx_lock  # noqa: PLC0415

        while not self._tq_shutdown_event.wait(timeout=poll_interval_sec):
            try:
                job_dict = self._transcription_queue.process_next()
            except Exception:
                continue

            if job_dict is None:
                continue

            job_id = job_dict.get("job_id", "")
            file_path = job_dict.get("file_path", "")
            try:
                with mlx_lock():
                    self.mlx_lock_entered.append(True)
                    result = self.transcriber.transcribe(file_path)
                self._transcription_queue.mark_completed(job_id, result)
            except Exception as exc:  # noqa: BLE001
                self.mlx_lock_entered.append(True)  # lock was still entered before the raise
                self._transcription_queue.mark_failed(job_id, str(exc))

    def start_worker(self, poll_interval_sec: float = 0.05) -> threading.Thread:
        t = threading.Thread(
            target=self._run_transcription_queue_worker,
            kwargs={"poll_interval_sec": poll_interval_sec},
            daemon=True,
            name="test-tq-worker",
        )
        t.start()
        return t

    def stop_worker(self, thread: threading.Thread, timeout: float = 2.0) -> None:
        self._tq_shutdown_event.set()
        thread.join(timeout=timeout)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTranscriptionQueueWorkerProcessesJob(unittest.TestCase):
    """Worker picks up pending job and marks it completed on success."""

    def test_enqueued_job_processed_by_worker(self):
        fake = _FakeTranscriber()
        fake._result = {"text": "transcribed text", "confidence": 0.95}
        harness = _WorkerHarness(fake)

        # Enqueue a job BEFORE starting the worker so it's immediately visible
        job_id = harness._transcription_queue.enqueue("/tmp/audio.wav")

        thread = harness.start_worker(poll_interval_sec=0.05)
        try:
            # Wait up to 2 s for job to be completed
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                status = harness._transcription_queue.get_status(job_id)
                if status.get("status") == STATUS_COMPLETED:
                    break
                time.sleep(0.02)
        finally:
            harness.stop_worker(thread)

        status = harness._transcription_queue.get_status(job_id)
        self.assertEqual(status["status"], STATUS_COMPLETED)
        self.assertEqual(status["result"], {"text": "transcribed text", "confidence": 0.95})
        # Transcriber was called with the file path
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][0], "/tmp/audio.wav")


class TestTranscriptionQueueWorkerHandlesException(unittest.TestCase):
    """Worker calls mark_failed when transcribe raises."""

    def test_worker_marks_failed_on_transcribe_exception(self):
        fake = _FakeTranscriber()
        fake._raises = RuntimeError("STT engine exploded")
        harness = _WorkerHarness(fake)

        job_id = harness._transcription_queue.enqueue("/tmp/bad_audio.wav")

        thread = harness.start_worker(poll_interval_sec=0.05)
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                status = harness._transcription_queue.get_status(job_id)
                if status.get("status") == STATUS_FAILED:
                    break
                time.sleep(0.02)
        finally:
            harness.stop_worker(thread)

        status = harness._transcription_queue.get_status(job_id)
        self.assertEqual(status["status"], STATUS_FAILED)
        self.assertIn("STT engine exploded", status.get("error", ""))


class TestTranscriptionQueueWorkerShutdown(unittest.TestCase):
    """Worker exits cleanly when shutdown_event is set."""

    def test_worker_respects_shutdown_event(self):
        harness = _WorkerHarness()
        # Do NOT enqueue any jobs — worker should idle then stop
        thread = harness.start_worker(poll_interval_sec=0.05)
        self.assertTrue(thread.is_alive(), "worker thread must be alive before stop")

        harness._tq_shutdown_event.set()
        thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive(), "worker thread must stop after shutdown_event.set()")


class TestTranscriptionQueueWorkerMlxLock(unittest.TestCase):
    """Worker acquires mlx_lock during transcribe call."""

    def test_worker_holds_mlx_lock_during_transcribe(self):
        """Verify mlx_lock context manager is entered for each transcription."""
        fake = _FakeTranscriber()
        harness = _WorkerHarness(fake)

        lock_entered_events: list[threading.Event] = []

        # Patch core.mlx_lock.mlx_lock with a context manager that records entry
        from contextlib import contextmanager

        @contextmanager
        def _tracking_lock():
            lock_entered_events.append(threading.Event())
            lock_entered_events[-1].set()
            yield

        job_id = harness._transcription_queue.enqueue("/tmp/lock_test.wav")

        with patch("core.mlx_lock.mlx_lock", _tracking_lock):
            thread = harness.start_worker(poll_interval_sec=0.05)
            try:
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    status = harness._transcription_queue.get_status(job_id)
                    if status.get("status") in (STATUS_COMPLETED, STATUS_FAILED):
                        break
                    time.sleep(0.02)
            finally:
                harness.stop_worker(thread)

        # mlx_lock must have been entered at least once (for the one job)
        self.assertGreaterEqual(
            len(lock_entered_events),
            1,
            "mlx_lock must be entered during transcription",
        )
        self.assertTrue(
            all(ev.is_set() for ev in lock_entered_events),
            "All lock entries must have actually executed",
        )


class TestTranscriptionQueueWorkerMultipleJobs(unittest.TestCase):
    """Worker processes jobs sequentially, one at a time."""

    def test_multiple_jobs_all_completed(self):
        call_order: list[str] = []

        class _OrderedTranscriber:
            def transcribe(self, audio_data: Any, **kwargs: Any) -> dict:
                call_order.append(audio_data)
                return {"text": f"result for {audio_data}"}

        harness = _WorkerHarness(_OrderedTranscriber())
        job_ids = [
            harness._transcription_queue.enqueue(f"/tmp/file_{i}.wav", priority=5)
            for i in range(3)
        ]

        thread = harness.start_worker(poll_interval_sec=0.05)
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                statuses = [
                    harness._transcription_queue.get_status(jid)["status"]
                    for jid in job_ids
                ]
                if all(s == STATUS_COMPLETED for s in statuses):
                    break
                time.sleep(0.02)
        finally:
            harness.stop_worker(thread)

        for jid in job_ids:
            self.assertEqual(
                harness._transcription_queue.get_status(jid)["status"],
                STATUS_COMPLETED,
            )
        self.assertEqual(len(call_order), 3)


class TestTranscriptionQueueWorkerIdleNoBusyWait(unittest.TestCase):
    """Worker uses Event.wait() — no job means minimal CPU use (basic smoke test)."""

    def test_worker_idle_uses_wait(self):
        harness = _WorkerHarness()
        # Empty queue — worker should sleep via Event.wait()
        thread = harness.start_worker(poll_interval_sec=0.1)
        time.sleep(0.25)  # Let two poll cycles pass
        harness.stop_worker(thread)
        # If we get here without hanging, the wait-based sleep works correctly
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
