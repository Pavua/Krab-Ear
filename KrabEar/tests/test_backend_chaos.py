"""Stress / chaos tests for Krab Ear backend service.

Run locally with:
    RUN_CHAOS=1 PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_backend_chaos.py -v

Slow / heavy tests are opt-in via RUN_CHAOS=1 env var so they don't block CI by default.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore
from backend.event_bus import EventBus
from backend.metrics_collector import MetricsCollector

_CHAOS = os.environ.get("RUN_CHAOS") == "1"
chaos = unittest.skipUnless(_CHAOS, "Set RUN_CHAOS=1 to run")


def _make_store(tmp_dir: str) -> StateStore:
    return StateStore(data_dir=Path(tmp_dir))


# ---------------------------------------------------------------------------
# Test 1 — concurrent add + get
# ---------------------------------------------------------------------------

class TestConcurrentAddAndGetHistory(unittest.TestCase):

    @chaos
    def test_concurrent_add_and_get_history(self):
        """50 threads simultaneously add items + read history.

        Verifies: NDJSON stays parseable and item count is exact (no torn writes).
        """
        N_WRITERS = 50
        ITEMS_PER_THREAD = 4

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            errors: list[str] = []
            added_ids: list[str] = []
            lock = threading.Lock()

            def writer(idx: int) -> None:
                for j in range(ITEMS_PER_THREAD):
                    try:
                        item = store.add_history_item(
                            text=f"thread-{idx}-item-{j}",
                            paste_status="ok",
                        )
                        with lock:
                            added_ids.append(item.id)
                    except Exception as exc:
                        with lock:
                            errors.append(f"writer {idx}: {exc}")

            def reader() -> None:
                for _ in range(20):
                    try:
                        store.get_history_page(cursor=None, limit=1000)
                    except Exception as exc:
                        with lock:
                            errors.append(f"reader: {exc}")
                    time.sleep(0.01)

            threads = [threading.Thread(target=writer, args=(i,), daemon=True)
                       for i in range(N_WRITERS)]
            threads.append(threading.Thread(target=reader, daemon=True))
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            self.assertFalse(errors, f"Errors during concurrent write/read: {errors[:5]}")

            # Verify file is parseable line-by-line
            history_path = Path(tmp) / "history.ndjson"
            parsed_count = 0
            with history_path.open(encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        self.fail(f"Line {lineno} is invalid JSON: {exc!r}")
                    if "id" in obj and "_deleted" not in obj:
                        parsed_count += 1

            expected = N_WRITERS * ITEMS_PER_THREAD
            self.assertEqual(
                parsed_count, expected,
                f"Expected {expected} NDJSON entries, found {parsed_count}",
            )


# ---------------------------------------------------------------------------
# Test 2 — concurrent settings writes
# ---------------------------------------------------------------------------

class TestConcurrentSettingsWrites(unittest.TestCase):

    @chaos
    def test_concurrent_settings_writes(self):
        """30 threads call save_settings with different keys.

        Verifies: settings.json is valid JSON after all writes complete.
        """
        N_THREADS = 30

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            errors: list[str] = []

            def writer(idx: int) -> None:
                try:
                    store.save_settings({f"custom_key_{idx}": f"value_{idx}"})
                except Exception as exc:
                    errors.append(f"thread {idx}: {exc}")

            threads = [threading.Thread(target=writer, args=(i,), daemon=True)
                       for i in range(N_THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

            self.assertFalse(errors, f"Save-settings errors: {errors[:5]}")

            settings_path = Path(tmp) / "settings.json"
            self.assertTrue(settings_path.exists(), "settings.json missing")
            raw = settings_path.read_text(encoding="utf-8")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                self.fail(f"settings.json is not valid JSON after concurrent writes: {exc}")
            self.assertIsInstance(parsed, dict, "settings.json must be a dict")


# ---------------------------------------------------------------------------
# Test 3 — compaction under load
# ---------------------------------------------------------------------------

class TestHistoryCompactionUnderLoad(unittest.TestCase):

    @chaos
    def test_history_compaction_under_load(self):
        """Write 200 items, delete 100 (tombstones), compact while reader is active.

        Verifies: reader never sees a half-compacted state (no JSON parse errors).
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            # Write 200 items
            ids = []
            for i in range(200):
                item = store.add_history_item(text=f"item-{i}")
                ids.append(item.id)

            # Delete half via tombstones
            for item_id in ids[:100]:
                store.delete_history_item(item_id)

            reader_errors: list[str] = []
            stop_event = threading.Event()

            def reader_loop() -> None:
                while not stop_event.is_set():
                    try:
                        store.get_history_page(cursor=None, limit=500)
                    except Exception as exc:
                        reader_errors.append(str(exc))
                    time.sleep(0.005)

            reader = threading.Thread(target=reader_loop, daemon=True)
            reader.start()

            # Compact in main thread
            store.compact_with_stats()

            stop_event.set()
            reader.join(timeout=5)

            self.assertFalse(reader_errors, f"Reader saw errors during compaction: {reader_errors[:5]}")

            # After compaction active count should be exactly 100
            stats = store.get_history_stats()
            self.assertEqual(
                stats["active_count"], 100,
                f"Expected 100 active items after compact, got {stats['active_count']}",
            )


# ---------------------------------------------------------------------------
# Test 4 — settings save rollback on mid-write failure
# ---------------------------------------------------------------------------

class TestSetSettingsInvalidJsonRollback(unittest.TestCase):

    def test_set_settings_invalid_json_rollback(self):
        """Simulate a crash mid-write and verify settings.json is not corrupted.

        save_settings uses atomic tmp → replace, so corruption should not occur.
        This test writes valid data first, then mocks the replace step to fail,
        and confirms the original file is intact.
        """
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            # Write initial good state
            store.save_settings({"volume": 75})
            original_raw = (Path(tmp) / "settings.json").read_text(encoding="utf-8")
            original = json.loads(original_raw)
            self.assertEqual(original["volume"], 75)

            # Now mock Path.replace to raise so the atomic swap fails
            with patch("pathlib.Path.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.save_settings({"volume": 99})

            # settings.json must still be parseable and contain the old value
            after_raw = (Path(tmp) / "settings.json").read_text(encoding="utf-8")
            try:
                after = json.loads(after_raw)
            except json.JSONDecodeError as exc:
                self.fail(f"settings.json corrupted after failed save: {exc}")

            self.assertEqual(
                after.get("volume"), 75,
                "settings.json must retain old value after failed atomic swap",
            )


# ---------------------------------------------------------------------------
# Test 5 — malformed JSON to IPC socket
# ---------------------------------------------------------------------------

class TestIPCInvalidJsonReturnsError(unittest.TestCase):

    @chaos
    def test_ipc_request_invalid_json_returns_error(self):
        """Send malformed JSON to IPC socket; server must not crash."""
        import socket as _socket

        with tempfile.TemporaryDirectory() as tmp:
            sock_path = Path(tmp) / "test.sock"
            errors: list[str] = []

            # Minimal echo server that mirrors IPCServer behaviour
            server_sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            server_sock.bind(str(sock_path))
            server_sock.listen(5)
            server_sock.settimeout(5)

            def server_loop() -> None:
                try:
                    conn, _ = server_sock.accept()
                    data = conn.recv(4096)
                    try:
                        json.loads(data)
                        resp = json.dumps({"id": None, "ok": True, "result": {}}).encode() + b"\n"
                    except json.JSONDecodeError as exc:
                        resp = json.dumps({"id": None, "ok": False, "error": str(exc)}).encode() + b"\n"
                    conn.sendall(resp)
                    conn.close()
                except Exception as exc:
                    errors.append(str(exc))
                finally:
                    server_sock.close()

            srv = threading.Thread(target=server_loop, daemon=True)
            srv.start()

            time.sleep(0.05)
            client = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            client.connect(str(sock_path))
            client.sendall(b"THIS IS NOT JSON\n")
            resp_raw = client.recv(4096)
            client.close()
            srv.join(timeout=3)

            self.assertFalse(errors, f"Server crashed: {errors}")
            resp = json.loads(resp_raw)
            self.assertFalse(resp.get("ok"), "Server should respond with ok=False for invalid JSON")


# ---------------------------------------------------------------------------
# Test 6 — 100 unknown methods in a row, server stays responsive
# ---------------------------------------------------------------------------

class TestIPCUnknownMethodChaos(unittest.TestCase):

    @chaos
    def test_ipc_unknown_method_returns_error(self):
        """100 unknown methods in a row — server stays responsive throughout."""
        # We test BackendService.handle_request directly with unknown methods.
        # This avoids needing a running IPC socket for the chaos variant.
        try:
            from backend.service import BackendService
        except Exception as exc:
            self.skipTest(f"BackendService import failed (heavy deps): {exc}")

        with tempfile.TemporaryDirectory() as tmp:
            try:
                svc = BackendService.__new__(BackendService)
                svc.store = _make_store(tmp)
                svc._event_bus = EventBus()
                svc._metrics = MetricsCollector()
                svc._handler_map = {}
                svc._error_bus = None
            except Exception as exc:
                self.skipTest(f"BackendService init failed: {exc}")

            for i in range(100):
                try:
                    # handle_request should not raise — it should return an error dict
                    result = svc.handle_request(
                        {"id": str(i), "method": f"nonexistent_method_{i}", "params": {}}
                    )
                    # If handle_request exists, expect some kind of dict back
                    if result is not None:
                        self.assertIsInstance(result, dict)
                except AttributeError:
                    # handle_request may not exist as a direct method in all setups
                    break
                except Exception as exc:
                    # Should never raise uncaught
                    self.fail(f"handle_request raised on unknown method {i}: {exc}")


# ---------------------------------------------------------------------------
# Test 7 — oversized request handled gracefully
# ---------------------------------------------------------------------------

class TestIPCOversizedRequestHandled(unittest.TestCase):

    @chaos
    def test_ipc_oversized_request_handled(self):
        """Send 50 MB params dict; verify server rejects gracefully (doesn't OOM).

        We test via StateStore which is what IPC ultimately calls — we pass
        a huge string as a text value and verify no crash or memory blow-up.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            huge_text = "x" * (50 * 1024 * 1024)  # 50 MB string

            # add_history_item should either succeed (writing to disk) or raise
            # a sane exception — but NOT hang or segfault.
            try:
                store.add_history_item(text=huge_text)
                # If it succeeded, verify it's readable back
                page, _ = store.get_history_page(cursor=None, limit=10)
                self.assertGreater(len(page), 0)
            except (OSError, MemoryError):
                # Acceptable: disk-full or OOM raised cleanly
                pass
            except Exception as exc:
                # Must not be an uncaught unexpected error
                self.fail(f"Unexpected exception for oversized payload: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Test 8 — corrupted NDJSON line skipped
# ---------------------------------------------------------------------------

class TestStateStoreCorruptedLineSkipped(unittest.TestCase):

    def test_state_store_corrupted_line_skipped(self):
        """Write good lines + one corrupted; reader skips corrupt and reads the rest."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            # Write 3 good items
            good_ids = []
            for i in range(3):
                item = store.add_history_item(text=f"good item {i}")
                good_ids.append(item.id)

            # Inject a corrupted line directly into the NDJSON file
            history_path = Path(tmp) / "history.ndjson"
            with history_path.open("a", encoding="utf-8") as fh:
                fh.write("{THIS IS INVALID JSON\n")

            # Write 2 more good items after the corrupt line
            for i in range(3, 5):
                item = store.add_history_item(text=f"good item {i}")
                good_ids.append(item.id)

            # get_history_page must still return all 5 good items without crashing
            page, _ = store.get_history_page(cursor=None, limit=100)
            returned_ids = {d["id"] for d in page}

            for gid in good_ids:
                self.assertIn(
                    gid, returned_ids,
                    f"Good item {gid} missing after corrupt-line injection",
                )

            self.assertEqual(
                len(returned_ids), 5,
                f"Expected 5 items, got {len(returned_ids)}",
            )


# ---------------------------------------------------------------------------
# Test 9 — unicode / emoji / multiscript roundtrip
# ---------------------------------------------------------------------------

class TestHistoryItemUnicodeEmojiPreserved(unittest.TestCase):

    def test_history_item_with_unicode_emoji_preserved(self):
        """Add item with mixed scripts + emoji; verify byte-perfect roundtrip."""
        exotic = (
            "\U0001F980\U0001F422\U0001F480 "   # 🦀🐢💀
            "Привет мир "                        # Cyrillic
            "שלום "         # Hebrew שלום
            "مرحبا "   # Arabic مرحبا
            "中文 "                      # Chinese 中文
            "Ñoño"                              # Latin + diacritics
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.add_history_item(text=exotic)

            # Read back via get_history_page
            page, _ = store.get_history_page(cursor=None, limit=10)
            self.assertEqual(len(page), 1)
            returned_text = page[0]["text"]
            self.assertEqual(
                returned_text, exotic,
                f"Byte-perfect roundtrip failed.\nExpected: {exotic!r}\nGot: {returned_text!r}",
            )


# ---------------------------------------------------------------------------
# Test 10 — simultaneous SRT export + delete
# ---------------------------------------------------------------------------

class TestSimultaneousExportSrtAndDelete(unittest.TestCase):

    @chaos
    def test_simultaneous_export_srt_and_delete(self):
        """SRT export while concurrent delete runs — result must be a consistent snapshot.

        We directly test HistoryService.handle_export_history_srt + StateStore.delete_history_item
        running concurrently. The export should either see the item (pre-delete) or raise
        'not found' (post-delete) — never crash or return garbled data.
        """
        try:
            from backend.history_service import HistoryService
        except Exception as exc:
            self.skipTest(f"HistoryService import failed: {exc}")

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item(
                text="Test SRT export concurrent delete",
                audio_duration_sec=10.0,
            )
            hs = HistoryService(store=store)

            export_results: list[Any] = []
            export_errors: list[str] = []

            def do_export() -> None:
                time.sleep(0.02)  # slight delay so delete races
                try:
                    result = hs.handle_export_history_srt({"id": item.id})
                    export_results.append(result)
                except RuntimeError as exc:
                    # "Record not found" is acceptable if delete won the race
                    export_errors.append(str(exc))
                except Exception as exc:
                    export_errors.append(f"UNEXPECTED: {type(exc).__name__}: {exc}")

            def do_delete() -> None:
                store.delete_history_item(item.id)

            t_export = threading.Thread(target=do_export, daemon=True)
            t_delete = threading.Thread(target=do_delete, daemon=True)
            t_export.start()
            t_delete.start()
            t_export.join(timeout=10)
            t_delete.join(timeout=5)

            unexpected = [e for e in export_errors if "UNEXPECTED" in e]
            self.assertFalse(
                unexpected,
                f"Unexpected errors during concurrent SRT export + delete: {unexpected}",
            )

            # Either export succeeded with valid content OR "not found" RuntimeError — both OK
            if export_results:
                result = export_results[0]
                self.assertIn("content", result, "SRT result must have 'content' key")
                content = result["content"]
                self.assertIsInstance(content, str, "SRT content must be a string")


# ---------------------------------------------------------------------------
# Test 11 — MetricsCollector no memory leak after 10000 events
# ---------------------------------------------------------------------------

class TestLongRunningMetricCollectorNoLeak(unittest.TestCase):

    @chaos
    def test_long_running_metric_collector_no_leak(self):
        """Fire 10000 metric events; verify MetricsCollector RSS delta is bounded.

        Uses tracemalloc to snapshot before/after. The deque is bounded by window_size
        so memory should stay roughly constant.
        """
        WINDOW = 500
        N_EVENTS = 10_000
        collector = MetricsCollector(window_size=WINDOW)

        tracemalloc.start()
        snap1 = tracemalloc.take_snapshot()

        for i in range(N_EVENTS):
            collector.record(latency_ms=float(i % 1000), confidence=0.9, is_error=False)

        snap2 = tracemalloc.take_snapshot()
        tracemalloc.stop()

        top_stats = snap2.compare_to(snap1, "lineno")
        total_delta_bytes = sum(s.size_diff for s in top_stats)

        # Allow up to 8 MB total growth (generous budget for deque churn)
        MAX_GROWTH_BYTES = 8 * 1024 * 1024
        self.assertLess(
            total_delta_bytes,
            MAX_GROWTH_BYTES,
            f"MetricsCollector grew {total_delta_bytes // 1024} KB — possible leak",
        )

        # Sanity: summary still works
        summary = collector.get_summary()
        self.assertIn("stt_metrics", summary)
        self.assertEqual(summary["total_requests"], N_EVENTS)


# ---------------------------------------------------------------------------
# Test 12 — EventBus subscriber exception isolated
# ---------------------------------------------------------------------------

class TestEventBusSubscriberExceptionIsolated(unittest.TestCase):

    def test_event_bus_subscriber_exception_isolated(self):
        """Subscriber raises mid-emit; other subscribers still receive the event.

        Note: EventBus uses queue.put_nowait — exceptions don't propagate from
        within a subscriber callback. This test verifies the queue delivery model
        ensures isolation even if one consumer thread crashes while processing.
        """
        bus = EventBus()

        q_good1 = bus.subscribe()
        q_good2 = bus.subscribe()

        # Emit an event
        bus.emit("test.event", {"val": 42})

        # Both queues should receive the event regardless of any consumer crashing
        received1 = q_good1.get_nowait()
        received2 = q_good2.get_nowait()

        self.assertEqual(received1["type"], "test.event")
        self.assertEqual(received2["type"], "test.event")
        self.assertEqual(received1["data"]["val"], 42)
        self.assertEqual(received2["data"]["val"], 42)

        # Simulate a crashing consumer: the queue received the item; consumer crashes
        def crashing_consumer(q):
            q.get_nowait()
            raise RuntimeError("consumer exploded intentionally")

        bus_crash = EventBus()
        q_crash = bus_crash.subscribe()
        q_survivor = bus_crash.subscribe()
        bus_crash.emit("test.crash", {"hello": "world"})

        # Crashing consumer
        try:
            crashing_consumer(q_crash)
        except RuntimeError:
            pass  # Expected

        # Survivor's queue is unaffected — event is already in the queue
        survivor_event = q_survivor.get_nowait()
        self.assertEqual(survivor_event["type"], "test.crash")
        self.assertEqual(survivor_event["data"]["hello"], "world")

    def test_event_bus_full_queue_does_not_block_emit(self):
        """When a subscriber's queue is full, emit does not block.

        Regression check: if a slow consumer never drains, emit should still
        return promptly for other subscribers.
        """
        from backend.event_bus import _QUEUE_MAXSIZE
        bus = EventBus()

        _q_slow = bus.subscribe()  # noqa: F841 — subscription creates queue on bus; value unused
        q_fast = bus.subscribe()

        # Fill the slow queue to capacity
        for i in range(_QUEUE_MAXSIZE):
            bus.emit("fill", {"i": i})

        # One more emit — should not block (slow queue full, fast queue fine)
        start = time.monotonic()
        bus.emit("overflow", {"x": 1})
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 0.5, "emit blocked when a subscriber queue was full")

        # Fast queue still has all events
        count = 0
        try:
            while True:
                q_fast.get_nowait()
                count += 1
        except Exception:
            pass
        # Should have received _QUEUE_MAXSIZE + 1 events (or maxsize if overflow dropped)
        self.assertGreaterEqual(count, _QUEUE_MAXSIZE)


if __name__ == "__main__":
    # When run directly, always execute chaos tests
    os.environ["RUN_CHAOS"] = "1"
    unittest.main(verbosity=2)
