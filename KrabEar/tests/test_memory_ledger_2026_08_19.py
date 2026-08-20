"""Tests for backend/memory_ledger.py — write-only cross-process memory ledger
(Memory Conductor spec §4, docs/superpowers/specs/2026-08-19-memory-conductor-design.md).

ALL tests use a TemporaryDirectory as `path=` and NEVER touch the real
~/.openclaw/memory_ledger.json (C-ONE-PATH is tested separately by mocking
Path.home()).

Coverage:
  (a) resolve_ledger_path() == ~/.openclaw/memory_ledger.json, absolute,
      unaffected by env vars (C-ONE-PATH, M8).
  (b) publish_own writes ONLY <owner>/-prefixed keys and preserves other
      owners' fresh entries (RMW-delta).
  (c) An entry with a stale (>120s) or missing updated_ts is dropped by the
      next publish (fail-closed GC).
  (d) Corrupt JSON is backed up as .corrupt-<ts> (retention: 5 newest);
      publish succeeds on a fresh file afterwards.
  (e) Two LedgerClients (different owners) publishing concurrently
      (threads x 50) lose nothing of each other.
  (f) Source contract: the sidecar lock file is the ONLY fcntl.flock() target
      in the module — the data file itself is never flocked.
  (g) read_all(nowait=True) under an externally held lock returns the empty
      doc immediately, never hangs.
"""

from __future__ import annotations

import ast
import fcntl
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Ensure project root on sys.path when run standalone (pytest or unittest).
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve()
_PROJECT_ROOT = _HERE.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend import memory_ledger  # noqa: E402
from backend.memory_ledger import LedgerClient, resolve_ledger_path  # noqa: E402


class ResolveLedgerPathTestCase(unittest.TestCase):
    """(a) one pure formula, absolute path, no env influence (C-ONE-PATH)."""

    def test_path_is_openclaw_memory_ledger_json(self):
        """Прод-формула. Conftest-шов (HIGH-3 финального гейта) снимается явно —
        здесь проверяется именно ПРОДОВЫЙ путь."""
        import backend.memory_ledger as ml
        prev = ml._TEST_PATH_OVERRIDE
        ml._TEST_PATH_OVERRIDE = None
        try:
            path = ml.resolve_ledger_path()
        finally:
            ml._TEST_PATH_OVERRIDE = prev
        self.assertEqual(path, (Path.home() / ".openclaw" / "memory_ledger.json").resolve())
        self.assertTrue(path.is_absolute())

    def test_env_var_does_not_influence_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            with patch("backend.memory_ledger.Path.home", return_value=fake_home):
                without_env = resolve_ledger_path()
                with patch.dict(
                    os.environ,
                    {
                        "MEMORY_LEDGER_PATH": "/tmp/bogus-should-be-ignored.json",
                        "KRAB_EAR_MEMORY_LEDGER_PATH": "/tmp/also-bogus.json",
                    },
                ):
                    with_env = resolve_ledger_path()
        self.assertEqual(without_env, with_env)


class LedgerClientBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.ledger_path = Path(self._tmpdir.name) / "memory_ledger.json"


class PublishOwnRmwDeltaTestCase(LedgerClientBase):
    """(b) publish_own writes only <owner>/-prefixed keys, preserves fresh siblings."""

    def test_publish_preserves_other_owners_fresh_entries(self):
        krab_ear = LedgerClient("krab_ear", path=self.ledger_path)
        krab = LedgerClient("krab", path=self.ledger_path)

        self.assertTrue(krab_ear.publish_own({"gigaam": {"state": "idle", "size_mb": 500}}))
        self.assertTrue(krab.publish_own({"brain": {"state": "active", "size_mb": 19000}}))

        doc = krab_ear.read_all()
        self.assertIn("krab_ear/gigaam", doc["entries"])
        self.assertIn("krab/brain", doc["entries"])

        mine = LedgerClient("mine", path=self.ledger_path)
        self.assertTrue(mine.publish_own({"whisper": {"state": "warm"}}))

        doc = mine.read_all()
        self.assertIn("krab_ear/gigaam", doc["entries"])
        self.assertIn("krab/brain", doc["entries"])
        self.assertIn("mine/whisper", doc["entries"])

    def test_republish_replaces_only_own_prefix(self):
        client = LedgerClient("krab_ear", path=self.ledger_path)
        other = LedgerClient("krab", path=self.ledger_path)
        other.publish_own({"brain": {"state": "active"}})

        client.publish_own({"gigaam": {"state": "idle"}})
        client.publish_own({"gigaam": {"state": "active"}})  # no more "krab_ear/gigaam(old)"

        doc = client.read_all()
        self.assertEqual(doc["entries"]["krab_ear/gigaam"]["state"], "active")
        self.assertEqual(len(doc["entries"]), 2)  # krab_ear/gigaam + krab/brain, no duplicates
        self.assertIn("krab/brain", doc["entries"])


class StaleEntryGcTestCase(LedgerClientBase):
    """(c) stale/missing updated_ts entries are dropped by the next publish."""

    def _seed_raw_doc(self, entries: dict):
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text(
            json.dumps({"v": 1, "entries": entries}), encoding="utf-8"
        )

    def test_stale_and_missing_ts_entries_are_gced_on_next_publish(self):
        now = time.time()
        self._seed_raw_doc(
            {
                "other/stale": {"owner": "other", "resident": "stale", "updated_ts": now - 200},
                "other/no_ts": {"owner": "other", "resident": "no_ts"},
                "fresh/kept": {"owner": "fresh", "resident": "kept", "updated_ts": now - 5},
            }
        )
        client = LedgerClient("mine", path=self.ledger_path)
        self.assertTrue(client.publish_own({"a": {"state": "idle"}}))

        doc = client.read_all()
        self.assertNotIn("other/stale", doc["entries"])
        self.assertNotIn("other/no_ts", doc["entries"])
        self.assertIn("fresh/kept", doc["entries"])
        self.assertIn("mine/a", doc["entries"])


class CorruptFileRecoveryTestCase(LedgerClientBase):
    """(d) corrupt JSON -> .corrupt-<ts> backup (retention 5), publish succeeds fresh."""

    def test_corrupt_json_is_backed_up_and_publish_succeeds(self):
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text("{ not json at all !!!", encoding="utf-8")

        client = LedgerClient("mine", path=self.ledger_path)
        self.assertTrue(client.publish_own({"a": {"state": "idle"}}))

        corrupt_files = list(self.ledger_path.parent.glob("memory_ledger.json.corrupt-*"))
        self.assertEqual(len(corrupt_files), 1)

        doc = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(set(doc["entries"].keys()), {"mine/a"})
        self.assertEqual(doc["v"], 1)

    def test_corrupt_backup_retention_keeps_5_newest(self):
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        # Pre-seed 5 fake backups with distinct, sortable, ascending timestamps.
        for ts in (100, 101, 102, 103, 104):
            (self.ledger_path.parent / f"memory_ledger.json.corrupt-{ts}").write_text("x")

        self.ledger_path.write_text("still not json", encoding="utf-8")
        client = LedgerClient("mine", path=self.ledger_path)
        with patch("backend.memory_ledger.time.time", return_value=200.0):
            self.assertTrue(client.publish_own({"a": {"state": "idle"}}))

        remaining = sorted(
            p.name for p in self.ledger_path.parent.glob("memory_ledger.json.corrupt-*")
        )
        self.assertEqual(len(remaining), 5)
        # oldest (100) evicted, newest (200) present
        self.assertNotIn("memory_ledger.json.corrupt-100", remaining)
        self.assertIn("memory_ledger.json.corrupt-200", remaining)


class ConcurrentPublishTestCase(LedgerClientBase):
    """(e) two owners publishing concurrently (threads x 50) lose nothing of each other."""

    def test_concurrent_publish_from_two_owners_preserves_both(self):
        errors = []

        def _worker(owner: str, resident: str, iterations: int):
            client = LedgerClient(owner, path=self.ledger_path, lock_timeout_sec=5.0)
            try:
                for i in range(iterations):
                    ok = client.publish_own({resident: {"state": "active", "iteration": i}})
                    if not ok:
                        errors.append(f"{owner} publish {i} was skipped (lock contention)")
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(f"{owner} raised {exc!r}")

        t1 = threading.Thread(target=_worker, args=("krab_ear", "gigaam", 50))
        t2 = threading.Thread(target=_worker, args=("krab", "brain", 50))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        self.assertFalse(t1.is_alive(), "krab_ear worker thread did not finish")
        self.assertFalse(t2.is_alive(), "krab worker thread did not finish")
        self.assertEqual(errors, [])

        final = LedgerClient("observer", path=self.ledger_path).read_all()
        self.assertIn("krab_ear/gigaam", final["entries"])
        self.assertIn("krab/brain", final["entries"])
        self.assertEqual(final["entries"]["krab_ear/gigaam"]["iteration"], 49)
        self.assertEqual(final["entries"]["krab/brain"]["iteration"], 49)


class SidecarLockOnlySourceContractTestCase(unittest.TestCase):
    """(f) the data file itself is NEVER flocked — only the sidecar lock file is."""

    def test_only_lock_path_is_ever_opened_for_flock(self):
        source = Path(memory_ledger.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        open_call_targets = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "open"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.args
            ):
                open_call_targets.append(ast.unparse(node.args[0]))

        self.assertTrue(open_call_targets, "expected at least one os.open() call in the module")
        for target in open_call_targets:
            self.assertIn(
                "_lock_path",
                target,
                f"os.open() target {target!r} must reference the sidecar lock path, "
                "never the data file (self._path)",
            )
            self.assertNotEqual(target, "self._path")

        flock_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "flock"
        ]
        self.assertTrue(flock_calls, "expected fcntl.flock() call(s) in the module")


class ReadAllNowaitTestCase(LedgerClientBase):
    """(g) read_all(nowait=True) under a held lock never hangs, returns empty doc."""

    def test_nowait_read_under_held_lock_does_not_hang(self):
        client = LedgerClient("mine", path=self.ledger_path)
        client.publish_own({"a": {"state": "idle"}})  # ensure a real file/lock path exist

        lock_path = self.ledger_path.with_name("memory_ledger.lock")
        holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(holder_fd, fcntl.LOCK_EX)
        try:
            result_holder = {}

            def _read():
                result_holder["doc"] = client.read_all(nowait=True)

            t = threading.Thread(target=_read)
            t.start()
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "read_all(nowait=True) hung under a held lock")
            self.assertEqual(result_holder.get("doc"), {"v": 1, "entries": {}})
        finally:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)


if __name__ == "__main__":
    unittest.main()
