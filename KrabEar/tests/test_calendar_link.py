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


class TestFindOverlappingEvent(_DarwinPatchedTestCase):
    """test_find_overlapping_event — event returned when recording overlaps."""

    def test_find_overlapping_event(self):
        """find_active_event returns event whose window overlaps at_time."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = SAMPLE_RAW
        mock_proc.stderr = ""
        with patch("backend.calendar_link.subprocess.run", return_value=mock_proc):
            linker = CalendarLinker(cache_minutes=1)
            result = linker.find_active_event(at_time=datetime(2024, 4, 25, 9, 0))
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Stand-up")

    def test_no_overlap_returns_none(self):
        """When osascript returns empty, no event is found."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = ""
        mock_proc.stderr = ""
        with patch("backend.calendar_link.subprocess.run", return_value=mock_proc):
            linker = CalendarLinker(cache_minutes=1)
            result = linker.find_active_event(at_time=datetime(2024, 4, 25, 9, 0))
        self.assertIsNone(result)


class TestUnicodeEventTitle(_DarwinPatchedTestCase):
    """test_unicode_event_title — titles with RU/ES/emoji parse correctly."""

    def test_unicode_event_title(self):
        """Titles containing Cyrillic, accented chars, and emoji are parsed."""
        epoch_s = SAMPLE_EPOCH_START
        epoch_e = SAMPLE_EPOCH_END
        unicode_titles = [
            "Совещание 🎉|||{}|||{}|||Офис|||Работа".format(epoch_s, epoch_e),
            "Reunión de equipo|||{}|||{}|||Sala B|||Personal".format(epoch_s, epoch_e),
            "会议 (CJK)|||{}|||{}|||Online|||Work".format(epoch_s, epoch_e),
        ]
        for raw_line in unicode_titles:
            with self.subTest(line=raw_line[:30]):
                mock_proc = MagicMock()
                mock_proc.returncode = 0
                mock_proc.stdout = raw_line + "\n"
                mock_proc.stderr = ""
                with patch("backend.calendar_link.subprocess.run", return_value=mock_proc):
                    linker = CalendarLinker(cache_minutes=1)
                    result = linker.find_active_event(at_time=datetime(2024, 4, 25, 9, 0))
                self.assertIsNotNone(result)
                self.assertTrue(len(result["title"]) > 0)


class TestHandlesOsascriptErrorGracefully(unittest.TestCase):
    """test_handles_osascript_error_gracefully — FileNotFoundError, generic exc."""

    def test_handles_osascript_error_gracefully(self):
        """Generic exception from subprocess.run → returns None."""
        with patch(
            "backend.calendar_link.subprocess.run",
            side_effect=OSError("unexpected OS error"),
        ):
            with patch("backend.calendar_link.platform.system", return_value="Darwin"):
                linker = CalendarLinker(cache_minutes=1)
                result = linker.find_active_event(at_time=datetime(2024, 4, 25, 9, 0))
        self.assertIsNone(result)

    def test_file_not_found_returns_none(self):
        """Missing osascript binary → graceful None."""
        with patch(
            "backend.calendar_link.subprocess.run",
            side_effect=FileNotFoundError("osascript not found"),
        ):
            with patch("backend.calendar_link.platform.system", return_value="Darwin"):
                linker = CalendarLinker(cache_minutes=1)
                result = linker.find_active_event(at_time=datetime(2024, 4, 25, 9, 0))
        self.assertIsNone(result)


class TestHandlesCalendarAppNotRunning(unittest.TestCase):
    """test_handles_calendar_app_not_running — non-zero rc + empty stdout."""

    def test_handles_calendar_app_not_running(self):
        """Non-zero rc with empty stdout (Calendar not running) → None."""
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "Calendar is not running."
        with patch("backend.calendar_link.subprocess.run", return_value=mock_proc):
            with patch("backend.calendar_link.platform.system", return_value="Darwin"):
                linker = CalendarLinker(cache_minutes=1)
                result = linker.find_active_event(at_time=datetime(2024, 4, 25, 9, 0))
        self.assertIsNone(result)


class TestConcurrentLink(_DarwinPatchedTestCase):
    """test_concurrent_link — thread-safe concurrent calls."""

    def test_concurrent_link(self):
        """Multiple threads calling find_active_event concurrently return consistent results."""
        import threading

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = SAMPLE_RAW
        mock_proc.stderr = ""
        results = []
        errors = []

        def call_linker(linker, at_time):
            try:
                results.append(linker.find_active_event(at_time=at_time))
            except Exception as e:
                errors.append(e)

        with patch("backend.calendar_link.subprocess.run", return_value=mock_proc):
            linker = CalendarLinker(cache_minutes=5)
            at_time = datetime(2024, 4, 25, 10, 0)
            threads = [threading.Thread(target=call_linker, args=(linker, at_time)) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 10)
        # All results must be either equal to the sample event or None
        for r in results:
            if r is not None:
                self.assertEqual(r["title"], "Stand-up")


class TestLinkReturnsEventId(_DarwinPatchedTestCase):
    """test_link_returns_event_id — returned dict contains expected keys."""

    def test_link_returns_event_id(self):
        """Returned event dict contains title, start_iso, end_iso, calendar_name keys."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = SAMPLE_RAW
        mock_proc.stderr = ""
        with patch("backend.calendar_link.subprocess.run", return_value=mock_proc):
            linker = CalendarLinker(cache_minutes=1)
            result = linker.find_active_event(at_time=datetime(2024, 4, 25, 9, 0))
        self.assertIsNotNone(result)
        for key in ("title", "start_iso", "end_iso", "calendar_name"):
            self.assertIn(key, result)
        # Internal _start_epoch must be stripped from result
        self.assertNotIn("_start_epoch", result)


class TestCalendarIPCDispatchWiringW1030(unittest.TestCase):
    """W1030: Verify link_to_calendar_event / get_calendar_link / search_by_calendar_event
    are actually in the service.py dispatch table (W1028 F1 CRITICAL regression guard).

    W947 claimed to wire these handlers but only added StateStore methods.
    W1030 completes the IPC layer. This test is a permanent regression guard.
    """

    REQUIRED_IPC_KEYS = [
        "link_to_calendar_event",
        "get_calendar_link",
        "search_by_calendar_event",
    ]
    REQUIRED_HANDLER_DEFS = [
        "_handle_link_to_calendar_event",
        "_handle_get_calendar_link",
        "_handle_search_by_calendar_event",
    ]

    def _read_service_source(self) -> str:
        service_path = Path(__file__).resolve().parent.parent / "backend" / "service.py"
        with open(service_path, encoding="utf-8") as f:
            return f.read()

    def test_dispatch_keys_present_in_service(self):
        """All 3 calendar IPC dispatch keys must appear in service.py dispatch table."""
        source = self._read_service_source()
        for key in self.REQUIRED_IPC_KEYS:
            self.assertIn(
                f'"{key}"',
                source,
                f'Dispatch key "{key}" missing from service.py — W1030 regression!',
            )

    def test_handler_methods_defined_in_service(self):
        """All 3 calendar _handle_* methods must be defined in service.py."""
        source = self._read_service_source()
        for handler in self.REQUIRED_HANDLER_DEFS:
            self.assertIn(
                f"def {handler}",
                source,
                f"Handler method {handler} missing from service.py — W1030 regression!",
            )

    def test_dispatch_keys_in_dispatch_block(self):
        """All 3 dispatch keys must appear inside the handlers dict block (not just comments)."""
        import re
        source = self._read_service_source()
        start = source.index("handlers: dict[str, Callable")
        end = source.index("\n        handler = handlers.get(method)")
        dispatch_block = source[start:end]
        for key in self.REQUIRED_IPC_KEYS:
            self.assertIn(
                f'"{key}"',
                dispatch_block,
                f'"{key}" not in dispatch block — W1030 regression!',
            )


if __name__ == "__main__":
    unittest.main()
