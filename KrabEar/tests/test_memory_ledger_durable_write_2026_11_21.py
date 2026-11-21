"""KE05C-16 / KE05C-17: запись леджера дописывается до конца и сбрасывается на диск.

Один ``os.write()`` может записать меньше запрошенного (короткая запись) — файл
публиковался усечённым как успешный. И после ``os.replace`` не было fsync файла и
родительского каталога: успех не переживал потерю питания.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.memory_ledger import LedgerClient  # noqa: E402

_DOC = {"v": 1, "entries": {"ear/brain": {"updated_ts": 1.0, "payload": "x" * 5000}}}


class DurableWrite(unittest.TestCase):
    def test_short_writes_are_completed(self):
        real_write = os.write

        def short_write(fd, data):
            return real_write(fd, bytes(data)[:7])  # диск принимает по 7 байт

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            client = LedgerClient("ear", path=path)
            with patch("backend.memory_ledger.os.write", side_effect=short_write):
                client._write_atomic(_DOC)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), _DOC)

    def test_zero_progress_write_raises_instead_of_looping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            client = LedgerClient("ear", path=path)
            with patch("backend.memory_ledger.os.write", return_value=0):
                with self.assertRaises(OSError):
                    client._write_atomic(_DOC)
            self.assertFalse(path.exists(), "усечённый файл опубликован")
            self.assertEqual([p.name for p in Path(tmp).glob(".ledger_tmp_*")], [])

    def test_file_and_directory_are_fsynced(self):
        calls: list[tuple[str, int]] = []
        real_fsync = os.fsync
        real_replace = os.replace

        def spy_fsync(fd):
            calls.append(("fsync", fd))
            return real_fsync(fd)

        def spy_replace(src, dst):
            calls.append(("replace", -1))
            return real_replace(src, dst)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            client = LedgerClient("ear", path=path)
            with patch("backend.memory_ledger.os.fsync", side_effect=spy_fsync), patch(
                "backend.memory_ledger.os.replace", side_effect=spy_replace
            ):
                client._write_atomic(_DOC)
            kinds = [k for k, _ in calls]
            self.assertIn("replace", kinds)
            ri = kinds.index("replace")
            self.assertIn("fsync", kinds[:ri], "файл не сброшен на диск до replace")
            self.assertIn("fsync", kinds[ri + 1 :], "каталог не сброшен на диск после replace")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), _DOC)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
