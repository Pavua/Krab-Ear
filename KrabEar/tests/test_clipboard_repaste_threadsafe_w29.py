"""wave-29 thread-safety: handle_repaste_item must iterate a SNAPSHOT of the shared
_clipboard_history list, not the live list.

_clipboard_history is shared by reference between HistoryService and
RecordingCoreService. RecordingCoreService.append()/trim (recording-completion
thread) mutates it concurrently with handle_repaste_item (IPC thread). Iterating the
LIVE list via reversed() under a concurrent shrink makes the built-in reverse
iterator terminate early (it_index >= size → StopIteration), silently skipping
entries — so an item that IS present (e.g. at index 0) can be reported "not found".

This is verified deterministically (no timing) via a list subclass whose reversed()
is lossy on the LIVE object but whose list(self) copy is faithful: the fix
(reversed(list(...))) finds the item; the unfixed code (reversed(self)) does not.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import HistoryService  # noqa: E402


class _LossyReversedList(list):
    """A list whose reversed() drops index 0 — simulates the built-in reverse
    iterator terminating early under a concurrent shrink. list(self) stays faithful,
    so a caller that snapshots before reversing is immune.
    """

    def __reversed__(self):
        return iter(self[:0:-1])  # reversed order, EXCLUDING index 0 (the 'target')


class ClipboardRepasteThreadSafeTest(unittest.TestCase):
    def test_repaste_iterates_snapshot_not_live_list(self):
        clip = _LossyReversedList([{"history_id": "target", "text": "FOUND", "ts": 0}])
        svc = HistoryService(
            store=MagicMock(),
            clipboard_history=clip,
            cached_settings=lambda: {},  # privacy mode OFF
        )
        # With the fix (reversed(list(clip))) the snapshot is a faithful plain-list
        # copy → target found. Without it (reversed(clip)) the lossy __reversed__
        # drops index 0 → RuntimeError "не найдена".
        res = svc.handle_repaste_item({"history_id": "target"})
        self.assertTrue(res.get("found"))
        self.assertEqual(res.get("text"), "FOUND")


if __name__ == "__main__":
    unittest.main()
