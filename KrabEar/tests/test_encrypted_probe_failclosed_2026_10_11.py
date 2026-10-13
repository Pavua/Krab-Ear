"""has_encrypted_history_in: нечитаемый существующий журнал = «возможно зашифрован».

Раньше OSError на lstat/open молча давал `continue`: при потерянном settings.json
и нечитаемом журнале с ENC1 флаг шифрования читался как OFF и новые записи
шли открытым текстом. Теперь неизвестность трактуется как «ENC1 есть».
Отсутствующий файл (ENOENT) и не-регулярные записи по-прежнему пропускаются.
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
from backend.history_crypto import SENTINEL  # noqa: E402


class EncryptedProbeFailClosedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_missing_file_is_not_encrypted(self) -> None:
        self.assertFalse(ss.has_encrypted_history_in([self.dir / "absent.ndjson"]))

    def test_plain_file_is_not_encrypted(self) -> None:
        p = self.dir / "plain.ndjson"
        p.write_text('{"id": "1"}\n', encoding="utf-8")
        self.assertFalse(ss.has_encrypted_history_in([p]))

    def test_enc1_line_is_detected(self) -> None:
        p = self.dir / "enc.ndjson"
        p.write_text(f"{SENTINEL}abc\n", encoding="utf-8")
        self.assertTrue(ss.has_encrypted_history_in([p]))

    def test_fifo_is_skipped_without_blocking(self) -> None:
        p = self.dir / "fifo.ndjson"
        os.mkfifo(p)
        self.assertFalse(ss.has_encrypted_history_in([p]))

    def test_unreadable_existing_journal_counts_as_possibly_encrypted(self) -> None:
        p = self.dir / "locked.ndjson"
        p.write_text('{"id": "1"}\n', encoding="utf-8")
        real_open = Path.open

        def deny(self_path, *a, **kw):
            if self_path == p:
                raise PermissionError(errno.EACCES, "denied")
            return real_open(self_path, *a, **kw)

        with patch.object(Path, "open", deny):
            self.assertTrue(ss.has_encrypted_history_in([p]))

    def test_lstat_error_other_than_enoent_counts_as_possibly_encrypted(self) -> None:
        p = self.dir / "weird.ndjson"
        p.write_text("x\n", encoding="utf-8")
        real_lstat = os.lstat

        def boom(path, *a, **kw):
            if Path(path) == p:
                raise OSError(errno.EIO, "io")
            return real_lstat(path, *a, **kw)

        with patch.object(ss.os, "lstat", side_effect=boom):
            self.assertTrue(ss.has_encrypted_history_in([p]))


if __name__ == "__main__":
    unittest.main()
