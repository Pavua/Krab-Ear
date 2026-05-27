"""Tests for W1255 F2 MED fix: archive_manager fcntl.flock cross-process safety.

Verifies that _append_ndjson and _rewrite_archive acquire LOCK_EX on the
sibling lock file before touching archive.ndjson.
"""

from __future__ import annotations

import ast
import fcntl
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# ---------------------------------------------------------------------------
# Path bootstrap (consistent with other test files in this repo)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.archive_manager import (  # noqa: E402
    ArchiveManager,
    _ARCHIVE_LOCK_FILE,
    _ARCHIVE_FILE,
    _ARCHIVE_SUBDIR,
)


# ---------------------------------------------------------------------------
# Minimal fake store
# ---------------------------------------------------------------------------

class _FakeStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict = {}

    def get_history_item_by_id(self, item_id: str):
        return self._items.get(item_id)

    def delete_history_item(self, item_id: str) -> None:
        self._items.pop(item_id, None)

    def add_history_item(self, **kwargs):
        pass

    def add_item(self, item_id: str, item_dict: dict) -> None:
        self._items[item_id] = item_dict


def _make_manager(tmp_dir: str) -> ArchiveManager:
    store = _FakeStore(tmp_dir)
    return ArchiveManager(store)


# ---------------------------------------------------------------------------
# Tests: _append_ndjson acquires flock
# ---------------------------------------------------------------------------

class TestAppendAcquiresFlock(unittest.TestCase):
    """_append_ndjson must acquire LOCK_EX via fcntl.flock."""

    def test_append_acquires_flock(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _make_manager(tmp)
            flock_calls: list[int] = []

            real_flock = fcntl.flock

            def recording_flock(fd, op):
                flock_calls.append(op)
                return real_flock(fd, op)

            with patch("fcntl.flock", side_effect=recording_flock):
                mgr._append_ndjson(mgr._archive_path, {"id": "x", "text": "t"})

            self.assertIn(fcntl.LOCK_EX, flock_calls, "LOCK_EX must be acquired during append")
            self.assertIn(fcntl.LOCK_UN, flock_calls, "LOCK_UN must be released after append")

    def test_append_uses_sibling_lock_file(self):
        """Lock is acquired on archive.ndjson.lock, not on archive.ndjson itself."""
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _make_manager(tmp)
            lock_path = mgr._lock_path
            fds_flocked: list[int] = []

            real_flock = fcntl.flock

            def recording_flock(fd, op):
                if op == fcntl.LOCK_EX:
                    fds_flocked.append(fd)
                return real_flock(fd, op)

            # Track which fileno belongs to the lock file by opening it ourselves
            with patch("fcntl.flock", side_effect=recording_flock):
                mgr._append_ndjson(mgr._archive_path, {"id": "x", "text": "t"})

            # The lock file must exist
            self.assertTrue(lock_path.exists(), "Lock file must exist")
            # At least one LOCK_EX must have been issued
            self.assertTrue(fds_flocked, "LOCK_EX must be acquired")

    def test_append_lock_file_name(self):
        """The lock file is named archive.ndjson.lock (not archive.ndjson)."""
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _make_manager(tmp)
            self.assertEqual(mgr._lock_path.name, _ARCHIVE_LOCK_FILE)
            # Confirm it's NOT the data file itself
            self.assertNotEqual(mgr._lock_path, mgr._archive_path)


# ---------------------------------------------------------------------------
# Tests: _rewrite_archive acquires flock
# ---------------------------------------------------------------------------

class TestRewriteAcquiresFlock(unittest.TestCase):
    """_rewrite_archive must acquire LOCK_EX via fcntl.flock."""

    def test_rewrite_acquires_flock(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _make_manager(tmp)
            flock_calls: list[int] = []

            real_flock = fcntl.flock

            def recording_flock(fd, op):
                flock_calls.append(op)
                return real_flock(fd, op)

            with patch("fcntl.flock", side_effect=recording_flock):
                mgr._rewrite_archive([{"id": "y", "text": "world"}])

            self.assertIn(fcntl.LOCK_EX, flock_calls, "LOCK_EX must be acquired during rewrite")
            self.assertIn(fcntl.LOCK_UN, flock_calls, "LOCK_UN must be released after rewrite")

    def test_rewrite_uses_sibling_lock_file(self):
        """Lock is acquired on the sibling .lock file, not on archive.ndjson."""
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _make_manager(tmp)
            lock_path = mgr._lock_path
            fds_flocked: list[int] = []

            real_flock = fcntl.flock

            def recording_flock(fd, op):
                if op == fcntl.LOCK_EX:
                    fds_flocked.append(fd)
                return real_flock(fd, op)

            with patch("fcntl.flock", side_effect=recording_flock):
                mgr._rewrite_archive([{"id": "y", "text": "world"}])

            self.assertTrue(lock_path.exists(), "Lock file must exist")
            self.assertTrue(fds_flocked, "LOCK_EX must be acquired")

    def test_rewrite_lock_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _make_manager(tmp)
            self.assertEqual(mgr._lock_path.name, _ARCHIVE_LOCK_FILE)
            self.assertNotEqual(mgr._lock_path, mgr._archive_path)


# ---------------------------------------------------------------------------
# Tests: flock held across full operation
# ---------------------------------------------------------------------------

class TestFlockHeldAcrossFullOperation(unittest.TestCase):
    """LOCK_EX must be held for the entire duration of each write operation."""

    def test_flock_held_across_append_operation(self):
        """LOCK_EX is acquired before the archive file is opened for append."""
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _make_manager(tmp)
            event_log: list[str] = []

            real_flock = fcntl.flock

            def tracking_flock(fd, op):
                if op == fcntl.LOCK_EX:
                    event_log.append("LOCK_EX")
                elif op == fcntl.LOCK_UN:
                    event_log.append("LOCK_UN")
                return real_flock(fd, op)

            archive_path = mgr._archive_path
            real_path_open = Path.open

            def tracking_path_open(self_path, mode="r", **kwargs):
                fh = real_path_open(self_path, mode, **kwargs)
                if self_path == archive_path and "a" in mode:
                    event_log.append("ARCHIVE_OPEN")
                return fh

            with patch("fcntl.flock", side_effect=tracking_flock):
                with patch.object(Path, "open", tracking_path_open):
                    mgr._append_ndjson(mgr._archive_path, {"id": "z", "text": "test"})

            self.assertIn("LOCK_EX", event_log, "LOCK_EX must be in event log")
            self.assertIn("ARCHIVE_OPEN", event_log, "Archive must be opened for append")
            self.assertIn("LOCK_UN", event_log, "LOCK_UN must be in event log")

            lock_ex_idx = next(i for i, e in enumerate(event_log) if e == "LOCK_EX")
            archive_open_idx = next(i for i, e in enumerate(event_log) if e == "ARCHIVE_OPEN")
            lock_un_idx = next(i for i, e in enumerate(event_log) if e == "LOCK_UN")

            self.assertLess(lock_ex_idx, archive_open_idx, "LOCK_EX must precede ARCHIVE_OPEN")
            self.assertLess(archive_open_idx, lock_un_idx, "ARCHIVE_OPEN must precede LOCK_UN")

    def test_flock_held_across_rewrite_operation(self):
        """LOCK_EX is acquired before the tmp file write; LOCK_UN after replace."""
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _make_manager(tmp)
            event_log: list[str] = []

            real_flock = fcntl.flock

            def tracking_flock(fd, op):
                if op == fcntl.LOCK_EX:
                    event_log.append("LOCK_EX")
                elif op == fcntl.LOCK_UN:
                    event_log.append("LOCK_UN")
                return real_flock(fd, op)

            real_path_replace = Path.replace

            def tracking_replace(self_path, target):
                event_log.append("REPLACE")
                return real_path_replace(self_path, target)

            with patch("fcntl.flock", side_effect=tracking_flock):
                with patch.object(Path, "replace", tracking_replace):
                    mgr._rewrite_archive([{"id": "r", "text": "rewrite"}])

            self.assertIn("LOCK_EX", event_log)
            self.assertIn("REPLACE", event_log)
            self.assertIn("LOCK_UN", event_log)

            lock_ex_idx = next(i for i, e in enumerate(event_log) if e == "LOCK_EX")
            replace_idx = next(i for i, e in enumerate(event_log) if e == "REPLACE")
            lock_un_idx = next(i for i, e in enumerate(event_log) if e == "LOCK_UN")

            self.assertLess(lock_ex_idx, replace_idx, "LOCK_EX must precede REPLACE")
            self.assertLess(replace_idx, lock_un_idx, "REPLACE must precede LOCK_UN")


# ---------------------------------------------------------------------------
# Tests: lock file created on init
# ---------------------------------------------------------------------------

class TestLockFileIsCreated(unittest.TestCase):
    def test_lock_file_created_on_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _make_manager(tmp)
            expected = mgr._archive_dir / _ARCHIVE_LOCK_FILE
            self.assertTrue(expected.exists(), f"Lock file {expected} must exist after __init__")

    def test_lock_file_path_is_sibling_to_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = _make_manager(tmp)
            self.assertEqual(mgr._lock_path.parent, mgr._archive_path.parent)
            self.assertEqual(mgr._lock_path.name, _ARCHIVE_LOCK_FILE)


# ---------------------------------------------------------------------------
# AST checks
# ---------------------------------------------------------------------------

class TestASTFcntlUsage(unittest.TestCase):
    """AST/source check: archive_manager.py imports fcntl and uses flock."""

    def _get_source(self) -> str:
        # File is at KrabEar/backend/archive_manager.py relative to PROJECT_ROOT
        src = PROJECT_ROOT / "KrabEar" / "backend" / "archive_manager.py"
        return src.read_text(encoding="utf-8")

    def test_fcntl_imported(self):
        source = self._get_source()
        tree = ast.parse(source)
        imports = [
            node.names[0].name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
        ]
        self.assertIn("fcntl", imports, "fcntl must be imported at module level")

    def test_flock_called(self):
        source = self._get_source()
        self.assertIn("fcntl.flock", source, "fcntl.flock must be called in archive_manager.py")

    def test_lock_ex_used(self):
        source = self._get_source()
        self.assertIn("LOCK_EX", source, "fcntl.LOCK_EX must be referenced in archive_manager.py")

    def test_lock_un_used(self):
        source = self._get_source()
        self.assertIn("LOCK_UN", source, "fcntl.LOCK_UN must be referenced in archive_manager.py")

    def test_lock_file_constant_defined(self):
        source = self._get_source()
        self.assertIn(_ARCHIVE_LOCK_FILE, source, f"{_ARCHIVE_LOCK_FILE} constant must be defined")


if __name__ == "__main__":
    unittest.main()
