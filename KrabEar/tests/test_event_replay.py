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
        # Missing params default to epoch=0 and now, which is a >7-day window
        # — handler returns {"ok": False, "reason": "time window too large"}.
        result = self.mgr.handle_replay_events({})
        self.assertFalse(result.get("ok", True))
        self.assertIn("reason", result)

    def test_handle_replay_events_valid(self):
        import time as _time
        from_ts = _time.time() - 5
        to_ts = _time.time() + 5
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

    def test_session_log_truncates_on_init(self):
        """Если файл уже существует, новая сессия усекает его (truncate), а не дописывает.

        W829 CRIT-1 fix: open("a") -> open("w"). Файл ограничен событиями текущей сессии.
        Предыдущее поведение приводило к неограниченному росту (~14 МБ/день, ~5 ГБ/год).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.ndjson"
            # Первая сессия — записывает "first"
            mgr1 = EventReplayManager(persist_path=path, max_buffer=100)
            mgr1.record_event("first", {})
            mgr1.close()
            self.assertEqual(len(path.read_text().splitlines()), 1)

            # Вторая сессия — должна усечь файл и записать только "second"
            mgr2 = EventReplayManager(persist_path=path, max_buffer=100)
            mgr2.record_event("second", {})
            mgr2.close()

            lines = path.read_text().splitlines()
            # Файл содержит ТОЛЬКО события текущей сессии (не накапливает прошлые)
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["type"], "second")


class TestShutdownIntegration(unittest.TestCase):
    """Тесты интеграции EventReplayManager с GracefulShutdownHandler (W969)."""

    def test_close_event_replay_called_at_shutdown(self):
        """GracefulShutdownHandler._close_event_replay вызывает close() на _event_replay."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "KrabEar"))
        from backend.shutdown_handler import GracefulShutdownHandler

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.ndjson"
            replay = EventReplayManager(persist_path=path, max_buffer=100)
            replay.record_event("before_shutdown", {"x": 1})

            # Сервис-заглушка с атрибутом _event_replay
            class FakeService:
                _event_replay = replay

            handler = GracefulShutdownHandler(data_dir=tmpdir)
            handler._close_event_replay(FakeService())

            # После вызова _close_event_replay файловый дескриптор закрыт
            self.assertIsNone(replay._file_handle)
            # Данные до закрытия были записаны
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["type"], "before_shutdown")

    def test_close_event_replay_no_attr_is_noop(self):
        """_close_event_replay не падает если у сервиса нет _event_replay."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "KrabEar"))
        from backend.shutdown_handler import GracefulShutdownHandler

        with tempfile.TemporaryDirectory() as tmpdir:
            handler = GracefulShutdownHandler(data_dir=tmpdir)

            class ServiceWithoutReplay:
                pass

            # Не должно бросить исключение
            handler._close_event_replay(ServiceWithoutReplay())


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
            # файл остаётся на диске (не удаляется), хотя содержимое усекается до ""
            self.assertTrue(path.exists())
            mgr.close()


class TestWave97RequiredCoverage(unittest.TestCase):
    """Wave 97 required tests — names match task spec exactly."""

    # test_persist_event_to_log
    def test_persist_event_to_log(self):
        """record_event writes a valid NDJSON line to the persist file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.ndjson"
            mgr = EventReplayManager(persist_path=path, max_buffer=100)
            mgr.record_event("stt.final", {"text": "hello world", "confidence": 0.97})
            mgr.close()

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry["type"], "stt.final")
            self.assertEqual(entry["data"]["text"], "hello world")
            self.assertIn("ts", entry)
            self.assertIn("seq", entry)

    # test_replay_full_log
    def test_replay_full_log(self):
        """replay_events with a wide range returns all buffered events in seq order."""
        mgr = EventReplayManager(max_buffer=100)
        event_types = ["stt.final", "stt.failed", "translation.done", "ping", "pong"]
        for et in event_types:
            mgr.record_event(et, {"et": et})

        from_ts = "2000-01-01T00:00:00+00:00"
        to_ts = "2100-01-01T00:00:00+00:00"
        events = mgr.replay_events(from_ts, to_ts)
        mgr.close()

        self.assertEqual(len(events), 5)
        returned_types = [e["type"] for e in events]
        for et in event_types:
            self.assertIn(et, returned_types)
        # Verify sequential order by seq field
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, sorted(seqs))

    # test_replay_time_range
    def test_replay_time_range(self):
        """replay_events returns only events within [from_ts, to_ts] inclusive."""
        mgr = EventReplayManager(max_buffer=100)
        now = datetime.now(timezone.utc)
        ts_old = (now - timedelta(seconds=120)).isoformat(timespec="seconds")
        ts_mid = (now - timedelta(seconds=60)).isoformat(timespec="seconds")
        ts_new = now.isoformat(timespec="seconds")

        with mgr._lock:
            mgr._buffer.append({"type": "old", "ts": ts_old, "data": {}, "seq": 1})
            mgr._buffer.append({"type": "mid", "ts": ts_mid, "data": {}, "seq": 2})
            mgr._buffer.append({"type": "new", "ts": ts_new, "data": {}, "seq": 3})

        # Range covers only mid and new
        from_ts = (now - timedelta(seconds=90)).isoformat(timespec="seconds")
        to_ts = ts_new
        events = mgr.replay_events(from_ts, to_ts)
        mgr.close()

        types = [e["type"] for e in events]
        self.assertNotIn("old", types)
        self.assertIn("mid", types)
        self.assertIn("new", types)

    # test_replay_filter_by_event_type
    def test_replay_filter_by_event_type(self):
        """get_events(event_type=...) returns only matching events."""
        mgr = EventReplayManager(max_buffer=100)
        mgr.record_event("stt.final", {"n": 1})
        mgr.record_event("stt.failed", {"n": 2})
        mgr.record_event("stt.final", {"n": 3})
        mgr.record_event("ping", {"n": 4})
        mgr.record_event("stt.final", {"n": 5})

        events = mgr.get_events(event_type="stt.final")
        mgr.close()

        self.assertEqual(len(events), 3)
        for ev in events:
            self.assertEqual(ev["type"], "stt.final")
        ns = [e["data"]["n"] for e in events]
        self.assertIn(1, ns)
        self.assertIn(3, ns)
        self.assertIn(5, ns)

    # test_log_rotation_when_too_large — ring buffer evicts oldest (in-memory rotation)
    def test_log_rotation_when_too_large(self):
        """When buffer is full, oldest events are evicted (ring-buffer rotation)."""
        capacity = 10
        mgr = EventReplayManager(max_buffer=capacity)
        # Write 3× more events than capacity
        for i in range(30):
            mgr.record_event("ev", {"i": i})

        stats = mgr.get_event_stats()
        mgr.close()

        # Buffer never exceeds capacity
        self.assertEqual(stats["total_events"], capacity)
        # The buffer_capacity field should reflect the configured max
        self.assertEqual(stats["buffer_capacity"], capacity)

    # test_corrupted_log_line_skipped
    def test_corrupted_log_line_skipped(self):
        """Events with unparseable timestamps are silently skipped during replay/filter."""
        mgr = EventReplayManager(max_buffer=100)
        now = datetime.now(timezone.utc)
        good_ts = now.isoformat(timespec="seconds")
        bad_ts = "NOT-A-TIMESTAMP"

        with mgr._lock:
            mgr._buffer.append({"type": "good", "ts": good_ts, "data": {}, "seq": 1})
            mgr._buffer.append({"type": "corrupted", "ts": bad_ts, "data": {}, "seq": 2})
            mgr._buffer.append({"type": "good2", "ts": good_ts, "data": {}, "seq": 3})

        from_ts = (now - timedelta(seconds=5)).isoformat(timespec="seconds")
        to_ts = (now + timedelta(seconds=5)).isoformat(timespec="seconds")
        events = mgr.replay_events(from_ts, to_ts)
        mgr.close()

        types = [e["type"] for e in events]
        self.assertIn("good", types)
        self.assertIn("good2", types)
        self.assertNotIn("corrupted", types)

    # test_concurrent_persist_replay
    def test_concurrent_persist_replay(self):
        """Concurrent record_event and get_events do not raise or corrupt data."""
        mgr = EventReplayManager(max_buffer=5000)
        errors = []
        result_counts = []

        def writer():
            try:
                for i in range(100):
                    mgr.record_event("w", {"i": i})
            except Exception as exc:
                errors.append(("writer", exc))

        def reader():
            try:
                for _ in range(20):
                    evs = mgr.get_events(limit=50)
                    result_counts.append(len(evs))
            except Exception as exc:
                errors.append(("reader", exc))

        threads = (
            [threading.Thread(target=writer) for _ in range(4)]
            + [threading.Thread(target=reader) for _ in range(4)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        mgr.close()

        self.assertEqual(errors, [], f"Unexpected exceptions: {errors}")
        # At least some reads returned results
        self.assertTrue(any(c > 0 for c in result_counts))
        # All counts are non-negative
        self.assertTrue(all(c >= 0 for c in result_counts))


class TestPrivacyModeGuard(unittest.TestCase):
    """W968 F4 — privacy_mode_enabled redacts event payload in record_event."""

    def test_record_event_redacts_in_privacy_mode(self):
        """В режиме конфиденциальности data заменяется заглушкой {redacted: True}."""
        def privacy_provider():
            return {"privacy_mode_enabled": True}

        mgr = EventReplayManager(max_buffer=100, settings_provider=privacy_provider)
        mgr.record_event("stt.final", {"text": "секрет", "confidence": 0.99})
        events = mgr.get_events()
        mgr.close()

        self.assertEqual(len(events), 1)
        data = events[0]["data"]
        self.assertTrue(data.get("redacted"), "data.redacted must be True in privacy mode")
        self.assertEqual(data.get("reason"), "privacy_mode")
        self.assertNotIn("text", data, "transcript text must not appear in privacy mode")
        self.assertNotIn("confidence", data, "confidence must not appear in privacy mode")

    def test_record_event_passes_data_when_privacy_disabled(self):
        """При privacy_mode_enabled=False данные пишутся без изменений."""
        def normal_provider():
            return {"privacy_mode_enabled": False}

        mgr = EventReplayManager(max_buffer=100, settings_provider=normal_provider)
        mgr.record_event("stt.final", {"text": "привет", "confidence": 0.95})
        events = mgr.get_events()
        mgr.close()

        self.assertEqual(len(events), 1)
        data = events[0]["data"]
        self.assertNotIn("redacted", data)
        self.assertEqual(data.get("text"), "привет")

    def test_record_event_no_settings_provider_passes_data(self):
        """Без settings_provider (None) данные пишутся без изменений."""
        mgr = EventReplayManager(max_buffer=100)
        mgr.record_event("ping", {"key": "value"})
        events = mgr.get_events()
        mgr.close()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"].get("key"), "value")

    def test_privacy_mode_guard_persisted_file_also_redacted(self):
        """В режиме конфиденциальности persisted NDJSON тоже содержит заглушку."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.ndjson"
            mgr = EventReplayManager(
                persist_path=path,
                max_buffer=100,
                settings_provider=lambda: {"privacy_mode_enabled": True},
            )
            mgr.record_event("stt.final", {"text": "конфиденциально"})
            mgr.close()

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertTrue(entry["data"].get("redacted"))
            self.assertNotIn("text", entry["data"])

    def test_settings_provider_exception_falls_back_to_no_redaction(self):
        """Если settings_provider бросает исключение, redaction не применяется."""
        def broken_provider():
            raise RuntimeError("Settings unavailable")

        mgr = EventReplayManager(max_buffer=100, settings_provider=broken_provider)
        mgr.record_event("ping", {"key": "value"})
        events = mgr.get_events()
        mgr.close()

        self.assertEqual(len(events), 1)
        # No redaction on provider failure — safe fallback
        self.assertEqual(events[0]["data"].get("key"), "value")


class TestW1444LimitValidationAndClearTruncate(unittest.TestCase):
    """W1444 — event_replay limit validation (F2 MED) + clear truncates file (F3 LOW)."""

    # F2 MED: non-integer limit returns structured error
    def test_get_event_log_invalid_limit_returns_error(self):
        """handle_get_event_log with non-integer limit returns structured IPC error."""
        mgr = EventReplayManager(max_buffer=100)
        mgr.record_event("ping", {})
        result = mgr.handle_get_event_log({"limit": "not-a-number"})
        mgr.close()

        self.assertFalse(result.get("ok"), "ok must be False on invalid limit")
        self.assertIn("error", result)
        self.assertIn("limit", result["error"])

    # F2 MED: out-of-range limit is clamped (not an error)
    def test_get_event_log_negative_limit_clamps(self):
        """handle_get_event_log with negative limit clamps to 1 (no error)."""
        mgr = EventReplayManager(max_buffer=100)
        for _ in range(5):
            mgr.record_event("ev", {})
        result = mgr.handle_get_event_log({"limit": -10})
        mgr.close()

        self.assertIn("events", result, "result must have 'events' key on clamped limit")
        self.assertGreaterEqual(result["count"], 1)

    # F3 LOW: clear() truncates persist file
    def test_clear_truncates_persist_file(self):
        """clear() empties both in-memory buffer and persist file on disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.ndjson"
            mgr = EventReplayManager(persist_path=path, max_buffer=50)
            mgr.record_event("before_clear", {"x": 1})
            mgr.record_event("before_clear", {"x": 2})
            # File has content before clear
            self.assertGreater(path.stat().st_size, 0)

            mgr.clear()

            # Buffer is empty
            self.assertEqual(mgr.get_event_stats()["total_events"], 0)
            # File is truncated (empty) after clear
            self.assertEqual(path.read_text(encoding="utf-8"), "")
            mgr.close()


class TestW1770FileBoundedAndClear(unittest.TestCase):
    """W1770 — файл event_replay.ndjson не растёт без границ внутри сессии,
    clear() обнуляет и файл, и кольцо, replay работает после пересборки.

    FINDING: record_event делал append на каждое событие; W829 truncate-on-restart
    ограничивал рост только МЕЖДУ перезапусками, а внутри длительной сессии
    (launchd backend живёт сутками) файл рос неограниченно. FIX: байтовый предел
    + атомарная пересборка из кольцевого буфера.
    """

    def test_file_bounded_when_many_events_past_cap(self):
        """Запись событий сильно сверх предела держит файл ограниченным по размеру,
        и в нём остаются САМЫЕ СВЕЖИЕ события. Старый код (без предела) рос бы
        линейно по числу событий.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.ndjson"
            # Кольцо мало (20) — полный дамп кольца (~2.6 КБ) заведомо влезает в
            # cap=6000 байт. Без предела 500 событий дали бы ~66 КБ.
            cap = 6000
            mgr = EventReplayManager(persist_path=path, max_buffer=20, max_file_bytes=cap)
            try:
                total = 500
                for i in range(total):
                    mgr.record_event("ev", {"i": i, "pad": "x" * 40})
                mgr._file_handle.flush()

                size = path.stat().st_size
                # Файл ограничен предложенным пределом (а НЕ ~total*line_bytes).
                self.assertLessEqual(
                    size, cap,
                    f"file grew past cap: {size} > {cap} (unbounded-growth regression)",
                )

                # Файл всё ещё содержит самые свежие события (последний i=499).
                lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
                self.assertTrue(lines, "persist file unexpectedly empty after compaction")
                parsed = [json.loads(ln) for ln in lines]
                last_is = [e["data"]["i"] for e in parsed]
                self.assertIn(total - 1, last_is, "most-recent event missing from bounded file")
                # И самые старые события вытеснены (i=0 не должен оставаться).
                self.assertNotIn(0, last_is, "oldest event should have been evicted")
            finally:
                mgr.close()

    def test_clear_empties_file_and_ring(self):
        """clear() обнуляет и файл на диске, и in-memory кольцо; последующая запись
        ложится в начало файла (без «дыры» от устаревшего смещения дескриптора).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.ndjson"
            mgr = EventReplayManager(persist_path=path, max_buffer=50)
            try:
                for i in range(20):
                    mgr.record_event("secret", {"text": f"cleartext-{i}"})
                self.assertGreater(path.stat().st_size, 0)
                self.assertEqual(mgr.get_event_stats()["total_events"], 20)

                mgr.clear()

                # Кольцо пусто.
                self.assertEqual(mgr.get_event_stats()["total_events"], 0)
                # Файл обнулён (но существует — purge усекает, не удаляет).
                self.assertTrue(path.exists())
                self.assertEqual(path.read_text(encoding="utf-8"), "")

                # Регрессия dangling-offset: запись после clear ложится с offset 0,
                # т.е. файл = ровно одна строка, без NUL-«дыры» впереди.
                mgr.record_event("after_clear", {"x": 1})
                mgr._file_handle.flush()
                content = path.read_text(encoding="utf-8")
                self.assertNotIn("\x00", content, "sparse NUL hole after clear (dangling offset)")
                lines = [ln for ln in content.splitlines() if ln]
                self.assertEqual(len(lines), 1)
                self.assertEqual(json.loads(lines[0])["type"], "after_clear")
            finally:
                mgr.close()

    def test_replay_and_get_event_log_work_after_compaction(self):
        """replay_events / handle_get_event_log корректны после того, как файл был
        пересобран из кольца (компакция не ломает чтение из in-memory буфера).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.ndjson"
            cap = 4000
            mgr = EventReplayManager(persist_path=path, max_buffer=20, max_file_bytes=cap)
            try:
                # Достаточно событий, чтобы пройти cap минимум раз → compaction.
                for i in range(300):
                    mgr.record_event("ev", {"i": i, "pad": "y" * 40})

                # Компакция действительно произошла: файл ограничен.
                self.assertLessEqual(path.stat().st_size, cap)

                # replay_events широким диапазоном возвращает текущее кольцо (20).
                events = mgr.replay_events("2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00")
                self.assertEqual(len(events), 20)
                seqs = [e["seq"] for e in events]
                self.assertEqual(seqs, sorted(seqs))
                # Свежайшее событие на месте.
                self.assertIn(299, [e["data"]["i"] for e in events])

                # handle_get_event_log тоже работает и не падает.
                res = mgr.handle_get_event_log({"limit": 100})
                self.assertIn("events", res)
                self.assertEqual(res["count"], 20)

                # Статистика согласована с кольцом.
                self.assertEqual(mgr.get_event_stats()["total_events"], 20)
            finally:
                mgr.close()

    def test_default_cap_does_not_compact_in_normal_use(self):
        """С дефолтным пределом (8 МБ) обычная сессия не вызывает пересборку:
        несколько событий просто дописываются, файл = их число строк.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.ndjson"
            mgr = EventReplayManager(persist_path=path, max_buffer=100)
            try:
                for i in range(10):
                    mgr.record_event("ev", {"i": i})
                mgr._file_handle.flush()
                lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
                self.assertEqual(len(lines), 10)
            finally:
                mgr.close()


if __name__ == "__main__":
    unittest.main()
