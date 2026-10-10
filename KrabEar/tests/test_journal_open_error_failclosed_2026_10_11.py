"""Ошибка открытия журнала (не ENOENT) не равна «журнал пуст».

Раньше `_read_history_ndjson_unlocked` при OSError на os.open (EACCES/EMFILE/EIO)
молча отдавал пустой поток, а компактация перезаписывала history.ndjson этим
пустым результатом и сбрасывала tombstones/дельты. Теперь чтение поднимает
ошибку, а компактация не трогает файлы.
"""

from __future__ import annotations

import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import state_store as ss  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


class JournalOpenErrorFailClosedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.store.add_history_item(text="alpha")
        self.store.add_history_item(text="beta")

    def _failing_open(self, target: Path, code: int = errno.EMFILE):
        real_open = os.open
        target_name = target.name

        def fake(path, flags, *args, **kwargs):
            if Path(path).name == target_name:
                raise OSError(code, os.strerror(code))
            return real_open(path, flags, *args, **kwargs)

        return patch.object(ss.os, "open", side_effect=fake)

    def test_reader_raises_on_open_error(self) -> None:
        with self._failing_open(self.store.history_path):
            with self.assertRaises(OSError):
                list(self.store._read_history_ndjson_unlocked(self.store.history_path))

    def test_missing_journal_is_still_empty(self) -> None:
        missing = self.store.data_dir / "no_such_journal.ndjson"
        self.assertEqual(list(self.store._read_history_ndjson_unlocked(missing)), [])

    def test_compaction_does_not_wipe_history_when_open_fails(self) -> None:
        before = self.store.history_path.read_bytes()
        with self._failing_open(self.store.history_path):
            with self.assertRaises(OSError):
                with self.store._lock():
                    self.store._compact_unlocked()
        self.assertEqual(self.store.history_path.read_bytes(), before)
        page, _ = self.store.get_history_page(cursor=None, limit=10)
        self.assertEqual(sorted(i["text"] for i in page), ["alpha", "beta"])


if __name__ == "__main__":
    unittest.main()
