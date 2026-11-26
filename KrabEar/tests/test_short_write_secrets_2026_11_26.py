"""Короткая запись (os.write < len) не должна публиковать усечённые секреты/лиз.

Класс дефекта: ENOSPC и сигналы дают частичную запись; одиночный os.write молча теряет хвост,
после чего усечённый файл публикуется (rename/link/replace) как успешный. memory_ledger и
privacy_audit это уже закрывали циклом; здесь — event_bridge токен, REST api key, brain_lease.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_REAL_WRITE = os.write


def _short_write(fd, data):
    """Пишет не больше 3 байт за вызов, как ядро при нехватке места."""
    return _REAL_WRITE(fd, bytes(data)[:3])


class ShortWriteSecretsTest(unittest.TestCase):
    def test_event_bridge_token_written_completely(self):
        from backend import event_bridge

        with tempfile.TemporaryDirectory() as d, mock.patch("os.write", _short_write):
            token = event_bridge._load_or_create_token(Path(d))
            stored = (Path(d) / "event_bridge_token").read_text(encoding="utf-8")
        self.assertEqual(stored, token)
        self.assertEqual(len(token), len(stored))

    def test_rest_api_key_create_written_completely(self):
        from backend import rest_api_key

        with tempfile.TemporaryDirectory() as d, mock.patch("os.write", _short_write):
            path = Path(d) / "rest_api_key"
            key = "k" * 48
            self.assertTrue(rest_api_key._create_atomically(path, key))
            self.assertEqual(path.read_text(encoding="utf-8"), key + "\n")

    def test_rest_api_key_replace_written_completely(self):
        from backend import rest_api_key

        with tempfile.TemporaryDirectory() as d, mock.patch("os.write", _short_write):
            path = Path(d) / "rest_api_key"
            path.write_text("short", encoding="utf-8")
            key = "z" * 48
            rest_api_key._replace_invalid(path, key)
            self.assertEqual(path.read_text(encoding="utf-8"), key + "\n")

    def test_brain_lease_payload_written_completely(self):
        from backend import brain_lease

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "lease.lock"
            path.write_text("{}", encoding="utf-8")
            fd = os.open(path, os.O_RDWR)
            payload = {"owner": "x" * 40, "exp_ts": 1.0, "acquired_ts": 0.5}
            try:
                with mock.patch("os.write", _short_write):
                    brain_lease._write_payload(fd, payload, path)
            finally:
                os.close(fd)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)

    def test_stalled_write_raises_not_loops_forever(self):
        from backend import rest_api_key

        with tempfile.TemporaryDirectory() as d, mock.patch("os.write", lambda fd, data: 0):
            path = Path(d) / "rest_api_key"
            with self.assertRaises(OSError):
                rest_api_key._create_atomically(path, "k" * 48)
            self.assertFalse(path.exists(), "усечённый ключ не опубликован")


if __name__ == "__main__":
    unittest.main()
