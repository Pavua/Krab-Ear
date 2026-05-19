"""Wave 209 — StateStore file-lock invariant deep tests.

Covers:
- test_concurrent_append_serialized        — 50 threads × append → no interleaved lines
- test_append_during_compact_safe          — 10 threads append + 1 thread compact → no data loss
- test_lock_released_on_exception          — exception inside _lock releases fcntl lock
- test_lock_inter_process_blocking         — two StateStore instances on same path serialize writes
- test_compact_preserves_active_items_under_load — compaction keeps all active items
- test_tombstone_delete_visible_after_lock_release — delete visible to next reader
- test_file_lock_timeout_handled_gracefully — non-blocking flock on locked file raises BlockingIOError
- test_unicode_lines_atomic                — Cyrillic/emoji text round-trips without corruption
- test_corrupted_line_recovery_during_compact — corrupt lines skipped, rest preserved
- test_appending_to_locked_store_waits     — second store waits until first releases lock
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_dir: str, **kwargs) -> StateStore:
    return StateStore(Path(tmp_dir) / "data", **kwargs)


def _add(store: StateStore, text: str = "hello") -> str:
    item = store.add_history_item(text)
    return item.id


def _read_raw_lines(path: Path) -> list[str]:
    """Return all non-empty lines from an NDJSON file."""
    with path.open("r", encoding="utf-8") as fh:
        return [ln.rstrip("\n") for ln in fh if ln.strip()]


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------

class TestConcurrentAppendSerialized(unittest.TestCase):
    """50 threads × append → no interleaved JSON, all 50 items present."""

    THREADS = 50

    def test_concurrent_append_serialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            def append_one(idx: int) -> str:
                item = store.add_history_item(f"item-{idx}", paste_status="ok")
                return item.id

            with ThreadPoolExecutor(max_workers=self.THREADS) as executor:
                futures = [executor.submit(append_one, i) for i in range(self.THREADS)]
                returned_ids = {f.result() for f in as_completed(futures)}

            # Every returned id must be unique
            self.assertEqual(len(returned_ids), self.THREADS)

            # Every line in history.ndjson must be valid JSON
            lines = _read_raw_lines(store.history_path)
            self.assertEqual(len(lines), self.THREADS)

            parsed_ids = set()
            for line in lines:
                obj = json.loads(line)  # raises on corrupt/interleaved bytes
                parsed_ids.add(obj["id"])

            # All ids on disk match what callers received
            self.assertEqual(parsed_ids, returned_ids)


class TestAppendDuringCompactSafe(unittest.TestCase):
    """10 appender threads + 1 compactor → no data loss after all settle."""

    APPENDERS = 10
    APPENDS_EACH = 20

    def test_append_during_compact_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp, compact_threshold_bytes=1)  # threshold=1 → always compact

            errors: list[Exception] = []

            def appender(idx: int) -> list[str]:
                ids = []
                for j in range(self.APPENDS_EACH):
                    try:
                        item = store.add_history_item(f"thread{idx}-item{j}")
                        ids.append(item.id)
                    except Exception as exc:
                        errors.append(exc)
                return ids

            def compactor():
                for _ in range(5):
                    try:
                        store.compact()
                    except Exception as exc:
                        errors.append(exc)
                    time.sleep(0.005)

            with ThreadPoolExecutor(max_workers=self.APPENDERS + 1) as executor:
                append_futures = [executor.submit(appender, i) for i in range(self.APPENDERS)]
                compact_future = executor.submit(compactor)
                compact_future.result()
                all_ids: set[str] = set()
                for f in as_completed(append_futures):
                    all_ids.update(f.result())

            self.assertFalse(errors, f"Errors during concurrent append+compact: {errors}")

            expected = self.APPENDERS * self.APPENDS_EACH
            self.assertEqual(len(all_ids), expected, "Duplicate IDs returned by appenders")

            # After all threads done, active items must equal total appended
            active = store._load_active_items_with_lock()
            active_ids = {item.id for item in active}
            missing = all_ids - active_ids
            self.assertFalse(missing, f"Items lost during concurrent compact: {missing}")


class TestLockReleasedOnException(unittest.TestCase):
    """Exception raised inside _lock() context must release the fcntl lock."""

    def test_lock_released_on_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            # Force an exception inside _lock
            try:
                with store._lock():
                    raise RuntimeError("deliberate error")
            except RuntimeError:
                pass

            # Lock must be free now — a new append should succeed immediately
            item = store.add_history_item("after exception")
            self.assertIsNotNone(item.id)

            # Verify file is readable and contains the item
            active = store._load_active_items_with_lock()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].id, item.id)

    def test_lock_released_on_exception_second_call_works(self):
        """After exception, second StateStore on same path can acquire lock."""
        with tempfile.TemporaryDirectory() as tmp:
            store1 = StateStore(Path(tmp) / "data")
            try:
                with store1._lock():
                    raise ValueError("oops")
            except ValueError:
                pass

            store2 = StateStore(Path(tmp) / "data")
            # Should not deadlock
            item = store2.add_history_item("from store2")
            self.assertIsNotNone(item.id)


class TestLockInterProcessBlocking(unittest.TestCase):
    """Two StateStore instances on the same path serialize writes correctly."""

    def test_lock_inter_process_blocking(self):
        """Second store's writes must not corrupt first store's data."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store_a = StateStore(data_dir)
            store_b = StateStore(data_dir)

            ids_a: list[str] = []
            ids_b: list[str] = []

            def writer_a():
                for i in range(30):
                    item = store_a.add_history_item(f"A-{i}")
                    ids_a.append(item.id)

            def writer_b():
                for i in range(30):
                    item = store_b.add_history_item(f"B-{i}")
                    ids_b.append(item.id)

            t_a = threading.Thread(target=writer_a)
            t_b = threading.Thread(target=writer_b)
            t_a.start()
            t_b.start()
            t_a.join()
            t_b.join()

            # All lines must be valid JSON
            lines = _read_raw_lines(store_a.history_path)
            self.assertEqual(len(lines), 60)
            for line in lines:
                json.loads(line)  # must not raise

            all_ids = set(ids_a) | set(ids_b)
            self.assertEqual(len(all_ids), 60, "Duplicate ids from concurrent stores")


class TestCompactPreservesActiveItemsUnderLoad(unittest.TestCase):
    """Compaction under concurrent appends must preserve all active items."""

    def test_compact_preserves_active_items_under_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            # Pre-populate
            seed_ids = {_add(store, f"seed-{i}") for i in range(20)}

            compact_errors: list[Exception] = []
            append_ids: list[str] = []
            lock = threading.Lock()

            def do_compact():
                for _ in range(3):
                    try:
                        store.compact()
                    except Exception as exc:
                        compact_errors.append(exc)
                    time.sleep(0.005)

            def do_append(idx: int):
                item = store.add_history_item(f"load-{idx}")
                with lock:
                    append_ids.append(item.id)

            with ThreadPoolExecutor(max_workers=15) as executor:
                compact_future = executor.submit(do_compact)
                append_futures = [executor.submit(do_append, i) for i in range(40)]
                compact_future.result()
                for f in as_completed(append_futures):
                    f.result()

            self.assertFalse(compact_errors, f"Compact errors: {compact_errors}")

            active = store._load_active_items_with_lock()
            active_ids = {item.id for item in active}

            # Seed items must all survive
            missing_seed = seed_ids - active_ids
            self.assertFalse(missing_seed, f"Seed items lost: {missing_seed}")

            # All appended items must be present
            missing_appended = set(append_ids) - active_ids
            self.assertFalse(missing_appended, f"Appended items lost: {missing_appended}")


class TestTombstoneDeleteVisibleAfterLockRelease(unittest.TestCase):
    """After delete_history_item, the item must not appear in the next reader."""

    def test_tombstone_delete_visible_after_lock_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item_id = _add(store, "to delete")

            # Confirm present
            active_before = store._load_active_items_with_lock()
            self.assertTrue(any(i.id == item_id for i in active_before))

            deleted = store.delete_history_item(item_id)
            self.assertTrue(deleted)

            # Lock was released; next reader must not see deleted item
            active_after = store._load_active_items_with_lock()
            self.assertFalse(any(i.id == item_id for i in active_after))

    def test_tombstone_delete_concurrent_readers_see_deletion(self):
        """Readers spawned after delete should never see the deleted item."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            ids = [_add(store, f"item-{i}") for i in range(10)]

            # Delete half
            for iid in ids[:5]:
                store.delete_history_item(iid)

            results: list[set] = []
            lock = threading.Lock()

            def read_active():
                active = store._load_active_items_with_lock()
                with lock:
                    results.append({i.id for i in active})

            threads = [threading.Thread(target=read_active) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            deleted_set = set(ids[:5])
            for result_set in results:
                overlap = result_set & deleted_set
                self.assertFalse(overlap, f"Deleted items appeared in reader: {overlap}")


class TestFileLockTimeoutHandledGracefully(unittest.TestCase):
    """Non-blocking flock on an already-locked file should raise BlockingIOError."""

    def test_file_lock_timeout_handled_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            acquired = threading.Event()
            release = threading.Event()

            def hold_lock():
                with store._lock():
                    acquired.set()
                    release.wait(timeout=5)

            holder = threading.Thread(target=hold_lock)
            holder.start()
            acquired.wait(timeout=5)

            # Attempt non-blocking lock on the same lock file
            lock_fd = os.open(str(store.lock_path), os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(lock_fd)
                release.set()
                holder.join(timeout=5)


class TestUnicodeLinesAtomic(unittest.TestCase):
    """Cyrillic, emoji, and mixed-script text must round-trip without corruption."""

    TEXTS = [
        "Привет мир — тест Unicode",
        "Hola mundo с emoji 🎤🦀",
        "日本語テスト with mixed スクリプト",
        "Ça va bien, café résumé naïve",
        "الْعَرَبِيَّةُ‎ mixed with ASCII",
        "𝕳𝖊𝖑𝖑𝖔 𝖂𝖔𝖗𝖑𝖉",  # mathematical bold fraktur
    ]

    def test_unicode_lines_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            def add_text(text: str) -> str:
                return store.add_history_item(text).id

            with ThreadPoolExecutor(max_workers=len(self.TEXTS)) as executor:
                futures = {executor.submit(add_text, t): t for t in self.TEXTS}
                id_to_text = {f.result(): text for f, text in futures.items()}

            active = store._load_active_items_with_lock()
            active_map = {item.id: item.text for item in active}

            for item_id, original_text in id_to_text.items():
                self.assertIn(item_id, active_map)
                self.assertEqual(active_map[item_id], original_text,
                                 f"Text corrupted for id={item_id}")

    def test_unicode_round_trip_through_compaction(self):
        """Unicode text must survive a compact() call intact."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            added = {}
            for text in self.TEXTS:
                item = store.add_history_item(text)
                added[item.id] = text

            store.compact()

            active = store._load_active_items_with_lock()
            active_map = {item.id: item.text for item in active}
            for item_id, original in added.items():
                self.assertEqual(active_map.get(item_id), original,
                                 f"Unicode corrupted after compact: {original!r}")


class TestCorruptedLineRecoveryDuringCompact(unittest.TestCase):
    """Corrupt NDJSON lines are skipped; valid items are preserved after compact."""

    def test_corrupted_line_recovery_during_compact(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            # Add some valid items
            valid_ids = [_add(store, f"valid-{i}") for i in range(5)]

            # Inject corrupt lines directly into history.ndjson
            with store.history_path.open("a", encoding="utf-8") as fh:
                fh.write("NOT_VALID_JSON\n")
                fh.write('{"id": "", "ts": "2026-01-01T00:00:00", "text": "empty id"}\n')
                fh.write("{truncated\n")
                fh.write("\n")

            # Compact should skip corrupt lines
            stats = store.compact_with_stats()
            self.assertEqual(stats["after_active_count"], 5)
            self.assertGreater(stats["before_history_lines"], 5)

            # All valid items must survive
            active = store._load_active_items_with_lock()
            active_ids = {item.id for item in active}
            for vid in valid_ids:
                self.assertIn(vid, active_ids)

    def test_corrupted_tombstone_does_not_delete_item(self):
        """Corrupt tombstone lines must not cause a valid item to be treated as deleted."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item_id = _add(store, "survivor")

            # Inject corrupt tombstone lines
            with store.tombstones_path.open("a", encoding="utf-8") as fh:
                fh.write("INVALID_TOMBSTONE\n")
                fh.write('{"id": ""}\n')  # empty id — should be ignored

            active = store._load_active_items_with_lock()
            active_ids = {item.id for item in active}
            self.assertIn(item_id, active_ids, "Valid item wrongly deleted by corrupt tombstone")


class TestAppendingToLockedStoreWaits(unittest.TestCase):
    """A store append blocks until a concurrent lock holder releases."""

    def test_appending_to_locked_store_waits(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            acquired = threading.Event()
            release = threading.Event()
            append_done = threading.Event()
            append_result: list[str] = []

            def hold_lock():
                with store._lock():
                    acquired.set()
                    # Hold the lock until test signals release
                    release.wait(timeout=5)

            def do_append():
                item = store.add_history_item("waiting append")
                append_result.append(item.id)
                append_done.set()

            holder = threading.Thread(target=hold_lock)
            appender = threading.Thread(target=do_append)

            holder.start()
            acquired.wait(timeout=5)

            # Start appender while lock is held; it must block
            appender.start()

            # Give appender time to attempt the lock
            time.sleep(0.05)

            # Append must not have completed yet (lock still held)
            self.assertFalse(append_done.is_set(),
                             "Appender completed while lock was held — no serialization!")

            # Release lock and let appender finish
            release.set()
            holder.join(timeout=5)
            append_done.wait(timeout=5)

            self.assertTrue(append_done.is_set(), "Appender never completed after lock release")
            self.assertEqual(len(append_result), 1)

            # Item must be on disk
            active = store._load_active_items_with_lock()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].id, append_result[0])

    def test_multiple_waiters_all_succeed(self):
        """Multiple threads blocked on lock must all eventually append successfully."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            WAITERS = 20
            barrier = threading.Barrier(WAITERS)
            results: list[str] = []
            lock = threading.Lock()

            def contend(idx: int):
                # All threads try to acquire at the same moment
                barrier.wait()
                item = store.add_history_item(f"waiter-{idx}")
                with lock:
                    results.append(item.id)

            threads = [threading.Thread(target=contend, args=(i,)) for i in range(WAITERS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(len(results), WAITERS)
            # All lines on disk must be valid JSON
            lines = _read_raw_lines(store.history_path)
            self.assertEqual(len(lines), WAITERS)
            for line in lines:
                json.loads(line)


if __name__ == "__main__":
    unittest.main()
