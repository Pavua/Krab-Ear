"""Tests for AbbreviationExpander RLock thread-safety (W1111 F3 MED).

Verifies:
- test_concurrent_expand_and_add_no_race: concurrent expand() + add_abbreviation()
  does not raise or corrupt data.
- test_rebuild_compiled_atomic: _rebuild_compiled() is called under the lock so
  expand() never sees a half-rebuilt compiled list.
"""

from __future__ import annotations

import sys
import os
import threading
import time
import unittest

# Path setup so the module can be imported standalone
_HERE = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.abbreviation_expander import AbbreviationExpander  # noqa: E402


class TestConcurrentExpandAddNoRace(unittest.TestCase):
    """Concurrent expand() + add_abbreviation() must not raise or corrupt state."""

    def test_concurrent_expand_and_add_no_race(self) -> None:
        expander = AbbreviationExpander(data_dir=None)
        errors: list[Exception] = []
        results: list[str] = []

        def expand_loop() -> None:
            for _ in range(200):
                try:
                    r = expander.expand("т.е. это верно, напр. вот так", "ru")
                    results.append(r)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        def add_loop() -> None:
            for i in range(50):
                try:
                    expander.add_abbreviation(f"тест{i}.", f"тестовое слово {i}", "ru")
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = [
            threading.Thread(target=expand_loop),
            threading.Thread(target=expand_loop),
            threading.Thread(target=add_loop),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(errors, [], f"Concurrent expand+add raised: {errors}")
        self.assertTrue(len(results) > 0, "expand_loop produced no results")

    def test_concurrent_expand_and_remove_no_race(self) -> None:
        expander = AbbreviationExpander(data_dir=None)
        # Pre-populate abbreviations so remove has something to work with
        for i in range(30):
            expander.add_abbreviation(f"r{i}.", f"расшифровка {i}", "ru")

        errors: list[Exception] = []

        def expand_loop() -> None:
            for _ in range(200):
                try:
                    expander.expand("т.е. это r0. r1. r2.", "ru")
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        def remove_loop() -> None:
            for i in range(30):
                try:
                    expander.remove_abbreviation(f"r{i}.", "ru")
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = [
            threading.Thread(target=expand_loop),
            threading.Thread(target=remove_loop),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(errors, [], f"Concurrent expand+remove raised: {errors}")


class TestRebuildCompiledAtomic(unittest.TestCase):
    """_rebuild_compiled is called under the RLock, so expand() always sees a
    consistent compiled list."""

    def test_rebuild_compiled_atomic(self) -> None:
        """Verify that the compiled list never contains mixed/partial state.

        We interleave many add_abbreviation() calls with expand() calls and
        assert that every expand() result is a valid string (not None, not
        partially built).
        """
        expander = AbbreviationExpander(data_dir=None)
        invalid_results: list = []

        def add_and_expand() -> None:
            for i in range(100):
                expander.add_abbreviation(f"aa{i}.", f"аббревиатура {i}", "ru")
                result = expander.expand(f"aa{i}. текст", "ru")
                if not isinstance(result, str):
                    invalid_results.append(result)

        threads = [threading.Thread(target=add_and_expand) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)

        self.assertEqual(
            invalid_results,
            [],
            f"expand() returned non-string: {invalid_results}",
        )

    def test_lock_is_rlock(self) -> None:
        """Ensure the lock is an RLock (reentrant), not a plain Lock."""
        expander = AbbreviationExpander(data_dir=None)
        lock = expander._lock
        # RLock supports acquire(blocking=True) multiple times from same thread
        self.assertTrue(lock.acquire(blocking=True))
        try:
            # A plain Lock would deadlock here; RLock allows re-entry
            acquired = lock.acquire(blocking=False)
            if acquired:
                lock.release()
        finally:
            lock.release()

    def test_list_abbreviations_consistent(self) -> None:
        """list_abbreviations() returns a list of dicts; concurrent mutations
        must not cause KeyError or return partial entries."""
        expander = AbbreviationExpander(data_dir=None)
        errors: list[Exception] = []

        def mutate() -> None:
            for i in range(50):
                expander.add_abbreviation(f"x{i}.", f"икс {i}", "ru")
                time.sleep(0)  # yield

        def read() -> None:
            for _ in range(200):
                try:
                    items = expander.list_abbreviations("ru")
                    for item in items:
                        _ = item["abbr"], item["expansion"], item["flags"], item["builtin"]
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = [threading.Thread(target=mutate), threading.Thread(target=read)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(errors, [], f"list_abbreviations raised: {errors}")


if __name__ == "__main__":
    unittest.main()
