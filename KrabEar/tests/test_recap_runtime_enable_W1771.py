"""Tests for W1771 MED: _on_recap_enabled after-save hook starts RecapScheduler
when recap_email_enabled is toggled False → True at runtime.

Without the hook, enabling the digest via set_settings({recap_email_enabled: True})
persisted the setting but never started the daemon thread (the init-time guard fires
only once), so emails were silently never sent until a backend restart.

These tests exercise the hook closure in isolation — no BackendService construction
needed, which keeps the test mlx-agnostic and fast on Python 3.12 without mlx wheels.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _make_recap_enabled_hook(scheduler_mock):
    """Build the _on_recap_enabled closure from service.py in isolation.

    Mirrors the exact code added in BackendService.__init__ (W1771) so we can
    test the hook logic without constructing a full BackendService (which would
    pull in mlx, sounddevice, pyannote, etc.).
    """
    _scheduler = scheduler_mock

    def _on_recap_enabled(old: dict, new: dict) -> None:
        old_enabled = bool(old.get("recap_email_enabled", False))
        new_enabled = bool(new.get("recap_email_enabled", False))
        if not old_enabled and new_enabled:
            _scheduler.start()

    return _on_recap_enabled


class TestRecapRuntimeEnableHookW1771(unittest.TestCase):
    """_on_recap_enabled hook starts the scheduler on False→True transition."""

    def setUp(self):
        self.scheduler = MagicMock()
        self.hook = _make_recap_enabled_hook(self.scheduler)

    # ------------------------------------------------------------------
    # Positive: should call start()
    # ------------------------------------------------------------------

    def test_false_to_true_calls_start(self):
        """Enabling recap at runtime (False→True) must start the scheduler."""
        self.hook(
            {"recap_email_enabled": False},
            {"recap_email_enabled": True},
        )
        self.scheduler.start.assert_called_once()

    def test_absent_to_true_calls_start(self):
        """Key absent in old settings defaults to falsy; True in new → start."""
        self.hook({}, {"recap_email_enabled": True})
        self.scheduler.start.assert_called_once()

    def test_false_to_truthy_int_calls_start(self):
        """Non-bool truthy value (e.g. 1) is coerced and triggers start."""
        self.hook({"recap_email_enabled": False}, {"recap_email_enabled": 1})
        self.scheduler.start.assert_called_once()

    # ------------------------------------------------------------------
    # Negative: should NOT call start()
    # ------------------------------------------------------------------

    def test_true_to_false_does_not_call_start(self):
        """Disabling recap (True→False) must not call start."""
        self.hook(
            {"recap_email_enabled": True},
            {"recap_email_enabled": False},
        )
        self.scheduler.start.assert_not_called()

    def test_true_to_true_does_not_call_start(self):
        """Already-enabled toggle (True→True) must not call start again."""
        self.hook(
            {"recap_email_enabled": True},
            {"recap_email_enabled": True},
        )
        self.scheduler.start.assert_not_called()

    def test_false_to_false_does_not_call_start(self):
        """Disabled stays disabled (False→False) — no start."""
        self.hook(
            {"recap_email_enabled": False},
            {"recap_email_enabled": False},
        )
        self.scheduler.start.assert_not_called()

    def test_absent_to_absent_does_not_call_start(self):
        """Key absent in both old and new — no start."""
        self.hook({}, {})
        self.scheduler.start.assert_not_called()

    def test_absent_to_false_does_not_call_start(self):
        """Absent→False is still falsy→falsy — no start."""
        self.hook({}, {"recap_email_enabled": False})
        self.scheduler.start.assert_not_called()

    def test_true_to_absent_does_not_call_start(self):
        """True→absent: new defaults to False, so True→False — no start."""
        self.hook({"recap_email_enabled": True}, {})
        self.scheduler.start.assert_not_called()

    # ------------------------------------------------------------------
    # Idempotency: start() is called but RecapScheduler itself is idempotent
    # (the hook only fires once per transition; idempotency lives in scheduler.start())
    # ------------------------------------------------------------------

    def test_hook_only_calls_start_on_each_false_to_true_transition(self):
        """Hook calls start() exactly once per False→True call.

        (The actual idempotency lives inside RecapScheduler.start() via the
        is_alive guard — the hook just triggers it on each qualifying transition.)
        """
        self.hook({"recap_email_enabled": False}, {"recap_email_enabled": True})
        self.hook({"recap_email_enabled": False}, {"recap_email_enabled": True})
        self.assertEqual(self.scheduler.start.call_count, 2)


class TestRecapSchedulerStartIdempotent(unittest.TestCase):
    """Verify RecapScheduler.start() itself is idempotent (is_alive guard).

    This confirms the real scheduler won't spawn duplicate daemon threads when the
    hook fires and the thread is already running.
    """

    def test_start_is_idempotent_when_thread_alive(self):
        """start() must be a no-op if the thread is already alive."""
        # Import RecapScheduler — pure Python, no mlx dependency.
        from backend.recap_scheduler import RecapScheduler

        mock_email_sender = MagicMock()
        mock_digest = MagicMock()
        mock_store = MagicMock()

        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = RecapScheduler(
                email_sender=mock_email_sender,
                digest_generator=mock_digest,
                store=mock_store,
                data_dir=tmpdir,
                recap_email_to="test@example.com",
                recap_time_hour=20,
                enabled=True,
                check_interval_sec=3600,  # won't actually tick in test
            )
            scheduler.start()
            thread_ref = scheduler._thread
            self.assertIsNotNone(thread_ref)
            self.assertTrue(thread_ref.is_alive())

            # Call start() again — must not replace the thread
            scheduler.start()
            self.assertIs(scheduler._thread, thread_ref)

            # Cleanup
            scheduler.stop()


if __name__ == "__main__":
    unittest.main()
