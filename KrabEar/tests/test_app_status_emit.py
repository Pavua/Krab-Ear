"""Tests for app.status SSE emit hooks (commit a5ba484).

Verifies:
1. ObsidianSyncManager.sync() emits `app.status` with op="obsidian_sync" per item,
   then op="idle" at end.
2. Backward compat: event_bus=None causes no errors.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.obsidian_sync import ObsidianSyncManager  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _CapturingEventBus:
    """Records all emit calls for assertion."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))

    def emit_typed(self, event_type, payload) -> None:
        key = event_type.value if hasattr(event_type, "value") else str(event_type)
        self.events.append((key, payload))


def _make_item(i: int = 0) -> dict:
    """Return a minimal HistoryItem-like dict."""
    return {
        "id": f"id{i:04d}",
        "ts": f"2026-05-09T0{i % 10}:00:00+00:00",
        "text": f"text {i}",
    }


def _status_events(bus: _CapturingEventBus) -> list[dict]:
    """Filter captured events to app.status payloads only."""
    return [payload for (etype, payload) in bus.events if etype == "app.status"]


# ---------------------------------------------------------------------------
# ObsidianSyncManager app.status emit tests
# ---------------------------------------------------------------------------

class ObsidianAppStatusEmitTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bus = _CapturingEventBus()
        data_dir = Path(self.tmp.name) / "data"
        data_dir.mkdir()
        self.mgr = ObsidianSyncManager(data_dir=data_dir, event_bus=self.bus)
        vault = Path(self.tmp.name) / "vault"
        vault.mkdir()
        self.mgr.configure(str(vault), folder="Calls")

    # ------------------------------------------------------------------
    # Empty list: started + idle only
    # ------------------------------------------------------------------

    def test_sync_empty_items_emits_started_and_idle(self) -> None:
        """sync([]) emits started then idle with no per-item events."""
        self.mgr.sync([], force=True)
        statuses = _status_events(self.bus)
        self.assertGreaterEqual(len(statuses), 2, "Expected at least started + idle")

        # First event is 'started'
        first = statuses[0]
        self.assertEqual(first.get("op"), "obsidian_sync")
        self.assertEqual(first.get("stage"), "started")
        self.assertEqual(first.get("total_files"), 0)

        # Last event is 'idle'
        last = statuses[-1]
        self.assertEqual(last.get("op"), "idle")

    # ------------------------------------------------------------------
    # Three items: started + 3 per-item (stage=syncing) + idle
    # ------------------------------------------------------------------

    def test_sync_three_items_emits_per_item_then_idle(self) -> None:
        """sync() emits one event per item (stage=syncing) between started and idle."""
        items = [_make_item(i) for i in range(3)]
        self.mgr.sync(items, force=True)
        statuses = _status_events(self.bus)

        # At minimum: started + 3 per-item + idle
        self.assertGreaterEqual(len(statuses), 5)

        # Last event is idle
        self.assertEqual(statuses[-1].get("op"), "idle")

        # First event is obsidian_sync/started
        self.assertEqual(statuses[0].get("op"), "obsidian_sync")
        self.assertEqual(statuses[0].get("stage"), "started")

        # Per-item events have stage=syncing and sequential file_index 1..3
        per_item = [
            s for s in statuses
            if s.get("op") == "obsidian_sync" and s.get("stage") == "syncing"
        ]
        self.assertEqual(len(per_item), 3, f"Expected 3 per-item events, got: {per_item}")
        for idx, event in enumerate(per_item):
            self.assertEqual(
                event.get("file_index"), idx + 1,
                f"Event {idx} has wrong file_index: {event}",
            )

    # ------------------------------------------------------------------
    # progress field
    # ------------------------------------------------------------------

    def test_sync_progress_reaches_one(self) -> None:
        """The last per-item event should have progress == 1.0."""
        items = [_make_item(i) for i in range(2)]
        self.mgr.sync(items, force=True)
        statuses = _status_events(self.bus)
        per_item = [
            s for s in statuses
            if s.get("op") == "obsidian_sync" and s.get("stage") == "syncing"
        ]
        self.assertTrue(len(per_item) > 0)
        last_item_event = per_item[-1]
        self.assertAlmostEqual(last_item_event.get("progress", 0.0), 1.0, places=5)

    # ------------------------------------------------------------------
    # total_files matches list length
    # ------------------------------------------------------------------

    def test_sync_started_event_has_correct_total(self) -> None:
        """The 'started' emit carries total_files == len(items)."""
        items = [_make_item(i) for i in range(5)]
        self.mgr.sync(items, force=True)
        statuses = _status_events(self.bus)
        started = statuses[0]
        self.assertEqual(started.get("total_files"), 5)

    # ------------------------------------------------------------------
    # idle op and progress
    # ------------------------------------------------------------------

    def test_sync_idle_event_has_progress_one(self) -> None:
        """The final idle event has progress=1.0."""
        self.mgr.sync([_make_item(0)], force=True)
        statuses = _status_events(self.bus)
        idle = statuses[-1]
        self.assertEqual(idle.get("op"), "idle")
        self.assertAlmostEqual(idle.get("progress", 0.0), 1.0, places=5)

    # ------------------------------------------------------------------
    # Incremental sync: skipped items still emit per-item event
    # ------------------------------------------------------------------

    def test_sync_skipped_items_still_emit_per_item(self) -> None:
        """Items skipped by incremental logic still emit a syncing status."""
        item = _make_item(0)
        # First sync to set last_sync_ts
        self.mgr.sync([item])
        self.bus.events.clear()

        # Second sync without force: item ts is older than last_sync_ts → skipped
        self.mgr.sync([item], force=False)
        statuses = _status_events(self.bus)

        # Should still have started + 1 per-item + idle
        per_item = [
            s for s in statuses
            if s.get("op") == "obsidian_sync" and s.get("stage") == "syncing"
        ]
        self.assertEqual(len(per_item), 1, "Expected 1 per-item event even for skipped item")
        self.assertEqual(statuses[-1].get("op"), "idle")

    # ------------------------------------------------------------------
    # Backward compat: no event_bus → no errors
    # ------------------------------------------------------------------

    def test_sync_no_event_bus_no_error(self) -> None:
        """Backward compat: if event_bus=None, sync() completes without errors."""
        data_dir2 = Path(self.tmp.name) / "data2"
        data_dir2.mkdir()
        vault2 = Path(self.tmp.name) / "vault2"
        vault2.mkdir()
        mgr2 = ObsidianSyncManager(data_dir=data_dir2)  # no event_bus
        mgr2.configure(str(vault2), folder="Calls")
        items = [_make_item(i) for i in range(2)]
        # Should not raise
        result = mgr2.sync(items, force=True)
        self.assertEqual(result.synced_count, 2)
        self.assertEqual(result.errors, [])

    # ------------------------------------------------------------------
    # emit is called on the event_bus (not emit_typed)
    # ------------------------------------------------------------------

    def test_only_emit_called_not_emit_typed(self) -> None:
        """ObsidianSyncManager uses bus.emit(), not bus.emit_typed()."""

        class _TrackingBus(_CapturingEventBus):
            emit_typed_called = False

            def emit_typed(self, event_type, payload) -> None:
                self.emit_typed_called = True
                super().emit_typed(event_type, payload)

        bus = _TrackingBus()
        data_dir3 = Path(self.tmp.name) / "data3"
        data_dir3.mkdir()
        vault3 = Path(self.tmp.name) / "vault3"
        vault3.mkdir()
        mgr3 = ObsidianSyncManager(data_dir=data_dir3, event_bus=bus)
        mgr3.configure(str(vault3), folder="Calls")
        mgr3.sync([_make_item(0)], force=True)

        self.assertFalse(bus.emit_typed_called, "emit_typed should not be called by sync()")
        self.assertGreater(len(bus.events), 0, "emit should have been called")

    # ------------------------------------------------------------------
    # Single item: verify file_index = 1
    # ------------------------------------------------------------------

    def test_sync_single_item_file_index_is_one(self) -> None:
        """A single-item sync emits file_index=1 in the syncing event."""
        self.mgr.sync([_make_item(0)], force=True)
        statuses = _status_events(self.bus)
        per_item = [
            s for s in statuses
            if s.get("op") == "obsidian_sync" and s.get("stage") == "syncing"
        ]
        self.assertEqual(len(per_item), 1)
        self.assertEqual(per_item[0].get("file_index"), 1)
        self.assertEqual(per_item[0].get("total_files"), 1)

    # ------------------------------------------------------------------
    # ts field present in all app.status events
    # ------------------------------------------------------------------

    def test_all_status_events_have_ts_field(self) -> None:
        """Every app.status event carries a 'ts' numeric timestamp."""
        self.mgr.sync([_make_item(0), _make_item(1)], force=True)
        statuses = _status_events(self.bus)
        for s in statuses:
            self.assertIn("ts", s, f"Missing 'ts' in event: {s}")
            self.assertIsInstance(s["ts"], (int, float), f"Non-numeric ts in event: {s}")


if __name__ == "__main__":
    unittest.main()
