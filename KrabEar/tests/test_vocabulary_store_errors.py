"""Tests for vocabulary.load_fail error push (Phase B.2 F3)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.vocabulary_store import VocabularyStore


def _make_store(with_bus: bool = True) -> tuple[VocabularyStore, Path]:
    tmp = tempfile.mkdtemp()
    store = VocabularyStore(data_dir=Path(tmp))
    if with_bus:
        store._error_bus = MagicMock()
    return store, Path(tmp)


class VocabularyStorePushErrorHelperTests(unittest.TestCase):
    """Unit tests for VocabularyStore._push_error helper."""

    def setUp(self) -> None:
        self.store, self.tmp_dir = _make_store()
        self.addCleanup(lambda: __import__("shutil").rmtree(str(self.tmp_dir), ignore_errors=True))

    def test_no_bus_does_not_raise(self) -> None:
        """_push_error with no _error_bus set must not raise."""
        store, tmp = _make_store(with_bus=False)
        self.addCleanup(lambda: __import__("shutil").rmtree(str(tmp), ignore_errors=True))
        store._push_error("vocabulary.load_fail", "test")  # must not raise

    def test_broken_bus_does_not_raise(self) -> None:
        """If error_bus.push itself throws, _push_error swallows the exception."""
        self.store._error_bus.push.side_effect = RuntimeError("bus broken")
        self.store._push_error("vocabulary.load_fail", "corrupted")  # must not raise

    def test_push_correct_code_and_component(self) -> None:
        self.store._push_error("vocabulary.load_fail", "OSError: permission denied")
        pushed = self.store._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "vocabulary.load_fail")
        self.assertEqual(pushed.component, "vocabulary")
        self.assertEqual(pushed.severity, "warn")

    def test_push_context_contains_path(self) -> None:
        self.store._push_error("vocabulary.load_fail", "parse error")
        pushed = self.store._error_bus.push.call_args[0][0]
        self.assertIn("path", pushed.context)


class VocabularyStoreLoadFailTests(unittest.TestCase):
    """VocabularyStore.load() pushes vocabulary.load_fail on OS and parse errors."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.store = VocabularyStore(data_dir=Path(self.tmp))
        self.store._error_bus = MagicMock()

    def test_os_error_on_read_pushes_load_fail(self) -> None:
        """OSError when reading the file triggers vocabulary.load_fail."""
        # Create the file so .exists() passes, then cause OSError on read_text
        self.store.path.touch()
        original_read_text = Path.read_text

        def _failing_read_text(self_path, *args, **kwargs):
            if self_path == self.store.path:
                raise OSError("Permission denied")
            return original_read_text(self_path, *args, **kwargs)

        with __import__("unittest.mock").mock.patch.object(
            Path, "read_text", _failing_read_text
        ):
            result = self.store.load()

        self.assertEqual(result, [])
        self.assertEqual(self.store._error_bus.push.call_count, 1)
        pushed = self.store._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "vocabulary.load_fail")
        self.assertEqual(pushed.severity, "warn")

    def test_invalid_json_pushes_load_fail(self) -> None:
        """Corrupted JSON triggers vocabulary.load_fail."""
        self.store.path.write_text("{ INVALID JSON }", encoding="utf-8")
        result = self.store.load()

        self.assertEqual(result, [])
        self.assertEqual(self.store._error_bus.push.call_count, 1)
        pushed = self.store._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "vocabulary.load_fail")

    def test_missing_file_no_push(self) -> None:
        """Missing vocabulary file returns [] without pushing (expected condition)."""
        # Ensure file doesn't exist
        if self.store.path.exists():
            self.store.path.unlink()
        result = self.store.load()
        self.assertEqual(result, [])
        self.store._error_bus.push.assert_not_called()

    def test_valid_vocabulary_no_push(self) -> None:
        """Valid vocabulary.json returns words without pushing."""
        payload = {"words": ["краб", "машина", "python"], "updated_at": "2026-05-04T00:00:00+00:00"}
        self.store.path.write_text(json.dumps(payload), encoding="utf-8")

        result = self.store.load()

        self.assertEqual(set(result), {"краб", "машина", "python"})
        self.store._error_bus.push.assert_not_called()

    def test_no_bus_os_error_returns_empty_gracefully(self) -> None:
        """Without error_bus, load() still returns [] on OSError."""
        store, tmp = _make_store(with_bus=False)
        self.addCleanup(lambda: __import__("shutil").rmtree(str(tmp), ignore_errors=True))
        store.path.touch()

        original_read_text = Path.read_text

        def _failing_read_text(self_path, *args, **kwargs):
            if self_path == store.path:
                raise OSError("Permission denied")
            return original_read_text(self_path, *args, **kwargs)

        with __import__("unittest.mock").mock.patch.object(Path, "read_text", _failing_read_text):
            result = store.load()

        self.assertEqual(result, [])

    def test_load_fail_pushed_twice_for_two_errors(self) -> None:
        """Two separate load() calls each push independently."""
        self.store.path.write_text("{ INVALID }", encoding="utf-8")
        self.store.load()
        self.store.load()
        self.assertEqual(self.store._error_bus.push.call_count, 2)


if __name__ == "__main__":
    unittest.main()
