"""test_transcription_queue_privacy_w30.py — Wave-30 privacy-gate + clear() tests.

Covers:
  FIX 1 (MED privacy gate) — handle_list_queue / handle_get_status suppress data
    when privacy_mode_fn returns True.
  FIX 2 (LOW cleanup)      — handle_peek also suppressed under privacy mode.
  FIX 3 (registry)         — TranscriptionQueue.clear() empties _jobs +
    _terminal_order + rewrites the persist file.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Path bootstrap — allow running standalone or via pytest from repo root.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.transcription_queue import TranscriptionQueue  # noqa: E402


class PrivacyGateListQueueTest(unittest.TestCase):
    """handle_list_queue returns suppressed response when privacy mode is active."""

    def _make_queue(self, privacy_on: bool) -> TranscriptionQueue:
        return TranscriptionQueue(privacy_mode_fn=lambda: privacy_on)

    def test_list_queue_normal_returns_jobs(self) -> None:
        q = self._make_queue(privacy_on=False)
        q.enqueue("/tmp/a.wav")
        result = q.handle_list_queue({})
        self.assertIn("jobs", result)
        self.assertEqual(len(result["jobs"]), 1)
        self.assertNotIn("reason", result)

    def test_list_queue_privacy_on_returns_empty(self) -> None:
        q = self._make_queue(privacy_on=True)
        q.enqueue("/tmp/b.wav")
        result = q.handle_list_queue({})
        self.assertEqual(result["jobs"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["reason"], "privacy_mode_active")

    def test_list_queue_no_privacy_fn_is_transparent(self) -> None:
        """No privacy_mode_fn → original behaviour, no suppression."""
        q = TranscriptionQueue()
        q.enqueue("/tmp/c.wav")
        result = q.handle_list_queue({})
        self.assertEqual(len(result["jobs"]), 1)

    def test_list_queue_privacy_fn_returning_false_is_transparent(self) -> None:
        q = TranscriptionQueue(privacy_mode_fn=lambda: False)
        q.enqueue("/tmp/d.wav")
        result = q.handle_list_queue({})
        self.assertEqual(len(result["jobs"]), 1)
        self.assertNotIn("reason", result)


class PrivacyGateGetStatusTest(unittest.TestCase):
    """handle_get_status returns suppressed response when privacy mode is active."""

    def test_get_status_privacy_on_suppresses_job(self) -> None:
        q = TranscriptionQueue(privacy_mode_fn=lambda: True)
        job_id = q.enqueue("/tmp/e.wav")
        result = q.handle_get_status({"job_id": job_id})
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "privacy_mode_active")
        # file_path must NOT be present
        self.assertNotIn("file_path", result)

    def test_get_status_privacy_off_returns_real_status(self) -> None:
        q = TranscriptionQueue(privacy_mode_fn=lambda: False)
        job_id = q.enqueue("/tmp/f.wav")
        result = q.handle_get_status({"job_id": job_id})
        self.assertEqual(result["job_id"], job_id)
        self.assertIn("file_path", result)
        self.assertNotIn("reason", result)

    def test_get_status_privacy_on_does_not_require_job_id(self) -> None:
        """Privacy gate fires BEFORE job_id validation, so no ValueError."""
        q = TranscriptionQueue(privacy_mode_fn=lambda: True)
        # Missing job_id — normally raises ValueError, but privacy gate short-circuits.
        result = q.handle_get_status({})
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "privacy_mode_active")


class PrivacyGatePeekTest(unittest.TestCase):
    """handle_peek returns suppressed response when privacy mode is active (FIX 2)."""

    def test_peek_privacy_on_suppresses(self) -> None:
        q = TranscriptionQueue(privacy_mode_fn=lambda: True)
        q.enqueue("/tmp/g.wav")
        result = q.handle_peek({})
        self.assertIsNone(result["job"])
        self.assertEqual(result["reason"], "privacy_mode_active")

    def test_peek_privacy_off_returns_job(self) -> None:
        q = TranscriptionQueue(privacy_mode_fn=lambda: False)
        q.enqueue("/tmp/h.wav")
        result = q.handle_peek({})
        self.assertIsNotNone(result["job"])
        self.assertNotIn("reason", result)

    def test_peek_no_fn_returns_job(self) -> None:
        q = TranscriptionQueue()
        q.enqueue("/tmp/i.wav")
        result = q.handle_peek({})
        self.assertIsNotNone(result["job"])


class ClearMethodTest(unittest.TestCase):
    """TranscriptionQueue.clear() empties all in-memory state (FIX 3 / registry)."""

    def test_clear_empties_jobs(self) -> None:
        q = TranscriptionQueue()
        q.enqueue("/tmp/j.wav")
        q.enqueue("/tmp/k.wav")
        self.assertEqual(len(q._jobs), 2)
        q.clear()
        self.assertEqual(len(q._jobs), 0)

    def test_clear_empties_terminal_order(self) -> None:
        q = TranscriptionQueue()
        job_id = q.enqueue("/tmp/l.wav")
        q.mark_completed(job_id, result={"text": "hello"})
        self.assertGreater(len(q._terminal_order), 0)
        q.clear()
        self.assertEqual(len(q._terminal_order), 0)

    def test_clear_resets_persist_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            persist = Path(td) / "queue.ndjson"
            q = TranscriptionQueue(persist_path=persist)
            q.enqueue("/tmp/m.wav")
            # File should have content now
            self.assertTrue(persist.exists())
            self.assertGreater(persist.stat().st_size, 0)
            q.clear()
            # After clear the persist file should be empty (or absent)
            content = persist.read_text(encoding="utf-8") if persist.exists() else ""
            self.assertEqual(content.strip(), "")

    def test_clear_then_enqueue_works(self) -> None:
        q = TranscriptionQueue()
        q.enqueue("/tmp/n.wav")
        q.clear()
        new_id = q.enqueue("/tmp/o.wav")
        self.assertEqual(len(q._jobs), 1)
        self.assertIn(new_id, q._jobs)

    def test_clear_on_empty_queue_is_noop(self) -> None:
        q = TranscriptionQueue()
        q.clear()  # Must not raise
        self.assertEqual(len(q._jobs), 0)


class PrivacyModeWiringTest(unittest.TestCase):
    """Validate privacy_mode_fn is stored and consulted correctly."""

    def test_privacy_fn_called_dynamically(self) -> None:
        """privacy_mode_fn is a callable — switching it off mid-session resumes data."""
        state = {"on": True}
        q = TranscriptionQueue(privacy_mode_fn=lambda: state["on"])
        q.enqueue("/tmp/p.wav")

        # Privacy on → suppressed
        result = q.handle_list_queue({})
        self.assertEqual(result["jobs"], [])

        # Privacy off → data visible
        state["on"] = False
        result = q.handle_list_queue({})
        self.assertEqual(len(result["jobs"]), 1)

    def test_no_privacy_fn_constructor(self) -> None:
        q = TranscriptionQueue()
        self.assertIsNone(q._privacy_mode_fn)

    def test_privacy_fn_stored(self) -> None:
        fn = lambda: False  # noqa: E731
        q = TranscriptionQueue(privacy_mode_fn=fn)
        self.assertIs(q._privacy_mode_fn, fn)


if __name__ == "__main__":
    unittest.main()
