"""R1: флаг шифрования fail-closed (NOW.md 14.09).

Битый settings.json больше не означает «шифрование выключено»:
assume-enabled + громкий history.encrypt_fail в error_bus.
Отсутствующий файл — свежий профиль, остаётся False.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_store(data_dir: Path):
    from backend.state_store import StateStore
    return StateStore(data_dir)


class EncryptionFlagFailClosedTest(unittest.TestCase):
    def _bus_codes(self, bus):
        return [c.args[0].code for c in bus.push.call_args_list]

    def test_corrupt_settings_assumes_enabled_and_shouts(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            (data_dir / "settings.json").write_text("{corrupt", encoding="utf-8")
            store = _make_store(data_dir)
            bus = MagicMock()
            store._error_bus = bus
            self.assertTrue(store._read_encryption_flag_unlocked())
            self.assertIn("history.encrypt_fail", self._bus_codes(bus))

    def test_missing_settings_stays_off(self):
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(Path(td))
            bus = MagicMock()
            store._error_bus = bus
            self.assertFalse(store._read_encryption_flag_unlocked())
            self.assertNotIn("history.encrypt_fail", self._bus_codes(bus))

    def test_flag_on_but_no_keychain_shouts(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            (data_dir / "settings.json").write_text(
                json.dumps({"history_encryption_enabled": True}), encoding="utf-8")
            store = _make_store(data_dir)
            bus = MagicMock()
            store._error_bus = bus
            with patch("backend.history_crypto.build_history_crypto",
                       return_value=None):
                self.assertIsNone(store._get_history_crypto())
            self.assertIn("history.encrypt_fail", self._bus_codes(bus))


if __name__ == "__main__":
    unittest.main()
