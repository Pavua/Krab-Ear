"""Пассивное чтение через настоящий Unix IPC, owned profile и CLI seam."""
import os
from pathlib import Path
import tempfile
import unittest

from test_plaintext_export_integration import IsolatedBackend


class TestPassiveHistoryKeyIPC(unittest.TestCase):
    def test_cold_read_missing_key_then_restore_same_backend(self):
        with tempfile.TemporaryDirectory(prefix="ear-key-", dir="/tmp") as directory:
            root = Path(directory).resolve()
            key = os.urandom(32)
            backend = IsolatedBackend(root, crypto_key=key)
            try:
                backend.start()
                seeded = backend.call("add_history_item", text="passive-key-synthetic")
                self.assertTrue(seeded["ok"])
            finally:
                backend.close()

            backend = IsolatedBackend(root, crypto_key=key, crypto_key_missing=True)
            try:
                backend.start()
                profile = root / "profile"
                before = {path.name: path.read_bytes() for path in profile.iterdir()
                          if path.is_file() and (path.name.startswith("history") or path.name == "settings.json")}
                failed = backend.call("get_history_page", limit=10)
                self.assertFalse(failed["ok"])
                self.assertEqual(backend.control("crypto_metadata")["provider_creates"], 0)
                self.assertTrue(backend.control("restore_history_key"))
                loaded = backend.call("get_history_page", limit=10)
                self.assertTrue(loaded["ok"])
                self.assertEqual([item["id"] for item in loaded["result"]["items"]],
                                 [seeded["result"]["id"]])
                self.assertEqual(backend.control("crypto_metadata")["provider_creates"], 0)
                after = {path.name: path.read_bytes() for path in profile.iterdir()
                         if path.is_file() and (path.name.startswith("history") or path.name == "settings.json")}
                self.assertEqual(after, before)
            finally:
                backend.close()


if __name__ == "__main__":
    unittest.main()
