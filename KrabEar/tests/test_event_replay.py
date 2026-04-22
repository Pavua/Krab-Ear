"""Тесты для EventReplayManager (KrabEar/backend/event_replay.py)."""

from __future__ import annotations
from backend.event_replay import EventReplayManager, _parse_ts
import unittest

import json
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Настройка путей для запуска как standalone
PROJECT_ROOT = Path(__file__).resolve().parents[2]
KRABEAR_ROOT = PROJECT_ROOT / "KrabEar"
for p in (str(PROJECT_ROOT), str(KRABEAR_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _ts(offset_sec: int = 0) -> str:
    """Возвращает ISO 8601 UTC timestamp со смещением от текущего времени."""
    dt = datetime.now(timezone.utc) + timedelta(seconds=offset_sec)
    return dt.isoformat(timespec="seconds")


class TestParseTs(unittest.TestCase):
    def test_valid_utc(self):
        ts = "2024-01-01T12:00:00+00:00"
        dt = _parse_ts(ts)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_naive_becomes_utc(self):
        ts = "2024-01-01T12:00:00"
        dt = _parse_ts(ts)
        self.assertIsNotNone(dt.tzinfo)

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            _parse_ts("not-a-date")


class TestRecordAndGet(unittest.TestCase):
    def setUp(self):
        self.mgr = EventReplayManager(max_buffer=100)

    def tearDown(self):
        self.mgr.close()

    def test_record_and_retrieve(self):
        self.mgr.record_event("stt.final", {"text": "hello"})
        events = self.mgr.get_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "stt.final")
        self.assertEqual(events[0]["data"]["text"], "hello")

    def test_event_has_required_fields(self):
        self.mgr.record_event("test.event", {"key": "val"})
        ev = self.mgr.get_events()[0]
        self.assertIn("type", ev)
        self.assertIn("ts", ev)
        self.assertIn("data", ev)
        self.assertIn("seq", ev)

    def test_limit_respected(self):
        for i in range(20):
            self.mgr.record_event("ping", {"i": i})
        events = self.mgr.get_events(limit=5)
        self.assertEqual(len(events), 5)

    def test_filter_by_event_type(self):
        self.mgr.record_event("stt.final", {"text": "a"})
        self.mgr.record_event("stt.failed", {"error": "e"})
        self.mgr.record_event("stt.final", {"text": "b"})
        events = self.mgr.get_events(event_type="stt.final")
        self.assertEqual(len(events), 2)
        for ev in events:
            self.assertEqual(ev["type"], "stt.final")

    def test_filter_since(self):
        # Записываем два события с искусственным смещением через прямую вставку
        mgr = self.mgr
        past_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(timespec="seconds")
        future_ts = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat(timespec="seconds")

        # Вставляем напрямую в буфер для точности timestamps
        with mgr._lock:
            mgr._buffer.append({"type": "a", "ts": past_ts, "data": {}, "seq": 1})
            mgr._buffer.append({"type": "b", "ts": future_ts, "data": {}, "seq": 2})

        cutoff = datetime.now(timezone.utc).isoformat(timespec="seconds")
        events = mgr.get_events(since=cutoff)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "b")

    def test_seq_monotonically_increases(self):
        for _ in range(5):
            self.mgr.record_event("x", {})
        seqs = [ev["seq"] for ev in self.mgr.get_events()]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))  # все уникальны


class TestReplayEvents(unittest.TestCase):
    def setUp(self):
        self.mgr = EventReplayManager(max_buffer=100)

    def tearDown(self):
        self.mgr.close()

    def test_replay_returns_events_in_range(self):
        now = datetime.now(timezone.utc)
        ts_before = (now - timedelta(seconds=30)).isoformat(timespec="seconds")
        ts_middle = now.isoformat(timespec="seconds")
        ts_after = (now + timedelta(seconds=30)).isoformat(timespec="seconds")

        with self.mgr._lock:
            self.mgr._buffer.append({"type": "a", "ts": ts_before, "data": {}, "seq": 1})
            self.mgr._buffer.append({"type": "b", "ts": ts_middle, "data": {}, "seq": 2})
            self.mgr._buffer.append({"type": "c", "ts": ts_after, "data": {}, "seq": 3})

        from_ts = (now - timedelta(seconds=35)).isoformat(timespec="seconds")
        to_ts = ts_middle
        events = self.mgr.replay_events(from_ts, to_ts)
        types = [e["type"] for e in events]
        self.assertIn("a", types)
        self.assertIn("b", types)
        self.assertNotIn("c", types)

    def test_replay_sorted_by_seq(self):
        now = datetime.now(timezone.utc)
        # Вставляем в обратном порядке seq
        with self.mgr._lock:
            ts = now.isoformat(timespec="seconds")
            self.mgr._buffer.append({"type": "z", "ts": ts, "data": {}, "seq": 3})
            self.mgr._buffer.append({"type": "a", "ts": ts, "data": {}, "seq": 1})
            self.mgr._buffer.append({"type": "m", "ts": ts, "data": {}, "seq": 2})

        from_ts = (now - timedelta(seconds=1)).isoformat(timespec="seconds")
        to_ts = (now + timedelta(seconds=1)).isoformat(timespec="seconds")
        events = self.mgr.replay_events(from_ts, to_ts)
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, sorted(seqs))

    def test_replay_invalid_ts_raises(self):
        with self.assertRaises(ValueError):
            self.mgr.replay_events("bad-ts", "also-bad")


class TestEventStats(unittest.TestCase):
    def setUp(self):
        self.mgr = EventReplayManager(max_buffer=100)

    def tearDown(self):
        self.mgr.close()

    def test_stats_empty(self):
        stats = self.mgr.get_event_stats()
        self.assertEqual(stats["total_events"], 0)
        self.assertIsInstance(stats["counts_by_type"], dict)
        self.assertIn("buffer_capacity", stats)

    def test_stats_counts(self):
        self.mgr.record_event("stt.final", {})
        self.mgr.record_event("stt.final", {})
        self.mgr.record_event("stt.failed", {})
        stats = self.mgr.get_event_stats()
        self.assertEqual(stats["total_events"], 3)
        self.assertEqual(stats["counts_by_type"]["stt.final"], 2)
        self.assertEqual(stats["counts_by_type"]["stt.failed"], 1)

    def test_stats_rate_includes_recent(self):
        self.mgr.record_event("ping", {})
        stats = self.mgr.get_event_stats()
        # Событие было только что, должно попасть в rate_per_minute
        self.assertIn("ping", stats["rate_per_minute_by_type"])


class TestRingBuffer(unittest.TestCase):
    def test_ring_buffer_evicts_oldest(self):
        mgr = EventReplayManager(max_buffer=5)
        for i in range(10):
            mgr.record_event("e", {"i": i})
        events = mgr.get_events(limit=100)
        self.assertEqual(len(events), 5)
        # Последние 5 — i=5..9
        vals = [e["data"]["i"] for e in events]
        self.assertEqual(vals, [5, 6, 7, 8, 9])
        mgr.close()


class TestPersistence(unittest.TestCase):
    def test_writes_ndjson_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "replay.ndjson"
            mgr = EventReplayManager(persist_path=path, max_buffer=100)
            mgr.record_event("stt.final", {"text": "test"})
            mgr.record_event("stt.failed", {"error": "oops"})
            mgr.close()

            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            first = json.loads(lines[0])
            self.assertEqual(first["type"], "stt.final")
            second = json.loads(lines[1])
            self.assertEqual(second["type"], "stt.failed")


class TestThreadSafety(unittest.TestCase):
    def test_concurrent_record(self):
        mgr = EventReplayManager(max_buffer=5000)
        errors = []

        def worker(n):
            try:
                for _ in range(50):
                    mgr.record_event("concurrent", {"n": n})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        stats = mgr.get_event_stats()
        self.assertEqual(stats["total_events"], 500)
        mgr.close()


class TestIPCHandlers(unittest.TestCase):
    def setUp(self):
        self.mgr = EventReplayManager(max_buffer=100)
        for i in range(3):
            self.mgr.record_event("stt.final", {"i": i})
        self.mgr.record_event("stt.failed", {"error": "e"})

    def tearDown(self):
        self.mgr.close()

    def test_handle_get_event_log_no_filter(self):
        result = self.mgr.handle_get_event_log({})
        self.assertEqual(result["count"], 4)
        self.assertIsInstance(result["events"], list)

    def test_handle_get_event_log_type_filter(self):
        result = self.mgr.handle_get_event_log({"event_type": "stt.failed"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["events"][0]["type"], "stt.failed")

    def test_handle_get_event_stats(self):
        result = self.mgr.handle_get_event_stats({})
        self.assertEqual(result["total_events"], 4)
        self.assertEqual(result["counts_by_type"]["stt.final"], 3)

    def test_handle_replay_events_missing_params(self):
        with self.assertRaises(ValueError):
            self.mgr.handle_replay_events({})

    def test_handle_replay_events_valid(self):
        from_ts = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="seconds")
        to_ts = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(timespec="seconds")
        result = self.mgr.handle_replay_events({"from_ts": from_ts, "to_ts": to_ts})
        self.assertGreaterEqual(result["count"], 4)


class TestReplayAll(unittest.TestCase):
    """replay_events с диапазоном, охватывающим все события — эквивалент replay_all."""

    def setUp(self):
        self.mgr = EventReplayManager(max_buffer=100)

    def tearDown(self):
        self.mgr.close()

    def test_replay_all_returns_all_events(self):
        """replay_events с очень широким диапазоном возвращает все события."""
        for i in range(5):
            self.mgr.record_event("ev", {"i": i})
        from_ts = "2000-01-01T00:00:00+00:00"
        to_ts = "2100-01-01T00:00:00+00:00"
        events = self.mgr.replay_events(from_ts, to_ts)
        self.assertEqual(len(events), 5)

    def test_replay_empty_range_returns_empty(self):
        """replay_events с from_ts > to_ts возвращает пустой список."""
        self.mgr.record_event("ev", {})
        now = datetime.now(timezone.utc)
        from_ts = (now + timedelta(seconds=60)).isoformat(timespec="seconds")
        to_ts = (now + timedelta(seconds=120)).isoformat(timespec="seconds")
        events = self.mgr.replay_events(from_ts, to_ts)
        self.assertEqual(events, [])

    def test_replay_no_events_returns_empty(self):
        """replay_events на пустом буфере всегда возвращает []."""
        from_ts = "2000-01-01T00:00:00+00:00"
        to_ts = "2100-01-01T00:00:00+00:00"
        events = self.mgr.replay_events(from_ts, to_ts)
        self.assertEqual(events, [])


class TestPersistenceReload(unittest.TestCase):
    """Персистенция: перезагрузка из файла сохраняет события."""

    def test_reload_from_ndjson(self):
        """Файл NDJSON с persist_path можно прочитать независимо от экземпляра."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.ndjson"
            mgr = EventReplayManager(persist_path=path, max_buffer=100)
            mgr.record_event("alpha", {"x": 1})
            mgr.record_event("beta", {"x": 2})
            mgr.close()

            # Новый экземпляр не загружает с диска (дизайн — in-memory),
            # но файл содержит оба события.
            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["type"], "alpha")
            self.assertEqual(json.loads(lines[1])["type"], "beta")

    def test_persist_path_parent_created(self):
        """persist_path создаёт родительскую директорию если не существует."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = Path(tmpdir) / "sub" / "dir" / "events.ndjson"
            mgr = EventReplayManager(persist_path=nested, max_buffer=10)
            mgr.record_event("test", {})
            mgr.close()
            self.assertTrue(nested.exists())
            self.assertEqual(len(nested.read_text().splitlines()), 1)

    def test_append_to_existing_file(self):
        """Если файл уже существует, события дописываются (append), а не перезаписываются."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.ndjson"
            # Первая сессия
            mgr1 = EventReplayManager(persist_path=path, max_buffer=100)
            mgr1.record_event("first", {})
            mgr1.close()
            # Вторая сессия
            mgr2 = EventReplayManager(persist_path=path, max_buffer=100)
            mgr2.record_event("second", {})
            mgr2.close()

            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            types = [json.loads(line)["type"] for line in lines]
            self.assertIn("first", types)
            self.assertIn("second", types)


class TestClearBuffer(unittest.TestCase):
    def test_clear_empties_buffer(self):
        mgr = EventReplayManager(max_buffer=50)
        for _ in range(10):
            mgr.record_event("x", {})
        mgr.clear()
        self.assertEqual(mgr.get_event_stats()["total_events"], 0)
        mgr.close()

    def test_clear_does_not_remove_persist_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "ev.ndjson"
            mgr = EventReplayManager(persist_path=path, max_buffer=50)
            mgr.record_event("x", {})
            mgr.clear()
            # файл остаётся, т.к. clear() не трогает диск
            self.assertTrue(path.exists())
            mgr.close()


if __name__ == "__main__":
    unittest.main()
