"""Wave 1044: IPC wiring tests for BulkReprocessor.

Verifies that BackendService correctly wires
bulk_reprocess_start / bulk_reprocess_cancel / bulk_reprocess_status
to the _bulk_reprocessor attribute (re-wired after Wave 65 removal — W1037 F4).

Strategy: instantiate BulkReprocessor with mocks and inject into a minimal
BackendService-like object; test the 3 handler methods directly without
needing to bootstrap the full service. Also tests the dispatch key names
by inspecting the handler source.
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.bulk_reprocess import BulkReprocessor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_reprocessor(store=None, transcriber=None, version_manager=None, event_bus=None):
    """Build a BulkReprocessor with all collaborators mocked by default."""
    if store is None:
        store = MagicMock()
        store._lock = MagicMock(return_value=contextlib.nullcontext())
        store._load_active_items_unlocked = MagicMock(return_value=[])
        store.update_history_item_text = MagicMock(return_value=True)
    if transcriber is None:
        transcriber = MagicMock()
    if version_manager is None:
        version_manager = MagicMock()
    return BulkReprocessor(
        store=store,
        transcriber=transcriber,
        version_manager=version_manager,
        event_bus=event_bus,
    )


class _FakeService:
    """Minimal stand-in for BackendService with just _bulk_reprocessor wired."""

    def __init__(self, bulk_reprocessor):
        self._bulk_reprocessor = bulk_reprocessor

    # The actual handler methods are copied from service.py logic
    def _handle_bulk_reprocess_start(self, params):
        only_low_confidence = bool(params.get("only_low_confidence", True))
        threshold = float(params.get("threshold", 0.7))
        dry_run = bool(params.get("dry_run", False))
        task_id = str(params.get("task_id", ""))
        return self._bulk_reprocessor.reprocess(
            only_low_confidence=only_low_confidence,
            threshold=threshold,
            dry_run=dry_run,
            task_id=task_id,
        )

    def _handle_bulk_reprocess_cancel(self, params):
        self._bulk_reprocessor.cancel()
        return {"ok": True}

    def _handle_bulk_reprocess_status(self, params):
        return {"cancel_requested": self._bulk_reprocessor._cancel_event.is_set()}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBulkReprocessStartDispatched(unittest.TestCase):
    """bulk_reprocess_start correctly calls BulkReprocessor.reprocess()."""

    def test_bulk_reprocess_start_dispatched(self):
        """Params are forwarded to reprocess() with correct types."""
        rp = _make_reprocessor()
        rp.reprocess = MagicMock(return_value={
            "total": 5, "reprocessed": 3, "skipped": 2, "errors": [], "cancelled": False,
        })
        svc = _FakeService(rp)

        result = svc._handle_bulk_reprocess_start({
            "only_low_confidence": True,
            "threshold": 0.7,
            "dry_run": False,
            "task_id": "test-task-1",
        })

        rp.reprocess.assert_called_once_with(
            only_low_confidence=True,
            threshold=0.7,
            dry_run=False,
            task_id="test-task-1",
        )
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["reprocessed"], 3)
        self.assertFalse(result["cancelled"])

    def test_bulk_reprocess_start_default_params(self):
        """Default params (only_low_confidence=True, threshold=0.7) are applied."""
        rp = _make_reprocessor()
        rp.reprocess = MagicMock(return_value={
            "total": 0, "reprocessed": 0, "skipped": 0, "errors": [], "cancelled": False,
        })
        svc = _FakeService(rp)

        svc._handle_bulk_reprocess_start({})

        rp.reprocess.assert_called_once_with(
            only_low_confidence=True,
            threshold=0.7,
            dry_run=False,
            task_id="",
        )

    def test_bulk_reprocess_start_dry_run_flag(self):
        """dry_run=True is correctly forwarded."""
        rp = _make_reprocessor()
        rp.reprocess = MagicMock(return_value={
            "total": 10, "reprocessed": 10, "skipped": 0, "errors": [], "cancelled": False,
        })
        svc = _FakeService(rp)

        result = svc._handle_bulk_reprocess_start({"dry_run": True})

        call_kwargs = rp.reprocess.call_args.kwargs
        self.assertTrue(call_kwargs["dry_run"])
        self.assertEqual(result["reprocessed"], 10)


class TestBulkReprocessStatusReturnsProgress(unittest.TestCase):
    """bulk_reprocess_status returns cancel_requested based on _cancel_event."""

    def test_bulk_reprocess_status_not_cancelled(self):
        """Returns cancel_requested=False when not cancelled."""
        rp = _make_reprocessor()
        # _cancel_event is fresh (not set)
        svc = _FakeService(rp)

        result = svc._handle_bulk_reprocess_status({})

        self.assertIn("cancel_requested", result)
        self.assertFalse(result["cancel_requested"])

    def test_bulk_reprocess_status_after_cancel(self):
        """Returns cancel_requested=True after cancel() is called."""
        rp = _make_reprocessor()
        rp.cancel()  # sets _cancel_event
        svc = _FakeService(rp)

        result = svc._handle_bulk_reprocess_status({})

        self.assertTrue(result["cancel_requested"])

    def test_bulk_reprocess_status_reset_after_new_run(self):
        """cancel_event clears when reprocess() is called again."""
        rp = _make_reprocessor()
        # Mark cancelled then reset via reprocess (dry_run to avoid I/O)
        rp.cancel()
        self.assertTrue(rp._cancel_event.is_set())
        rp.reprocess(dry_run=True, task_id="reset-test")
        # After reprocess, _reset_cancel() is called
        self.assertFalse(rp._cancel_event.is_set())


class TestBulkReprocessCancelDispatched(unittest.TestCase):
    """bulk_reprocess_cancel calls BulkReprocessor.cancel()."""

    def test_bulk_reprocess_cancel_dispatched(self):
        """cancel() is called and {"ok": True} is returned."""
        rp = _make_reprocessor()
        svc = _FakeService(rp)

        result = svc._handle_bulk_reprocess_cancel({})

        self.assertTrue(rp._cancel_event.is_set(), "cancel() should set _cancel_event")
        self.assertEqual(result, {"ok": True})

    def test_bulk_reprocess_cancel_idempotent(self):
        """Calling cancel twice is safe."""
        rp = _make_reprocessor()
        svc = _FakeService(rp)

        svc._handle_bulk_reprocess_cancel({})
        result = svc._handle_bulk_reprocess_cancel({})

        self.assertEqual(result, {"ok": True})
        self.assertTrue(rp._cancel_event.is_set())


class TestBulkReprocessServiceWiring(unittest.TestCase):
    """Verify service.py contains the 3 dispatch entries and _bulk_reprocessor."""

    def test_service_py_has_bulk_reprocessor_import(self):
        """service.py imports BulkReprocessor."""
        service_path = os.path.join(PROJECT_ROOT, "backend", "service.py")
        with open(service_path) as f:
            source = f.read()
        self.assertIn("from backend.bulk_reprocess import BulkReprocessor", source,
                      "BulkReprocessor not imported in service.py")

    def test_service_py_instantiates_bulk_reprocessor(self):
        """service.py instantiates self._bulk_reprocessor."""
        service_path = os.path.join(PROJECT_ROOT, "backend", "service.py")
        with open(service_path) as f:
            source = f.read()
        self.assertIn("self._bulk_reprocessor = BulkReprocessor(", source,
                      "self._bulk_reprocessor not instantiated in service.py")

    def test_service_py_has_all_three_dispatch_entries(self):
        """service.py dispatch table contains all 3 bulk_reprocess entries."""
        service_path = os.path.join(PROJECT_ROOT, "backend", "service.py")
        with open(service_path) as f:
            source = f.read()
        for method in ("bulk_reprocess_start", "bulk_reprocess_cancel", "bulk_reprocess_status"):
            self.assertIn(f'"{method}"', source,
                          f"Dispatch entry {method!r} missing from service.py")

    def test_service_py_has_all_three_handler_methods(self):
        """service.py defines _handle_bulk_reprocess_{start,cancel,status}."""
        service_path = os.path.join(PROJECT_ROOT, "backend", "service.py")
        with open(service_path) as f:
            source = f.read()
        for suffix in ("start", "cancel", "status"):
            name = f"_handle_bulk_reprocess_{suffix}"
            self.assertIn(f"def {name}(", source,
                          f"Handler method {name!r} missing from service.py")


if __name__ == "__main__":
    unittest.main()
