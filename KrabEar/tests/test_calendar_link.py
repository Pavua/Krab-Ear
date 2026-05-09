"""Тесты для CalendarLinker и связанных методов StateStore."""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock


# Module-level helper: автоматически patch'ит platform.system → "Darwin" в setUp
# для тестов которые мокают subprocess.run. Без этого CalendarLinker.find_active_event
# делает early-return на Linux (CI) ДО вызова subprocess.run → ассерты падают.
class _DarwinPatchedTestCase(unittest.TestCase):
    """Базовый класс для тестов CalendarLinker которые мокают subprocess.run.

    Активирует platform.system → "Darwin" patcher на всё время теста,
    чтобы early-return на не-macOS CI не съедал mock'и subprocess.run.
    """

    def setUp(self) -> None:
        super().setUp()
        self._darwin_patcher = patch("backend.calendar_link.platform.system", return_value="Darwin")
        self._darwin_patcher.start()
        self.addCleanup(self._darwin_patcher.stop)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.calendar_link import CalendarLinker, _parse_osascript_output, _epoch_to_iso
from backend.state_store import StateStore

SAMPLE_EPOCH_START = 1714000000
SAMPLE_EPOCH_END = 1714003600
SAMPLE_RAW = f"Stand-up|||{SAMPLE_EPOCH_START}|||{SAMPLE_EPOCH_END}|||Room 1|||Work\n"
SAMPLE_EVENT = {
    "title": "Stand-up",
    "start_iso": _epoch_to_iso(SAMPLE_EPOCH_START),
    "end_iso": _epoch_to_iso(SAMPLE_EPOCH_END),
    "location": "Room 1",
    "calendar_name": "Work",
}


class TestParseOsascriptOutput(unittest.TestCase):
    def test_single_event(self):
        events = _parse_osascript_output(SAMPLE_RAW)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "Stand-up")
        self.assertEqual(events[0]["location"], "Room 1")

    def test_empty_input(self):
        self.assertEqual(_parse_osascript_output(""), [])

    def test_missing_separator(self):
        self.assertEqual(_parse_osascript_output("No pipe delimiter here"), [])

    def test_multiple_events(self):
        raw = (
            f"Meeting A|||{SAMPLE_EPOCH_START}|||{SAMPLE_EPOCH_END}|||Office|||Cal1\n"
            f"Meeting B|||{SAMPLE_EPOCH_START + 300}|||{SAMPLE_EPOCH_END}|||Online|||Cal2\n"
        )
        events = _parse_osascript_output(raw)
        self.assertEqual(len(events), 2)

    def test_empty_location_and_calendar(self):
        raw = f"Quick|||{SAMPLE_EPOCH_START}|||{SAMPLE_EPOCH_END}||||||\n"  # 6 pipes = empty loc + empty cal
        events = _parse_osascript_output(raw)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "Quick")

    def test_invalid_epoch_skipped(self):
        raw = f"Bad|||notanint|||{SAMPLE_EPOCH_END}|||loc|||cal\n"
        events = _parse_osascript_output(raw)
        self.assertEqual(len(events), 0)


class TestFindActiveEventFound(_DarwinPatchedTestCase):
    def test_returns_event_dict(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = SAMPLE_RAW
        mock_proc.stderr = ""
        with patch("backend.calendar_link.subprocess.run", return_value=mock_proc):
            linker = CalendarLinker(cache_minutes=5)
            result = linker.find_active_event(at_time=datetime(2024, 4, 25, 10, 0))
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Stand-up")
        self.assertNotIn("_start_epoch", result)


class TestFindActiveEventNotFound(unittest.TestCase):
    def test_empty_output_returns_none(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = ""
        mock_proc.stderr = ""
        with patch("backend.calendar_link.subprocess.run", return_value=mock_proc):
            linker = CalendarLinker(cache_minutes=5)
            result = linker.find_active_event(at_time=datetime(2024, 4, 25, 10, 0))
        self.assertIsNone(result)


class TestFindActiveEventMultiple(_DarwinPatchedTestCase):
    def test_picks_earliest_start(self):
        epoch_a = SAMPLE_EPOCH_START
        epoch_b = SAMPLE_EPOCH_START + 600
        raw = (
            f"Meeting B|||{epoch_b}|||{SAMPLE_EPOCH_END}|||Online|||Cal\n"
            f"Meeting A|||{epoch_a}|||{SAMPLE_EPOCH_END}|||Office|||Cal\n"
        )
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = raw
        mock_proc.stderr = ""
        with patch("backend.calendar_link.subprocess.run", return_value=mock_proc):
            linker = CalendarLinker(cache_minutes=5)
            result = linker.find_active_event(at_time=datetime(2024, 4, 25, 10, 0))
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Meeting A")


class TestFindActiveEventPermissionDenied(unittest.TestCase):
    def test_not_authorized_returns_none(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "Not authorized to send Apple events to Calendar."
        with patch("backend.calendar_link.subprocess.run", return_value=mock_proc):
            linker = CalendarLinker(cache_minutes=5)
            result = linker.find_active_event(at_time=datetime(2024, 4, 25, 10, 0))
        self.assertIsNone(result)

    def test_timeout_returns_none(self):
        import subprocess
        with patch("backend.calendar_link.subprocess.run", side_effect=subprocess.TimeoutExpired("osascript", 10)):
            linker = CalendarLinker(cache_minutes=5)
            result = linker.find_active_event(at_time=datetime(2024, 4, 25, 10, 0))
        self.assertIsNone(result)


class TestCalendarLinkCacheHit(_DarwinPatchedTestCase):
    def test_second_call_uses_cache(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = SAMPLE_RAW
        mock_proc.stderr = ""
        with patch("backend.calendar_link.subprocess.run", return_value=mock_proc) as mock_run:
            linker = CalendarLinker(cache_minutes=5)
            at_time = datetime(2024, 4, 25, 10, 0)
            r1 = linker.find_active_event(at_time=at_time)
            r2 = linker.find_active_event(at_time=at_time)
        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(r1, r2)


class TestCalendarLinkCacheExpiry(_DarwinPatchedTestCase):
    def test_expired_cache_calls_again(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = SAMPLE_RAW
        mock_proc.stderr = ""
        with patch("backend.calendar_link.subprocess.run", return_value=mock_proc) as mock_run:
            linker = CalendarLinker(cache_minutes=5)
            at_time = datetime(2024, 4, 25, 10, 0)
            linker.find_active_event(at_time=at_time)
            # Simulate cache expiry. NB: time.monotonic() can be small после fresh
            # boot (~50s uptime), поэтому `0.0` может НЕ превысить cache_ttl_sec=300
            # и дать ложный cache hit. Используем большое отрицательное смещение —
            # гарантированный expiry независимо от uptime.
            linker._cache_at_time = -1e9
            linker.find_active_event(at_time=at_time)
        self.assertEqual(mock_run.call_count, 2)


class TestCalendarLinkNonMacOS(unittest.TestCase):
    def test_non_darwin_returns_none(self):
        with patch("backend.calendar_link.platform.system", return_value="Linux"):
            linker = CalendarLinker(cache_minutes=5)
            result = linker.find_active_event(at_time=datetime(2024, 4, 25, 10, 0))
        self.assertIsNone(result)


class TestCalendarLinkIPCHandlers(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.store = StateStore(data_dir=Path(self._tmpdir))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _add_item(self, text: str = "test") -> str:
        item = self.store.add_history_item(text=text)
        return item.id

    def test_update_and_get_calendar_link(self):
        item_id = self._add_item("прошло совещание")
        ok = self.store.update_history_item_calendar(item_id, SAMPLE_EVENT)
        self.assertTrue(ok)
        event = self.store.get_history_item_calendar(item_id)
        self.assertIsNotNone(event)
        self.assertEqual(event["title"], "Stand-up")

    def test_update_nonexistent_item_returns_false(self):
        ok = self.store.update_history_item_calendar("nonexistent-id", SAMPLE_EVENT)
        self.assertFalse(ok)

    def test_get_nonexistent_returns_none(self):
        result = self.store.get_history_item_calendar("nonexistent-id")
        self.assertIsNone(result)

    def test_update_empty_event_returns_false(self):
        item_id = self._add_item()
        ok = self.store.update_history_item_calendar(item_id, {})
        self.assertFalse(ok)

    def test_search_by_calendar_event_found(self):
        item_id = self._add_item("совещание")
        self.store.update_history_item_calendar(item_id, SAMPLE_EVENT)
        results = self.store.search_by_calendar_event("Stand")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["item_id"], item_id)

    def test_search_by_calendar_event_empty_query_returns_all(self):
        item_id_a = self._add_item("A")
        item_id_b = self._add_item("B")
        event_b = dict(SAMPLE_EVENT)
        event_b["title"] = "Another Meeting"
        self.store.update_history_item_calendar(item_id_a, SAMPLE_EVENT)
        self.store.update_history_item_calendar(item_id_b, event_b)
        results = self.store.search_by_calendar_event("")
        item_ids = {r["item_id"] for r in results}
        self.assertIn(item_id_a, item_ids)
        self.assertIn(item_id_b, item_ids)

    def test_search_no_match_returns_empty(self):
        item_id = self._add_item()
        self.store.update_history_item_calendar(item_id, SAMPLE_EVENT)
        results = self.store.search_by_calendar_event("xyzzy-impossible-match")
        self.assertEqual(results, [])

    def test_calendar_links_path_created(self):
        self.assertTrue(self.store.calendar_links_path.exists())

    def test_last_write_wins(self):
        item_id = self._add_item()
        self.store.update_history_item_calendar(item_id, SAMPLE_EVENT)
        new_event = dict(SAMPLE_EVENT)
        new_event["title"] = "Updated Event"
        self.store.update_history_item_calendar(item_id, new_event)
        event = self.store.get_history_item_calendar(item_id)
        self.assertEqual(event["title"], "Updated Event")


if __name__ == "__main__":
    unittest.main()
