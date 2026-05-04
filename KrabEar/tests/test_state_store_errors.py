"""Tests for history.write_fail error push (Phase B.2 F3)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore


class StateStorePushErrorHelperTests(unittest.TestCase):
    """Unit tests for StateStore._push_error helper."""

    def _make_store_with_bus(self) -> StateStore:
        tmp = tempfile.mkdtemp()
        store = StateStore(Path(tmp))
        store._error_bus = MagicMock()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        return store

    def test_no_bus_does_not_raise(self) -> None:
        """_push_error with no _error_bus set must not raise."""
        tmp = tempfile.mkdtemp()
        store = StateStore(Path(tmp))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        # No _error_bus injected
        store._push_error("history.write_fail", "test debug")  # must not raise

    def test_broken_bus_does_not_raise(self) -> None:
        """If error_bus.push itself throws, _push_error swallows the exception."""
        store = self._make_store_with_bus()
        store._error_bus.push.side_effect = RuntimeError("bus broken")
        store._push_error("history.write_fail", "disk full")  # must not raise

    def test_push_calls_bus_with_correct_code(self) -> None:
        store = self._make_store_with_bus()
        store._push_error("history.write_fail", "PermissionError: permission denied")
        self.assertEqual(store._error_bus.push.call_count, 1)
        pushed = store._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "history.write_fail")
        self.assertEqual(pushed.component, "history")
        self.assertEqual(pushed.severity, "critical")

    def test_push_contains_data_dir_in_context(self) -> None:
        store = self._make_store_with_bus()
        store._push_error("history.write_fail", "OSError")
        pushed = store._error_bus.push.call_args[0][0]
        self.assertIn("data_dir", pushed.context)


class StateStoreWriteFailTests(unittest.TestCase):
    """add_history_item pushes history.write_fail when _append_ndjson fails."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name))
        self.store._error_bus = MagicMock()

    def test_append_ndjson_failure_pushes_write_fail(self) -> None:
        """When _append_ndjson raises (e.g. disk full), history.write_fail is pushed."""
        with patch.object(StateStore, "_append_ndjson", side_effect=OSError("No space left on device")):
            with self.assertRaises(OSError):
                self.store.add_history_item(text="test text")

        self.assertEqual(self.store._error_bus.push.call_count, 1)
        pushed = self.store._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "history.write_fail")
        self.assertEqual(pushed.severity, "critical")

    def test_permission_denied_pushes_write_fail(self) -> None:
        """PermissionError on write also triggers history.write_fail."""
        with patch.object(StateStore, "_append_ndjson",
                          side_effect=PermissionError("Permission denied")):
            with self.assertRaises(PermissionError):
                self.store.add_history_item(text="hello world")

        pushed = self.store._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "history.write_fail")
        self.assertIn("PermissionError", pushed.message_debug)

    def test_successful_write_does_not_push(self) -> None:
        """Successful add_history_item does not trigger any error push."""
        self.store.add_history_item(text="normal text")
        self.store._error_bus.push.assert_not_called()

    def test_write_fail_re_raises_after_push(self) -> None:
        """The exception is re-raised after pushing to error_bus (fail-loudly)."""
        with patch.object(StateStore, "_append_ndjson", side_effect=OSError("ENOSPC")):
            with self.assertRaises(OSError):
                self.store.add_history_item(text="test")

        # Verified above; also check push happened before raise
        self.assertEqual(self.store._error_bus.push.call_count, 1)

    def test_no_bus_write_fail_still_raises(self) -> None:
        """Without error_bus, exception still propagates normally."""
        tmp = tempfile.mkdtemp()
        store = StateStore(Path(tmp))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        # No _error_bus

        with patch.object(StateStore, "_append_ndjson", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.add_history_item(text="test")


if __name__ == "__main__":
    unittest.main()
